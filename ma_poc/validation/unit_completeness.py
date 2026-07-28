"""Reconcile emitted units with an exhaustively observed availability route.

Acceptance criteria:
- Count only rows with a real displayed unit anchor and a positive numeric rent.
- Never treat a synthetic id or a floor-plan placeholder as a reconciled unit.
- Mark a property COMPLETE only when the operator supplied an availability
  count for the same route/context and every observed unit is present in the
  emitted output.
- Keep UNKNOWN separate from INCOMPLETE so an unenumerated route cannot be
  reported as complete merely because no mismatch was observed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class UnitCompletenessStatus(StrEnum):
    """Outcome of reconciling one availability route with emitted units."""

    COMPLETE = "COMPLETE"
    COMPLETE_PUBLIC_ZERO = "COMPLETE_PUBLIC_ZERO"
    INCOMPLETE = "INCOMPLETE"
    UNKNOWN = "UNKNOWN"
    ACCESS_BLOCKED = "ACCESS_BLOCKED"


class UnitCompletenessAudit(BaseModel):
    """Evidence-backed completeness decision for one property route/context."""

    status: UnitCompletenessStatus
    routes_checked: list[str] = Field(default_factory=list)
    expected_available_count: int | None = None
    observed_unit_keys: list[str] = Field(default_factory=list)
    captured_unit_keys: list[str] = Field(default_factory=list)
    missing_observed_unit_keys: list[str] = Field(default_factory=list)
    captured_not_observed_unit_keys: list[str] = Field(default_factory=list)
    reason: str


_SYNTHETIC_PREFIXES = ("inferred_", "unkeyable_", "plan_")
_UNIT_KEY_FIELDS = ("unit_number", "apartment_number", "apartment", "unit_id")
_RENT_FIELDS = (
    "rent_low",
    "rent_high",
    "market_rent_low",
    "market_rent_high",
    "asking_rent",
    "rent",
    "price",
)


def _positive_number(value: Any) -> bool:
    """Return whether ``value`` represents a strictly positive numeric rent."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, str):
        value = value.strip().replace("$", "").replace(",", "")
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def normalise_unit_key(value: Any) -> str | None:
    """Return a conservative comparable unit key, rejecting synthetic values.

    The normalization intentionally preserves leading zeroes (``011T`` is
    not necessarily the same unit as ``11T``) while ignoring visual separators
    such as ``#``, spaces, and hyphens.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.lower().startswith(_SYNTHETIC_PREFIXES):
        return None
    normalized = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if not normalized or normalized.isdigit() and len(normalized) > 8:
        return None
    return normalized


def _strict_unit_key(row: dict[str, Any]) -> str | None:
    """Return a comparable key only for a real unit row with a published rent."""
    quality = str(row.get("data_quality_flag") or "").upper()
    tier = str(row.get("extraction_tier") or "").upper()
    if (
        bool(row.get("is_floor_plan_level"))
        or "PLAN_LEVEL" in quality
        or "PLAN_LEVEL" in tier
    ):
        return None
    if not any(_positive_number(row.get(field)) for field in _RENT_FIELDS):
        return None
    for field in _UNIT_KEY_FIELDS:
        key = normalise_unit_key(row.get(field))
        if key:
            return key
    return None


def _strict_keys(rows: Iterable[dict[str, Any]]) -> set[str]:
    """Return deduplicated strict unit keys from arbitrary parsed rows."""
    return {key for row in rows if (key := _strict_unit_key(row)) is not None}


def _make_audit(
    *,
    status: UnitCompletenessStatus,
    reason: str,
    routes: list[str],
    expected_available_count: int | None,
    observed: set[str],
    captured: set[str],
    missing: set[str],
    extra: set[str],
) -> UnitCompletenessAudit:
    """Build a typed immutable audit record from reconciled key sets."""
    return UnitCompletenessAudit(
        status=status,
        reason=reason,
        routes_checked=routes,
        expected_available_count=expected_available_count,
        observed_unit_keys=sorted(observed),
        captured_unit_keys=sorted(captured),
        missing_observed_unit_keys=sorted(missing),
        captured_not_observed_unit_keys=sorted(extra),
    )


def reconcile_unit_completeness(
    *,
    captured_units: Iterable[dict[str, Any]],
    observed_units: Iterable[dict[str, Any]],
    routes_checked: Iterable[str],
    expected_available_count: int | None = None,
    access_blocked: bool = False,
) -> UnitCompletenessAudit:
    """Reconcile emitted units against one fully enumerated public route.

    Args:
        captured_units: Units emitted by the production run for this property.
        observed_units: Units parsed from the public availability route for the
            same move-in date and plan/property context.
        routes_checked: Canonical URLs used to obtain ``observed_units``.
        expected_available_count: Operator-visible available-unit count for
            exactly the same context. ``None`` means enumeration cannot prove
            that the route was exhaustive.
        access_blocked: The known availability route could not be read.

    Returns:
        A decision that distinguishes a proven mismatch, a proven complete
        route, an explicit public zero, an unprovable result, and access block.

    Raises:
        ValueError: If ``expected_available_count`` is negative.
    """
    if expected_available_count is not None and expected_available_count < 0:
        raise ValueError("expected_available_count must be non-negative")

    routes = sorted({route.strip() for route in routes_checked if route.strip()})
    captured = _strict_keys(captured_units)
    observed = _strict_keys(observed_units)
    missing = observed - captured
    extra = captured - observed

    if access_blocked:
        return _make_audit(
            status=UnitCompletenessStatus.ACCESS_BLOCKED,
            reason="Known availability route was not readable; completeness is unproven.",
            routes=routes,
            expected_available_count=expected_available_count,
            observed=observed,
            captured=captured,
            missing=missing,
            extra=extra,
        )
    if expected_available_count is None:
        return _make_audit(
            status=UnitCompletenessStatus.UNKNOWN,
            reason=(
                "No operator-visible count was captured for this exact route/context; "
                "the observed rows cannot prove exhaustive coverage."
            ),
            routes=routes,
            expected_available_count=expected_available_count,
            observed=observed,
            captured=captured,
            missing=missing,
            extra=extra,
        )
    if expected_available_count == 0 and not observed and not captured:
        return _make_audit(
            status=UnitCompletenessStatus.COMPLETE_PUBLIC_ZERO,
            reason="Operator explicitly reported zero available units and no strict unit rows were found.",
            routes=routes,
            expected_available_count=expected_available_count,
            observed=observed,
            captured=captured,
            missing=missing,
            extra=extra,
        )
    if len(observed) != expected_available_count:
        return _make_audit(
            status=UnitCompletenessStatus.INCOMPLETE,
            reason=(
                "Observed strict-unit count does not reconcile with the operator-visible "
                f"count ({len(observed)} observed vs {expected_available_count} expected)."
            ),
            routes=routes,
            expected_available_count=expected_available_count,
            observed=observed,
            captured=captured,
            missing=missing,
            extra=extra,
        )
    if missing:
        return _make_audit(
            status=UnitCompletenessStatus.INCOMPLETE,
            reason=(
                "Production output is missing one or more strict units observed on the "
                "availability route."
            ),
            routes=routes,
            expected_available_count=expected_available_count,
            observed=observed,
            captured=captured,
            missing=missing,
            extra=extra,
        )
    if extra:
        return _make_audit(
            status=UnitCompletenessStatus.INCOMPLETE,
            reason=(
                "Production output includes strict units not present on the same public "
                "availability route."
            ),
            routes=routes,
            expected_available_count=expected_available_count,
            observed=observed,
            captured=captured,
            missing=missing,
            extra=extra,
        )
    return _make_audit(
        status=UnitCompletenessStatus.COMPLETE,
        reason="Operator count and every observed strict unit reconcile with production output.",
        routes=routes,
        expected_available_count=expected_available_count,
        observed=observed,
        captured=captured,
        missing=missing,
        extra=extra,
    )
