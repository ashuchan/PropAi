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

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    _unwrap_name_blob,
    bed_label_from,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

# Iframe URL: <iframe src="https://sightmap.com/embed/{embed_code}">. The
# embed_code is NOT the API client_key — the embed page itself contains the
# real API URL in window.__APP_CONFIG__.sightmaps[*].href. Pattern derived
# from 2026-05-13 probe of livesoundatpeninsulajax.com / hickoryhighlandsapts.com.
#
# IMPORTANT: must scope to actual ``<iframe src=...>`` tags. Some property
# pages embed a SightMap *config blob* inside ``<script type="application/json">``
# (e.g. Jonah Digital's sightmap-config payload) — those carry the embed URL
# as data, not as a live iframe. Firing the fallback on those would replace
# the embedded-portal-hint path that the generic adapter relies on for
# fetch-candidate surfacing. See test_portal_hint_survives_full_scrape_chain.
#
# 2026-05-20 broadening (feature_fail_1429 cluster #5): the legacy
# ``<iframe src=...>`` requirement missed the dominant real-world embed
# forms — Fancybox lazy-load anchors (``<a data-src="...">``), Engrain
# SDK JS assignments (``var EngrainedUrl = '...'``), plain anchors, and
# anywhere else the URL appears in the page text before the iframe is
# instantiated. Live-verified 2026-05-20 on griffisresidential,
# cambridgeondevonshireapartments, soltrafirewheel — iframe regex
# returned [] on all three; broadened URL regex returns the right code.
# Reserved infra segments (``embed/api``, ``embed/app``, ``embed/admin``)
# are still filtered downstream in ``find_sightmap_embed_codes``.
_SIGHTMAP_EMBED_URL_RE = re.compile(
    r"(?:https?:)?//(?:[a-z0-9-]+\.)?sightmap\.com/embed/([a-zA-Z0-9_-]{4,32})",
    re.IGNORECASE,
)
# Pull the SightMaps API URL out of the embed page's bootstrap config. The
# href is JSON-encoded (forward-slashes escaped as \/) so the regex tolerates
# both forms.
_SIGHTMAP_APP_CONFIG_HREF_RE = re.compile(
    r'"href"\s*:\s*"(https?:[\\/]+sightmap\.com[\\/]+app[\\/]+api[\\/]+v1[\\/]+'
    r'[a-z0-9]+[\\/]+sightmaps[\\/]+\d+)"',
    re.IGNORECASE,
)
# 2026-05-13 (C7 SightMap Angular SPA, teammate analysis): when the SightMap
# widget is wrapped in an Angular SPA (e.g. equityapartments.com), the iframe
# materialises AFTER the fetch_result.body is captured — but the direct API
# URL ``sightmap.com/app/api/v1/{client_key}/sightmaps/{sightmap_id}`` often
# appears inline in the Angular bundle, hardcoded as a config value. Picking
# it up directly lets the adapter recover ~70% of SHAPE_REJECTED cases that
# would otherwise need an extended Playwright wait we can't afford in the
# fetch budget. The regex tolerates both literal URLs and JSON-escaped
# (``\/``) forms.
_SIGHTMAP_DIRECT_API_RE = re.compile(
    r'https?:[\\/]+(?:[a-z0-9-]+\.)?sightmap\.com[\\/]+app[\\/]+api[\\/]+v1'
    r'[\\/]+[a-z0-9_-]+[\\/]+sightmaps[\\/]+\d+(?:[\\/]?[a-zA-Z0-9_-]*)?',
    re.IGNORECASE,
)

if TYPE_CHECKING:
    from playwright.async_api import Page


# 2026-04-20: structured tier codes mirror the RentCafe pattern. Each
# failure mode gets its own tier label so reporting can split misrouted
# properties (e.g. Vegas TouchTour sites that aren't actually SightMap) from
# genuine empty inventory or genuine field-name drift.
_TIER_BASE = "TIER_1_API_SIGHTMAP"
_TIER_NO_RESPONSE = f"{_TIER_BASE}_NO_RESPONSE"
_TIER_SHAPE_REJECTED = f"{_TIER_BASE}_SHAPE_REJECTED"
_TIER_AMENITIES_ONLY = f"{_TIER_BASE}_AMENITIES_ONLY"
_TIER_PARSE_FAILED = f"{_TIER_BASE}_PARSE_FAILED"
# 2026-05-25: every raw unit had price=null + display_price=null +
# available_on=null + display_available_on=null. The adapter used to emit
# these as AVAILABLE rows with rent_range="" — verified via canary
# deep-probe to be the largest single zero-rent cluster (2,605 rows over
# 6 properties incl. Altis Blue Lake 318/318, EON Squared 476/476, Hyde
# Park McKinney 285/285, 240 Park Avenue 204/204). New tier signals that
# the adapter saw real units but the operator publishes neither rent nor
# availability — dropped to prevent false-positive AVAILABLE rows.
_TIER_OPERATOR_RENT_NOT_PUBLISHED = f"{_TIER_BASE}_OPERATOR_RENT_NOT_PUBLISHED"

# Threshold above which a partial parse triggers a SIGHTMAP_PARTIAL_JOIN
# warning even on a successful extract. 20% chosen because at ~64.9% missing-
# rent rate observed on TIER_1_API scrapes (04-20 report), even a 20% silent
# loss is enough to make the upstream signal wrong.
_PARTIAL_JOIN_FRACTION = 0.2


def _sightmap_deposit(u: dict[str, Any]) -> str:
    """Refundable security deposit from a SightMap unit's expense breakdown.

    Deposit is not a top-level field — it lives in
    ``static_expenses[].expenses[]`` (the expense DEFINITIONS, keyed by ``id``)
    with the per-unit dollar figure in ``expense_amounts[<id>].amount``. Only
    the "Security Deposit (Refundable)" line carries a real number; the
    "Security Deposit Alternative" line is always ``"Varies"`` (skip it).

    Returns ``"$500"`` when a numeric amount is present, else ``""``. Population
    is SPARSE — verified live 2026-07-16, only a minority of SightMap
    properties publish it (anthemeverett $500; most others none) — so this
    only fills the subset that does, and never invents a value.
    """
    amounts = u.get("expense_amounts")
    if not isinstance(amounts, dict):
        return ""
    for grp in u.get("static_expenses") or []:
        if not isinstance(grp, dict):
            continue
        for e in grp.get("expenses") or []:
            if not isinstance(e, dict):
                continue
            lbl = str(e.get("label") or "").lower()
            if "deposit" in lbl and "refundable" in lbl:
                amt = (amounts.get(str(e.get("id"))) or {}).get("amount")
                if amt in (None, "", 0):
                    continue
                try:
                    return f"${int(float(str(amt).replace(',', ''))):,}"
                except (TypeError, ValueError):
                    return ""
    return ""


