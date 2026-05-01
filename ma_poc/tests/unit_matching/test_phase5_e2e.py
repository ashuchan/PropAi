"""
Phase 5: End-to-end integration tests.
These tests simulate a complete multi-day run sequence through the full stack:
  scrape_properties._add()
  → state_store.upsert_units()  (with full pre-strip units)
  → carry_forward_units()
  → strip for output

No Playwright. No network. Scrape results are constructed as dicts.
"""
import pytest
from scrape_properties import transform_units_from_scrape, _add_dedup_key_for_unit
from state_store import StateStore
from identity_fallback import compute_fallback_unit_id
from tests.unit_matching.conftest import (
    CID, D1, D2, D3, D4, store, stable_fallback_id, make_api_unit, make_no_id_unit
)


def scrape_to_state(store, scrape_result, canonical_id, run_date):
    """
    Helper: run the full pipeline for one property on one day.
    Mirrors what daily_runner.py does after Phase 3 fix:
      1. transform
      2. carry-forward if needed
      3. upsert_units (with full units, NOT stripped)
      4. return (unit_diff, public_units_for_output)
    """
    target_units = transform_units_from_scrape(scrape_result)

    # Carry-forward if no units extracted and property is known
    cf_used = False
    if not target_units and store.is_known(canonical_id):
        target_units = store.carry_forward_units(canonical_id, run_date)
        cf_used = True

    # State write: full units (pre-strip)
    unit_diff = store.upsert_units(canonical_id, target_units, run_date)
    store.upsert_property(canonical_id, {
        "canonical_id": canonical_id,
        "last_scrape_status": "SUCCESS" if not cf_used else "FAILED",
        "last_units_count": len(target_units),
    }, run_date)

    # Strip for output (after state write)
    output_units = [{k: v for k, v in u.items() if not k.startswith("_")}
                    for u in target_units]

    return unit_diff, output_units, cf_used


class TestE2EPriceChange:

    def test_e2e_01_unit_matched_across_price_change(self, store):
        """
        Property with 3 units. Between Day 1 and Day 2, unit "201" changes rent.
        Expected: 201 appears in 'updated', not in 'new' or 'disappeared'.
        Proves: Bugs 1+2+3 fixed end-to-end.
        """
        # Pre-register
        store.upsert_property(CID, {"canonical_id": CID}, D1)

        # Day 1 scrape
        sr_d1 = {"_raw_api_responses": [{
            "url": "https://sightmap.com/app/api/v1/leasables",
            "body": {"data": {
                "units": [
                    {"unit_number": "101", "price": 1800, "floor_plan_id": "fp1"},
                    {"unit_number": "201", "price": 2100, "floor_plan_id": "fp2"},
                    {"unit_number": "301", "price": 1650, "floor_plan_id": "fp1"},
                ],
                "floor_plans": [
                    {"id": "fp1", "bedroom_count": 2, "bathroom_count": 1, "area": 950},
                    {"id": "fp2", "bedroom_count": 3, "bathroom_count": 2, "area": 1200},
                ],
            }},
        }], "units": []}
        diff_d1, out_d1, _ = scrape_to_state(store, sr_d1, CID, D1)
        assert set(diff_d1["new"]) == {"101", "201", "301"}

        # Day 2: unit 201 repriced
        sr_d2 = {"_raw_api_responses": [{
            "url": "https://sightmap.com/app/api/v1/leasables",
            "body": {"data": {
                "units": [
                    {"unit_number": "101", "price": 1800, "floor_plan_id": "fp1"},
                    {"unit_number": "201", "price": 2200, "floor_plan_id": "fp2"},  # price up
                    {"unit_number": "301", "price": 1650, "floor_plan_id": "fp1"},
                ],
                "floor_plans": [
                    {"id": "fp1", "bedroom_count": 2, "bathroom_count": 1, "area": 950},
                    {"id": "fp2", "bedroom_count": 3, "bathroom_count": 2, "area": 1200},
                ],
            }},
        }], "units": []}
        diff_d2, out_d2, _ = scrape_to_state(store, sr_d2, CID, D2)

        assert "201" in diff_d2["updated"], "repriced unit must be 'updated'"
        assert diff_d2["new"] == [], "no new units expected"
        assert diff_d2["disappeared"] == [], "no disappeared units expected"
        assert len(out_d2) == 3


