"""
SightMap adapter.

Research log
------------
Web sources consulted:
  - https://sightmap.com — SightMap interactive property maps (accessed 2026-04-17)
  - https://engrain.com/sightmap — Engrain SightMap product page confirming API structure
Real payloads inspected (from data/runs/*/raw_api/):
  - 268836 (Hawthorne at Traditions) — sightmap.com/app/api/v1/rxwjj7ldw1e/sightmaps/80671
    amenities-only response (no units in this endpoint capture)
  - 256856 (Vive) — sightmap.com/app/api/v1/5evek1d2vqo/sightmaps/103868
    amenities-only response (same pattern)
  - 283726 — sightmap.com/app/api/v1/... amenities endpoint
Key findings:
  - API endpoint: sightmap.com/app/api/v1/{client_key}/sightmaps/{sightmap_id}
  - Response envelope: data.units[] joined to data.floor_plans[] by floor_plan_id
  - Unit fields: price (number), display_price (string), area (number), display_area,
    unit_number, label, floor_id, building, available_on, display_available_on,
    specials_description
  - Floor plan fields: id, name, filter_label, bedroom_count, bathroom_count
  - Known gotchas: The /sightmaps/ endpoint can return amenities-only when the
    property map is configured without unit data. When units[] exists, SightMap
    only lists leasable (available) inventory — all units are status AVAILABLE.
    Parser ported from scripts/entrata.py:433 (_parse_sightmap_payload).
    - 2026-04-19 fix: removed "sightmap.com" URL filter from extract().
      lasvegasliving.com (Summer Winds, Madera) proxies SightMap data through
      its own CDN — no sightmap.com in the response URL. Replaced with
      _is_sightmap_response() body-shape check so any domain serving
      SightMap-shaped JSON is matched.
    - 2026-04-19: added three-way error differentiation: SIGHTMAP_NO_RESPONSE
      vs SIGHTMAP_AMENITIES_ONLY vs SIGHTMAP_PARSE_FAILED.
    - 2026-04-20 fix: structured failure tier codes (NO_RESPONSE /
      SHAPE_REJECTED / AMENITIES_ONLY / PARSE_FAILED) plus SIGHTMAP_PARTIAL_JOIN
      warning when >20% of units cannot be joined to a floor plan. Tightened
      _is_sightmap_response so a bare ``data.amenities`` array no longer
      false-matches as SightMap.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


# 2026-05-13 port (cluster #5 SHAPE_REJECTED recovery): broadened embed
# URL recognition. Pre-port the iframe-only regex returned [] on Fancybox
# lazy-load anchors (``<a data-src="...">``), Engrain SDK JS
# (``var EngrainedUrl = '...';``), and plain ``<a href>`` anchors that
# carry the embed URL. Live-verified 2026-05-20 on griffisresidential,
# cambridgeondevonshireapartments, soltrafirewheel. The broadened regex
# matches the embed URL in any DOM context; ``find_sightmap_embed_codes``
# below filters reserved infra segments (embed/api, embed/app,
# embed/admin) and JSON-value position so config blobs don't false-match.
_SIGHTMAP_EMBED_URL_RE = re.compile(
    r"(?:https?:)?//(?:[a-z0-9-]+\.)?sightmap\.com/embed/([a-zA-Z0-9_-]{4,32})",
    re.IGNORECASE,
)

# The SightMap embed page's bootstrap config holds the real API URL in
# ``window.__APP_CONFIG__.sightmaps[*].href``. The href is JSON-encoded
# (forward-slashes escaped as ``\/``) so the regex tolerates both forms.
_SIGHTMAP_APP_CONFIG_HREF_RE = re.compile(
    r'"href"\s*:\s*"(https?:[\\/]+sightmap\.com[\\/]+app[\\/]+api[\\/]+v1[\\/]+'
    r'[a-z0-9]+[\\/]+sightmaps[\\/]+\d+)"',
    re.IGNORECASE,
)

# 2026-05-13 port (C7 SightMap Angular SPA): when the SightMap widget is
# wrapped in an Angular SPA (e.g. equityapartments.com), the iframe
# materialises AFTER fetch_result.body is captured. But the direct API URL
# ``sightmap.com/app/api/v1/{client_key}/sightmaps/{sightmap_id}`` often
# appears inline in the Angular bundle, hardcoded as a config value.
# Recovers ~70% of SHAPE_REJECTED cases that would otherwise need an
# extended Playwright wait. Tolerates JSON-escaped (``\/``) forms.
_SIGHTMAP_DIRECT_API_RE = re.compile(
    r'https?:[\\/]+(?:[a-z0-9-]+\.)?sightmap\.com[\\/]+app[\\/]+api[\\/]+v1'
    r'[\\/]+[a-z0-9_-]+[\\/]+sightmaps[\\/]+\d+(?:[\\/]?[a-zA-Z0-9_-]*)?',
    re.IGNORECASE,
)


# 2026-04-20: structured tier codes mirror the RentCafe pattern. Each
# failure mode gets its own tier label so reporting can split misrouted
# properties (e.g. Vegas TouchTour sites that aren't actually SightMap) from
# genuine empty inventory or genuine field-name drift.
_TIER_BASE = "TIER_1_API_SIGHTMAP"
_TIER_NO_RESPONSE = f"{_TIER_BASE}_NO_RESPONSE"
_TIER_SHAPE_REJECTED = f"{_TIER_BASE}_SHAPE_REJECTED"
_TIER_AMENITIES_ONLY = f"{_TIER_BASE}_AMENITIES_ONLY"
_TIER_PARSE_FAILED = f"{_TIER_BASE}_PARSE_FAILED"

# Threshold above which a partial parse triggers a SIGHTMAP_PARTIAL_JOIN
# warning even on a successful extract. 20% chosen because at ~64.9% missing-
# rent rate observed on TIER_1_API scrapes (04-20 report), even a 20% silent
# loss is enough to make the upstream signal wrong.
_PARTIAL_JOIN_FRACTION = 0.2


def parse_sightmap_payload(body: Any, url: str) -> tuple[list[dict[str, str]], int]:
    """SightMap dedicated parser.

    Joins data.units[] to data.floor_plans[] by floor_plan_id so each unit
    gets name/beds/baths from its floor plan plus price/sqft/availability.

    2026-05-22 Phase 2a: also surfaces floor_plans with no available units
    as plan-only rows (``unit_number=""``) so the floor plan identity
    appears in the property's ``floor_plans[]`` output. Pre-fix, plans
    with zero available units silently vanished — only the units list
    drove the output, and SightMap properties often have plans where the
    inventory is fully leased while the plan itself is still advertised
    on the marketing site.

    Returns a (rows, dropped_count) tuple. ``rows`` mixes unit-level and
    plan-level rows; ``post_process.classify()`` partitions them via the
    ``unit_number`` field. ``dropped_count`` is the number of raw units
    that could not be joined to a floor plan and were silently skipped —
    the caller raises ``SIGHTMAP_PARTIAL_JOIN`` when this exceeds 20% of
    the input.

    Ported from scripts/entrata.py:433.
    """
    rows_out: list[dict[str, str]] = []
    dropped = 0
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return rows_out, dropped

    raw_units = data.get("units") or []
    raw_fps = data.get("floor_plans") or []
    if not isinstance(raw_units, list):
        raw_units = []

    fp_by_id: dict[str, dict[str, Any]] = {}
    for fp in raw_fps if isinstance(raw_fps, list) else []:
        if isinstance(fp, dict) and fp.get("id") is not None:
            fp_by_id[str(fp["id"])] = fp

    # Track which floor plans contributed at least one emitted unit row.
    # Plans absent from this set become plan-only rows (Phase 2a).
    covered_fp_ids: set[str] = set()

    for u in raw_units:
        if not isinstance(u, dict):
            continue
        fp_id = str(u.get("floor_plan_id") or "")
        if fp_id not in fp_by_id:
            # Unit cannot be joined to a floor plan — skip. The extract()
            # caller surfaces this as SIGHTMAP_PARSE_FAILED (or the partial-
            # join warning when only a fraction is dropped) so field-name
            # drift is diagnosable rather than silently emitting stub records.
            dropped += 1
            continue
        fp = fp_by_id[fp_id]

        price = u.get("price")
        price_i: int | None = None
        if isinstance(price, (int, float)) and price > 0:
            price_i = int(price)
        else:
            price_i = money_to_int(str(u.get("display_price") or ""))

        area = u.get("area")
        if isinstance(area, (int, float)) and area > 0:
            sqft = str(int(area))
        else:
            sqft = str(u.get("display_area") or "").strip()

        beds = fp.get("bedroom_count")
        baths = fp.get("bathroom_count")
        name = fp.get("name") or fp.get("filter_label") or ""

        rows_out.append(
            make_unit_dict(
                floor_plan_name=str(name),
                bed_label=bed_label_from(beds, str(name)),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths) if baths is not None else "",
                sqft=sqft,
                unit_number=str(u.get("unit_number") or u.get("label") or ""),
                floor=str(u.get("floor_id") or ""),
                building=str(u.get("building") or ""),
                rent_range=f"${price_i:,}" if price_i else str(u.get("display_price") or ""),
                concession=str(u.get("specials_description") or ""),
                availability_status="AVAILABLE",
                available_units="1",
                availability_date=str(u.get("available_on") or u.get("display_available_on") or ""),
                source_api_url=url,
                extraction_tier=_TIER_BASE,
            )
        )
        covered_fp_ids.add(fp_id)

    # 2026-05-22 Phase 2a — plans-without-units → plan_summary rows.
    # SightMap floor_plan objects carry plan-level price/area summaries that
    # the marketing site renders even when zero units are available. Surface
    # them so ``floor_plans[]`` reflects the full plan inventory rather than
    # just the available-now subset.
    for fp_id, fp in fp_by_id.items():
        if fp_id in covered_fp_ids:
            continue  # dup-prevention: plan covered by a unit row already
        name = fp.get("name") or fp.get("filter_label") or ""
        beds = fp.get("bedroom_count")
        baths = fp.get("bathroom_count")

        # Plan-level rent — SightMap exposes ``lowest_rent`` / ``highest_rent``
        # on the floor_plan object (verified against captured bodies); fall
        # through to display_price when only the range string is present.
        rent_lo: int | None = None
        rent_hi: int | None = None
        if isinstance(fp.get("lowest_rent"), (int, float)) and fp["lowest_rent"] > 0:
            rent_lo = int(fp["lowest_rent"])
        else:
            rent_lo = money_to_int(str(fp.get("display_lowest_rent") or ""))
        if isinstance(fp.get("highest_rent"), (int, float)) and fp["highest_rent"] > 0:
            rent_hi = int(fp["highest_rent"])
        else:
            rent_hi = money_to_int(str(fp.get("display_highest_rent") or "")) or rent_lo

        # Plan-level sqft — ``area_min`` / ``area_max`` ints, or the
        # ``display_area`` range string.
        sqft_lo_raw = fp.get("area_min") or fp.get("min_area")
        sqft_hi_raw = fp.get("area_max") or fp.get("max_area")
        if (isinstance(sqft_lo_raw, (int, float)) and sqft_lo_raw > 0):
            if (isinstance(sqft_hi_raw, (int, float)) and sqft_hi_raw > 0
                    and int(sqft_hi_raw) != int(sqft_lo_raw)):
                sqft = f"{int(sqft_lo_raw)}-{int(sqft_hi_raw)}"
            else:
                sqft = str(int(sqft_lo_raw))
        else:
            sqft = str(fp.get("display_area") or "").strip()

        rows_out.append(
            make_unit_dict(
                floor_plan_name=str(name),
                bed_label=bed_label_from(beds, str(name)),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths) if baths is not None else "",
                sqft=sqft,
                unit_number="",  # empty → post_process routes to plan_summaries
                rent_low=rent_lo,
                rent_high=rent_hi,
                availability_status="UNKNOWN",
                source_api_url=url,
                extraction_tier=_TIER_BASE,
            )
        )

    return rows_out, dropped


def _is_sightmap_response(body: Any) -> bool:
    """Return True if *body* looks like a SightMap API response.

    Matches on body shape rather than source URL so that portal sites
    (e.g. lasvegasliving.com) that proxy SightMap data through their own
    CDN domain are handled correctly.

    Positive match criteria (any one sufficient):
    - body["data"]["sightmap_id"] is present (explicit SightMap identifier)
    - body["data"]["floor_plans"] is a non-empty list whose first entry has
      SightMap-specific keys (bedroom_count / bathroom_count / filter_label)
    - body["data"] has BOTH "units" and "floor_plans"

    The 2026-04-20 fix tightens the prior loose check that matched any CMS
    with a ``data.amenities`` array — a positive shape match must now show
    SightMap-specific structure, not just an amenities list.
    """
    if not isinstance(body, dict):
        return False
    data = body.get("data")
    if not isinstance(data, dict):
        return False
    if "sightmap_id" in data:
        return True
    fps = data.get("floor_plans")
    if isinstance(fps, list) and fps and isinstance(fps[0], dict):
        sightmap_fp_keys = {"bedroom_count", "bathroom_count", "filter_label"}
        if sightmap_fp_keys & set(fps[0].keys()):
            return True
    if "units" in data and "floor_plans" in data:
        return True
    return False


class SightMapAdapter:
    """SightMap PMS adapter. Parses sightmap.com API responses."""

    pms_name: str = "sightmap"
    _fingerprints: list[str] = ["sightmap.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from SightMap API responses captured during page load."""
        from ma_poc.pms.adapters._adapter_telemetry import log_adapter_stage

        pid_for_log = str(getattr(ctx, "property_id", "") or "unknown")
        result = AdapterResult(tier_used=_TIER_BASE)
        all_units: list[dict[str, str]] = []
        # Aggregate across all matched responses so the partial-join check
        # is computed against the run as a whole rather than per-response.
        total_raw_units = 0
        total_dropped = 0
        n_matched_shape = 0

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        for resp in api_responses:
            url = resp.get("url", "")
            body = resp.get("body")
            if not isinstance(body, dict):
                continue
            if not _is_sightmap_response(body):
                continue
            n_matched_shape += 1
            data = body.get("data") or {}
            raw_units_list = data.get("units") if isinstance(data, dict) else None
            if isinstance(raw_units_list, list):
                total_raw_units += len(raw_units_list)
            units, dropped = parse_sightmap_payload(body, url)
            total_dropped += dropped
            if units:
                all_units.extend(units)
                result.api_responses.append(resp)

        log_adapter_stage(
            "sightmap",
            pid_for_log,
            "xhr_capture",
            "ok" if all_units else "no_shape_match" if api_responses else "no_api_responses",
            units=len(all_units),
            reason=(
                f"network_responses={len(api_responses)} matched_shape={n_matched_shape} "
                f"total_raw_units={total_raw_units} dropped={total_dropped}"
            ),
            n_responses=len(api_responses),
            matched_shape=n_matched_shape,
            total_raw_units=total_raw_units,
            join_dropped=total_dropped,
        )

        if all_units:
            # Stage 1 validity gate — drops dim-less rows before they leak
            # into properties.json. Lazy import: see RentCafe adapter for the
            # cycle-break rationale.
            from ma_poc.extraction.post_process import post_process

            _pp_parsed = len(all_units)
            _pp = post_process(all_units, property_id=getattr(ctx, "property_id", None))
            if _pp.n_admitted > 0:
                # D16: strict unit-level / plan-level partition.
                result.units = list(_pp.units)
                result.plan_summaries = list(_pp.plan_summaries)
                result.post_process_meta = _pp.to_meta()
                result.winning_url = (
                    result.api_responses[0].get("url") if result.api_responses else None
                )
                result.confidence = min(0.95, 0.7 + 0.05 * _pp.n_admitted)
                result.tier_used = _TIER_BASE
                # Even on success, surface silent unit-level loss when the
                # SightMap-internal join rate drops below the 80% floor.
                # ``total_raw_units`` / ``total_dropped`` track join-time
                # losses upstream of the validity gate, so the percentage
                # is independent of the validity filtering above.
                if total_raw_units > 0 and total_dropped > _PARTIAL_JOIN_FRACTION * total_raw_units:
                    result.errors.append(
                        f"SIGHTMAP_PARTIAL_JOIN: {total_dropped} of {total_raw_units} "
                        f"units could not be joined to a floor plan "
                        f"({total_dropped / total_raw_units:.0%} silently dropped) — "
                        "inspect floor_plan_id field on dropped units for drift"
                    )
                return result
            # Parsed N rows but every one failed unit-validity (no numeric
            # dimension). Record and fall through to failure classification.
            result.errors.append(
                f"SIGHTMAP_VALIDITY_REJECTED: {_pp_parsed} parsed rows "
                f"failed unit_validity (no numeric dimension)"
            )

        # 2026-05-13 port: SHAPE_REJECTED iframe fallback. Before
        # falling all the way through to the structured failure
        # classification below, try the embed-code / direct-API
        # discovery path against the entry HTML. Covers ~70% of
        # SHAPE_REJECTED cases (Angular-SPA + Fancybox + Engrain).
        # Skipped when api_responses is non-empty AND already contains
        # a sightmap-shaped body -- in that case the real failure is
        # PARSE_FAILED / AMENITIES_ONLY, not "we never reached
        # SightMap at all".
        sightmap_responses_pre = [
            r
            for r in api_responses
            if isinstance(r.get("body"), dict) and _is_sightmap_response(r.get("body"))
        ]
        if not sightmap_responses_pre and not all_units:
            iframe_units = await _try_sightmap_iframe_fallback(ctx, result)
            if iframe_units:
                from ma_poc.extraction.post_process import post_process

                _pp_ifr = post_process(
                    iframe_units, property_id=getattr(ctx, "property_id", None)
                )
                if _pp_ifr.n_admitted > 0:
                    result.units = list(_pp_ifr.units)
                    result.plan_summaries = list(_pp_ifr.plan_summaries)
                    result.post_process_meta = _pp_ifr.to_meta()
                    # Keep the base tier label -- fallback recovery is
                    # accounted for in result.api_responses[*].via.
                    result.tier_used = _TIER_BASE
                    result.confidence = min(0.90, 0.7 + 0.04 * _pp_ifr.n_admitted)
                    return result

        # Failure path: classify via structured sub-codes mirroring the RentCafe
        # adapter pattern.
        result.confidence = 0.0
        sightmap_responses = [
            r
            for r in api_responses
            if isinstance(r.get("body"), dict) and _is_sightmap_response(r.get("body"))
        ]
        if not api_responses:
            result.tier_used = _TIER_NO_RESPONSE
            result.errors.append("SIGHTMAP_NO_RESPONSE: no network responses captured during page load")
        elif not sightmap_responses:
            result.tier_used = _TIER_SHAPE_REJECTED
            result.errors.append(
                f"SIGHTMAP_SHAPE_REJECTED: {len(api_responses)} responses captured, "
                "none matched SightMap envelope (data.{units|floor_plans|sightmap_id})"
            )
        else:
            # Some shape-matched responses but extraction emitted zero units.
            saw_units = False
            for r in sightmap_responses:
                data = (r.get("body") or {}).get("data") or {}
                raw_units = data.get("units") if isinstance(data, dict) else None
                if isinstance(raw_units, list) and raw_units:
                    saw_units = True
                    result.tier_used = _TIER_PARSE_FAILED
                    result.errors.append(
                        f"SIGHTMAP_PARSE_FAILED: units[] present ({len(raw_units)} entries) "
                        f"but join produced 0 records — field name mismatch likely; "
                        f"inspect raw_api payload for {str(r.get('url', '?'))[:80]}"
                    )
                else:
                    if not saw_units:
                        result.tier_used = _TIER_AMENITIES_ONLY
                    result.errors.append(
                        f"SIGHTMAP_AMENITIES_ONLY: sightmap response at "
                        f"{str(r.get('url', '?'))[:80]} "
                        "has no units[] — map may be configured as amenities-only; "
                        "check for a separate /available or /assets endpoint"
                    )

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    def matches_response_body(self, body: Any) -> bool:
        """Body-shape check used by ``detector.confirm_detection``.

        Returns True if *body* plausibly belongs to SightMap. Reuses the
        adapter's own envelope check so router and parser stay in sync.
        """
        return _is_sightmap_response(body)


