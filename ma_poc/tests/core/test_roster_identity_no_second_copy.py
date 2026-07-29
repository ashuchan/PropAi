"""Task #73 — structural guard: there must be exactly ONE roster-identity implementation.

This module shipped as two copies that were not ancestors of each other (660 lines on
``codex/fix-rentcafe-floorplan-id-identity``, 1,049 on ``worktree-wf_7ddcc9ac-38a-2``),
diverging in five functions, and neither branch landed. Reconciling them was task #73;
this file is the check that would have made task #73 unnecessary.

Structural on purpose — it fails on the *re-fork*, at the moment a second definition
appears, rather than six weeks later when the two copies have drifted far enough to
produce different answers on the same run. Modelled on
``tests/pms/test_appfolio_scope_contract.py``, which does the same job for the AppFolio
``/listings`` predicate.

Paths are derived from the imported module object, never from the CWD — see
``tests/pms/test_fixture_paths_cwd_independent.py`` for why that matters in this repo.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from ma_poc.core import roster_identity

#: Definitions that constitute the roster-identity rule. A second ``def`` of any of these
#: anywhere in the package is a fork, whatever the file is called.
_OWNED_DEFINITIONS: tuple[str, ...] = (
    "def parse_unit_locations",
    "def roster_is_foreign",
    "def roster_fingerprint",
    "def find_roster_collisions",
    "def collision_verdicts_by_position",
    "def scope_rows_to_property",
    "def roster_scope",
    "def guard_property_record",
    "def apply_roster_identity",
    "def unit_level_rows",
    "def plan_level_rows",
)

#: Output-schema keys that must be imported rather than restated. A consumer that writes
#: ``record["unverified_units"]`` has pinned a private copy of the schema; renaming the
#: constant then leaves the two halves disagreeing silently.
#:
#: ``"UNIT_ROUTE_UNVERIFIED"`` is deliberately NOT in this list. The same string is a
#: row-level ``data_quality_flag`` in ``pms/scraper.promote_verified_unit_rows`` ("this ROW
#: had no apartment anchor") and a property-level ``_meta["verdict"]`` here ("this
#: PROPERTY's roster is not verified"). Two meanings, one spelling, in different
#: namespaces — a textual ban cannot tell them apart and would force a false refactor.
#: :func:`test_the_shared_verdict_spelling_stays_in_step` pins them together instead.
_OWNED_LITERALS: tuple[str, ...] = (
    '"unverified_units"',
    "'unverified_units'",
    '"unverified_floor_plans"',
    "'unverified_floor_plans'",
)


def _module_path() -> Path:
    return Path(inspect.getfile(roster_identity)).resolve()


def _package_root() -> Path:
    # ma_poc/core/roster_identity.py -> ma_poc/
    return _module_path().parents[1]


def _python_sources() -> list[Path]:
    root = _package_root()
    return [
        p
        for p in sorted(root.rglob("*.py"))
        if "/tests/" not in p.as_posix() and not p.name.startswith("test_")
    ]


def test_exactly_one_module_is_named_roster_identity() -> None:
    """Two files with this name is how the fork was invisible in a file listing."""
    found = sorted(p for p in _package_root().rglob("roster_identity.py"))
    assert found == [_module_path()], (
        "a second roster_identity.py exists: "
        f"{[str(p) for p in found]} — reconcile them into "
        f"{_module_path()} rather than picking a winner"
    )


@pytest.mark.parametrize("definition", _OWNED_DEFINITIONS)
def test_no_second_definition_of_a_roster_identity_rule(definition: str) -> None:
    """The rule is defined once. Consumers import it; they do not restate it."""
    owner = _module_path()
    offenders = [
        p.as_posix()
        for p in _python_sources()
        if p != owner and definition in p.read_text()
    ]
    assert not offenders, (
        f"{definition!r} is re-declared outside {owner.name}: {offenders}. "
        "Import it from ma_poc.core.roster_identity instead — this module already "
        "existed as two divergent copies once (task #73)."
    )


@pytest.mark.parametrize("literal", _OWNED_LITERALS)
def test_no_consumer_hardcodes_a_roster_identity_key(literal: str) -> None:
    """The output-schema keys live on the module as constants, not as string literals.

    A consumer that writes ``record["unverified_units"]`` has pinned a private copy of the
    schema; renaming the constant then leaves the two halves disagreeing silently.
    """
    owner = _module_path()
    offenders = [
        p.as_posix()
        for p in _python_sources()
        if p != owner and literal in p.read_text()
    ]
    assert not offenders, (
        f"{literal} is hard-coded in {offenders} — import the constant from "
        "ma_poc.core.roster_identity (QUARANTINE_KEY / QUARANTINE_PLANS_KEY / "
        "UNVERIFIED_VERDICT)."
    )


def test_the_shared_verdict_spelling_stays_in_step() -> None:
    """``UNIT_ROUTE_UNVERIFIED`` means two things; both must keep spelling it the same.

    ``pms/scraper.promote_verified_unit_rows`` stamps it as a per-ROW data-quality flag
    for a row it could not anchor to an apartment id; this module stamps it as a
    per-PROPERTY verdict. They are not the same claim, so neither should import the
    other's constant — but a rename on one side while the other keeps the old spelling
    would leave two flags that look unrelated in the output. Catch it here.
    """
    from ma_poc.pms import scraper

    src = Path(inspect.getfile(scraper)).read_text()
    assert roster_identity.UNVERIFIED_VERDICT in src, (
        "pms/scraper.py no longer spells the row-level flag "
        f"{roster_identity.UNVERIFIED_VERDICT!r}. If that was a deliberate rename, rename "
        "roster_identity.UNVERIFIED_VERDICT with it or record here why they now differ."
    )


def test_every_consumer_imports_the_shared_module() -> None:
    """Anything that mentions roster identity must import it, not reimplement it."""
    owner = _module_path()
    for path in _python_sources():
        if path == owner:
            continue
        src = path.read_text()
        if "roster_identity" not in src:
            continue
        assert "from ma_poc.core.roster_identity import" in src or (
            "import ma_poc.core.roster_identity" in src
        ), (
            f"{path.as_posix()} refers to roster_identity without importing the shared "
            "module — that is what a fork looks like on its first day."
        )


# ── the scan itself, table-tested ───────────────────────────────────────────
#
# The checks above are substring scans over source text, and a substring scan is a
# pattern match: too broad and it forces false refactors (the first draft of this file
# banned "UNIT_ROUTE_UNVERIFIED" and tripped on pms/scraper.py, where the same string is
# a row-level flag with a different meaning); too narrow and the fork it exists to catch
# walks straight past it. Both directions are pinned here on synthetic source lines.

_SCAN_TABLE: tuple[tuple[str, str, bool], ...] = (
    # (case, source line, should the OWNED-DEFINITION scan flag it?)
    ("plain re-definition", "def roster_is_foreign(units):", True),
    ("re-definition, async", "async def roster_is_foreign(units):", True),
    ("re-definition, indented in a class", "    def apply_roster_identity(self):", True),
    ("second copy of the fingerprint", "def roster_fingerprint(rows, *, min_rows=5):", True),
    ("second copy of the position variant",
     "def collision_verdicts_by_position(props):", True),
    ("second copy of the plan predicate", "def plan_level_rows(units):", True),
    # must NOT match — these are the shapes a legitimate consumer has.
    ("importing it", "from ma_poc.core.roster_identity import roster_is_foreign", False),
    ("calling it", "verdict = roster_is_foreign(record['units'])", False),
    ("mentioning it in a docstring", "    # see roster_is_foreign for the threshold", False),
    ("a private helper that merely wraps it",
     "def _judge(record): return roster_is_foreign(record['units'])", False),
    ("a similarly-named but different rule",
     "def roster_is_foreign_to_the_state(units):", True),  # deliberately caught: a fork
    ("an unrelated function", "def unit_rows_for_report(units):", False),
    ("re-export in __all__", '    "roster_is_foreign",', False),
    ("type annotation reference", "cb: Callable[..., None] = roster_is_foreign", False),
)


@pytest.mark.parametrize(
    ("case", "line", "expected"), _SCAN_TABLE, ids=[c for c, _, _ in _SCAN_TABLE]
)
def test_the_definition_scan_matches_forks_and_not_consumers(
    case: str, line: str, expected: bool
) -> None:
    """``def <name>`` as a substring: what it catches and what it lets through.

    ``roster_is_foreign_to_the_state`` is flagged on purpose — a near-name copy is exactly
    the fork this file exists to stop, and the cost of the false positive is one rename.
    """
    hit = any(d in line for d in _OWNED_DEFINITIONS)
    assert hit is expected, f"{case}: {line!r} -> matched={hit}, expected {expected}"


_LITERAL_TABLE: tuple[tuple[str, str, bool], ...] = (
    ("hard-coded quarantine key, double", 'rec["unverified_units"] = rows', True),
    ("hard-coded quarantine key, single", "rec['unverified_units'] = rows", True),
    ("hard-coded plan quarantine", 'rec["unverified_floor_plans"] = plans', True),
    # must NOT match
    ("importing the constant",
     "from ma_poc.core.roster_identity import QUARANTINE_KEY", False),
    ("using the constant", "rec[QUARANTINE_KEY] = rows", False),
    ("the row-level flag with the same words",
     '_append_quality_flag(row, "UNIT_ROUTE_UNVERIFIED")', False),
    ("a different key that merely contains 'unverified'",
     'rec["unverified_note"] = "x"', False),
    ("prose in a comment", "# rows land in unverified_units", False),
)


@pytest.mark.parametrize(
    ("case", "line", "expected"), _LITERAL_TABLE, ids=[c for c, _, _ in _LITERAL_TABLE]
)
def test_the_literal_scan_bans_only_the_schema_keys(
    case: str, line: str, expected: bool
) -> None:
    hit = any(lit in line for lit in _OWNED_LITERALS)
    assert hit is expected, f"{case}: {line!r} -> matched={hit}, expected {expected}"


def test_scope_rows_to_property_does_not_accept_a_state_argument() -> None:
    """The one behaviour the 660-line copy had and this module deliberately does not.

    ``state`` was accepted and never read in BOTH copies — the body matched on
    ``(city, zip)`` only — and two call sites passed one believing it narrowed the match.
    A 5-digit ZIP already determines the state. Pinned so the dead parameter is not
    "restored" by someone diffing against the old copy and seeing a missing argument.
    """
    params = inspect.signature(roster_identity.scope_rows_to_property).parameters
    assert "state" not in params, (
        "scope_rows_to_property must not take a `state` argument: the ZIP determines the "
        "state, and an accepted-and-ignored parameter reads as a narrower filter than it "
        "is. See the function docstring."
    )
    assert "city" in params
    assert "zip_code" in params
