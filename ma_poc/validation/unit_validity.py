"""Single source of truth for "is this a unit?"

User contract (2026-05-11): a row is a unit **iff it carries at least one
numeric physical dimension**. Rent is NOT part of the validity check.
``floor_plan_name`` is identity-text, not a dimension.

Physical dimensions = ``beds`` OR ``baths`` OR ``area``, post-inference,
post-sanity, read via canonical alias resolution.

Why this is correct:

  * "Hoboken" with only ``floor_plan_name`` set → not a unit (the Skyline
    at Kessler 2026-05-11 regression — nestiolistings neighborhoods
    endpoint emitted neighborhood names which floated through to
    floor_plan_name).
  * "Traditional 2x2 1019 SF" with beds=2, baths=2, area=-1 → valid
    (beds=2 is a real dimension; area=-1 is the explicit ABSENT sentinel
    and treated as not-present here, but beds alone is enough).
  * "1 Bedroom" alone, after ``infer()`` fills beds=1 from the name → valid.
  * A row with rent=$1500 but no dims → **not** a unit (we know it has a
    price but not what we're buying).

Pre-condition for callers: ``infer()`` and ``sanity_bound()`` have already
run. Without inference, name-encoded dimensions ("1 Bedroom") wouldn't
have been materialized; without sanity, junk values (beds=99, area=5)
would pass.

This module is **pure** and has no side effects.

See docs/2026_05_11_regressions_fix_design.md for the design.
"""

from __future__ import annotations

from typing import Any

from ma_poc.extraction.canonical import (
    BATHS_KEYS,
    BEDS_KEYS,
    SQFT_KEYS,
    get_numeric,
)

# ── Diagnostic constants ─────────────────────────────────────────────────────

#: Returned by ``absence_reasons`` when the corresponding dimension is missing.
#: Stable string set — consumers (telemetry, canary diff) can group on these.
REASON_NO_BEDS: str = "NO_BEDS"
REASON_NO_BATHS: str = "NO_BATHS"
REASON_NO_SQFT: str = "NO_SQFT"


# ── Public predicates ────────────────────────────────────────────────────────


def has_dimension(unit: dict[str, Any]) -> bool:
    """``True`` iff *unit* carries at least one numeric beds/baths/area value.

    Reads via canonical's alias resolution (case-insensitive, FieldValue-
    aware, ABSENT-sentinel-aware). Returns False for empty dicts, for dicts
    with only floor_plan_name set, for dicts with only rent set.
    """
    return (
        get_numeric(unit, BEDS_KEYS) is not None
        or get_numeric(unit, BATHS_KEYS) is not None
        or get_numeric(unit, SQFT_KEYS) is not None
    )


def is_valid_unit(unit: dict[str, Any]) -> bool:
    """A row qualifies as a unit iff it has at least one physical dimension.

    The single authoritative predicate. Replaces the six inconsistent gates
    surveyed in the 2026-05-11 design audit (parse_generic_api,
    parse_api_responses, has_unit_signals, _container_yields_unit,
    _offer_to_unit, schema_gate.is_substantive).

    Pre-condition: ``infer()`` and ``sanity_bound()`` have already run.
    Without inference, a row with only ``floor_plan_name="1 Bedroom"`` would
    fail this check (beds=None) — but ``infer()`` would have inferred
    beds=1 by then, so the check passes.

    Inputs that aren't dicts (defensive) return False — they're not units.
    """
    if not isinstance(unit, dict):
        return False
    return has_dimension(unit)


def absence_reasons(unit: dict[str, Any]) -> list[str]:
    """Return ordered list of dimension-absence reasons for *unit*.

    Returned strings are drawn from the ``REASON_*`` constants. Useful for
    structured telemetry: emitting ``EventKind.UNIT_VALIDITY_REJECTED`` with
    ``reasons=absence_reasons(unit)`` lets analytics group rejections by
    which dimensions were missing.

    An empty list means the unit has every dimension. A unit that
    ``is_valid_unit`` accepts will typically still have non-empty
    absence_reasons (e.g. beds present but baths absent), so callers
    should distinguish "missing dim" from "invalid row".
    """
    out: list[str] = []
    if not isinstance(unit, dict):
        return [REASON_NO_BEDS, REASON_NO_BATHS, REASON_NO_SQFT]
    if get_numeric(unit, BEDS_KEYS) is None:
        out.append(REASON_NO_BEDS)
    if get_numeric(unit, BATHS_KEYS) is None:
        out.append(REASON_NO_BATHS)
    if get_numeric(unit, SQFT_KEYS) is None:
        out.append(REASON_NO_SQFT)
    return out
