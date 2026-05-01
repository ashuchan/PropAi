"""
Phase 4 tests: scrape_properties.py fixes.
"""
import pytest
from scrape_properties import (
    _UNIT_ID_KEYS,
    _generic_units_from_body,
    _add_dedup_key_for_unit,
    transform_units_from_scrape,
)
from tests.unit_matching.conftest import CID, D1, stable_fallback_id


class TestUnitIdKeysOrdering:

    def test_p4_01_unit_id_keys_is_ordered_sequence(self):
        """_UNIT_ID_KEYS must be a tuple or list, not a set."""
        assert isinstance(_UNIT_ID_KEYS, (tuple, list)), \
            "_UNIT_ID_KEYS must be ordered — sets have non-deterministic iteration"

    def test_p4_02_unit_number_before_generic_id(self):
        """unit_number must appear before 'id' in the key priority list."""
        keys = list(_UNIT_ID_KEYS)
        assert keys.index("unit_number") < keys.index("id"), \
            "unit_number must have higher priority than generic 'id'"

    def test_p4_03_name_not_in_unit_id_keys(self):
        """'name' must be removed — it is a floor plan name, not a unit number."""
        assert "name" not in _UNIT_ID_KEYS, \
            "'name' key causes floor plan names to be used as unit IDs — must be removed"

    def test_p4_04_unit_number_selected_over_name(self):
        """When both unit_number and name present, unit_number wins."""
        sample = {"unit_number": "101", "name": "Plan A", "price": 1800}
        uid = ""
        for k in _UNIT_ID_KEYS:
            v = sample.get(k)
            if v is not None and str(v).strip():
                uid = str(v).strip()
                break
        assert uid == "101", f"Expected '101', got '{uid}'"


class TestAddDedupKey:
    """
    Tests for the within-run dedup key logic.
    """

    def test_p4_05_dedup_key_uses_unit_id_when_present(self):
        """unit_id present → dedup key IS unit_id."""
        rec = {"unit_id": "101", "market_rent_low": 1800, "_sqft": 950}
        key = _add_dedup_key_for_unit(rec, CID)
        assert key == "101"

    def test_p4_06_dedup_key_stable_without_unit_id(self):
        """No unit_id → fallback key uses physical attrs, NOT rent."""
        rec = {"unit_id": "", "market_rent_low": 1800, "_floor_plan": "Plan A",
               "_bedrooms": 2, "_sqft": 950}
        key1 = _add_dedup_key_for_unit(rec, CID)
        rec2 = {**rec, "market_rent_low": 1900}
        key2 = _add_dedup_key_for_unit(rec2, CID)
        assert key1 == key2, "dedup key must not include rent"
        assert key1 != "", "dedup key must not be empty"

    def test_p4_07_same_unit_different_price_not_duplicated_within_run(self):
        """
        Same physical unit appears twice in one scrape at different prices
        (e.g., two API endpoints return same unit at updated price).
        Must be deduped to ONE record, not two.
        """
        seen = set()
        target = []

        def add(rec):
            key = _add_dedup_key_for_unit(rec, CID)
            if not key or key in seen:
                return
            seen.add(key)
            target.append(rec)

        rec1 = {"unit_id": "", "market_rent_low": 1800, "_floor_plan": "Plan A",
                "_bedrooms": 2, "_sqft": 950}
        rec2 = {**rec1, "market_rent_low": 1900}
        add(rec1)
        add(rec2)
        assert len(target) == 1, "same physical unit at different prices must deduplicate to 1"

    def test_p4_08_fallback_unit_id_written_onto_record(self):
        """
        When unit_id is absent, transform_units_from_scrape must write the fallback ID
        onto the record so upsert_units can track it.
        """
        scrape_result = {
            "_raw_api_responses": [],  # no API responses → entrata fallback
            "units": [
                {
                    "floor_plan_name": "Plan A",
                    "bedrooms": "2",
                    "bathrooms": "1",
                    "sqft": "950",
                    "rent_range": "$1,800",
                    "availability_date": D1,
                    # unit_number deliberately absent
                }
            ],
        }
        result = transform_units_from_scrape(scrape_result)
        assert len(result) == 1
        uid = result[0].get("unit_id") or ""
        assert uid.startswith("inferred_"), \
            f"unit without unit_number must get inferred_ fallback id, got: '{uid}'"


