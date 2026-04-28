"""Tests for experiment_sampler + experiment_comparator.

The sampler is exercised against a synthetic ``properties.json`` written
to a temp dir so we don't depend on actual production artefacts. The
comparator is fed plain ``ReplayResult`` dicts so it can be tested
without the adapter or the LLM.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ma_poc.services.experiment_comparator import (
    ReplayResult,
    compare,
    summarise,
)
from ma_poc.services.experiment_sampler import (
    DEFAULT_BUCKET_TARGETS,
    Sample,
    pick_sample,
)


def _record(
    cid: str,
    *,
    tier: str = "TIER_1_API",
    units: int = 1,
    verdict: str = "",
    link_hop: bool = False,
    has_extract_result: bool = False,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "Property Name": f"Prop {cid}",
        "Property ID": cid,
        "Website": f"https://{cid}.example.com",
        "_meta": {"canonical_id": cid, "scrape_tier_used": tier, "verdict": verdict},
        "units": [{"unit_id": str(i)} for i in range(units)],
    }
    if has_extract_result:
        rec["_extract_result"] = {"tier_used": tier}
    if link_hop:
        rec["_link_hop_success"] = True
    return rec


def _seed_run_dir(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    """Create a tmp data/runs/{date}/properties.json with the given records."""
    run_dir = tmp_path / "runs" / "2026-01-01"
    run_dir.mkdir(parents=True)
    (run_dir / "properties.json").write_text(json.dumps(records), encoding="utf-8")
    return run_dir


def test_pick_sample_assigns_records_to_correct_buckets(tmp_path: Path) -> None:
    records = [
        _record("t1-1", tier="TIER_1_API", units=10),
        _record("t1-2", tier="TIER_1_PROFILE_MAPPING", units=8),
        _record("t23-1", tier="TIER_2_JSONLD", units=5),
        _record("t23-2", tier="TIER_3_DOM", units=4),
        _record("llm-1", tier="TIER_4_LLM_API", units=3),
        _record("llm-2", tier="TIER_4_LLM", units=2),
        _record("hop-1", tier="TIER_4_LLM_API", units=6, link_hop=True),
        _record("fail-1", tier="TIER_1_API", units=0, verdict="FAILED_NO_DATA"),
        _record("fail-2", tier="TIER_4_LLM", units=0),
    ]
    run_dir = _seed_run_dir(tmp_path, records)
    samples = pick_sample(run_dir, n=20, seed=1)
    by_bucket: dict[str, list[Sample]] = {}
    for s in samples:
        by_bucket.setdefault(s.bucket, []).append(s)

    assert "tier1" in by_bucket
    assert "tier_2_3" in by_bucket
    assert "tier4_llm" in by_bucket
    assert "link_hop" in by_bucket
    assert "failed_no_data" in by_bucket


def test_pick_sample_deterministic_across_runs(tmp_path: Path) -> None:
    records = [_record(f"P-{i:03d}", units=i + 1) for i in range(40)]
    run_dir = _seed_run_dir(tmp_path, records)
    a = pick_sample(run_dir, n=10, seed=42)
    b = pick_sample(run_dir, n=10, seed=42)
    assert [s.canonical_id for s in a] == [s.canonical_id for s in b]


def test_pick_sample_seed_changes_selection(tmp_path: Path) -> None:
    records = [_record(f"P-{i:03d}", units=i + 1) for i in range(40)]
    run_dir = _seed_run_dir(tmp_path, records)
    a = {s.canonical_id for s in pick_sample(run_dir, n=10, seed=1)}
    b = {s.canonical_id for s in pick_sample(run_dir, n=10, seed=2)}
    assert a != b


def test_pick_sample_bucket_filter_returns_only_that_bucket(tmp_path: Path) -> None:
    records = [
        _record("t1-1", tier="TIER_1_API", units=10),
        _record("llm-1", tier="TIER_4_LLM_API", units=3),
        _record("fail-1", tier="TIER_1_API", units=0, verdict="FAILED_NO_DATA"),
    ]
    run_dir = _seed_run_dir(tmp_path, records)
    samples = pick_sample(run_dir, n=5, seed=1, bucket_filter="failed_no_data")
    assert len(samples) == 1
    assert samples[0].bucket == "failed_no_data"


def test_pick_sample_prefers_extract_result_tier_over_meta(tmp_path: Path) -> None:
    """Jugnu pipeline writes tier_used inside _extract_result. Sampler must
    read it from there even when _meta.scrape_tier_used is also present."""
    record = _record("X", tier="TIER_4_LLM_API", units=2, has_extract_result=True)
    record["_meta"]["scrape_tier_used"] = "TIER_1_API"
    run_dir = _seed_run_dir(tmp_path, [record])
    samples = pick_sample(run_dir, n=1, seed=1)
    assert samples[0].tier_used == "TIER_4_LLM_API"
    assert samples[0].bucket == "tier4_llm"


def test_pick_sample_raises_when_properties_missing(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "2026-01-01"
    run_dir.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        pick_sample(run_dir, n=5, seed=1)


def test_pick_sample_targets_sum_to_one() -> None:
    assert abs(sum(DEFAULT_BUCKET_TARGETS.values()) - 1.0) < 1e-6


def _full_unit(uid: str) -> dict[str, Any]:
    return {
        "unit_id": uid,
        "beds": 1,
        "baths": 1,
        "sqft": 700,
        "rent_low": 1500,
        "available_date": "2026-05-01",
    }


def _minimum_only_unit(uid: str) -> dict[str, Any]:
    return {"unit_id": uid, "beds": 1, "baths": 1, "sqft": 700}


def test_compare_zero_change_returns_zero_deltas() -> None:
    legacy = ReplayResult(units=[_full_unit("1")], tier_used="TIER_4_LLM_API", llm_cost_usd=0.01, llm_calls=1)
    universe = ReplayResult(units=[_full_unit("1")], tier_used="TIER_4_UNIVERSE", llm_cost_usd=0.01, llm_calls=1)
    c = compare(legacy, universe, canonical_id="X", bucket="tier4_llm")
    assert c.unit_count_delta == 0
    assert c.unit_set_overlap_jaccard == 1.0
    assert c.regression_flag is False
    assert c.win_flag is False
    assert all(abs(v) < 1e-6 for v in c.field_coverage_delta.values())


def test_compare_universe_strict_win_flagged() -> None:
    legacy = ReplayResult(units=[_minimum_only_unit("1")], tier_used="TIER_4_LLM_API")
    universe = ReplayResult(units=[_full_unit("1"), _full_unit("2")], tier_used="TIER_4_UNIVERSE")
    c = compare(legacy, universe, canonical_id="X", bucket="failed_no_data")
    assert c.unit_count_delta == 1
    assert c.win_flag is True
    assert c.field_coverage_delta["rent"] > 0
    assert c.field_coverage_delta["available_date"] > 0


def test_compare_regression_flagged_when_universe_empty() -> None:
    legacy = ReplayResult(units=[_full_unit("1")], tier_used="TIER_4_LLM_API")
    universe = ReplayResult(units=[], tier_used="TIER_4_UNIVERSE", errors=["no-units"])
    c = compare(legacy, universe, canonical_id="X", bucket="tier4_llm")
    assert c.regression_flag is True
    assert c.win_flag is False
    assert c.unit_count_delta == -1


def test_compare_quality_gate_flips_labelled() -> None:
    legacy = ReplayResult(units=[_minimum_only_unit("1")], tier_used="TIER_4_LLM_API")
    universe = ReplayResult(units=[_full_unit("1")], tier_used="TIER_4_UNIVERSE")
    c = compare(legacy, universe, canonical_id="X", bucket="tier4_llm")
    assert c.quality_gate_important_flip == "gain"
    assert c.quality_gate_minimum_flip == "unchanged"


def test_compare_jaccard_partial_overlap() -> None:
    legacy = ReplayResult(units=[_full_unit("1"), _full_unit("2")], tier_used="x")
    universe = ReplayResult(units=[_full_unit("2"), _full_unit("3")], tier_used="x")
    c = compare(legacy, universe, canonical_id="X", bucket="tier4_llm")
    assert abs(c.unit_set_overlap_jaccard - 1.0 / 3.0) < 1e-6


def test_summarise_empty_returns_no_promote() -> None:
    s = summarise([])
    assert s.n_compared == 0
    assert s.promote is False


def test_summarise_promotes_when_thresholds_met() -> None:
    comps = [
        compare(
            ReplayResult(units=[_minimum_only_unit(str(i))], tier_used="legacy"),
            ReplayResult(units=[_full_unit(str(i))], tier_used="universe"),
            canonical_id=f"P-{i}",
            bucket="failed_no_data",
        )
        for i in range(20)
    ]
    s = summarise(comps)
    assert s.n_regressions == 0
    assert s.n_wins == 20
    assert s.promote is True


def test_summarise_holds_when_regressions_high() -> None:
    comps = [
        compare(
            ReplayResult(units=[_full_unit(str(i))], tier_used="legacy"),
            ReplayResult(units=[], tier_used="universe"),
            canonical_id=f"P-{i}",
            bucket="tier4_llm",
        )
        for i in range(5)
    ] + [
        compare(
            ReplayResult(units=[_full_unit(str(i))], tier_used="legacy"),
            ReplayResult(units=[_full_unit(str(i))], tier_used="universe"),
            canonical_id=f"Q-{i}",
            bucket="tier4_llm",
        )
        for i in range(5)
    ]
    s = summarise(comps)
    assert s.n_regressions == 5
    assert s.promote is False
    assert "regression_rate" in s.promote_reason


def test_summarise_holds_when_cost_delta_too_high() -> None:
    comps = [
        compare(
            ReplayResult(units=[_full_unit(str(i))], tier_used="legacy", llm_cost_usd=0.01),
            ReplayResult(units=[_full_unit(str(i))], tier_used="universe", llm_cost_usd=1.0),
            canonical_id=f"P-{i}",
            bucket="tier4_llm",
        )
        for i in range(10)
    ]
    s = summarise(comps)
    assert s.promote is False
    assert "avg_cost_delta" in s.promote_reason


def test_summarise_per_bucket_breakdown() -> None:
    comps = [
        compare(
            ReplayResult(units=[_minimum_only_unit("1")], tier_used="legacy"),
            ReplayResult(units=[_full_unit("1")], tier_used="universe"),
            canonical_id="A",
            bucket="failed_no_data",
        ),
        compare(
            ReplayResult(units=[_full_unit("2")], tier_used="legacy"),
            ReplayResult(units=[_full_unit("2")], tier_used="universe"),
            canonical_id="B",
            bucket="tier4_llm",
        ),
    ]
    s = summarise(comps)
    assert "failed_no_data" in s.by_bucket
    assert "tier4_llm" in s.by_bucket
    assert s.by_bucket["failed_no_data"]["wins"] == 1
    assert s.by_bucket["tier4_llm"]["wins"] == 0


def test_comparison_to_dict_round_trips_through_json() -> None:
    legacy = ReplayResult(units=[_full_unit("1")], tier_used="legacy", llm_cost_usd=0.01)
    universe = ReplayResult(units=[_full_unit("1")], tier_used="universe", llm_cost_usd=0.02)
    c = compare(legacy, universe, canonical_id="X", bucket="tier4_llm")
    payload = json.dumps(c.to_dict())
    parsed = json.loads(payload)
    assert parsed["canonical_id"] == "X"
    assert parsed["bucket"] == "tier4_llm"
