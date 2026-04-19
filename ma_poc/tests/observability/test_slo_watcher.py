"""Tests for slo_watcher — SLO threshold checks."""
from __future__ import annotations

from ma_poc.observability.slo_watcher import SloThresholds, check


def _make_prop(tier: str = "TIER_1_API") -> dict:
    return {"_meta": {"scrape_tier_used": tier, "canonical_id": "p1"}}


def test_slo_all_green_returns_empty() -> None:
    props = [_make_prop("TIER_1_API") for _ in range(100)]
    violations = check({"llm": 0.5}, props)
    assert violations == []


def test_slo_success_rate_violation() -> None:
    props = [_make_prop("TIER_1_API") for _ in range(80)]
    props.extend([_make_prop("FAILED") for _ in range(20)])
    violations = check({"llm": 0.0}, props)
    names = [v.name for v in violations]
    assert "success_rate" in names


def test_slo_llm_cost_violation_samples_top_spenders() -> None:
    violations = check({"llm": 2.0}, [_make_prop()])
    names = [v.name for v in violations]
    assert "llm_cost_per_run" in names


def test_slo_vision_fallback_violation() -> None:
    props = [_make_prop("TIER_5_VISION") for _ in range(10)]
    props.extend([_make_prop("TIER_1_API") for _ in range(90)])
    violations = check({"llm": 0.0}, props)
    names = [v.name for v in violations]
    assert "vision_fallback_rate" in names


def test_slo_drift_noise_violation() -> None:
    props = [{"_meta": {"scrape_tier_used": "TIER_1_API", "flagged": True}} for _ in range(5)]
    props.extend([_make_prop() for _ in range(95)])
    violations = check({"llm": 0.0}, props)
    names = [v.name for v in violations]
    assert "drift_noise" in names


def test_slo_custom_thresholds_respected() -> None:
    t = SloThresholds(success_rate_min=0.5)
    props = [_make_prop("TIER_1_API") for _ in range(60)]
    props.extend([_make_prop("FAILED") for _ in range(40)])
    violations = check({"llm": 0.0}, props, thresholds=t)
    names = [v.name for v in violations]
    assert "success_rate" not in names  # 60% > 50%
