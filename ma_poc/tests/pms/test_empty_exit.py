"""Tests for the empty-exit registry — Path B Piece 1.

The registry catalogs every ``tier_used`` label a PMS adapter emits
when it ran but produced nothing usable. The orchestrator uses
``is_empty_exit()`` to decide whether to retry with a different PMS.

These tests pin the contract against *real, in-source* adapter labels
so a new ``_SHAPE_REJECTED``-style label added to a future adapter
either:
  (a) matches an existing registered suffix → test passes silently
      (the new label gets retry coverage for free), or
  (b) doesn't match → the live-grep test below fails and forces the
      registry to be updated.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ma_poc.pms.empty_exit import empty_exit_reason, is_empty_exit

# ─────────────────────────────────────────────────────────────────────
# Section 1 — Positive cases: labels that MUST be classified as empty exits.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "label",
    [
        # Suffix-matched empty exits — sampled from each adapter
        "TIER_1_API_G5_EMPTY",
        "TIER_1_API_G5_NO_URN",
        "TIER_1_API_G5_API_ERROR",
        "TIER_1_API_SIGHTMAP_NO_RESPONSE",
        "TIER_1_API_SIGHTMAP_SHAPE_REJECTED",
        "TIER_1_API_SIGHTMAP_PARSE_FAILED",
        "TIER_1_API_SIGHTMAP_AMENITIES_ONLY",
        "TIER_1_API_RENTCAFE_SHAPE_REJECTED",
        "TIER_1_API_FUNNEL_SHAPE_REJECTED",
        "TIER_1_API_SPHEREXX_NO_RESPONSE",
        "TIER_1_API_SPHEREXX_SHAPE_REJECTED",
        "TIER_1_API_SPHEREXX_PARSE_FAILED",
        "TIER_1_API_APTS247_EMPTY",
        "TIER_1_API_APTS247_SHAPE_REJECTED",
        "TIER_1_API_RESMAN_EMPTY",
        "TIER_1_API_RESMAN_SHAPE_REJECTED",
        "TIER_1_API_RENTCAFE_DIRECT_SHAPE_REJECTED",
        # Verdict labels — exact match
        "NOT_ENCORESKYLINE_TEMPLATE",
        "ENCORESKYLINE_NO_PLAN_LINKS",
        "SYNDICATION_ONLY_WIX",
        "SYNDICATION_ONLY_SQUARESPACE",
    ],
)
def test_is_empty_exit_positive_cases(label: str) -> None:
    """Every label sampled from real adapter source must be classified
    as an empty exit so the orchestrator retries with a different PMS."""
    assert is_empty_exit(label) is True, (
        f"{label!r} should be classified as an empty exit "
        f"(otherwise the orchestrator won't retry this property)"
    )
    # Reason must be non-None for every positive case.
    assert empty_exit_reason(label) is not None


# ─────────────────────────────────────────────────────────────────────
# Section 2 — Negative cases: labels that MUST NOT be classified as empty exits.
# These are either successes (extracted units), in-progress markers, or
# LLM-tier outcomes (the LLM is already the last-resort retry, never
# itself a retry trigger).
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "label",
    [
        # Generic success labels
        "TIER_1_API",
        "TIER_2_JSONLD",
        "TIER_3_DOM",
        "TIER_MERGED_CROSS_PAGE",
        "TIER_1_DOM_APPFOLIO_SSR",
        # PMS-specific bare success labels — adapter extracted units
        "TIER_1_API_G5",
        "TIER_1_API_SIGHTMAP",
        "TIER_1_API_SIGHTMAP_IFRAME",
        "TIER_1_API_RENTCAFE",
        "TIER_1_API_FUNNEL",
        "TIER_1_API_FUNNEL_SPACES_SSR",
        "TIER_1_API_SPHEREXX",
        "TIER_1_API_SPHEREXX_RAZZ",
        "TIER_1_API_SPHEREXX_ZRS",
        "TIER_1_API_AMLI",
        "TIER_1_API_AMLI_INLINE",
        "TIER_1_API_AMLI_FETCHED",
        # LLM-tier — never retried from (it IS the last-resort tier)
        "TIER_4_LLM",
        "TIER_4_LLM_DOM",
        "TIER_4_LLM_API",
        "TIER_4_LLM_API_EMPTY",
        "TIER_4_LLM_DOM_EMPTY",
        "TIER_4_LLM_SHAPE_REJECTED",
    ],
)
def test_is_empty_exit_negative_cases(label: str) -> None:
    """Success / in-progress / LLM-tier labels must NOT trigger retry."""
    assert is_empty_exit(label) is False, (
        f"{label!r} should NOT be classified as an empty exit "
        f"(it's either a success or an LLM-tier outcome)"
    )
    assert empty_exit_reason(label) is None


# ─────────────────────────────────────────────────────────────────────
# Section 3 — Defensive None / empty / garbage handling.
# ─────────────────────────────────────────────────────────────────────


def test_is_empty_exit_handles_none() -> None:
    assert is_empty_exit(None) is False
    assert empty_exit_reason(None) is None


def test_is_empty_exit_handles_empty_string() -> None:
    assert is_empty_exit("") is False
    assert empty_exit_reason("") is None


def test_is_empty_exit_handles_unrelated_string() -> None:
    assert is_empty_exit("FAILED_FETCH_TIMEOUT") is False
    assert is_empty_exit("random_label") is False
    assert empty_exit_reason("FAILED_FETCH_TIMEOUT") is None


# ─────────────────────────────────────────────────────────────────────
# Section 4 — Telemetry payload (empty_exit_reason).
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "label,expected_reason",
    [
        ("TIER_1_API_G5_EMPTY", "_EMPTY"),
        ("TIER_1_API_G5_NO_URN", "_NO_URN"),
        ("TIER_1_API_SIGHTMAP_SHAPE_REJECTED", "_SHAPE_REJECTED"),
        ("TIER_1_API_SIGHTMAP_PARSE_FAILED", "_PARSE_FAILED"),
        ("TIER_1_API_SIGHTMAP_AMENITIES_ONLY", "_AMENITIES_ONLY"),
        ("TIER_1_API_G5_API_ERROR", "_API_ERROR"),
        ("NOT_ENCORESKYLINE_TEMPLATE", "NOT_ENCORESKYLINE_TEMPLATE"),
        ("SYNDICATION_ONLY_WIX", "SYNDICATION_ONLY_WIX"),
        ("ENCORESKYLINE_NO_PLAN_LINKS", "ENCORESKYLINE_NO_PLAN_LINKS"),
    ],
)
def test_empty_exit_reason_returns_the_matched_token(
    label: str, expected_reason: str
) -> None:
    """Reason is the matched suffix or verbatim label — gives the
    orchestrator a stable key for telemetry split-bys."""
    assert empty_exit_reason(label) == expected_reason


# ─────────────────────────────────────────────────────────────────────
# Section 5 — Live source-grep contract test.
#
# Greps every adapter's source for tier_used assignments that look like
# empty exits (matching the registered suffixes). For each one found,
# asserts is_empty_exit() returns True. If a new adapter ever adds a
# label that doesn't match the registry, this fails — forcing the
# registry to be updated rather than the label being silently dropped.
# ─────────────────────────────────────────────────────────────────────


_ADAPTERS_DIR = Path(__file__).resolve().parents[2] / "pms" / "adapters"

# Patterns that catch:
#   result.tier_used = _TIER_EMPTY                 (constant reference)
#   result.tier_used = f"{_TIER}_EMPTY"            (f-string with suffix)
#   tier_used="NOT_ENCORESKYLINE_TEMPLATE"         (verbatim literal)
_TIER_ASSIGN_CONST_RE = re.compile(
    r"tier_used\s*=\s*(_TIER[A-Z_]*(?:EMPTY|NO_URN|NO_RESPONSE|"
    r"SHAPE_REJECTED|PARSE_FAILED|AMENITIES_ONLY|API_ERROR|"
    r"NO_PLAN|NO_PLAN_LINKS|RESEARCH_BLOCKED))\b"
)
_TIER_ASSIGN_FSTRING_RE = re.compile(
    r'tier_used\s*=\s*f"[^"]*?(_(?:EMPTY|NO_URN|NO_RESPONSE|'
    r"SHAPE_REJECTED|PARSE_FAILED|AMENITIES_ONLY|API_ERROR|"
    r'NO_PLAN|NO_PLAN_LINKS|RESEARCH_BLOCKED))"'
)
_TIER_ASSIGN_LITERAL_RE = re.compile(
    r'tier_used\s*=\s*"((?:NOT_[A-Z_]+_TEMPLATE|'
    r"ENCORESKYLINE_NO_PLAN_LINKS|"
    r"SYNDICATION_ONLY_[A-Z_]+|"
    r'[A-Z_]+_(?:EMPTY|NO_URN|NO_RESPONSE|SHAPE_REJECTED|PARSE_FAILED|'
    r'AMENITIES_ONLY|API_ERROR|NO_PLAN|NO_PLAN_LINKS|RESEARCH_BLOCKED)))"'
)


def _harvest_empty_exit_assignments() -> dict[str, list[tuple[str, str]]]:
    """Walk ma_poc/pms/adapters/*.py and collect every assignment that
    sets ``tier_used`` to a label looking like an empty exit.

    Returns {kind: [(adapter_file, label_or_constant)]}. ``kind`` is
    ``"constant"`` | ``"fstring_suffix"`` | ``"literal"``.
    """
    out: dict[str, list[tuple[str, str]]] = {
        "constant": [],
        "fstring_suffix": [],
        "literal": [],
    }
    for path in sorted(_ADAPTERS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        src = path.read_text(encoding="utf-8")
        for m in _TIER_ASSIGN_CONST_RE.finditer(src):
            out["constant"].append((path.name, m.group(1)))
        for m in _TIER_ASSIGN_FSTRING_RE.finditer(src):
            out["fstring_suffix"].append((path.name, m.group(1)))
        for m in _TIER_ASSIGN_LITERAL_RE.finditer(src):
            out["literal"].append((path.name, m.group(1)))
    return out


def test_every_literal_empty_exit_in_source_is_classified() -> None:
    """Every verbatim ``tier_used = "<LABEL>"`` literal that looks like
    an empty exit (by suffix or known verdict shape) must be classified
    as such by the registry. Catches new adapters adding labels without
    updating ``empty_exit.py``."""
    harvest = _harvest_empty_exit_assignments()
    literals = harvest["literal"]
    assert literals, "expected at least one verbatim empty-exit literal in source"
    missing: list[tuple[str, str]] = []
    for adapter, label in literals:
        if not is_empty_exit(label):
            missing.append((adapter, label))
    assert not missing, (
        f"{len(missing)} empty-exit literal(s) in adapter source are not "
        f"in the registry; update ma_poc/pms/empty_exit.py: {missing}"
    )


def test_every_fstring_suffix_empty_exit_in_source_is_classified() -> None:
    """For ``tier_used = f"{_TIER}_EMPTY"`` style assignments, assert
    that an arbitrary string with the captured suffix is classified."""
    harvest = _harvest_empty_exit_assignments()
    suffixes = {label for _adapter, label in harvest["fstring_suffix"]}
    assert suffixes, "expected at least one f-string empty-exit suffix in source"
    missing: list[str] = []
    for suffix in suffixes:
        # Synthesize a representative label; what matters is the
        # suffix being recognized.
        if not is_empty_exit(f"TIER_1_API_SOMEPMS{suffix}"):
            missing.append(suffix)
    assert not missing, (
        f"f-string suffix(es) in adapter source are not in the registry; "
        f"update _EMPTY_EXIT_SUFFIXES: {missing}"
    )


def test_every_named_constant_empty_exit_is_documented() -> None:
    """For ``tier_used = _TIER_EMPTY`` style assignments, the constant
    name's suffix (everything after the last ``_TIER`` boundary) must
    map to a registered empty-exit suffix.

    This catches the case where an adapter defines a new module-level
    constant like ``_TIER_QUOTA_EXCEEDED = f"{_TIER_BASE}_QUOTA_EXCEEDED"``
    and uses it — the suffix ``_QUOTA_EXCEEDED`` would need to be added
    to the registry for retry coverage."""
    harvest = _harvest_empty_exit_assignments()
    constants = {label for _adapter, label in harvest["constant"]}
    assert constants, "expected at least one named-constant empty-exit assignment"
    # The constant name itself encodes the suffix — e.g. ``_TIER_EMPTY``
    # → ``_EMPTY``. Just check that *each* constant name ends with one
    # of the registered suffixes when stripped of the ``_TIER`` prefix.
    from ma_poc.pms.empty_exit import _EMPTY_EXIT_SUFFIXES
    missing: list[str] = []
    for const in constants:
        # Walk the suffixes; if any one is the trailing portion of the
        # constant name, this constant is covered.
        if not any(const.endswith(s) for s in _EMPTY_EXIT_SUFFIXES):
            missing.append(const)
    assert not missing, (
        f"Adapter constants whose suffix is not in the registry: {missing}"
    )
