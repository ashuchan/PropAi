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
    _format_udr_plan_code,
    _udr_view_model_dates,
    canonical_udr_url_from_html,
    is_udr_url,
    parse_udr_jsonld,
    udr_pricing_urls,
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
    image URL: cambridgewoods_b15t_combined_3d.gif → 'B1.5T' (decimal
    restored from the filename — user QQ 2026-05-24)."""
    units = parse_udr_jsonld(_CAMBRIDGE_WOODS_FRAGMENT, source_url="x")
    assert units[0]["floor_plan_name"] == "B1.5T"
    assert units[1]["floor_plan_name"] == "A1D"


def test_parse_udr_jsonld_preserves_repeated_numeric_building_prefixes() -> None:
    """Vitruvian West: repeated numeric prefixes are physical buildings."""
    html = """
    <script type="application/ld+json">
    {
      "@type": "ItemList",
      "itemListElement": [
        {"@type":"ListItem","item":{"@type":"Apartment",
         "name":"Apartment #1 - 124","url":"?unitid=11",
         "offers":{"price":1306,"availability":"https://schema.org/InStock"}}},
        {"@type":"ListItem","item":{"@type":"Apartment",
         "name":"Apartment #1 - 128","url":"?unitid=12",
         "offers":{"price":1381,"availability":"https://schema.org/InStock"}}}
      ]
    }
    </script>
    """
    units = parse_udr_jsonld(html, source_url="x")
    assert [unit["unit_number"] for unit in units] == ["1-124", "1-128"]
    assert [unit["building"] for unit in units] == ["1", "1"]


def test_parse_udr_jsonld_preserves_alphanumeric_building_prefix() -> None:
    """Arbor Park: the alphanumeric prefix is part of the public unit ID."""
    html = """
    <script type="application/ld+json">
    {"@type":"ItemList","itemListElement":[
      {"@type":"ListItem","item":{"@type":"Apartment",
       "name":"Apartment #03R - 0202","url":"?unitid=13453695",
       "offers":{"price":1765,"availability":"https://schema.org/InStock"}}}
    ]}
    </script>
    """
    units = parse_udr_jsonld(html, source_url="x")
    assert units[0]["unit_number"] == "03R-0202"
    assert units[0]["building"] == "03R"
def test_parse_udr_jsonld_joins_first_party_view_model_date_by_unit() -> None:
    """UDR JSON-LD omits dates, while the adjacent first-party view model
    publishes the exact visible date keyed by marketing unit number."""
    view_model = """
    <script>
    window.udr.jsonObjPropertyViewModel = {
      "floorPlans": [{"units": [
        {
          "marketingName": "4020",
          "AvailableDateLabel": "9/25/2026",
          "rentsMatrix": [{"MoveInDate": "2026-09-26"}]
        },
        {
          "lookUpName": "14218",
          "AvailableDateLabel": "",
          "rentsMatrix": [{"MoveInDate": "2026-08-20"}]
        }
      ]}]
    };
    </script>
    """

    units = parse_udr_jsonld(_CAMBRIDGE_WOODS_FRAGMENT + view_model, source_url="x")

    assert units[0]["availability_date"] == "9/25/2026"
    assert units[0]["available_date"] == "9/25/2026"
    assert units[1]["availability_date"] == "2026-08-20"


def test_udr_date_join_prefers_native_id_when_labels_repeat_across_buildings() -> None:
    html = """
    <script type="application/ld+json">
    {"@type":"ItemList","itemListElement":[
      {"@type":"ListItem","item":{"@type":"Apartment",
       "name":"Apartment #7 - 105","url":"?unitid=13670653",
       "offers":{"price":2100,"availability":"https://schema.org/InStock"}}},
      {"@type":"ListItem","item":{"@type":"Apartment",
       "name":"Apartment #19 - 105","url":"?unitid=13679999",
       "offers":{"price":2200,"availability":"https://schema.org/InStock"}}},
      {"@type":"ListItem","item":{"@type":"Apartment",
       "name":"Apartment #7 - 106","url":"?unitid=13670654",
       "offers":{"price":2150,"availability":"https://schema.org/InStock"}}}
    ]}
    </script>
    <script>
    window.udr.jsonObjPropertyViewModel = {"floorPlans":[{"units":[
      {"marketingName":"105","apartmentId":13670653,
       "AvailableDateLabel":"9/19/2026"},
      {"marketingName":"105","realpageunitid":"13679999",
       "AvailableDateLabel":"10/2/2026"},
      {"marketingName":"106","apartmentId":13670654,
       "AvailableDateLabel":"9/25/2026"}
    ]}]};
    </script>
    """

    units = parse_udr_jsonld(html, source_url="https://www.udr.com/example")
    by_id = {row["source_ids"]["udr_unitid"]: row for row in units}

    assert by_id["13670653"]["unit_number"] == "7-105"
    assert by_id["13670653"]["availability_date"] == "9/19/2026"
    assert by_id["13679999"]["unit_number"] == "19-105"
    assert by_id["13679999"]["availability_date"] == "10/2/2026"
    # The repeated public label is deliberately not accepted as a fallback.
    assert "label:105" not in _udr_view_model_dates(html)


def test_udr_view_model_date_parse_is_non_fatal() -> None:
    assert _udr_view_model_dates("<script>no marker</script>") == {}
    assert _udr_view_model_dates(
        "window.udr.jsonObjPropertyViewModel = {not json};"
    ) == {}


@pytest.mark.parametrize("raw, expected", [
    # Live shapes from Cambridge Woods 2026-05-24
    ("a1a", "A1A"),
    ("a1b", "A1B"),
    ("a1c", "A1C"),
    ("a1d", "A1D"),
    ("a1e", "A1E"),
    ("b15t", "B1.5T"),       # decimal restored
    ("b25at", "B2.5AT"),     # decimal restored, trailing letters preserved
    ("b25bt", "B2.5BT"),
    ("b25ct", "B2.5CT"),
    ("b25dt", "B2.5DT"),
    # Edge cases
    ("", ""),
    ("c25", "C2.5"),         # trailing digits at end
    ("simple", "SIMPLE"),    # no digits at all → uppercase only
    ("a1", "A1"),            # only one digit
])
def test_format_udr_plan_code_inserts_decimal(raw: str, expected: str) -> None:
    """The decimal-insert rule: '<letters><digit><digit><letters?>' →
    '<letters><digit>.<digit><letters?>'. Verified live on all 13
    Cambridge Woods plans."""
    assert _format_udr_plan_code(raw) == expected


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


def test_udr_vanity_homepage_accepts_only_official_canonical() -> None:
    html = """
    <link rel="stylesheet" href="https://www.udr.com/not-canonical/">
    <link href="https://www.udr.com/washington-dc-apartments/u-street-corridor/view-14/?tracking=1#hero"
          rel="alternate canonical">
    """
    assert canonical_udr_url_from_html(html) == (
        "https://www.udr.com/"
        "washington-dc-apartments/u-street-corridor/view-14/"
    )


@pytest.mark.parametrize(
    "href",
    [
        "https://www.udr.com.evil.test/community/",
        "https://udr.com@evil.test/community/",
        "http://www.udr.com/community/",
        "//www.udr.com/community/",
        "https://www.udr.com:8443/community/",
    ],
)
def test_udr_vanity_homepage_rejects_unsafe_canonical(href: str) -> None:
    html = f'<link rel="canonical" href="{href}">'
    assert canonical_udr_url_from_html(html) == ""
    assert not is_udr_url(href)


def test_udr_pricing_urls_cover_root_and_contact_leaf() -> None:
    root = (
        "https://www.udr.com/tampa-apartments/"
        "university-center/cambridge-woods"
    )
    assert udr_pricing_urls(root + "/contact-us/") == [
        root + "/contact-us/apartments-pricing/",
        root + "/apartments-pricing/",
    ]
    assert udr_pricing_urls(root + "/apartments-pricing/") == []
    assert udr_pricing_urls("https://notudr.com/community/") == []


@pytest.mark.asyncio
async def test_generic_plan_text_recovers_udr_vanity_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PID 36925: view14.com must hop only to its official UDR canonical."""
    from types import SimpleNamespace

    from ma_poc.pms.adapters import _probe
    from ma_poc.pms.adapters.base import AdapterContext
    from ma_poc.pms.adapters.generic_plan_text import GenericPlanTextAdapter
    from ma_poc.pms.detector import detect_pms

    homepage = """
    <html><head>
      <link rel="canonical"
            href="https://www.udr.com/washington-dc-apartments/u-street-corridor/view-14/">
    </head><body>View 14 apartments</body></html>
    """
    pricing_url = (
        "https://www.udr.com/washington-dc-apartments/"
        "u-street-corridor/view-14/apartments-pricing/"
    )
    probed: list[str] = []

    def fake_probe_get(url: str, timeout: int = 15) -> SimpleNamespace:
        probed.append(url)
        return SimpleNamespace(
            status_code=200,
            text=_CAMBRIDGE_WOODS_FRAGMENT,
        )

    monkeypatch.setattr(_probe, "probe_get", fake_probe_get)
    ctx = AdapterContext(
        base_url="https://www.view14.com/",
        detected=detect_pms("https://www.view14.com/", homepage),
        profile=None,
        expected_total_units=None,
        property_id="36925",
        fetch_result=SimpleNamespace(body=homepage.encode()),
    )

    result = await GenericPlanTextAdapter().extract(object(), ctx)  # type: ignore[arg-type]

    assert probed == [pricing_url]
    assert result.tier_used == "TIER_1_JSONLD_UDR"
    assert result.winning_url == pricing_url
    assert [row["unit_number"] for row in result.units] == ["4020", "14218"]


# ─────────────────────────────────────────────────────────────────────
# 3) Source-level wiring check — generic.py invokes _parse_udr
# ─────────────────────────────────────────────────────────────────────


def test_generic_invokes_strict_udr_parser_gate() -> None:
    """Pin the parser plus exact-host/canonical vanity wiring."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[3] / "pms" / "adapters" / "generic.py").read_text(encoding="utf-8")
    assert "_parse_udr" in src, (
        "generic.py no longer imports _parse_udr — UDR audit fix is gone."
    )
    assert "_is_udr_url" in src, (
        "generic.py no longer gates UDR extraction on the exact official host."
    )
    assert "_canonical_udr_url" in src, (
        "generic.py no longer recognizes official canonical links on vanity sites."
    )
