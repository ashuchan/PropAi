"""
Phase 2 tests: state_store.upsert_units rewrite.
All tests must be RED before implementation.
"""
import pytest
from tests.unit_matching.conftest import (
    CID, D1, D2, D3, D4,
    make_api_unit, make_no_id_unit, make_stripped_unit,
    stable_fallback_id, store,
)


class TestUnitIdMatching:

    def test_p2_01_unit_number_matches_across_runs(self, store):
        """T1: stable unit_id present → matched on price change, not minted as new."""
        store.upsert_units(CID, [make_api_unit("101", rent_lo=1800)], D1)
        diff = store.upsert_units(CID, [make_api_unit("101", rent_lo=1850)], D2)
        assert "101" in diff["updated"], "rent change on known unit_id must be 'updated'"
        assert diff["new"] == [], "must not mint new unit on rent change"
        assert diff["disappeared"] == []

    def test_p2_02_fallback_key_stable_across_rent_change(self, store):
        """T2: no unit_id → fallback hash excludes rent → same unit on price change."""
        u1 = make_no_id_unit(rent_lo=1800)
        u2 = make_no_id_unit(rent_lo=1900)
        store.upsert_units(CID, [u1], D1)
        diff = store.upsert_units(CID, [u2], D2)
        assert diff["new"] == [], "rent change must not create new unit (fallback hash is rent-free)"
        assert diff["disappeared"] == []
        total = len(diff["updated"]) + len(diff["unchanged"])
        assert total == 1

    def test_p2_03_fallback_key_stable_across_avail_date_change(self, store):
        """T14: available_date change must not change identity."""
        u1 = make_no_id_unit(avail_date=D1)
        u2 = make_no_id_unit(avail_date=D3)
        store.upsert_units(CID, [u1], D1)
        diff = store.upsert_units(CID, [u2], D2)
        assert diff["new"] == [], "avail_date change must not create new unit"

    def test_p2_04_unit_id_takes_priority_over_fallback(self, store):
        """When unit_id is present, it is used; fallback is not computed."""
        u = make_api_unit("101")
        u["_floor_plan"] = "Plan A"
        u["_bedrooms"] = 2
        store.upsert_units(CID, [u], D1)
        assert "101" in store.unit_index[CID], "unit_id must be the key, not inferred_"

    def test_p2_05_no_id_no_physical_fields_unit_rescued(self, store):
        """Unit with no unit_id and no physical attributes: rescued with unkeyable_ id."""
        u = {"unit_id": "", "market_rent_low": 1800, "available_date": D1}
        diff = store.upsert_units(CID, [u], D1)
        assert len(diff["new"]) == 1, "unit must be rescued, not dropped"
        assert diff["new"][0].startswith("unkeyable_")
        assert diff["synthetic_key_used"] == 1
        assert len(store.unit_index.get(CID, {})) == 1


class TestChangeDetection:

    def test_p2_06_rent_change_detected(self, store):
        """market_rent_low change must appear in updated, changed_fields."""
        store.upsert_units(CID, [make_api_unit("101", rent_lo=1800)], D1)
        diff = store.upsert_units(CID, [make_api_unit("101", rent_lo=1900)], D2)
        assert "101" in diff["updated"]
        assert "market_rent_low" in store.unit_index[CID]["101"]["changed_fields"]

    def test_p2_07_avail_date_change_detected(self, store):
        """available_date change must appear in updated, changed_fields."""
        store.upsert_units(CID, [make_api_unit("101", avail_date=D1)], D1)
        diff = store.upsert_units(CID, [make_api_unit("101", avail_date=D3)], D2)
        assert "101" in diff["updated"]
        assert "available_date" in store.unit_index[CID]["101"]["changed_fields"]

    def test_p2_08_no_change_is_unchanged(self, store):
        """Identical unit across runs must be 'unchanged'."""
        store.upsert_units(CID, [make_api_unit("101")], D1)
        diff = store.upsert_units(CID, [make_api_unit("101")], D2)
        assert "101" in diff["unchanged"]
        assert diff["updated"] == []


class TestPhysicalAttributeStorage:

    def test_p2_09_physical_attrs_stored_from_prefixed_fields(self, store):
        """
        _sqft/_floor_plan/_bedrooms must be stored in snapshot.
        This test proves Phase 3 fix is necessary: stripping BEFORE upsert_units
        would make this test fail.
        """
        u = make_api_unit("101", sqft=950, floor_plan="Plan A", beds=2, baths=1)
        store.upsert_units(CID, [u], D1)
        snap = store.unit_index[CID]["101"]
        assert snap["sqft"] == 950, "sqft must be stored from _sqft"
        assert snap["floor_plan_name"] == "Plan A", "floor_plan_name must be stored from _floor_plan"
        assert snap["bedrooms"] == 2, "bedrooms must be stored from _bedrooms"

    def test_p2_10_stripped_unit_loses_physical_attrs(self, store):
        """
        REGRESSION PROOF: if _ fields are stripped BEFORE upsert_units (current broken
        behaviour), physical attrs are None in snapshot.
        This test must PASS (documenting the current broken state) before Phase 3 is applied.
        After Phase 3, this test remains but the Phase 3 tests prove the fix.
        """
        u = make_stripped_unit("101")  # no _ fields
        store.upsert_units(CID, [u], D1)
        snap = store.unit_index[CID]["101"]
        assert snap["sqft"] is None, "stripped unit has no sqft — this is the bug being fixed"
        assert snap["floor_plan_name"] is None


