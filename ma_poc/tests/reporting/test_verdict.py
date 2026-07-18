"""Tests for verdict — property-level outcome computation."""

from __future__ import annotations

from dataclasses import dataclass, field

from ma_poc.reporting.verdict import Verdict, compute


@dataclass
class FakeExtractResult:
    records: list = field(default_factory=list)


@dataclass
class FakeValidated:
    accepted: list = field(default_factory=list)
    rejected: list = field(default_factory=list)


def test_verdict_ssl_error_is_failed_unreachable() -> None:
    r = compute(fetch_outcome="HARD_FAIL")
    assert r.verdict == Verdict.FAILED_UNREACHABLE


def test_verdict_empty_extract_is_failed_no_data() -> None:
    r = compute(fetch_outcome="OK", extract_result=FakeExtractResult())
    assert r.verdict == Verdict.FAILED_NO_DATA


def test_verdict_carry_forward_wins_over_fetch_failure() -> None:
    r = compute(fetch_outcome="HARD_FAIL", carry_forward_applied=True)
    assert r.verdict == Verdict.CARRY_FORWARD


def test_verdict_majority_reject_is_partial() -> None:
    v = FakeValidated(accepted=[1], rejected=[1, 2, 3])
    r = compute(
        fetch_outcome="OK",
        extract_result=FakeExtractResult(records=[1, 2, 3, 4]),
        validated=v,
    )
    assert r.verdict == Verdict.PARTIAL


def test_verdict_all_accept_is_success() -> None:
    v = FakeValidated(accepted=[1, 2, 3])
    r = compute(
        fetch_outcome="OK",
        extract_result=FakeExtractResult(records=[1, 2, 3]),
        validated=v,
    )
    assert r.verdict == Verdict.SUCCESS


# ── 2026-07-18 verdict-hygiene bundle ────────────────────────────────────────

def _terminal(units: list[dict], override: str | None = None):
    """compute() reaching the terminal SUCCESS-vs-PLAN downgrade block."""
    return compute(
        fetch_outcome="OK",
        extract_result=FakeExtractResult(records=units),
        validated=FakeValidated(accepted=units),
        verdict_quality_override=override,
        units=units,
    )


def test_demote_guard_ignores_stale_plan_stamp_for_unit_level() -> None:
    # (e): a Path-C SUCCESS_PLAN_LEVEL stamp must NOT bury units that carry
    # real identity + rent.
    units = [
        {"unit_id": "101", "rent_low": 1500, "availability_status": "AVAILABLE"},
        {"unit_id": "102", "rent_low": 1600, "availability_status": "AVAILABLE"},
    ]
    assert _terminal(units, override="SUCCESS_PLAN_LEVEL").verdict == Verdict.SUCCESS


def test_demote_guard_keeps_plan_level_when_genuinely_plan() -> None:
    # (e): stale stamp stands when the rows are genuinely plan-level.
    units = [
        {"unit_id": "inferred_1", "availability_status": "UNAVAILABLE"},
        {"unit_id": "inferred_2", "availability_status": "UNAVAILABLE"},
    ]
    assert (
        _terminal(units, override="SUCCESS_PLAN_LEVEL").verdict
        == Verdict.SUCCESS_PLAN_LEVEL
    )


def test_source_id_makes_inferred_rows_unit_level() -> None:
    # (c): inferred_ unit_ids but real per-unit source ids → unit-level.
    units = [
        {"unit_id": "inferred_a", "rent_low": 1500, "availability_status": "AVAILABLE",
         "source_ids": {"sightmap_unit_id": "SM123"}},
        {"unit_id": "inferred_b", "rent_low": 1600, "availability_status": "AVAILABLE",
         "source_ids": {"sightmap_unit_id": "SM124"}},
    ]
    assert _terminal(units).verdict == Verdict.SUCCESS


def test_all_inferred_without_source_id_stays_plan_level() -> None:
    # (c) guard: inferred ids and NO source id → still plan-level.
    units = [
        {"unit_id": "inferred_a", "rent_low": 1500, "availability_status": "AVAILABLE"},
        {"unit_id": "inferred_b", "rent_low": 1600, "availability_status": "AVAILABLE"},
    ]
    assert _terminal(units).verdict == Verdict.SUCCESS_PLAN_LEVEL


def test_unavailable_rentless_cells_dont_dilute_rent_signal() -> None:
    # (b): 1 priced-available unit + 5 UNAVAILABLE-rentless real-uid map cells
    # → rent-coverage computed over available/real-anchor units only → SUCCESS.
    units = [{"unit_id": "1", "rent_low": 1500, "availability_status": "AVAILABLE"}]
    units += [
        {"unit_id": str(i), "availability_status": "UNAVAILABLE"} for i in range(2, 7)
    ]
    assert _terminal(units).verdict == Verdict.SUCCESS
