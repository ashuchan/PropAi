"""
AppFolio adapter.

Research log
------------
Web sources consulted:
  - https://www.appfolio.com/ — AppFolio property management platform (accessed 2026-04-17)
  - https://www.appfolio.com/property-manager — Listing format documentation
Real payloads inspected (from data/runs/*/raw_api/):
  - 12617 (Stoney Brook) — /api/v1/community_info/ returning community-level metadata
    (name, address, total_unit_count, available_unit_count) but no unit-level data
  - 12807 — /api/v3/tokens/lists/ returning pagination wrapper with community info
Key findings:
  - API endpoint: /api/v1/community_info/, /api/v1/community_extra_info/,
    /api/v3/tokens/lists/ — these return property-level metadata only
  - AppFolio listing pages typically embed unit data in HTML or use
    /listings/rental_applications endpoint for individual unit detail
  - Response envelope: {meta: {limit, total_count, offset}, objects: [...]}
  - Unit ID field: not available in captured community-level APIs
  - Rent field(s): not available in community-level responses; unit pages have price in HTML
  - Known gotchas: AppFolio community API does not contain unit-level pricing; unit data
    comes from DOM parsing of the listing page. AppFolio uses a standard listing card
    layout with .js-listing-card containers. Less than 3 real payloads with unit data
    available — adapter handles API where present and falls through to DOM parsing.
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


def parse_appfolio_listings(items: list[dict[str, Any]], url: str) -> list[dict[str, str]]:
    """Parse AppFolio listing/floorplan objects into standard unit dicts.

    Handles both /listings endpoint (bedrooms, price, sqft) and
    /floorplans/all endpoint (bed, bath, rent, sq_ft).
    """
    units: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = get_field(item, "name", "listing_type", "property_type", "apartment_type")
        beds_str = get_field(item, "bed", "bedrooms", "beds", "bedroom_count")
        baths_str = get_field(item, "bath", "bathrooms", "baths", "bathroom_count")
        beds = int(float(beds_str)) if beds_str else None
        baths = int(float(baths_str)) if baths_str else None
        sqft = get_field(item, "sq_ft", "sqft", "square_feet", "squareFeet", "area")

        rent_lo = money_to_int(get_field(item, "price", "rent", "minRent", "asking_rent"))
        rent_hi = money_to_int(get_field(item, "maxRent", "max_rent"))

        unit_num = get_field(item, "unit_number", "unitNumber", "unit_id", "id", "label")
        avail_date = get_field(item, "available_date", "availableDate", "move_in_date")
        status = get_field(item, "status", "availability_status")

        units.append(make_unit_dict(
            floor_plan_name=name,
            bed_label=bed_label_from(beds, name),
            bedrooms=str(beds) if beds is not None else "",
            bathrooms=str(baths) if baths is not None else "",
            sqft=sqft,
            unit_number=unit_num,
            rent_range=format_rent_range(rent_lo, rent_hi),
            availability_status="AVAILABLE" if not status or "avail" in status.lower() else status.upper(),
            availability_date=avail_date,
            source_api_url=url,
            extraction_tier="TIER_1_API_APPFOLIO",
        ))
    return units


def _is_appfolio_response(body: Any) -> bool:
    """Check if a response body looks like AppFolio listing/floorplan data.

    Requires at least two unit-signal keys (price/rent + beds/sqft) to avoid
    false positives on community-level metadata endpoints.

    AppFolio/Apts247 uses: bed, bath, rent, sq_ft, name (floorplans endpoint)
    or bedrooms, price, sqft (listings endpoint).
    """
    _UNIT_SIGNAL_KEYS = {"sqft", "bedrooms", "price", "rent", "listing_type",
                         "square_feet", "asking_rent", "beds", "bathrooms",
                         "bed", "bath", "sq_ft", "rent_from"}

    def _has_signals(items: list[dict[str, Any]]) -> bool:
        if not items or not isinstance(items[0], dict):
            return False
        return len(_UNIT_SIGNAL_KEYS & set(items[0].keys())) >= 2

    if isinstance(body, dict):
        objects = body.get("objects") or body.get("results") or body.get("listings")
        if isinstance(objects, list):
            return _has_signals(objects)
    if isinstance(body, list):
        return _has_signals(body)
    return False


class AppFolioAdapter:
    """AppFolio PMS adapter."""

    pms_name: str = "appfolio"
    _fingerprints: list[str] = ["appfolio.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from AppFolio API responses captured during page load."""
        result = AdapterResult(tier_used="TIER_1_API_APPFOLIO")
        all_units: list[dict[str, str]] = []

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        for resp in api_responses:
            body = resp.get("body")
            if _is_appfolio_response(body):
                url = resp.get("url", "")
                items: list[dict[str, Any]] = []
                if isinstance(body, dict):
                    items = body.get("objects") or body.get("results") or body.get("listings") or []
                elif isinstance(body, list):
                    items = body
                units = parse_appfolio_listings(items, url)
                if units:
                    all_units.extend(units)
                    result.api_responses.append(resp)

        if all_units:
            result.units = all_units
            result.winning_url = result.api_responses[0].get("url") if result.api_responses else None
            result.confidence = min(0.95, 0.7 + 0.05 * len(all_units))
        else:
            result.confidence = 0.0
            result.errors.append("No AppFolio unit data found in captured API responses")

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