# ────────────────────────────────────────────────────────────────────
# 2026-05-13 port (Commit 7 of MAY13_API_TIER_PORT_PLAN.md):
# SHAPE_REJECTED recovery via embed-code + direct-API discovery.
# Helper functions are exposed at module scope so the adapter and any
# future iframe-fallback orchestrator can call them; tests exercise
# them in isolation.
# ────────────────────────────────────────────────────────────────────


def _entry_html_from_ctx(ctx: AdapterContext) -> str | None:
    """Decode the entry-page HTML off ``ctx.fetch_result.body``.

    Returns ``None`` when no body is available. Bytes are UTF-8 decoded
    with the ``replace`` error handler. Never raises.
    """
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None) if fr is not None else None
    if isinstance(body, bytes):
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return None
    if isinstance(body, str):
        return body
    return None


def find_sightmap_embed_codes(html: str) -> list[str]:
    """Return SightMap embed codes (deduped) found anywhere in *html*.

    Accepts the embed URL in any *loading-shaped* DOM context --
    ``<iframe src=>``, ``<a data-src=>`` (Fancybox lightbox lazy-loading),
    ``<a href=>``, ``var EngrainedUrl = '...'`` JS assignments, etc. The
    embed-code-bearing URL is distinctive enough on its own that the
    surrounding element type isn't a useful filter.

    Skips matches in JSON-value position (preceded by ``":`` or ``": "``).
    Those are config blobs the adapter can't act on directly. Reserved
    infra segments (embed/api, embed/app, embed/admin, embed/embed) are
    filtered -- these are SightMap's internal routes, never customer
    embed codes.
    """
    if not html or "sightmap.com" not in html.lower():
        return []
    seen: set[str] = set()
    codes: list[str] = []
    for m in _SIGHTMAP_EMBED_URL_RE.finditer(html):
        # Skip JSON-value position. Real cluster #5 forms preceding
        # the URL:
        #   data-src="...                -> preceded by `="`
        #   var EngrainedUrl = '...'     -> preceded by `= '`
        #   src=...                      -> preceded by `=`
        # JSON-value form preceding the URL:
        #   "embed_url":"...             -> preceded by `":"`
        # Walk back over an optional whitespace + quote, then check
        # for a colon. Distinguishes ``":"https://...`` (skip) from
        # ``="https://...`` (keep) without false-positives.
        start = m.start()
        i = start - 1
        if i >= 0 and html[i] in "'\"":
            i -= 1
        while i >= 0 and html[i] in " \t":
            i -= 1
        if i >= 0 and html[i] == ":":
            continue

        code = m.group(1)
        # Reserved infra segments -- never customer codes.
        if code.lower() in {"embed", "app", "admin", "api"}:
            continue
        if code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def extract_sightmap_api_url(embed_html: str) -> str | None:
    """Pull the SightMap API URL out of an embed page's bootstrap config.

    Looks for ``window.__APP_CONFIG__.sightmaps[*].href``. Returns ``None``
    when the regex doesn't match. JSON-escaped forward slashes are
    normalised to ``/``.
    """
    if not embed_html:
        return None
    m = _SIGHTMAP_APP_CONFIG_HREF_RE.search(embed_html)
    if not m:
        return None
    return m.group(1).replace("\\/", "/")


