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

import re
from typing import Any

from ma_poc.extraction.canonical import (
    BATHS_KEYS,
    BEDS_KEYS,
    FP_NAME_KEYS,
    RENT_HI_KEYS,
    RENT_LO_KEYS,
    SQFT_KEYS,
    get_numeric,
    get_str,
)

# ── Diagnostic constants ─────────────────────────────────────────────────────

#: Returned by ``absence_reasons`` when the corresponding dimension is missing.
#: Stable string set — consumers (telemetry, canary diff) can group on these.
REASON_NO_BEDS: str = "NO_BEDS"
REASON_NO_BATHS: str = "NO_BATHS"
REASON_NO_SQFT: str = "NO_SQFT"

#: Returned by ``plan_rejection_reason`` for plans dropped at the
#: substantive-plan gate (2026-05-24 cohort). Stable strings so the
#: telemetry/analyzer can group rejections by cause.
REASON_PLAN_NO_DIMENSIONS: str = "PLAN_NO_DIMENSIONS"
REASON_PLAN_MARKETING_TAGLINE: str = "PLAN_MARKETING_TAGLINE"
REASON_PLAN_NAME_ONLY: str = "PLAN_NAME_ONLY"

# Floor-plan names that are clearly marketing taglines, not plan identifiers.
# Real plan names are short codes ("A1", "B2", "Studio Plan A", "Lofts",
# "Mountain View"); marketing taglines enumerate multiple bedroom counts in
# one string ("Studio, 1-, 2-, and 3-bedroom homes with loft layouts").
# Run 2026-05-24 canonical case: PID 257570 moderacherrycreek.
_MARKETING_TAGLINE_MAX_LEN: int = 50
# Counts distinct "N bed[room]" or "N-bedroom" tokens. A name enumerating
# 2+ bedroom counts ("Studio, 1-, 2-, and 3-bedroom homes") is marketing
# copy, not a plan identifier.
_BED_COUNT_TOKEN_RE = re.compile(
    r"\b(\d+)\s*[-\s]\s*(?:bed|br|bedroom)s?\b",
    re.IGNORECASE,
)
# "Studio" or "efficiency" also counts as a distinct bedroom-class token
# — co-occurring with a digit-bed token signals enumeration.
_STUDIO_TOKEN_RE = re.compile(r"\b(?:studio|efficiency)\b", re.IGNORECASE)
_MARKETING_PHRASE_RE = re.compile(
    r"\b(?:choose\s+from|various|multiple\s+(?:floor|plan)|available\s+(?:in|as))\b",
    re.IGNORECASE,
)


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


def _looks_like_marketing_tagline(name: str) -> bool:
    """``True`` if *name* looks like marketing copy rather than a plan identifier.

    Real floor-plan names are short codes or short labels: "A1", "B2",
    "Studio Plan A", "Lofts", "1BR/1BA", "Mountain View". Marketing
    taglines enumerate multiple bedroom counts ("1, 2, 3-bedroom homes")
    or use sales phrases ("Choose from various floor plans").

    Used by ``is_substantive_plan`` to reject the canonical case from run
    2026-05-24 PID 257570 moderacherrycreek where dom_scan extracted
    ``"Studio, 1-, 2-, and 3-bedroom homes with loft layouts"`` as a
    floor_plan_name from a page-level aggregate.
    """
    if not isinstance(name, str):
        return False
    s = name.strip()
    if not s:
        return False
    if len(s) > _MARKETING_TAGLINE_MAX_LEN:
        return True
    if _MARKETING_PHRASE_RE.search(s):
        return True
    # Count distinct bedroom-class tokens. ≥2 means the name enumerates
    # multiple plan types, which is marketing copy not a plan identifier.
    n_bed_tokens = len(_BED_COUNT_TOKEN_RE.findall(s))
    if _STUDIO_TOKEN_RE.search(s):
        n_bed_tokens += 1
    if n_bed_tokens >= 2:
        return True
    return False


def is_substantive_plan(plan: dict[str, Any]) -> bool:
    """A plan-level row qualifies as substantive iff it carries real evidence.

    Stricter than ``is_valid_unit``. A row with only ``beds=1`` and every
    other field absent / sentinel passes ``is_valid_unit`` (one dimension
    is enough for unit-level validity), but we don't want to ship that as
    a floor_plan summary — it's a near-empty row.

    A plan is substantive iff ANY of:

      * It has a real rent (rent_low or rent_high present and > 100). Real
        rent is the strongest evidence the row came from listing data.
      * It has a real area (sqft present and not the ABSENT sentinel).
      * It has BOTH beds AND baths together (a real plan class even
        without rent / area / name — e.g. APTS247 emits "1 bed / 1 bath"
        plans with no dimensions because the per-unit data is on a
        detail page the entry pass didn't load).
      * It has one of beds/baths AND a defensible floor_plan_name
        (not a marketing tagline, not empty).

    Pre-condition: ``infer()`` and ``sanity_bound()`` have already run, so
    the area=-1 sentinel and rent==None are already canonicalised.

    Run 2026-05-24 motivation: 252 SUCCESS_PLAN_LEVEL properties shipped
    plan rows with ``area=-1, rent_low=None, floor_plan_name=None`` (or a
    marketing tagline) — all admitted because ``is_valid_unit`` accepted
    them on the beds dimension alone. See playbook
    ``docs/dom_quality_and_llm_reduction_playbook.md``.
    """
    if not isinstance(plan, dict):
        return False

    # Strongest signal: real rent.
    for key_tuple in (RENT_LO_KEYS, RENT_HI_KEYS):
        rent = get_numeric(plan, key_tuple)
        if rent is not None and rent > 100:
            return True

    # Strong signal: real area (sentinel ABSENT already rejected by get_numeric).
    sqft = get_numeric(plan, SQFT_KEYS)
    if sqft is not None and sqft > 0:
        return True

    beds = get_numeric(plan, BEDS_KEYS)
    baths = get_numeric(plan, BATHS_KEYS)
    name = get_str(plan, FP_NAME_KEYS)
    name_ok = bool(name) and not _looks_like_marketing_tagline(name)

    # Moderate signal: beds AND baths together. Reject when the name is
    # a known marketing tagline even if beds+baths are present — the row
    # almost certainly came from a page-level aggregate, not a per-plan
    # card.
    if beds is not None and baths is not None:
        if name and _looks_like_marketing_tagline(name):
            return False
        return True

    # Weakest signal: one dimension + a defensible name.
    if (beds is not None or baths is not None) and name_ok:
        return True

    return False


def plan_rejection_reason(plan: dict[str, Any]) -> str:
    """Diagnostic reason code for a plan rejected by ``is_substantive_plan``.

    Returns a stable ``REASON_PLAN_*`` string so telemetry / canary diff
    can group rejections. Callers must only invoke this when
    ``is_substantive_plan(plan)`` is False.
    """
    if not isinstance(plan, dict):
        return REASON_PLAN_NO_DIMENSIONS
    name = get_str(plan, FP_NAME_KEYS)
    if name and _looks_like_marketing_tagline(name):
        return REASON_PLAN_MARKETING_TAGLINE
    beds = get_numeric(plan, BEDS_KEYS)
    sqft = get_numeric(plan, SQFT_KEYS)
    if beds is None and sqft is None:
        return REASON_PLAN_NO_DIMENSIONS
    return REASON_PLAN_NAME_ONLY


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
