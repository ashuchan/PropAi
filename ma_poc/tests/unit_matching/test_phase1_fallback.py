"""
Phase 1 tests: identity_fallback.py rewrite.
ALL tests in this file must be RED before implementation starts.
Run: pytest tests/unit_matching/test_phase1_fallback.py -v
Expect: ALL FAIL (ImportError or AssertionError).
"""
import hashlib
import pytest
from ma_poc.core.identity import compute_fallback_unit_id, compute_fallback_id
from tests.unit_matching.conftest import stable_fallback_id

CID = "prop_001"


class TestComputeFallbackUnitId:
    """Tests for the v2 compute_fallback_unit_id function."""

    # ── Stable hash — physical attributes only ────────────────────────────────

    def test_p1_01_same_unit_different_rent_same_hash(self):
        """Rent change must NOT change the fallback ID. Core invariant."""
        u1 = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": "950",
              "rent_low": 1800, "available_date": "2026-04-25"}
        u2 = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": "950",
              "rent_low": 1950, "available_date": "2026-04-25"}
        assert compute_fallback_unit_id(u1, CID) == compute_fallback_unit_id(u2, CID), \
            "rent change must not change fallback ID"

    def test_p1_02_same_unit_different_avail_date_same_hash(self):
        """Available date change must NOT change fallback ID — unit re-lists after lease."""
        u1 = {"floor_plan_name": "Plan A", "beds": "1", "baths": "1", "area": "700",
              "rent_low": 1500, "available_date": "2026-04-25"}
        u2 = {"floor_plan_name": "Plan A", "beds": "1", "baths": "1", "area": "700",
              "rent_low": 1500, "available_date": "2026-06-01"}
        assert compute_fallback_unit_id(u1, CID) == compute_fallback_unit_id(u2, CID), \
            "available_date change must not change fallback ID"

    def test_p1_03_different_beds_different_hash(self):
        """Different bedroom count = different physical unit = different hash."""
        u1 = {"floor_plan_name": "Plan A", "beds": "1", "baths": "1", "area": "700"}
        u2 = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": "700"}
        assert compute_fallback_unit_id(u1, CID) != compute_fallback_unit_id(u2, CID)

    def test_p1_04_different_sqft_different_hash(self):
        """Different sqft = different physical unit."""
        u1 = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": "900"}
        u2 = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": "1100"}
        assert compute_fallback_unit_id(u1, CID) != compute_fallback_unit_id(u2, CID)

    def test_p1_05_sqft_rounded_to_10_absorbs_noise(self):
        """sqft 948 and 952 are the same bucket (both round to 950)."""
        u1 = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": "948"}
        u2 = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": "952"}
        assert compute_fallback_unit_id(u1, CID) == compute_fallback_unit_id(u2, CID), \
            "sqft within 10-unit bucket must produce same hash"

    def test_p1_06_sqft_different_buckets_different_hash(self):
        """sqft 940 and 960 are different buckets."""
        u1 = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": "940"}
        u2 = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": "960"}
        assert compute_fallback_unit_id(u1, CID) != compute_fallback_unit_id(u2, CID)

    def test_p1_07_area_minus_one_treated_as_missing(self):
        """area=-1 sentinel must be treated as None — not hashed as -1."""
        u1 = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": -1}
        u2 = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": None}
        # Both should produce the same hash (area absent in both cases)
        assert compute_fallback_unit_id(u1, CID) == compute_fallback_unit_id(u2, CID), \
            "area=-1 sentinel must hash identically to area=None"

    def test_p1_08_returns_none_when_floor_plan_missing(self):
        """Without floor_plan, fewer than 2 identifying fields — return None."""
        u = {"beds": "2", "baths": "1", "area": "950"}
        assert compute_fallback_unit_id(u, CID) is None

    def test_p1_09_returns_none_when_only_one_field(self):
        """floor_plan alone (no beds/baths/area) — return None."""
        u = {"floor_plan_name": "Plan A"}
        assert compute_fallback_unit_id(u, CID) is None

    def test_p1_10_returns_inferred_prefix(self):
        """Result must start with 'inferred_'."""
        u = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": "950"}
        result = compute_fallback_unit_id(u, CID)
        assert result is not None
        assert result.startswith("inferred_"), f"Expected inferred_ prefix, got: {result}"

    def test_p1_11_result_is_16_char_hash_plus_prefix(self):
        """Hash portion must be 16 hex chars."""
        u = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": "950"}
        result = compute_fallback_unit_id(u, CID)
        assert len(result) == len("inferred_") + 16, f"Unexpected length: {result}"

    def test_p1_12_deterministic_across_calls(self):
        """Same inputs always produce same output."""
        u = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": "950"}
        assert compute_fallback_unit_id(u, CID) == compute_fallback_unit_id(u, CID)

    def test_p1_13_different_property_different_hash(self):
        """Same unit on different property must produce different hash."""
        u = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": "950"}
        assert compute_fallback_unit_id(u, "prop_001") != compute_fallback_unit_id(u, "prop_002")

    def test_p1_14_accepts_underscore_prefixed_field_aliases(self):
        """
        Must accept _floor_plan, _bedrooms, _bathrooms, _sqft
        (the names present BEFORE the daily_runner _ strip).
        This is critical: _add() in scrape_properties calls this BEFORE stripping.
        """
        u_prefixed  = {"_floor_plan": "Plan A", "_bedrooms": "2", "_bathrooms": "1", "_sqft": "950"}
        u_canonical = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": "950"}
        result_prefixed  = compute_fallback_unit_id(u_prefixed, CID)
        result_canonical = compute_fallback_unit_id(u_canonical, CID)
        assert result_prefixed is not None, "Must not return None for _-prefixed fields"
        assert result_prefixed == result_canonical, \
            "Prefixed and canonical field names must produce identical hash"

    def test_p1_15_floor_plan_name_case_insensitive(self):
        """'Plan A' and 'plan a' must produce same hash."""
        u1 = {"floor_plan_name": "Plan A", "beds": "2", "area": "950"}
        u2 = {"floor_plan_name": "PLAN A", "beds": "2", "area": "950"}
        assert compute_fallback_unit_id(u1, CID) == compute_fallback_unit_id(u2, CID)

    def test_p1_16_floor_plan_whitespace_normalised(self):
        """'Plan  A' (double space) and 'Plan A' must produce same hash."""
        u1 = {"floor_plan_name": "Plan  A", "beds": "2", "area": "950"}
        u2 = {"floor_plan_name": "Plan A",  "beds": "2", "area": "950"}
        assert compute_fallback_unit_id(u1, CID) == compute_fallback_unit_id(u2, CID)

    def test_p1_17_matches_conftest_stable_fallback_id_helper(self):
        """
        Result must match the conftest.stable_fallback_id() helper.
        This validates that test T2 in later phases uses the correct expected value.
        """
        u = {"floor_plan_name": "Plan A", "beds": "2", "baths": "1", "area": "950"}
        expected = stable_fallback_id(CID, "Plan A", "2", "1", "950")
        assert compute_fallback_unit_id(u, CID) == expected


