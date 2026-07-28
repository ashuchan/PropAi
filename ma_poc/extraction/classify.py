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

import re
from typing import Any, Final, Literal

from ma_poc.core.source_ids import PER_UNIT_EVIDENCE_KEYS, normalize_source_id_key
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

# ``Available Now`` is published on both floor-plan cards and actual-unit
# cards, so it is not identity evidence. A calendar date, in contrast, is
# materially more specific: it can only promote an otherwise-anchorless row
# when the source actually displayed a dated availability.
_CALENDAR_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\b(?:19|20)\d{2}\s*[-/]\s*\d{1,2}\s*[-/]\s*\d{1,2}\b|"
    r"\b\d{1,2}\s*[-/]\s*\d{1,2}\s*[-/]\s*(?:19|20)\d{2}\b|"
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+(?:19|20)\d{2}\b)",
    re.IGNORECASE,
)


def _is_specific_calendar_date(value: Any) -> bool:
    """Return whether an availability field contains a published calendar date.

    Textual availability states such as ``Now`` / ``Available Now`` are not
    sufficient because a marketing floor-plan card commonly carries exactly
    the same state. The classifier stays deliberately conservative here: an
    anchorless row with only that state remains a plan summary and is eligible
    for an availability-route recovery.
    """
    return isinstance(value, str) and bool(_CALENDAR_DATE_RE.search(value))


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
        source = getattr(uid_fv, "source", None)
        source_name = getattr(source, "value", source)
        if isinstance(source_name, str) and source_name == "identity_fallback":
            return False
    uid_str = get_str(unit, UID_KEYS)
    if uid_str is None:
        return False
    if uid_str.startswith("inferred_"):
        return False
    # The generic DOM fallback can mistake a layout/control label (for
    # example ``Left`` on Cypress Grove) for a unit number. Reuse the
    # adapter's conservative junk-token vocabulary before that value can
    # suppress unit-level recovery.
    try:
        from ma_poc.pms.adapters._parsing import is_junk_unit_number

        if is_junk_unit_number(uid_str):
            return False
    except Exception:
        # Classification must remain total even if an optional adapter helper
        # is unavailable during an isolated unit test.
        pass
    return True


def _has_per_unit_source_id(unit: dict[str, Any]) -> bool:
    """Return whether registered source provenance proves a physical unit.

    Some API payloads expose a native identifier only in ``source_ids``.  It
    is still a real anchor: paired with ``Available Now`` or ``Lease Now`` it
    must remain a unit row.  The registry keeps this deliberately narrow so
    plan IDs (including RentCafe ``floorplanId``) cannot promote a plan card.
    """
    source_ids = unit.get("source_ids")
    if not isinstance(source_ids, dict):
        return False
    for raw_key, value in source_ids.items():
        if normalize_source_id_key(raw_key) not in PER_UNIT_EVIDENCE_KEYS:
            continue
        if value in (None, "", -1, "-1"):
            continue
        text = str(value).strip()
        if text and text.lower() not in {"null", "none"}:
            return True
    return False


def _has_per_unit_signal(unit: dict[str, Any]) -> bool:
    """``True`` when any per-apartment-only field is populated.

    Plan-level rows don't carry availability dates, floors, or buildings —
    those are specific to a physical apartment in inventory.
    """
    for k in _UNIT_LEVEL_SIGNAL_KEYS:
        value = unit.get(k)
        if k in {"available_date", "availability_date", "availabledate"}:
            if _is_specific_calendar_date(value):
                return True
            continue
        if is_present(value):
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
    if _has_per_unit_source_id(unit):
        return "unit"
    if _has_per_unit_signal(unit):
        return "unit"
    return "plan"
