"""
AvalonBay adapter.

Research log
------------
Web sources consulted:
  - https://www.avaloncommunities.com/ — AvalonBay Communities public site (accessed 2026-04-17)
  - AvalonBay is a single-REIT custom stack; all properties share avaloncommunities.com
Real payloads inspected (from data/runs/*/raw_api/):
  - No AvalonBay-specific API captures in current data set (fewer than 3 real payloads)
  - AvalonBay properties not present in the 78-property CSV used for captured runs
Key findings:
  - API endpoint: avaloncommunities.com uses a custom React SPA with embedded JSON data
    and/or XHR calls to internal API endpoints for pricing/availability
  - Response envelope: varies; typically embedded in page JS or fetched via XHR
  - Known gotchas: AvalonBay is a single REIT with a custom stack — not a PMS platform
    used by multiple management companies. All AvalonBay properties live on
    avaloncommunities.com. Without real captured payloads, this adapter implements
    a generic API response parser for the expected field patterns. Research-blocked
    until real captures are available.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    get_field,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


def parse_avalonbay_units(
    items: list[dict[str, Any]],
    url: str,
    summary: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Parse AvalonBay unit objects into standard unit dicts.

    AvalonBay's community-units API returns units with bedroomNumber, bathroomNumber,
    squareFeet, unitName, floorPlan.name, floorNumber, availableDateUnfurnished.
    Rent is NOT on individual units — it's in unitsSummary.totalPricesStartingAt
    keyed by bedroom count string ("0", "1", "2", "3").
    """
    # Build bedroom -> starting rent lookup from summary.
    starting_rents: dict[int, int] = {}
    if summary:
        prices = summary.get("totalPricesStartingAt") or summary.get("netEffectivePricesStartingAt") or {}
        for bed_key, price_obj in prices.items():
            if isinstance(price_obj, dict):
                rent_val = price_obj.get("unfurnished") or price_obj.get("furnished")
                if isinstance(rent_val, (int, float)):
                    starting_rents[int(bed_key)] = int(rent_val)

    units: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # AvalonBay-specific fields
        unit_name = get_field(item, "unitName", "unitNumber", "unit_number", "unitId", "id", "label")
        beds_str = get_field(item, "bedroomNumber", "bedrooms", "beds", "bedRooms", "bedroom_count")
        baths_str = get_field(item, "bathroomNumber", "bathrooms", "baths", "bathRooms", "bathroom_count")
        beds = int(float(beds_str)) if beds_str else None
        baths = int(float(baths_str)) if baths_str else None
        sqft = get_field(item, "squareFeet", "sqft", "square_feet", "area")

        # Floor plan name from nested object or flat field
        fp_obj = item.get("floorPlan")
        if isinstance(fp_obj, dict):
            fp_name = fp_obj.get("name") or ""
        else:
            fp_name = get_field(item, "floorPlanName", "floorplanName", "name", "planName")

        # Rent from individual unit or from summary by bedroom count
        rent_lo = money_to_int(get_field(item, "minRent", "rent_min", "price", "askingRent"))
        rent_hi = money_to_int(get_field(item, "maxRent", "rent_max", "maxAskingRent"))
        if rent_lo is None and beds is not None and beds in starting_rents:
            rent_lo = starting_rents[beds]

        floor = get_field(item, "floorNumber", "floor", "floor_id")
        avail_date = get_field(item, "availableDateUnfurnished", "availableDate", "available_date")
        # Trim ISO timestamp to date portion
        if avail_date and "T" in avail_date:
            avail_date = avail_date.split("T")[0]

        concession = ""
        promos = item.get("promotions")
        if isinstance(promos, list) and promos:
            concession = promos[0].get("promotionTitle", "")

        units.append(
            make_unit_dict(
                floor_plan_name=fp_name,
                bed_label=bed_label_from(beds, fp_name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths) if baths is not None else "",
                sqft=sqft,
                unit_number=unit_name,
                floor=floor,
                rent_range=format_rent_range(rent_lo, rent_hi),
                concession=concession,
                availability_status="AVAILABLE",
                availability_date=avail_date,
                source_api_url=url,
                extraction_tier="TIER_1_API_AVALONBAY",
            )
        )
    return units


