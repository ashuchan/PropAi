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


def test_slo_reads_jugnu_extract_result_tier() -> None:
    """Verdict + ``_extract_result.tier_used`` keep success_rate accurate.

    Regression: the 2026-04-19 run had ``_extract_result`` missing from
    every property record, so SLO observed=0.0000 success despite 47
    properties verdicted SUCCESS. Lock the Jugnu shape here so the keys
    can't drift silently again.
    """
    succ = [
        {
            "_meta": {"verdict": "SUCCESS", "canonical_id": f"s{i}"},
            "_extract_result": {"tier_used": "TIER_3_DOM", "llm_cost_usd": 0.0},
        }
        for i in range(95)
    ]
    fail = [
        {
            "_meta": {"verdict": "FAILED_NO_DATA", "canonical_id": f"f{i}"},
            "_extract_result": {"tier_used": "TIER_4_LLM", "llm_cost_usd": 0.05},
        }
        for i in range(5)
    ]
    violations = check({"llm": 0.5}, succ + fail)
    names = [v.name for v in violations]
    assert "success_rate" not in names  # 95% meets the 95% threshold


# ── llm_tax_avoidance_rate ──────────────────────────────────────────────────


def _make_jugnu_prop(tier: str, *, verdict: str = "SUCCESS") -> dict:
    """Mirror the Jugnu property shape so tests pin the same key paths
    the runner produces. ``_extract_result.tier_used`` is the Jugnu key;
    ``_meta.scrape_tier_used`` is the legacy fallback."""
    return {
        "_meta": {"verdict": verdict, "canonical_id": "p"},
        "_extract_result": {"tier_used": tier, "llm_cost_usd": 0.0},
    }


def test_llm_tax_avoidance_rate_default_threshold_is_zero() -> None:
    """Default ``llm_tax_avoidance_min`` must be 0.0 so existing runs
    without the new replay tier don't suddenly flag. Operators ratchet
    it up once a baseline is established."""
    t = SloThresholds()
    assert t.llm_tax_avoidance_min == 0.0


def test_llm_tax_avoidance_rate_observes_replay_wins() -> None:
    """When ratcheted to 0.5, a population where 80% of successes won
    via the zero-LLM replay tier passes; 30% replay wins fails.

    This is the metric we'd watch to detect another "every property
    starts COLD" regression: in the broken state observed in shard_0 of
    2026-05-08 (0/224 successes won via replay), this would alert at any
    non-zero threshold."""
    props_high_replay = (
        [_make_jugnu_prop("TIER_1_PROFILE_MAPPING") for _ in range(80)]
        + [_make_jugnu_prop("TIER_4_LLM_DOM") for _ in range(20)]
    )
    t = SloThresholds(llm_tax_avoidance_min=0.5)
    violations = check({"llm": 0.0}, props_high_replay, thresholds=t)
    names = [v.name for v in violations]
    assert "llm_tax_avoidance_rate" not in names

    props_low_replay = (
        [_make_jugnu_prop("TIER_1_PROFILE_MAPPING") for _ in range(30)]
        + [_make_jugnu_prop("TIER_4_LLM_DOM") for _ in range(70)]
    )
    violations = check({"llm": 0.0}, props_low_replay, thresholds=t)
    names = [v.name for v in violations]
    assert "llm_tax_avoidance_rate" in names


def test_llm_tax_avoidance_rate_excludes_failed_from_denominator() -> None:
    """Failed properties can't have replayed by definition. Including
    them in the denominator would conflate "fetch broken" with "loop
    broken" — two failure modes that need different responses."""
    props = (
        [_make_jugnu_prop("TIER_1_PROFILE_MAPPING") for _ in range(50)]
        + [_make_jugnu_prop("TIER_4_LLM_DOM") for _ in range(40)]
        # 10 unreachable properties — should NOT count against avoidance.
        + [_make_jugnu_prop("FAILED", verdict="FAILED_UNREACHABLE") for _ in range(10)]
    )
    # 50/90 successful = 55.5% replay rate, threshold 0.5 → no violation.
    t = SloThresholds(
        llm_tax_avoidance_min=0.5,
        success_rate_min=0.0,  # don't trip the success_rate gate too
    )
    violations = check({"llm": 0.0}, props, thresholds=t)
    names = [v.name for v in violations]
    assert "llm_tax_avoidance_rate" not in names


def test_llm_tax_avoidance_rate_handles_all_failures() -> None:
    """Edge case — when zero properties succeeded, the metric is
    undefined. Don't divide by zero, don't emit a violation; let other
    checks (success_rate) carry the alert."""
    props = [
        _make_jugnu_prop("FAILED", verdict="FAILED_UNREACHABLE") for _ in range(10)
    ]
    t = SloThresholds(llm_tax_avoidance_min=0.5, success_rate_min=0.0)
    violations = check({"llm": 0.0}, props, thresholds=t)
    names = [v.name for v in violations]
    assert "llm_tax_avoidance_rate" not in names
