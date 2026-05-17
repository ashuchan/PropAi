"""D16 test 8 — inverse-B4 plan-code guard.

B4 catches plan rows that duplicate units by ``floor_plan_id``. The
inverse-B4 guard catches the opposite leak: a row in ``units`` whose
``unit_number`` matches a known plan code is almost certainly a plan-
aggregate row that leaked through with a noisy unit_number field.

The guard moves such rows from ``units`` to ``plan_summaries`` and
increments ``inverse_b4_rerouted``.
"""

from __future__ import annotations

from ma_poc.extraction.post_process import post_process


class TestInverseB4Guard:
    def test_unit_number_matching_plan_code_rerouted(self):
        """Row with unit_number="2B4" → plan_summaries when a plan row
        carries floor_plan_id="2B4"."""
        items = [
            # Real unit with availability_date — admitted as unit.
            {
                "unit_number": "618",
                "available_date": "2026-06-01",
                "beds": 1,
                "area": 750,
                "rent_low": 1500,
                "floor_plan_id": "1A2",
            },
            # Plan-aggregate row with floor_plan_id="2B4". No per-unit
            # signals, no unit_number — classified as plan.
            {
                "floor_plan_name": "Plan 2B4",
                "floor_plan_id": "2B4",
                "beds": 2,
                "baths": 2,
                "area": 1100,
                "rent_low": 2000,
            },
            # The suspect row: unit_number="2B4" with availability_date
            # (per-unit signal so it gets through classify as a "unit").
            # Inverse-B4 should re-route it to plan_summaries.
            {
                "unit_number": "2B4",
                "available_date": "2026-06-15",
                "beds": 2,
                "baths": 2,
                "area": 1100,
                "rent_low": 2050,
            },
        ]
        result = post_process(items, property_id="P1")
        # 1 real unit. 2 plan rows (the plan-aggregate + the rerouted).
        assert len(result.units) == 1
        assert len(result.plan_summaries) >= 1
        assert result.inverse_b4_rerouted >= 1
        # The real unit "618" survives in units.
        assert any(
            u.get("unit_number") == "618" for u in result.units
        ), "Real unit '618' should remain in units"

    def test_no_rerouting_when_no_plan_codes(self):
        """Without any plan_summaries, the guard has nothing to compare
        against and does not reroute any units."""
        items = [
            {
                "unit_number": "618",
                "available_date": "2026-06-01",
                "beds": 1,
                "area": 750,
            },
            {
                "unit_number": "619",
                "available_date": "2026-06-02",
                "beds": 1,
                "area": 750,
            },
        ]
        result = post_process(items, property_id="P1")
        assert len(result.units) == 2
        assert result.inverse_b4_rerouted == 0

    def test_unit_number_not_matching_plan_code_kept(self):
        """A real apartment number that doesn't collide with any plan
        code stays in units."""
        items = [
            {
                "floor_plan_name": "Plan A",
                "floor_plan_id": "2B4",
                "beds": 2,
                "area": 1100,
                "rent_low": 2000,
            },
            {
                "unit_number": "618",  # 618 is not a plan code
                "available_date": "2026-06-01",
                "beds": 1,
                "area": 750,
                "rent_low": 1500,
            },
        ]
        result = post_process(items, property_id="P1")
        assert any(u.get("unit_number") == "618" for u in result.units)
        assert result.inverse_b4_rerouted == 0
