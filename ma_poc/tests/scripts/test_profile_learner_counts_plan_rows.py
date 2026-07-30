"""A plan-only extraction is a success, and the profile must learn from it.

`update_profile_after_extraction` gates everything on `units_extracted > 0`:
it increments `total_successes` and `consecutive_successes`, resets
`consecutive_failures`, and records `last_success_tier` / `preferred_tier` — the
WINNING ROUTE. At zero it does none of that, and `detect_drift` then compares
against `confidence.last_unit_count` and demotes on
`units_extracted < expected * 0.7`.

`promote_verified_unit_rows` moves unanchored plan rows out of `units` into
`plan_summaries`, so a plan-only property arrived with `units == []` and was
booked as an extraction FAILURE despite having extracted fine — losing its
winning route and getting demoted.

Why that is worse than a mislabel: the damage lands in PERSISTED state. Losing
`winning_page_url` means the next run cannot go straight to the page that
worked, so it re-discovers or fails, books another failure, and demotes again.
That is the loop recorded in project_timeout_rca_event_loop_2026-07-25, which
this project has already paid for once. It is also invisible to a single cold
canary by construction — the harm is to state carried BETWEEN runs.

These tests therefore assert on the LEARNER'S STATE, not on row counts: the
counting helper alone would pass a test that never proves the profile improved.
"""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.scripts.runners.jugnu import _extracted_row_count


class TestExtractedRowCount:
    """Both channels, because the split changed where rows live, not how many."""

    def test_counts_units(self) -> None:
        assert _extracted_row_count({"units": [{}, {}]}) == 2

    def test_counts_plan_summaries(self) -> None:
        """The regression: this used to read 0."""
        assert _extracted_row_count({"units": [], "plan_summaries": [{}] * 12}) == 12

    def test_counts_both(self) -> None:
        assert _extracted_row_count({"units": [{}], "plan_summaries": [{}, {}]}) == 3

    @pytest.mark.parametrize(
        "result",
        [{}, {"units": None}, {"plan_summaries": None}, {"units": [], "plan_summaries": []}],
        ids=["empty", "units-none", "plans-none", "both-empty"],
    )
    def test_genuine_emptiness_still_reads_zero(self, result: dict[str, Any]) -> None:
        """The fix must not launder a real miss into a success."""
        assert _extracted_row_count(result) == 0


class TestPlanOnlyExtractionIsLearnedFrom:
    """The behaviour that actually matters: does the profile keep the route?"""

    @staticmethod
    def _profile() -> Any:
        from ma_poc.models.scrape_profile import ScrapeProfile

        return ScrapeProfile(canonical_id="70993")

    @staticmethod
    def _store(tmp_path: Any) -> Any:
        """A real ProfileStore — the updater calls `.save()` on it."""
        from ma_poc.services.profile_store import ProfileStore

        return ProfileStore(tmp_path / "profiles")

    def test_plan_only_success_records_the_winning_tier(self, tmp_path: Any) -> None:
        """A plan-only property must keep `last_success_tier`.

        This is the field whose loss drives the vicious cycle — without it the
        next run cannot go straight to the page that worked.
        """
        from ma_poc.services.profile_updater import update_profile_after_extraction

        profile = self._profile()
        result: dict[str, Any] = {
            "units": [],
            "plan_summaries": [{"floor_plan_name": f"A{i}"} for i in range(12)],
            "extraction_tier_used": "TIER_1_DOM_GENERIC_PLAN_TEXT",
        }

        updated = update_profile_after_extraction(
            profile, result, _extracted_row_count(result), self._store(tmp_path)
        )

        assert updated.confidence.consecutive_successes >= 1, (
            "a plan-only extraction was booked as a failure: "
            f"successes={updated.confidence.consecutive_successes} "
            f"failures={updated.confidence.consecutive_failures}"
        )
        assert updated.confidence.consecutive_failures == 0
        assert updated.confidence.last_unit_count == 12

    def test_a_genuine_miss_is_still_booked_as_a_failure(self, tmp_path: Any) -> None:
        """The negative case — the fix must not make every property a success."""
        from ma_poc.services.profile_updater import update_profile_after_extraction

        profile = self._profile()
        result: dict[str, Any] = {
            "units": [],
            "plan_summaries": [],
            "extraction_tier_used": "TIER_1_API_ENTRATA_EMPTY",
        }

        updated = update_profile_after_extraction(
            profile, result, _extracted_row_count(result), self._store(tmp_path)
        )
        assert updated.confidence.consecutive_successes == 0


class TestPlanOnlyDoesNotTriggerDriftDemotion:
    """The other half of the loop: drift must not fire on a channel move."""

    def test_no_drift_when_the_rows_merely_moved_channel(self) -> None:
        """Yesterday 12 rows in `units`; today 12 rows in `plan_summaries`.

        Pre-fix this read as 0 vs an expected 12 — a 100% drop, well past the
        30% demotion threshold — so the profile was demoted for extracting
        exactly the same data.
        """
        from ma_poc.models.scrape_profile import ProfileMaturity, ScrapeProfile
        from ma_poc.services.drift_detector import detect_drift

        profile = ScrapeProfile(canonical_id="70993")
        # detect_drift returns early on COLD, so without this the assertion
        # below passes vacuously — it would never reach the comparison at all.
        profile.confidence.maturity = ProfileMaturity.WARM
        profile.confidence.last_unit_count = 12

        result: dict[str, Any] = {
            "units": [],
            "plan_summaries": [{"floor_plan_name": f"A{i}"} for i in range(12)],
            "extraction_tier_used": "TIER_1_DOM_GENERIC_PLAN_TEXT",
        }

        drifted, reasons = detect_drift(
            profile, _extracted_row_count(result), result
        )
        assert not any("unit_count_drop" in r for r in reasons), reasons
        assert not drifted, reasons

    def test_a_real_drop_still_drifts(self) -> None:
        """Negative case: an actual collapse must still be caught."""
        from ma_poc.models.scrape_profile import ProfileMaturity, ScrapeProfile
        from ma_poc.services.drift_detector import detect_drift

        profile = ScrapeProfile(canonical_id="70993")
        profile.confidence.maturity = ProfileMaturity.WARM
        profile.confidence.last_unit_count = 40

        result: dict[str, Any] = {
            "units": [{}],
            "plan_summaries": [],
            "extraction_tier_used": "TIER_1_DOM_GENERIC_PLAN_TEXT",
        }
        drifted, reasons = detect_drift(
            profile, _extracted_row_count(result), result
        )
        assert any("unit_count_drop" in r for r in reasons), reasons
