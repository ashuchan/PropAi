"""
RentCafe Tier 3 template.

Selectors (primary + secondary + generic fallbacks):
  primary:    .unitContainer, .pricingWrapper, .floorplanName, .unitNumber,
              .rent, .availabilityDate, .sqft
  secondary:  div[data-unit], .unit-row, .unit-card
  generic:    [class*='floorplan'], [class*='FloorPlan'], .fp-group,
              .floorplan-item, .apartment-item, .plan-card, article, .card

Handles list view AND floorplan-grouped view.
Falls back to regex-based text extraction when CSS selectors miss fields.
Returns [] on total selector failure (caller treats as Tier 3 failure).
"""
from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from models.unit_record import AvailabilityStatus, UnitRecord
from templates._common import (
    attr_of,
    full_text,
    parse_availability,
    parse_availability_date,
    parse_floor,
    parse_floor_plan_type,
    parse_int_from_text,
    parse_rent,
    parse_sqft,
    regex_extract_from_text,
    text_of,
)

# Re-export legacy names for backward compatibility with entrata/appfolio
# imports that existed before the _common module was created.
_availability = parse_availability
_parse_int = parse_int_from_text
_parse_rent = parse_rent
_text_of = text_of

# ── Container selector cascade (most specific -> most generic) ────────────────

_PRIMARY_UNIT_SELECTORS = [
    ".unitContainer",
]

_FLOORPLAN_GROUP_SELECTORS = [
    ".floorplanContainer",
]

_SECONDARY_UNIT_SELECTORS = [
    ".pricingWrapper",
    "div[data-unit]",
    ".unit-row",
    ".unit-card",
    ".unitRow",
    ".unit-listing",
]

_GENERIC_CARD_SELECTORS = [
    "[class*='floorplan']",
    "[class*='FloorPlan']",
    ".fp-group",
    ".floorplan-item",
    ".floor-plan-card",
    ".apartment-item",
    ".plan-card",
    "[data-floor-plan]",
]


def _find_units_in_soup(soup: BeautifulSoup) -> list[Tag]:
    """Try selector cascades to find unit container elements."""
    # 1. Primary: direct unit containers
    units = soup.select(".unitContainer")
    if units:
        return units

    # 2. Floorplan-grouped view: .floorplanContainer > .pricingWrapper
    for fp_sel in _FLOORPLAN_GROUP_SELECTORS:
        fps = soup.select(fp_sel)
        if fps:
            grouped: list[Tag] = []
            for fp in fps:
                wrappers = fp.select(".pricingWrapper")
                if wrappers:
                    grouped.extend(wrappers)
                else:
                    # The floorplan container itself might be the unit card
                    grouped.append(fp)
            if grouped:
                return grouped

    # 3. Secondary selectors
    for sel in _SECONDARY_UNIT_SELECTORS:
        found = soup.select(sel)
        if found:
            return found

    # 4. Generic card selectors
    for sel in _GENERIC_CARD_SELECTORS:
        found = soup.select(sel)
        if len(found) >= 2:
            return found

    return []


def _extract_from_card(
    card: Tag,
    property_id: str,
    parent_fp_name: str | None = None,
) -> UnitRecord | None:
    """
    Extract a UnitRecord from a single card/row element.
    Uses CSS selectors first, falls back to regex on the card's full text.
    """
    # ── CSS-selector-based extraction ─────────────────────────────────────
    # Check the card element itself for data attributes
    card_unit_attr = card.get("data-unit") or card.get("data-unit-number")
    if isinstance(card_unit_attr, list):
        card_unit_attr = card_unit_attr[0] if card_unit_attr else None

    unit_number = (
        text_of(card, ".unitNumber")
        or text_of(card, ".unit-number")
        or text_of(card, ".unitNo")
        or (str(card_unit_attr) if card_unit_attr else None)
        or attr_of(card, "[data-unit]", "data-unit")
        or attr_of(card, "[data-unit-number]", "data-unit-number")
    )

    rent_text = (
        text_of(card, ".rent")
        or text_of(card, ".pricingWrapper .rent")
        or text_of(card, ".price")
        or text_of(card, ".unit-price")
        or text_of(card, "[class*='rent']")
        or text_of(card, "[class*='price']")
    )

    sqft_text = (
        text_of(card, ".sqft")
        or text_of(card, ".unit-sqft")
        or text_of(card, ".squareFeet")
        or text_of(card, "[class*='sqft']")
        or text_of(card, "[class*='sq-ft']")
    )

    avail_text = (
        text_of(card, ".availabilityDate")
        or text_of(card, ".availability")
        or text_of(card, ".unit-availability")
        or text_of(card, "[class*='avail']")
    )

    fp_text = (
        text_of(card, ".floorplanName")
        or text_of(card, ".unit-type")
        or text_of(card, ".floor-plan-name")
        or text_of(card, ".unit-floorplan")
        or text_of(card, ".floorplan")
        or text_of(card, "[class*='planName']")
        or text_of(card, "[class*='plan-name']")
    )

    floor_text = (
        text_of(card, ".floor")
        or text_of(card, ".unit-floor")
        or text_of(card, "[class*='floor']")
    )

    building_text = (
        text_of(card, ".building")
        or text_of(card, ".unit-building")
        or text_of(card, "[class*='building']")
    )

    # ── Regex fallback on card text for missing fields ────────────────────
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

    # Floor plan type: prefer explicit selector, then parent context, then regex
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
    """Parse RentCafe page HTML into UnitRecords. Returns [] on failure."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")

    # Detect floorplan-grouped layout to pass parent floor plan name
    records: list[UnitRecord] = []
    seen: set[str] = set()

    # Try floorplan-grouped view first (carries parent floorplan name to children)
    fps = soup.select(".floorplanContainer")
    if fps:
        for fp in fps:
            parent_name = text_of(fp, ".floorplanName") or text_of(fp, ".floorplan-name")
            children = fp.select(".unitContainer") or fp.select(".pricingWrapper")
            if children:
                for child in children:
                    rec = _extract_from_card(child, property_id, parent_fp_name=parent_name)
                    if rec and rec.unit_number not in seen:
                        seen.add(rec.unit_number)
                        records.append(rec)
            else:
                # Floorplan container without sub-units — skip (it's a header)
                pass
        if records:
            return records

    # Flat layout: find unit containers directly
    units = _find_units_in_soup(soup)
    if not units:
        return []

    for u in units:
        rec = _extract_from_card(u, property_id)
        if rec and rec.unit_number not in seen:
            seen.add(rec.unit_number)
            records.append(rec)

    return records