class TestComputeFallbackId:
    """Tests for the legacy v1 compute_fallback_id function (field name fixes only)."""

    def test_p1_18_v1_returns_none_without_floor_plan(self):
        u = {"bedrooms": "2", "sqft": "950", "asking_rent": "1800"}
        assert compute_fallback_id(u) is None

    def test_p1_19_v1_returns_none_without_bedrooms(self):
        u = {"floor_plan_type": "2BR", "sqft": "950", "asking_rent": "1800"}
        assert compute_fallback_id(u) is None

    def test_p1_20_v1_inferred_prefix(self):
        u = {"floor_plan_type": "2BR", "bedrooms": "2", "sqft": "950", "asking_rent": "1800"}
        result = compute_fallback_id(u)
        assert result is not None and result.startswith("inferred_")

    def test_p1_21_v1_rent_still_in_hash_documented(self):
        """
        v1 still includes rent (kept for backward compat with any existing inferred_ IDs
        in the state file). This test DOCUMENTS the known limitation — do not fix v1.
        The platform must migrate to compute_fallback_unit_id (v2).
        """
        u1 = {"floor_plan_type": "2BR", "bedrooms": "2", "sqft": "950", "asking_rent": "1800"}
        u2 = {"floor_plan_type": "2BR", "bedrooms": "2", "sqft": "950", "asking_rent": "2000"}
        # v1 includes rent rounded to nearest 25 — different buckets = different hash
        # 1800 rounds to 1800, 2000 rounds to 2000 — different
        result1 = compute_fallback_id(u1)
        result2 = compute_fallback_id(u2)
        assert result1 != result2, \
            "DOCUMENTED: v1 is rent-volatile. Do not fix — migrate to v2."
