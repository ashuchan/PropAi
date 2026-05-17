"""D16 test 1+3 — partition contract for post_process output.

After D16, ``post_process`` returns a ``PostProcessResult`` whose
``units`` carries ONLY unit-level rows and whose ``plan_summaries``
carries ONLY plan-level rows. No row appears in both. This pins the
strict-disjointness invariant that adapters depend on.
"""

from __future__ import annotations

from ma_poc.extraction.classify import classify
from ma_poc.extraction.post_process import post_process


class TestPartitionDisjointness:
    def test_units_and_plan_summaries_are_disjoint(self):
        """No row appears in both partitions."""
        items = [
            {  # unit-level (natural unit_id)
                "unit_number": "618",
                "available_date": "2026-06-01",
                "beds": 1,
                "area": 750,
                "rent_low": 1500,
            },
            {  # plan-level (no per-apartment identity)
                "floor_plan_name": "Plan B",
                "beds": 2,
                "baths": 2,
                "area": 1100,
                "rent_low": 2000,
            },
        ]
        result = post_process(items, property_id="P1")
        # Build identity sets — id() is fine because post_process emits
        # references, not deep copies.
        unit_ids = {id(u) for u in result.units}
        plan_ids = {id(p) for p in result.plan_summaries}
        assert not (unit_ids & plan_ids), (
            "Partition violated — a row appears in both units and plan_summaries"
        )

    def test_classify_agrees_with_partition(self):
        """For each admitted row, ``classify`` matches the partition."""
        items = [
            {
                "unit_number": "618",
                "available_date": "2026-06-01",
                "beds": 1,
                "area": 750,
            },
            {
                "floor_plan_name": "Plan B",
                "beds": 2,
                "baths": 2,
                "area": 1100,
            },
        ]
        result = post_process(items, property_id="P1")
        plan_codes = frozenset(
            str(p.get("floor_plan_id") or "").strip().lower()
            for p in result.plan_summaries
            if p.get("floor_plan_id")
        )
        for u in result.units:
            assert classify(u, plan_codes=plan_codes) == "unit"
        for p in result.plan_summaries:
            assert classify(p) == "plan"


class TestOlympicByWindsorScenario:
    """Mirrors the D16 motivating case — 15 real units + plan-aggregate
    rows. Strict partition keeps units count at 15; plan-aggregate rows
    end up in plan_summaries only."""

    def test_real_units_and_plan_aggregates_partitioned(self):
        items = []
        # 15 real units with natural unit_numbers.
        for un in (
            "618", "414", "518", "526", "330", "230", "415", "652",
            "252", "354", "244", "444", "419", "634", "348",
        ):
            items.append({
                "unit_number": un,
                "available_date": "2026-06-01",
                "beds": 1,
                "baths": 1,
                "area": 750,
                "rent_low": 1500,
                "floor_plan_id": f"real_plan_{un[:1]}",
            })
        # 14 plan-aggregate rows — no unit_number, no per-unit signal.
        for plan_code in (
            "2B4", "1A7", "1A6", "1A2", "1A3", "1A4", "1A5",
            "2A1", "2A2", "2A3", "2B1", "2B2", "2B3", "3A1",
        ):
            items.append({
                "floor_plan_name": f"Plan {plan_code}",
                "floor_plan_id": plan_code,
                "beds": 2,
                "baths": 2,
                "area": 1100,
                "rent_low": 2000,
            })
        result = post_process(items, property_id="OLYMPIC_BY_WINDSOR")
        # 15 real apartments stay as units. 14 plan rows stay as plans.
        assert len(result.units) == 15, (
            f"Expected 15 unit-level rows, got {len(result.units)} — "
            "plan-aggregate rows leaked into units?"
        )
        # plan_summaries has the 14 plan-aggregate rows (none rerouted
        # because no unit_number matches a plan code).
        assert len(result.plan_summaries) == 14
        assert result.inverse_b4_rerouted == 0


class TestPartitionToMetaPayload:
    def test_to_meta_exposes_dedup_counts(self):
        items = [
            {"unit_number": "618", "available_date": "2026-06-01", "beds": 1, "area": 750},
            {"unit_number": "618", "available_date": "2026-06-01", "beds": 1, "area": 750},
        ]
        result = post_process(items, property_id="P1")
        meta = result.to_meta()
        assert "cross_page_dedup_collapses" in meta
        assert "inverse_b4_rerouted" in meta
        assert "n_unit_level" in meta
        assert "n_plan_level" in meta
        assert meta["cross_page_dedup_collapses"] == 1