class TestE2EPartialScrapeGracePeriod:

    def test_e2e_02_partial_scrape_no_premature_disappear(self, store):
        """
        Property has 10 units. Day 2 scrape returns only 6 (partial failure).
        Within grace period: no units marked disappeared.
        Day 3: still 6 returned. After grace_days=2: 4 units marked disappeared.
        """
        store.upsert_property(CID, {"canonical_id": CID}, D1)

        def make_scrape(unit_ids):
            return {"_raw_api_responses": [{
                "url": "https://sightmap.com/api",
                "body": {"data": {
                    "units": [{"unit_number": uid, "price": 1800, "floor_plan_id": "fp1"}
                               for uid in unit_ids],
                    "floor_plans": [{"id": "fp1", "bedroom_count": 2, "area": 900}],
                }},
            }], "units": []}

        all_ids = [str(i) for i in range(10)]
        partial_ids = [str(i) for i in range(6)]

        # Day 1: all 10
        scrape_to_state(store, make_scrape(all_ids), CID, D1)

        # Day 2: only 6 (within grace)
        diff_d2, _, _ = scrape_to_state(store, make_scrape(partial_ids), CID, D2)
        assert diff_d2["disappeared"] == [], f"Day 2 partial — within grace, got: {diff_d2['disappeared']}"

        # Day 3: still 6 (grace_days=2 → now at streak=2 → disappeared)
        diff_d3, _, _ = scrape_to_state(store, make_scrape(partial_ids), CID, D3)
        disappeared = set(diff_d3["disappeared"])
        assert disappeared == {"6", "7", "8", "9"}, \
            f"After grace_days=2, expected 4 disappeared, got: {disappeared}"


class TestE2ECarryForward:

    def test_e2e_03_cf_fires_on_zero_unit_success(self, store):
        """
        Day 1: successful scrape, 5 units.
        Day 2: scrape returns 0 units (SUCCESS status) — CF must fire.
        Proves: CF condition now includes `not target_units`.
        """
        store.upsert_property(CID, {"canonical_id": CID}, D1)

        sr_d1 = {"_raw_api_responses": [{
            "url": "https://sightmap.com/api",
            "body": {"data": {
                "units": [{"unit_number": str(i), "price": 1800, "floor_plan_id": "fp1"}
                           for i in range(5)],
                "floor_plans": [{"id": "fp1", "bedroom_count": 2, "area": 900}],
            }},
        }], "units": []}
        scrape_to_state(store, sr_d1, CID, D1)

        # Day 2: zero-unit scrape
        sr_d2 = {"_raw_api_responses": [], "units": []}
        diff_d2, out_d2, cf_used = scrape_to_state(store, sr_d2, CID, D2)
        assert cf_used, "CF must fire when scrape returns 0 units"
        assert len(out_d2) == 5, "output must contain 5 CF units"
        assert diff_d2["disappeared"] == []

    def test_e2e_04_cf_units_have_physical_attrs(self, store):
        """
        CF units must carry sqft/floor_plan/bedrooms from the prior snapshot.
        Proves: Phases 2+3 combined — strip moved, attrs stored, attrs emitted in CF.
        """
        store.upsert_property(CID, {"canonical_id": CID}, D1)
        sr = {"_raw_api_responses": [{
            "url": "https://sightmap.com/api",
            "body": {"data": {
                "units": [{"unit_number": "101", "price": 1800, "floor_plan_id": "fp1",
                            "available_on": D1}],
                "floor_plans": [{"id": "fp1", "bedroom_count": 2, "bathroom_count": 1,
                                  "area": 950, "name": "Plan A"}],
            }},
        }], "units": []}
        scrape_to_state(store, sr, CID, D1)

        # Day 2: fails, CF fires
        cf = store.carry_forward_units(CID, D2)
        assert len(cf) == 1
        assert cf[0]["sqft"] == 950, "CF unit must carry sqft"
        assert cf[0]["floor_plan_name"] == "Plan A", "CF unit must carry floor_plan_name"
        assert cf[0]["bedrooms"] == 2, "CF unit must carry bedrooms"
        assert cf[0]["carryforward_days"] == 1

    def test_e2e_05_cold_property_cf_on_first_failure(self, store):
        """
        T13: COLD property pre-registered → immediately eligible for CF.
        Day 1: successful scrape builds state.
        Day 2: fails → CF fires because property was pre-registered before Day 1.
        """
        # Pre-register (the Phase 3 fix)
        store.upsert_property(CID, {"canonical_id": CID}, D1)
        assert store.is_known(CID)

        sr = {"_raw_api_responses": [{
            "url": "https://sightmap.com/api",
            "body": {"data": {
                "units": [{"unit_number": "X1", "price": 2000, "floor_plan_id": "fp1"}],
                "floor_plans": [{"id": "fp1", "bedroom_count": 1, "area": 600}],
            }},
        }], "units": []}
        scrape_to_state(store, sr, CID, D1)

        cf = store.carry_forward_units(CID, D2)
        assert len(cf) == 1, "CF must fire for pre-registered COLD property after first success"


