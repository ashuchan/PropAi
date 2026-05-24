"""UDR Schema.org JSON-LD ItemList adapter tests.

Audit xlsx 2026-05-23 row #41: Cambridge Woods unit "13664212" — the
generic DOM tier was shipping the URL-param ``unitid`` (UDR's internal
8-digit id) instead of parsing the displayed unit number out of the
Schema.org Apartment ``name`` field ("Apartment #8 - 4020" → "4020").

Live-verified on 2026-05-24 against
udr.com/tampa-apartments/university-center/cambridge-woods/floor-plans
— 13 units across 7 unique unitids — every one's displayed name
follows ``Apartment #<seq> - <unit_number>``.
"""
from __future__ import annotations

import pytest

from ma_poc.pms.adapters._udr import (
    _extract_unit_from_udr_name,
    parse_udr_jsonld,
)

# ─────────────────────────────────────────────────────────────────────
# 1) Name parser — verbatim shapes from the live Cambridge Woods probe
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw, expected", [
    # Live shapes captured 2026-05-24
    ("Apartment #8 - 4020", "4020"),
    ("Apartment #27B - 202", "202"),
    ("Apartment #43A - 101", "101"),
    ("Apartment #3 - 102", "102"),
    ("Apartment #11 - 14218", "14218"),
    ("Apartment #6 - 14245", "14245"),
    ("Apartment #1 - 14256", "14256"),
    ("Apartment #36 - 14186", "14186"),
    ("Apartment #10 - 14240", "14240"),
    ("Apartment #7 - 4010", "4010"),
    # Edge: en-dash / em-dash variants the regex should tolerate
    ("Apartment #5 – 305", "305"),
    ("Apartment #5 — 306", "306"),
    # Edge: extra whitespace
    ("  Apartment  #12  -  500  ", "500"),
    # No expected shape — fall back to raw name
    ("Penthouse Suite", "Penthouse Suite"),
    ("4020", "4020"),
    # Empty
    ("", ""),
])
def test_extract_unit_from_udr_name(raw: str, expected: str) -> None:
    assert _extract_unit_from_udr_name(raw) == expected


# ─────────────────────────────────────────────────────────────────────
# 2) Full JSON-LD parser — the audit's signature case
# ─────────────────────────────────────────────────────────────────────


_CAMBRIDGE_WOODS_FRAGMENT = """
<!DOCTYPE html>
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "ItemList",
  "name": "Apartments and Pricing for Cambridge Woods | Tampa",
  "itemListElement": [
    {
      "@type": "ListItem",
      "position": 1,
      "item": {
        "@type": ["Apartment", "Product"],
        "name": "Apartment #8 - 4020",
        "description": "2 Beds | 1.5 Baths | 1309 Sq. Ft",
        "image": "/globalassets/communities/cambridge-woods/floor-plans/cambridgewoods_b15t_combined_3d.gif",
        "url": "https://www.udr.com/tampa-apartments/university-center/cambridge-woods/apartments-pricing/?unitid=13664212",
        "offers": {
          "@type": "Offer",
          "url": "https://www.udr.com/tampa-apartments/university-center/cambridge-woods/apartments-pricing/?unitid=13664212",
          "availability": "https://schema.org/InStock",
          "price": 1833,
          "priceCurrency": "USD"
        },
        "floorSize": {"@type": "QuantitativeValue", "unitCode": "FTK", "value": 1309},
        "numberOfBathroomsTotal": 1,
        "numberOfBedrooms": 2
      }
    },
    {
      "@type": "ListItem",
      "position": 2,
      "item": {
        "@type": ["Apartment", "Product"],
        "name": "Apartment #11 - 14218",
        "description": "1 Bed | 1 Bath | 750 Sq. Ft",
        "image": "/globalassets/communities/cambridge-woods/floor-plans/cambridgewoods_a1d_3d.gif",
        "url": "https://www.udr.com/tampa-apartments/university-center/cambridge-woods/apartments-pricing/?unitid=3913462",
        "offers": {
          "@type": "Offer",
          "url": "https://www.udr.com/tampa-apartments/university-center/cambridge-woods/apartments-pricing/?unitid=3913462",
          "availability": "https://schema.org/InStock",
          "price": 1295,
          "priceCurrency": "USD"
        },
        "floorSize": {"@type": "QuantitativeValue", "value": 750},
        "numberOfBathroomsTotal": 1,
        "numberOfBedrooms": 1
      }
    }
  ]
}
</script>
</head><body></body></html>
"""


