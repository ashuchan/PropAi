"""Unit-level inference passes.

Every inference (filling a gap from one field using values present in other
fields) lives here. Single ordered pipeline: cheap-and-narrow first
(bed/bath/sqft from plan name), then string-parsing (rent range → numeric),
then identity fallback (unit_id from physical attributes).

Inference must run **before** validation. ``unit_validity.is_valid_unit``
reads numeric beds/baths/area; if those values are derivable from the plan
name (``"1 Bedroom"`` → beds=1, ``"Traditional 2x2 1019 SF"`` → sqft=1019),
``infer`` makes them so before the gate fires.

This module is **pure**: ``infer(unit)`` returns a new dict, never mutates
its input. Idempotent: ``infer(infer(u)) == infer(u)``.

See docs/2026_05_11_regressions_fix_design.md for the design and the
"inference-before-validation" principle.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Final

from ma_poc.core.identity import compute_fallback_unit_id
from ma_poc.extraction.canonical import (
    BATHS_KEYS,
    BEDS_KEYS,
    FP_NAME_KEYS,
    RENT_HI_KEYS,
    RENT_LO_KEYS,
    RENT_RANGE_KEYS,
    SQFT_KEYS,
    UID_KEYS,
    get_numeric,
    get_str,
)
from ma_poc.pms.adapters._parsing import (
    compute_floor_plan_id,
    infer_bed_bath_from_name,
    parse_rent_range,
)

# ── Idempotency marker ────────────────────────────────────────────────────────

#: ``infer`` sets this to ``True`` on its returned dict so a second call
#: short-circuits. Tested via ``test_infer_is_idempotent``.
_INFERRED_MARKER: Final[str] = "_inferred"


# ── Sqft-from-name inference ──────────────────────────────────────────────────
#
# Patterns observed in production (2026-05-11 cloud-run):
#   "Traditional 2x2 1019 SF"          → 1019
#   "Traditional 1x1 770 SF"           → 770
#   "Traditional 2x2 1031-1195 SF"     → 1031 (low end of range)
#   "Studio 0x1 334-619 SF"            → 334
#   "682-708sqft"                      → 682
#   "1019 sqft"                        → 1019
#   "1019 sq ft"                       → 1019
#   "1019 sq.ft."                      → 1019
#   "1019 square feet"                 → 1019
#
# Range semantics: returns the LOW end. Honest under-estimate; the unit
# could be as small as that. Median would synthesize a value not present
# in the source.

# Range pattern: "N-M SF" — tried first because it's more specific.
_SQFT_RANGE_RE: Final = re.compile(
    r"(\d{2,5})\s*-\s*(\d{2,5})\s*(?:sq\.?\s*ft\.?|sqft|sf|square\s*feet)\b",
    re.IGNORECASE,
)
# Single-value pattern: "N SF"
_SQFT_SINGLE_RE: Final = re.compile(
    r"(\d{2,5})\s*(?:sq\.?\s*ft\.?|sqft|sf|square\s*feet)\b",
    re.IGNORECASE,
)

#: Plan sqft must fall in this range to count as inferred. Mirrors the
#: production sanity bound — values outside are almost certainly the
#: regex matching a non-sqft number (year-built, unit-count, etc.).
_SQFT_MIN: Final[int] = 150
_SQFT_MAX: Final[int] = 10_000


def infer_sqft_from_name(name: str | None) -> int | None:
    """Pull a sqft number out of a plan-name string.

    Returns ``None`` when no recognisable sqft is found OR when the matched
    number falls outside ``[150, 10000]`` (the production sanity range).

    Range strings like ``"682-708sqft"`` return the LOW end (682). The
    function never raises and never invents data — every output value is
    grounded in a literal substring of *name*.

    Apply only as a **fallback** when the adapter / API didn't deliver a
    numeric sqft. Never overwrite a non-null source value with the inferred
    one — extraction is always more authoritative than name parsing.
    """
    if not name or not isinstance(name, str):
        return None
    s = name.strip()
    if not s:
        return None

    # Range first (more specific match) — captures "N-M" before the
    # single-value regex would pick up "M" alone.
    m = _SQFT_RANGE_RE.search(s)
    if m:
        try:
            low = int(m.group(1))
        except ValueError:
            low = -1
        if _SQFT_MIN <= low <= _SQFT_MAX:
            return low

    m = _SQFT_SINGLE_RE.search(s)
    if m:
        try:
            n = int(m.group(1))
        except ValueError:
            n = -1
        if _SQFT_MIN <= n <= _SQFT_MAX:
            return n

    return None


# ── Orchestrator ──────────────────────────────────────────────────────────────


def infer(
    unit: dict[str, Any],
    *,
    property_id: str | None = None,
) -> dict[str, Any]:
    """Run every inference pass on *unit*, in order. Returns a new dict.

    Passes (each only fills a gap, never overwrites):

    1. **Bed/bath from plan name** — ``infer_bed_bath_from_name`` over
       ``floor_plan_name``. Fills ``beds`` and/or ``baths`` when the name
       encodes them ("1 Bedroom", "2x2", "1BD/1BR-1", etc.).
    2. **Sqft from plan name** — ``infer_sqft_from_name`` over
       ``floor_plan_name``. Fills ``area`` when the name encodes it
       ("Traditional 2x2 1019 SF", "682-708sqft").
    3. **Rent from rent_range** — ``parse_rent_range`` over the
       ``rent_range`` string. Fills ``rent_low`` / ``rent_high`` when
       neither is numeric but the human-readable range string is set.
    4. **Unit-id fallback** — ``compute_fallback_unit_id`` (requires
       ``property_id`` and at least 2 of [fp_name, beds, baths, sqft]).
       Sets ``unit_id`` to ``"inferred_<hash>"`` and ``_inferred_id=True``.
       Runs AFTER passes 1+2 so the fallback has the post-inference
       physical attributes to hash.
    5. **Floor plan ID** — ``compute_floor_plan_id`` over the post-inference
       fp_name / beds / baths. Deterministic 12-char hash.

    Idempotency: a second call on the result returns it unchanged (marker
    ``_inferred=True`` short-circuits).

    Non-mutation: input dict is deep-copied; the returned dict is a fresh
    object. Mutations to the result never propagate back.

    Args:
        unit: The unit dict from any producer shape.
        property_id: Canonical property id; required for the unit-id
            fallback and the floor_plan_id derivation. When ``None``,
            those two passes are skipped (the rest still run).
    """
    if not isinstance(unit, dict):
        return unit

    # Idempotent fast-path: a second call on an already-inferred dict
    # returns it unchanged. Defensive deepcopy so the caller still gets
    # a fresh object they can mutate without aliasing.
    if unit.get(_INFERRED_MARKER) is True:
        return copy.deepcopy(unit)

    out: dict[str, Any] = copy.deepcopy(unit)

    fp_name = get_str(out, FP_NAME_KEYS)

    # Pass 1: beds / baths from plan name
    if fp_name:
        beds_now = get_numeric(out, BEDS_KEYS)
        baths_now = get_numeric(out, BATHS_KEYS)
        if beds_now is None or baths_now is None:
            inf_beds, inf_baths = infer_bed_bath_from_name(fp_name)
            if beds_now is None and inf_beds is not None:
                out["beds"] = inf_beds
            if baths_now is None and inf_baths is not None:
                out["baths"] = inf_baths

    # Pass 2: sqft from plan name
    if fp_name:
        sqft_now = get_numeric(out, SQFT_KEYS)
        if sqft_now is None:
            inf_sqft = infer_sqft_from_name(fp_name)
            if inf_sqft is not None:
                out["area"] = inf_sqft

    # Pass 3: rent_low / rent_high from rent_range string
    rent_lo_now = get_numeric(out, RENT_LO_KEYS)
    rent_hi_now = get_numeric(out, RENT_HI_KEYS)
    if rent_lo_now is None and rent_hi_now is None:
        rrange = get_str(out, RENT_RANGE_KEYS)
        if rrange:
            try:
                lo, hi = parse_rent_range(rrange)
            except Exception:
                lo, hi = None, None
            if lo is not None:
                out["rent_low"] = float(lo)
            if hi is not None:
                out["rent_high"] = float(hi)

    # Pass 4: unit_id fallback (uses post-inference physical attrs)
    #
    # D16 item 7: skip the inferred-id assignment when a real ``unit_number``
    # is present on the row. The apartment number IS the natural per-unit
    # identity; computing an ``inferred_<hash>`` from (fp_name, beds, baths)
    # on top of it causes two distinct failures downstream:
    #   1. Two real apartments on different plans with no unit_id (but with
    #      different unit_numbers) collide into the same inferred hash.
    #   2. The same apartment seen on two floor-plan pages with different
    #      fp_name values gets two DIFFERENT inferred ids, and the merger's
    #      Rank-1 unit_id keying can't reconcile them.
    #
    # Why no explicit `unit_number` short-circuit here: ``UID_KEYS`` already
    # includes the unit_number aliases (``unit_number``, ``_unit_number``,
    # ``unitnumber``, ``apartmentname``, ``apartment_name``,
    # ``display_unit_number``, ``displayunitnumber``, plus ``label``). So
    # ``get_str(out, UID_KEYS)`` returns the unit_number value when no
    # ``unit_id`` is present — ``uid_now is None`` already implies "no
    # unit-identity alias present at all," and the fallback is correctly
    # skipped whenever a unit_number is set.
    #
    # If a future change removes unit_number aliases from ``UID_KEYS``,
    # add an explicit ``extract_unit_number(out) is not None`` guard here.
    if property_id is not None:
        uid_now = get_str(out, UID_KEYS)
        if uid_now is None:
            try:
                fb = compute_fallback_unit_id(out, str(property_id))
            except Exception:
                fb = None
            if fb is not None:
                out["unit_id"] = fb
                out["_inferred_id"] = True

    # Pass 5: floor_plan_id from post-inference fp_name / beds / baths
    if not out.get("floor_plan_id"):
        beds_final = get_numeric(out, BEDS_KEYS)
        baths_final = get_numeric(out, BATHS_KEYS)
        try:
            fpid = compute_floor_plan_id(
                str(property_id) if property_id is not None else None,
                fp_name,
                int(beds_final) if beds_final is not None else None,
                float(baths_final) if baths_final is not None else None,
            )
        except Exception:
            fpid = None
        if fpid:
            out["floor_plan_id"] = fpid

    out[_INFERRED_MARKER] = True
    return out
