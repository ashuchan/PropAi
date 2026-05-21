"""Funnel Leasing (formerly Nestio) adapter — detector + URL builder +
API parser.

Validated 2026-05-21 against 3 distinct Essex Property Trust properties
(Belcarra, Connolly Station, Allure at Scripps Ranch). All exhibit the
same pattern:
  • ``<div id="CommunityId" data-communityid="...">`` in HTML
  • ``/api/properties/{prop_id}/availability?start_date=...&end_date=
    ...&format=spa`` returns ``{result: {floorplans: [{units: [...]}]}}``
  • curl_cffi ``impersonate="chrome120"`` bypasses Vercel's bot
    challenge cleanly (plain curl gets 429).

Fixtures (real captures, 2026-05-21):
  • ``belcarra_property_page.html`` (1.0 MB), ``belcarra_api_response.json`` (29 fps, 6 units)
  • ``connolly_station_property_page.html`` (993 KB), ``connolly_station_api_response.json`` (8 fps, 17 units)

Funnel customer roster (per their marketing): Essex ~247 props, Cortland,
UDR, RedPeak, Monument, Avanti, Dermot. The Essex pattern (server-side
proxy at ``{site}.com/api/properties/{id}/availability``) is what this
adapter targets; non-proxied direct-to-Nestio integrations (Dermot's
``/properties/building/availability/apartment?id=...&team_id=...``) are
not covered today and need separate work.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from ma_poc.pms.adapters._funnel import (
    build_availability_api_url,
    detect_funnel,
    find_property_id,
    parse_funnel_api_response,
)

_FIXTURE = Path("ma_poc/tests/fixtures/funnel")


def _load_html(name: str) -> str:
    return (_FIXTURE / name).read_text(encoding="utf-8")


def _load_json(name: str) -> dict:
    return json.loads((_FIXTURE / name).read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────────────
# Detector
# ─────────────────────────────────────────────────────────────────────


def test_detect_matches_belcarra_property_page() -> None:
    assert detect_funnel(_load_html("belcarra_property_page.html")) is True


def test_detect_matches_connolly_station_property_page() -> None:
    """Generalizes across distinct Essex properties — not overfit to Belcarra."""
    assert detect_funnel(_load_html("connolly_station_property_page.html")) is True


def test_detect_rejects_html_without_communityid() -> None:
    """Plain HTML without the marker is NOT Funnel."""
    html = "<html><body><h1>Welcome</h1><p>Generic page.</p></body></html>"
    assert detect_funnel(html) is False


def test_detect_does_not_match_bare_data_property_id() -> None:
    """Other CMSes use ``data-property-id`` for unrelated purposes
    (Cortland's favorites button). Require pairing with Funnel asset
    host to accept the secondary marker."""
    html = (
        '<html><body><a data-property-id="348" data-favorite-id="348">'
        'Save</a></body></html>'
    )
    assert detect_funnel(html) is False


def test_detect_accepts_secondary_marker_with_asset_host() -> None:
    """If primary ``data-communityid`` is absent but the HTML
    references a Funnel asset host AND has ``data-property-id``, accept
    it as Funnel-shaped — this covers properties where the CommunityId
    div is dynamically inserted post-load."""
    html = (
        '<html><body>'
        '<img src="https://assets.nestiostatic.com/community_logos/x.jpg">'
        '<button data-property-id="42">Apply</button>'
        '</body></html>'
    )
    assert detect_funnel(html) is True


def test_detect_empty_or_none() -> None:
    assert detect_funnel("") is False
    assert detect_funnel("<html></html>") is False


# ─────────────────────────────────────────────────────────────────────
# Property-id discovery
# ─────────────────────────────────────────────────────────────────────


def test_find_property_id_belcarra() -> None:
    """Belcarra → 510860 (confirmed via live probe + HAR)."""
    pid = find_property_id(_load_html("belcarra_property_page.html"))
    assert pid == "510860"


def test_find_property_id_connolly_station() -> None:
    """Connolly Station → 518103 (different property, same pattern)."""
    pid = find_property_id(_load_html("connolly_station_property_page.html"))
    assert pid == "518103"


def test_find_property_id_returns_none_when_absent() -> None:
    assert find_property_id("<html></html>") is None
    assert find_property_id("") is None


def test_find_property_id_picks_first_community_id() -> None:
    """Some property pages may have multiple data-communityid attrs
    (e.g. footer widget); the first one is the correct property."""
    html = (
        '<div data-communityid="123" id="CommunityId"></div>'
        '<footer><div data-communityid="999"></div></footer>'
    )
    assert find_property_id(html) == "123"


# ─────────────────────────────────────────────────────────────────────
# API URL builder
# ─────────────────────────────────────────────────────────────────────


def test_build_api_url_default_60_day_window() -> None:
    """Default date range is today + 60 days."""
    url = build_availability_api_url(
        "https://www.essexapartmenthomes.com/", "510860",
        start_date=date(2026, 5, 21),
    )
    assert url == (
        "https://www.essexapartmenthomes.com"
        "/api/properties/510860/availability"
        "?start_date=2026-05-21&end_date=2026-07-20&format=spa"
    )


def test_build_api_url_custom_dates() -> None:
    url = build_availability_api_url(
        "https://www.essexapartmenthomes.com/apartments/bellevue/belcarra",
        "510860",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 8, 30),
    )
    assert url.endswith(
        "/api/properties/510860/availability"
        "?start_date=2026-06-01&end_date=2026-08-30&format=spa"
    )


def test_build_api_url_strips_path_keeps_origin() -> None:
    """When given a deep property URL, the API URL is rooted at origin
    — not relative to the property path."""
    url = build_availability_api_url(
        "https://www.essexapartmenthomes.com/apartments/sunnyvale/bristol-commons",
        "491712",
        start_date=date(2026, 5, 21),
        end_date=date(2026, 7, 20),
    )
    # Path is replaced by /api/properties/...
    assert "/apartments/sunnyvale" not in url
    assert url.startswith("https://www.essexapartmenthomes.com/api/properties/")


# ─────────────────────────────────────────────────────────────────────
# API response parser
# ─────────────────────────────────────────────────────────────────────


def test_parse_belcarra_returns_units() -> None:
    """Belcarra fixture has 6 units across 29 floor plans."""
    data = _load_json("belcarra_api_response.json")
    units = parse_funnel_api_response(data, source_url="https://x.test/")
    assert len(units) == 6, f"expected 6 units; got {len(units)}"


def test_parse_belcarra_unit_fields() -> None:
    """Each unit must carry the standard fields populated from the
    Funnel schema. First unit verified against the live probe."""
    data = _load_json("belcarra_api_response.json")
    units = parse_funnel_api_response(data, source_url="https://x.test/")
    # Find unit C518 (the one seen in probe + Chrome MCP)
    c518 = next((u for u in units if u["unit_number"] == "C518"), None)
    assert c518 is not None, (
        f"C518 missing; got: {[u['unit_number'] for u in units]}"
    )
    assert c518["bedrooms"] == "0"
    assert c518["bathrooms"] == "1"
    assert c518["sqft"] == "552"
    assert c518["market_rent_low"] == 2237
    assert c518["market_rent_high"] == 3439
    assert c518["rent_range"] == "$2,237 - $3,439"
    assert c518["availability_date"] == "2026-05-12"
    assert c518["floor_plan_name"] == "Plan SB"
    assert c518["availability_status"] == "AVAILABLE"
    assert c518["source"] == "funnel_api"
    assert c518["property_unit_id"]  # has Funnel unit_id


def test_parse_connolly_station_returns_17_units() -> None:
    """Different property, larger result set (17 units / 8 floor plans).
    Confirms the parser isn't overfit to Belcarra's shape."""
    data = _load_json("connolly_station_api_response.json")
    units = parse_funnel_api_response(data, source_url="https://x.test/")
    assert len(units) == 17, f"expected 17 units; got {len(units)}"


def test_parse_connolly_station_inherits_floor_plan_per_unit() -> None:
    """Every unit carries its own floor_plan_name from the per-unit
    payload — not lost to the floor-plan-level container."""
    data = _load_json("connolly_station_api_response.json")
    units = parse_funnel_api_response(data, source_url="")
    no_plan = [u for u in units if not u["floor_plan_name"]]
    assert no_plan == [], (
        f"{len(no_plan)} units missing floor_plan_name"
    )


def test_parse_handles_concession_from_specials() -> None:
    """When the Funnel ``specials`` array carries a concession, the
    description is surfaced as the concession field."""
    data = {
        "result": {
            "floorplans": [{
                "name": "1BR",
                "units": [{
                    "unit_id": 1, "name": "101",
                    "floorplan_name": "1BR", "beds": 1, "baths": 1,
                    "sqft": 700, "minimum_rent": 1500, "maximum_rent": 1500,
                    "availability_date": "2026-06-01T00:00:00.000Z",
                    "specials": [
                        {"description": "1 Month Free on 13-month lease"}
                    ],
                    "amenities": [],
                }],
            }],
        }
    }
    units = parse_funnel_api_response(data)
    assert len(units) == 1
    assert units[0]["concession"] == "1 Month Free on 13-month lease"


def test_parse_strips_iso_timestamp_from_availability_date() -> None:
    """Funnel ships ``YYYY-MM-DDTHH:MM:SS.000Z``; we keep only the
    date prefix so it matches the rest of the adapter's date format."""
    data = _load_json("belcarra_api_response.json")
    units = parse_funnel_api_response(data)
    for u in units:
        if u["availability_date"]:
            assert "T" not in u["availability_date"], (
                f"date has T-suffix: {u['availability_date']}"
            )
            assert re.match(r"^\d{4}-\d{2}-\d{2}$", u["availability_date"])


def test_parse_dedups_units_by_unit_id() -> None:
    """If the same unit somehow appears in multiple floor plans (e.g.
    re-categorisation), dedup by unit_id to avoid double-emitting."""
    data = {
        "result": {
            "floorplans": [
                {"name": "A", "units": [
                    {"unit_id": 42, "name": "X", "floorplan_name": "A",
                     "beds": 1, "baths": 1, "sqft": 700,
                     "minimum_rent": 1500, "maximum_rent": 1500,
                     "availability_date": "", "specials": []},
                ]},
                {"name": "B", "units": [
                    {"unit_id": 42, "name": "X", "floorplan_name": "B",
                     "beds": 1, "baths": 1, "sqft": 700,
                     "minimum_rent": 1500, "maximum_rent": 1500,
                     "availability_date": "", "specials": []},
                ]},
            ]
        }
    }
    units = parse_funnel_api_response(data)
    assert len(units) == 1


def test_parse_returns_empty_on_malformed() -> None:
    assert parse_funnel_api_response({}) == []
    assert parse_funnel_api_response(None) == []  # type: ignore[arg-type]
    assert parse_funnel_api_response({"result": {}}) == []
    assert parse_funnel_api_response({"result": {"floorplans": "not-a-list"}}) == []


def test_parse_handles_zero_rent_unit() -> None:
    """Belcarra fixture has no such zero-rent units, but verify the
    parser handles them sanely (empty rent → emit with empty rent fields,
    not crash)."""
    data = {
        "result": {
            "floorplans": [{"name": "X", "units": [
                {"unit_id": 1, "name": "1A", "floorplan_name": "X",
                 "beds": 1, "baths": 1, "sqft": 700,
                 "minimum_rent": None, "maximum_rent": None,
                 "availability_date": "", "specials": []},
            ]}]
        }
    }
    units = parse_funnel_api_response(data)
    assert len(units) == 1
    assert units[0]["market_rent_low"] is None
    assert units[0]["rent_range"] == ""
    assert units[0]["availability_status"] == ""
