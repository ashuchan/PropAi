"""FortressTech adapter tests — parser + detector + registry wiring.

Acceptance (canary 1ef1060 regr#14, 2026-05-25):

- Iframe URL recoverable from marketing-page HTML for both
  ``availability.fortresstech.io`` and ``embed.fortresstech.io`` subdomains;
  ``portal.fortresstech.io`` (auth/contact) is NOT extracted as a units source.
- Live Carlson Place iframe SSR fixture parses to 9 unit-level rows with
  concrete unit numbers (e.g. ``CPC-105``), per-unit rent, ``unitMoveInDate``,
  and floor-plan dims (bed/bath/sqft).
- Empty units array (``"data":[]`` under ``queryKey:["units"]``) returns []
  without raising.
- Money / bed / bath parsing handles the FortressTech payload conventions
  (``unitQuotingRent`` as a number, ``floorPlanBeds`` 0 = Studio, decimal
  baths like 1.5 preserved).
- Detector routes the FortressTech iframe HTML marker to pms="fortresstech";
  ``portal.fortresstech.io`` (auth/contact) alone does NOT route here.
- Adapter is registered and ``matches_response_body`` accepts SSR / iframe
  fingerprints.
"""

from __future__ import annotations

from pathlib import Path

import ma_poc.pms.adapters  # noqa: F401  # populate adapter registry
from ma_poc.pms.adapters.fortresstech import (
    FortressTechAdapter,
    _balanced_json_array,
    _extract_units_from_ssr_chunk,
    _items_to_units,
    find_fortresstech_iframe_url,
    parse_fortresstech_iframe_html,
)
from ma_poc.pms.adapters.registry import get_adapter
from ma_poc.pms.detector import _detect_html_markers

FIXTURES = Path(__file__).parent / "fixtures" / "fortresstech"


def _load_iframe_html() -> str:
    return (FIXTURES / "carlson_place_iframe.html").read_text(encoding="utf-8")


def _load_parent_html() -> str:
    return (FIXTURES / "carlson_place_parent.html").read_text(encoding="utf-8")


def test_find_iframe_url_availability_subdomain() -> None:
    """Carlson Place fixture has the ``availability.`` subdomain iframe."""
    url = find_fortresstech_iframe_url(_load_parent_html())
    assert url is not None
    assert "availability.fortresstech.io/unit-availability/" in url
    # Both UUID-shaped path segments must be preserved verbatim.
    assert "26b2b2cc-7df5-4d1f-b437-23fdf9e45d83" in url
    assert "08c07271-eb06-4536-909a-7c6afe663068" in url


def test_find_iframe_url_embed_subdomain() -> None:
    """Sister sites use the ``embed.`` subdomain — same path shape."""
    html = (
        '<iframe src="https://www.embed.fortresstech.io/unit-availability/'
        'abcd1234-5678-90ab-cdef-1234567890ab/'
        '9876fedc-ba98-7654-3210-fedcba987654/?version=2" '
        'frameborder="0"></iframe>'
    )
    url = find_fortresstech_iframe_url(html)
    assert url is not None
    assert "embed.fortresstech.io/unit-availability/" in url


def test_find_iframe_url_rejects_portal_subdomain() -> None:
    """``portal.fortresstech.io`` is the auth/contact host — no unit data."""
    html = (
        '<iframe src="https://www.portal.fortresstech.io/'
        '26b2b2cc-7df5-4d1f-b437-23fdf9e45d83/'
        '08c07271-eb06-4536-909a-7c6afe663068/contact-us/" '
        'frameborder="0"></iframe>'
    )
    assert find_fortresstech_iframe_url(html) is None


def test_find_iframe_url_no_iframe() -> None:
    assert find_fortresstech_iframe_url("") is None
    assert find_fortresstech_iframe_url("<html><body>nothing here</body></html>") is None


def test_balanced_json_array_handles_nested_brackets_and_strings() -> None:
    """Bracket balancer must skip brackets inside string literals + escapes."""
    s = '[1, 2, [3, "][4]"], {"k": [5, 6]}]rest'
    arr = _balanced_json_array(s, 0)
    assert arr == '[1, 2, [3, "][4]"], {"k": [5, 6]}]'

    # Open-bracket not at start position → None
    assert _balanced_json_array("[1,2]", 1) is None
    # Unbalanced
    assert _balanced_json_array("[1, [2", 0) is None


def test_extract_units_from_live_ssr_chunk() -> None:
    """The live Carlson Place SSR chunk yields exactly 9 unit dicts."""
    html = _load_iframe_html()
    # Decode the embedded chunk the same way the parser does, then drill in.
    import json
    import re

    push_re = re.compile(r"self\.__next_f\.push\(\[\s*1\s*,\s*(\"(?:[^\"\\]|\\.)*\")\s*\]\)", re.DOTALL)
    found: list[dict] = []
    for m in push_re.finditer(html):
        chunk = json.loads(m.group(1))
        if not isinstance(chunk, str) or '"queryKey":["units"]' not in chunk:
            continue
        items = _extract_units_from_ssr_chunk(chunk)
        if items:
            found = items
            break
    assert len(found) == 9
    first = found[0]
    assert first["unitNumber"] == "CPC-105"
    assert first["unitQuotingRent"] == 1095
    assert first["floorPlanName"] == "The Sheyenne"
    assert first["floorPlanBeds"] == 1
    assert first["floorPlanSquareFeet"] == 777