def _plan_name_key(raw: Any) -> str:
    """Normalised comparison key for a SightMap floor-plan name.

    SightMap ``floor_plans[].name`` is frequently a DOUBLE-ENCODED JSON string
    (``'{"name":"JRA1","provider_id":"4710554"}'``), so a raw string compare
    would never match the unwrapped name ``make_unit_dict`` writes onto the
    emitted unit rows. ``_unwrap_name_blob`` is the same helper
    ``make_unit_dict`` applies, which keeps the plan-row dedupe key aligned
    with what actually ships.

    Returns a casefolded, whitespace-collapsed key, or ``""`` when no usable
    name is present (callers must not dedupe on an empty key — an unnamed plan
    should still emit).
    """
    name = _unwrap_name_blob(raw)
    return " ".join(name.split()).casefold()


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

    seen_fp_ids: set[str] = set()
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
        seen_fp_ids.add(fp_id)

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
            # 2026-05-25 chip #10 follow-up: ``display_area`` occasionally
            # carries a literal ``-1`` (and other non-positive sentinels)
            # when the operator has not published square footage in
            # SightMap. Earlier emit-then-flag handling left these rows
            # with ``sqft="-1"`` (15 rows in TIER_1_API_SIGHTMAP_IFRAME_
            # PLAN_LEVEL). Normalise non-positive numerics here so the
            # downstream sqft=-1 cohort metric stops counting them.
            display_area_raw = str(u.get("display_area") or "").strip()
            sqft = display_area_raw
            if display_area_raw:
                try:
                    if float(display_area_raw.replace(",", "")) <= 0:
                        sqft = ""
                except ValueError:
                    pass

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
                # As-displayed label, e.g. "HOME 302" / "APT PH14". Distinct
                # from unit_number on 221/221 fixture units; the prefix is
                # operator-specific, which is why it is captured and never
                # reconstructed. Empty when SightMap omits it.
                unit_name=str(u.get("display_unit_number") or ""),
                floor=str(u.get("floor_id") or ""),
                building=str(u.get("building") or ""),
                rent_range=f"${price_i:,}" if price_i else str(u.get("display_price") or ""),
                deposit=_sightmap_deposit(u),
                concession=str(u.get("specials_description") or ""),
                availability_status="AVAILABLE",
                available_units="1",
                availability_date=str(u.get("available_on") or u.get("display_available_on") or ""),
                source_ids={
                    k: v
                    for k, v in {
                        "sightmap_unit_id": u.get("id"),
                        "sightmap_floor_plan_id": fp_id,
                    }.items()
                    if v
                },
                source_api_url=url,
                extraction_tier=_TIER_BASE,
            )
        )

    # 2026-06-27 chip: plan-level rows for floor plans with NO units in units[].
    # SightMap's units[] commonly omits sold-out / not-yet-released plans (e.g.
    # townhome HH*/TH* prefixes on Billingsley properties). Without this pass,
    # those plans never appear in the output and the property's catalogue is
    # truncated (Hudson 18/30, Hastings End 12/25, August Hills 9/15 vs
    # apartments.com ground truth, 2026-06-27 QC). Emit one row per such plan
    # marked UNAVAILABLE / available_units="0" so downstream catalogue diff
    # surfaces the full inventory while the operator's published-rent gate keeps
    # unit-level metrics unaffected.
    #
    # 2026-07-25 correction — dedupe by plan NAME, not by floor_plan id.
    # ``data.floor_plans`` is an id-keyed history, not a deduped catalogue:
    # SightMap retains superseded plan records with fresh ids and the same
    # name. Live-verified on Residences at Mazza (embed dqw97d5zvo9, sightmap
    # 92572): 293 floor_plans carry only 163 distinct names — 89 duplicate-name
    # groups covering 219 of the 293 records. Emitting one row per unjoined id
    # therefore (a) duplicated the same plan up to 3× and (b) asserted
    # UNAVAILABLE/available_units="0" for 43 plans that simultaneously shipped a
    # real AVAILABLE priced unit under a sibling id — two contradictory rows for
    # one plan. Run-wide on the 2026-07-25 5k canary that was 200 contradictory
    # rows across 82 of 505 SightMap properties.
    #
    # Both filters are name-scoped and additive: a plan whose name has no real
    # unit and has not already been emitted still produces exactly one
    # catalogue row, preserving the 2026-06-27 chip's coverage intent
    # (3,828 of 4,028 plan rows in that same canary are genuinely-new names).
    real_plan_names: set[str] = {
        _plan_name_key(u.get("floor_plan_name")) for u in units_out
    }
    real_plan_names.discard("")
    emitted_plan_names: set[str] = set()

    for fp_id, fp in fp_by_id.items():
        if fp_id in seen_fp_ids:
            continue
        plan_key = _plan_name_key(fp.get("name") or fp.get("filter_label") or "")
        # Contradiction guard: this plan already shipped a real, priced,
        # AVAILABLE unit under a different floor_plan id. Claiming it is
        # UNAVAILABLE is false.
        if plan_key and plan_key in real_plan_names:
            continue
        # Duplicate guard: SightMap kept several ids for one plan name.
        if plan_key and plan_key in emitted_plan_names:
            continue
        if plan_key:
            emitted_plan_names.add(plan_key)
        beds = fp.get("bedroom_count")
        baths = fp.get("bathroom_count")
        name = fp.get("name") or fp.get("filter_label") or ""
        units_out.append(
            make_unit_dict(
                floor_plan_name=str(name),
                bed_label=bed_label_from(beds, str(name)),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths) if baths is not None else "",
                sqft="",
                unit_number="",
                floor="",
                building="",
                rent_range="",
                concession="",
                availability_status="UNAVAILABLE",
                available_units="0",
                availability_date="",
                source_ids={"sightmap_floor_plan_id": fp_id},
                source_api_url=url,
                extraction_tier=_TIER_BASE,
                data_quality_flag="SIGHTMAP_PLAN_PRESENCE",
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


def _sightmap_identity_gate(
    ctx: AdapterContext,
    body: Any,
    url: str,
    result: AdapterResult,
    *,
    require_positive: bool = False,
) -> Any | None:
    """Validate SightMap ``data.asset`` before accepting its roster.

    Captured in-page responses may predate the vendor's asset metadata, so
    they reject explicit mismatches but retain an observable ``UNKNOWN``.
    Detached warm replays use ``require_positive=True`` in
    :mod:`ma_poc.pms.sightmap_direct`.
    """

    from ma_poc.pms.property_identity import (
        MATCH,
        MISMATCH,
        evaluate_observed_from_context,
        sightmap_observed_identity,
    )

    identity = evaluate_observed_from_context(ctx, sightmap_observed_identity(body))
    configured = bool(getattr(ctx, "property_name", "") or getattr(ctx, "address", ""))
    if identity.status == MISMATCH or (require_positive and configured and identity.status != MATCH):
        result.errors.append(
            "SIGHTMAP_PROPERTY_IDENTITY_REJECTED: "
            f"url={url[:160]} status={identity.status} "
            f"evidence={','.join(identity.evidence)} "
            f"observed={identity.observed_name or identity.observed_address!r}"
        )
        return None
    return identity


def _record_sightmap_unit_source(
    result: AdapterResult,
    *,
    url: str,
    body: Any,
    unit_count: int,
    identity: Any,
) -> None:
    from ma_poc.pms.source_provenance import build_unit_source_provenance

    result.unit_source_provenance.append(
        build_unit_source_provenance(
            provider="sightmap",
            source_url=url,
            body=body,
            unit_count=unit_count,
            identity=identity,
        )
    )


def _try_subpage_sightmap_with_prices(
    ctx: AdapterContext, result: AdapterResult
) -> list[dict[str, str]]:
    """Search property subpages for a SightMap embed that DOES publish
    prices. Some operators run two SightMap embeds on the same property:
    a "full map" on the homepage (all units, no prices — just for floor-
    plan visualization) and a separate "availability" SightMap on
    ``/availability/`` (only currently-leasable units, WITH prices).

    Live-verified 2026-05-23 on roserawesmont.com:
      - homepage embed ``n9w616yev71``: 295 units, 0 prices
      - /availability/ embed ``r5v51x35wny``: 8 units, 8 with prices
        (#3135 $2,420 / #3203 $2,560 — matches the operator UI)

    Without this, the rent-gap flag would wrongly mark Rosera as
    "operator doesn't publish rent" — when actually they do, just via
    a second embed the homepage didn't link directly. Defensive: only
    fires when the primary SightMap path returned no rent AND a
    different embed code exists on a subpage.
    """
    from urllib.parse import urlparse

    base_url = str(getattr(ctx, "base_url", "") or "")
    if not base_url:
        return []
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return []
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # Collect the embed codes already seen via the primary path so the
    # subpage hunt doesn't re-probe them.
    seen_embeds: set[str] = set()
    fr = getattr(ctx, "fetch_result", None)
    body0 = getattr(fr, "body", None) if fr is not None else None
    html0 = body0.decode("utf-8", errors="replace") if isinstance(body0, bytes) else str(body0 or "")
    for m in _SIGHTMAP_EMBED_URL_RE.finditer(html0):
        seen_embeds.add(m.group(1))

    try:
        from ma_poc.pms.adapters._probe import probe_get
    except Exception:
        return []

    # Probe the conventional availability/apartments paths. Same paths
    # used by the SecureCafe drill (proven discovery surface).
    for path in ("/availability/", "/apartments/", "/availability"):
        try:
            r = probe_get(origin + path, timeout=15)
        except Exception:
            continue
        if r.status_code != 200 or not r.text:
            continue
        # Find every SightMap embed on this page that wasn't already
        # probed by the primary path.
        new_codes: list[str] = []
        for m in _SIGHTMAP_EMBED_URL_RE.finditer(r.text):
            code = m.group(1)
            if code not in seen_embeds:
                seen_embeds.add(code)
                new_codes.append(code)
        if not new_codes:
            continue
        for code in new_codes:
            try:
                er = probe_get(f"https://sightmap.com/embed/{code}", timeout=12)
                href_m = _SIGHTMAP_APP_CONFIG_HREF_RE.search(er.text or "")
                if not href_m:
                    continue
                api_url = href_m.group(1).replace("\\/", "/")
                ar = probe_get(api_url, timeout=15)
                if ar.status_code != 200 or not ar.text:
                    continue
                body = json.loads(ar.text)
            except Exception as exc:
                result.errors.append(
                    f"sightmap-subpage-probe-error[{code}]: "
                    f"{type(exc).__name__}: {str(exc)[:80]}"
                )
                continue
            identity = _sightmap_identity_gate(ctx, body, api_url, result)
            if identity is None:
                continue
            sub_units, _ = parse_sightmap_payload(body, api_url)
            # Only accept this subpage embed if it actually has rent —
            # else we'd just be swapping one no-rent embed for another.
            with_rent = sum(
                1
                for u in sub_units
                if str(u.get("rent_range") or "").strip() not in {"", "$0", "0"}
            )
            if with_rent > 0:
                result.errors.append(
                    f"sightmap-subpage-override: found {with_rent}/"
                    f"{len(sub_units)} units with rent at "
                    f"{origin}{path} (embed {code}) — swapping in"
                )
                # Stash the discovery URL so the verdict layer reports it.
                result.api_responses.append(
                    {
                        "url": api_url,
                        "status": 200,
                        "body": "<sightmap-subpage>",
                        "via": "subpage_availability",
                    }
                )
                result.winning_url = api_url
                result.unit_source_provenance.clear()
                _record_sightmap_unit_source(
                    result,
                    url=api_url,
                    body=body,
                    unit_count=len(sub_units),
                    identity=identity,
                )
                return sub_units
    return []


def _try_avalon_override_for_sightmap(
    ctx: AdapterContext, result: AdapterResult
) -> list[dict[str, Any]]:
    """Avalon SHIPS rent in its own Fusion CMS blob, even when its
    SightMap embed shows ``price: null``. wimberlyapthome.com is the
    canonical example: SightMap detector fires first (sightmap.com
    iframe present), SightMap API returns 372 units with null prices,
    but the SAME homepage's Avalon Fusion JSON has rent + sqft on
    every unit (verified live: 25 units, $1,150/662sqft for #236).

    Before treating an all-null-price SightMap response as a genuine
    operator-rent-gap, check the page HTML for Avalon's distinctive
    ``"unitId":"AVB-`` marker. If present, run parse_avalonbay_html
    and return those units instead. Returns ``[]`` when no Avalon
    signal is found or parsing yielded nothing — the caller then
    falls through to the rent-gap flag for genuinely non-Avalon
    sites (Rosera, Vanguard, Decron portfolio, etc.).
    """
    html = ""
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None) if fr is not None else None
    if isinstance(body, bytes):
        html = body.decode("utf-8", errors="replace")
    elif isinstance(body, str):
        html = body
    if not html or '"unitId":"AVB-' not in html.replace(" ", "").replace("\n", ""):
        # Fast prefilter — only run the heavier parse_avalonbay_html when
        # the Avalon signature is genuinely present. The .replace() guards
        # against incidental whitespace in synthetic test fixtures.
        if not html or "AVB-" not in html:
            return []
    try:
        from ma_poc.pms.adapters.avalonbay import parse_avalonbay_html

        base_url = str(getattr(ctx, "base_url", "") or "")
        avalon_units = parse_avalonbay_html(html, base_url)
    except Exception as exc:
        result.errors.append(
            f"sightmap-avalon-override-error: "
            f"{type(exc).__name__}: {str(exc)[:100]}"
        )
        return []
    if avalon_units:
        from ma_poc.pms.property_identity import evaluate_from_context
        from ma_poc.pms.source_provenance import build_unit_source_provenance

        # This override replaces the SightMap roster completely, so replace
        # (rather than append to) provenance with the marketing HTML that
        # actually produced the admitted Avalon units.
        result.unit_source_provenance = [
            build_unit_source_provenance(
                provider="avalonbay",
                source_url=base_url,
                body=html,
                unit_count=len(avalon_units),
                identity=evaluate_from_context(ctx),
                response_kind="marketing_html_unit_roster",
            )
        ]
    return avalon_units


