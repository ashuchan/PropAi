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
    UID_KEYS,
    get_str,
    is_present,
)

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


def _has_natural_unit_identity(unit: dict[str, Any]) -> bool:
    """``True`` when ``unit_id`` (or its aliases) carries a real per-apartment
    identifier — not an ``inferred_<hash>`` fallback, not a floorplan-level
    surrogate.

    Rules:
      * If ``_inferred_id`` is True → inferred identity → not natural.
      * If the resolved UID is None or starts with ``inferred_`` → not natural.
      * Otherwise → natural.
    """
    # Explicit infer marker wins.
    if unit.get("_inferred_id") is True:
        return False
    # FieldValue from the merger carries provenance — IDENTITY_FALLBACK is
    # the in-band signal that the unit_id was computed, not extracted.
    uid_fv = unit.get("unit_id")
    if hasattr(uid_fv, "source") and hasattr(uid_fv, "value"):
        source_name = getattr(uid_fv.source, "value", uid_fv.source)
        if isinstance(source_name, str) and source_name == "identity_fallback":
            return False
    uid_str = get_str(unit, UID_KEYS)
    if uid_str is None:
        return False
    if uid_str.startswith("inferred_"):
        return False
    return True


def _has_per_unit_signal(unit: dict[str, Any]) -> bool:
    """``True`` when any per-apartment-only field is populated.

    Plan-level rows don't carry availability dates, floors, or buildings —
    those are specific to a physical apartment in inventory.
    """
    for k in _UNIT_LEVEL_SIGNAL_KEYS:
        if is_present(unit.get(k)):
            return True
    return False


def classify(unit: dict[str, Any]) -> UnitLevel:
    """Return ``"unit"`` when the row describes a specific apartment;
    otherwise ``"plan"`` (a floor-plan-level summary).

    Pre-condition: ``infer()`` has run. Inferred identity is recognised
    via the ``_inferred_id`` marker or the ``"inferred_"`` UID prefix.

    Defensive: returns ``"plan"`` for non-dict input (the row clearly does
    not carry unit-level identity).
    """
    if not isinstance(unit, dict):
        return "plan"
    if _has_natural_unit_identity(unit):
        return "unit"
    if _has_per_unit_signal(unit):
        return "unit"
    return "plan"
