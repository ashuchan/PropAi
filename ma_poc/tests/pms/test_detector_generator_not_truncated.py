"""``_iter_html_markers`` is a generator — a bare ``return`` deletes candidates.

f46a490 added a SecureCafe UnitID+FloorPlanID rule yielding ``rentcafe`` at 0.93
and terminated it with a bare ``return``. In a generator that does not end the
rule, it ends ITERATION: every later candidate vanishes. Half the function's
yields (29 of 58, across 21 vendors) sat after that point, including
``generic_plan_text`` — the last-resort plan-level rescue.

The winner was never affected: consumers rank by confidence and nothing
suppressed exceeded 0.93. What died was the Path-B RETRY LIST, whose whole job
is to hold the alternatives for when the first pick extracts nothing. And the
failure was silent in the worst way — a truncated candidate list is reported as
"no candidates", which reads as *absence of evidence* rather than *a search that
was cut short*.

Two tests, deliberately at different levels: a structural one that forbids the
construct anywhere in the generator (so a future rule cannot reintroduce it),
and a behavioural one on a real SecureCafe-shaped page.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from ma_poc.pms import detector as _detector_mod
from ma_poc.pms.detector import _iter_html_markers

_DETECTOR_SRC = Path(inspect.getsourcefile(_detector_mod) or "")
_GENERATORS = ("_iter_html_markers",)


def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse(_DETECTOR_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {_DETECTOR_SRC.name}")


class TestNoBareReturnInMarkerGenerators:
    """Structural: the construct must not exist at all."""

    def test_it_is_actually_a_generator(self) -> None:
        """Guard the guard — if it stops yielding, this test is meaningless."""
        for name in _GENERATORS:
            fn = _fn(name)
            yields = [n for n in ast.walk(fn) if isinstance(n, (ast.Yield, ast.YieldFrom))]
            assert yields, f"{name} no longer yields; this guard is now vacuous"

    def test_no_bare_return_truncates_the_candidate_stream(self) -> None:
        """A bare ``return`` here silently deletes every later candidate.

        Precedence belongs in the confidence value, which the consumers rank on.
        If a rule ever genuinely needs to be terminal, express that at the
        CONSUMER (take the top candidate) — never by emptying the generator that
        also feeds the retry list.
        """
        for name in _GENERATORS:
            fn = _fn(name)
            bare = [
                n.lineno
                for n in ast.walk(fn)
                if isinstance(n, ast.Return) and n.value is None
            ]
            total_yields = len(
                [n for n in ast.walk(fn) if isinstance(n, (ast.Yield, ast.YieldFrom))]
            )
            assert not bare, (
                f"{name} has a bare `return` at line(s) {bare}. It is a GENERATOR "
                f"with {total_yields} yields — `return` ends ITERATION and deletes "
                "every candidate after it, gutting the Path-B retry list. Use "
                "if/else and let confidence decide precedence."
            )


class TestSecureCafeStillYieldsTheTail:
    """Behavioural: the tail survives on a page that fires the 0.93 rule."""

    #: A SecureCafe portal page that ALSO carries an Entrata marker, so a
    #: correct generator must yield both rentcafe@0.93 and the entrata candidate.
    _HTML = (
        '<html><body>'
        '<a href="https://sub.securecafe.com/onlineleasing/demo/'
        'availableunits.aspx?FloorPlanID=123&UnitID=456">Apply</a>'
        '<script src="https://cdn.entrata.com/prospect-portal/app.js"></script>'
        '</body></html>'
    )

    def test_the_high_confidence_rule_still_fires(self) -> None:
        got = list(_iter_html_markers(self._HTML))
        assert any(
            p == "rentcafe" and c == 0.93 for p, c, _ in got
        ), f"the UnitID+FloorPlanID rule stopped firing: {[(p, c) for p, c, _ in got]}"

    def test_later_candidates_are_not_deleted(self) -> None:
        """The regression: with the bare `return`, this list held rentcafe only."""
        got = list(_iter_html_markers(self._HTML))
        names = {p for p, _, _ in got}
        assert names - {"rentcafe"}, (
            "the candidate stream contains ONLY rentcafe — the generator was "
            f"truncated after the 0.93 yield. Got: {[(p, c) for p, c, _ in got]}"
        )

    def test_rentcafe_is_not_yielded_twice(self) -> None:
        """if/else, not fall-through: the 0.90 marker must not also fire."""
        confs = sorted(c for p, c, _ in _iter_html_markers(self._HTML) if p == "rentcafe")
        assert 0.90 not in confs, (
            f"both the 0.93 and 0.90 rentcafe rules fired ({confs}) — the branch "
            "fell through instead of using else."
        )