def test_parse_udr_jsonld_uses_displayed_name_not_internal_unitid() -> None:
    """The audit signature: unit '13664212' (internal) → '4020' (displayed)."""
    units = parse_udr_jsonld(_CAMBRIDGE_WOODS_FRAGMENT, source_url="https://www.udr.com/tampa-apartments/university-center/cambridge-woods/floor-plans")
    assert len(units) == 2
    u1 = units[0]
    assert u1["unit_number"] == "4020", (
        f"audit signature: unit_number should be '4020' (from 'Apartment #8 - 4020'); "
        f"got {u1['unit_number']!r} — the URL-param unitid (13664212) is back."
    )
    assert u1["bedrooms"] == "2"
    assert u1["bathrooms"] == "1"
    assert u1["sqft"] == "1309"
    assert "$1,833" in u1["rent_range"]
    assert u1["availability_status"] == "AVAILABLE"
    assert u1["extraction_tier"] == "TIER_1_JSONLD_UDR"
    # Internal unitid preserved in source_ids for cross-reference
    assert u1.get("appfolio_listing_id") is None  # confirm we didn't mis-key
    # Just verify the internal id is somewhere in the row
    assert "13664212" in str(u1), "internal unitid should be preserved for provenance"

    u2 = units[1]
    assert u2["unit_number"] == "14218"
    assert u2["bedrooms"] == "1"
    assert u2["sqft"] == "750"


def test_parse_udr_jsonld_extracts_floor_plan_from_image_filename() -> None:
    """UDR doesn't ship a clean plan name in JSON-LD; derive from the
    image URL: cambridgewoods_b15t_combined_3d.gif → 'B15T'."""
    units = parse_udr_jsonld(_CAMBRIDGE_WOODS_FRAGMENT, source_url="x")
    assert units[0]["floor_plan_name"] == "B15T"
    assert units[1]["floor_plan_name"] == "A1D"


def test_parse_udr_jsonld_returns_empty_when_no_itemlist() -> None:
    """Pages without ItemList JSON-LD (homepage, contact page, etc.)
    yield no units so the caller can chain to the next tier."""
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"WebSite","name":"UDR"}'
        '</script></head></html>'
    )
    assert parse_udr_jsonld(html, source_url="x") == []


def test_parse_udr_jsonld_returns_empty_when_no_jsonld_blocks() -> None:
    """No <script application/ld+json> tags at all → empty."""
    assert parse_udr_jsonld("<html><body>Hello</body></html>", source_url="x") == []
    assert parse_udr_jsonld("", source_url="x") == []


def test_parse_udr_jsonld_skips_malformed_json_blocks() -> None:
    """Malformed JSON-LD blocks shouldn't crash the parser — silently
    skip and move to the next block."""
    html = """
    <script type="application/ld+json">
    {not json
    </script>
    """ + _CAMBRIDGE_WOODS_FRAGMENT
    units = parse_udr_jsonld(html, source_url="x")
    assert len(units) == 2
    assert units[0]["unit_number"] == "4020"


