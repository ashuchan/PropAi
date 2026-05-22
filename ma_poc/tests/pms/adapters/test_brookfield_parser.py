"""Brookfield Properties WP-middleware parser tests.

Live-capture fixture is the Turtle Cove (PID 2519) response trimmed to
3 floor-plan rows. Validates the schema projection, URL/body gating, and
the explicit decision NOT to fabricate ``concession_value`` from
``originalMinRent`` (see parser module docstring for the rationale).
"""

from __future__ import annotations

import json
from pathlib import Path

from ma_poc.pms.adapters._brookfield_parser import (
    is_brookfield_url,
    parse_brookfield_units,
    try_parse_brookfield,
)

FIXTURES = Path(__file__).parent / "fixtures" / "brookfield"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# URL gate
# ---------------------------------------------------------------------------


def test_is_brookfield_url_matches_wp_middleware() -> None:
    assert is_brookfield_url(
        "https://rent.brookfieldproperties.com/wp-json/middleware/v1/getFloorplans/?propertyId[]=1807803"
    )


def test_is_brookfield_url_matches_uppercase_host() -> None:
    assert is_brookfield_url(
        "https://Rent.BrookfieldProperties.com/wp-json/middleware/v1/getFloorplans/"
    )


def test_is_brookfield_url_rejects_marketing_host() -> None:
    # Property's vanity marketing host is NOT Brookfield middleware.
    assert not is_brookfield_url("https://turtlecoveapartments.com/property/turtle-cove/")


def test_is_brookfield_url_rejects_empty() -> None:
    assert not is_brookfield_url("")


# ---------------------------------------------------------------------------
# Body-shape gate
# ---------------------------------------------------------------------------


def test_try_parse_brookfield_rejects_non_brookfield_url() -> None:
    units, matched = try_parse_brookfield(
        {"url": "https://example.com/api/x", "body": _load("turtle_cove_capture.json")}
    )
    assert not matched
    assert units == []


def test_try_parse_brookfield_rejects_brookfield_url_with_wrong_body_shape() -> None:
    # URL matches but body is a dict (not the documented list-at-root).
    # Without the body-shape guard we'd accept and emit zero rows; with it
    # we surface matched=False so the caller can fall through.
    units, matched = try_parse_brookfield(
        {
            "url": "https://rent.brookfieldproperties.com/wp-json/middleware/v1/getFloorplans/",
            "body": {"unexpected": "wrapper"},
        }
    )
    assert not matched
    assert units == []


def test_try_parse_brookfield_rejects_list_without_floorplan_name() -> None:
    # An unrelated list-at-root payload from the same host should be ignored.
    units, matched = try_parse_brookfield(
        {
            "url": "https://rent.brookfieldproperties.com/wp-json/something-else/",
            "body": [{"unrelated": "data"}],
        }
    )
    assert not matched
    assert units == []


# ---------------------------------------------------------------------------
# Field projection (live capture)
# ---------------------------------------------------------------------------


def test_parse_brookfield_extracts_all_fixture_rows() -> None:
    body = _load("turtle_cove_capture.json")
    url = "https://rent.brookfieldproperties.com/wp-json/middleware/v1/getFloorplans/?propertyId[]=1807803"
    units = parse_brookfield_units(body, url)
    assert len(units) == 3


def test_parse_brookfield_preserves_floor_plan_names_verbatim() -> None:
    body = _load("turtle_cove_capture.json")
    units = parse_brookfield_units(body, "")
    names = [u["floor_plan_name"] for u in units]
    # We do NOT rewrite to canonical names (the LLM did this and shipped
    # wrong mappings into profile_replay). Raw API name is the source of
    # truth — the canonicalisation belongs in a separate plan-resolver
    # step, not here.
    assert names == ["1B Renovation 3", "2B Renovation 3", "1A Renovation 3"]


def test_parse_brookfield_rent_uses_minimum_maximum_rent() -> None:
    body = _load("turtle_cove_capture.json")
    units = parse_brookfield_units(body, "")
    first = units[0]
    # minimumRent=1922 / maximumRent=2558 from the fixture.
    assert first["market_rent_low"] == 1922
    assert first["market_rent_high"] == 2558