# Non-numeric rent_range sentinels that masquerade as "has rent" but
# carry no positive price. Lowercased before comparison. Catches the
# residue rows in TIER_1_API_SIGHTMAP_IFRAME_PLAN_LEVEL / _DIRECT_PLAN_LEVEL
# / _DIRECT cohorts where ``display_price`` is a placeholder string the
# original chip #10 exclude-set ({"", "$0", "0"}) missed.
_SIGHTMAP_ZERO_RENT_SENTINELS: frozenset[str] = frozenset({
    "", "$0", "0", "$0.00", "0.00", "$0,000",
    "n/a", "na", "-", "—", "tbd", "call", "contact",
    "call for pricing", "contact for pricing", "inquire",
})


def _sightmap_unit_has_rent(u: dict[str, Any]) -> bool:
    """True iff *u* carries a positive numeric rent.

    Checks every canonical rent field (chip #10 originally checked only
    ``market_rent_low``/``market_rent_high``; post-process ``infer``
    canonicalises to ``rent_low``/``rent_high`` so both must be sampled
    to avoid the zero-rent residue cohort the chip #10 follow-up targets).
    Non-numeric ``rent_range`` strings (``"TBD"``, ``"Call for pricing"``,
    ``"—"``, ``"$0.00"``, etc.) are treated as no-rent via the
    ``_SIGHTMAP_ZERO_RENT_SENTINELS`` set.
    """
    rr = str(u.get("rent_range") or "").strip()
    if rr and rr.lower() not in _SIGHTMAP_ZERO_RENT_SENTINELS:
        return True
    for k in ("market_rent_low", "market_rent_high", "rent_low", "rent_high",
              "asking_rent", "rent"):
        v = u.get(k)
        if isinstance(v, bool):  # bool is int subclass — exclude explicitly
            continue
        if isinstance(v, (int, float)) and v > 0:
            return True
    return False


