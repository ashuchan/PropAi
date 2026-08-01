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

  Seam 1  generic fallback: a canonical unit roster always wins; plan-only
          fallback can replace only a truly empty primary.
  Seam 2  retry win condition (~1983): ``_retry_win_condition`` requires
          ``res.units``.
  Seam 3  retry fallback: empty results cannot enter the plan fallback set,
          while channel-split plan summaries remain eligible evidence.

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
    """Seam 1 — generic fallback must be a strict semantic improvement."""

    def test_generic_fallback_adoption_matrix(self) -> None:
        from ma_poc.pms.adapters.base import AdapterResult
        from ma_poc.pms.scraper import _should_adopt_generic_fallback

        empty = AdapterResult()
        plan = AdapterResult(plan_summaries=[{"floor_plan_name": "A1"}])
        other_plan = AdapterResult(units=[{"floor_plan_name": "B1"}])
        units = AdapterResult(units=[{"unit_number": "204", "rent_low": 1500}])

        assert not _should_adopt_generic_fallback(plan, empty)
        assert not _should_adopt_generic_fallback(plan, other_plan)
        assert _should_adopt_generic_fallback(empty, other_plan)
        assert _should_adopt_generic_fallback(plan, units)
        assert not _should_adopt_generic_fallback(empty, empty)

    def test_generic_fallback_cannot_relabel_entrata_plan_ids_as_units(self) -> None:
        """Andante/Brownstone: the same plan PKs are not apartments."""
        from ma_poc.pms.adapters.base import AdapterResult
        from ma_poc.pms.scraper import _should_adopt_generic_fallback

        primary = AdapterResult(
            plan_summaries=[
                {
                    "floor_plan_name": "Pisa",
                    "bedrooms": "1",
                    "sqft": "689",
                    "market_rent_low": 1405,
                    "source_ids": {"entrata_fpid": "525217"},
                },
                {
                    "floor_plan_name": "Milan",
                    "bedrooms": "1",
                    "sqft": "742",
                    "market_rent_low": 1355,
                    "source_ids": {"entrata_fpid": "525219"},
                },
            ]
        )
        generic = AdapterResult(
            units=[
                {
                    "unit_number": "525217",
                    "floor_plan_name": "Pisa",
                    "bedrooms": "1",
                    "sqft": "689",
                    "market_rent_low": 1405,
                },
                {
                    "unit_number": "525219",
                    "floor_plan_name": "Milan",
                    "bedrooms": "1",
                    "sqft": "742",
                    "market_rent_low": 1355,
                },
            ]
        )

        assert not _should_adopt_generic_fallback(primary, generic)

    def test_real_generic_unit_roster_still_improves_a_plan_catalogue(self) -> None:
        from ma_poc.pms.adapters.base import AdapterResult
        from ma_poc.pms.scraper import _should_adopt_generic_fallback

        primary = AdapterResult(
            plan_summaries=[
                {
                    "floor_plan_name": "Pisa",
                    "bedrooms": "1",
                    "sqft": "689",
                    "source_ids": {"entrata_fpid": "525217"},
                },
                {
                    "floor_plan_name": "Milan",
                    "bedrooms": "1",
                    "sqft": "742",
                    "source_ids": {"entrata_fpid": "525219"},
                },
            ]
        )
        real_units = AdapterResult(
            units=[
                {
                    "unit_number": "A-101",
                    "floor_plan_name": "Pisa",
                    "sqft": "689",
                    "market_rent_low": 1405,
                },
                {
                    "unit_number": "B-204",
                    "floor_plan_name": "Milan",
                    "sqft": "742",
                    "market_rent_low": 1355,
                },
            ]
        )

        assert _should_adopt_generic_fallback(primary, real_units)


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
    """Seam 3 — only genuine plan evidence can become the retry fallback."""

    def test_empty_result_cannot_become_plan_fallback(self) -> None:
        from ma_poc.pms.adapters.base import AdapterResult
        from ma_poc.pms.scraper import _retry_plan_rows

        assert _retry_plan_rows(AdapterResult(units=[], plan_summaries=[])) == []

        split_plan = {"floor_plan_name": "A1", "beds": 1, "sqft": 750}
        assert _retry_plan_rows(
            AdapterResult(units=[], plan_summaries=[split_plan])
        ) == [split_plan]


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