def test_parse_udr_jsonld_dedupes_by_internal_unitid() -> None:
    """A unit may appear twice (e.g. duplicate ItemList) — dedup on
    the URL-param unitid so we don't double-count."""
    # Wrap the same unit twice in a second ItemList
    html = _CAMBRIDGE_WOODS_FRAGMENT + """
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "ItemList",
      "itemListElement": [
        {
          "@type": "ListItem",
          "position": 1,
          "item": {
            "@type": "Apartment",
            "name": "Apartment #99 - 4020",
            "url": "https://www.udr.com/x/?unitid=13664212",
            "offers": {"@type":"Offer","price":2000,"availability":"https://schema.org/InStock"},
            "numberOfBedrooms": 2,
            "numberOfBathroomsTotal": 1
          }
        }
      ]
    }
    </script>
    """
    units = parse_udr_jsonld(html, source_url="x")
    # Dedup applied — only one unit for unitid 13664212 even though
    # it appears in two ItemLists
    assert len([u for u in units if "13664212" in str(u)]) == 1


def test_parse_udr_jsonld_marks_outofstock_as_unavailable() -> None:
    """Schema.org InStock → AVAILABLE; anything else (OutOfStock,
    PreOrder, SoldOut) → UNAVAILABLE."""
    html = """
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "ItemList",
      "itemListElement": [{
        "@type": "ListItem",
        "item": {
          "@type": "Apartment",
          "name": "Apartment #1 - 100",
          "url": "?unitid=1",
          "offers": {
            "@type": "Offer",
            "price": 1500,
            "availability": "https://schema.org/OutOfStock"
          },
          "numberOfBedrooms": 1,
          "numberOfBathroomsTotal": 1
        }
      }]
    }
    </script>
    """
    units = parse_udr_jsonld(html, source_url="x")
    assert len(units) == 1
    assert units[0]["availability_status"] == "UNAVAILABLE"


def test_parse_udr_jsonld_handles_single_string_apartment_type() -> None:
    """UDR sometimes ships @type as a bare string instead of a list."""
    html = """
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "ItemList",
      "itemListElement": [{
        "@type": "ListItem",
        "item": {
          "@type": "Apartment",
          "name": "Apartment #5 - 305",
          "url": "?unitid=999",
          "offers": {"@type":"Offer","price":1500,"availability":"https://schema.org/InStock"},
          "numberOfBedrooms": 1,
          "numberOfBathroomsTotal": 1
        }
      }]
    }
    </script>
    """
    units = parse_udr_jsonld(html, source_url="x")
    assert len(units) == 1
    assert units[0]["unit_number"] == "305"


def test_parse_udr_jsonld_skips_items_that_arent_apartments() -> None:
    """Schema.org ItemLists can carry mixed types; ignore non-Apartment
    items (e.g. BreadcrumbList items, Article items)."""
    html = """
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "ItemList",
      "itemListElement": [
        {
          "@type": "ListItem",
          "item": {"@type": "WebPage", "name": "Apartment #99 - 9999"}
        },
        {
          "@type": "ListItem",
          "item": {
            "@type": "Apartment",
            "name": "Apartment #1 - 100",
            "url": "?unitid=1",
            "offers": {"@type":"Offer","price":1500,"availability":"https://schema.org/InStock"},
            "numberOfBedrooms": 1,
            "numberOfBathroomsTotal": 1
          }
        }
      ]
    }
    </script>
    """
    units = parse_udr_jsonld(html, source_url="x")
    assert len(units) == 1
    assert units[0]["unit_number"] == "100"


# ─────────────────────────────────────────────────────────────────────
# 3) Source-level wiring check — generic.py invokes _parse_udr
# ─────────────────────────────────────────────────────────────────────


def test_generic_invokes_udr_parser_for_udr_dot_com() -> None:
    """Pin the wiring: generic.py must import _parse_udr AND call it
    when the base_url contains udr.com. A future refactor that drops
    this gate fails the test loudly."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[3] / "pms" / "adapters" / "generic.py").read_text(encoding="utf-8")
    assert "_parse_udr" in src, (
        "generic.py no longer imports _parse_udr — UDR audit fix is gone."
    )
    assert "udr.com" in src.lower(), (
        "generic.py no longer gates the UDR parser by domain."
    )
