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

import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    get_field,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.source_provenance import build_unit_source_provenance

if TYPE_CHECKING:
    from playwright.async_api import Page


def parse_avalonbay_units(
    items: list[dict[str, Any]],
    url: str,
    summary: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
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

    units: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # AvalonBay-specific fields
        public_unit_name = get_field(item, "unitName", "unitNumber", "unit_number", "label")
        native_unit_id = get_field(item, "unitId", "id")
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

        # Rent from individual unit or from summary by bedroom count.
        # 2026-05-23: Avalon's rendered-HTML Fusion blob (the dominant
        # source for the 43+ TIER_1_5_EMBEDDED properties in the
        # area-but-no-rent cohort) nests rent at
        # ``startingAtPricesUnfurnished.prices.price`` / ``.netEffective
        # Price``. The flat ``minRent`` / ``price`` paths the adapter
        # checked first miss it entirely — units extract with sqft +
        # unit_id but no rent, downgrading the verdict to SUCCESS_PLAN_
        # LEVEL with reason "no_rent_signal". Verified live across 5
        # Avalon sites 2026-05-23 (montville, meydenbauer, union,
        # frisco, alderwood) — all use the same nested layout.
        rent_lo = money_to_int(get_field(item, "minRent", "rent_min", "price", "askingRent"))
        rent_hi = money_to_int(get_field(item, "maxRent", "rent_max", "maxAskingRent"))
        if rent_lo is None:
            nested_prices = (item.get("startingAtPricesUnfurnished") or {}).get("prices") or {}
            if isinstance(nested_prices, dict):
                # Prefer the published "price" (asking rent before concessions);
                # fall back to netEffective which is identical when no promo,
                # smaller when a promo applies (we keep both lo/hi separate).
                # The Fusion JSON ships these as int literals (not strings) so
                # money_to_int (string-only) won't accept them — coerce via
                # int()/float() first, str-fallback for safety.
                def _coerce_rent(v: Any) -> int | None:
                    if v is None:
                        return None
                    if isinstance(v, (int, float)):
                        n = int(v)
                        return n if n > 0 else None
                    return money_to_int(str(v))

                rent_lo = _coerce_rent(nested_prices.get("price")) or _coerce_rent(
                    nested_prices.get("netEffectivePrice")
                )
        # Bedroom-summary fallback only when per-unit and nested both empty.
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

        unit = make_unit_dict(
            floor_plan_name=fp_name,
            bed_label=bed_label_from(beds, fp_name),
            bedrooms=str(beds) if beds is not None else "",
            bathrooms=str(baths) if baths is not None else "",
            sqft=sqft,
            unit_number=public_unit_name or native_unit_id,
            unit_name=public_unit_name,
            floor=floor,
            rent_range=format_rent_range(rent_lo, rent_hi),
            concession=concession,
            availability_status="AVAILABLE",
            availability_date=avail_date,
            source_api_url=url,
            extraction_tier="TIER_1_API_AVALONBAY",
            source_ids=({"avalonbay_unit_id": native_unit_id} if native_unit_id else None),
        )
        if native_unit_id:
            # ``unitName`` is intentionally a short public label and repeats
            # across Arlington Square buildings. The Fusion-native unitId is
            # the physical property-scoped apartment anchor.
            unit["unit_id"] = native_unit_id
        units.append(unit)
    return units


# ─── HTML embedded-JSON extractor (Fusion CMS, 2026-05-23) ────────────────

# Avalon's avaloncommunities.com property pages are SSR'd with the Fusion
# CMS (Arc Publishing). The full unit inventory ships inline in a
# ``<script id="fusion-metadata">`` blob containing
# ``Fusion.globalContent = { …, "units": [ {unitId, unitName, bedroomNumber,
# bathroomNumber, squareFeet, floorPlan: {name}, availableDateUnfurnished,
# startingAtPricesUnfurnished: {prices: {price, totalPrice, netEffective
# Price}}, … }, … ] }``. The data is COMPLETE — every field the adapter
# needs is present. The TIER_1_5_EMBEDDED generic embedded-JSON tier
# extracted ``unitId`` + ``squareFeet`` but missed the nested rent path
# entirely (43 properties stamped SUCCESS_PLAN_LEVEL by no_rent_signal).
#
# Verified live across 5 Avalon properties 2026-05-23 (montville,
# meydenbauer, union, frisco, alderwood): all use the same Fusion shape
# with 28-82 ``startingAtPricesUnfurnished`` blocks per page and unit_id
# prefix ``AVB-``.
_FUSION_UNITS_ARRAY_RE = re.compile(r'"units"\s*:\s*\[\s*(?=\{[^}]*"unitId"\s*:\s*"AVB-)', re.DOTALL)
# Gate: whitespace-tolerant. Live Fusion ships compact JSON, but the
# tests construct synthetic blobs via json.dumps (default separators
# include spaces). Either form must clear the gate.
_AVALON_UNIT_GATE_RE = re.compile(r'"unitId"\s*:\s*"AVB-', re.IGNORECASE)


def _extract_balanced_array(html: str, start: int) -> str:
    """Given an index of an opening '[', return the substring up to the
    matching ']'. Respects nested {} and [] and JSON string literals.
    Returns '' if no balanced close found within the rest of html.
    """
    if start >= len(html) or html[start] != "[":
        return ""
    depth = 0
    i = start
    in_str = False
    esc = False
    while i < len(html):
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return html[start : i + 1]
        i += 1
    return ""


def parse_avalonbay_html(html: str, url: str) -> list[dict[str, Any]]:
    """Extract Avalon unit dicts from a property page's embedded Fusion JSON.

    Returns ``[]`` when the page is not an Avalon Fusion-CMS page (the
    ``"units":[{ "unitId":"AVB-`` signature is the gate — anchored on
    both the array key and the Avalon-specific unit_id prefix so this
    cannot misfire on a non-Avalon page that happens to carry a
    ``"units":[…]`` array).

    Each found unit dict is passed through the existing
    :func:`parse_avalonbay_units`, which handles the bedroomNumber /
    squareFeet / floorPlan.name / startingAtPricesUnfurnished.prices.
    price extraction. Single source of truth for Avalon field mapping.
    """
    if not html or not _AVALON_UNIT_GATE_RE.search(html):
        return []
    m = _FUSION_UNITS_ARRAY_RE.search(html)
    if not m:
        # The whitespace-tolerant lookahead fallback handles synthetic
        # JSON.dumps fixtures (spaces after colons) — live Fusion is
        # compact and matched by the primary regex.
        m = re.search(
            r'"units"\s*:\s*\[\s*(?=\{\s*"unitId"\s*:\s*"AVB-)',
            html,
            re.DOTALL,
        )
        if not m:
            return []
    # Walk back from the regex match to find the '[' that opens the array.
    arr_start = m.end() - 1
    # The lookahead ate '{' but the array opens before it — find it.
    arr_open = html.rfind("[", m.start(), arr_start + 1)
    if arr_open < 0:
        return []
    arr_str = _extract_balanced_array(html, arr_open)
    if not arr_str:
        return []
    try:
        import json

        items = json.loads(arr_str)
    except (ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    return parse_avalonbay_units([it for it in items if isinstance(it, dict)], url)


class AvalonBayAdapter:
    """AvalonBay Communities PMS adapter."""

    pms_name: str = "avalonbay"
    _fingerprints: list[str] = ["avaloncommunities.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from AvalonBay API responses captured during page load.

        AvalonBay's community-units endpoint returns:
        {unitsSummary: {totalPricesStartingAt: {bedrooms: {unfurnished: price}}},
         units: [{unitName, bedroomNumber, squareFeet, floorPlan: {name}, ...}]}
        """
        result = AdapterResult(tier_used="TIER_1_API_AVALONBAY")
        all_units: list[dict[str, str]] = []

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        for resp in api_responses:
            body = resp.get("body")
            if not isinstance(body, dict):
                continue

            # AvalonBay-specific: look for units + unitsSummary together
            units_list = body.get("units")
            summary = body.get("unitsSummary")
            if isinstance(units_list, list) and units_list and isinstance(units_list[0], dict):
                # Check for AvalonBay-specific keys
                first = units_list[0]
                if any(k in first for k in ("bedroomNumber", "unitName", "squareFeet", "floorPlan")):
                    url = resp.get("url", "")
                    units = parse_avalonbay_units(units_list, url, summary)
                    if units:
                        all_units.extend(units)
                        result.api_responses.append(resp)
                        result.unit_source_provenance.append(
                            build_unit_source_provenance(
                                provider="avalonbay",
                                source_url=str(url),
                                body=body,
                                unit_count=len(units),
                                identity={
                                    "property_id": str(getattr(ctx, "property_id", "") or ""),
                                    "base_url": str(getattr(ctx, "base_url", "") or ""),
                                },
                            )
                        )
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
                    result.unit_source_provenance.append(
                        build_unit_source_provenance(
                            provider="avalonbay",
                            source_url=str(url),
                            body=body,
                            unit_count=len(units),
                            identity={
                                "property_id": str(getattr(ctx, "property_id", "") or ""),
                                "base_url": str(getattr(ctx, "base_url", "") or ""),
                            },
                        )
                    )

        # 2026-05-23: when no API-response path produced units, parse
        # the rendered HTML's Fusion embedded JSON. Avalon is SSR — the
        # complete unit inventory ships inline; the API-response path
        # only fires on a separate XHR which the browser may or may not
        # have replayed. This adds a deterministic source-of-truth fall-
        # through that lifts ~43 EMBEDDED-tier-classified Avalon
        # properties from SUCCESS_PLAN_LEVEL to true SUCCESS.
        if not all_units:
            html = ""
            fr = getattr(ctx, "fetch_result", None)
            body = getattr(fr, "body", None) if fr is not None else None
            if isinstance(body, bytes):
                html = body.decode("utf-8", errors="replace")
            elif isinstance(body, str):
                html = body
            base_url = str(getattr(ctx, "base_url", "") or "")
            if html:
                html_units = parse_avalonbay_html(html, base_url)
                if html_units:
                    all_units.extend(html_units)
                    # Stamp the HTML source so the verdict layer / report
                    # can distinguish this path from the XHR path. Same
                    # tier root, distinct suffix.
                    for u in all_units:
                        if u.get("extraction_tier") == "TIER_1_API_AVALONBAY":
                            u["extraction_tier"] = "TIER_1_HTML_AVALONBAY_FUSION"
                    result.tier_used = "TIER_1_HTML_AVALONBAY_FUSION"
                    result.api_responses.append(
                        {
                            "url": base_url,
                            "status": 200,
                            "body": "<avalonbay-fusion-html>",
                            "via": "html_embedded_fusion",
                        }
                    )
                    result.unit_source_provenance.append(
                        build_unit_source_provenance(
                            provider="avalonbay",
                            source_url=base_url,
                            body=html,
                            unit_count=len(html_units),
                            identity={
                                "property_id": str(getattr(ctx, "property_id", "") or ""),
                                "base_url": base_url,
                            },
                            response_kind="embedded_unit_roster",
                        )
                    )

        if all_units:
            from ma_poc.extraction.post_process import post_process

            _pp_parsed = len(all_units)
            _pp = post_process(all_units, property_id=getattr(ctx, "property_id", None))
            if _pp.n_admitted > 0:
                result.units = _pp.admitted
                result.plan_summaries = _pp.plan_summaries
                result.winning_url = result.api_responses[0].get("url") if result.api_responses else None
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