def test_parse_iframe_html_live_fixture() -> None:
    """End-to-end: live iframe HTML → 9 standard unit dicts."""
    rows = parse_fortresstech_iframe_html(
        _load_iframe_html(), "https://www.availability.fortresstech.io/unit-availability/x/y/"
    )
    assert len(rows) == 9
    unit_nos = {r["unit_number"] for r in rows}
    assert "CPC-105" in unit_nos
    assert "CPD-310" in unit_nos
    assert "CPB-209" in unit_nos

    cpc105 = next(r for r in rows if r["unit_number"] == "CPC-105")
    assert cpc105["market_rent_low"] == 1095
    assert cpc105["market_rent_high"] == 1095
    assert cpc105["floor_plan_name"] == "The Sheyenne"
    assert cpc105["bedrooms"] == "1"
    assert cpc105["bathrooms"] == "1"
    assert cpc105["sqft"] == "777"
    assert cpc105["availability_status"] == "AVAILABLE"
    assert cpc105["availability_date"] == "2026-08-15"
    assert cpc105["extraction_tier"] == "TIER_1_SSR_FORTRESSTECH"
    # Stable PMS-native UUID survives into source_ids for cross-run dedup.
    assert (
        cpc105["source_ids"].get("fortresstech_unit_id")
        == "95e1da1e-f12a-42fc-93f1-5f39529a7fe2"
    )


def test_parse_iframe_html_handles_empty_units_array() -> None:
    """A fully-rendered iframe with zero availability returns [] cleanly."""
    chunk = (
        '5:["$","$Lf",null,{"state":{"queries":[{"state":{"data":'
        '{"meta":{"count":0},"data":[]},"status":"success"},'
        '"queryKey":["units"]}]}}]'
    )
    import json

    # Build a synthetic self.__next_f.push wrapper around the chunk.
    wrapped = (
        '<script>self.__next_f.push([1,' + json.dumps(chunk) + '])</script>'
    )
    rows = parse_fortresstech_iframe_html(wrapped, "u")
    assert rows == []


def test_parse_iframe_html_no_ssr_chunks() -> None:
    """Body without ``self.__next_f`` returns [] without crashing."""
    assert parse_fortresstech_iframe_html("<html>no next.js here</html>", "u") == []
    assert parse_fortresstech_iframe_html("", "u") == []


def test_items_to_units_studio_and_decimal_baths() -> None:
    """Bed=0 → Studio label; 1.5 baths preserved as decimal string."""
    items = [
        {
            "unitId": "studio-uuid",
            "unitNumber": "S-101",
            "unitQuotingRent": 875,
            "unitMoveInDate": "2026-06-01",
            "floorPlanName": "The Studio",
            "floorPlanBeds": 0,
            "floorPlanBaths": 1,
            "floorPlanSquareFeet": 500,
        },
        {
            "unitId": "split-bath-uuid",
            "unitNumber": "B-204",
            "unitQuotingRent": 1450,
            "unitMoveInDate": "2026-07-15",
            "floorPlanName": "The Plus",
            "floorPlanBeds": 2,
            "floorPlanBaths": 1.5,
            "floorPlanSquareFeet": 950,
        },
    ]
    rows = _items_to_units(items, "u")
    assert len(rows) == 2

    studio = rows[0]
    assert studio["bedrooms"] == "0"
    assert studio["bed_label"] == "Studio"
    assert studio["bathrooms"] == "1"

    plus = rows[1]
    assert plus["bedrooms"] == "2"
    assert plus["bathrooms"] == "1.5"


def test_items_to_units_skips_rows_without_unit_number() -> None:
    """Rows missing ``unitNumber`` are dropped (no anonymous unit-level data)."""
    items = [
        {"unitNumber": "", "unitQuotingRent": 1000, "floorPlanBeds": 1},
        {"unitNumber": "ok-1", "unitQuotingRent": 1100, "floorPlanBeds": 1},
    ]
    rows = _items_to_units(items, "u")
    assert len(rows) == 1
    assert rows[0]["unit_number"] == "ok-1"


def test_detector_routes_fortresstech_iframe_marker() -> None:
    """Squarespace + FortressTech iframe → pms="fortresstech"."""
    html = (
        "<html><head><meta name='generator' content='Squarespace 8.0'></head>"
        "<body><iframe src='https://www.availability.fortresstech.io/"
        "unit-availability/26b2b2cc-7df5-4d1f-b437-23fdf9e45d83/"
        "08c07271-eb06-4536-909a-7c6afe663068/'></iframe></body></html>"
    ).lower()
    res = _detect_html_markers(html)
    assert res is not None
    assert res[0] == "fortresstech"


def test_detector_does_not_route_portal_only_iframe() -> None:
    """A page with ONLY the portal.fortresstech.io contact-us iframe is not
    a FortressTech unit-data source — detector must not route there.
    """
    html = (
        "<html><body><iframe src='https://www.portal.fortresstech.io/"
        "26b2b2cc-7df5-4d1f-b437-23fdf9e45d83/"
        "08c07271-eb06-4536-909a-7c6afe663068/contact-us/'></iframe>"
        "</body></html>"
    ).lower()
    res = _detect_html_markers(html)
    if res is not None:
        assert res[0] != "fortresstech"


def test_adapter_registered_and_matches_body() -> None:
    a = get_adapter("fortresstech")
    assert isinstance(a, FortressTechAdapter)
    assert a.pms_name == "fortresstech"
    assert a.matches_response_body("<iframe src='https://embed.fortresstech.io/...'>") is True
    assert a.matches_response_body('{"queryKey":["units"]}') is True
    assert a.matches_response_body("nothing here") is False
    # bytes also accepted
    assert (
        a.matches_response_body(b"<iframe src='https://availability.fortresstech.io/...'>")
        is True
    )


def test_static_fingerprints_includes_both_subdomains() -> None:
    a = FortressTechAdapter()
    fps = a.static_fingerprints()
    assert any("availability.fortresstech.io" in f for f in fps)
    assert any("embed.fortresstech.io" in f for f in fps)
