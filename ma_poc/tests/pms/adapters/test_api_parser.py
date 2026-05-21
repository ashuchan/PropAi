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

    # 2026-05-13 port: deposit -> rent leakage fix
    # (project_run_2026_05_11). Pre-port "$1,200 - $1,400" was sub-cleaned
    # to "12001400" and emitted as 12_001_400 (then nulled by sanity
    # bounds, so the user-visible symptom was null rent). Post-port the
    # first numeric token wins and the low bound is returned.
    def test_range_string_returns_low_bound(self):
        assert _money_to_int("$1,200 - $1,400") == 1200
        assert _money_to_int("1200 - 1400") == 1200
        assert _money_to_int("$1,200-$1,400") == 1200

    def test_range_no_longer_concatenates_to_absurd_value(self):
        # Regression guard for the original sub-cleaning bug.
        out = _money_to_int("$1,200 - $1,400")
        assert out is not None and out < 50000, (
            f"range string produced {out}; bug would have produced 12001400"
        )


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


# ────────────────────────────────────────────────────────────────────
# 2026-05-13 port: _unwrap_name_blob + make_unit_dict dual-key emission.
# See ma_poc/docs/MAY13_API_TIER_PORT_PLAN.md §4 Commit 4.
# ────────────────────────────────────────────────────────────────────


class TestUnwrapNameBlob:
    """JSON-object floor-plan names like ``{"name":"B06","provider_id":"..."}``
    must reduce to ``"B06"`` so downstream consumers don't ship the blob.
    Memory project_run_2026_05_11: 2,534 rows affected historically."""

    def test_passthrough_plain_string(self):
        from ma_poc.pms.adapters._parsing import _unwrap_name_blob
        assert _unwrap_name_blob("Studio A") == "Studio A"

    def test_none_returns_empty(self):
        from ma_poc.pms.adapters._parsing import _unwrap_name_blob
        assert _unwrap_name_blob(None) == ""

    def test_unwraps_json_object_with_name_key(self):
        from ma_poc.pms.adapters._parsing import _unwrap_name_blob
        assert _unwrap_name_blob('{"name":"B06","provider_id":"abc"}') == "B06"

    def test_unwraps_with_whitespace(self):
        from ma_poc.pms.adapters._parsing import _unwrap_name_blob
        assert _unwrap_name_blob('  {"name": "Studio Loft"} ') == "Studio Loft"

    def test_passes_through_object_without_name_key(self):
        from ma_poc.pms.adapters._parsing import _unwrap_name_blob
        # No "name" key -> return original (caller can still inspect/discard).
        assert _unwrap_name_blob('{"id": 1, "label": "A"}') == '{"id": 1, "label": "A"}'

    def test_stringifies_non_string_input(self):
        from ma_poc.pms.adapters._parsing import _unwrap_name_blob
        assert _unwrap_name_blob(123) == "123"


class TestMakeUnitDictDualDateKeys:
    """make_unit_dict must emit BOTH ``availability_date`` (legacy v1)
    AND ``available_date`` (v2 schema-reader convention).

    Background: ma_poc/core/schema_v2.py:282 reads
    ``unit.get("available_date")`` first. Pre-port adapters set only
    ``availability_date`` and the date silently dropped at the v2
    boundary on any code path that bypassed the jugnu v2 alias resolver.
    """

    def test_emits_both_date_keys(self):
        from ma_poc.pms.adapters._parsing import make_unit_dict
        u = make_unit_dict(availability_date="2026-06-15")
        assert u["availability_date"] == "2026-06-15"
        assert u["available_date"] == "2026-06-15"

    def test_both_keys_empty_when_no_date_supplied(self):
        from ma_poc.pms.adapters._parsing import make_unit_dict
        u = make_unit_dict()
        assert u["availability_date"] == ""
        assert u["available_date"] == ""

    def test_floor_plan_name_blob_unwrapped(self):
        """make_unit_dict pipes floor_plan_name through _unwrap_name_blob
        so adapters that emit JSON-object names don't poison the v2
        output."""
        from ma_poc.pms.adapters._parsing import make_unit_dict
        u = make_unit_dict(floor_plan_name='{"name":"A1","provider_id":"x"}')
        assert u["floor_plan_name"] == "A1"


# ────────────────────────────────────────────────────────────────────
# Coverage fills for _parsing.py helpers. These exercise the edge
# paths missed by adapter-level tests.
# ────────────────────────────────────────────────────────────────────