import re as _re_dom

# 2026-05-24 Phase 1 cascade — AvalonBay DOM fallback.
#
# AvalonBay marketing pages SSR-render unit listings into ``.unit-item``
# cards. Each card carries inline text in the canonical AvalonBay format:
#
#   "[Special|Virtual tour] <unit_id> <community_name> "
#   "<beds> bed · <baths> bath · <sqft> sqft · Available "
#   "[<package_name>] Base rent starting at $ <rent> / <term> mo. lease "
#   "[Furnished starting at $ <furn_rent>] Available starting <date>"
#
# Live-verified on PID 1918 (eaves West Windsor — 6 .unit-item cards) and
# PID 36964 (Avalon Meydenbauer — 6 .unit-item cards) on 2026-05-24. The
# selector `.unit-item` appears 70-80x per page (including child markup);
# the parent containers ARE the unit rows. Filter by structural content
# check to avoid sub-element noise.
#
# This is the ``try_dom`` companion to the JSON-API path in ``extract``;
# kicks in when the API path doesn't return units (capture missed,
# bot-blocked, or schema drift).

_AVB_UNIT_ID_RE = _re_dom.compile(r"\b(\d{3}-\w+|\d{2}[A-Z]-\d+)\b")
# Match either "<N> bed" / "<N> beds" OR a bare "Studio" / "studio" token.
# AvalonBay text "Studio · 1 bath · 506 sqft" has no "bed" suffix after
# "Studio" so the prior regex requiring "<n> bed" missed studios entirely.
_AVB_BEDS_RE = _re_dom.compile(
    r"\b(\d+)\s*beds?\b|\b(studio)\b",
    _re_dom.IGNORECASE,
)
_AVB_BATHS_RE = _re_dom.compile(r"\b(\d+(?:\.\d+)?)\s*bath\b", _re_dom.IGNORECASE)
_AVB_SQFT_RE = _re_dom.compile(r"\b(\d{2,5})\s*sqft\b", _re_dom.IGNORECASE)
_AVB_RENT_RE = _re_dom.compile(
    r"Base\s+rent\s+starting\s+at\s*\$\s*([\d,]+)",
    _re_dom.IGNORECASE,
)
_AVB_AVAIL_RE = _re_dom.compile(
    r"Available\s+starting\s+([A-Z][a-z]{2}\s+\d{1,2}(?:,\s*\d{4})?)",
)


def parse_avalonbay_dom_units(html: str, url: str) -> list[dict[str, str]]:
    """Parse AvalonBay SSR ``.unit-item`` cards from rendered HTML.

    Returns the same unit-dict shape as ``parse_avalonbay_units`` so the
    downstream cascade treats them identically. Skips elements that
    don't carry the canonical "Base rent starting at $" sentinel — the
    ``.unit-item`` class is reused on parent containers + image
    wrappers that we don't want to emit.
    """
    if not html or "unit-item" not in html:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []
    units: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for node in soup.select(".unit-item"):
        text = node.get_text(" ", strip=True)
        if not text or "Base rent starting at" not in text:
            continue
        rent_m = _AVB_RENT_RE.search(text)
        if not rent_m:
            continue
        rent_val = money_to_int(rent_m.group(1))
        if rent_val is None:
            continue
        unit_id_m = _AVB_UNIT_ID_RE.search(text)
        unit_id = unit_id_m.group(1) if unit_id_m else ""
        if unit_id and unit_id in seen_ids:
            continue
        if unit_id:
            seen_ids.add(unit_id)
        beds_m = _AVB_BEDS_RE.search(text)
        beds: int | None = None
        if beds_m:
            # Group 1 = numeric beds, group 2 = "studio" sentinel.
            num_v = beds_m.group(1)
            studio_v = beds_m.group(2)
            if studio_v:
                beds = 0
            elif num_v:
                try:
                    beds = int(num_v)
                except (ValueError, TypeError):
                    beds = None
        baths_m = _AVB_BATHS_RE.search(text)
        baths: float | None = float(baths_m.group(1)) if baths_m else None
        sqft_m = _AVB_SQFT_RE.search(text)
        sqft = sqft_m.group(1) if sqft_m else ""
        avail_m = _AVB_AVAIL_RE.search(text)
        avail_raw = avail_m.group(1) if avail_m else ""
        units.append(
            make_unit_dict(
                floor_plan_name="",
                bed_label=bed_label_from(beds, ""),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(int(baths) if baths and baths == int(baths) else baths) if baths is not None else "",
                sqft=sqft,
                unit_number=unit_id,
                rent_range=format_rent_range(rent_val, rent_val),
                availability_status="AVAILABLE",
                availability_date=avail_raw,
                source_api_url=url,
                extraction_tier="TIER_3_DOM_AVALONBAY_SSR",
            )
        )
    return units


