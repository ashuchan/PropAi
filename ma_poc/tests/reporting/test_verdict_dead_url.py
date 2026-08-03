"""Tests for the Stage 3 DEAD_URL outcome.

Covers:
  * ``Verdict.DEAD_URL`` enum value + ``FetchOutcome.DEAD_URL``
  * ``compute()`` returns ``Verdict.DEAD_URL`` when ``fetch_outcome="DEAD_URL"``
  * ``verdict_excluded_from_success_rate`` recognises DEAD_URL only
  * Distinction from ``FAILED_UNREACHABLE``: dead URLs are terminal +
    excluded from success-rate denominator; FAILED_UNREACHABLE is transient
    and stays in the funnel
"""

from __future__ import annotations

import pytest

from ma_poc.fetch.contracts import FetchOutcome
from ma_poc.reporting.verdict import (
    Verdict,
    compute,
    verdict_excluded_from_success_rate,
    verdict_is_success,
)

# ── Enum addition ────────────────────────────────────────────────────────────


def test_dead_url_is_in_verdict_enum() -> None:
    assert Verdict.DEAD_URL.value == "DEAD_URL"
    assert "DEAD_URL" in {v.value for v in Verdict}


def test_dead_url_is_in_fetch_outcome_enum() -> None:
    assert FetchOutcome.DEAD_URL.value == "DEAD_URL"
    assert "DEAD_URL" in {v.value for v in FetchOutcome}


# ── compute() recognition ────────────────────────────────────────────────────


class TestComputeDeadUrl:
    def test_fetch_outcome_dead_url_yields_dead_url_verdict(self):
        result = compute(fetch_outcome="DEAD_URL")
        assert result.verdict == Verdict.DEAD_URL
        assert result.source == "fetch"

    def test_fetch_outcome_dead_url_distinct_from_hard_fail(self):
        """A 404 (DEAD_URL) is terminal; a 500 (HARD_FAIL) routes to
        FAILED_UNREACHABLE which is transient."""
        dead = compute(fetch_outcome="DEAD_URL")
        hard = compute(fetch_outcome="HARD_FAIL")
        assert dead.verdict == Verdict.DEAD_URL
        assert hard.verdict == Verdict.FAILED_UNREACHABLE
        assert dead.verdict != hard.verdict

    def test_carry_forward_takes_precedence_over_dead_url(self):
        """If a prior run's state is reusable, prefer carry-forward over
        flagging the URL as dead. The dead-URL classification is for the
        retry-loop side of the system."""
        result = compute(fetch_outcome="DEAD_URL", carry_forward_applied=True)
        assert result.verdict == Verdict.CARRY_FORWARD

    def test_dead_url_reason_describes_classification(self):
        result = compute(fetch_outcome="DEAD_URL")
        assert "dead" in result.reason.lower()

    @pytest.mark.parametrize(
        ("adapter", "unit_id"),
        [
            ("entrata", "E-101"),
            ("realpage_cws", "CWS-68"),
            ("knock", "K-210"),
        ],
    )
    def test_recovered_units_outrank_dead_entry_url(
        self,
        adapter: str,
        unit_id: str,
    ) -> None:
        """A dead configured URL cannot veto a successful bounded sub-route."""
        units = [
            {
                "unit_number": unit_id,
                "market_rent_low": 1800,
                "extraction_tier": f"TIER_1_API_{adapter.upper()}",
            }
        ]

        result = compute(
            fetch_outcome="DEAD_URL",
            extract_result={"units": units},
            units=units,
        )

        assert result.verdict == Verdict.SUCCESS


# ── verdict_is_success ───────────────────────────────────────────────────────


def test_dead_url_is_not_success() -> None:
    assert verdict_is_success(Verdict.DEAD_URL) is False
    assert verdict_is_success("DEAD_URL") is False


# ── verdict_excluded_from_success_rate ───────────────────────────────────────


class TestVerdictExcludedFromSuccessRate:
    def test_dead_url_excluded(self):
        """Only DEAD_URL is excluded from the denominator. Reporting layers
        compute success-rate as ``successes / (total - excluded)``."""
        assert verdict_excluded_from_success_rate(Verdict.DEAD_URL) is True
        assert verdict_excluded_from_success_rate("DEAD_URL") is True

    @pytest.mark.parametrize(
        "verdict",
        [
            Verdict.SUCCESS,
            Verdict.SUCCESS_PLAN_LEVEL,
            Verdict.FAILED_UNREACHABLE,
            Verdict.FAILED_NO_DATA,
            Verdict.CARRY_FORWARD,
            Verdict.PARTIAL,
        ],
    )
    def test_other_verdicts_stay_in_denominator(self, verdict):
        """FAILED_UNREACHABLE is transient (the URL might come back); it
        stays in the denominator. CARRY_FORWARD reuses prior state — also
        in the denominator (it didn't actually re-extract today)."""
        assert verdict_excluded_from_success_rate(verdict) is False

    def test_none_is_not_excluded(self):
        assert verdict_excluded_from_success_rate(None) is False

    def test_unknown_string_is_not_excluded(self):
        assert verdict_excluded_from_success_rate("MADE_UP_VERDICT") is False
