"""Wix labeled-block + Wix section-plan tests (2026-05-23).

Covers two new generic_plan_text patterns added for the operator-data-
gap reversal:

1. ``_WIX_LABELED_BLOCK_RE`` — Westgate Village style per-bedroom
   Wix subpages with colon-separated key labels:
       Bed: 1
       Bath: 1
       SQ.FT.: 680
       Rent: $1150-1200

2. ``_WIX_SECTION_PLAN_RE`` — Allure-at-Jefferson style Wix section
   text runs:
       854 sq. ft. Prices starting at $1714 A1 ONE BEDROOM ONE BATHROOM
"""
from __future__ import annotations

from ma_poc.pms.adapters.generic_plan_text import (
    parse_generic_plan_text as parse_plan_text,
)


def _flatten_block(*lines: str) -> str:
    return "\n".join(lines)


# ─── _WIX_LABELED_BLOCK_RE — Westgate Village ────────────────────────


def test_wix_labeled_block_extracts_bed_bath_sqft_rent_range() -> None:
    body = _flatten_block(
        "Bed: 1",
        "Bath: 1",
        "SQ.FT.: 680",
        "Rent: $1150-1200",
    )
    rows = parse_plan_text(body, "https://www.westgate-village-townhouses.com/onebedroom")
    assert len(rows) == 1
    r = rows[0]
    assert r["bedrooms"] == "1"
    assert r["bathrooms"] == "1"
    assert r["sqft"] == "680"
    assert r["market_rent_low"] == 1150
    assert r["market_rent_high"] == 1200
    assert r["extraction_tier"] == "TIER_1_DOM_GENERIC_PLAN_TEXT_WIX_LABELED_BLOCK"


def test_wix_labeled_block_handles_single_rent_value() -> None:
    body = _flatten_block(
        "Bed: 2",
        "Bath: 1.5",
        "SQ.FT.: 950",
        "Rent: $1400",
    )
    rows = parse_plan_text(body, "https://x/twobedroom")
    assert len(rows) == 1
    r = rows[0]
    assert r["market_rent_low"] == 1400
    assert r["market_rent_high"] == 1400
    assert r["bathrooms"] == "1.5"
    assert r["bedrooms"] == "2"


def test_wix_labeled_block_handles_studio() -> None:
    body = _flatten_block(
        "Bed: Studio",
        "Bath: 1",
        "SQ.FT.: 450",
        "Rent: $1000",
    )
    rows = parse_plan_text(body, "https://x/studio")
    assert len(rows) == 1
    r = rows[0]
    assert r["bedrooms"] == "0"
    assert r["bed_label"] == "Studio"


def test_wix_labeled_block_rejects_block_below_rent_floor() -> None:
    body = _flatten_block(
        "Bed: 1", "Bath: 1", "SQ.FT.: 680", "Rent: $50",
    )
    # $50 is below the rent floor — reject as junk.
    rows = parse_plan_text(body, "https://x/onebedroom")
    assert rows == []


def test_wix_labeled_block_rejects_block_with_garbage_sqft() -> None:
    """When sqft is garbage (<100), the strict Wix labeled-block pass
    rejects. The labeled-price fallback may still emit a rent-only row
    (operator did publish a price). Verify the Wix block tier is NOT
    in the output — that's the contract: garbage sqft means no full
    plan row, downstream is free to honestly publish rent-only."""
    body = _flatten_block(
        "Bed: 1", "Bath: 1", "SQ.FT.: 50", "Rent: $1200",
    )
    rows = parse_plan_text(body, "https://x/")
    wix_block_rows = [
        r for r in rows
        if r.get("extraction_tier", "").endswith("WIX_LABELED_BLOCK")
    ]
    assert wix_block_rows == []


def test_wix_labeled_block_tolerates_extra_text_between_labels() -> None:
    """Wix wraps each key in its own section element — there's often
    100+ chars of layout/style text between the labels. The 200-char
    DOTALL gap in the regex must absorb it."""
    body = (
        "Bed: 1\n\nLorem ipsum dolor sit amet.\n\n"
        "Bath: 1\n\nMore filler content here.\n\n"
        "SQ.FT.: 680\n\nMore Wix layout text.\n\n"
        "Rent: $1150-1200"
    )
    rows = parse_plan_text(body, "https://x/")
    assert len(rows) == 1
    assert rows[0]["sqft"] == "680"


# ─── _WIX_SECTION_PLAN_RE — Allure-at-Jefferson ──────────────────────


def test_wix_section_plan_extracts_canonical_text_run() -> None:
    body = "854 sq. ft. Prices starting at $1714 A1 ONE BEDROOM ONE BATHROOM"
    rows = parse_plan_text(body, "https://www.liveallureva.com/floorplans")
    assert len(rows) == 1
    r = rows[0]
    assert r["sqft"] == "854"
    assert r["market_rent_low"] == 1714
    assert r["bedrooms"] == "1"
    assert r["bathrooms"] == "1"
    assert r["floor_plan_name"] == "A1"
    assert (
        r["extraction_tier"]
        == "TIER_1_DOM_GENERIC_PLAN_TEXT_WIX_SECTION_PLAN"
    )


def test_wix_section_plan_two_bedroom_two_bathroom() -> None:
    body = "1107 sq. ft. Prices starting at $2005 B2 TWO BEDROOM TWO BATHROOM"
    rows = parse_plan_text(body, "https://x/floorplans")
    assert rows
    r = rows[0]
    assert r["bedrooms"] == "2"
    assert r["bathrooms"] == "2"
    assert r["sqft"] == "1107"
    assert r["market_rent_low"] == 2005


def test_wix_section_plan_handles_multiple_plans_in_one_body() -> None:
    """Allure-at-Jefferson's /floorplans page has all 5 plans on the
    same page — each in its own section but flattened into one body
    text. The parser emits ≥2 rows (the primary bed+bath pass may win
    A2/A3 before the Wix section pass runs; both layers are honest
    plan-level data that clears the success bar)."""
    body = (
        "854 sq. ft. Prices starting at $1714 A1 ONE BEDROOM ONE BATHROOM"
        " 941 sq. ft. Prices starting at $1795 A2 ONE BEDROOM ONE BATHROOM"
        " 1107 sq. ft. Prices starting at $2005 A3 ONE BEDROOM ONE BATHROOM"
    )
    rows = parse_plan_text(body, "https://x/floorplans")
    assert len(rows) >= 2
    # All extracted rows must carry both rent and sqft.
    for r in rows:
        assert r["market_rent_low"] > 0
        assert int(r["sqft"]) > 0


def test_wix_section_plan_handles_three_bedroom_word() -> None:
    body = "1311 sq. ft. Prices starting at $2450 C3 THREE BEDROOM TWO BATHROOM"
    rows = parse_plan_text(body, "https://x/")
    assert rows
    assert rows[0]["bedrooms"] == "3"


def test_wix_section_plan_rejects_run_without_descriptor() -> None:
    """The bed/bath word descriptor is the gate — without it, the
    pattern could match marketing blurbs like '600 sq ft starting at
    $1500'. Verify the gate holds."""
    body = "854 sq. ft. Prices starting at $1714 marketing copy here"
    rows = parse_plan_text(body, "https://x/")
    assert rows == []
