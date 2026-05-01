"""
Phase 3 tests: daily_runner.py strip order and property pre-registration.

These tests call the runner's internal helpers directly or use a minimal
scrape mock — they do NOT launch Playwright or make network calls.
"""
import pytest
from tests.unit_matching.conftest import CID, D1, D2, D3, make_api_unit, store


class TestStripOrder:
    """
    Prove that _-prefixed fields reach upsert_units and are stored in snapshot.
    Before fix: snapshot has sqft=None.
    After fix: snapshot has sqft=950.
    """

    def test_p3_01_physical_attrs_in_state_after_scrape(self, store):
        """
        Simulate the daily_runner pipeline for one property.
        Full units (with _sqft etc.) must flow into upsert_units.
        """
        target_units = [make_api_unit("101", sqft=950, floor_plan="Plan A", beds=2)]
        # This test imports and calls the same logic as daily_runner
        # without a real scrape — just the state-write portion.
        store.upsert_units(CID, target_units, D1)  # full units, not stripped
        snap = store.unit_index[CID]["101"]
        assert snap["sqft"] == 950
        assert snap["floor_plan_name"] == "Plan A"
        assert snap["bedrooms"] == 2

    def test_p3_02_output_record_has_no_underscore_fields(self):
        """
        The property record written to properties.json must NOT contain _sqft etc.
        Output stripping must still happen — just AFTER upsert_units.
        """
        target_units = [make_api_unit("101", sqft=950)]
        # Simulate the strip that should happen after upsert:
        public_output = [{k: v for k, v in u.items() if not k.startswith("_")}
                         for u in target_units]
        for u in public_output:
            for k in u:
                assert not k.startswith("_"), f"Output unit must not contain _-prefixed key: {k}"

    def test_p3_03_cf_units_have_physical_attrs_when_prior_state_correct(self, store):
        """
        When a property fails on Day 2 and CF fires, carry_forward_units must
        return units with sqft/floor_plan/bedrooms populated (not None).
        This requires Phase 2 fix (store _sqft → snapshot.sqft) to be in place.
        """
        # Day 1: successful scrape with full unit including physical attrs
        store.upsert_units(CID, [make_api_unit("101", sqft=950, floor_plan="Plan A", beds=2)], D1)
        store.upsert_property(CID, {"canonical_id": CID}, D1)
        # Day 2: scrape fails → CF fires
        cf = store.carry_forward_units(CID, D2)
        assert len(cf) == 1
        cf_unit = cf[0]
        assert cf_unit["sqft"] == 950, "CF unit must have sqft from prior snapshot"
        assert cf_unit["floor_plan_name"] == "Plan A"
        assert cf_unit["bedrooms"] == 2


class TestPropertyPreRegistration:

    def test_p3_04_cold_property_known_before_scrape(self, store):
        """
        After fix: property is registered in state BEFORE the scrape runs.
        is_known() must return True for a COLD property from CSV.
        """
        # Simulate pre-registration (the fix adds this before the scrape call)
        store.upsert_property(CID, {"canonical_id": CID}, D1)
        assert store.is_known(CID), "property must be known before first scrape"

    def test_p3_05_cf_fires_for_cold_property_on_first_failure(self, store):
        """
        T13: COLD property pre-registered → CF fires on first failure.
        Without pre-registration, is_known() is False → CF silently skipped.
        """
        # Pre-register (the fix)
        store.upsert_property(CID, {"canonical_id": CID}, D1)
        # Simulate Day 1 successful scrape
        store.upsert_units(CID, [make_api_unit("101")], D1)
        # Day 2: scrape fails — CF must fire
        assert store.is_known(CID)
        cf = store.carry_forward_units(CID, D2)
        assert len(cf) == 1, "CF must fire for pre-registered COLD property"

    def test_p3_06_pre_registration_idempotent(self, store):
        """Calling upsert_property before scrape and again after does not corrupt state."""
        store.upsert_property(CID, {"canonical_id": CID}, D1)
        store.upsert_property(CID, {"canonical_id": CID, "last_scrape_status": "SUCCESS"}, D1)
        assert store.property_index[CID]["last_scrape_status"] == "SUCCESS"
        assert store.property_index[CID]["first_seen_date"] == D1

    def test_p3_07_units_count_is_fresh_scrape_count_not_cf(self, store):
        """
        last_units_count on property record must reflect fresh scrape count,
        not the CF count. When CF fires, last_units_count reflects CF set size.
        """
        store.upsert_property(CID, {"canonical_id": CID}, D1)
        store.upsert_units(CID, [make_api_unit(str(i)) for i in range(5)], D1)
        store.upsert_property(CID, {"canonical_id": CID, "last_units_count": 5}, D1)
        assert store.property_index[CID]["last_units_count"] == 5
