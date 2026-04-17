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

from pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    get_field,
    make_unit_dict,
    money_to_int,
)
from pms.adapters.base import AdapterContext, AdapterResult

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

        units.append(make_unit_dict(
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
        ))
    return units


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

        if all_units:
            result.units = all_units
            result.winning_url = result.api_responses[0].get("url") if result.api_responses else None
            result.confidence = min(0.90, 0.7 + 0.05 * len(all_units))
        else:
            result.confidence = 0.0
            result.errors.append("No AvalonBay unit data found in captured API responses")

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