class TestParsingHelpers:
    def test_get_field_first_present_key_wins(self):
        from ma_poc.pms.adapters._parsing import get_field
        d = {"a": "x", "b": "y"}
        assert get_field(d, "a", "b") == "x"

    def test_get_field_skips_empty_and_list_and_dict_without_subkey(self):
        from ma_poc.pms.adapters._parsing import get_field
        # None / "" / [] / {} are all skip-worthy.
        d = {"a": None, "b": "", "c": [], "d": {}, "e": "found"}
        assert get_field(d, "a", "b", "c", "d", "e") == "found"

    def test_get_field_unwraps_nested_dict_via_min_low_amount(self):
        from ma_poc.pms.adapters._parsing import get_field
        # Nested dict with 'min' is unwrapped to its scalar.
        d = {"rent": {"min": 1351, "max": 1500}}
        assert get_field(d, "rent") == "1351"
        # Same for 'low'
        d = {"rent": {"low": 1200}}
        assert get_field(d, "rent") == "1200"
        # 'effectiveRent' is also recognised.
        d = {"rent": {"effectiveRent": 1450}}
        assert get_field(d, "rent") == "1450"

    def test_get_field_returns_empty_when_all_keys_missing(self):
        from ma_poc.pms.adapters._parsing import get_field
        assert get_field({}, "a", "b") == ""

    def test_format_rent_range_one_value(self):
        from ma_poc.pms.adapters._parsing import format_rent_range
        # Single value -> no range marker.
        assert format_rent_range(1500, 1500) == "$1,500"
        assert format_rent_range(1500, None) == "$1,500"
        assert format_rent_range(None, 1500) == "$1,500"
        assert format_rent_range(None, None) == ""

    def test_format_rent_range_proper_range(self):
        from ma_poc.pms.adapters._parsing import format_rent_range
        assert format_rent_range(1500, 1700) == "$1,500 - $1,700"

    def test_parse_rent_range_handles_various_forms(self):
        from ma_poc.pms.adapters._parsing import parse_rent_range
        # Single value
        assert parse_rent_range("$1,500") == (1500, 1500)
        # Range with hyphen
        assert parse_rent_range("$1,200 - $1,500") == (1200, 1500)
        # No-dollar form
        assert parse_rent_range("1295-1500") == (1295, 1500)
        # Garbage returns (None, None)
        assert parse_rent_range("") == (None, None)
        assert parse_rent_range("not a price") == (None, None)
        # Type-defensive: non-string -> (None, None)
        assert parse_rent_range(None) == (None, None)  # type: ignore[arg-type]

    def test_parse_rent_range_rejects_out_of_band_numbers(self):
        """Sanity filter: numbers outside $200-$50K are dropped so a
        rent string that accidentally contains a sqft (e.g. "$100 750sqft")
        doesn't pollute the rent slot."""
        from ma_poc.pms.adapters._parsing import parse_rent_range
        # 100 too low, 750 in range -> picked up but ranged
        out = parse_rent_range("$100 - 750")
        # 100 filtered out by sanity (under 200 floor).
        assert out == (750, 750) or out == (None, None)

    def test_rent_in_sanity_range_bounds(self):
        from ma_poc.pms.adapters._parsing import rent_in_sanity_range
        assert rent_in_sanity_range(None) is True   # null ok
        assert rent_in_sanity_range(200) is True
        assert rent_in_sanity_range(50000) is True
        assert rent_in_sanity_range(199) is False
        assert rent_in_sanity_range(50001) is False

    def test_bed_label_from_studio(self):
        from ma_poc.pms.adapters._parsing import bed_label_from
        assert bed_label_from(0) == "Studio"
        assert bed_label_from(None, "Studio Loft") == "Studio"

    def test_bed_label_from_n_bedroom(self):
        from ma_poc.pms.adapters._parsing import bed_label_from
        assert bed_label_from(1) == "1 Bedroom"
        assert bed_label_from(2) == "2 Bedroom"

    def test_bed_label_from_unknown_returns_empty(self):
        from ma_poc.pms.adapters._parsing import bed_label_from
        assert bed_label_from(None) == ""

    def test_money_to_int_returns_none_on_unparseable_numeric(self):
        """The post-regex ``int(float(...))`` raise path."""
        from ma_poc.pms.adapters._parsing import money_to_int
        # No digits at all -> regex finds nothing -> None
        assert money_to_int("no digits") is None
        assert money_to_int("") is None

    def test_compute_floor_plan_id_deterministic(self):
        from ma_poc.pms.adapters._parsing import compute_floor_plan_id
        a = compute_floor_plan_id("P1", "A1", 1, 1.0)
        b = compute_floor_plan_id("P1", "A1", 1, 1.0)
        assert a == b
        # Different inputs -> different hash
        c = compute_floor_plan_id("P1", "B2", 2, 2.0)
        assert a != c

    def test_compute_floor_plan_id_returns_none_without_signal(self):
        """No name and no beds/baths -> can't identify a plan -> None."""
        from ma_poc.pms.adapters._parsing import compute_floor_plan_id
        assert compute_floor_plan_id("P1", None, None, None) is None
        assert compute_floor_plan_id("P1", "", None, None) is None
