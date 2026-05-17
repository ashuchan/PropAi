"""Tests for F5 — JSON-LD reclassification (no phantom units)."""

from __future__ import annotations

import json

from ma_poc.pms.adapters._html_extract import parse_jsonld


def _jsonld_html(data: dict) -> str:
    return f'<script type="application/ld+json">{json.dumps(data)}</script>'


def test_jsonld_single_apartment_schema_returns_empty_units() -> None:
    """Single Apartment schema object = property metadata only."""
    html = _jsonld_html(
        {
            "@type": "Apartment",
            "name": "Northside Place",
            "numberOfRooms": 14,
            "address": {"@type": "PostalAddress", "addressLocality": "Chicago"},
        }
    )
    meta, units = parse_jsonld(html)
    assert units == []


def test_jsonld_itemlist_with_two_distinct_units_returns_units() -> None:
    """ItemList with 2 units that differ in bedrooms and rent -> emit units."""
    html = _jsonld_html(
        {
            "@type": "ItemList",
            "itemListElement": [
                {
                    "@type": "Apartment",
                    "name": "Studio A",
                    "numberOfRooms": 0,
                    "offers": {"@type": "Offer", "price": 1200},
                },
                {
                    "@type": "Apartment",
                    "name": "1BR B",
                    "numberOfRooms": 1,
                    "offers": {"@type": "Offer", "price": 1600},
                },
            ],
        }
    )
    meta, units = parse_jsonld(html)
    assert len(units) >= 2


def test_jsonld_itemlist_with_two_identical_units_returns_empty() -> None:
    """ItemList where all items are identical (same name, same price) = empty."""
    html = _jsonld_html(
        {
            "@type": "ItemList",
            "itemListElement": [
                {
                    "@type": "Apartment",
                    "name": "Unit",
                    "numberOfRooms": 1,
                    "offers": {"@type": "Offer", "price": 1200},
                },
                {
                    "@type": "Apartment",
                    "name": "Unit",
                    "numberOfRooms": 1,
                    "offers": {"@type": "Offer", "price": 1200},
                },
            ],
        }
    )
    meta, units = parse_jsonld(html)
    assert units == []


def test_jsonld_property_metadata_extracted_even_when_no_units() -> None:
    """Property-level metadata is always returned even when units list is empty."""
    html = _jsonld_html(
        {
            "@type": "ApartmentComplex",
            "name": "Test Complex",
            "url": "https://test.com",
        }
    )
    meta, units = parse_jsonld(html)
    assert units == []
    assert meta.get("name") == "Test Complex"


def test_jsonld_apartment_array_with_varying_rent_returns_units() -> None:
    """Array of ApartmentUnit objects with distinct rents produces units."""
    html = _jsonld_html(
        {
            "@graph": [
                {
                    "@type": "ApartmentUnit",
                    "name": "1A",
                    "numberOfRooms": 1,
                    "offers": {"@type": "Offer", "price": 1100},
                },
                {
                    "@type": "ApartmentUnit",
                    "name": "2B",
                    "numberOfRooms": 2,
                    "offers": {"@type": "Offer", "price": 1500},
                },
            ],
        }
    )
    meta, units = parse_jsonld(html)
    assert len(units) >= 2


def test_jsonld_parse_returns_tuple_not_list() -> None:
    """parse_jsonld must return a (dict, list) tuple, not a plain list."""
    html = _jsonld_html({"@type": "Apartment", "name": "Test"})
    result = parse_jsonld(html)
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert isinstance(result[0], dict)
    assert isinstance(result[1], list)


# ── C8 emit-gate tightening (2026-05-13 teammate analysis) ───────────────────


def test_jsonld_name_only_apartment_no_dimensions_does_not_emit() -> None:
    """C8.1 regression: An Apartment node with name but no price/size/bedroom
    data must NOT be emitted as a "1 unit" result. The planner uses the
    pre-post_process unit count to decide whether to escalate to LLM rescue;
    a fake unit triggers wasted LLM tokens. Teammate-cited example:
    elevatetosequoia.com -> 1 fake unit -> ESCALATE_LINK_HOP -> LLM rescue empty.
    """
    html = _jsonld_html(
        {
            "@type": "Apartment",
            "name": "Pet Friendly",  # actually an amenity, mis-typed as Apartment
        }
    )
    meta, units = parse_jsonld(html)
    assert units == []


def test_jsonld_itemlist_with_units_having_dimensions_still_emits() -> None:
    """Counter-regression: ItemList with ApartmentUnits that DO carry
    rent/size/bedroom data continues to emit normally — the C8 gate only
    blocks name-only nodes that have no quantitative dimension."""
    html = _jsonld_html(
        {
            "@type": "ItemList",
            "itemListElement": [
                {
                    "@type": "ApartmentUnit",
                    "name": "Plan A1",
                    "numberOfRooms": 1,
                    "offers": {"@type": "Offer", "price": 1850},
                },
                {
                    "@type": "ApartmentUnit",
                    "name": "Plan B2",
                    "numberOfRooms": 2,
                    "floorSize": {"@type": "QuantitativeValue", "value": 950},
                },
            ],
        }
    )
    meta, units = parse_jsonld(html)
    assert len(units) == 2


def test_jsonld_itemlist_dimensionless_apartment_filtered_others_emit() -> None:
    """C8: within an ItemList, an ApartmentUnit with only a name and no
    rent/size/bedrooms must NOT be emitted. Valid sibling units continue
    to emit. (Note: F5 reclassification requires >=2 distinct units to
    pass through, so the test has 2 valid + 1 dimensionless.)"""
    html = _jsonld_html(
        {
            "@type": "ItemList",
            "itemListElement": [
                {
                    "@type": "ApartmentUnit",
                    "name": "Pet Friendly",  # mis-typed amenity, no dimensions
                },
                {
                    "@type": "ApartmentUnit",
                    "name": "Plan A1",
                    "numberOfRooms": 1,
                    "offers": {"@type": "Offer", "price": 1200},
                },
                {
                    "@type": "ApartmentUnit",
                    "name": "Plan B2",
                    "numberOfRooms": 2,
                    "offers": {"@type": "Offer", "price": 1500},
                },
            ],
        }
    )
    meta, units = parse_jsonld(html)
    # The dimensionless "Pet Friendly" must NOT be in the output.
    names = {u.get("floor_plan_name") for u in units}
    assert "Pet Friendly" not in names
    # The 2 valid items must still emit.
    assert {"Plan A1", "Plan B2"}.issubset(names)