def _drop_zero_info_sightmap_units(
    units: list[dict[str, Any]],
    result: AdapterResult,
    *,
    keep_dated_no_rent: bool = False,
) -> int:
    """Drop SightMap units with no positive rent.

    The original chip #10 (2026-05-25 ``dbd7d77``) dropped units that had
    neither rent NOR an availability date — closing the 2,605-row
    ``TIER_1_API_SIGHTMAP_IFRAME`` cluster from altisbluelake /
    eonflaglervillage / hydeparkmckinney / 240parkave. That helper
    intentionally kept "dated-but-not-priced" rows as informational
    signal (the rescue chain then stamped ``data_gaps=["rent"]``).

    Follow-up deep-probe 2026-05-25: the kept dated-no-rent rows are the
    residue cohort: 199 in TIER_1_API_SIGHTMAP_IFRAME_PLAN_LEVEL, 80 in
    TIER_1_API_SIGHTMAP_DIRECT_PLAN_LEVEL, 99 in TIER_1_API_SIGHTMAP_DIRECT.
    The verdict layer (scraper.py ~L1236) downgrades these to ``_PLAN_LEVEL``
    after no_rent retry exhaustion, but the dated-no-rent unit rows still
    ship as zero-rent inventory. Default behaviour now drops them too.

    Filter gate (per unit):
      - no positive numeric rent in ANY canonical rent field
        (``market_rent_low``/``_high``, ``rent_low``/``_high``,
        ``asking_rent``, ``rent``)
      - AND ``rent_range`` is empty / in ``_SIGHTMAP_ZERO_RENT_SENTINELS``
        (catches ``"$0.00"``, ``"TBD"``, ``"—"``, ``"Call for pricing"``)

    Set ``keep_dated_no_rent=True`` to opt into the original 2026-05-25
    behaviour (keep rows that carry a populated ``availability_date``
    even when rent is absent). No production caller takes that branch
    after the follow-up — it's preserved for callers that explicitly
    want the dated-no-rent informational rows.

    Mutates *units* in place. Returns the drop count. Appends a single
    ``sightmap-zero-info-dropped`` line to ``result.errors`` so the count
    is auditable per scrape.
    """
    if not units:
        return 0

    def _has_date(u: dict[str, Any]) -> bool:
        for k in ("availability_date", "available_date"):
            v = u.get(k)
            if not v:
                continue
            s = str(v).strip().lower()
            if s and s not in {"none", "null", "n/a", "-"}:
                return True
        return False

    keep: list[dict[str, Any]] = []
    dropped = 0
    for u in units:
        # Plan-presence rows (floor plans with no units in units[]) are catalogue
        # markers by construction — no rent expected. Keep them; the zero-info
        # filter targets unit-level rows operators failed to price.
        if u.get("data_quality_flag") == "SIGHTMAP_PLAN_PRESENCE":
            keep.append(u)
        elif _sightmap_unit_has_rent(u):
            keep.append(u)
        elif keep_dated_no_rent and _has_date(u):
            keep.append(u)
        else:
            dropped += 1
    if dropped:
        units[:] = keep
        result.errors.append(
            f"sightmap-zero-info-dropped: {dropped} unit(s) had no positive "
            f"rent (operator publishes no price for this unit) — skipped "
            f"to prevent false-positive AVAILABLE rows"
        )
    return dropped


