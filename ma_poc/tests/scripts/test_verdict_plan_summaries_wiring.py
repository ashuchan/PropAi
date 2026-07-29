"""The verdict call site must forward ``plan_summaries``, not just ``units``.

Why this is a source-level guard rather than a behavioural test
--------------------------------------------------------------
``reporting.verdict.compute()`` has accepted ``plan_summaries`` — and returned
``SUCCESS_PLAN_LEVEL`` for a non-empty value — since Stage 2 landed. The defect
this file guards was never in ``compute()``; it was that ``_process_property``
in ``scripts/runners/jugnu.py`` did not pass the argument. So every existing
``compute()`` unit test passed while production mislabelled real extractions.

``promote_verified_unit_rows`` (``pms/scraper.py``) admits a row into ``units``
only when it carries a native, non-surrogate apartment anchor, and MOVES every
unanchored plan row to ``result["plan_summaries"]`` (``scraper.py:3032``), which
the emitted record surfaces as ``floor_plans``. A plan-only property therefore
reaches the verdict with ``units == []`` and its data intact somewhere else.
Without the argument, ``compute()`` sees only the empty list and returns
``FAILED_NO_DATA`` / "no records extracted".

Measured on the 265-property dq29 canary before the fix: **37 of the 77
zero-unit properties** were this bug — data present in ``floor_plans``, verdict
``FAILED_NO_DATA``. Gold was unaffected (plan rows are never gold), but the
reported success rate collapses, which is exactly the kind of false signal that
gets read as "the scraper broke".

The invariant, stated so it survives refactors: a verdict computed from a
property's rows must consider BOTH channels those rows can land in. Any call
that narrows the question to ``units`` alone re-introduces the bug.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_RUNNER = (
    Path(__file__).resolve().parents[2] / "scripts" / "runners" / "jugnu.py"
)

#: Names ``reporting.verdict.compute`` is imported under in the runner.
_VERDICT_CALLEES = frozenset({"compute_verdict", "_compute_verdict"})

#: The timeout-salvage verdict call is exempt, and deliberately so.
#:
#: It reads its rows from ``_partial_state`` — the ``_external_partial_ref``
#: dict that survives coroutine cancellation — and that checkpoint carries
#: ``units`` / ``tier_used`` / ``profile_hints`` / ``operator_no_availability``
#: but NO plan channel (``pms/scraper.py`` writes ``_partial_result`` only into
#: ``shared_budget``, which the runner does not read). So there is genuinely no
#: ``plan_summaries`` in scope to forward.
#:
#: It is NOT enough to checkpoint the plan rows and pass them here. The salvage
#: record ships ``failed["units"] = _partial_units`` and has no plan channel of
#: its own, so crediting the verdict as SUCCESS_PLAN_LEVEL while emitting a
#: record with no rows would replace an honest FAILED with a success that has
#: nothing behind it — strictly worse than the current state. Fixing the
#: timeout path means checkpointing the plan rows AND emitting them, together.
#: Tracked separately; see the task referenced in the commit body.
#:
#: This is a single-line allow-list, not a category exemption: any NEW verdict
#: call that omits ``plan_summaries`` still fails this test.
_SALVAGE_EXEMPT_REASON = "timeout salvage: no plan channel is checkpointed"


def _verdict_calls() -> list[ast.Call]:
    """Every call to the verdict computer in the runner module.

    Returns:
        The matching :class:`ast.Call` nodes, in source order.
    """
    tree = ast.parse(_RUNNER.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _VERDICT_CALLEES
    ]


def test_runner_has_verdict_calls() -> None:
    """Guard the guard: if the callee is renamed, fail loudly rather than pass.

    An AST test that silently matches nothing is worse than no test, so assert
    the population is non-empty before asserting anything about its members.
    """
    calls = _verdict_calls()
    assert calls, (
        "no calls to "
        f"{sorted(_VERDICT_CALLEES)} found in {_RUNNER.name} — the verdict "
        "computer was renamed or re-imported. Update _VERDICT_CALLEES so this "
        "guard keeps checking the real call site instead of vacuously passing."
    )


def test_units_bearing_verdict_calls_also_pass_plan_summaries() -> None:
    """A call that asks about ``units`` must also ask about ``plan_summaries``.

    Scoped to calls that pass ``units``: those are the ones deciding a verdict
    from a property's extracted rows, so those are the ones that must consider
    both channels. Calls that pass neither (e.g. a pure fetch-outcome verdict)
    are legitimately out of scope.
    """
    offenders = []
    for call in _verdict_calls():
        kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        if "units" not in kwargs or "plan_summaries" in kwargs:
            continue
        # Identify the exempt salvage call STRUCTURALLY — by the variable it
        # reads its rows from — so the allow-list survives the line moving.
        # ``units=_partial_units or None`` is the salvage signature.
        if _SALVAGE_EXEMPT_REASON and "_partial_units" in ast.unparse(
            kwargs["units"]
        ):
            continue
        offenders.append(call.lineno)

    assert not offenders, (
        f"{_RUNNER.name} line(s) {offenders}: verdict computed from `units` "
        "without passing `plan_summaries`. promote_verified_unit_rows moves "
        "unanchored plan rows OUT of `units` into result['plan_summaries'], so "
        "a plan-only property arrives here with units==[] and its rows intact "
        "in the other channel. Omitting the argument makes compute() return "
        "FAILED_NO_DATA for a property that extracted fine — 37 of 77 "
        "zero-unit properties on the dq29 canary. Pass "
        "plan_summaries=result.get('plan_summaries')."
    )


@pytest.mark.parametrize(
    ("units", "plans", "expected"),
    [
        ([], [{"floor_plan_name": "A1"}], "SUCCESS_PLAN_LEVEL"),
        ([], [], "FAILED_NO_DATA"),
    ],
    ids=["plan-only-is-success", "truly-empty-still-fails"],
)
def test_compute_honours_plan_summaries(
    units: list[dict[str, object]],
    plans: list[dict[str, object]],
    expected: str,
) -> None:
    """The behaviour the wiring unlocks — and the case it must NOT swallow.

    The second case matters as much as the first: the fix must not turn a
    genuinely empty extraction into a success. A property with nothing in
    either channel is still ``FAILED_NO_DATA``.
    """
    from ma_poc.reporting.verdict import compute

    result = compute(
        fetch_outcome="OK",
        extract_result={"units": units},
        units=units or None,
        plan_summaries=plans or None,
    )
    assert result.verdict.value == expected
