"""MAA HTML-direct units extraction tests (2026-05-23).

Background — the routing-bypass fix:
  26 of 31 MAA properties in the canary stamped TIER_1_5_EMBEDDED with
  SUCCESS_PLAN_LEVEL despite a working MAACAdapter. The active
  ``/api/properties/{ULID}/units/available/`` fetch silently failed in
  production (the canary report shows GenericAdapter winning via
  embedded_json, AFTER MAACAdapter would have returned empty). Generic's
  parser doesn't know MAA field names (minimumRent/maximumRent), so
  units extract with sqft + unit_id but rent stays null.

  Fix: when API fetch returns no units, parse the same data from the
  Next.js SSR-embedded ``"units":[{...}]`` array inline in the HTML.
  Verified live 2026-05-23: maa-wade-park has 122 units inline with
  every field the API returns.
"""
from __future__ import annotations

import json

from ma_poc.pms.adapters.maac import (
    _MAAC_UNITS_ARRAY_RE,
    _extract_balanced_array,
    parse_maac_html,
)

# ─── balanced-bracket walker (shared shape with Avalon) ──────────────


def test_extract_balanced_array_simple() -> None:
    html = 'prefix [1, 2, 3] suffix'
    assert _extract_balanced_array(html, 7) == "[1, 2, 3]"


def test_extract_balanced_array_with_nested_objects() -> None:
    html = '[{"a": 1, "b": [2, 3]}, {"c": [4]}]'
    assert _extract_balanced_array(html, 0) == html


def test_extract_balanced_array_string_with_brackets() -> None:
    """Brackets inside string literals must not move the depth counter."""
    html = '[{"name": "foo[bar]baz"}]'
    assert _extract_balanced_array(html, 0) == html


def test_extract_balanced_array_unbalanced() -> None:
    assert _extract_balanced_array('[1, 2, 3', 0) == ""


# ─── MAA-specific gate: requires both 'units' array + 'minimumRent' ──


def _maa_unit(
    apartment_name: str = "2213",
    beds: int = 0,
    baths: int = 1,
    sqft: int = 616,
    min_rent: int = 1277,
    max_rent: int = 2315,
    fp_name: str = "Studio 0x1",
    available_date: str = "03/13/2026",
    unit_status: str = "Vacant Unrented Ready",
) -> dict:
    """Build a MAA-shaped unit dict matching the live 2026-05-23
    maa-wade-park SSR blob."""
    return {
        "id": "01KGMK6FFW29W2NEHB4YJBAZ65",
        "apartmentName": apartment_name,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
        "minimumRent": min_rent,
        "maximumRent": max_rent,
        "floorplanName": fp_name,
        "availableDate": available_date,
        "unitStatus": unit_status,
        "floor": "2nd Floor",
        "rentCafeFloorplanId": "2321341",
        "rentCafePropertyId": "611806",
    }


def _wrap_nextjs_blob(units_array_json: str) -> str:
    """Wrap a units-array JSON string in a Next.js-style SSR blob
    mimicking the MAA community page layout."""
    return (
        '<html><body>'
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"community":'
        '{"propertyIntegrationID":"01JQW8XJQCTJBPN8ZC32B40XQB"},'
        '"units":' + units_array_json + '}}}'
        '</script></body></html>'
    )


def test_parse_maac_html_extracts_full_inventory() -> None:
    """The smoking-gun fix — embedded JSON yields units with rent."""
    units_json = json.dumps([
        _maa_unit(apartment_name="2213", min_rent=1277, max_rent=2315, sqft=616),
        _maa_unit(apartment_name="2214", min_rent=1450, max_rent=2500, sqft=750),
        _maa_unit(apartment_name="3101", min_rent=1850, max_rent=3100, sqft=1050, beds=2, baths=2),
    ])
    html = _wrap_nextjs_blob(units_json)
    units = parse_maac_html(html, "https://www.maac.com/maa-test/")
    assert len(units) == 3
    assert units[0]["unit_number"] == "2213"
    assert units[0]["market_rent_low"] == 1277
    assert units[0]["sqft"] == "616"
    assert units[0]["bedrooms"] == "0"
    assert units[2]["market_rent_low"] == 1850
    assert units[2]["sqft"] == "1050"


def test_parse_maac_html_empty_when_no_minimum_rent_marker() -> None:
    """Defensive gate — non-MAA pages must NOT misfire even if they
    happen to carry a ``"units":[...]`` array. Pattern is anchored on
    the MAA-specific minimumRent field."""
    html = '<html>{"units":[{"id":"X","name":"foo","price":1500}]}</html>'
    assert parse_maac_html(html, "https://x.com") == []


def test_parse_maac_html_empty_when_blob_truncated() -> None:
    """A partially-loaded SSR page (network failed mid-stream) must
    return [] rather than feed partial units downstream."""
    truncated = (
        '<script>{"units":[{"id":"X","minimumRent":1500,"apartmentName":"1"'
    )
    assert parse_maac_html(truncated, "https://x.com") == []


def test_parse_maac_html_empty_string_input() -> None:
    assert parse_maac_html("", "https://x.com") == []
    assert parse_maac_html("<html>no minimumRent here</html>", "https://x.com") == []


def test_maac_units_regex_matches_units_array_opening() -> None:
    """The regex matches any ``"units":[{`` opening — by design, since
    nested objects in MAA's first unit make a lookahead-on-minimumRent
    impossible (verified: maa-wade-park's first unit has nested
    ``floorPlanImage:{...}`` before ``minimumRent``). MAA-shape is
    enforced AFTER parse via the post-filter test below."""
    assert _MAAC_UNITS_ARRAY_RE.search('"units":[{"id":"X"}]') is not None
    assert _MAAC_UNITS_ARRAY_RE.search('"units" : [ {') is not None
    assert _MAAC_UNITS_ARRAY_RE.search('no units key here') is None


def test_parse_maac_html_post_filter_rejects_non_maa_shape() -> None:
    """A non-MAA ``"units":[…]`` array (RentCafe / SightMap shape) must
    NOT produce units — even though the opening regex matches, the
    post-parse check requires at least one item with ``minimumRent``.
    Critical guard — protects against parsing RentCafe widgets that
    co-exist on the same page as the MAA marker."""
    # The page mentions "minimumRent" elsewhere (e.g. in a comment) so
    # the prefilter passes, but the array itself uses RentCafe shape.
    html = (
        '<script>{"units":[{"id":"X","unit_number":"101",'
        '"rent_range":"$1500"}]}</script>'
        '<!-- minimumRent appears here as text only -->'
    )
    # No minimumRent on any unit dict → post-filter rejects → []
    assert parse_maac_html(html, "https://x.com") == []


def test_parse_maac_html_picks_maa_array_when_multiple_arrays_present() -> None:
    """A Next.js page may have multiple ``"units":[…]`` arrays (e.g.
    nested for site map data + the real MAA units). The parser must
    iterate them and pick the one whose items carry minimumRent."""
    non_maa = '"units":[{"id":"A","label":"site"}]'
    maa = json.dumps([_maa_unit(apartment_name="9001", min_rent=1999)])
    html = (
        f'<script>{{{non_maa},"other":1}}</script>'
        f'<script>{{"props":{{"units":{maa}}}}}</script>'
    )
    units = parse_maac_html(html, "https://www.maac.com/x/")
    assert len(units) == 1
    assert units[0]["unit_number"] == "9001"
    assert units[0]["market_rent_low"] == 1999