def _flag_sightmap_units_operator_rent_gap(
    units: list[dict[str, Any]], result: AdapterResult
) -> int:
    """Stamp ``data_gaps=["rent"]`` + ``data_quality_flag="RENT_NOT_
    PUBLISHED"`` on each unit when the operator clearly does not
    publish rent.

    Gating — ALL of these must hold (defensive against over-flagging):
      - ≥3 units in the result (single/duo units could be a parser
        edge case, not a portfolio-wide policy)
      - 100% of units have a positive area value (confirms the
        adapter extracted real unit-level structure)
      - 0 units carry any rent value (across rent_range and the
        numeric market_rent_low/high fields)

    schema_gate._has_rent honors the flag → no_rent retry doesn't
    fire → tier stays _SIGHTMAP / _SIGHTMAP_IFRAME (not _PLAN_LEVEL),
    verdict ships as SUCCESS not SUCCESS_PLAN_LEVEL. Returns the
    number flagged for diagnostics; 0 when the gate is not met
    (defensive no-op for properties that DO publish rent).
    """
    if not units or len(units) < 3:
        return 0

    def _unit_has_rent(u: dict[str, Any]) -> bool:
        # rent_range non-empty/non-zero, or numeric market rents.
        rr = str(u.get("rent_range") or "").strip()
        if rr and rr not in {"", "$0", "0"}:
            return True
        for k in ("market_rent_low", "market_rent_high", "rent_low", "rent_high"):
            v = u.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return True
        return False

    def _unit_has_area(u: dict[str, Any]) -> bool:
        for k in ("sqft", "area", "_sqft"):
            v = u.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return True
            if isinstance(v, str):
                s = v.strip().replace(",", "")
                if s and s != "0":
                    try:
                        if float(s) > 0:
                            return True
                    except ValueError:
                        pass
        return False

    if any(_unit_has_rent(u) for u in units):
        return 0
    if not all(_unit_has_area(u) for u in units):
        return 0

    flagged = 0
    for u in units:
        gaps = u.get("data_gaps") or []
        if "rent" not in gaps:
            gaps = list(gaps) + ["rent"]
            u["data_gaps"] = gaps
        if not u.get("data_quality_flag"):
            u["data_quality_flag"] = "RENT_NOT_PUBLISHED"
        flagged += 1
    if flagged:
        result.errors.append(
            f"sightmap-rent-not-published: flagged {flagged} units — "
            f"operator does not publish rent via SightMap (every unit "
            f"price is null with full area + plan_name + unit_id)"
        )
    return flagged


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
            identity = _sightmap_identity_gate(ctx, body, url, result)
            if identity is None:
                continue
            units, dropped = parse_sightmap_payload(body, url)
            total_dropped += dropped
            if units:
                all_units.extend(units)
                result.api_responses.append(resp)
                _record_sightmap_unit_source(
                    result,
                    url=url,
                    body=body,
                    unit_count=len(units),
                    identity=identity,
                )

        if all_units:
            # Stage 1 validity gate — drops dim-less rows before they leak
            # into properties.json. Lazy import: see RentCafe adapter for the
            # cycle-break rationale.
            from ma_poc.extraction.post_process import post_process

            _pp_parsed = len(all_units)
            _pp = post_process(all_units, property_id=getattr(ctx, "property_id", None))
            if _pp.n_admitted > 0:
                result.units = _pp.admitted
                result.plan_summaries = _pp.plan_summaries
                result.winning_url = (
                    result.api_responses[0].get("url") if result.api_responses else None
                )
                result.confidence = min(0.95, 0.7 + 0.05 * _pp.n_admitted)
                result.tier_used = _TIER_BASE
                # 2026-05-25: drop units with NO rent + NO availability
                # date. These are placeholder rows from SightMap (price
                # null, available_on null) that previously emitted as
                # AVAILABLE with empty rent — see canary deep-probe 2,605-
                # row cluster verified on altisbluelake / eonflaglervillage
                # / hydeparkmckinney / 240parkave. Runs BEFORE the
                # Avalon/subpage rescue chain so the rescue paths still see
                # an empty result.units and trigger correctly. Mixed-price
                # properties also get cleaned (priced units kept, zero-
                # info subset dropped).
                _drop_zero_info_sightmap_units(result.units, result)
                if result.units:
                    # Re-tally confidence on the surviving subset (mixed-
                    # price case may have shed a large fraction of rows).
                    result.confidence = min(0.95, 0.7 + 0.05 * len(result.units))
                # 2026-05-23: Avalon-override + operator-rent-gap flag.
                # If 0 of the SightMap units have rent, FIRST check
                # whether the page is actually Avalon-backed (its own
                # Fusion CMS publishes rent even when SightMap shows
                # null price — wimberlyapthome.com is the canonical
                # case). If yes, swap in the Avalon-extracted units.
                # If no Avalon signal, apply the rent-gap flag for
                # genuinely non-Avalon operators (Rosera, Decron, etc.).
                # 2026-05-25 chip #10 follow-up: share the rent predicate
                # with the filter so paths agree on what "has rent" means
                # (post-process canonicalises ``market_rent_*`` →
                # ``rent_low/_high`` for some adapters).
                _has_any_rent = any(
                    _sightmap_unit_has_rent(u) for u in result.units
                )
                if not _has_any_rent:
                    avalon_units = _try_avalon_override_for_sightmap(ctx, result)
                    if avalon_units:
                        _pp_av = post_process(
                            avalon_units,
                            property_id=getattr(ctx, "property_id", None),
                        )
                        if _pp_av.n_admitted > 0:
                            result.units = _pp_av.admitted
                            result.plan_summaries = _pp_av.plan_summaries
                            result.tier_used = "TIER_1_HTML_AVALONBAY_FUSION_VIA_SIGHTMAP"
                            result.confidence = min(
                                0.92, 0.7 + 0.05 * _pp_av.n_admitted
                            )
                            result.errors.append(
                                f"sightmap-avalon-override: SightMap had "
                                f"{_pp.n_admitted} null-price units; "
                                f"Avalon Fusion HTML has "
                                f"{_pp_av.n_admitted} with real rent — "
                                f"swapped in"
                            )
                            return result
                    # Avalon override empty — try the subpage SightMap
                    # discovery path (Rosera-style two-embed pattern).
                    sub_units = _try_subpage_sightmap_with_prices(ctx, result)
                    if sub_units:
                        _pp_sub = post_process(
                            sub_units,
                            property_id=getattr(ctx, "property_id", None),
                        )
                        if _pp_sub.n_admitted > 0:
                            result.units = _pp_sub.admitted
                            result.plan_summaries = _pp_sub.plan_summaries
                            result.tier_used = f"{_TIER_BASE}_SUBPAGE"
                            result.confidence = min(
                                0.92, 0.7 + 0.05 * _pp_sub.n_admitted
                            )
                            return result
                    # No rescue available. Two sub-cases:
                    # (a) result.units is empty — the zero-info drop took
                    #     out every row. Emit the new OPERATOR_RENT_NOT_
                    #     PUBLISHED tier so the verdict layer does not
                    #     ship this as SUCCESS.
                    # (b) result.units still has rows — those carry a
                    #     date but no rent. Apply the existing rent-gap
                    #     flag so they ship as "dated-no-rent" rather
                    #     than misclassified AVAILABLE.
                    if not result.units:
                        result.tier_used = _TIER_OPERATOR_RENT_NOT_PUBLISHED
                        result.confidence = 0.0
                        result.errors.append(
                            f"SIGHTMAP_OPERATOR_RENT_NOT_PUBLISHED: "
                            f"dropped all {_pp.n_admitted} admitted units "
                            f"(no rent + no date on any unit, no Avalon "
                            f"signal, no subpage rescue) — operator-wide "
                            f"rent suppression confirmed"
                        )
                        return result
                    _flag_sightmap_units_operator_rent_gap(result.units, result)
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

        # Iframe-direct fallback. The captured network log can miss the
        # SightMap XHR when the iframe loads after our 2-second settle
        # budget. Synthesise the API call by extracting the embed URL from
        # the rendered HTML, fetching the embed page to read its
        # ``window.__APP_CONFIG__`` block, then calling the
        # ``app/api/v1/.../sightmaps/`` endpoint directly. Source:
        # 2026-05-13 live probe of livesoundatpeninsulajax.com.
        if not all_units:
            iframe_units = await _try_sightmap_iframe_fallback(ctx, result)
            if iframe_units:
                from ma_poc.extraction.post_process import post_process

                _pp = post_process(
                    iframe_units, property_id=getattr(ctx, "property_id", None)
                )
                if _pp.n_admitted > 0:
                    result.units = _pp.admitted
                    result.plan_summaries = _pp.plan_summaries
                    result.tier_used = f"{_TIER_BASE}_IFRAME"
                    result.confidence = min(0.90, 0.65 + 0.05 * _pp.n_admitted)
                    # 2026-05-25: drop zero-info units before the Avalon /
                    # subpage rescue chain (same rationale as the primary
                    # path — keep priced units, shed the placeholders).
                    _drop_zero_info_sightmap_units(result.units, result)
                    if result.units:
                        result.confidence = min(
                            0.90, 0.65 + 0.05 * len(result.units)
                        )
                    # Same Avalon-override + rent-gap flag chain for the
                    # iframe path. Without this, wimberly-style sites
                    # taking the iframe-fallback path would still get
                    # wrongly flagged as rent-not-published.
                    _has_any_rent = any(
                        _sightmap_unit_has_rent(u) for u in result.units
                    )
                    if not _has_any_rent:
                        avalon_units = _try_avalon_override_for_sightmap(ctx, result)
                        if avalon_units:
                            _pp_av = post_process(
                                avalon_units,
                                property_id=getattr(ctx, "property_id", None),
                            )
                            if _pp_av.n_admitted > 0:
                                result.units = _pp_av.admitted
                                result.plan_summaries = _pp_av.plan_summaries
                                result.tier_used = (
                                    "TIER_1_HTML_AVALONBAY_FUSION_VIA_SIGHTMAP_IFRAME"
                                )
                                result.confidence = min(
                                    0.92, 0.7 + 0.05 * _pp_av.n_admitted
                                )
                                return result
                        sub_units = _try_subpage_sightmap_with_prices(ctx, result)
                        if sub_units:
                            _pp_sub = post_process(
                                sub_units,
                                property_id=getattr(ctx, "property_id", None),
                            )
                            if _pp_sub.n_admitted > 0:
                                result.units = _pp_sub.admitted
                                result.plan_summaries = _pp_sub.plan_summaries
                                result.tier_used = f"{_TIER_BASE}_IFRAME_SUBPAGE"
                                result.confidence = min(
                                    0.92, 0.7 + 0.05 * _pp_sub.n_admitted
                                )
                                return result
                        # No rescue. Empty-after-drop → new tier code;
                        # remaining dated-no-rent rows → existing flag.
                        if not result.units:
                            result.tier_used = (
                                f"{_TIER_OPERATOR_RENT_NOT_PUBLISHED}_IFRAME"
                            )
                            result.confidence = 0.0
                            result.errors.append(
                                f"SIGHTMAP_OPERATOR_RENT_NOT_PUBLISHED: "
                                f"iframe-fallback dropped all "
                                f"{_pp.n_admitted} admitted units (no "
                                f"rent + no date on any unit, no rescue) "
                                f"— operator-wide rent suppression"
                            )
                            return result
                        _flag_sightmap_units_operator_rent_gap(result.units, result)
                    return result

        # Failure path: classify via structured sub-codes mirroring the RentCafe
        # adapter pattern.
        result.confidence = 0.0
        sightmap_responses = [
            r
            for r in api_responses
            if isinstance(r.get("body"), dict) and _is_sightmap_response(r.get("body"))
        ]
        if not api_responses or not sightmap_responses:
            # 2026-05-24 HAR-driven probe: when no SightMap responses
            # were captured (NO_RESPONSE or SHAPE_REJECTED), try
            # discovering the sightmap embed code from the marketing
            # page body and hit ``sightmap.com/app/api/v1/{TOKEN}/
            # sightmaps/{ID}`` directly.
            #
            # PRODUCTION-READY (default ON for canary).
            #
            # Live validation 2026-05-24: 3/5 SHAPE_REJECTED cohort
            # props lift with the deep-path probe (livahwatukee 15
            # units, residencesatfalconnorth 22 units,
            # creekwoodapartmenthomes 1). The 2 misses load the embed
            # code via /internal-page-widgets/ POST API which needs
            # per-property section IDs — covered by a future chip task.
            #
            # Gated behind ``DISABLE_SIGHTMAP_DIRECT_PROBE`` (inverted
            # — opt-out, not opt-in) so existing portal-hint test
            # fixtures using a mock sightmap embed can suppress the
            # probe via that env. Canary leaves the env unset → probe
            # fires by default.
            import os as _sm_os
            _disabled = bool(
                _sm_os.environ.get("DISABLE_SIGHTMAP_DIRECT_PROBE")
            )
            try:
                direct_units = (
                    await _try_direct_sightmap_api_probe(ctx, result)
                    if not _disabled
                    else []
                )
            except Exception as exc:  # noqa: BLE001
                direct_units = []
                result.errors.append(
                    f"sightmap-direct-probe-error: "
                    f"{type(exc).__name__}: {str(exc)[:80]}"
                )
            if direct_units:
                from ma_poc.extraction.post_process import post_process

                _ppd = post_process(
                    direct_units, property_id=getattr(ctx, "property_id", None)
                )
                if _ppd.n_admitted > 0:
                    result.units = _ppd.admitted
                    result.plan_summaries = _ppd.plan_summaries
                    result.tier_used = "TIER_1_API_SIGHTMAP_DIRECT"
                    result.confidence = min(
                        0.92, 0.7 + 0.04 * _ppd.n_admitted
                    )
                    # 2026-05-25: shed zero-info units from the direct
                    # probe too. If everything dropped, surface the new
                    # OPERATOR_RENT_NOT_PUBLISHED tier rather than ship
                    # an empty SUCCESS.
                    _drop_zero_info_sightmap_units(result.units, result)
                    if not result.units:
                        result.tier_used = (
                            f"{_TIER_OPERATOR_RENT_NOT_PUBLISHED}_DIRECT"
                        )
                        result.confidence = 0.0
                        result.errors.append(
                            f"SIGHTMAP_OPERATOR_RENT_NOT_PUBLISHED: "
                            f"direct probe returned {_ppd.n_admitted} "
                            f"admitted units, all with no rent + no date "
                            f"— operator-wide rent suppression"
                        )
                    else:
                        result.confidence = min(
                            0.92, 0.7 + 0.04 * len(result.units)
                        )
                    result.api_responses.append({
                        "url": direct_units[0].get("source_api_url", ""),
                        "status": 200,
                        "body": "<sightmap-direct-probe>",
                        "via": "sightmap_direct_probe",
                    })
                    return result

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