class AvalonBayAdapter:
    """AvalonBay Communities PMS adapter."""

    pms_name: str = "avalonbay"
    _fingerprints: list[str] = ["avaloncommunities.com"]

    async def try_dom(self, page: Any, html: str, ctx: AdapterContext) -> Any:
        """2026-05-24 Phase 1 cascade hook — DOM fallback for AvalonBay
        when the API capture missed or returned empty.

        Wraps ``parse_avalonbay_dom_units`` which extracts ``.unit-item``
        cards from SSR HTML. Routes units through shared dq_guards.
        Live-verified on PID 1918 (6 units) + PID 36964 (6 units) —
        canonical AvalonBay marketing page shape on 2026-05-24.
        """
        from ma_poc.pms.adapters.base import AdapterDomResult

        if not html or ".unit-item" not in html and "unit-item" not in html:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_AVALONBAY_SSR",
                reason="no_unit_item_marker",
            )
        try:
            url = getattr(ctx, "base_url", "") or ""
            raw_units = parse_avalonbay_dom_units(html, url)
        except Exception as e:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_AVALONBAY_SSR",
                reason=f"parse_exception:{type(e).__name__}",
            )
        if not raw_units:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_AVALONBAY_SSR",
                reason="parser_silent_empty",
            )
        try:
            from ma_poc.extraction.dq_guards import apply_unit_guards
            guarded = apply_unit_guards(
                raw_units,
                property_id=getattr(ctx, "property_id", ""),
                source_html=html,
                detect_same_rent=True,
            )
        except Exception:
            guarded = raw_units
        if not guarded:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_AVALONBAY_SSR",
                reason="dq_guards_rejected_all",
            )
        return AdapterDomResult(
            units=guarded,
            plan_summaries=[],
            tier_used="TIER_3_DOM_AVALONBAY_SSR",
            selector_signature=".unit-item+Base-rent-starting-at",
            confidence=0.9 if len(guarded) >= 3 else 0.75,
            debug={"raw_count": len(raw_units), "guarded_count": len(guarded)},
        )

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from AvalonBay API responses captured during page load.

        AvalonBay's community-units endpoint returns:
        {unitsSummary: {totalPricesStartingAt: {bedrooms: {unfurnished: price}}},
         units: [{unitName, bedroomNumber, squareFeet, floorPlan: {name}, ...}]}
        """
        result = AdapterResult(tier_used="TIER_1_API_AVALONBAY")
        all_units: list[dict[str, str]] = []
        # 2026-05-22 Phase 2c: collect the latest unitsSummary seen across
        # responses so plan_summary emission below can iterate
        # ``totalPricesStartingAt`` for bedroom buckets that have no live
        # unit rows.
        latest_summary: dict[str, Any] | None = None

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        for resp in api_responses:
            body = resp.get("body")
            if not isinstance(body, dict):
                continue

            # AvalonBay-specific: look for units + unitsSummary together
            units_list = body.get("units")
            summary = body.get("unitsSummary")
            if isinstance(summary, dict):
                latest_summary = summary
            if isinstance(units_list, list) and units_list and isinstance(units_list[0], dict):
                # Check for AvalonBay-specific keys
                first = units_list[0]
                if any(k in first for k in ("bedroomNumber", "unitName", "squareFeet", "floorPlan")):
                    url = resp.get("url", "")
                    units = parse_avalonbay_units(units_list, url, summary)
                    if units:
                        all_units.extend(units)
                        result.api_responses.append(resp)
                    continue

            # Fallback: generic envelope search for non-AvalonBay responses
            items: list[dict[str, Any]] = []
            for key in ("units", "floorPlans", "floor_plans", "apartments", "results"):
                candidate = body.get(key)
                if isinstance(candidate, list) and candidate and isinstance(candidate[0], dict):
                    items = candidate
                    break
            if not items:
                for outer_key in ("data", "response", "result"):
                    nested = body.get(outer_key)
                    if isinstance(nested, dict):
                        for key in ("units", "floorPlans", "floor_plans", "apartments"):
                            candidate = nested.get(key)
                            if isinstance(candidate, list) and candidate and isinstance(candidate[0], dict):
                                items = candidate
                                break
                    if items:
                        break

            if items:
                url = resp.get("url", "")
                units = parse_avalonbay_units(items, url)
                if units:
                    all_units.extend(units)
                    result.api_responses.append(resp)

        # 2026-05-22 Phase 2c — bedroom buckets in unitsSummary with no live
        # unit rows become plan_summary rows. AvalonBay's
        # ``totalPricesStartingAt`` keys are bedroom counts ("0", "1", "2",
        # "3"); each represents a coarse floor-plan grouping visible on
        # the marketing site as "Studios from $1,500", "1BR from $1,800".
        # Plans without an emitted unit silently disappeared pre-fix.
        if latest_summary:
            covered_beds: set[int] = set()
            for u in all_units:
                beds_str = u.get("bedrooms")
                if beds_str:
                    try:
                        covered_beds.add(int(beds_str))
                    except (TypeError, ValueError):
                        pass
            prices = (
                latest_summary.get("totalPricesStartingAt")
                or latest_summary.get("netEffectivePricesStartingAt")
                or {}
            )
            if isinstance(prices, dict):
                for bed_key, price_obj in prices.items():
                    try:
                        beds_int = int(bed_key)
                    except (TypeError, ValueError):
                        continue
                    if beds_int in covered_beds:
                        continue  # dup-prevention: bucket has a live unit row
                    if not isinstance(price_obj, dict):
                        continue
                    rent_val = price_obj.get("unfurnished") or price_obj.get("furnished")
                    if not isinstance(rent_val, (int, float)) or rent_val <= 0:
                        continue
                    all_units.append(
                        make_unit_dict(
                            floor_plan_name=(
                                "Studio" if beds_int == 0 else f"{beds_int} Bedroom"
                            ),
                            bed_label=bed_label_from(beds_int, ""),
                            bedrooms=str(beds_int),
                            bathrooms="",
                            sqft="",
                            unit_number="",  # → post_process routes to plan_summaries
                            rent_low=int(rent_val),
                            rent_high=int(rent_val),
                            availability_status="UNKNOWN",
                            availability_date="",
                            source_api_url=(
                                result.api_responses[0].get("url", "")
                                if result.api_responses else ""
                            ),
                            extraction_tier="TIER_1_API_AVALONBAY",
                        )
                    )

        if all_units:
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
                result.confidence = min(0.90, 0.7 + 0.05 * _pp.n_admitted)
            else:
                result.confidence = 0.0
                result.errors.append(
                    f"AVALONBAY_VALIDITY_REJECTED: {_pp_parsed} parsed rows "
                    f"failed unit_validity (no numeric dimension)"
                )
        else:
            result.confidence = 0.0
            result.errors.append("No AvalonBay unit data found in captured API responses")

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
