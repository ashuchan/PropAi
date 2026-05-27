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


def test_slo_success_no_availability_counts_as_success() -> None:
    """2026-05-27. RentCafe waitlist-cohort properties resolve to
    SUCCESS_NO_AVAILABILITY (operator published zero inventory). The
    scrape itself succeeded, so the SLO must count them toward the
    success-rate numerator — they MUST NOT increment failure_rate."""
    props = [
        {
            "_meta": {
                "verdict": "SUCCESS_NO_AVAILABILITY",
                "canonical_id": f"nai{i}",
            },
            "_extract_result": {"tier_used": "TIER_1_DOM_NO_AVAILABILITY"},
        }
        for i in range(100)
    ]
    violations = check({"llm": 0.0}, props)
    names = [v.name for v in violations]
    assert "success_rate" not in names


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


# ── Bug A v0.2 defence-in-depth — events.jsonl as authoritative secondary ───


def test_slo_consults_events_when_meta_verdict_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """When ``_meta.verdict`` is missing for every property (the
    2026-05-11 production-observed shape — Bug A), the success-rate
    SLO must still flag correctly using events.jsonl as the
    authoritative secondary source.

    Without this, ``slo_watcher.check`` would silently classify every
    failed property as a success and skip paging on a run that is
    actually losing ~40% of its data.
    """
    import json

    run_dir = tmp_path / "run-test"
    run_dir.mkdir()
    # 70 actual successes, 30 actual failures per events.jsonl. All
    # properties have empty _meta.verdict (Bug A's prod shape).
    properties = [
        {
            "_meta": {"canonical_id": f"P{i}"},  # NO verdict, NO scrape_tier_used
            "_extract_result": {"tier_used": "TIER_1_API", "llm_cost_usd": 0.0},
        }
        for i in range(100)
    ]
    events = (
        [
            {"kind": "output.property_emitted", "property_id": f"P{i}", "verdict": "SUCCESS"}
            for i in range(70)
        ]
        + [
            {"kind": "output.property_emitted", "property_id": f"P{i}", "verdict": "FAILED_NO_DATA"}
            for i in range(70, 100)
        ]
    )
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )

    # WITH run_dir — events consulted, 30/100 failures → success_rate 70% < 95% → violation.
    violations = check({"llm": 0.0}, properties, run_dir=run_dir)
    names = [v.name for v in violations]
    assert "success_rate" in names, (
        "success_rate SLO must fire when 30% of properties failed per "
        "events.jsonl, even though _meta.verdict is empty everywhere. "
        "Without events consultation the runner would silently report "
        "100% success and no paging."
    )


def test_slo_legacy_no_run_dir_path_still_works() -> None:
    """When ``run_dir`` is omitted (legacy callers, unit tests not staging
    a run dir), behaviour must match pre-v0.2: only ``_meta.verdict`` is
    consulted, no I/O is attempted. Backwards-compat guarantee."""
    # 80 successes + 20 failures, all encoded in _meta.verdict only.
    props = [_make_jugnu_prop("TIER_3_DOM", verdict="SUCCESS") for _ in range(80)]
    props.extend(
        _make_jugnu_prop("TIER_1_API", verdict="FAILED_NO_DATA") for _ in range(20)
    )
    violations = check({"llm": 0.0}, props)  # NO run_dir argument
    names = [v.name for v in violations]
    # 80% < 95% success threshold → violation expected.
    assert "success_rate" in names


def test_slo_events_win_on_disagreement(
    tmp_path,  # type: ignore[no-untyped-def]
    caplog,
) -> None:
    """When _meta.verdict and the emit event disagree (a runner bug —
    they're written from the same source by construction), events win
    and a warning is logged. Documents the contract so a future change
    to the runner that creates disagreement is loud, not silent."""
    import json
    import logging

    caplog.set_level(logging.WARNING, logger="ma_poc.reporting.verdict")

    run_dir = tmp_path / "run-test"
    run_dir.mkdir()
    # _meta says SUCCESS (would-be-100% per legacy path) but events say
    # all 5 failed. Events must win → success_rate 0% → violation.
    properties = [
        {
            "_meta": {"canonical_id": f"P{i}", "verdict": "SUCCESS"},
            "_extract_result": {"tier_used": "TIER_1_API", "llm_cost_usd": 0.0},
        }
        for i in range(5)
    ]
    events = [
        {"kind": "output.property_emitted", "property_id": f"P{i}", "verdict": "FAILED_NO_DATA"}
        for i in range(5)
    ]
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )

    violations = check({"llm": 0.0}, properties, run_dir=run_dir)
    names = [v.name for v in violations]
    assert "success_rate" in names

    disagreement_warnings = [
        rec for rec in caplog.records if "verdict disagreement" in rec.message
    ]
    assert disagreement_warnings, (
        "expected at least one 'verdict disagreement' warning when "
        "_meta and events disagree"
    )
