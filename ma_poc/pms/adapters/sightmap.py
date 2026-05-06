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
    - 2026-05-06: added direct-fetch fallback. When the existing
      ctx._api_responses loop yields no units AND the page HTML contains a
      ``sightmap.com/embed/{hashid}`` reference, fetch the embed → parse
      ``window.__APP_CONFIG__.sightmaps[0].href`` → fetch the JSON API and
      run it through ``parse_sightmap_payload``. Bulk scan of 81 still-failing
      properties showed 9 of 81 (11%) had a real SightMap embed on a sub-page
      that the resolver/iframe-load timing didn't surface as an XHR.
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


# 2026-04-20: structured tier codes mirror the RentCafe pattern. Each
# failure mode gets its own tier label so reporting can split misrouted
# properties (e.g. Vegas TouchTour sites that aren't actually SightMap) from
# genuine empty inventory or genuine field-name drift.
_TIER_BASE = "TIER_1_API_SIGHTMAP"
_TIER_DIRECT_FETCH = f"{_TIER_BASE}_DIRECT_FETCH"
_TIER_NO_RESPONSE = f"{_TIER_BASE}_NO_RESPONSE"
_TIER_SHAPE_REJECTED = f"{_TIER_BASE}_SHAPE_REJECTED"
_TIER_AMENITIES_ONLY = f"{_TIER_BASE}_AMENITIES_ONLY"
_TIER_PARSE_FAILED = f"{_TIER_BASE}_PARSE_FAILED"

# Embed shape: https://sightmap.com/embed/{hashid}. The hashid is 6+ chars of
# [a-z0-9]; tested embeds in the 2026-05-06 dive ranged 9–13 chars.
_EMBED_RE = re.compile(r"https?://(?:www\.)?sightmap\.com/embed/([a-z0-9]{6,})", re.IGNORECASE)
# WordPress ECS-Spaces plugin emits the embed URL inside a JSON config blob
# with escaped slashes — `"sightmap_url":"https:\/\/sightmap.com\/embed\/...".
_EMBED_ESCAPED_RE = re.compile(
    r"sightmap_url[\"']?\s*:\s*[\"']https?:\\?/\\?/(?:www\.)?sightmap\.com\\?/embed\\?/([a-z0-9]{6,})",
    re.IGNORECASE,
)
# `__APP_CONFIG__` is set as a literal `window.__APP_CONFIG__ = {...}` JS
# assignment inside the embed page. Capture the JSON object.
_APP_CONFIG_RE = re.compile(
    r"window\.__APP_CONFIG__\s*=\s*(\{.*?\})\s*\n",
    re.DOTALL,
)

# Threshold above which a partial parse triggers a SIGHTMAP_PARTIAL_JOIN
# warning even on a successful extract. 20% chosen because at ~64.9% missing-
# rent rate observed on TIER_1_API scrapes (04-20 report), even a 20% silent
# loss is enough to make the upstream signal wrong.
_PARTIAL_JOIN_FRACTION = 0.2