def _entry_html_from_ctx(ctx: AdapterContext) -> str | None:
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


def _normalize_sightmap_slashes(html: str) -> str:
    """Un-escape JSON slash escapes so ``sightmap.com\\/embed\\/{code}`` and
    the ``\\u002f`` variant match the literal-slash SightMap regexes."""
    if not html:
        return html
    return (
        html.replace("\\u002f", "/")
        .replace("\\u002F", "/")
        .replace("\\/", "/")
    )


def find_sightmap_embed_codes(html: str) -> list[str]:
    """Return any SightMap embed codes found anywhere in *html* (deduped).

    Accepts the embed URL in any *loading-shaped* DOM context —
    ``<iframe src=>``, ``<a data-src=>`` (Fancybox lightbox lazy-loading),
    ``<a href=>``, ``var EngrainedUrl = '...'`` JS assignments, etc. —
    because the embed-code-bearing URL is distinctive enough on its own
    that the surrounding element type isn't a useful filter.

    Skips matches that appear in JSON-value position (preceded by ``":``
    or ``": "``). Those are config blobs the SightMap adapter can't act
    on directly without first being routed there by a different signal
    — and the embedded-portal-hint propagation path in the generic
    adapter (``detect_embedded_portal_urls``) already handles them. See
    ``test_portal_hint_survives_full_scrape_chain``.

    Reserved infra path segments (``embed/api``, ``embed/app``,
    ``embed/admin``) are filtered out — these are SightMap's own
    internal routes, never customer embed codes.
    """
    if not html or "sightmap.com" not in html.lower():
        return []
    # 2026-07-12: normalize escaped slashes so JSON-embedded embed URLs
    # (IMT WordPress: ``"sightmap_embed_url":"https:\/\/sightmap.com\/embed\/
    # {code}"`` and the ``/`` variant) are matched by the literal-slash
    # regex. Done on a copy used for the whole scan so the JSON-position
    # look-back below stays index-consistent.
    html = _normalize_sightmap_slashes(html)

    seen: set[str] = set()
    codes: list[str] = []
    for m in _SIGHTMAP_EMBED_URL_RE.finditer(html):
        # Skip JSON-value position. Real cluster #5 forms preceding the URL:
        #   data-src="...   → preceded by `="`
        #   var EngrainedUrl = '...'  → preceded by `= '`
        #   src=...         → preceded by `=`
        # JSON-value form preceding the URL:
        #   "embed_url":"...  → preceded by `":"`  (also `": "` with whitespace)
        # Walk back over an optional whitespace + quote, then check for a
        # colon. That distinguishes ``":"https://...`` (skip) from
        # ``="https://...`` (keep) without false-positives.
        start = m.start()
        i = start - 1
        if i >= 0 and html[i] in "'\"":
            i -= 1
        while i >= 0 and html[i] in " \t":
            i -= 1
        if i >= 0 and html[i] == ":":
            # 2026-07-12: a ``":"``-preceded URL is normally a config blob
            # (handled by the generic portal-hint path). But when the JSON
            # KEY is a real sightmap/engrain embed key, it IS the live embed
            # URL — keep it (IMT sightmap_embed_url form). Otherwise skip.
            _key_ctx = html[max(0, start - 60):start].lower()
            if not any(
                k in _key_ctx
                for k in (
                    "sightmap_embed_url",
                    "sightmap_url",
                    "sightmap_link",
                    "engrainedurl",
                )
            ):
                continue

        code = m.group(1)
        # Reserved infra segments — never customer codes.
        if code.lower() in {"embed", "app", "admin", "api"}:
            continue
        if code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


# ----------------------------------------------------------------------
# 2026-05-24 HAR-driven addition: direct sightmap.com/app/api/v1/ probe
# ----------------------------------------------------------------------
# Some SightMap-fronted properties don't fire the SightMap XHR during
# the canary's network capture window (iframe lazy-loads or operator
# unhides on user interaction). The marketing page body still contains
# the embed code, which lets us reconstruct the canonical API URL and
# fetch it via direct curl_cffi. HAR-verified across 7 SHAPE_REJECTED
# props (livahwatukee, traditionapthomes, residencesatfalconnorth,
# creekwoodapartmenthomes, livegreenview).

import re as _sm_re  # noqa: E402 — local to keep this section self-contained

_SM_EMBED_RE = _sm_re.compile(
    r"(?:https?:)?//sightmap\.com/(?:embed|app/embed)/([a-zA-Z0-9_-]{4,32})",
    _sm_re.IGNORECASE,
)
# Reserved infra path segments — ``embed/api.js`` is the loader,
# ``embed/app`` and ``embed/admin`` are infra paths. None of these are
# property-specific embed codes; skip them so they don't pollute the
# probe candidate list. Matches the filter the existing
# ``find_sightmap_embed_codes`` already applies (see line 65-74 of
# sightmap.py docstring).
_SM_RESERVED_CODES = {"api", "app", "admin", "embed", "v1", "v2"}


def _extract_sightmap_embed_codes(body: str) -> list[str]:
    """Find SightMap embed codes (the short alphanumeric token from
    ``sightmap.com/embed/{TOKEN}``) anywhere in *body*.

    Filters reserved infra paths (``embed/api.js``, ``embed/app`` etc.)
    that look like embed codes but aren't.

    Returns a list of distinct codes in document order. Empty list if
    no markers found.
    """
    if not body:
        return []
    # Entrata/Spaces config blobs can JSON-escape every slash in the exact
    # published ``sightmap_url`` value (``https:\/\/sightmap.com\/embed\/
    # {code}``).  The iframe fallback already normalizes this shape before
    # applying its embed regex; keep the direct-probe discovery path aligned.
    body = _normalize_sightmap_slashes(body)
    out: list[str] = []
    for m in _SM_EMBED_RE.finditer(body):
        code = m.group(1)
        if not code or code.lower() in _SM_RESERVED_CODES:
            continue
        # An embed code is alphanumeric, at least 5 chars, includes a
        # mix of digits + letters (heuristic to weed out non-codes)
        if len(code) < 5:
            continue
        if code not in out:
            out.append(code)
    return out