def find_sightmap_direct_api_urls(html: str) -> list[str]:
    """Pull direct SightMap API URLs (deduped) found inline in *html*.

    URL form: ``sightmap.com/app/api/v1/{client}/sightmaps/{id}``. Used
    by the Angular-SPA fallback path (C7 2026-05-13) when the embed
    iframe has not yet materialised but the Angular bundle has the API
    URL inline.
    """
    if not html or "sightmap.com" not in html.lower():
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _SIGHTMAP_DIRECT_API_RE.finditer(html):
        url = m.group(0).replace("\\/", "/")
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


async def _try_sightmap_iframe_fallback(
    ctx: AdapterContext, result: AdapterResult
) -> list[dict[str, str]]:
    """SHAPE_REJECTED recovery: follow the iframe URL ourselves.

    Two passes:

    Pass 1 -- direct API URLs inline in HTML (Angular SPA bundle
        pattern; faster than the embed-page indirection and works when
        the iframe hasn't yet rendered).

    Pass 2 -- embed-iframe fallback: fetch the embed page, extract the
        API URL from ``window.__APP_CONFIG__``, fetch the API.

    Returns an empty list on any failure (silent; the caller continues
    to the normal failure-classification path). Appends a result entry
    on success so api_responses bookkeeping reflects the recovery.
    """
    html = _entry_html_from_ctx(ctx)
    if not html:
        return []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/json,*/*;q=0.8",
    }

    # Pass 1: direct API URLs inline in HTML.
    direct_urls = find_sightmap_direct_api_urls(html)
    if direct_urls:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
                for api_url in direct_urls[:3]:
                    try:
                        ar = await c.get(api_url, headers=headers)
                    except Exception:
                        continue
                    if ar.status_code != 200:
                        continue
                    try:
                        body = ar.json()
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(body, dict) or not _is_sightmap_response(body):
                        continue
                    units, _dropped = parse_sightmap_payload(body, api_url)
                    if units:
                        result.api_responses.append({
                            "url": api_url,
                            "status": 200,
                            "body": body,
                            "via": "direct_api_fallback",
                        })
                        result.winning_url = api_url
                        return units
        except Exception as exc:
            result.errors.append(
                f"sightmap-direct-api-fallback-error: "
                f"{type(exc).__name__}: {str(exc)[:120]}"
            )

    # Pass 2: embed-iframe fallback.
    codes = find_sightmap_embed_codes(html)
    if not codes:
        return []
    embed_code = codes[0]
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
            embed_url = f"https://sightmap.com/embed/{embed_code}"
            er = await c.get(embed_url, headers=headers)
            if er.status_code != 200 or not er.text:
                return []
            api_url = extract_sightmap_api_url(er.text)
            if not api_url:
                return []
            ar = await c.get(api_url, headers=headers)
            if ar.status_code != 200:
                return []
            try:
                body = ar.json()
            except (json.JSONDecodeError, ValueError):
                return []
        if not isinstance(body, dict) or not _is_sightmap_response(body):
            return []
        units, _dropped = parse_sightmap_payload(body, api_url)
        if units:
            result.api_responses.append({
                "url": api_url,
                "status": 200,
                "body": body,
                "via": "iframe_fallback",
            })
            result.winning_url = api_url
        return units
    except Exception as exc:
        result.errors.append(
            f"sightmap-iframe-fallback-error: embed={embed_code!r} "
            f"{type(exc).__name__}: {str(exc)[:120]}"
        )
        return []
