"""
OneSite (RealPage) adapter.

Research log
------------
Web sources consulted:
  - https://www.realpage.com/property-management-software/onesite/ — OneSite product (accessed 2026-04-17)
  - RealPage API patterns from scripts/entrata.py and scrape_properties.py
Real payloads inspected (from data/runs/*/raw_api/):
  - 293707 — api.ws.realpage.com/v2/property/7824595/floorplans returning
    {status, message, response: {propertyKey, floorplans: [...]}} with fields:
    id, name, bedRooms, bathRooms, minimumSquareFeet, maximumSquareFeet,
    minimumMarketRent, maximumMarketRent, rentRange, depositAmount, numberOfUnitsDisplay
  - 293707 (run 2026-04-14) — same endpoint, identical schema
Key findings:
  - API endpoint: api.ws.realpage.com/v2/property/{property_id}/floorplans
    and api.ws.realpage.com/v2/property/{property_id}/units (may be null)
  - Response envelope: {status, message, response: {floorplans: [...]}}
  - Unit ID field: id (floorplan-level)
  - Rent field(s): minimumMarketRent/maximumMarketRent (numbers), rentRange (display string)
  - Known gotchas: /units endpoint can return null, [], or {response: null} — three
    shapes for "no availability". When /units is null, emit floorplan-level records
    with rent but no unit_number. Split-endpoint pattern: floorplans + units are
    separate API calls. OneSite URLs have numeric prefix: {id}.onlineleasing.realpage.com
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._daily_runner_parsers import (
    realpage_units_to_adapter_shape as _dr_realpage_units,
)
from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


def parse_realpage_floorplans(body: dict[str, Any], url: str) -> list[dict[str, str]]:
    """Parse RealPage /floorplans response into standard unit dicts."""
    units: list[dict[str, str]] = []
    response = body.get("response", {})
    if not isinstance(response, dict):
        return units
    floorplans = response.get("floorplans") or []
    if not isinstance(floorplans, list):
        return units

    for fp in floorplans:
        if not isinstance(fp, dict):
            continue
        name = str(fp.get("name") or "")
        beds_raw = fp.get("bedRooms")
        baths_raw = fp.get("bathRooms")
        beds = int(beds_raw) if beds_raw is not None and str(beds_raw).isdigit() else None
        baths = int(baths_raw) if baths_raw is not None and str(baths_raw).isdigit() else None

        sqft_lo = str(fp.get("minimumSquareFeet") or "")
        sqft_hi = str(fp.get("maximumSquareFeet") or "")
        sqft = sqft_lo if sqft_lo == sqft_hi or not sqft_hi else f"{sqft_lo}-{sqft_hi}"

        rent_lo_raw = fp.get("minimumMarketRent")
        rent_hi_raw = fp.get("maximumMarketRent")
        rent_lo = int(rent_lo_raw) if isinstance(rent_lo_raw, (int, float)) else None
        rent_hi = int(rent_hi_raw) if isinstance(rent_hi_raw, (int, float)) else None

        deposit = str(fp.get("depositAmount") or "")
        num_units = str(fp.get("numberOfUnitsDisplay") or "")
        # 2026-05-19: OneSite floorplan objects sometimes carry a first-
        # available date the adapter previously dropped (fleet-wide 0%
        # available_date on TIER_1_API_ONESITE). Alias-tolerant + additive:
        # empty when absent, so no existing output changes. schema_v2.
        # _format_date handles "NOW"/relative/2-digit forms downstream.
        avail_dt = next(
            (
                str(fp[k])
                for k in (
                    "availableDate",
                    "firstAvailableDate",
                    "dateAvailable",
                    "minimumAvailableDate",
                    "availabilityDate",
                    "available_date",
                    "minAvailableDate",
                )
                if fp.get(k)
            ),
            "",
        )

        units.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths) if baths is not None else "",
                sqft=sqft,
                unit_number=str(fp.get("id") or ""),
                rent_range=format_rent_range(rent_lo, rent_hi),
                deposit=deposit,
                availability_status="AVAILABLE",
                availability_date=avail_dt,
                available_units=num_units,
                source_api_url=url,
                extraction_tier="TIER_1_API_ONESITE",
            )
        )
    return units


def _is_realpage_response(body: Any) -> bool:
    """Check if a response body looks like a RealPage API response."""
    if not isinstance(body, dict):
        return False
    response = body.get("response")
    if isinstance(response, dict) and "floorplans" in response:
        return True
    return False


def _is_realpage_units_response(body: Any, url: str) -> bool:
    """Check for the RealPage /units endpoint shape.

    RealPage splits data across /floorplans and /units. The /units endpoint
    can return null, [], or {response: null} when nothing is available, and
    a list of unit dicts when there is. daily_runner's
    _realpage_units_from_body handles all three shapes.
    """
    if "realpage" not in url.lower():
        return False
    if "/units" not in url.lower():
        return False
    # Accept null / [] / {response: [...]}
    return True


class OneSiteAdapter:
    """OneSite (RealPage) PMS adapter."""

    pms_name: str = "onesite"
    _fingerprints: list[str] = ["onlineleasing.realpage.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from RealPage API responses captured during page load.

        Tier labels (post-2026-05-20 cluster #6 fix):

          ``TIER_1_API_ONESITE``           — real unit-level data parsed
          ``TIER_1_API_ONESITE_EMPTY``     — parsed but post_process admitted 0
                                             (validity gate rejected everything)
          ``TIER_1_API_ONESITE_NO_RESPONSE`` — no RealPage-shaped responses at
                                             all (page is an OLL widget shell
                                             that hasn't fired its API yet —
                                             cluster #6 pattern)

        Empty-exit labels (``_EMPTY``, ``_NO_RESPONSE``) trigger the
        scraper's Path B/C retry mechanism with the next-best PMS, AND
        let Step 8 generic fallback run. Previously the adapter set the
        bare success label even on no-data, blocking both recovery paths.
        """
        result = AdapterResult(tier_used="TIER_1_API_ONESITE")
        all_units: list[dict[str, str]] = []
        # Track whether any RealPage-shaped response was seen at all so
        # we can distinguish "page is an empty OLL shell" from "data was
        # there but everything failed validity".
        saw_any_realpage_response = False

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        for resp in api_responses:
            body = resp.get("body")
            url = resp.get("url", "")
            if _is_realpage_response(body) and isinstance(body, dict):
                saw_any_realpage_response = True
                units = parse_realpage_floorplans(body, url)
                if units:
                    all_units.extend(units)
                    result.api_responses.append(resp)
            elif _is_realpage_units_response(body, url):
                saw_any_realpage_response = True
                # RealPage /units endpoint — body may be null/[]/{response:[...]}
                try:
                    units = _dr_realpage_units(body, url) or []
                except Exception as exc:
                    units = []
                    result.errors.append(f"realpage-units-parse-error: {exc}")
                if units:
                    all_units.extend(units)
                    result.api_responses.append(resp)

        if all_units:
            # Stage 1 validity gate.
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
            else:
                # Responses were captured but every row failed the validity
                # gate. Mark as empty-exit so retry / Step 8 generic can try.
                result.tier_used = "TIER_1_API_ONESITE_EMPTY"
                result.confidence = 0.0
                result.errors.append(
                    f"ONESITE_VALIDITY_REJECTED: {_pp_parsed} parsed rows "
                    f"failed unit_validity (no numeric dimension)"
                )
        else:
            # No usable RealPage data at all. Two flavors:
            #   _NO_RESPONSE — no RealPage-shaped responses captured (cluster
            #                  #6 OLL-widget-shell pattern: page bodyLen ~ 386,
            #                  OneSite floorplans API never fired)
            #   _EMPTY       — RealPage responses captured but floorplans/
            #                  units lists were all empty
            if saw_any_realpage_response:
                result.tier_used = "TIER_1_API_ONESITE_EMPTY"
            else:
                result.tier_used = "TIER_1_API_ONESITE_NO_RESPONSE"
            result.confidence = 0.0
            result.errors.append(
                "No RealPage/OneSite floorplan data found in captured API responses"
            )

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