def test_parse_brookfield_does_not_fabricate_concession_value() -> None:
    body = _load("turtle_cove_capture.json")
    units = parse_brookfield_units(body, "")
    # The historical LLM mapping wrote concession_value = originalMinRent.
    # originalMinRent (1835) < minimumRent (1922) on the first row, which
    # is the OPPOSITE direction a real discount points. We refuse to
    # fabricate a numeric concession; we only flag has-specials as text.
    for u in units:
        assert "concession_value" not in u or u.get("concession_value") in (None, "")
    # All three fixture rows have hasSpecials="1".
    assert all(u["concession"] == "Has specials" for u in units)


def test_parse_brookfield_sqft_uses_sqft_acronym_fields() -> None:
    body = _load("turtle_cove_capture.json")
    units = parse_brookfield_units(body, "")
    # 1B Renovation 3 has min=max=845.
    assert units[0]["sqft"] == "845"
    # 2B Renovation 3: min=max=1160.
    assert units[1]["sqft"] == "1160"


def test_parse_brookfield_availability_count_and_date() -> None:
    body = _load("turtle_cove_capture.json")
    units = parse_brookfield_units(body, "")
    first = units[0]
    assert first["available_units"] == "12"
    assert first["availability_date"] == "2026-04-28"
    assert first["availability_status"] == "AVAILABLE"


def test_parse_brookfield_handles_zero_availability() -> None:
    body = [
        {
            "floorplanName": "Phantom",
            "beds": "1",
            "baths": "1",
            "minimumSQFT": "700",
            "maximumSQFT": "700",
            "minimumRent": "1500.00",
            "maximumRent": "1500.00",
            "availableUnitsCount": "0",
            "availableDate": "",
            "hasSpecials": "0",
            "propertyName": "Phantom Apartments",
        }
    ]
    units = parse_brookfield_units(body, "")
    assert len(units) == 1
    assert units[0]["availability_status"] == "UNKNOWN"
    assert units[0]["available_units"] == ""
    assert units[0]["concession"] == ""


def test_parse_brookfield_trims_sentinel_dates() -> None:
    body = [
        {
            "floorplanName": "Sentinel",
            "beds": "1",
            "baths": "1",
            "minimumSQFT": "700",
            "maximumSQFT": "700",
            "minimumRent": "1500.00",
            "maximumRent": "1500.00",
            "availableUnitsCount": "1",
            # Brookfield sometimes ships pre-epoch sentinel dates.
            "availableDate": "0000-00-00",
            "hasSpecials": "0",
            "propertyName": "Sentinel Apartments",
        }
    ]
    units = parse_brookfield_units(body, "")
    assert units[0]["availability_date"] == ""


def test_parse_brookfield_sqft_range_when_min_neq_max() -> None:
    body = [
        {
            "floorplanName": "Range Plan",
            "beds": "1",
            "baths": "1",
            "minimumSQFT": "700",
            "maximumSQFT": "750",
            "minimumRent": "1500.00",
            "maximumRent": "1700.00",
            "availableUnitsCount": "1",
            "availableDate": "2026-06-01",
            "hasSpecials": "0",
            "propertyName": "Range Apartments",
        }
    ]
    units = parse_brookfield_units(body, "")
    assert units[0]["sqft"] == "700-750"


def test_parse_brookfield_propagates_source_url() -> None:
    body = _load("turtle_cove_capture.json")
    url = "https://rent.brookfieldproperties.com/wp-json/middleware/v1/getFloorplans/?propertyId[]=1807803"
    units = parse_brookfield_units(body, url)
    for u in units:
        assert u["source_api_url"] == url
        assert u["extraction_tier"] == "TIER_1_API_BROOKFIELD"


def test_parse_brookfield_emits_plan_level_rows_with_empty_unit_number() -> None:
    # The middleware response is floor-plan-level — no per-apartment unit
    # numbers. We surface them with empty unit_number so post_process can
    # route to plan_summaries automatically.
    body = _load("turtle_cove_capture.json")
    units = parse_brookfield_units(body, "")
    for u in units:
        assert u["unit_number"] == ""


def test_try_parse_brookfield_full_round_trip() -> None:
    resp = {
        "url": "https://rent.brookfieldproperties.com/wp-json/middleware/v1/getFloorplans/?propertyId[]=1807803",
        "body": _load("turtle_cove_capture.json"),
    }
    units, matched = try_parse_brookfield(resp)
    assert matched
    assert len(units) == 3