def parse_sightmap_payload(body: Any, url: str) -> tuple[list[dict[str, str]], int]:
    """SightMap dedicated parser.

    Joins data.units[] to data.floor_plans[] by floor_plan_id so each unit
    gets name/beds/baths from its floor plan plus price/sqft/availability.

    Returns a (units, dropped_count) tuple. ``dropped_count`` is the number of
    raw units that could not be joined to a floor plan and were silently
    skipped — the caller raises ``SIGHTMAP_PARTIAL_JOIN`` when this exceeds
    20% of the input. Surfacing this prevents the 04-20 failure mode where
    "successful" SightMap scrapes silently lost the majority of inventory due
    to a floor_plan_id key drift on the SightMap side.

    Ported from scripts/entrata.py:433.
    """
    units_out: list[dict[str, str]] = []
    dropped = 0
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return units_out, dropped

    raw_units = data.get("units") or []
    raw_fps = data.get("floor_plans") or []
    if not isinstance(raw_units, list) or not raw_units:
        return units_out, dropped

    fp_by_id: dict[str, dict[str, Any]] = {}
    for fp in raw_fps if isinstance(raw_fps, list) else []:
        if isinstance(fp, dict) and fp.get("id") is not None:
            fp_by_id[str(fp["id"])] = fp

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

        units_out.append(
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
    return units_out, dropped


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


def _ctx_html(ctx: AdapterContext) -> str | None:
    """Pull raw HTML out of ``ctx.fetch_result.body`` (bytes or str).

    Used by the direct-fetch fallback to scan for SightMap embed URLs without
    needing a live Playwright page handle. Returns None if no body is present.
    """
    fr = getattr(ctx, "fetch_result", None)
    if fr is None:
        return None
    body = getattr(fr, "body", None)
    if body is None:
        return None
    if isinstance(body, bytes):
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return None
    if isinstance(body, str):
        return body
    return None


def _find_embed_hashid(html: str) -> str | None:
    """First ``sightmap.com/embed/{hashid}`` reference found in *html*.

    Checks both forward-slash and escaped-slash variants — Engrain's WordPress
    plugin emits the URL JSON-escaped inside `spacesConfig`.
    """
    if not html:
        return None
    m = _EMBED_RE.search(html)
    if m:
        return m.group(1).lower()
    m = _EMBED_ESCAPED_RE.search(html)
    if m:
        return m.group(1).lower()
    return None


async def fetch_sightmap_units_direct(
    embed_hashid: str,
    *,
    timeout: float = 12.0,
) -> tuple[list[dict[str, str]], str, list[str]]:
    """Direct-fetch path: embed → __APP_CONFIG__ → JSON API → parsed units.

    Returns ``(units, source_url, errors)``. ``units`` is empty on any failure;
    ``errors`` carries a human-readable diagnostic so the caller can surface
    why a property failed.

    Steps:
    1. GET ``https://sightmap.com/embed/{embed_hashid}`` — small HTML shell
       containing ``window.__APP_CONFIG__`` JSON.
    2. Parse the JSON; pick ``sightmaps[0].href`` (the per-property API URL).
    3. GET that API URL with ``--compressed`` semantics (httpx auto-handles).
       Body-shape-check it via ``_is_sightmap_response``.
    4. Run ``parse_sightmap_payload`` and return the units.

    The function only does network I/O — no logging, no metrics. Caller wires
    the result into the AdapterResult tier_used / errors / api_responses.
    """
    import httpx  # lazy: keeps adapter import cheap when not exercised

    errors: list[str] = []
    embed_url = f"https://sightmap.com/embed/{embed_hashid}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/html;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            embed_resp = await client.get(embed_url, headers=headers)
            if embed_resp.status_code != 200:
                errors.append(
                    f"SIGHTMAP_DIRECT_FETCH_EMBED_HTTP_{embed_resp.status_code}: {embed_url}"
                )
                return [], embed_url, errors

            embed_html = embed_resp.text
            cfg_match = _APP_CONFIG_RE.search(embed_html)
            if not cfg_match:
                errors.append(
                    f"SIGHTMAP_DIRECT_FETCH_NO_APP_CONFIG: embed at {embed_url} "
                    "lacks window.__APP_CONFIG__ assignment"
                )
                return [], embed_url, errors

            try:
                cfg = json.loads(cfg_match.group(1))
            except json.JSONDecodeError as exc:
                errors.append(
                    f"SIGHTMAP_DIRECT_FETCH_BAD_APP_CONFIG: {exc}; embed={embed_url}"
                )
                return [], embed_url, errors

            sightmaps = cfg.get("sightmaps") if isinstance(cfg, dict) else None
            if not isinstance(sightmaps, list) or not sightmaps:
                errors.append(
                    f"SIGHTMAP_DIRECT_FETCH_NO_SIGHTMAPS: __APP_CONFIG__ has no sightmaps[]; embed={embed_url}"
                )
                return [], embed_url, errors

            first = sightmaps[0]
            api_url = first.get("href") if isinstance(first, dict) else None
            if not isinstance(api_url, str) or not api_url:
                errors.append(
                    f"SIGHTMAP_DIRECT_FETCH_NO_HREF: sightmaps[0] has no href; embed={embed_url}"
                )
                return [], embed_url, errors

            api_resp = await client.get(api_url, headers=headers)
            if api_resp.status_code != 200:
                errors.append(
                    f"SIGHTMAP_DIRECT_FETCH_API_HTTP_{api_resp.status_code}: {api_url}"
                )
                return [], api_url, errors

            try:
                body = api_resp.json()
            except (json.JSONDecodeError, ValueError) as exc:
                errors.append(f"SIGHTMAP_DIRECT_FETCH_BAD_JSON: {exc}; api={api_url}")
                return [], api_url, errors

            if not _is_sightmap_response(body):
                errors.append(
                    f"SIGHTMAP_DIRECT_FETCH_SHAPE_REJECTED: api={api_url} "
                    "did not match SightMap envelope (data.{units|floor_plans|sightmap_id})"
                )
                return [], api_url, errors

            units, _dropped = parse_sightmap_payload(body, api_url)
            if not units:
                errors.append(
                    f"SIGHTMAP_DIRECT_FETCH_NO_UNITS: parsed envelope had no units; api={api_url}"
                )
            return units, api_url, errors
    except Exception as exc:
        # httpx exceptions, network errors, etc. — never propagate.
        errors.append(f"SIGHTMAP_DIRECT_FETCH_EXC: {type(exc).__name__}: {exc}")
        return [], embed_url, errors


class SightMapAdapter:
    """SightMap PMS adapter. Parses sightmap.com API responses."""

    pms_name: str = "sightmap"
    _fingerprints: list[str] = ["sightmap.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from SightMap API responses captured during page load."""
        result = AdapterResult(tier_used=_TIER_BASE)
        all_units: list[dict[str, str]] = []
        # Aggregate across all matched responses so the partial-join check
        # is computed against the run as a whole rather than per-response.
        total_raw_units = 0
        total_dropped = 0

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        for resp in api_responses:
            url = resp.get("url", "")
            body = resp.get("body")
            if not isinstance(body, dict):
                continue
            if not _is_sightmap_response(body):
                continue
            data = body.get("data") or {}
            raw_units_list = data.get("units") if isinstance(data, dict) else None
            if isinstance(raw_units_list, list):
                total_raw_units += len(raw_units_list)
            units, dropped = parse_sightmap_payload(body, url)
            total_dropped += dropped
            if units:
                all_units.extend(units)
                result.api_responses.append(resp)

        if all_units:
            result.units = all_units
            result.winning_url = result.api_responses[0].get("url") if result.api_responses else None
            result.confidence = min(0.95, 0.7 + 0.05 * len(all_units))
            result.tier_used = _TIER_BASE
            # Even on success, surface silent unit-level loss when the join
            # rate drops below the 80% floor.
            if total_raw_units > 0 and total_dropped > _PARTIAL_JOIN_FRACTION * total_raw_units:
                result.errors.append(
                    f"SIGHTMAP_PARTIAL_JOIN: {total_dropped} of {total_raw_units} "
                    f"units could not be joined to a floor plan "
                    f"({total_dropped / total_raw_units:.0%} silently dropped) — "
                    "inspect floor_plan_id field on dropped units for drift"
                )
            return result

        # 2026-05-06: direct-fetch fallback. Before classifying as failed,
        # scan the page HTML for a `sightmap.com/embed/{hashid}` reference and
        # fetch the embed → API directly. Bulk scan showed ~11% of failing
        # properties have a real embed URL on a sub-page that the iframe-load
        # timing race never surfaced as a captured XHR.
        html = _ctx_html(ctx)
        embed_hashid = _find_embed_hashid(html) if html else None
        if embed_hashid:
            direct_units, direct_url, direct_errors = await fetch_sightmap_units_direct(
                embed_hashid
            )
            result.errors.extend(direct_errors)
            if direct_units:
                result.units = direct_units
                result.winning_url = direct_url
                result.tier_used = _TIER_DIRECT_FETCH
                # Lower confidence ceiling than primary path (0.85 vs 0.95) to
                # signal that this came from a side-channel rather than a
                # network capture; planner can still STOP on it.
                result.confidence = min(0.85, 0.6 + 0.05 * len(direct_units))
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
