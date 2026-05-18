"""Verdict promotion when every link-hop was bot-blocked.

When entry fetch is OK but every link-hop comes back ``BOT_BLOCKED``
(Cloudflare challenge, SGCAPTCHA wall, PerimeterX, etc.), the property
is functionally unreachable for extraction. Pre-fix these flowed through
``FAILED_NO_DATA`` ("page loaded but no inventory found"), which is the
wrong bucket — implying the page WAS reachable. The verdict layer now
promotes them to ``FAILED_UNREACHABLE`` so failures.csv reflects reality
and triage doesn't get misled.

Canonical incident: Yardi ``/conventional/`` Cloudflare cluster on the
2026-05-18 cloud run — 193 PIDs (54% of FAILED_NO_DATA) where every hop
URL was a 5.6 KB Cloudflare challenge stub.

Covers:
  - ``_all_hops_bot_blocked()`` helper edge cases.
  - ``compute()`` promotion behaviour for the OK-entry / no-records /
    all-hops-blocked combination + interaction with other verdict signals
    (plan-summaries, records present, fetch-failed entry).
"""

from __future__ import annotations

from ma_poc.reporting.verdict import (
    Verdict,
    _all_hops_bot_blocked,
    compute,
)


# ── _all_hops_bot_blocked helper ─────────────────────────────────────────────


def test_helper_true_when_total_equals_blocked() -> None:
    assert _all_hops_bot_blocked({"hop_count_total": 3, "hop_count_bot_blocked": 3}) is True


def test_helper_false_when_one_hop_succeeded() -> None:
    assert _all_hops_bot_blocked({"hop_count_total": 3, "hop_count_bot_blocked": 2}) is False


def test_helper_false_when_no_hops_attempted() -> None:
    """A property whose entry succeeded and never needed a hop must NOT
    be reclassified — there's no signal at all."""
    assert _all_hops_bot_blocked({"hop_count_total": 0, "hop_count_bot_blocked": 0}) is False


def test_helper_handles_missing_or_malformed_keys() -> None:
    assert _all_hops_bot_blocked({}) is False
    assert _all_hops_bot_blocked(None) is False
    assert _all_hops_bot_blocked({"hop_count_total": "garbage"}) is False  # type: ignore[dict-item]


# ── compute() promotion integration ──────────────────────────────────────────


def test_compute_promotes_to_unreachable_when_all_hops_blocked() -> None:
    """The Yardi ``/conventional/`` pattern: entry OK, extract empty, every hop
    BOT_BLOCKED. Pre-fix returned FAILED_NO_DATA; post-fix returns
    FAILED_UNREACHABLE because the page is functionally unreachable
    for extraction."""
    result = compute(
        fetch_outcome="OK",
        extract_result={"records": []},
        hop_summary={"hop_count_total": 2, "hop_count_bot_blocked": 2},
    )
    assert result.verdict == Verdict.FAILED_UNREACHABLE
    assert "all_hops_bot_blocked" in result.reason


def test_compute_keeps_failed_no_data_when_some_hops_ok() -> None:
    """A mixed-outcome hop set means we DID reach the page; FAILED_NO_DATA
    is the correct classification."""
    result = compute(
        fetch_outcome="OK",
        extract_result={"records": []},
        hop_summary={"hop_count_total": 3, "hop_count_bot_blocked": 1},
    )
    assert result.verdict == Verdict.FAILED_NO_DATA


def test_compute_keeps_failed_no_data_when_no_hops_attempted() -> None:
    """Entry was fine, extraction returned empty, no hops tried. Pre-fix
    behaviour preserved."""
    result = compute(
        fetch_outcome="OK",
        extract_result={"records": []},
        hop_summary={"hop_count_total": 0, "hop_count_bot_blocked": 0},
    )
    assert result.verdict == Verdict.FAILED_NO_DATA


def test_compute_promotes_when_extract_result_missing() -> None:
    """When extract_result is None AND all hops blocked, the same promotion
    applies (covers the path where the cascade never reached extraction)."""
    result = compute(
        fetch_outcome="OK",
        extract_result=None,
        hop_summary={"hop_count_total": 4, "hop_count_bot_blocked": 4},
    )
    assert result.verdict == Verdict.FAILED_UNREACHABLE


def test_compute_keeps_success_when_records_present() -> None:
    """SUCCESS short-circuits BEFORE the hop check — extraction won, so
    BOT_BLOCKED hops are irrelevant."""
    result = compute(
        fetch_outcome="OK",
        extract_result={"records": [{"unit_id": "1"}]},
        hop_summary={"hop_count_total": 2, "hop_count_bot_blocked": 2},
    )
    assert result.verdict == Verdict.SUCCESS


def test_compute_keeps_success_plan_level_when_plan_summaries_present() -> None:
    """SUCCESS_PLAN_LEVEL takes priority over the bot-block promotion when
    the extractor produced plan-level rows on the entry page."""
    result = compute(
        fetch_outcome="OK",
        extract_result={"records": []},
        plan_summaries=[{"floor_plan_name": "1BR"}],
        hop_summary={"hop_count_total": 2, "hop_count_bot_blocked": 2},
    )
    assert result.verdict == Verdict.SUCCESS_PLAN_LEVEL
