"""
Composite confidence scoring helpers.

Acceptance criteria:
- All confidence values clamped to [0.0, 1.0] before being assigned to a model
  (bug-hunt #9 — Pydantic Field constraints will reject out-of-range, but
  callers should not rely on that as a guard).
- Per-required-field weighting: required fields drive the score; preferred
  fields raise it; missing required fields degrade it by a fixed step.
"""
from __future__ import annotations

from typing import Iterable, Mapping


REQUIRED_FIELDS = ("unit_number", "asking_rent", "availability_status")
PREFERRED_FIELDS = ("sqft", "floor_plan_type")


def clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def required_field_score(record: Mapping[str, object]) -> float:
    """1.0 if all required fields present, otherwise -0.15 per missing required field."""
    missing = sum(1 for f in REQUIRED_FIELDS if not record.get(f))
    return clamp(1.0 - 0.15 * missing)


def composite(record: Mapping[str, object]) -> float:
    """Required + preferred contribution. Clamped."""
    base = required_field_score(record)
    pref_present = sum(1 for f in PREFERRED_FIELDS if record.get(f))
    bonus = 0.0 if pref_present == len(PREFERRED_FIELDS) else -0.05 * (len(PREFERRED_FIELDS) - pref_present)
    return clamp(base + bonus)


def average(records: Iterable[Mapping[str, object]]) -> float:
    items = list(records)
    if not items:
        return 0.0
    return clamp(sum(composite(r) for r in items) / len(items))


def low_confidence_fields(record: Mapping[str, object]) -> list[str]:
    return [f for f in REQUIRED_FIELDS if not record.get(f)]