async def _try_direct_sightmap_api_probe(
    ctx: AdapterContext,
    result: AdapterResult | None = None,
) -> list[dict[str, str]]:
    """Discover sightmap embed codes from the captured page body, hit
    the embed-redirect endpoint to learn the {TOKEN}/{ID} pairing,
    then fetch ``sightmap.com/app/api/v1/{TOKEN}/sightmaps/{ID}`` for
    the actual unit data. Returns parsed units (empty list when no
    embed found, probe failed, or response was amenities-only).

    Never raises — the caller's outer try/except is for defensive
    measure only.
    """
    fr = getattr(ctx, "fetch_result", None)
    raw = getattr(fr, "body", None) if fr is not None else None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", errors="replace")
        except Exception:
            return []
    if not isinstance(raw, str) or not raw:
        return []

    codes = _extract_sightmap_embed_codes(raw)

    # The embed code often lives on a deep marketing path (e.g.
    # ``/{city}/{slug}/conventional/`` for Entrata-themed SightMap
    # sites). Live-verified 2026-05-24 on livahwatukee.com — homepage
    # had only ``sightmap.com/embed/api.js`` (the JS loader), but
    # ``/phoenix/liv-ahwatukee/conventional/`` had the actual embed
    # code ``8xvrmoo6pjk``. Probe common deep paths when homepage came
    # back empty.
    if not codes:
        from urllib.parse import urlparse as _urlparse

        from ma_poc.pms.adapters._probe import probe_get as _probe

        final_url = ""
        if fr is not None:
            final_url = str(getattr(fr, "final_url", "") or "")
        final_url = final_url or getattr(ctx, "base_url", "") or ""
        try:
            p = _urlparse(final_url)
            base = (
                f"{p.scheme}://{p.netloc}"
                if p.scheme and p.netloc
                else ""
            )
        except Exception:
            base = ""

        # First, try the Entrata vanity deep path discovered in the
        # captured body: ``/{city}/{slug}/conventional/`` is where
        # ``<iframe id="sightmap">`` lives on Entrata-themed sites.
        # Live-verified 2026-05-24 on residencesatfalconnorth.com.
        if base:
            _re_deep = _sm_re.compile(
                r'href=["\']?'
                r'(https?://[^"\'<>\s]+/(?:[\w-]+/){1,3}'
                r'(?:conventional|affordable)/?[^"\'<>\s]*?)["\'>]',
                _sm_re.IGNORECASE,
            )
            for _m in _re_deep.finditer(raw):
                cand = _m.group(1).split("?")[0].split("#")[0]
                if (
                    cand
                    and _urlparse(cand).netloc.endswith(_urlparse(base).netloc)
                ):
                    try:
                        r_deep = _probe(cand, timeout=12)
                    except Exception:
                        continue
                    if r_deep.status_code == 200 and r_deep.text:
                        codes = _extract_sightmap_embed_codes(r_deep.text)
                        if codes:
                            break

        # Try the Engrain `/internal-page-widgets/` POST API.
        # Many Engrain-themed Entrata sites embed SightMap via a
        # ``<article class="pp-section-placeholder ..." data-website-
        # page-section="{N}" data-website-page-type-id="{M}"
        # data-layout-type-id="{K}">`` element on the deep Entrata
        # vanity path. The body for the iframe is fetched via a POST
        # to ``{host}/internal-page-widgets/`` with the section params
        # in form-encoded payload. Live-verified 2026-05-24 on
        # traditionapthomes.com (section 774 → JSON with
        # ``sightmap_url``).
        if not codes and base:
            _re_deep_attr = _sm_re.compile(
                r'href=["\']?'
                r'(https?://[^"\'<>\s]+/(?:[\w-]+/){1,3}'
                r'(?:conventional|affordable)/?[^"\'<>\s]*?)["\'>]',
                _sm_re.IGNORECASE,
            )
            _section_re = _sm_re.compile(
                r'data-website-page-section=["\'](\d+)["\']'
                r'\s+data-website-page-type-id=["\'](\d+)["\']'
                r'\s+data-layout-type-id=["\'](\d+)["\']',
                _sm_re.IGNORECASE,
            )
            for _m_link in _re_deep_attr.finditer(raw):
                _deep_url = (
                    _m_link.group(1).split("?")[0].split("#")[0]
                )
                if not _deep_url:
                    continue
                if not _urlparse(_deep_url).netloc.endswith(
                    _urlparse(base).netloc
                ):
                    continue
                try:
                    _r_deep = _probe(_deep_url, timeout=12)
                except Exception:
                    continue
                if _r_deep.status_code != 200 or not _r_deep.text:
                    continue
                _m_attrs = _section_re.search(_r_deep.text)
                if not _m_attrs:
                    continue
                _section_id = _m_attrs.group(1)
                _page_type_id = _m_attrs.group(2)
                _layout_type_id = _m_attrs.group(3)
                try:
                    from curl_cffi import requests as _ipw_cc

                    _ipw_resp = _ipw_cc.post(
                        f"{base}/internal-page-widgets/",
                        headers={
                            "Accept": "*/*",
                            "Content-Type": (
                                "application/x-www-form-urlencoded; "
                                "charset=UTF-8"
                            ),
                            "Origin": base,
                            "Referer": _deep_url,
                            "X-Requested-With": "XMLHttpRequest",
                        },
                        data={
                            "internal_page_widgets[website_page_section]":
                                _section_id,
                            "internal_page_widgets[layout_type_id]":
                                _layout_type_id,
                            "internal_page_widgets[website_page_type_id]":
                                _page_type_id,
                        },
                        timeout=15,
                        impersonate="chrome120",
                    )
                except Exception:
                    continue
                if _ipw_resp.status_code != 200 or not _ipw_resp.text:
                    continue
                _sm_url_m = _sm_re.search(
                    r'"sightmap_url"\s*:\s*"([^"]+)"',
                    _ipw_resp.text,
                )
                if not _sm_url_m:
                    continue
                _sm_url = _sm_url_m.group(1).replace("\\/", "/")
                _code_m = _sm_re.search(
                    r'sightmap\.com/embed/([\w-]+)', _sm_url
                )
                if (
                    _code_m
                    and _code_m.group(1).lower() not in _SM_RESERVED_CODES
                ):
                    codes = [_code_m.group(1)]
                    break

        if not codes and base:
            for sub in (
                "/floorplans/", "/floor-plans/", "/floorplans",
                "/availability/", "/apartments/",
            ):
                try:
                    r_sub = _probe(base + sub, timeout=12)
                except Exception:
                    continue
                if r_sub.status_code != 200 or not r_sub.text:
                    continue
                codes = _extract_sightmap_embed_codes(r_sub.text)
                if codes:
                    break

    if not codes:
        return []

    from ma_poc.pms.adapters._probe import probe_get

    for code in codes[:3]:  # cap probing budget
        # Step 1: fetch the embed page to discover the sightmap_id
        embed_url = (
            f"https://sightmap.com/embed/{code}?enable_api=1"
        )
        try:
            r = probe_get(embed_url, timeout=15)
        except Exception:
            continue
        if r.status_code != 200 or not r.text:
            continue
        # The embed HTML has the canonical /sightmaps/{ID} reference,
        # but inside ``window.__APP_CONFIG__`` as JSON-encoded string
        # with escaped slashes (``sightmap.com\/app\/api\/v1\/...``).
        # Live-verified 2026-05-24 on creekwoodapartmenthomes embed
        # ``gow3zg5zp2m``: only the escaped form appears in the HTML.
        # Tolerate both literal ``/`` and escaped ``\/`` between
        # segments.
        m_token = _sm_re.search(
            r'sightmap\.com(?:\\/|/)app(?:\\/|/)api(?:\\/|/)v1'
            r'(?:\\/|/)([a-z0-9]+)(?:\\/|/)sightmaps(?:\\/|/)(\d+)',
            r.text,
            _sm_re.IGNORECASE,
        )
        if not m_token:
            # Some pages embed only the bare /sightmaps/{ID} in JSON
            m_token = _sm_re.search(
                r'"sightmap_id"\s*:\s*"?(\d+)',
                r.text,
                _sm_re.IGNORECASE,
            )
            if m_token:
                # Use the same embed code as the token portion (the canonical
                # URL uses a different short-code that we can't recover
                # without the JS context). Skip this branch — without both
                # halves we can't construct the URL.
                continue
            continue
        api_token, sightmap_id = m_token.group(1), m_token.group(2)

        # Step 2: fetch the canonical API
        api_url = (
            f"https://sightmap.com/app/api/v1/{api_token}/sightmaps/{sightmap_id}"
        )
        try:
            r2 = probe_get(api_url, timeout=20)
        except Exception:
            continue
        if r2.status_code != 200 or not r2.text:
            continue
        try:
            import json as _sm_json

            body = _sm_json.loads(r2.text)
        except Exception:
            continue
        if not _is_sightmap_response(body):
            continue
        identity = None
        if result is not None:
            identity = _sightmap_identity_gate(ctx, body, api_url, result)
            if identity is None:
                continue
        # Reuse the existing join parser
        units, _dropped = parse_sightmap_payload(body, api_url)
        if units:
            # Set the new tier label
            for u in units:
                u["extraction_tier"] = "TIER_1_API_SIGHTMAP_DIRECT"
            if result is not None:
                _record_sightmap_unit_source(
                    result,
                    url=api_url,
                    body=body,
                    unit_count=len(units),
                    identity=identity,
                )
            return units

    return []


