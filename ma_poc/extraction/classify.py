"""Tag a validated row as unit-level inventory vs plan-level summary.

A row is **unit-level** when it carries something specific to one apartment
— a real unit identifier, an availability date, a specific floor, or a
specific building. Otherwise the row is **plan-level**: a description of a
floor plan's typical dimensions and rent range, with no claim about which
specific apartments are actually available.

Why this matters:

  * JSON-LD Offers describe plans, not units (Schema.org ``Offer`` has no
    per-unit identity).
  * "Starting at $1,500" marketing cards describe plans, not units.
  * RentCafe ``/getFloorplans`` responses carry one entry per plan, even
    when the property has many physical apartments per plan.

A user asking "how many units are available?" gets the wrong answer when
plan-level rows are counted as units (the 2026-05-11 Luxe 88 case — 16
"units" extracted from JSON-LD, but each was actually a plan summary).

Pre-condition: ``infer()`` has run, so a row that carries a fallback
``unit_id`` is marked with ``_inferred_id=True``. ``classify`` distinguishes
inferred-identity rows (plan-level) from natural-identity rows (unit-level).

This module is **pure**: deterministic, no side effects, never raises.

See docs/2026_05_11_regressions_fix_design.md, Stage 2.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from ma_poc.extraction.canonical import (
    AVAIL_STATUS_KEYS,
    RENT_HI_KEYS,
    RENT_LO_KEYS,
    SQFT_KEYS,
    UID_KEYS,
    get_numeric,
    get_str,
    is_present,
)
from ma_poc.extraction.unit_number import extract_unit_number, normalize_unit_number

#: Returned by ``classify`` — string-typed so the value is JSON-friendly
#: and the contract is explicit at the boundary.
UnitLevel = Literal["unit", "plan"]


#: Fields that, when set with a real (non-inferred) value, indicate the row
#: describes a *specific physical apartment* rather than a plan summary.
#: Order matches the check order in ``_has_natural_unit_identity``.
_UNIT_LEVEL_SIGNAL_KEYS: Final[tuple[str, ...]] = (
    "available_date",
    "availability_date",
    "availabledate",
    "floor",
    "building",
    "buildingname",
    "building_name",
)


def _has_natural_unit_identity(
    unit: dict[str, Any],
    plan_codes: frozenset[str] | None = None,
) -> bool:
    """``True`` when the row carries a real per-apartment identifier.

    Two paths to natural identity:

    1. ``unit_id`` (resolved via the alias tuple) is non-inferred and
       non-empty — the original rule.
    2. **D16 item 5 — unit_number promotion**: ``unit_number`` is present,
       normalises to a non-empty key, AND that key is NOT in *plan_codes*
       (the set of known floor_plan_ids for the property). A unit row
       whose ``unit_number`` matches a plan code is almost certainly a
       plan-aggregate row that leaked through with a noisy unit_number
       field, not a real apartment.

    Rules:
      * ``_inferred_id=True`` → never natural via path 1; promotion is
        still allowed via path 2.
      * Resolved UID starting with ``"inferred_"`` → never natural via
        path 1; promotion is still allowed via path 2.
      * Empty / whitespace / sentinel unit_number → no promotion.
      * unit_number that matches any entry in *plan_codes* → no promotion
        (the inverse-B4 guard at the classify boundary).

    Args:
        unit: The unit dict to classify.
        plan_codes: Set of known plan-code identifiers for this property
            (typically the ``floor_plan_id`` values from sibling
            plan-aggregate rows). When provided, a row whose unit_number
            normalises to a member of this set is denied promotion. When
            ``None`` or empty, the plan-code check is a no-op — promotion
            is allowed for any non-empty normalised unit_number.
    """
    # --- Path 1: natural unit_id ----------------------------------------------
    path1_ok = True
    if unit.get("_inferred_id") is True:
        path1_ok = False
    else:
        # FieldValue from the merger carries provenance — IDENTITY_FALLBACK is
        # the in-band signal that the unit_id was computed, not extracted.
        uid_fv = unit.get("unit_id")
        if hasattr(uid_fv, "source") and hasattr(uid_fv, "value"):
            source_name = getattr(uid_fv.source, "value", uid_fv.source)
            if isinstance(source_name, str) and source_name == "identity_fallback":
                path1_ok = False
        if path1_ok:
            uid_str = get_str(unit, UID_KEYS)
            if uid_str is None or uid_str.startswith("inferred_"):
                path1_ok = False
    if path1_ok:
        return True

    # --- Path 2: unit_number promotion (D16 item 5) ---------------------------
    raw_unit_number = extract_unit_number(unit)
    if raw_unit_number is None:
        return False
    normalised = normalize_unit_number(raw_unit_number)
    if not normalised:
        return False
    if plan_codes and normalised in plan_codes:
        # Plan-code-shaped unit_number — deny promotion. The row is almost
        # certainly a plan-aggregate that leaked through with a noisy
        # unit_number field (e.g. "2B4" present as unit_number AND as a
        # sibling row's floor_plan_id).
        return False
    return True


def _has_per_unit_signal(unit: dict[str, Any]) -> bool:
    """``True`` when any per-apartment-only field is populated.

    Plan-level rows don't carry availability dates, floors, or buildings —
    those are specific to a physical apartment in inventory.

    2026-05-17 — extended to recognise **rent + AVAILABLE status** as a
    per-unit signal. Real-world case: LLM-DOM extraction on properties
    like dovevalleyapts.com / abodes.com / lakewood-apartments emits
    rows like ``{floor_plan_name: "A1", beds: 1, baths: 1, sqft: 673,
    market_rent_low: 1759, availability_status: "AVAILABLE"}`` —
    AVAILABLE units priced for a specific lease term. Pre-fix these
    landed in plan_summaries because they lacked ``available_date``
    (move-in date). On PIDs 20959 (6/12), 55317 (3/8) etc, these were
    distinct lease offerings on the same floor plan but at different
    rents — the page representation said two A1s exist at $1604 and
    $1759. Pre-fix, the $1759 row was demoted to plan-level and dropped
    at the v2 output boundary (Bug A). With this signal, the row stays
    unit-level and ships through ``units`` properly.

    Guard: rent alone is NOT enough (a row "$1500–$2000" with no status
    is a plan-level rent range). We require ``availability_status`` to
    be explicitly AVAILABLE — that lifts the row from "this plan exists
    at these prices" to "an apartment at this price is available".

    2026-05-24 — Phase 0.2 extension (run 2026-05-23 forensic). The LLM
    defaults ``availability_status`` to ``UNKNOWN`` when a per-plan page
    shows rent + sqft + beds but no explicit "Available now" badge. On
    66% of TIER_4_LLM_DOM ``units=1`` SUCCESS ships, the raw LLM
    response carried 4-8 valid plans with rent + sqft + beds but
    ``UNKNOWN`` status. Pre-fix, all 4-8 got demoted to plan_summaries
    and only 1 inferred-id row survived to ``units``. Live-verified on
    PIDs 12133 (livebh ashford, 8 plans), 10979 (thehudson, 4 plans),
    47669 (harbor canterbury, 3 plans), 8738 (theentro, 15 plans).

    The new clause: a row that carries CONCRETE physical dimensions
    (rent_low AND sqft both non-null) is a per-unit signal even when
    status is UNKNOWN. The gate is intentionally narrow:

      * Requires BOTH rent_low and sqft. A rent-only row is still
        plan-level (the "$1500-$2000" range guard from 2026-05-17).
      * Stub rows (no rent, no sqft, just a plan code) still fail.
      * The exact-three-plans-same-rent pattern (concession-leak; PID
        11727 "from $675") is caught downstream by the
        ``detect_same_rent_leak`` post-process in ``dq_guards``.

    Recovery estimate at landing: ~400 TIER_4_LLM_DOM properties move
    from 1 unit to 3-8 units shipped.
    """
    for k in _UNIT_LEVEL_SIGNAL_KEYS:
        if is_present(unit.get(k)):
            return True
    # Rent-bearing AVAILABLE row — see docstring.
    if _is_available_with_rent(unit):
        return True
    # 2026-05-24 Phase 0.2: rent + sqft both present → promote even when
    # availability_status is UNKNOWN. See docstring for evidence.
    if _has_rent_and_sqft(unit):
        return True
    return False


def _has_rent_and_sqft(unit: dict[str, Any]) -> bool:
    """``True`` iff *unit* carries both a numeric rent (low or high) AND
    a numeric sqft. Used by :func:`_has_per_unit_signal` to promote
    rent-bearing rows with concrete dimensions to unit-level even when
    ``availability_status`` is UNKNOWN.

    The combination of rent + sqft is the canonical "this is a real
    apartment on inventory" signal: marketing pages advertise ranges
    ("$1500-$2000") without sqft, and plan-summary cards carry sqft
    without rent. Both together indicates a specific priced unit.

    Reads via the canonical alias tables so producer field-name
    variants (``market_rent_low``, ``asking_rent``, ``area``,
    ``minimumsqft``, …) all hit the same predicate.
    """
    has_rent = (
        get_numeric(unit, RENT_LO_KEYS) is not None
        or get_numeric(unit, RENT_HI_KEYS) is not None
    )
    if not has_rent:
        return False
    return get_numeric(unit, SQFT_KEYS) is not None


#: Status-string values that mean "this floor plan has at least one
#: physical unit available". Read via :data:`AVAIL_STATUS_KEYS` so
#: producer variants of the key name (``availability_status`` / ``status``
#: / ``IsAvailable`` / …) all hit the same predicate.
_AVAILABLE_STATUS_VALUES: frozenset[str] = frozenset({
    "AVAILABLE", "AVAIL", "OPEN", "TRUE", "1", "YES",
})


def _is_available_with_rent(unit: dict[str, Any]) -> bool:
    """``True`` iff *unit* carries (a) AVAILABLE status AND (b) any rent
    value. Used by :func:`_has_per_unit_signal` to promote rent-bearing
    available rows to unit-level even when they lack ``available_date``.

    Reads via the canonical alias tables so producer-specific field
    names (``asking_rent``, ``market_rent_low``, ``IsAvailable`` …) all
    hit the same predicate.

    ``"TRUE"`` / ``"1"`` / ``"YES"`` accepted because some producers emit
    boolean ``available: true`` which coerces to ``"True"`` through
    ``get_str``. Bool literals serialised through JSON come back as
    string ``"True"`` / ``"False"``; the uppercase check handles both
    casings.
    """
    status = (get_str(unit, AVAIL_STATUS_KEYS) or "").upper()
    if status not in _AVAILABLE_STATUS_VALUES:
        return False
    if get_numeric(unit, RENT_LO_KEYS) is not None:
        return True
    if get_numeric(unit, RENT_HI_KEYS) is not None:
        return True
    return False


def classify(
    unit: dict[str, Any],
    plan_codes: frozenset[str] | None = None,
) -> UnitLevel:
    """Return ``"unit"`` when the row describes a specific apartment;
    otherwise ``"plan"`` (a floor-plan-level summary).

    Pre-condition: ``infer()`` has run. Inferred identity is recognised
    via the ``_inferred_id`` marker or the ``"inferred_"`` UID prefix.

    Args:
        unit: The unit dict to classify.
        plan_codes: Optional set of normalised plan-code identifiers for
            the surrounding property. Passed through to the unit_number
            promotion check (D16 item 5) — a row whose unit_number
            matches a known plan code is denied promotion to unit-level.

    Defensive: returns ``"plan"`` for non-dict input (the row clearly does
    not carry unit-level identity).
    """
    if not isinstance(unit, dict):
        return "plan"
    if _has_natural_unit_identity(unit, plan_codes=plan_codes):
        return "unit"
    if _has_per_unit_signal(unit):
        return "unit"
    return "plan"
