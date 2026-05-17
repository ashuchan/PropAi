"""D16 — post_process invariants beyond the per-feature tests.

This file pins behaviours that span features and edge cases worth
documenting explicitly:

  * Idempotency: a second call on the output partition produces the
    same partition and zero new collapses.
  * Chicken-and-egg edge: a single row whose ``unit_number`` matches
    its own ``floor_plan_id`` (with no sibling plan-aggregate row) is
    admitted as a unit — the two-pass classify cannot detect this in
    isolation. Documented as expected behaviour.
"""

from __future__ import annotations

from ma_poc.extraction.post_process import post_process


class TestPostProcessIdempotency:
    def test_second_call_produces_same_partition(self):
        """``post_process`` is idempotent — running on its own output
        does not change the partition."""
        items = [
            {  # natural unit
                "unit_number": "618",
                "available_date": "2026-06-01",
                "beds": 1,
                "area": 750,
                "rent_low": 1500,
                "floor_plan_id": "1A2",
            },
            {  # plan-aggregate
                "floor_plan_name": "Plan 2B4",
                "floor_plan_id": "2B4",
                "beds": 2,
                "area": 1100,
                "rent_low": 2000,
            },
        ]
        first = post_process(items, property_id="P1")
        # Re-feed the admitted rows. Use ``admitted`` so units + plans
        # both go through again.
        replayed_input = list(first.units) + list(first.plan_summaries)
        second = post_process(replayed_input, property_id="P1")

        assert second.n_unit_level == first.n_unit_level
        assert second.n_plan_level == first.n_plan_level

    def test_second_call_finds_no_new_collapses(self):
        """Cross-page dedup is idempotent: the first call merged
        duplicates so the second call finds none."""
        items = [
            {"unit_number": "618", "floor_plan_name": "1A2", "beds": 1, "area": 750},
            {"unit_number": "618", "floor_plan_name": "2B4", "beds": 1, "area": 750},
            {"unit_number": "619", "floor_plan_name": "1A2", "beds": 1, "area": 750},
        ]
        first = post_process(items, property_id="P1")
        assert first.cross_page_dedup_collapses == 1

        replayed = list(first.units) + list(first.plan_summaries)
        second = post_process(replayed, property_id="P1")
        assert second.cross_page_dedup_collapses == 0, (
            "Second pass should find no duplicates — the first pass merged them"
        )

    def test_inverse_b4_partition_outcome_is_stable(self):
        """Inverse-B4 produces a stable OUTCOME (the row lands in the
        same partition on every pass), even though the internal "work
        counter" is non-zero on each pass.

        Mechanism: a suspect row with a plan-code-shaped unit_number
        AND a per-unit signal (e.g. availability_date) is classified
        as "unit" via the per-unit-signal rule, then inverse-B4
        reroutes it to plan_summaries. On a replay, the same row is
        re-classified as "unit" again (per-unit-signal still fires)
        and rerouted again. The user-visible partition is stable;
        only the counter increments.

        Cross-page dedup, by contrast, IS counter-idempotent — the
        first call merged duplicates so the second finds none. The
        two passes have different idempotency semantics by design.
        """
        items = [
            {
                "unit_number": "618",
                "available_date": "2026-06-01",
                "beds": 1,
                "area": 750,
                "rent_low": 1500,
                "floor_plan_id": "1A2",
            },
            {
                "floor_plan_name": "Plan 2B4",
                "floor_plan_id": "2B4",
                "beds": 2,
                "area": 1100,
                "rent_low": 2000,
            },
            {
                "unit_number": "2B4",
                "available_date": "2026-06-15",
                "beds": 2,
                "area": 1100,
                "rent_low": 2050,
            },
        ]
        first = post_process(items, property_id="P1")
        assert first.inverse_b4_rerouted >= 1

        replayed = list(first.units) + list(first.plan_summaries)
        second = post_process(replayed, property_id="P1")

        # Outcome-idempotency: same partition shape, same membership.
        assert second.n_unit_level == first.n_unit_level
        assert second.n_plan_level == first.n_plan_level


class TestTwoPassClassifyEdgeCases:
    """Document the two-pass classify chicken-and-egg edge.

    Pass 1 classifies each row with no ``plan_codes`` context. A row
    with only ``unit_number`` is promoted (Path 2) and lands in the
    "units" partition. ``plan_codes`` is collected from Pass-1 plans
    only — so a row promoted via unit_number does NOT contribute its
    ``floor_plan_id`` to plan_codes.

    Pass 2 then re-classifies with the discovered plan_codes. The
    inverse-B4 guard catches plan-code-shaped unit_numbers only when
    a sibling plan-aggregate row carries that floor_plan_id.
    """

    def test_isolated_suspect_row_admitted_as_unit(self):
        """A single row with ``unit_number=floor_plan_id`` and no
        sibling plan-aggregate row is admitted as a unit. This is the
        documented limitation — the system cannot disambiguate without
        external evidence."""
        items = [
            {
                "unit_number": "2B4",
                "floor_plan_id": "2B4",
                "available_date": "2026-06-15",
                "beds": 2,
                "area": 1100,
                "rent_low": 2050,
            },
        ]
        result = post_process(items, property_id="P1")
        # The row is admitted as a unit because no sibling plan row
        # carries floor_plan_id="2B4" for the guard to match against.
        assert len(result.units) == 1
        assert result.inverse_b4_rerouted == 0

    def test_sibling_plan_aggregate_disambiguates(self):
        """With a sibling plan-aggregate row carrying the matching
        floor_plan_id, the inverse-B4 guard reroutes the suspect row."""
        items = [
            # Plan-aggregate row carrying floor_plan_id="2B4"
            {
                "floor_plan_name": "Plan 2B4",
                "floor_plan_id": "2B4",
                "beds": 2,
                "area": 1100,
                "rent_low": 2000,
            },
            # Suspect row — same shape as the isolated test above
            {
                "unit_number": "2B4",
                "floor_plan_id": "2B4",
                "available_date": "2026-06-15",
                "beds": 2,
                "area": 1100,
                "rent_low": 2050,
            },
        ]
        result = post_process(items, property_id="P1")
        assert result.inverse_b4_rerouted >= 1
        # The suspect row was rerouted; the plan-aggregate row stays.
        # Both end up in plan_summaries.
        assert len(result.plan_summaries) == 2

    def test_two_units_share_plan_code_neither_rerouted(self):
        """Two real units that happen to share a plan code without any
        plan-aggregate row stay as units (no false reroute)."""
        items = [
            {
                "unit_number": "618",
                "floor_plan_id": "1A2",
                "available_date": "2026-06-01",
                "beds": 1,
                "area": 750,
                "rent_low": 1500,
            },
            {
                "unit_number": "619",
                "floor_plan_id": "1A2",
                "available_date": "2026-06-15",
                "beds": 1,
                "area": 750,
                "rent_low": 1500,
            },
        ]
        result = post_process(items, property_id="P1")
        assert len(result.units) == 2
        assert result.inverse_b4_rerouted == 0
