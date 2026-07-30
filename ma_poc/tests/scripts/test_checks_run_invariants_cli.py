"""The full-run invariant CLI, and the scope labelling that makes silence readable.

Two things are under test, and they exist for the same reason.

`run_jugnu` calls `_emit_run_invariant_issues` per shard. Production shards a run
across 100 Cloud Run tasks of ~50 properties, so that call compares ~50
properties out of 4,982. Replaying the 22 known collision groups from
run-2026-07-27-full-0d54ca7 against that run's real shard assignment, a
per-shard check catches 1 of 22 groups and surfaces 76 of 3,869 duplicate rows.
The cross-run half is worse: only PROFILE_GCS_PREFIX is synced to a task, never
prior `runs/`, so it never executes at all.

So: (1) a CLI that reads the ASSEMBLED run and therefore actually has coverage,
and (2) a scope record on the per-shard path, because the real hazard is not the
narrow scope — it is an empty issues.jsonl being read as a clean run.

The distinction a skipped check must never blur is SKIPPED vs CLEAN. Several
tests below exist only to pin that down; a check that reports "nothing found"
when it did not look is the failure mode this whole file is defending against.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ma_poc.scripts.checks.run_invariants import (
    _dedupe_by_id,
    load_run_properties,
    main,
)


def _rows(n: int, *, base: int = 0) -> list[dict[str, Any]]:
    return [
        {"area": 600 + base + i, "rent_low": 1200 + base + i, "rent_high": 1200 + base + i, "beds": 1}
        for i in range(n)
    ]


def _prop(pid: str, rows: list[dict[str, Any]], *, name: str = "P", pms: str = "appfolio") -> dict[str, Any]:
    return {
        "apartment_id": pid,
        "proj_name": name,
        "units": rows,
        "_meta": {"provenance": {"detected_pms": pms}},
    }


def _write_shards(run_dir: Path, shards: list[list[dict[str, Any]]]) -> None:
    """Lay out a collected production run: shard_N/properties.json."""
    for i, props in enumerate(shards):
        d = run_dir / f"shard_{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "properties.json").write_text(json.dumps(props), encoding="utf-8")


class TestLoadRunProperties:
    """Reading nothing is the dangerous failure: it prints a clean bill of health."""

    def test_unions_every_shard(self, tmp_path: Path) -> None:
        run = tmp_path / "2026-07-30"
        _write_shards(run, [[_prop("1", _rows(2))], [_prop("2", _rows(2))], [_prop("3", _rows(2))]])
        props, sources = load_run_properties(run)
        assert {str(p["apartment_id"]) for p in props} == {"1", "2", "3"}
        assert len(sources) == 3

    def test_reads_the_unsharded_layout(self, tmp_path: Path) -> None:
        """A single-process or local run writes properties.json directly."""
        run = tmp_path / "2026-07-30"
        run.mkdir(parents=True)
        (run / "properties.json").write_text(json.dumps([_prop("1", _rows(2))]), encoding="utf-8")
        props, sources = load_run_properties(run)
        assert len(props) == 1 and sources == ["properties.json"]

    def test_reads_the_flat_shard_json_layout(self, tmp_path: Path) -> None:
        """The flattened shape: shard_*.json at the run root, no subdir."""
        run = tmp_path / "2026-07-30"
        run.mkdir(parents=True)
        (run / "shard_0.json").write_text(json.dumps([_prop("1", _rows(2))]), encoding="utf-8")
        (run / "shard_1.json").write_text(json.dumps([_prop("2", _rows(2))]), encoding="utf-8")
        props, sources = load_run_properties(run)
        assert {str(p["apartment_id"]) for p in props} == {"1", "2"}
        assert len(sources) == 2

    def test_falls_back_to_a_recursive_search(self, tmp_path: Path) -> None:
        """An unexpected nesting depth must degrade to slow, not to silent."""
        run = tmp_path / "2026-07-30"
        deep = run / "collected" / "task-7"
        deep.mkdir(parents=True)
        (deep / "properties.json").write_text(json.dumps([_prop("1", _rows(2))]), encoding="utf-8")
        props, _ = load_run_properties(run)
        assert len(props) == 1, "a nested layout read as an empty run"

    def test_an_unreadable_shard_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        """One corrupt shard must not cost the other 99."""
        run = tmp_path / "2026-07-30"
        _write_shards(run, [[_prop("1", _rows(2))], [_prop("2", _rows(2))]])
        (run / "shard_1" / "properties.json").write_text("{not json", encoding="utf-8")
        props, sources = load_run_properties(run)
        assert len(props) == 1 and len(sources) == 1

    def test_a_shard_holding_a_non_list_is_ignored(self, tmp_path: Path) -> None:
        run = tmp_path / "2026-07-30"
        _write_shards(run, [[_prop("1", _rows(2))]])
        (run / "shard_9").mkdir()
        (run / "shard_9" / "properties.json").write_text('{"oops": true}', encoding="utf-8")
        props, _ = load_run_properties(run)
        assert len(props) == 1


class TestDedupe:
    def test_a_property_present_twice_does_not_collide_with_itself(self) -> None:
        """A retry shard would otherwise manufacture a finding on every property.

        That is the fastest way to get a real detector muted, so it is pinned.
        """
        shared = _rows(5)
        deduped = _dedupe_by_id([_prop("1", list(shared)), _prop("1", list(shared))])
        assert len(deduped) == 1

    def test_distinct_properties_are_both_kept(self) -> None:
        assert len(_dedupe_by_id([_prop("1", _rows(3)), _prop("2", _rows(3))])) == 2

    def test_a_record_with_no_id_is_not_merged_away(self) -> None:
        """Falling back to a constant key would silently drop real properties."""
        assert len(_dedupe_by_id([{"units": _rows(3)}, {"units": _rows(3)}])) == 2


class TestFindsWhatPerShardCannot:
    """The whole point: a collision SPLIT ACROSS SHARDS must be found."""

    def test_a_collision_spread_over_two_shards_is_caught(self, tmp_path: Path, capsys: Any) -> None:
        run = tmp_path / "2026-07-30"
        shared = _rows(6)
        _write_shards(
            run,
            [[_prop("278139", list(shared), name="Redwood Brunswick", pms="funnel")],
             [_prop("77994", list(shared), name="Redwood Sugarcreek", pms="rentcafe")]],
        )
        assert main(["--run-dir", str(run)]) == 2
        out = capsys.readouterr().out
        assert "1 group(s)" in out
        assert "Redwood Brunswick" in out and "Redwood Sugarcreek" in out

    def test_differing_detection_is_surfaced(self, tmp_path: Path, capsys: Any) -> None:
        run = tmp_path / "2026-07-30"
        shared = _rows(6)
        _write_shards(
            run,
            [[_prop("1", list(shared), pms="funnel")], [_prop("2", list(shared), pms="rentcafe")]],
        )
        main(["--run-dir", str(run)])
        assert "YES" in capsys.readouterr().out, "the strongest tell was not shown"

    def test_duplicate_row_count_is_reported(self, tmp_path: Path, capsys: Any) -> None:
        """The number that sizes the defect: rows x (members - 1)."""
        run = tmp_path / "2026-07-30"
        shared = _rows(10)
        _write_shards(run, [[_prop(str(i), list(shared))] for i in range(3)])
        main(["--run-dir", str(run)])
        out = capsys.readouterr().out
        assert "20 duplicate rows" in out, out


class TestExitCodes:
    """0 clean · 1 could not run · 2 findings — the checks/ convention."""

    def test_clean_run_exits_zero(self, tmp_path: Path) -> None:
        run = tmp_path / "2026-07-30"
        _write_shards(run, [[_prop("1", _rows(4))], [_prop("2", _rows(4, base=500))]])
        assert main(["--run-dir", str(run)]) == 0

    def test_findings_exit_two(self, tmp_path: Path) -> None:
        run = tmp_path / "2026-07-30"
        shared = _rows(5)
        _write_shards(run, [[_prop("1", list(shared))], [_prop("2", list(shared))]])
        assert main(["--run-dir", str(run)]) == 2

    def test_missing_directory_exits_one(self, tmp_path: Path) -> None:
        assert main(["--run-dir", str(tmp_path / "nope")]) == 1

    def test_no_properties_exits_one_not_zero(self, tmp_path: Path) -> None:
        """An empty run must not be reported as a clean run."""
        run = tmp_path / "2026-07-30"
        run.mkdir(parents=True)
        assert main(["--run-dir", str(run)]) == 1

    def test_missing_prior_directory_exits_one(self, tmp_path: Path) -> None:
        run = tmp_path / "2026-07-30"
        _write_shards(run, [[_prop("1", _rows(4))]])
        assert main(["--run-dir", str(run), "--prior-run-dir", str(tmp_path / "nope")]) == 1


class TestSkippedIsNotClean:
    """A check that did not look must never read as a check that found nothing."""

    def test_drift_without_a_prior_run_says_skipped(self, tmp_path: Path, capsys: Any) -> None:
        run = tmp_path / "2026-07-30"
        _write_shards(run, [[_prop("1", _rows(4))]])
        main(["--run-dir", str(run)])
        out = capsys.readouterr().out
        assert "SKIPPED" in out, "a check that never ran printed as clean"
        assert "envelope drift NOT checked" in out

    def test_an_empty_prior_run_is_skipped_not_clean(self, tmp_path: Path, capsys: Any) -> None:
        run = tmp_path / "2026-07-30"
        _write_shards(run, [[_prop("1", _rows(4))]])
        prior = tmp_path / "2026-07-29"
        _write_shards(prior, [[]])
        main(["--run-dir", str(run), "--prior-run-dir", str(prior)])
        assert "SKIPPED" in capsys.readouterr().out

    def test_drift_that_ran_and_found_nothing_says_none(self, tmp_path: Path, capsys: Any) -> None:
        """The negative control for the two tests above."""
        run = tmp_path / "2026-07-30"
        rows = _rows(4)
        _write_shards(run, [[_prop("1", list(rows))]])
        prior = tmp_path / "2026-07-29"
        _write_shards(prior, [[_prop("1", list(rows))]])
        main(["--run-dir", str(run), "--prior-run-dir", str(prior)])
        out = capsys.readouterr().out
        assert "SKIPPED" not in out
        assert "none — compared against 1 prior-run properties" in out


class TestDriftHalf:
    def test_a_narrowed_envelope_is_reported(self, tmp_path: Path, capsys: Any) -> None:
        run = tmp_path / "2026-07-30"
        _write_shards(run, [[_prop("222727", [{"area": 618, "rent_high": 1351, "rent_low": 1351, "beds": 1},
                                              {"area": 1005, "rent_high": 1472, "rent_low": 1472, "beds": 1}])]])
        prior = tmp_path / "2026-07-29"
        _write_shards(prior, [[_prop("222727", [{"area": 618, "rent_high": 1351, "rent_low": 1351, "beds": 1},
                                                {"area": 1239, "rent_high": 1952, "rent_low": 1952, "beds": 3}])]])
        assert main(["--run-dir", str(run), "--prior-run-dir", str(prior)]) == 2
        assert "rent_high_envelope_narrowed" in capsys.readouterr().out


class TestJsonMode:
    def test_json_is_parseable_and_labels_its_scope(self, tmp_path: Path, capsys: Any) -> None:
        run = tmp_path / "2026-07-30"
        shared = _rows(5)
        _write_shards(run, [[_prop("1", list(shared))], [_prop("2", list(shared))]])
        assert main(["--run-dir", str(run), "--json"]) == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["scope"] == "complete_run"
        assert payload["n_properties_compared"] == 2
        assert payload["n_files_read"] == 2
        assert payload["duplicate_rows"] == 5
        assert payload["envelope_drift_checked"] is False

    def test_json_marks_drift_checked_when_it_ran(self, tmp_path: Path, capsys: Any) -> None:
        run = tmp_path / "2026-07-30"
        rows = _rows(4)
        _write_shards(run, [[_prop("1", list(rows))]])
        prior = tmp_path / "2026-07-29"
        _write_shards(prior, [[_prop("1", list(rows))]])
        main(["--run-dir", str(run), "--prior-run-dir", str(prior), "--json"])
        assert json.loads(capsys.readouterr().out)["envelope_drift_checked"] is True


class TestThresholdsAreExposed:
    def test_min_rows_raises_the_bar(self, tmp_path: Path) -> None:
        run = tmp_path / "2026-07-30"
        shared = _rows(4)
        _write_shards(run, [[_prop("1", list(shared))], [_prop("2", list(shared))]])
        assert main(["--run-dir", str(run)]) == 2
        assert main(["--run-dir", str(run), "--min-rows", "99"]) == 0

    def test_max_print_suppression_is_stated_not_silent(self, tmp_path: Path, capsys: Any) -> None:
        """A silent cap reads as 'that was everything'."""
        run = tmp_path / "2026-07-30"
        shards = []
        for g in range(3):
            shared = _rows(5, base=g * 1000)
            shards += [[_prop(f"{g}a", list(shared))], [_prop(f"{g}b", list(shared))]]
        _write_shards(run, shards)
        main(["--run-dir", str(run), "--max-print", "1"])
        assert "not shown (--max-print)" in capsys.readouterr().out


class TestScopeRecordOnThePerShardPath:
    """`_emit_run_invariant_issues` must always state what it compared.

    Without this, an empty issues.jsonl from a 50-property shard is
    indistinguishable from a clean 4,982-property run — which is exactly how
    3,869 duplicate rows shipped on 07-27 under a check that printed nothing.
    """

    @staticmethod
    def _issues(run_dir: Path) -> list[dict[str, Any]]:
        path = run_dir / "issues.jsonl"
        if not path.is_file():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def test_a_scope_record_is_emitted_on_a_clean_shard(self, tmp_path: Path) -> None:
        from ma_poc.scripts.runners.jugnu import _emit_run_invariant_issues

        run_dir = tmp_path / "runs" / "2026-07-30"
        run_dir.mkdir(parents=True)
        _emit_run_invariant_issues([_prop("1", _rows(4))], run_dir)
        scope = [i for i in self._issues(run_dir) if i["code"] == "RUN_INVARIANTS_SCOPE"]
        assert scope, "a clean shard wrote nothing at all — silence reads as a clean run"
        d = scope[0]["details"]
        assert d["n_properties_compared"] == 1
        assert d["identical_payload_checked"] is True
        assert d["envelope_drift_checked"] is False
        assert d["scope"] == "single_process"

    def test_the_scope_record_names_the_full_run_command(self, tmp_path: Path) -> None:
        """The record has to tell a reader how to get real coverage."""
        from ma_poc.scripts.runners.jugnu import _emit_run_invariant_issues

        run_dir = tmp_path / "runs" / "2026-07-30"
        run_dir.mkdir(parents=True)
        _emit_run_invariant_issues([_prop("1", _rows(4))], run_dir)
        scope = [i for i in self._issues(run_dir) if i["code"] == "RUN_INVARIANTS_SCOPE"][0]
        assert "run_invariants" in scope["details"]["full_run_check"]

    def test_findings_also_carry_the_population(self, tmp_path: Path) -> None:
        """A consumer filtering by code sees only the finding lines."""
        from ma_poc.scripts.runners.jugnu import _emit_run_invariant_issues

        run_dir = tmp_path / "runs" / "2026-07-30"
        run_dir.mkdir(parents=True)
        shared = _rows(5)
        _emit_run_invariant_issues(
            [_prop("1", list(shared)), _prop("2", list(shared)), _prop("3", _rows(4, base=900))],
            run_dir,
        )
        found = [i for i in self._issues(run_dir) if i["code"] == "CROSS_PROPERTY_IDENTICAL_PAYLOAD"]
        assert found and found[0]["details"]["n_properties_compared"] == 3

    def test_scope_record_reports_drift_checked_when_a_prior_run_exists(self, tmp_path: Path) -> None:
        from ma_poc.scripts.runners.jugnu import _emit_run_invariant_issues

        runs = tmp_path / "runs"
        prior = runs / "2026-07-29"
        prior.mkdir(parents=True)
        (prior / "properties.json").write_text(json.dumps([_prop("1", _rows(4))]), encoding="utf-8")
        cur = runs / "2026-07-30"
        cur.mkdir(parents=True)
        _emit_run_invariant_issues([_prop("1", _rows(4))], cur)
        scope = [i for i in self._issues(cur) if i["code"] == "RUN_INVARIANTS_SCOPE"][0]
        assert scope["details"]["envelope_drift_checked"] is True
        assert scope["details"]["n_prior_properties"] == 1

    def test_the_scope_record_is_informational_not_a_warning(self, tmp_path: Path) -> None:
        """It fires on every run; at WARNING it would train people to ignore it."""
        from ma_poc.scripts.runners.jugnu import _emit_run_invariant_issues

        run_dir = tmp_path / "runs" / "2026-07-30"
        run_dir.mkdir(parents=True)
        _emit_run_invariant_issues([_prop("1", _rows(4))], run_dir)
        scope = [i for i in self._issues(run_dir) if i["code"] == "RUN_INVARIANTS_SCOPE"][0]
        assert scope["severity"] == "INFO"

    @pytest.mark.parametrize(
        ("props", "run_dir_given"),
        [([], True), ([_prop("1", _rows(4))], False)],
        ids=["no-properties", "no-run-dir"],
    )
    def test_degenerate_inputs_still_write_nothing(
        self, tmp_path: Path, props: list[dict[str, Any]], run_dir_given: bool
    ) -> None:
        """No scope record when there was no run to scope."""
        from ma_poc.scripts.runners.jugnu import _emit_run_invariant_issues

        run_dir = tmp_path / "runs" / "2026-07-30"
        run_dir.mkdir(parents=True)
        _emit_run_invariant_issues(props, run_dir if run_dir_given else None)
        assert self._issues(run_dir) == []