def extract_sightmap_api_url(embed_html: str) -> str | None:
    """Pull the SightMap API URL out of an embed page's ``window.__APP_CONFIG__``."""
    if not embed_html:
        return None
    m = _SIGHTMAP_APP_CONFIG_HREF_RE.search(embed_html)
    if not m:
        return None
    # JSON-escaped forward slashes are common (\/); normalise to /.
    return m.group(1).replace("\\/", "/")


def find_sightmap_direct_api_urls(html: str) -> list[str]:
    """Pull any direct SightMap API URLs found inline in *html*.

    Returns deduplicated, slash-normalised URLs of the form
    ``sightmap.com/app/api/v1/{client}/sightmaps/{id}``. Used by the
    Angular-SPA fallback path (C7 2026-05-13) when the embed iframe has
    not yet materialised but the Angular bundle has the API URL inline.
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
    """When no SightMap API was captured during page load, follow the
    iframe URL ourselves: fetch the embed page, extract the
    ``__APP_CONFIG__`` API URL, fetch that, parse units.

    Also tries any direct SightMap API URLs found inline in the page
    HTML — covers the Angular-SPA case (C7 2026-05-13) where the iframe
    has not yet materialised but the bundle has the API URL hardcoded.

    Returns an empty list on any failure (silent; the caller continues to
    the normal failure-classification path).
    """
    html = _entry_html_from_ctx(ctx)
    if not html:
        return []

    # 2026-05-27 chip: operators that embed sightmap.com/embed/{id} on a
    # non-sightmap host return richer unit-level JSON when the embed
    # request carries a Referer pointing at the operator site (the
    # SightMap CDN gates some payload fields by Referer origin). Derive
    # the operator origin from ctx.base_url and thread it as Referer on
    # both the embed-page fetch and the downstream api fetch. UA is
    # bumped to Chrome 120 to match the impersonation profile already
    # used in _try_direct_sightmap_api_probe.
    operator_referer: str | None = None
    try:
        from urllib.parse import urlparse as _urlparse
        _p = _urlparse(getattr(ctx, "base_url", "") or "")
        if _p.scheme and _p.netloc and "sightmap.com" not in _p.netloc:
            operator_referer = f"{_p.scheme}://{_p.netloc}/"
    except Exception:
        operator_referer = None

    chrome120_ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    base_headers = {
        "User-Agent": chrome120_ua,
        "Accept": "text/html,application/json,*/*;q=0.8",
    }
    embed_headers = dict(base_headers)
    if operator_referer:
        embed_headers["Referer"] = operator_referer
    # Local alias for back-compat with the rest of this function.
    headers = embed_headers

    # Pass 1: direct API URLs inline in HTML (Angular SPA bundle pattern).
    # Faster than the embed-page indirection and works when the iframe
    # hasn't yet rendered. Try each candidate; first valid response wins.
    direct_urls = find_sightmap_direct_api_urls(html)
    if direct_urls:
        # 2026-07-16 (Lever 1): route via probe_get (residential/PROBE_PROXY_URL
        # + cost-gated Web Unlocker) instead of a bare httpx client from the GCP
        # runner IP. sightmap.com's CDN CF-blocks the runner IP, so the unrouted
        # fetch returned nothing in the page=None prod path — verified: the same
        # reconstructed API URLs return full inventory through residential.
        try:
            from ma_poc.pms.adapters._probe import probe_get
            for api_url in direct_urls[:3]:
                try:
                    ar = await asyncio.to_thread(
                        probe_get, api_url, headers=headers, timeout=15
                    )
                except Exception:
                    continue
                if getattr(ar, "status_code", 0) != 200:
                    continue
                try:
                    body = json.loads(getattr(ar, "text", "") or "")
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(body, dict) or not _is_sightmap_response(body):
                    continue
                identity = _sightmap_identity_gate(ctx, body, api_url, result)
                if identity is None:
                    continue
                units, _dropped = parse_sightmap_payload(body, api_url)
                if units:
                    result.api_responses.append(
                        {"url": api_url, "status": 200, "body": body,
                         "via": "direct_api_fallback"}
                    )
                    result.winning_url = api_url
                    _record_sightmap_unit_source(
                        result,
                        url=api_url,
                        body=body,
                        unit_count=len(units),
                        identity=identity,
                    )
                    return units
        except Exception as exc:
            result.errors.append(
                f"sightmap-direct-api-fallback-error: "
                f"{type(exc).__name__}: {str(exc)[:120]}"
            )

    # Pass 2: embed-iframe fallback. Look for ``<iframe src=...sightmap.com/
    # embed/{code}>``, fetch the embed page, parse out the API URL from
    # ``window.__APP_CONFIG__``.
    codes = find_sightmap_embed_codes(html)
    if not codes:
        return []
    embed_code = codes[0]
    try:
        # 2026-07-16 (Lever 1): probe_get (residential + WU) instead of bare
        # httpx — same CF-block rationale as Pass 1.
        from ma_poc.pms.adapters._probe import probe_get
        embed_url = f"https://sightmap.com/embed/{embed_code}"
        er = await asyncio.to_thread(
            probe_get, embed_url, headers=embed_headers, timeout=15
        )
        if getattr(er, "status_code", 0) != 200 or not getattr(er, "text", ""):
            return []
        api_url = extract_sightmap_api_url(er.text)
        if not api_url:
            return []
        # API call: Referer is the embed URL itself (mirrors the
        # browser's iframe → XHR chain).
        api_headers = dict(base_headers)
        api_headers["Referer"] = embed_url
        if operator_referer:
            api_headers["Origin"] = operator_referer.rstrip("/")
        ar = await asyncio.to_thread(
            probe_get, api_url, headers=api_headers, timeout=15
        )
        if getattr(ar, "status_code", 0) != 200:
            return []
        try:
            body = json.loads(getattr(ar, "text", "") or "")
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(body, dict) or not _is_sightmap_response(body):
            return []
        identity = _sightmap_identity_gate(ctx, body, api_url, result)
        if identity is None:
            return []
        units, _dropped = parse_sightmap_payload(body, api_url)
        if units:
            result.api_responses.append(
                {"url": api_url, "status": 200, "body": body, "via": "iframe_fallback"}
            )
            result.winning_url = api_url
            _record_sightmap_unit_source(
                result,
                url=api_url,
                body=body,
                unit_count=len(units),
                identity=identity,
            )
        return units
    except Exception as exc:
        result.errors.append(
            f"sightmap-iframe-fallback-error: embed={embed_code!r} "
            f"{type(exc).__name__}: {str(exc)[:120]}"
        )
        return []
