"""Tests for the 2026-05-17 Fix 4 change to ``classify``.

When a row carries explicit ``availability_status == AVAILABLE`` plus any
present rent value, it counts as a per-unit signal — the row represents
an available apartment at a specific price even when the LLM/DOM extractor
didn't capture the move-in date. PIDs 20959 dovevalleyapts (6/12 units
lost), 55317 abodes (3/8 lost) regressed pre-fix because their LLM rows
matched this shape and were demoted to ``plan_summaries`` which the v2
formatter dropped (Bug A).
"""

from __future__ import annotations

from ma_poc.extraction.classify import classify


class TestAvailableRentPromotion:
    def test_available_with_rent_low_is_unit(self):
        """AVAILABLE + market_rent_low → unit-level."""
        row = {
            "floor_plan_name": "A1",
            "bedrooms": 1,
            "bathrooms": 1,
            "sqft": 673,
            "market_rent_low": 1759,
            "availability_status": "AVAILABLE",
        }
        assert classify(row) == "unit"

    def test_available_with_rent_high_only_is_unit(self):
        """AVAILABLE + market_rent_high (no low) → unit-level."""
        row = {
            "floor_plan_name": "B2",
            "bedrooms": 2,
            "bathrooms": 2,
            "sqft": 1028,
            "market_rent_high": 1948,
            "availability_status": "AVAILABLE",
        }
        assert classify(row) == "unit"

    def test_available_with_asking_rent_alias_is_unit(self):
        """AVAILABLE + asking_rent (alias for market_rent_low via
        RENT_LO_KEYS) → unit-level."""
        row = {
            "floor_plan_name": "Studio",
            "bedrooms": 0,
            "bathrooms": 1,
            "sqft": 500,
            "asking_rent": 1500,
            "availability_status": "AVAILABLE",
        }
        assert classify(row) == "unit"

    def test_unavailable_with_rent_stays_plan(self):
        """UNAVAILABLE status — even with rent — stays plan-level. We don't
        know which physical unit this represents."""
        row = {
            "floor_plan_name": "B1",
            "bedrooms": 2,
            "bathrooms": 1,
            "sqft": 903,
            "market_rent_low": 1850,
            "availability_status": "UNAVAILABLE",
        }
        assert classify(row) == "plan"

    def test_unknown_status_with_rent_stays_plan(self):
        """UNKNOWN status doesn't promote — rent alone is a plan-level
        signal (rent range) without availability confirmation."""
        row = {
            "floor_plan_name": "C1",
            "bedrooms": 3,
            "bathrooms": 2,
            "sqft": 1220,
            "market_rent_low": 2500,
            "availability_status": "UNKNOWN",
        }
        assert classify(row) == "plan"

    def test_available_status_no_rent_stays_plan(self):
        """AVAILABLE alone — no rent value — stays plan-level. Without
        rent we have no specific price to attribute to a specific unit."""
        row = {
            "floor_plan_name": "A1",
            "bedrooms": 1,
            "bathrooms": 1,
            "sqft": 673,
            "availability_status": "AVAILABLE",
        }
        assert classify(row) == "plan"

    def test_avail_synonym_promotes_with_rent(self):
        """``AVAIL`` is a common abbreviation for AVAILABLE in DOM
        extractions and should promote the same way."""
        row = {
            "floor_plan_name": "A1",
            "bedrooms": 1,
            "sqft": 673,
            "market_rent_low": 1500,
            "availability_status": "AVAIL",
        }
        assert classify(row) == "unit"

    def test_canonical_alias_resolution_for_status(self):
        """``available`` and ``status`` keys (not just
        ``availability_status``) also drive promotion."""
        row_a = {
            "floor_plan_name": "X",
            "beds": 1,
            "market_rent_low": 1400,
            "available": "AVAILABLE",
        }
        row_b = {
            "floor_plan_name": "Y",
            "beds": 1,
            "market_rent_low": 1400,
            "status": "AVAILABLE",
        }
        assert classify(row_a) == "unit"
        assert classify(row_b) == "unit"

    def test_distinct_rents_same_plan_both_promote(self):
        """Two A1 rows at different rents (different lease terms) both
        promote — distinct lease offerings on the same plan. Reproduces
        the PID 20959 dovevalleyapts loss: pre-fix, the $1759 row was
        demoted because it lacked ``available_date`` while the $1604
        row (same dims) was kept because it had a date."""
        row1 = {
            "floor_plan_name": "A1",
            "bedrooms": 1,
            "sqft": 673,
            "market_rent_low": 1604,
            "availability_status": "AVAILABLE",
            "available_date": "2026-06-08",
        }
        row2 = {
            "floor_plan_name": "A1",
            "bedrooms": 1,
            "sqft": 673,
            "market_rent_low": 1759,
            "availability_status": "AVAILABLE",
            "available_date": None,
        }
        assert classify(row1) == "unit"
        assert classify(row2) == "unit"


class TestRegressionGuards:
    """Make sure Fix 4 doesn't break existing classify semantics."""

    def test_pure_plan_aggregate_stays_plan(self):
        """A row that's clearly plan-level (no rent, no per-unit signals)
        stays plan-level. Luxe 88 JSON-LD shape."""
        row = {
            "floor_plan_name": "1-Bedroom",
            "bedrooms": 1,
            "bathrooms": 1,
            "sqft": 700,
        }
        assert classify(row) == "plan"

    def test_real_unit_id_still_wins_over_status(self):
        """A natural unit_id beats every other signal — Path 1 in
        _has_natural_unit_identity is still the strongest indicator."""
        row = {
            "unit_id": "217",
            "bedrooms": 1,
            "availability_status": "UNAVAILABLE",
        }
        assert classify(row) == "unit"
