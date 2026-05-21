"""Tests for the 2026-05-20 verdict-honesty downgrade.

Covers the SUCCESS → SUCCESS_PLAN_LEVEL downgrade rules added to
``compute()`` after the JSON-LD inflated-SUCCESS audit
(`project_jsonld_recovery_2026-05-20.md`):

1. **Path C marker override**: when scraper.py Path C plan-level fallback
   stamps ``result["_verdict_quality"] = "SUCCESS_PLAN_LEVEL"``, the
   verdict labeler honors it even when records would otherwise be
   admitted.
2. **All-inferred UIDs**: when every accepted unit has an ``inferred_*``
   UID prefix (synthetic fallback assigned by schema_gate), the property
   is plan-level — downgrade.
3. **No rent signal**: when no unit carries a numeric rent value, the
   property is plan-level — downgrade.

Pre-existing verdicts (FAILED_*, DEAD_URL, CARRY_FORWARD, PARTIAL)
must NOT be affected by the downgrade signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ma_poc.reporting.verdict import Verdict, compute


@dataclass
class _FakeExtract:
    records: list[dict[str, Any]] = field(default_factory=list)


# ── Path C plan-level marker override ────────────────────────────────────────


class TestPathCMarkerOverride:
    def test_marker_downgrades_otherwise_success(self) -> None:
        """SUCCESS verdict with Path C marker → SUCCESS_PLAN_LEVEL."""
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=[{"unit_id": "217", "beds": 2, "market_rent_low": 1500}]),
            verdict_quality_override="SUCCESS_PLAN_LEVEL",
        )
        assert result.verdict == Verdict.SUCCESS_PLAN_LEVEL
        assert result.reason == "path_c_plan_level_fallback"
        assert result.source == "validate"

    def test_marker_does_not_affect_failed_unreachable(self) -> None:
        """A fetch failure dominates the Path C marker — stale data shouldn't
        promote a failed scrape to plan-level."""
        result = compute(
            fetch_outcome="HARD_FAIL",
            extract_result=_FakeExtract(records=[]),
            verdict_quality_override="SUCCESS_PLAN_LEVEL",
        )
        assert result.verdict == Verdict.FAILED_UNREACHABLE

    def test_marker_does_not_affect_dead_url(self) -> None:
        result = compute(
            fetch_outcome="DEAD_URL",
            verdict_quality_override="SUCCESS_PLAN_LEVEL",
        )
        assert result.verdict == Verdict.DEAD_URL

    def test_marker_does_not_affect_carry_forward(self) -> None:
        result = compute(
            carry_forward_applied=True,
            verdict_quality_override="SUCCESS_PLAN_LEVEL",
        )
        assert result.verdict == Verdict.CARRY_FORWARD

    def test_unrelated_override_string_ignored(self) -> None:
        """Only the canonical ``"SUCCESS_PLAN_LEVEL"`` literal triggers the
        downgrade — typos / unrelated values fall through unchanged."""
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=[{"unit_id": "217", "beds": 2, "market_rent_low": 1500}]),
            verdict_quality_override="DOWNGRADE_PLEASE",
            units=[{"unit_id": "217", "market_rent_low": 1500}],
        )
        assert result.verdict == Verdict.SUCCESS

    def test_none_override_keeps_success(self) -> None:
        """Default ``None`` override → no behavior change."""
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=[{"unit_id": "217", "beds": 2, "market_rent_low": 1500}]),
            verdict_quality_override=None,
            units=[{"unit_id": "217", "market_rent_low": 1500}],
        )
        assert result.verdict == Verdict.SUCCESS


# ── All-inferred UID downgrade ───────────────────────────────────────────────


class TestAllInferredDowngrade:
    def test_every_unit_inferred_yields_plan_level(self) -> None:
        """All units have ``inferred_*`` UID prefix → SUCCESS_PLAN_LEVEL."""
        units = [
            {"unit_id": "inferred_a1b2c3d4", "beds": 1, "market_rent_low": 1500},
            {"unit_id": "inferred_e5f6g7h8", "beds": 2, "market_rent_low": 1800},
            {"unit_id": "inferred_i9j0k1l2", "beds": 3, "market_rent_low": 2200},
        ]
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=units),
            units=units,
        )
        assert result.verdict == Verdict.SUCCESS_PLAN_LEVEL
        assert "all_inferred_uids" in result.reason
        assert "3 units" in result.reason

    def test_mixed_uids_keeps_success(self) -> None:
        """One real UID among inferred ones → SUCCESS (the real one anchors)."""
        units = [
            {"unit_id": "inferred_a1b2c3d4", "beds": 1, "market_rent_low": 1500},
            {"unit_id": "217", "beds": 2, "market_rent_low": 1800},
        ]
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=units),
            units=units,
        )
        assert result.verdict == Verdict.SUCCESS

    def test_all_real_uids_keeps_success(self) -> None:
        units = [
            {"unit_id": "101", "beds": 1, "market_rent_low": 1500},
            {"unit_id": "202", "beds": 2, "market_rent_low": 1800},
        ]
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=units),
            units=units,
        )
        assert result.verdict == Verdict.SUCCESS

    def test_inferred_prefix_substring_only_matched_at_start(self) -> None:
        """``"some-inferred_thing"`` should NOT trigger — only leading prefix."""
        units = [
            {"unit_id": "trailing-inferred_xyz", "beds": 1, "market_rent_low": 1500},
        ]
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=units),
            units=units,
        )
        assert result.verdict == Verdict.SUCCESS


# ── No-rent-signal downgrade ─────────────────────────────────────────────────


class TestNoRentSignalDowngrade:
    def test_zero_rent_units_yields_plan_level(self) -> None:
        """All units have real UIDs but no rent → SUCCESS_PLAN_LEVEL.

        Captures the Stonewater/Chatwell-pre-Nestin shape: real apartment
        identities visible in DOM but rents not published (Call for pricing).
        """
        units = [
            {"unit_id": "101", "beds": 1, "market_rent_low": None},
            {"unit_id": "202", "beds": 2, "market_rent_low": None},
        ]
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=units),
            units=units,
        )
        assert result.verdict == Verdict.SUCCESS_PLAN_LEVEL
        assert "no_rent_signal" in result.reason

    def test_some_rent_keeps_success(self) -> None:
        """≥50 % of units carry a numeric rent → still SUCCESS (matches
        ``property_has_rent_signal`` default threshold)."""
        units = [
            {"unit_id": "101", "beds": 1, "market_rent_low": 1500},
            {"unit_id": "202", "beds": 2, "market_rent_low": None},
        ]
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=units),
            units=units,
        )
        assert result.verdict == Verdict.SUCCESS

    def test_zero_rent_with_real_uids_still_downgrades(self) -> None:
        """Order matters: ``all_inferred`` check fires first; if some units
        have real UIDs but none carry rent, the no-rent check downgrades."""
        units = [
            {"unit_id": "101", "beds": 1},  # real UID, no rent
            {"unit_id": "202", "beds": 2},  # real UID, no rent
        ]
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=units),
            units=units,
        )
        assert result.verdict == Verdict.SUCCESS_PLAN_LEVEL
        assert "no_rent_signal" in result.reason

    def test_zero_rent_with_inferred_uids_uses_inferred_reason(self) -> None:
        """When BOTH conditions apply (all inferred + all no-rent), the
        ``all_inferred`` reason wins because it checks first — both reasons
        are valid downgrades, neither is wrong."""
        units = [
            {"unit_id": "inferred_abc", "beds": 1},
            {"unit_id": "inferred_xyz", "beds": 2},
        ]
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=units),
            units=units,
        )
        assert result.verdict == Verdict.SUCCESS_PLAN_LEVEL
        assert "all_inferred_uids" in result.reason


# ── Back-compat: omitting units arg keeps prior behaviour ────────────────────


class TestBackCompat:
    def test_omitting_units_keeps_success(self) -> None:
        """Callers that don't pass ``units`` keep pre-2026-05-20 behavior:
        SUCCESS unchanged (the new downgrade logic only fires when ``units``
        is explicitly provided)."""
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=[{"unit_id": "x"}]),
        )
        assert result.verdict == Verdict.SUCCESS

    def test_empty_units_list_keeps_success(self) -> None:
        """Empty units list is treated the same as omitted — the records-
        admitted path already finalized the SUCCESS earlier in compute()."""
        result = compute(
            fetch_outcome="OK",
            extract_result=_FakeExtract(records=[{"unit_id": "x"}]),
            units=[],
        )
        assert result.verdict == Verdict.SUCCESS
