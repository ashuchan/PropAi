"""A zero-row result must never suppress a non-zero one — locked at the source.

This is the invariant #80 feared and #82 investigated. #82's worry, quoted:
"Flipping the [detector] gate alone would route Carraway-shape and
Gull-Prairie-shape properties to an adapter that parses ZERO rows for them — and
they currently DO ship plan rows ... a zero-row Tier-1 result outranks a
non-zero fallback ... a guaranteed net LOSS." #82 then REFUTED that this happens
today: the code guards it at three selection seams in ``scrape``. But nothing
LOCKED those guards, so the plan→unit re-routing work (#80/#50/#85/#89) could
silently reintroduce the regression by editing one ``if``.

So this is a source-contract test (the convention this codebase already uses for
scraper hooks — see test_path_b_retry_telemetry's "checked for drift via the
source-grep contract test"). It asserts, structurally, that each of the three
seams keeps its non-empty guard:

  Seam 1  generic fallback (~2931): ``adapter_result = fallback_result`` only
          inside an ``if`` testing ``fallback_result.units``.
  Seam 2  retry win condition (~1983): ``_retry_win_condition`` requires
          ``res.units``.
  Seam 3  plan-level baseline restore (~2227): ``adapter_result =
          _baseline_result`` only inside an ``if`` testing
          ``_baseline_result.units`` (so a re-routed empty primary cannot lose
          the baseline plan rows).

If any assertion fails, a change removed a guard that prevents an empty result
from winning. That is the exact defect the plan-level recovery program must not
ship. Fix the code, not the test.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import ma_poc.pms.scraper as _scraper_mod

_SRC = Path(inspect.getsourcefile(_scraper_mod) or "").read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _func(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(_TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in scraper.py — update this guard")


def _test_refs_units_of(test: ast.expr, var: str) -> bool:
    """True if ``test`` references ``<var>.units`` anywhere."""
    return any(
        isinstance(n, ast.Attribute)
        and n.attr == "units"
        and isinstance(n.value, ast.Name)
        and n.value.id == var
        for n in ast.walk(test)
    )


def _body_assigns(body: list[ast.stmt], target: str, value: str) -> bool:
    """True if any statement in ``body`` is ``<target> = <value>``."""
    for stmt in body:
        for n in ast.walk(stmt):
            if (
                isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == target for t in n.targets)
                and isinstance(n.value, ast.Name)
                and n.value.id == value
            ):
                return True
    return False


def _guarded_assignment_exists(fn: ast.AST, guard_var: str, target: str, value: str) -> bool:
    """An ``if`` whose test references ``guard_var.units`` and whose body
    assigns ``target = value``. This is the "empty cannot win" shape."""
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.If)
            and _test_refs_units_of(node.test, guard_var)
            and _body_assigns(node.body, target, value)
        ):
            return True
    return False


class TestGenericFallbackSeam:
    """Seam 1 — the generic fallback replaces the primary ONLY when it has units."""

    def test_generic_fallback_is_guarded_by_units(self) -> None:
        scrape = _func("scrape")
        assert _guarded_assignment_exists(
            scrape, "fallback_result", "adapter_result", "fallback_result"
        ), (
            "The generic fallback no longer promotes `fallback_result` behind an "
            "`if fallback_result.units:` guard. Without it, a zero-row generic "
            "fallback could overwrite a non-empty adapter_result — the winner-"
            "selection regression #82 exists to prevent."
        )


class TestRetryWinConditionSeam:
    """Seam 2 — a retry winner must have units."""

    def test_retry_win_condition_requires_units(self) -> None:
        win = _func("_retry_win_condition")
        refs_units = any(
            isinstance(n, ast.Attribute)
            and n.attr == "units"
            and isinstance(n.value, ast.Name)
            and n.value.id == "res"
            for n in ast.walk(win)
        )
        assert refs_units, (
            "_retry_win_condition no longer requires `res.units`, so a retry "
            "could win with zero rows and discard a non-empty baseline."
        )


class TestBaselineRestoreSeam:
    """Seam 3 — when every retry loses, a plan-level baseline is restored, not dropped."""

    def test_baseline_restore_is_guarded_by_units(self) -> None:
        scrape = _func("scrape")
        assert _guarded_assignment_exists(
            scrape, "_baseline_result", "adapter_result", "_baseline_result"
        ), (
            "The plan-level baseline restore no longer checks "
            "`_baseline_result.units`. A property re-routed to an adapter that "
            "returns zero must fall back to its baseline plan rows — dropping "
            "them is the net-loss #80 warned about."
        )


class TestNegativeControls:
    """The helper must actually discriminate — or the asserts above are vacuous."""

    def test_helper_rejects_an_unguarded_assignment(self) -> None:
        tree = ast.parse("def f():\n    adapter_result = fallback_result\n")
        fn = tree.body[0]
        assert not _guarded_assignment_exists(
            fn, "fallback_result", "adapter_result", "fallback_result"
        )

    def test_helper_accepts_a_guarded_assignment(self) -> None:
        tree = ast.parse(
            "def f():\n"
            "    if fallback_result.units:\n"
            "        adapter_result = fallback_result\n"
        )
        fn = tree.body[0]
        assert _guarded_assignment_exists(
            fn, "fallback_result", "adapter_result", "fallback_result"
        )

    def test_helper_rejects_wrong_guard_variable(self) -> None:
        """Guarding on the WRONG object's units must not count."""
        tree = ast.parse(
            "def f():\n"
            "    if other.units:\n"
            "        adapter_result = fallback_result\n"
        )
        fn = tree.body[0]
        assert not _guarded_assignment_exists(
            fn, "fallback_result", "adapter_result", "fallback_result"
        )
