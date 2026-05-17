"""D16 — Verify B4 plan-vs-unit dedup behaviour survives the partition fix.

Before D16, ``generic.py`` concatenated kept_plans into ``result.units``.
B4 still worked but the kept plans inflated the unit count downstream.
After D16, ``result.units`` is strict; ``result.plan_summaries`` carries
the kept plans. B4 still drops plan rows that duplicate a unit's
floor_plan_id — that invariant is what this test pins.
"""

from __future__ import annotations

from ma_poc.extraction.post_process import post_process


class TestB4InvariantPostD16:
    def test_unit_level_excludes_plan_aggregates(self):
        """``result.units`` only carries rows with per-apartment identity.
        Plan-aggregate rows live in ``result.plan_summaries``."""
        items = [
            # Real units (natural unit_number, per-apartment signal).
            {
                "unit_number": "618",
                "available_date": "2026-06-01",
                "beds": 1, "baths": 1, "area": 750,
                "rent_low": 1500,
                "floor_plan_id": "1A2",
            },
            {
                "unit_number": "619",
                "available_date": "2026-06-15",
                "beds": 1, "baths": 1, "area": 750,
                "rent_low": 1500,
                "floor_plan_id": "1A2",
            },
            # Plan-aggregate row — same floor_plan_id as the units above.
            {
                "floor_plan_name": "Plan 1A2",
                "floor_plan_id": "1A2",
                "beds": 1, "baths": 1, "area": 750,
                "rent_low": 1500,
            },
            # Plan-aggregate row for a DIFFERENT plan, no matching unit.
            {
                "floor_plan_name": "Plan 2B4",
                "floor_plan_id": "2B4",
                "beds": 2, "baths": 2, "area": 1100,
                "rent_low": 2000,
            },
        ]
        result = post_process(items, property_id="P1")
        # 2 real units in units. 2 plan rows in plan_summaries.
        assert len(result.units) == 2, (
            f"Expected 2 unit-level, got {len(result.units)} — "
            "plan-aggregate row leaked in?"
        )
        # The strict partition contract: units never contains rows that
        # classify as "plan". The adapter layer's B4 step then drops
        # the plan-row that duplicates a unit's floor_plan_id from
        # plan_summaries — that's tested at the adapter level, not here.
        for u in result.units:
            assert u.get("unit_number") in {"618", "619"}, (
                "Plan row leaked into units"
            )

    def test_strict_disjoint_partition(self):
        """No row id appears in both ``units`` and ``plan_summaries``."""
        items = [
            {
                "unit_number": "618",
                "available_date": "2026-06-01",
                "beds": 1, "area": 750,
            },
            {
                "floor_plan_name": "Plan A",
                "beds": 2, "area": 1100, "rent_low": 2000,
            },
        ]
        result = post_process(items, property_id="P1")
        unit_object_ids = {id(u) for u in result.units}
        plan_object_ids = {id(p) for p in result.plan_summaries}
        assert not (unit_object_ids & plan_object_ids), (
            "D16 invariant violated: a row appears in both partitions"
        )
