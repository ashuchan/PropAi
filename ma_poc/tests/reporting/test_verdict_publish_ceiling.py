"""Publish-ceiling → verdict upgrade (2026-07-25).

``reporting.publish_ceiling`` has graded every zero-unit property since the
Lever-6 work, but the grade was WRITE-ONLY: it landed in
``_meta.publish_ceiling`` and nothing ever read it back into the verdict. A
property we had *proven* publishes no rent still shipped as FAILED_NO_DATA, so
correctly determining an operator's disclosure limit cost us success rate.

Two halves are tested here, because either one alone leaves the feature inert:

1. The decision function ``verdict_for_publish_ceiling`` — what an upgrade is
   allowed to do, and (more importantly) what it must refuse to do.
2. The WIRING in ``_process_property`` — that the function is actually called,
   and that the upgraded value reaches BOTH ``_meta.verdict`` and the
   PROPERTY_EMITTED event.

Half 2 is not ceremony. ``run_report`` resolves disagreement between the two
sources in favour of the EVENT (see
``tests/integration/contracts/test_verdict_meta_persistence.py::
test_resolver_event_wins_on_disagreement_and_warns``). If the emit kept
reporting the pre-upgrade verdict, every upgrade would be silently reverted in
the headline count while ``properties.json`` claimed otherwise — the exact
silent-miscount class that regression was written to prevent.
"""

from __future__ import annotations

import inspect

from ma_poc.reporting.publish_ceiling import PublishCeiling
from ma_poc.reporting.verdict import (
    Verdict,
    verdict_for_publish_ceiling,
    verdict_is_success,
)

_FAILED = Verdict.FAILED_NO_DATA.value


# ── 1. What an upgrade is allowed to do ──────────────────────────────────────


class TestUpgradeGrants:
    def test_confirmed_no_data_becomes_success_no_data_published(self) -> None:
        got = verdict_for_publish_ceiling(_FAILED, PublishCeiling.CONFIRMED_NO_DATA.value)
        assert got == Verdict.SUCCESS_NO_DATA_PUBLISHED.value

    def test_confirmed_plan_only_becomes_success_plan_only_published(self) -> None:
        got = verdict_for_publish_ceiling(_FAILED, PublishCeiling.CONFIRMED_PLAN_ONLY.value)
        assert got == Verdict.SUCCESS_PLAN_ONLY_PUBLISHED.value

    def test_both_upgrades_count_toward_the_success_numerator(self) -> None:
        assert verdict_is_success(Verdict.SUCCESS_NO_DATA_PUBLISHED)
        assert verdict_is_success(Verdict.SUCCESS_PLAN_ONLY_PUBLISHED)

    def test_upgrades_stay_distinct_values_not_folded_into_success(self) -> None:
        """The buckets must be visible in a plain verdict count.

        Folding them into ``SUCCESS`` would hide zero-data and plan-only
        properties inside the headline number — the whole point of grading them
        is to be able to say "no data available" out loud.
        """
        distinct = {
            Verdict.SUCCESS.value,
            Verdict.SUCCESS_NO_DATA_PUBLISHED.value,
            Verdict.SUCCESS_PLAN_ONLY_PUBLISHED.value,
        }
        assert len(distinct) == 3


# ── 2. What an upgrade must refuse to do ─────────────────────────────────────


class TestUpgradeRefusals:
    def test_extraction_miss_is_never_upgraded(self) -> None:
        """THE cardinal guard: rent tokens present + zero units extracted is OUR
        bug, not the operator's ceiling. rentcafe pid 18158 carried "No
        apartments available" AND listed 2 units at $1,795 — a no-data claim
        must never rest on a marker string.
        """
        assert verdict_for_publish_ceiling(_FAILED, PublishCeiling.EXTRACTION_MISS.value) is None

    def test_needs_render_is_never_upgraded(self) -> None:
        # SPA shell — we couldn't verify offline, so we haven't proven anything.
        assert verdict_for_publish_ceiling(_FAILED, PublishCeiling.NEEDS_RENDER.value) is None

    def test_uncertain_is_never_upgraded(self) -> None:
        assert verdict_for_publish_ceiling(_FAILED, PublishCeiling.UNCERTAIN.value) is None

    def test_only_confirmed_grades_are_upgradeable(self) -> None:
        """Pins the allow-list against a future PublishCeiling member silently
        inheriting gold-eligibility by being added to the enum."""
        upgradeable = {
            g.value
            for g in PublishCeiling
            if verdict_for_publish_ceiling(_FAILED, g.value) is not None
        }
        assert upgradeable == {
            PublishCeiling.CONFIRMED_NO_DATA.value,
            PublishCeiling.CONFIRMED_PLAN_ONLY.value,
        }

    def test_non_failed_no_data_verdicts_are_left_alone(self) -> None:
        """Deliberately narrow: only FAILED_NO_DATA is a candidate. In
        particular a FAILED_UNREACHABLE (we never saw the page) must not be
        laundered into a success by a stale ceiling grade.
        """
        for current in (
            Verdict.SUCCESS.value,
            Verdict.SUCCESS_PLAN_LEVEL.value,
            Verdict.FAILED_UNREACHABLE.value,
            Verdict.DEAD_URL.value,
            Verdict.CARRY_FORWARD.value,
            Verdict.PARTIAL.value,
            None,
        ):
            assert (
                verdict_for_publish_ceiling(current, PublishCeiling.CONFIRMED_NO_DATA.value)
                is None
            ), f"{current} must not be upgradeable"

    def test_missing_or_unknown_grade_is_not_an_upgrade(self) -> None:
        assert verdict_for_publish_ceiling(_FAILED, None) is None
        assert verdict_for_publish_ceiling(_FAILED, "") is None
        assert verdict_for_publish_ceiling(_FAILED, "SOMETHING_NEW") is None


# ── 3. The wiring — without this the grade is write-only ─────────────────────


class TestProcessPropertyWiring:
    """Source-level pins on ``_process_property``.

    The failure this guards against produces no test failure anywhere else:
    the function exists, its unit tests pass, and the pipeline simply never
    calls it. That is precisely how this shipped inert the first time.
    """

    @staticmethod
    def _src() -> str:
        from ma_poc.scripts.runners.jugnu import _process_property

        return inspect.getsource(_process_property)

    def test_process_property_calls_the_upgrade(self) -> None:
        assert "verdict_for_publish_ceiling(" in self._src(), (
            "_process_property no longer calls verdict_for_publish_ceiling — "
            "the publish-ceiling grade is write-only again and every proven "
            "no-data property is back to counting as FAILED_NO_DATA."
        )

    def test_emitted_verdict_reads_the_shared_upgraded_value(self) -> None:
        """The emit must ship the SAME value as ``_meta.verdict``.

        ``run_report`` lets the event win on disagreement, so an emit that
        still read ``verdict.verdict.value`` would silently undo every upgrade
        in the headline count.
        """
        src = self._src()
        assert "verdict=verdict_value," in src, (
            "PROPERTY_EMITTED no longer emits the post-upgrade verdict_value; "
            "the event ledger and _meta.verdict will disagree and the event wins."
        )
        assert "verdict=verdict.verdict.value," not in src, (
            "PROPERTY_EMITTED reverted to the pre-upgrade verdict."
        )

    def test_upgrade_records_what_it_overrode(self) -> None:
        assert "verdict_upgraded_from" in self._src(), (
            "an upgrade must leave an audit trail of the verdict it replaced"
        )
