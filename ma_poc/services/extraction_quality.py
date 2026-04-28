"""Extraction quality checks and unit-merge utilities for the site-universe explorer.

This module provides three things the explorer loop needs:

1. ``meets_minimum_fields(unit)`` — true when a unit has beds AND baths AND sqft
   (the "minimum" set defined in the enhancement spec).
2. ``has_important_fields(unit)`` — true when a unit has rent AND availability date
   (the "important" set; missing these triggers an availability-focused re-rank).
3. ``aggregate_quality(units, threshold)`` — fraction-based property-level verdict
   used to decide whether the loop can stop.
4. ``merge_units(existing, new)`` — additive merge that reuses the dedupe key
   formula already in :func:`ma_poc.pms.adapters.generic.parse_generic_api`
   (``unit_id`` first, then ``floor_plan_name|beds|sqft|rent_low``). On collision,
   keeps the record with more substantive fields, breaking ties by
   per-unit ``_confidence``.

No I/O, no LLM, no async — pure functions, fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# Field-name aliases. The Jugnu pipeline carries records in two shapes during
# its lifetime — the raw v1-ish dicts emitted by parsers and the v2 transform.
# We tolerate both so the quality check can run anywhere in the pipeline.
_BEDS_KEYS: tuple[str, ...] = ("beds", "bedrooms")
_BATHS_KEYS: tuple[str, ...] = ("baths", "bathrooms")
_SQFT_KEYS: tuple[str, ...] = ("sqft", "area", "square_feet")
_RENT_KEYS: tuple[str, ...] = (
    "rent_low",
    "market_rent_low",
    "asking_rent",
    "rent",
)
_AVAILABILITY_KEYS: tuple[str, ...] = (
    "available_date",
    "availability_date",
    "move_in_date",
)
_UNIT_ID_KEYS: tuple[str, ...] = ("unit_id", "unit_number", "_unit_number")
_FLOOR_PLAN_KEYS: tuple[str, ...] = ("floor_plan_name", "_floor_plan", "floorplan_name")


def _present(value: Any) -> bool:
    """Return True when a field carries a real, non-sentinel value.

    Mirrors :func:`ma_poc.validation.schema_gate._is_present` so the two
    quality gates agree on what "missing" means: ``None``, empty string,
    and the ``-1`` area sentinel all read as absent.
    """
    if value is None:
        return False
    if value == -1:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _first_present(unit: dict[str, Any], keys: Iterable[str]) -> Any:
    """Return the first non-empty value across a tuple of aliased field names."""
    for k in keys:
        v = unit.get(k)
        if _present(v):
            return v
    return None


def meets_minimum_fields(unit: dict[str, Any]) -> bool:
    """True when the unit has beds AND baths AND sqft.

    These are the "minimum" fields defined in the universe-explorer spec.
    A unit missing any of the three is treated as too thin to count toward
    a successful extraction stop.
    """
    return all(
        _first_present(unit, keys) is not None
        for keys in (_BEDS_KEYS, _BATHS_KEYS, _SQFT_KEYS)
    )


def has_important_fields(unit: dict[str, Any]) -> bool:
    """True when the unit has rent AND an availability date.

    The "important" set — missing these is what triggers an availability-
    focused re-rank of the remaining queue rather than terminating the loop.
    """
    return (
        _first_present(unit, _RENT_KEYS) is not None
        and _first_present(unit, _AVAILABILITY_KEYS) is not None
    )


@dataclass(frozen=True)
class QualityVerdict:
    """Property-level verdict used by the explorer to decide stop / continue / re-rank."""

    minimum_met: bool
    important_met: bool
    confidence: float
    minimum_pass_rate: float
    important_pass_rate: float
    unit_count: int


def aggregate_quality(
    units: list[dict[str, Any]],
    threshold: float = 0.7,
) -> QualityVerdict:
    """Compute the property-level quality verdict.

    A property is considered to pass a gate when at least ``threshold``
    fraction of its units pass that gate's per-unit predicate. Empty unit
    lists fail both gates with confidence 0.

    The ``confidence`` field averages each unit's ``_confidence`` (set by
    LLM extractors) and falls back to 0 when no unit reports one.
    """
    if not units:
        return QualityVerdict(
            minimum_met=False,
            important_met=False,
            confidence=0.0,
            minimum_pass_rate=0.0,
            important_pass_rate=0.0,
            unit_count=0,
        )

    n = len(units)
    min_pass = sum(1 for u in units if meets_minimum_fields(u)) / n
    imp_pass = sum(1 for u in units if has_important_fields(u)) / n

    confidences = [
        float(u["_confidence"])
        for u in units
        if isinstance(u.get("_confidence"), (int, float))
    ]
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0

    return QualityVerdict(
        minimum_met=min_pass >= threshold,
        important_met=imp_pass >= threshold,
        confidence=avg_conf,
        minimum_pass_rate=min_pass,
        important_pass_rate=imp_pass,
        unit_count=n,
    )


# ---------------------------------------------------------------------------
# merge_units — used to combine results across exploration steps
# ---------------------------------------------------------------------------


def _dedupe_key(unit: dict[str, Any]) -> str:
    """Build the same dedupe key used by parse_generic_api.

    Prefers an explicit unit identifier; falls back to a fingerprint of
    floor plan, beds, sqft, and rent_low. The formula mirrors
    :func:`ma_poc.pms.adapters.generic.parse_generic_api` so units merged
    here collapse the same way they would inside a single tier.
    """
    unit_id = _first_present(unit, _UNIT_ID_KEYS)
    if unit_id is not None:
        return f"id:{unit_id}"
    name = _first_present(unit, _FLOOR_PLAN_KEYS) or ""
    beds = _first_present(unit, _BEDS_KEYS) or ""
    sqft = _first_present(unit, _SQFT_KEYS) or ""
    rent = _first_present(unit, _RENT_KEYS) or ""
    return f"fp:{name}|{beds}|{sqft}|{rent}"


def _substantive_count(unit: dict[str, Any]) -> int:
    """Count how many of beds, baths, sqft, rent, availability are present.

    Used as the primary tiebreaker when two records collide on a dedupe key.
    The record with more populated substantive fields wins.
    """
    return sum(
        1
        for keys in (
            _BEDS_KEYS,
            _BATHS_KEYS,
            _SQFT_KEYS,
            _RENT_KEYS,
            _AVAILABILITY_KEYS,
        )
        if _first_present(unit, keys) is not None
    )


def _merge_field_preserve(existing: Any, candidate: Any) -> Any:
    """Pick the better of two field values without overwriting present with absent."""
    if _present(candidate) and not _present(existing):
        return candidate
    return existing


def _merge_pair(winner: dict[str, Any], loser: dict[str, Any]) -> dict[str, Any]:
    """Merge ``loser``'s populated fields into ``winner`` without overwriting."""
    merged = dict(winner)
    for k, v in loser.items():
        merged[k] = _merge_field_preserve(merged.get(k), v)
    return merged


def merge_units(
    existing: list[dict[str, Any]],
    new: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge two unit lists, deduping on the canonical key.

    On collision:
      1. The record with more substantive fields wins.
      2. Ties broken by higher per-unit ``_confidence`` (defaults to 0).
      3. Final tie: existing record wins (stable order).

    The losing record's populated fields are then merged in without
    overwriting any populated field on the winner — so a partial second
    extraction never erases data the first one captured.
    """
    by_key: dict[str, dict[str, Any]] = {}
    for unit in existing:
        key = _dedupe_key(unit)
        by_key[key] = unit

    for unit in new:
        key = _dedupe_key(unit)
        if key not in by_key:
            by_key[key] = unit
            continue

        current = by_key[key]
        cur_score = _substantive_count(current)
        new_score = _substantive_count(unit)

        if new_score > cur_score:
            winner, loser = unit, current
        elif new_score < cur_score:
            winner, loser = current, unit
        else:
            cur_conf = float(current.get("_confidence") or 0.0)
            new_conf = float(unit.get("_confidence") or 0.0)
            if new_conf > cur_conf:
                winner, loser = unit, current
            else:
                winner, loser = current, unit

        by_key[key] = _merge_pair(winner, loser)

    return list(by_key.values())
