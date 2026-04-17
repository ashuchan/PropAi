"""
Generic fallback adapter.

This adapter contains what remains of the current cascade from the main scraper
after PMS-specific branches (widget filter, map parser, API probe) are moved
into their respective adapters.

Research log
------------
Web sources consulted:
  - Internal: scripts main scraper parse_api_responses() (lines 503-664)
  - Internal: scripts main scraper extract_embedded_json() (lines 1229-1363)
  - Internal: scripts main scraper parse_jsonld() (lines 926-1008)
  - Internal: scripts main scraper parse_dom() (lines 1012-1176)
Real payloads inspected (from data/runs/*/raw_api/):
  - Multiple properties with various API shapes (Yardi /api/v1/, /api/v3/,
    Knock doorway-api, custom REST endpoints)
  - 12617 (Stoney Brook) — community_info endpoint (community-level only)
  - 254976 (San Artes) — gounion property status endpoint (property metadata)
Key findings:
  - Generic parser must handle 50+ key name variants for unit fields
  - Response envelopes vary: direct list[], {objects: [...]}, {data: {units: [...]}},
    {response: {floorplans: [...]}}, {results: [...]}
  - LLM/Vision tiers only run for pms=="unknown"; detected PMS failures skip LLM
  - Ported from parse_api_responses() with PMS-specific branches removed
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    get_field,
    make_unit_dict,
    money_to_int,
    rent_in_sanity_range,
)
from pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


def _find_unit_list(body: Any) -> list[dict[str, Any]]:
    """Attempt to find a list of unit/floorplan dicts in an API response body.

    Searches multiple envelope shapes: direct list, dict with known keys,
    one level of nesting (data.units, response.floorplans, etc.).
    """
    _LIST_KEYS = (
        "floorPlans", "floor_plans", "FloorPlans", "floorplans",
        "units", "apartments", "availabilities",
        "results", "items", "listings",
    )

    if isinstance(body, list) and body and isinstance(body[0], dict):
        return body

    if isinstance(body, dict):
        for k in _LIST_KEYS:
            v = body.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        # One level deeper
        for outer in ("data", "response", "result", "body"):
            nested = body.get(outer)
            if isinstance(nested, dict):
                for k in _LIST_KEYS:
                    v = nested.get(k)
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        return v
            # response might be a list directly
            if isinstance(nested, list) and nested and isinstance(nested[0], dict):
                return nested

    return []


def _has_unit_signals(items: list[dict[str, Any]]) -> bool:
    """Check if a list of dicts has enough unit/floorplan signals to be worth parsing."""
    if not items:
        return False
    _SIGNAL_KEYS = {
        "rent", "minRent", "maxRent", "min_rent", "max_rent",
        "price", "askingRent", "monthlyRent", "baseRent",
        "bedrooms", "beds", "bedRooms", "bed", "sqft", "squareFeet",
        "square_footage", "sq_ft", "minimumSquareFeet",
        "no_of_bedroom", "unitNumber", "unit_number", "unitId", "unit_id",
        "floorPlanName", "floor_plan_name", "floorplan_name", "floorplan-name",
        "availableDate", "available_date", "availableCount",
        "minimumRent", "maximumRent", "minimumMarketRent", "maximumMarketRent",
        "rentRange", "depositAmount", "numberOfUnitsDisplay",
    }
    sample_keys = set(items[0].keys())
    return len(sample_keys & _SIGNAL_KEYS) >= 2


def parse_generic_api(items: list[dict[str, Any]], url: str) -> list[dict[str, str]]:
    """Parse a generic list of unit/floorplan dicts using broad key name matching.

    Ported from the main scraper parse_api_responses() with PMS-specific branches
    removed (all PMS parsers moved to their own adapters).
    """
    units: list[dict[str, str]] = []
    seen: set[str] = set()

    for item in items:
        if not isinstance(item, dict):
            continue

        name = get_field(item, "floorPlanName", "floor_plan_name", "floorplan_name",
                         "floorplan-name", "name", "unitType", "planName")
        beds_str = get_field(item, "bedrooms", "beds", "bedroom_count", "bedRooms",
                             "numBedrooms", "no_of_bedroom", "bd", "bed")
        baths_str = get_field(item, "bathrooms", "baths", "bathroom_count", "bathRooms",
                              "numBathrooms", "no_of_bathroom", "ba", "bath")
        sqft_str = get_field(item, "sqft", "squareFeet", "square_feet", "minSqft",
                             "minimumSquareFeet", "size", "area", "square_footage",
                             "sq_ft", "maximumSquareFeet")
        unit_num = get_field(item, "unitNumber", "unit_number", "unitId", "unit_id",
                             "label", "display_unit_number", "id", "unit_name")
        rent_lo_str = get_field(item, "minRent", "rent_min", "min_rent", "startingFrom",
                                "askingRent", "price", "rent", "minimumRent",
                                "minimumMarketRent", "baseRent", "display_price",
                                "monthlyRent", "startingPrice")
        rent_hi_str = get_field(item, "maxRent", "rent_max", "max_rent", "maxAskingRent",
                                "endingAt", "maximumRent", "maximumMarketRent")
        avail_str = get_field(item, "availableCount", "available_count", "numAvailable",
                              "unitsAvailable", "units_available", "availableUnitsCount")
        avail_dt = get_field(item, "availableDate", "available_date", "moveInDate",
                             "moveInReady", "availableOn", "readyDate")
        floor_str = get_field(item, "floor", "floorNumber", "floor_id", "floorId")
        building_str = get_field(item, "building", "buildingName", "building_name")
        deposit_str = get_field(item, "deposit", "securityDeposit", "security_deposit",
                                "depositAmount")
        concession_str = get_field(item, "concession", "special", "promotion",
                                   "specials_description", "specialsDescription")
        plan_type = get_field(item, "floorPlanType", "type", "bedBath", "BedBath")
        status_str = get_field(item, "status", "availability_status", "leaseStatus", "unit_status")

        # Dedup gate: skip if missing ALL of [name, beds, sqft, rent_lo]
        if not any([name, beds_str, sqft_str, rent_lo_str]):
            continue

        # Dedup key
        dedup = unit_num or f"{name}|{beds_str}|{sqft_str}|{rent_lo_str}"
        if dedup in seen:
            continue
        seen.add(dedup)

        beds = int(float(beds_str)) if beds_str else None
        baths = int(float(baths_str)) if baths_str else None
        rent_lo = money_to_int(rent_lo_str)
        rent_hi = money_to_int(rent_hi_str)

        # Rent sanity check
        if not rent_in_sanity_range(rent_lo) or not rent_in_sanity_range(rent_hi):
            continue

        bl = bed_label_from(beds, name)
        if not bl and plan_type:
            bl = plan_type

        units.append(make_unit_dict(
            floor_plan_name=name,
            bed_label=bl,
            bedrooms=str(beds) if beds is not None else "",
            bathrooms=str(baths) if baths is not None else "",
            sqft=sqft_str,
            unit_number=unit_num,
            floor=floor_str,
            building=building_str,
            rent_range=format_rent_range(rent_lo, rent_hi),
            deposit=deposit_str,
            concession=concession_str,
            availability_status=status_str.upper() if status_str else "AVAILABLE",
            available_units=avail_str,
            availability_date=avail_dt,
            source_api_url=url,
            extraction_tier="TIER_1_API",
        ))

    return units


class GenericAdapter:
    """Generic fallback adapter.

    Contains the generic API parser and (when pms=="unknown") the full cascade
    including LLM/Vision tiers. When invoked for a detected PMS that failed,
    LLM/Vision tiers are skipped (controlled by ctx or skip_llm flag).
    """

    pms_name: str = "generic"
    _fingerprints: list[str] = []

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Run generic extraction cascade on captured API responses.

        For detected PMS failures (pms != "unknown"), only deterministic tiers run.
        LLM/Vision are reserved for truly unknown sites.
        """
        result = AdapterResult(tier_used="TIER_1_API")
        all_units: list[dict[str, str]] = []
        skip_llm = ctx.detected.pms != "unknown"

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        for resp in api_responses:
            body = resp.get("body")
            items = _find_unit_list(body)
            if items and _has_unit_signals(items):
                url = resp.get("url", "")
                units = parse_generic_api(items, url)
                if units:
                    all_units.extend(units)
                    result.api_responses.append(resp)

        if all_units:
            result.units = all_units
            result.winning_url = result.api_responses[0].get("url") if result.api_responses else None
            result.confidence = min(0.85, 0.6 + 0.05 * len(all_units))
            return result

        # No units from API — if LLM is allowed (unknown PMS), the orchestrator
        # will handle LLM/Vision tiers. We just report failure here.
        result.confidence = 0.0
        if skip_llm:
            result.errors.append(
                f"Generic fallback found no units for detected PMS '{ctx.detected.pms}'; "
                "LLM/Vision skipped for non-unknown PMS"
            )
        else:
            result.errors.append("Generic parser found no units in captured API responses")

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
