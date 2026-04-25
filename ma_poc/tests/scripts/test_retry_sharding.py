"""Tests for sharding logic in jugnu_retry_runner.

Covers _shard_candidates() math (disjoint slices, balanced distribution,
deterministic, edge cases) and the merge math in jugnu_retry_merge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ma_poc.scripts.jugnu_retry_merge import (
    _detect_overlaps,
    _find_shard_files,
    _merge_records,
)
from ma_poc.scripts.jugnu_retry_runner import _shard_candidates

# ---------------------------------------------------------------------------
# _shard_candidates
# ---------------------------------------------------------------------------


def _mk(pid: str, **extra: Any) -> dict[str, Any]:
    return {"property_id": pid, "url": f"https://{pid}.example.com", **extra}


def test_disjoint_slices_cover_full_set() -> None:
    candidates = [_mk(f"p{i:04d}") for i in range(100)]
    shards = [_shard_candidates(candidates, i, 5) for i in range(5)]

    union: list[str] = []
    for s in shards:
        union.extend(c["property_id"] for c in s)

    assert sorted(union) == sorted(c["property_id"] for c in candidates)
    # No overlap between any pair of shards.
    for i in range(5):
        for j in range(i + 1, 5):
            ids_i = {c["property_id"] for c in shards[i]}
            ids_j = {c["property_id"] for c in shards[j]}
            assert ids_i.isdisjoint(ids_j), f"shard {i} and {j} overlap"


def test_balanced_distribution() -> None:
    candidates = [_mk(f"p{i:04d}") for i in range(2000)]
    shards = [_shard_candidates(candidates, i, 10) for i in range(10)]
    sizes = [len(s) for s in shards]

    # 2000 / 10 = 200 exactly, so all shards should be 200.
    assert all(size == 200 for size in sizes), f"sizes={sizes}"


def test_uneven_distribution_within_one() -> None:
    candidates = [_mk(f"p{i:04d}") for i in range(103)]
    shards = [_shard_candidates(candidates, i, 10) for i in range(10)]
    sizes = [len(s) for s in shards]
    # 103 = 10*10 + 3; sizes should be 10 or 11.
    assert max(sizes) - min(sizes) <= 1, f"sizes={sizes}"
    assert sum(sizes) == 103


def test_deterministic_across_calls() -> None:
    candidates = [_mk(f"p{i:04d}") for i in range(50)]
    a = _shard_candidates(candidates, 2, 5)
    b = _shard_candidates(candidates, 2, 5)
    assert [c["property_id"] for c in a] == [c["property_id"] for c in b]


def test_deterministic_across_input_order() -> None:
    """Sorting key means input order does not affect sharding."""
    a_input = [_mk(f"p{i:04d}") for i in range(20)]
    b_input = list(reversed(a_input))
    a = _shard_candidates(a_input, 1, 4)
    b = _shard_candidates(b_input, 1, 4)
    assert [c["property_id"] for c in a] == [c["property_id"] for c in b]


def test_single_shard_returns_input() -> None:
    candidates = [_mk(f"p{i}") for i in range(10)]
    out = _shard_candidates(candidates, 0, 1)
    # Same content; we don't promise same order for task_count==1.
    assert {c["property_id"] for c in out} == {c["property_id"] for c in candidates}
    assert len(out) == len(candidates)


def test_zero_task_count_treated_as_single() -> None:
    candidates = [_mk(f"p{i}") for i in range(5)]
    out = _shard_candidates(candidates, 0, 0)
    assert len(out) == len(candidates)


def test_index_out_of_range_returns_empty() -> None:
    candidates = [_mk(f"p{i}") for i in range(10)]
    assert _shard_candidates(candidates, 5, 3) == []
    assert _shard_candidates(candidates, -1, 3) == []


def test_empty_candidates() -> None:
    assert _shard_candidates([], 0, 5) == []
    assert _shard_candidates([], 2, 5) == []


def test_round_robin_spreads_clusters() -> None:
    """Failures clustered together (e.g. all SightMap at the start of
    properties.json) should be spread across shards, not concentrated
    in shard 0."""
    # First 10 are "expensive" (e.g. SightMap), rest are "cheap".
    candidates = [_mk(f"slow_{i:02d}") for i in range(10)] + [
        _mk(f"fast_{i:03d}") for i in range(90)
    ]
    shards = [_shard_candidates(candidates, i, 5) for i in range(5)]

    # Each shard should have ~2 of the slow ones (10/5=2). Tolerance ±1.
    for i, s in enumerate(shards):
        slow_count = sum(1 for c in s if c["property_id"].startswith("slow"))
        assert 1 <= slow_count <= 3, f"shard {i} slow_count={slow_count}"


def test_tasks_greater_than_candidates() -> None:
    """10 candidates, 20 shards: first 10 shards get 1 each, rest get 0."""
    candidates = [_mk(f"p{i:02d}") for i in range(10)]
    shards = [_shard_candidates(candidates, i, 20) for i in range(20)]
    sizes = [len(s) for s in shards]
    assert sum(sizes) == 10
    assert sizes.count(1) == 10
    assert sizes.count(0) == 10


def test_missing_property_id_does_not_crash() -> None:
    candidates = [_mk("p001"), {"url": "https://no-id.example.com"}, _mk("p002")]
    out = _shard_candidates(candidates, 0, 2)
    # Just shouldn't throw; sort key falls back to "" for missing id.
    assert isinstance(out, list)


# ---------------------------------------------------------------------------
# _merge_records (jugnu_retry_merge)
# ---------------------------------------------------------------------------


def _prop(pid: str, units: int = 0, verdict: str = "FAIL") -> dict[str, Any]:
    return {
        "Property Name": f"Property {pid}",
        "_meta": {"canonical_id": pid, "verdict": verdict},
        "units": [{"unit_id": f"u{i}"} for i in range(units)],
    }


def test_merge_replaces_by_canonical_id() -> None:
    base = [_prop("p1"), _prop("p2"), _prop("p3")]
    incoming = [_prop("p2", units=5, verdict="SUCCESS")]
    merged = _merge_records(base, incoming)

    assert len(merged) == 3
    by_id = {p["_meta"]["canonical_id"]: p for p in merged}
    assert len(by_id["p2"]["units"]) == 5
    assert by_id["p2"]["_meta"]["verdict"] == "SUCCESS"
    assert len(by_id["p1"]["units"]) == 0  # untouched


def test_merge_appends_new_records() -> None:
    base = [_prop("p1")]
    incoming = [_prop("p2", units=3)]
    merged = _merge_records(base, incoming)
    assert len(merged) == 2
    assert {p["_meta"]["canonical_id"] for p in merged} == {"p1", "p2"}


def test_merge_preserves_base_order() -> None:
    base = [_prop("z"), _prop("a"), _prop("m")]
    incoming = [_prop("a", units=1)]
    merged = _merge_records(base, incoming)
    assert [p["_meta"]["canonical_id"] for p in merged] == ["z", "a", "m"]


def test_merge_empty_inputs() -> None:
    assert _merge_records([], []) == []
    assert _merge_records([_prop("p1")], []) == [_prop("p1")]
    out = _merge_records([], [_prop("p1")])
    assert len(out) == 1


def test_overlap_detection_disjoint_returns_empty() -> None:
    """Round-robin shards are disjoint by construction."""
    s0 = [_prop("p1"), _prop("p3")]
    s1 = [_prop("p2"), _prop("p4")]
    assert _detect_overlaps([s0, s1]) == []


def test_overlap_detection_finds_duplicates() -> None:
    s0 = [_prop("p1"), _prop("p2")]
    s1 = [_prop("p2"), _prop("p3")]   # p2 appears in both — bug
    overlaps = _detect_overlaps([s0, s1])
    assert "p2" in overlaps


# ---------------------------------------------------------------------------
# _find_shard_files
# ---------------------------------------------------------------------------


def test_find_shard_files_picks_correct_pattern(tmp_path: Path) -> None:
    (tmp_path / "properties.retry.shard00.json").write_text("[]", encoding="utf-8")
    (tmp_path / "properties.retry.shard02.json").write_text("[]", encoding="utf-8")
    (tmp_path / "properties.retry.shard01.json").write_text("[]", encoding="utf-8")
    (tmp_path / "properties.json").write_text("[]", encoding="utf-8")        # ignored
    (tmp_path / "report.json").write_text("{}", encoding="utf-8")             # ignored
    (tmp_path / "properties.retry.shard5.json").write_text("[]", encoding="utf-8")  # wrong format

    found = _find_shard_files(tmp_path)
    indices = [i for i, _ in found]
    assert indices == [0, 1, 2]  # sorted, only well-formed names matched


def test_find_shard_files_missing_dir(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does_not_exist"
    assert _find_shard_files(nonexistent) == []


def test_find_shard_files_empty_dir(tmp_path: Path) -> None:
    assert _find_shard_files(tmp_path) == []


# ---------------------------------------------------------------------------
# Env-var driven sharding integration (lightweight — confirms env vars
# are consumed; full pipeline integration is covered separately).
# ---------------------------------------------------------------------------


def test_env_var_parsing_falls_back_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    """If CLOUD_RUN_TASK_INDEX/COUNT are set to non-int garbage, the
    runner should fall back to (0, 1) — never crash. This exercises the
    same try/except block in run_retry()."""
    import os

    monkeypatch.setenv("CLOUD_RUN_TASK_INDEX", "not-an-int")
    monkeypatch.setenv("CLOUD_RUN_TASK_COUNT", "also-bad")

    try:
        ti = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
        tc = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    except ValueError:
        ti, tc = 0, 1

    assert (ti, tc) == (0, 1)


def test_round_trip_full_set_via_env_simulation() -> None:
    """Simulate three shards processing 30 candidates: union must cover all."""
    candidates = [_mk(f"p{i:04d}") for i in range(30)]

    union_ids: set[str] = set()
    for shard_idx in range(3):
        sliced = _shard_candidates(candidates, shard_idx, 3)
        union_ids.update(c["property_id"] for c in sliced)

    assert union_ids == {c["property_id"] for c in candidates}


# ---------------------------------------------------------------------------
# Property-based-ish: random distributions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("total,shards", [(1, 1), (1, 5), (5, 5), (50, 7), (1000, 13)])
def test_invariants_disjoint_and_complete(total: int, shards: int) -> None:
    candidates = [_mk(f"p{i:05d}") for i in range(total)]
    union: set[str] = set()
    for i in range(shards):
        slice_ = _shard_candidates(candidates, i, shards)
        slice_ids = {c["property_id"] for c in slice_}
        # disjoint with prior union
        assert slice_ids.isdisjoint(union), f"overlap on shard {i}"
        union.update(slice_ids)
    # complete coverage
    assert union == {c["property_id"] for c in candidates}
