"""The run-level invariants must actually fire, and must never cost us a run.

`run_invariants` is a pure library; these tests cover the WIRING — that the
runner calls it at end-of-run, records findings into `issues.jsonl`, discovers a
prior run for the cross-run half, and degrades safely when it cannot.

The safety property is load-bearing. These checks run after a run has already
succeeded, so a QC failure that propagated would destroy work that was otherwise
complete. `_emit_run_invariant_issues` therefore swallows everything, and that
behaviour is asserted rather than assumed.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from ma_poc.scripts.runners import jugnu as _jugnu_mod
from ma_poc.scripts.runners.jugnu import (
    _emit_run_invariant_issues,
    _find_prior_run_properties,
)

_RUNNER_SRC = Path(inspect.getsourcefile(_jugnu_mod) or "")


class TestTheRunnerActuallyCallsIt:
    """A library nobody calls is not a check.

    The tests below drive `_emit_run_invariant_issues` directly, so they ALL stay
    green if the call site disappears from `run_jugnu` — a mutation run proved
    exactly that. Driving the real end-of-run path would need a full scrape
    harness, so assert the wiring structurally instead.
    """

    @staticmethod
    def _run_jugnu() -> ast.AsyncFunctionDef | ast.FunctionDef:
        tree = ast.parse(_RUNNER_SRC.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "run_jugnu"
            ):
                return node
        raise AssertionError("run_jugnu not found — update this guard")

    def test_run_jugnu_invokes_the_invariants(self) -> None:
        called = {
            n.func.id
            for n in ast.walk(self._run_jugnu())
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "_emit_run_invariant_issues" in called, (
            "run_jugnu no longer calls _emit_run_invariant_issues, so the "
            "run-level invariants never fire in production. The unit tests in "
            "this file cannot catch that — they call the function directly."
        )

    def test_it_runs_before_the_report_is_built(self) -> None:
        """Findings must exist by the time the report is assembled.

        Ordering matters: issues.jsonl is a run artifact, and a consumer reading
        the report alongside it should not see a report built before QC ran.
        """
        fn = self._run_jugnu()
        lines = {
            n.func.id: n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert lines.get("_emit_run_invariant_issues", 10**9) < lines.get(
            "build_run_report", 0
        ), "invariants must be emitted before build_run_report"


def _prop(pid: str, rows: list[dict[str, Any]], *, name: str = "P") -> dict[str, Any]:
    return {
        "apartment_id": pid,
        "proj_name": name,
        "units": rows,
        "_meta": {"provenance": {"detected_pms": "appfolio"}},
    }


def _rows(n: int) -> list[dict[str, Any]]:
    return [{"area": 600 + i, "rent_low": 1200 + i, "rent_high": 1200 + i, "beds": 1} for i in range(n)]


def _issues(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "issues.jsonl"
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


class TestIdenticalPayloadWiring:
    def test_a_collision_is_recorded(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "runs" / "2026-07-29"
        run_dir.mkdir(parents=True)
        shared = _rows(4)
        _emit_run_invariant_issues(
            [_prop("1", list(shared), name="A"), _prop("2", list(shared), name="B")], run_dir
        )
        codes = [i["code"] for i in _issues(run_dir)]
        assert "CROSS_PROPERTY_IDENTICAL_PAYLOAD" in codes, codes

    def test_the_finding_names_every_property_involved(self, tmp_path: Path) -> None:
        """A finding that does not say WHICH properties is not actionable."""
        run_dir = tmp_path / "runs" / "2026-07-29"
        run_dir.mkdir(parents=True)
        shared = _rows(4)
        _emit_run_invariant_issues(
            [_prop("278139", list(shared)), _prop("77994", list(shared))], run_dir
        )
        found = [i for i in _issues(run_dir) if i["code"] == "CROSS_PROPERTY_IDENTICAL_PAYLOAD"]
        assert set(found[0]["details"]["property_ids"]) == {"278139", "77994"}
        assert found[0]["details"]["n_rows"] == 4

    def test_a_clean_run_writes_nothing(self, tmp_path: Path) -> None:
        """No noise on the happy path, or the file becomes unreadable."""
        run_dir = tmp_path / "runs" / "2026-07-29"
        run_dir.mkdir(parents=True)
        _emit_run_invariant_issues(
            [_prop("1", _rows(4)), _prop("2", _rows(9)[5:])], run_dir
        )
        assert _issues(run_dir) == []


class TestPriorRunDiscovery:
    def test_finds_the_most_recent_earlier_run(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        for day, pid in (("2026-07-26", "old"), ("2026-07-28", "recent")):
            d = runs / day
            d.mkdir(parents=True)
            (d / "properties.json").write_text(json.dumps([_prop(pid, _rows(2))]))
        cur = runs / "2026-07-29"
        cur.mkdir(parents=True)
        prior = _find_prior_run_properties(cur)
        assert prior and str(prior[0]["apartment_id"]) == "recent"

    def test_ignores_a_later_run(self, tmp_path: Path) -> None:
        """Lexical order is chronological here; a future dir is not a baseline."""
        runs = tmp_path / "runs"
        later = runs / "2026-07-31"
        later.mkdir(parents=True)
        (later / "properties.json").write_text(json.dumps([_prop("future", _rows(2))]))
        cur = runs / "2026-07-29"
        cur.mkdir(parents=True)
        assert _find_prior_run_properties(cur) is None

    def test_skips_an_empty_prior_run(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        empty = runs / "2026-07-28"
        empty.mkdir(parents=True)
        (empty / "properties.json").write_text("[]")
        good = runs / "2026-07-27"
        good.mkdir(parents=True)
        (good / "properties.json").write_text(json.dumps([_prop("good", _rows(2))]))
        cur = runs / "2026-07-29"
        cur.mkdir(parents=True)
        prior = _find_prior_run_properties(cur)
        assert prior and str(prior[0]["apartment_id"]) == "good"

    @pytest.mark.parametrize("layout", ["no-siblings", "unreadable"])
    def test_degrades_to_not_checked(self, tmp_path: Path, layout: str) -> None:
        """No baseline must mean 'not checked', never a false finding."""
        runs = tmp_path / "runs"
        cur = runs / "2026-07-29"
        cur.mkdir(parents=True)
        if layout == "unreadable":
            bad = runs / "2026-07-28"
            bad.mkdir(parents=True)
            (bad / "properties.json").write_text("{not json")
        assert _find_prior_run_properties(cur) is None


class TestEnvelopeDriftWiring:
    def test_drift_is_recorded_against_the_prior_run(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        prior = runs / "2026-07-28"
        prior.mkdir(parents=True)
        wide = [
            {"area": 618, "rent_low": 1351, "rent_high": 1351, "beds": 1},
            {"area": 1239, "rent_low": 1952, "rent_high": 1952, "beds": 3},
        ]
        (prior / "properties.json").write_text(json.dumps([_prop("222727", wide)]))
        cur = runs / "2026-07-29"
        cur.mkdir(parents=True)
        narrow = [
            {"area": 618, "rent_low": 1351, "rent_high": 1351, "beds": 1},
            {"area": 1005, "rent_low": 1472, "rent_high": 1472, "beds": 1},
        ]
        _emit_run_invariant_issues([_prop("222727", narrow)], cur)
        codes = [i["code"] for i in _issues(cur)]
        assert "PUBLISHED_ENVELOPE_DRIFT" in codes, codes


class TestQcNeverBreaksTheRun:
    """These run AFTER a successful run. A raise here would destroy that work."""

    def test_a_broken_check_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ma_poc.validation.run_invariants as ri

        def _boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("invariant exploded")

        monkeypatch.setattr(ri, "find_identical_payload_groups", _boom)
        run_dir = tmp_path / "runs" / "2026-07-29"
        run_dir.mkdir(parents=True)
        _emit_run_invariant_issues([_prop("1", _rows(4))], run_dir)  # must not raise

    @pytest.mark.parametrize(
        ("props", "run_dir_given"),
        [([], True), ([_prop("1", _rows(4))], False)],
        ids=["no-properties", "no-run-dir"],
    )
    def test_degenerate_inputs_are_no_ops(
        self, tmp_path: Path, props: list[dict[str, Any]], run_dir_given: bool
    ) -> None:
        run_dir = tmp_path / "runs" / "2026-07-29"
        run_dir.mkdir(parents=True)
        _emit_run_invariant_issues(props, run_dir if run_dir_given else None)
        assert _issues(run_dir) == []
