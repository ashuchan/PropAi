"""Tests for pms/adapters/_api_parser.py — the Jugnu-native parser utilities.

PR-1: verifies that the extracted functions behave identically to the
originals in scripts/entrata.py and scripts/scrape_properties.py.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters._api_parser import (
    TARGET_JSONLD_TYPES,
    _RENT_KEYS,
    _RENT_MAX,
    _RENT_MIN,
    _UNIT_ID_KEYS,
    _extract_rent,
    _find_list,
    _get,
    _is_low_signal_units,
    _jsonld_floor_size,
    _jsonld_item_has_unit_signal,
    _money_to_int,
    _units_below_expected,
    _walk_jsonld,
    parse_api_responses,
    parse_sightmap_payload,
    realpage_units_from_body,
    realpage_units_to_adapter_shape,
)


# ── _money_to_int ──────────────────────────────────────────────────────────────

class TestMoneyToInt:
    def test_dollar_sign_and_commas(self):
        assert _money_to_int("$1,450") == 1450

    def test_float_string(self):
        assert _money_to_int("1450.00") == 1450

    def test_plain_int(self):
        assert _money_to_int(1450) == 1450

    def test_with_currency_suffix(self):
        assert _money_to_int("1,450 USD") == 1450

    def test_none_returns_none(self):
        assert _money_to_int(None) is None

    def test_empty_string_returns_none(self):
        assert _money_to_int("") is None

    def test_non_numeric_returns_none(self):
        assert _money_to_int("N/A") is None

    def test_zero_returns_zero(self):
        assert _money_to_int("0") == 0


# ── _get ───────────────────────────────────────────────────────────────────────

class TestGet:
    def test_first_key_wins(self):
        d = {"a": "1", "b": "2"}
        assert _get(d, "a", "b") == "1"

    def test_falls_through_to_second(self):
        d = {"b": "2"}
        assert _get(d, "a", "b") == "2"

    def test_skips_empty_string(self):
        d = {"a": "", "b": "x"}
        assert _get(d, "a", "b") == "x"

    def test_skips_none(self):
        d = {"a": None, "b": "x"}
        assert _get(d, "a", "b") == "x"

    def test_unwraps_nested_dict_min(self):
        d = {"rent": {"min": 1200, "max": 1500}}
        assert _get(d, "rent") == "1200"

    def test_returns_empty_when_nothing_matches(self):
        d = {"x": "y"}
        assert _get(d, "a", "b") == ""


# ── _find_list ─────────────────────────────────────────────────────────────────

class TestFindList:
    def test_finds_first_matching_key(self):
        d = {"units": [{"id": 1}], "other": []}
        assert _find_list(d, ("units",)) == [{"id": 1}]

    def test_skips_empty_lists(self):
        d = {"units": [], "floorPlans": [{"id": 1}]}
        assert _find_list(d, ("units", "floorPlans")) == [{"id": 1}]

    def test_returns_empty_on_non_dict(self):
        assert _find_list([1, 2], ("units",)) == []

    def test_returns_empty_when_no_key_found(self):
        d = {"x": "y"}
        assert _find_list(d, ("units",)) == []


# ── _extract_rent ──────────────────────────────────────────────────────────────

class TestExtractRent:
    def test_flat_keys(self):
        lo, hi = _extract_rent({"minRent": 1200, "maxRent": 1500})
        assert lo == 1200
        assert hi == 1500

    def test_nested_dict(self):
        lo, hi = _extract_rent({"rent": {"min": 1350, "max": 1350}})
        assert lo == 1350
        assert hi == 1350

    def test_nested_list(self):
        lo, hi = _extract_rent({"rentTerms": [{"rent": 1200, "term": 12}, {"rent": 1100}]})
        assert lo == 1100  # smallest value wins

    def test_missing_hi_copies_lo(self):
        lo, hi = _extract_rent({"price": 1500})
        assert lo == hi == 1500

    def test_no_rent(self):
        lo, hi = _extract_rent({"name": "Unit A"})
        assert lo is None
        assert hi is None


# ── JSON-LD helpers ────────────────────────────────────────────────────────────

class TestJsonLd:
    def test_target_types_constant(self):
        assert "Apartment" in TARGET_JSONLD_TYPES
        assert "Offer" in TARGET_JSONLD_TYPES
        assert "Place" not in TARGET_JSONLD_TYPES

    def test_walk_finds_nested(self):
        node = {
            "@graph": [
                {"@type": "Apartment", "name": "Unit A"},
                {"@type": "LocalBusiness", "child": {"@type": "Offer", "name": "O1"}},
            ]
        }
        out: list = []
        _walk_jsonld(node, out)
        types = [x["@type"] for x in out]
        assert "Apartment" in types
        assert "Offer" in types

    def test_jsonld_floor_size_dict(self):
        item = {"floorSize": {"value": 850}}
        assert _jsonld_floor_size(item) == "850"

    def test_jsonld_floor_size_scalar(self):
        item = {"floorSize": 900}
        assert _jsonld_floor_size(item) == "900"

    def test_jsonld_floor_size_missing(self):
        assert _jsonld_floor_size({}) == ""

    def test_item_has_unit_signal_offers(self):
        item = {"@type": "ApartmentComplex", "offers": {"price": 1500}}
        assert _jsonld_item_has_unit_signal(item) is True

    def test_item_has_unit_signal_no_data(self):
        item = {"@type": "ApartmentComplex", "name": "Test Property"}
        assert _jsonld_item_has_unit_signal(item) is False

    def test_item_has_unit_signal_floor_size(self):
        item = {"@type": "Apartment", "floorSize": {"value": 850}}
        assert _jsonld_item_has_unit_signal(item) is True


# ── _is_low_signal_units ───────────────────────────────────────────────────────

class TestIsLowSignal:
    def test_empty_is_low_signal(self):
        assert _is_low_signal_units([]) is True

    def test_unit_with_rent_is_not_low_signal(self):
        units = [{"floor_plan_name": "A1", "rent_range": "$1,500"}]
        assert _is_low_signal_units(units) is False

    def test_unit_with_id_is_not_low_signal(self):
        units = [{"unit_number": "101", "floor_plan_name": "A1"}]
        assert _is_low_signal_units(units) is False

    def test_name_only_is_low_signal(self):
        units = [{"floor_plan_name": "A1"}]
        assert _is_low_signal_units(units) is True


# ── _units_below_expected ──────────────────────────────────────────────────────

class TestUnitsBelowExpected:
    def test_no_expected_returns_false(self):
        assert _units_below_expected([{"rent_range": "$1,500"}] * 2, None) is False

    def test_enough_units_returns_false(self):
        units = [{"rent_range": "$1,500"}] * 5
        assert _units_below_expected(units, 10) is False

    def test_few_units_with_rent_returns_false(self):
        units = [{"rent_range": "$1,500"}]
        assert _units_below_expected(units, 50) is False

    def test_few_units_without_rent_returns_true(self):
        units = [{"floor_plan_name": "A1"}]
        assert _units_below_expected(units, 50) is True


# ── parse_sightmap_payload ─────────────────────────────────────────────────────

class TestParseSightmapPayload:
    def _body(self):
        return {
            "data": {
                "floor_plans": [
                    {"id": "1", "name": "1BR", "bedroom_count": 1, "bathroom_count": 1},
                ],
                "units": [
                    {"floor_plan_id": "1", "unit_number": "101", "price": 1500,
                     "area": 750, "available_on": "2026-06-01"},
                ],
            }
        }

    def test_basic_parse(self):
        units = parse_sightmap_payload(self._body(), "https://sightmap.com/api")
        assert len(units) == 1
        u = units[0]
        assert u["unit_number"] == "101"
        assert u["floor_plan_name"] == "1BR"
        assert u["rent_range"] == "$1,500"
        assert u["sqft"] == "750"
        assert u["bed_label"] == "1 Bedroom"

    def test_studio_label(self):
        body = {
            "data": {
                "floor_plans": [{"id": "1", "name": "Studio", "bedroom_count": 0}],
                "units": [{"floor_plan_id": "1", "price": 1200}],
            }
        }
        units = parse_sightmap_payload(body, "https://sightmap.com")
        assert units[0]["bed_label"] == "Studio"

    def test_empty_body_returns_empty(self):
        assert parse_sightmap_payload({}, "https://sightmap.com") == []

    def test_extraction_tier(self):
        units = parse_sightmap_payload(self._body(), "https://sightmap.com/api")
        assert units[0]["extraction_tier"] == "TIER_1_API_SIGHTMAP"


# ── realpage_units_from_body ───────────────────────────────────────────────────

class TestRealPageParser:
    def test_floorplans_endpoint(self):
        body = {
            "response": {
                "floorplans": [
                    {"id": "FP1", "name": "1BR/1BA", "bedRooms": 1,
                     "sqft": 750, "minRent": 1400, "maxRent": 1500},
                ]
            }
        }
        units = realpage_units_from_body(body, "https://api.ws.realpage.com/floorplans")
        assert len(units) == 1
        u = units[0]
        assert u["unit_id"] == "FP1"
        assert u["market_rent_low"] == 1400
        assert u["_floor_plan"] == "1BR/1BA"

    def test_units_endpoint(self):
        body = {
            "response": [
                {"unitNumber": "101", "rent": 1500, "availableDate": "2026-06-01"},
                {"unitNumber": "102", "rent": 100},  # below rent min — skipped
            ]
        }
        units = realpage_units_from_body(body, "https://api.ws.realpage.com/units")
        assert len(units) == 1
        assert units[0]["unit_id"] == "101"
        assert units[0]["market_rent_low"] == 1500

    def test_null_response_returns_empty(self):
        body = {"response": None}
        assert realpage_units_from_body(body, "url") == []

    def test_adapter_shape_conversion(self):
        body = {
            "response": [{"unitNumber": "A1", "rent": 1500}]
        }
        units = realpage_units_to_adapter_shape(body, "https://api.ws.realpage.com/units")
        assert len(units) == 1
        u = units[0]
        assert u["unit_number"] == "A1"
        assert u["rent_range"] == "$1,500"
        assert "extraction_tier" in u


# ── parse_api_responses ────────────────────────────────────────────────────────

class TestParseApiResponses:
    def test_flat_list_response(self):
        responses = [{
            "url": "https://example.com/api/units",
            "body": [
                {"floorPlanName": "1BR", "minRent": 1500, "beds": "1", "sqft": "750"},
                {"floorPlanName": "2BR", "minRent": 2000, "beds": "2", "sqft": "1000"},
            ],
        }]
        units = parse_api_responses(responses)
        assert len(units) == 2
        names = {u["floor_plan_name"] for u in units}
        assert names == {"1BR", "2BR"}

    def test_nested_envelope(self):
        responses = [{
            "url": "https://example.com/api/availability",
            "body": {"data": {"units": [{"floorPlanName": "A1", "minRent": 1300}]}},
        }]
        units = parse_api_responses(responses)
        assert len(units) == 1
        assert units[0]["floor_plan_name"] == "A1"

    def test_deduplication(self):
        item = {"floorPlanName": "1BR", "minRent": 1500, "beds": "1"}
        responses = [{"url": "url", "body": [item, item]}]
        units = parse_api_responses(responses)
        assert len(units) == 1

    def test_skips_no_signal_items(self):
        responses = [{
            "url": "url",
            "body": [{"irrelevant_field": "value"}],
        }]
        units = parse_api_responses(responses)
        assert units == []

    def test_sightmap_host_routing(self):
        body = {
            "data": {
                "floor_plans": [{"id": "1", "name": "Studio", "bedroom_count": 0}],
                "units": [{"floor_plan_id": "1", "price": 1200}],
            }
        }
        responses = [{"url": "https://app.sightmap.com/api", "body": body}]
        units = parse_api_responses(responses)
        assert len(units) == 1
        assert units[0]["extraction_tier"] == "TIER_1_API_SIGHTMAP"

    def test_rent_display_formatting(self):
        responses = [{
            "url": "url",
            "body": [{"floorPlanName": "A1", "minRent": 1200, "maxRent": 1500}],
        }]
        units = parse_api_responses(responses)
        assert units[0]["rent_range"] == "$1,200 - $1,500"

    def test_empty_responses_returns_empty(self):
        assert parse_api_responses([]) == []

    def test_bed_label_studio_from_zero(self):
        responses = [{
            "url": "url",
            "body": [{"floorPlanName": "Studio", "beds": "0", "minRent": 1100}],
        }]
        units = parse_api_responses(responses)
        assert units[0]["bed_label"] == "Studio"

    def test_multiple_apis_merged(self):
        responses = [
            {"url": "url1", "body": [{"floorPlanName": "1BR", "minRent": 1500}]},
            {"url": "url2", "body": [{"floorPlanName": "2BR", "minRent": 2000}]},
        ]
        units = parse_api_responses(responses)
        assert len(units) == 2


# ── Constants ──────────────────────────────────────────────────────────────────

class TestConstants:
    def test_rent_bounds(self):
        assert _RENT_MIN == 200
        assert _RENT_MAX == 50_000

    def test_unit_id_keys_ordered_tuple(self):
        assert isinstance(_UNIT_ID_KEYS, tuple)
        assert "unit_number" in _UNIT_ID_KEYS
        assert "unit_id" in _UNIT_ID_KEYS

    def test_rent_keys_is_set(self):
        assert "minRent" in _RENT_KEYS
        assert "rent" in _RENT_KEYS
        assert "price" in _RENT_KEYS


# ── plan/unit foreign-key join (ported from main, 2026-05-22) ───────────────
# When an API response holds BOTH a plan list and a unit list joined by a
# foreign key (layoutId / floorPlanId / etc.), the unit list is authoritative
# (per-unit rent + availability) but lacks sqft — sqft lives on the plan. The
# "largest list wins" rule emits units with empty sqft / floor_plan_name.
# The FK detector joins them so each unit inherits its plan's area + name.


class TestPlanUnitForeignKeyJoin:
    _BODY = {
        "layouts": [
            {"id": 1, "area": 850, "beds": 1, "baths": 1, "name": "The Oak"},
            {"id": 2, "area": 1100, "beds": 2, "baths": 2, "name": "The Maple"},
            {"id": 3, "area": 1300, "beds": 3, "baths": 2, "name": "The Birch"},
            {"id": 4, "area": 600, "beds": 0, "baths": 1, "name": "The Studio"},
        ],
        "units": [
            {"layoutId": 1, "unitNumber": "101", "displayPrice": 1500,
             "availableOn": "2026-06-01"},
            {"layoutId": 2, "unitNumber": "205", "displayPrice": 2100,
             "availableOn": "2026-06-15"},
        ],
    }

    def test_units_inherit_plan_sqft_via_fk(self):
        """Each unit must inherit area + floor-plan name from its FK plan."""
        units = parse_api_responses(
            [{"url": "https://x.test/api/inventory", "body": self._BODY}]
        )
        assert len(units) == 2, f"expected 2 units, got {len(units)}"
        by_num = {u["unit_number"]: u for u in units}
        u101 = by_num["101"]
        assert str(u101.get("sqft")) == "850", f"unit 101 sqft: {u101.get('sqft')!r}"
        assert u101["floor_plan_name"] == "The Oak"
        assert u101["rent_range"] == "$1,500"
        u205 = by_num["205"]
        assert str(u205.get("sqft")) == "1100"
        assert u205["floor_plan_name"] == "The Maple"

    def test_unit_fields_win_over_plan_on_conflict(self):
        """Per-unit data is authoritative — a unit's own value overrides
        the plan's on any overlapping key."""
        body = {
            "layouts": [{"id": 1, "area": 850, "beds": 1, "baths": 1,
                         "name": "Plan A", "displayPrice": 999}],
            "units": [
                {"layoutId": 1, "unitNumber": "1A", "displayPrice": 1700,
                 "availableOn": "2026-07-01"},
                {"layoutId": 1, "unitNumber": "1B", "displayPrice": 1750,
                 "availableOn": "2026-07-05"},
            ],
        }
        units = parse_api_responses([{"url": "https://x.test/api", "body": body}])
        assert len(units) == 2
        rents = sorted(u["rent_range"] for u in units)
        # the unit's displayPrice (1700/1750) wins, not the plan's 999
        assert rents == ["$1,700", "$1,750"]

    def test_no_fk_pair_falls_back_to_largest(self):
        """A response with no plan/unit FK pair must still parse via the
        existing 'largest list wins' path — the join is additive only."""
        body = {"units": [
            {"floorPlanName": "1BR", "minRent": 1500, "beds": "1", "sqft": "750"},
            {"floorPlanName": "2BR", "minRent": 2000, "beds": "2", "sqft": "1000"},
        ]}
        units = parse_api_responses([{"url": "https://x.test/api", "body": body}])
        assert len(units) == 2
        assert {u["floor_plan_name"] for u in units} == {"1BR", "2BR"}
