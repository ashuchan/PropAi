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
    - 2026-04-19 research note: data/runs/2026-04-17/raw_api/268836.json
      contains a unit-bearing SightMap payload (sightmap.com/app/api/v1/
      rxwjj7ldw1e/sightmaps/80671). Observed field names confirm the current
      parser: units[].{floor_plan_id, price, display_price, unit_number, label,
      area, display_area, floor_id, building, available_on, display_available_on,
      specials_description}; floor_plans[].{id, name, filter_label,
      bedroom_count, bathroom_count}. No payload starting with 24928/24929/
      liveotis/ovationco/sightmap was present in raw_api as of 2026-04-19.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


def parse_sightmap_payload(body: Any, url: str) -> list[dict[str, str]]:
    """SightMap dedicated parser.

    Joins data.units[] to data.floor_plans[] by floor_plan_id so each unit
    gets name/beds/baths from its floor plan plus price/sqft/availability.

    Ported from scripts/entrata.py:433.
    """
    units_out: list[dict[str, str]] = []
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return units_out

    raw_units = data.get("units") or []
    raw_fps = data.get("floor_plans") or []
    if not isinstance(raw_units, list) or not raw_units:
        return units_out

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
            # caller surfaces this as SIGHTMAP_PARSE_FAILED so field-name
            # drift is diagnosable rather than silently emitting stub records.
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

        units_out.append(make_unit_dict(
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
            extraction_tier="TIER_1_API_SIGHTMAP",
        ))
    return units_out


def _is_sightmap_response(body: Any) -> bool:
    """Return True if *body* looks like a SightMap API response.

    Matches on body shape rather than source URL so that portal sites
    (e.g. lasvegasliving.com) that proxy SightMap data through their own
    CDN domain are handled correctly.

    Positive match criteria (any one sufficient):
    - body is a dict with a "data" key whose value has a "units" or
      "floor_plans" or "amenities" subkey (SightMap data envelope)
    - body["data"]["sightmap_id"] exists (direct SightMap identifier)
    """
    if not isinstance(body, dict):
        return False
    data = body.get("data")
    if not isinstance(data, dict):
        return False
    sightmap_keys = {"units", "floor_plans", "amenities", "sightmap_id"}
    return bool(sightmap_keys & set(data.keys()))


class SightMapAdapter:
    """SightMap PMS adapter. Parses sightmap.com API responses."""

    pms_name: str = "sightmap"
    _fingerprints: list[str] = ["sightmap.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from SightMap API responses captured during page load."""
        result = AdapterResult(tier_used="TIER_1_API_SIGHTMAP")
        all_units: list[dict[str, str]] = []

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        for resp in api_responses:
            url = resp.get("url", "")
            body = resp.get("body")
            if not isinstance(body, dict):
                continue
            if not _is_sightmap_response(body):
                continue
            units = parse_sightmap_payload(body, url)
            if units:
                all_units.extend(units)
                result.api_responses.append(resp)

        if all_units:
            result.units = all_units
            result.winning_url = result.api_responses[0].get("url") if result.api_responses else None
            result.confidence = min(0.95, 0.7 + 0.05 * len(all_units))
        else:
            result.confidence = 0.0
            sightmap_responses = [
                r for r in api_responses
                if isinstance(r.get("body"), dict) and _is_sightmap_response(r.get("body"))
            ]
            if not sightmap_responses:
                result.errors.append(
                    "SIGHTMAP_NO_RESPONSE: no SightMap-shaped response captured — "
                    "check if the page loads sightmap.com assets at all"
                )
            else:
                for r in sightmap_responses:
                    data = r.get("body", {}).get("data", {})
                    raw_units = data.get("units") or []
                    if not raw_units:
                        result.errors.append(
                            f"SIGHTMAP_AMENITIES_ONLY: sightmap response at {r.get('url','?')[:80]} "
                            f"has no units[] — map may be configured as amenities-only; "
                            f"check for a separate /available or /assets endpoint"
                        )
                    else:
                        result.errors.append(
                            f"SIGHTMAP_PARSE_FAILED: units[] present ({len(raw_units)} entries) "
                            f"but join produced 0 records — field name mismatch likely; "
                            f"inspect raw_api payload for {r.get('url','?')[:80]}"
                        )

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
