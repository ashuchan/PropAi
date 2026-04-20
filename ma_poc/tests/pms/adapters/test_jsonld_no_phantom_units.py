"""Tests for F5 — JSON-LD reclassification (no phantom units)."""
from __future__ import annotations

import json

from ma_poc.pms.adapters._html_extract import parse_jsonld


def _jsonld_html(data: dict) -> str:
    return f'<script type="application/ld+json">{json.dumps(data)}</script>'


def test_jsonld_single_apartment_schema_returns_empty_units() -> None:
    """Single Apartment schema object = property metadata only."""
    html = _jsonld_html({
        "@type": "Apartment",
        "name": "Northside Place",
        "numberOfRooms": 14,
        "address": {"@type": "PostalAddress", "addressLocality": "Chicago"},
    })
    meta, units = parse_jsonld(html)
    assert units == []


def test_jsonld_itemlist_with_two_distinct_units_returns_units() -> None:
    """ItemList with 2 units that differ in bedrooms and rent -> emit units."""
    html = _jsonld_html({
        "@type": "ItemList",
        "itemListElement": [
            {"@type": "Apartment", "name": "Studio A", "numberOfRooms": 0,
             "offers": {"@type": "Offer", "price": 1200}},
            {"@type": "Apartment", "name": "1BR B", "numberOfRooms": 1,
             "offers": {"@type": "Offer", "price": 1600}},
        ],
    })
    meta, units = parse_jsonld(html)
    assert len(units) >= 2


def test_jsonld_itemlist_with_two_identical_units_returns_empty() -> None:
    """ItemList where all items are identical (same name, same price) = empty."""
    html = _jsonld_html({
        "@type": "ItemList",
        "itemListElement": [
            {"@type": "Apartment", "name": "Unit", "numberOfRooms": 1,
             "offers": {"@type": "Offer", "price": 1200}},
            {"@type": "Apartment", "name": "Unit", "numberOfRooms": 1,
             "offers": {"@type": "Offer", "price": 1200}},
        ],
    })
    meta, units = parse_jsonld(html)
    assert units == []


def test_jsonld_property_metadata_extracted_even_when_no_units() -> None:
    """Property-level metadata is always returned even when units list is empty."""
    html = _jsonld_html({
        "@type": "ApartmentComplex",
        "name": "Test Complex",
        "url": "https://test.com",
    })
    meta, units = parse_jsonld(html)
    assert units == []
    assert meta.get("name") == "Test Complex"


def test_jsonld_apartment_array_with_varying_rent_returns_units() -> None:
    """Array of ApartmentUnit objects with distinct rents produces units."""
    html = _jsonld_html({
        "@graph": [
            {"@type": "ApartmentUnit", "name": "1A",
             "numberOfRooms": 1, "offers": {"@type": "Offer", "price": 1100}},
            {"@type": "ApartmentUnit", "name": "2B",
             "numberOfRooms": 2, "offers": {"@type": "Offer", "price": 1500}},
        ],
    })
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
