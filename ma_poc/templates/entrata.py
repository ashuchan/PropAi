"""
Entrata Tier 3 template.

Ported from the production scraper (scripts/entrata.py) with a deep selector
cascade and regex-based text extraction fallback.

Selectors (priority order):
  primary:    .entrata-unit-row, .unit-number, .unit-price, .unit-availability,
              .unit-sqft
  secondary:  tr.unit, .availability-table .unit
  entrata:    .fp-group, .floorplan-item, .floor-plan-card, .fp-item,
              [class*='FloorPlan'], [class*='floorPlan'], [class*='floor-plan']
  generic:    .apartment-item, .unit-card, .plan-card, [data-floor-plan],
              [data-unit], article, .card

Lazy-load handling: caller (tier3_templates.py) scrolls to bottom + waits
for networkidle on Playwright pages BEFORE calling extract().
"""
from __future__ import annotations

from typing import Optional

from bs4 import BeautifulSoup, Tag

from models.unit_record import AvailabilityStatus, UnitRecord
from templates._common import (
    attr_of,
    full_text,
    parse_availability,
    parse_availability_date,
    parse_floor,
    parse_floor_plan_type,
    parse_rent,
    parse_sqft,
    regex_extract_from_text,
    text_of,
)

# ── Container selector cascade (most specific -> most generic) ────────────────

_UNIT_ROW_SELECTORS = [
    ".entrata-unit-row",
    "tr.unit",
    ".availability-table .unit",
    ".unit-row",
    ".unit-listing",
]

_FLOORPLAN_CARD_SELECTORS = [
    ".fp-group",
    ".floorplan-item",
    ".floor-plan-card",
    ".fp-item",
    "[class*='FloorPlan']",
    "[class*='floorPlan']",
    "[class*='floor-plan']",
    "[class*='floorplan']",
]

_GENERIC_SELECTORS = [
    ".apartment-item",
    ".unit-card",
    ".plan-card",
    "[data-floor-plan]",
    "[data-unit]",
]


def _find_containers(soup: BeautifulSoup) -> list[Tag]:
    """Try selector cascades to find unit/floorplan containers."""
    # 1. Unit-level row selectors (most precise)
    for sel in _UNIT_ROW_SELECTORS:
        found = soup.select(sel)
        if found:
            return found

    # 2. Floorplan-level card selectors (Entrata-specific)
    for sel in _FLOORPLAN_CARD_SELECTORS:
        found = soup.select(sel)
        if len(found) >= 2:
            return found

    # 3. Generic selectors
    for sel in _GENERIC_SELECTORS:
        found = soup.select(sel)
        if len(found) >= 2:
            return found

    return []


def _extract_from_row(
    row: Tag,
    property_id: str,
    parent_fp_name: Optional[str] = None,
) -> Optional[UnitRecord]:
    """
    Extract a UnitRecord from a single row/card element.
    CSS selectors first, then regex fallback on the element's full text.
    """
    # ── CSS-selector-based extraction ─────────────────────────────────────
    # Check the row element itself for data attributes
    row_unit_attr = row.get("data-unit") or row.get("data-unit-number") or row.get("data-unit-id")
    if isinstance(row_unit_attr, list):
        row_unit_attr = row_unit_attr[0] if row_unit_attr else None

    unit_number = (
        text_of(row, ".unit-number")
        or text_of(row, ".unitNo")
        or text_of(row, ".unitNumber")
        or text_of(row, "[class*='unit-number']")
        or text_of(row, "[class*='unitNumber']")
        or (str(row_unit_attr) if row_unit_attr else None)
        or attr_of(row, "[data-unit]", "data-unit")
        or attr_of(row, "[data-unit-number]", "data-unit-number")
        or attr_of(row, "[data-unit-id]", "data-unit-id")
    )

    rent_text = (
        text_of(row, ".unit-price")
        or text_of(row, ".price")
        or text_of(row, ".rent")
        or text_of(row, "[class*='price']")
        or text_of(row, "[class*='rent']")
    )

    sqft_text = (
        text_of(row, ".unit-sqft")
        or text_of(row, ".sqft")
        or text_of(row, ".squareFeet")
        or text_of(row, "[class*='sqft']")
        or text_of(row, "[class*='sq-ft']")
        or text_of(row, "[class*='squareFeet']")
    )

    avail_text = (
        text_of(row, ".unit-availability")
        or text_of(row, ".availability")
        or text_of(row, "[class*='avail']")
        or text_of(row, "[class*='status']")
    )

    fp_text = (
        text_of(row, ".unit-floorplan")
        or text_of(row, ".floorplan")
        or text_of(row, ".floor-plan-name")
        or text_of(row, "[class*='planName']")
        or text_of(row, "[class*='plan-name']")
        or text_of(row, "[class*='floorplan']")
    )

    floor_text = (
        text_of(row, ".floor")
        or text_of(row, "[class*='floor']")
    )

    building_text = (
        text_of(row, ".building")
        or text_of(row, "[class*='building']")
    )

    # ── Heading-based name extraction (for floorplan cards) ───────────────
    if not fp_text:
        for heading_sel in ("h1", "h2", "h3", "h4", "[class*='name']", "[class*='title']"):
            fp_text = text_of(row, heading_sel)
            if fp_text and len(fp_text) < 80:
                break
            fp_text = None

    # ── Regex fallback on full text ───────────────────────────────────────
    row_text = full_text(row)
    regex_fields = regex_extract_from_text(row_text)

    if not unit_number:
        unit_number = regex_fields.get("unit_number")  # type: ignore[assignment]
    if not unit_number:
        return None

    asking_rent = parse_rent(rent_text)
    if asking_rent is None:
        asking_rent = regex_fields.get("asking_rent")  # type: ignore[assignment]

    sqft = parse_sqft(sqft_text)
    if sqft is None:
        sqft = regex_fields.get("sqft")  # type: ignore[assignment]

    availability_status = parse_availability(avail_text)
    if availability_status == AvailabilityStatus.UNKNOWN and avail_text is None:
        availability_status = regex_fields.get(  # type: ignore[assignment]
            "availability_status", AvailabilityStatus.UNKNOWN
        )

    availability_date = parse_availability_date(avail_text)
    if availability_date is None:
        availability_date = regex_fields.get("availability_date")  # type: ignore[assignment]

    floor_plan_type = parse_floor_plan_type(fp_text)
    if not floor_plan_type and parent_fp_name:
        floor_plan_type = parse_floor_plan_type(parent_fp_name)
    if not floor_plan_type:
        floor_plan_type = regex_fields.get("floor_plan_type")  # type: ignore[assignment]

    floor = parse_floor(floor_text)
    if floor is None:
        floor = regex_fields.get("floor")  # type: ignore[assignment]

    building = building_text.strip() if building_text else None

    return UnitRecord(
        property_id=property_id,
        unit_number=str(unit_number).strip(),
        floor_plan_type=floor_plan_type,
        asking_rent=asking_rent,
        sqft=sqft,
        availability_status=availability_status,
        availability_date=availability_date,
        floor=floor,
        building=building,
        extraction_tier=3,
    )


def extract(html: str, property_id: str) -> list[UnitRecord]:
    """Parse Entrata page HTML into UnitRecords. Returns [] on failure."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    containers = _find_containers(soup)
    if not containers:
        return []

    records: list[UnitRecord] = []
    seen: set[str] = set()

    for container in containers:
        rec = _extract_from_row(container, property_id)
        if rec and rec.unit_number not in seen:
            seen.add(rec.unit_number)
            records.append(rec)

    return records