class TestE2EFallbackIdentity:

    def test_e2e_06_no_unit_number_tracked_across_runs(self, store):
        """
        Property where API returns no unit_number — uses inferred_ fallback.
        Same unit across two runs must be matched as 'updated', not 'new'.
        Proves: Phase 4 fix (_add writes fallback id onto record).
        """
        store.upsert_property(CID, {"canonical_id": CID}, D1)

        def make_no_id_scrape(rent):
            return {
                "_raw_api_responses": [],  # no API → entrata fallback
                "units": [{
                    "floor_plan_name": "Studio A",
                    "bedrooms": "0",
                    "bathrooms": "1",
                    "sqft": "500",
                    "rent_range": f"${rent}",
                    "availability_date": D1,
                    # unit_number absent
                }],
            }

        diff_d1, _, _ = scrape_to_state(store, make_no_id_scrape(1200), CID, D1)
        assert len(diff_d1["new"]) == 1
        uid = diff_d1["new"][0]
        assert uid.startswith("inferred_"), f"fallback unit must get inferred_ id, got: {uid}"

        # Day 2: same unit, rent changed
        diff_d2, _, _ = scrape_to_state(store, make_no_id_scrape(1250), CID, D2)
        assert uid in diff_d2["updated"], \
            "same physical unit with rent change must be 'updated', not 'new'"
        assert diff_d2["new"] == []
        assert diff_d2["disappeared"] == []

    def test_e2e_07_two_units_same_plan_different_sqft_are_separate(self, store):
        """
        T9: Property has 2 units on Plan B — one at 900 sqft, one at 1100 sqft.
        Must be tracked as 2 independent units even without unit_number.
        """
        store.upsert_property(CID, {"canonical_id": CID}, D1)
        sr = {
            "_raw_api_responses": [],
            "units": [
                {"floor_plan_name": "Plan B", "bedrooms": "2", "sqft": "900",
                 "rent_range": "$1,800", "availability_date": D1},
                {"floor_plan_name": "Plan B", "bedrooms": "2", "sqft": "1100",
                 "rent_range": "$2,100", "availability_date": D1},
            ],
        }
        diff, _, _ = scrape_to_state(store, sr, CID, D1)
        assert len(diff["new"]) == 2, "two units with different sqft must be 2 separate entries"


class TestE2ESentinelValues:

    def test_e2e_08_area_sentinel_does_not_corrupt_matching(self, store):
        """
        T8: unit with area=-1 must not crash and must not corrupt fuzzy matching.
        """
        store.upsert_property(CID, {"canonical_id": CID}, D1)
        sr = {
            "_raw_api_responses": [],
            "units": [{
                "unit_number": "B01",
                "floor_plan_name": "Plan C",
                "bedrooms": "1",
                "sqft": "-1",   # sentinel
                "rent_range": "$1,500",
                "availability_date": D1,
            }],
        }
        diff, _, _ = scrape_to_state(store, sr, CID, D1)
        assert "B01" in diff["new"]
        snap = store.unit_index[CID]["B01"]
        assert snap["sqft"] is None, "area=-1 must be stored as None"

        # Day 2: same unit, still area=-1
        diff_d2, _, _ = scrape_to_state(store, sr, CID, D2)
        assert "B01" in diff_d2["unchanged"] or "B01" in diff_d2["updated"]
        assert diff_d2["disappeared"] == []


class TestE2EMultiProperty:

    def test_e2e_09_two_properties_state_is_isolated(self, store):
        """
        Units from Property A must never appear in Property B's state.
        """
        CID_A, CID_B = "prop_A", "prop_B"
        store.upsert_property(CID_A, {"canonical_id": CID_A}, D1)
        store.upsert_property(CID_B, {"canonical_id": CID_B}, D1)

        store.upsert_units(CID_A, [make_api_unit("101")], D1)
        store.upsert_units(CID_B, [make_api_unit("201")], D1)

        assert "101" not in store.unit_index.get(CID_B, {})
        assert "201" not in store.unit_index.get(CID_A, {})

    def test_e2e_10_save_reload_full_pipeline_state(self, store):
        """
        Full state (including absent_streak, carryforward_days, disappeared_since)
        must survive a save/load cycle.
        """
        store.upsert_property(CID, {"canonical_id": CID}, D1)
        store.upsert_units(CID, [make_api_unit("101")], D1)
        store.upsert_units(CID, [], D2, disappeared_grace_days=3)  # absent streak=1
        store.save()
        store.load()
        snap = store.unit_index[CID]["101"]
        assert snap["absent_streak"] == 1
        assert snap["disappeared_since"] == D2
        assert snap["sqft"] is not None  # physical attrs survived