class TestCarryForwardDays:

    def test_p2_11_carryforward_days_pass_through(self, store):
        """T4: carryforward_days from incoming unit must NOT be reset to 0."""
        store.upsert_units(CID, [make_api_unit("101")], D1)
        store.upsert_property(CID, {"canonical_id": CID}, D1)
        cf = store.carry_forward_units(CID, D2)
        assert cf[0]["carryforward_days"] == 1, "CF units must have carryforward_days=1"
        # Now upsert the CF units — days must survive
        store.upsert_units(CID, cf, D2)
        assert store.unit_index[CID]["101"]["carryforward_days"] == 1, \
            "upsert_units must not reset carryforward_days to 0"

    def test_p2_12_carryforward_days_accumulates(self, store):
        """Three consecutive CF days → carryforward_days=3."""
        store.upsert_units(CID, [make_api_unit("101")], D1)
        store.upsert_property(CID, {"canonical_id": CID}, D1)
        for day in [D2, D3, D4]:
            cf = store.carry_forward_units(CID, day)
            store.upsert_units(CID, cf, day)
        assert store.unit_index[CID]["101"]["carryforward_days"] == 3

    def test_p2_13_fresh_unit_has_zero_carryforward_days(self, store):
        """A freshly scraped unit always has carryforward_days=0."""
        store.upsert_units(CID, [make_api_unit("101")], D1)
        assert store.unit_index[CID]["101"]["carryforward_days"] == 0


class TestGracePeriod:

    def test_p2_14_no_disappear_on_first_miss(self, store):
        """T5: unit absent for 1 run within grace period → NOT disappeared."""
        store.upsert_units(CID, [make_api_unit("101")], D1)
        diff = store.upsert_units(CID, [], D2, disappeared_grace_days=2)
        assert diff["disappeared"] == [], "single miss within grace period must not disappear"

    def test_p2_15_disappear_after_grace_period(self, store):
        """T6: unit absent for grace_days+1 consecutive runs → disappeared."""
        store.upsert_units(CID, [make_api_unit("101")], D1)
        diff_d2 = store.upsert_units(CID, [], D2, disappeared_grace_days=2)
        assert diff_d2["disappeared"] == [], "day 1 of absence — within grace"
        diff_d3 = store.upsert_units(CID, [], D3, disappeared_grace_days=2)
        assert "101" in diff_d3["disappeared"], "day 2 of absence — grace exceeded"

    def test_p2_16_reappearance_resets_streak(self, store):
        """T7: unit reappears → absent_streak=0, disappeared_since=None."""
        store.upsert_units(CID, [make_api_unit("101")], D1)
        store.upsert_units(CID, [], D2, disappeared_grace_days=2)
        diff = store.upsert_units(CID, [make_api_unit("101")], D3)
        snap = store.unit_index[CID]["101"]
        assert snap["absent_streak"] == 0
        assert snap["disappeared_since"] is None
        assert "101" not in diff["disappeared"]

    def test_p2_17_partial_scrape_within_grace(self, store):
        """T5 extended: 60% of units returned → 40% within grace, none disappeared."""
        units = [make_api_unit(str(i)) for i in range(10)]
        store.upsert_units(CID, units, D1)
        partial = [make_api_unit(str(i)) for i in range(6)]
        diff = store.upsert_units(CID, partial, D2, disappeared_grace_days=2)
        assert diff["disappeared"] == [], "partial scrape within grace must not disappear units"
        assert len(diff["unchanged"]) == 6

    def test_p2_18_genuine_new_unit_alongside_stable(self, store):
        """T10: existing 5 units + 1 new → new=1, unchanged=5, disappeared=0."""
        store.upsert_units(CID, [make_api_unit(str(i)) for i in range(5)], D1)
        diff = store.upsert_units(CID, [make_api_unit(str(i)) for i in range(6)], D2)
        assert diff["new"] == ["5"]
        assert len(diff["unchanged"]) == 5
        assert diff["disappeared"] == []


class TestDataQuality:

    def test_p2_19_area_minus_one_stored_as_none(self, store):
        """T8: area=-1 sentinel must be coerced to None in snapshot."""
        u = make_api_unit("301")
        u["_sqft"] = -1
        store.upsert_units(CID, [u], D1)
        assert store.unit_index[CID]["301"]["sqft"] is None, "area=-1 must be stored as None"

    def test_p2_20_different_sqft_buckets_are_separate_keys(self, store):
        """T9: two units same floor plan, different sqft — two entries."""
        u1 = make_no_id_unit(floor_plan="Plan B", sqft=900)
        u2 = make_no_id_unit(floor_plan="Plan B", sqft=1100)
        store.upsert_units(CID, [u1, u2], D1)
        assert len(store.unit_index[CID]) == 2, \
            "different sqft buckets must produce separate fallback keys"

    def test_p2_21_first_seen_date_preserved_on_update(self, store):
        """first_seen_date must not change when a unit is updated."""
        store.upsert_units(CID, [make_api_unit("101")], D1)
        store.upsert_units(CID, [make_api_unit("101", rent_lo=1900)], D2)
        assert store.unit_index[CID]["101"]["first_seen_date"] == D1

    def test_p2_22_disappeared_since_set_on_first_miss(self, store):
        """disappeared_since is set on first miss even within grace period."""
        store.upsert_units(CID, [make_api_unit("101")], D1)
        store.upsert_units(CID, [], D2, disappeared_grace_days=3)
        snap = store.unit_index[CID]["101"]
        assert snap["disappeared_since"] == D2, "disappeared_since tracks first absence"
        assert snap["absent_streak"] == 1

    def test_p2_23_save_and_reload_preserves_state(self, store):
        """State persists across save/load cycle."""
        store.upsert_units(CID, [make_api_unit("101")], D1)
        store.save()
        store.load()
        assert "101" in store.unit_index[CID]