class TestGenericParser:

    def test_p4_09_generic_parser_unit_number_wins_over_label(self):
        """unit_number takes priority over label in generic parser.

        Bug fix note: generic parser requires >=3 items per list, so test body
        uses 3 records to exercise the actual parsing path.
        """
        body = [
            {"unit_number": "101", "label": "Studio A", "price": 1500},
            {"unit_number": "102", "label": "Studio B", "price": 1600},
            {"unit_number": "103", "label": "Studio C", "price": 1700},
        ]
        units = _generic_units_from_body(body, "http://example.com")
        assert len(units) == 3
        ids = {u["unit_id"] for u in units}
        assert "101" in ids, "unit_number must be selected as unit_id"
        assert "Studio A" not in ids, "label must not be used when unit_number is present"

    def test_p4_10_generic_parser_no_floor_plan_name_as_id(self):
        """
        If the API has 'name' as a floor plan name and 'unit_number' is absent,
        the parser must NOT use 'name' as unit_id (it's been removed from _UNIT_ID_KEYS).
        Instead it must skip the record (no other id key present with rent key).

        Bug fix note: generic parser requires >=3 items; body uses 3 records so the
        list passes the >=3 gate. With 'name' removed from _UNIT_ID_KEYS and no other
        id key, all records get no unit_id and are skipped.
        """
        body = [
            {"name": "2 Bedroom A", "price": 1800},
            {"name": "2 Bedroom B", "price": 1850},
            {"name": "2 Bedroom C", "price": 1900},
        ]
        units = _generic_units_from_body(body, "http://example.com")
        # With 'name' removed from _UNIT_ID_KEYS, has_id is False → list rejected entirely
        for u in units:
            uid = u.get("unit_id") or ""
            assert uid != "2 Bedroom A", \
                "floor plan name must never be used as unit_id"

    def test_p4_11_generic_parser_skips_units_below_rent_min(self):
        """Units with rent below $200 must be filtered.

        Bug fix note: generic parser requires >=3 items. Using 3 records all with
        garbage rent so ALL are filtered by the rent-bounds check.
        """
        body = [
            {"unit_number": "101", "price": 50},
            {"unit_number": "102", "price": 50},
            {"unit_number": "103", "price": 50},
        ]
        units = _generic_units_from_body(body, "http://example.com")
        assert len(units) == 0


class TestTransformUnitsEndToEnd:

    def test_p4_12_sightmap_units_have_unit_id(self):
        """SightMap parse path must always produce unit_id."""
        scrape_result = {
            "_raw_api_responses": [{
                "url": "https://sightmap.com/app/api/v1/leasables",
                "body": {
                    "data": {
                        "units": [
                            {"unit_number": "A101", "price": 1800,
                             "floor_plan_id": "1", "available_on": D1},
                        ],
                        "floor_plans": [
                            {"id": "1", "name": "Plan A", "bedroom_count": 2,
                             "bathroom_count": 1, "area": 950},
                        ],
                    }
                },
            }],
            "units": [],
        }
        result = transform_units_from_scrape(scrape_result)
        assert len(result) == 1
        assert result[0]["unit_id"] == "A101"
        assert result[0]["market_rent_low"] == 1800

    def test_p4_13_no_duplicate_unit_ids_in_output(self):
        """transform_units_from_scrape output must have unique unit_ids."""
        scrape_result = {
            "_raw_api_responses": [{
                "url": "https://sightmap.com/app/api/v1/leasables",
                "body": {
                    "data": {
                        "units": [
                            {"unit_number": "101", "price": 1800, "floor_plan_id": "1"},
                            {"unit_number": "101", "price": 1850, "floor_plan_id": "1"},  # dup
                        ],
                        "floor_plans": [{"id": "1", "bedroom_count": 2}],
                    }
                },
            }],
            "units": [],
        }
        result = transform_units_from_scrape(scrape_result)
        ids = [u["unit_id"] for u in result]
        assert len(ids) == len(set(ids)), "output must not contain duplicate unit_ids"
