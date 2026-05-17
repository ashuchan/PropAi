"""D16 test 11 — ordering invariant.

The post_process pipeline runs:
  partition (classify) → cross-page unit-number dedup → inverse-B4 guard

Reversing dedup and inverse-B4 lets plan-code-shaped unit_numbers
pollute the dedup buckets and over-collapse units. This test pins the
ordering by constructing inputs where reordering would produce a
different output.
"""

from __future__ import annotations

from ma_poc.extraction.post_process import post_process


class TestOrderingInvariant:
    def test_dedup_runs_before_inverse_b4(self):
        """If inverse-B4 ran before dedup, a plan-code-shaped unit_number
        would be rerouted BEFORE we had a chance to merge it with its
        cross-page duplicate. Verify dedup happens first by checking
        that ``cross_page_dedup_collapses > 0`` for two same-unit_number
        rows, regardless of whether inverse-B4 later reroutes them."""
        items = [
            # Two rows that share unit_number — must be deduped FIRST.
            {
                "unit_number": "618",
                "floor_plan_name": "1A2",
                "beds": 1,
                "area": 750,
                "rent_low": 1500,
            },
            {
                "unit_number": "618",
                "floor_plan_name": "1A2",
                "beds": 1,
                "area": 750,
                "available_date": "2026-06-01",
            },
        ]
        result = post_process(items, property_id="P1")
        assert result.cross_page_dedup_collapses == 1, (
            "Dedup must run before inverse-B4 — two same-unit_number rows "
            "should fold to one."
        )

    def test_partition_runs_before_dedup(self):
        """Plan rows must not enter the dedup buckets. If partition ran
        AFTER dedup, plan rows sharing a unit_number with a unit row
        would incorrectly merge."""
        items = [
            # Real unit "618"
            {
                "unit_number": "618",
                "available_date": "2026-06-01",
                "beds": 1,
                "area": 750,
                "rent_low": 1500,
            },
            # Plan row (no per-apartment identity) — goes to plan_summaries,
            # NOT into the unit-number dedup buckets.
            {
                "floor_plan_name": "Plan B",
                "beds": 2,
                "area": 1100,
                "rent_low": 2000,
            },
        ]
        result = post_process(items, property_id="P1")
        # Two distinct rows — one unit, one plan. Dedup did NOT fold them
        # together (they're in different partitions).
        assert len(result.units) == 1
        assert len(result.plan_summaries) == 1
        assert result.cross_page_dedup_collapses == 0


class TestB4AdapterLayerPreserved:
    """B4 (in the adapter) drops plan rows that duplicate units by
    ``floor_plan_id``. That behaviour must survive D16. This test verifies
    that the post_process partition produces inputs that the adapter's
    B4 step can still consume."""

    def test_post_process_emits_plans_with_floor_plan_id(self):
        items = [
            {
                "unit_number": "618",
                "available_date": "2026-06-01",
                "beds": 1,
                "area": 750,
                "floor_plan_id": "PLAN_A",
            },
            {
                "floor_plan_name": "Plan A",
                "floor_plan_id": "PLAN_A",  # duplicates the unit's plan
                "beds": 1,
                "area": 750,
            },
        ]
        result = post_process(items, property_id="P1")
        # Both rows admitted; plan-aggregate row is in plan_summaries
        # with its floor_plan_id intact so the adapter's B4 step can
        # match and drop it.
        assert len(result.units) == 1
        assert len(result.plan_summaries) == 1
        assert result.plan_summaries[0].get("floor_plan_id") == "PLAN_A"
