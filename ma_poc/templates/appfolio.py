"""
AppFolio Tier 3 template.

Selectors (priority order):
  primary:    .js-listing-card, .listing-unit-detail-table, .price, .status, .sqft
  secondary:  .listing-card, table.units tr, .property-unit
  generic:    [data-unit-number], .unit-card, .unit-row, .apartment-item

Handles paginated unit tables (each page concatenated by caller before extract).
Falls back to regex-based text extraction when CSS selectors miss fields.
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

# ── Container selector cascade ────────────────────────────────────────────────

_PRIMARY_SELECTORS = [
    ".js-listing-card",
]

_SECONDARY_SELECTORS = [
    ".listing-card",
    "table.units tr",
    ".property-unit",
    ".listing-unit-detail-table tr",
]

_GENERIC_SELECTORS = [
    "[data-unit-number]",
    ".unit-card",
    ".unit-row",
    ".apartment-item",
    "[data-unit]",
]


def _find_containers(soup: BeautifulSoup) -> list[Tag]:
    """Try selector cascades to find listing card containers."""
    for sel in _PRIMARY_SELECTORS:
        found = soup.select(sel)
        if found:
            return found

    for sel in _SECONDARY_SELECTORS:
        found = soup.select(sel)
        if found:
            return found

    for sel in _GENERIC_SELECTORS:
        found = soup.select(sel)
        if len(found) >= 2:
            return found

    return []


def _extract_from_card(card: Tag, property_id: str) -> Optional[UnitRecord]:
    """
    Extract a UnitRecord from a single listing card.
    CSS selectors first, then regex fallback on full card text.
    """
    # ── CSS-selector-based extraction ─────────────────────────────────────
    # Check the card element itself for data attributes first
    card_unit_attr = card.get("data-unit-number") or card.get("data-unit")
    if isinstance(card_unit_attr, list):
        card_unit_attr = card_unit_attr[0] if card_unit_attr else None

    unit_number = (
        text_of(card, ".unit-number")
        or text_of(card, ".js-listing-card-unit")
        or text_of(card, ".unitNumber")
        or text_of(card, "[class*='unit-number']")
        or (str(card_unit_attr) if card_unit_attr else None)
        or attr_of(card, "[data-unit-number]", "data-unit-number")
        or attr_of(card, "[data-unit]", "data-unit")
    )

    rent_text = (
        text_of(card, ".price")
        or text_of(card, ".rent")
        or text_of(card, ".unit-price")
        or text_of(card, "[class*='price']")
        or text_of(card, "[class*='rent']")
    )

    sqft_text = (
        text_of(card, ".sqft")
        or text_of(card, ".size")
        or text_of(card, ".squareFeet")
        or text_of(card, "[class*='sqft']")
        or text_of(card, "[class*='sq-ft']")
    )

    avail_text = (
        text_of(card, ".status")
        or text_of(card, ".availability")
        or text_of(card, "[class*='status']")
        or text_of(card, "[class*='avail']")
    )

    fp_text = (
        text_of(card, ".unit-type")
        or text_of(card, ".bed-bath")
        or text_of(card, ".floorplan")
        or text_of(card, "[class*='type']")
        or text_of(card, "[class*='bed']")
    )

    floor_text = (
        text_of(card, ".floor")
        or text_of(card, "[class*='floor']")
    )

    building_text = (
        text_of(card, ".building")
        or text_of(card, "[class*='building']")
    )

    # ── Regex fallback on card text ───────────────────────────────────────
    card_text = full_text(card)
    regex_fields = regex_extract_from_text(card_text)

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
    """Parse AppFolio page HTML into UnitRecords. Returns [] on failure."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    containers = _find_containers(soup)
    if not containers:
        return []

    records: list[UnitRecord] = []
    seen: set[str] = set()

    for card in containers:
        rec = _extract_from_card(card, property_id)
        if rec and rec.unit_number not in seen:
            seen.add(rec.unit_number)
            records.append(rec)

    return records
