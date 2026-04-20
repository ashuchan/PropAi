"""Tests for F7 — run_report funnel and pre-extraction termination separation."""
from __future__ import annotations

from pathlib import Path

from ma_poc.reporting.run_report import build


def _prop_short_circuit(err_fragment: str = "TRANSIENT") -> dict:
    return {
        "_meta": {
            "scrape_tier_used": "generic:no_body_short_circuit",
            "verdict": "FAILED_UNREACHABLE",
            "errors": [f"FAILED_UNREACHABLE: fetch_outcome={err_fragment}"],
        },
        "errors": [f"FAILED_UNREACHABLE: fetch_outcome={err_fragment}"],
    }


def _prop_success(tier: str = "TIER_3_DOM") -> dict:
    return {"_meta": {"scrape_tier_used": tier, "verdict": "SUCCESS"}}


def test_run_report_funnel_has_five_expected_keys(tmp_path: Path) -> None:
    props = [_prop_success(), _prop_short_circuit()]
    report = build(props, tmp_path, "2026-04-22")
    assert "totals" in report
    totals = report["totals"]
    for key in ("properties", "succeeded", "failed", "carry_forward", "success_rate_pct"):
        assert key in totals, f"Missing key: {key}"


def test_run_report_tier_distribution_excludes_no_body_short_circuit(tmp_path: Path) -> None:
    props = [_prop_success("TIER_3_DOM"), _prop_short_circuit("TRANSIENT")]
    report = build(props, tmp_path, "2026-04-22")
    assert "generic:no_body_short_circuit" not in report["tier_distribution"]
    assert report["tier_distribution"].get("TIER_3_DOM") == 1


def test_run_report_pre_extraction_terminations_counts_fetch_outcomes(tmp_path: Path) -> None:
    props = [
        _prop_short_circuit("TRANSIENT"),
        _prop_short_circuit("TRANSIENT"),
        _prop_short_circuit("HARD_FAIL"),
        _prop_short_circuit("BOT_BLOCKED"),
        _prop_success(),
    ]
    report = build(props, tmp_path, "2026-04-22")
    terms = report.get("pre_extraction_terminations", {})
    # Should have at least some pre-extraction entries
    total_pre = sum(terms.values())
    assert total_pre >= 3  # 2 transient + 1 hard_fail + 1 bot_blocked = at least 3


def test_run_report_back_compat_total_succeeded_unchanged(tmp_path: Path) -> None:
    """totals.succeeded must equal input_properties minus failed — regardless of F7 changes."""
    props = [_prop_success()] * 4 + [_prop_short_circuit()] * 2
    report = build(props, tmp_path, "2026-04-22")
    assert report["totals"]["properties"] == 6
    assert report["totals"]["succeeded"] + report["totals"]["failed"] == 6
