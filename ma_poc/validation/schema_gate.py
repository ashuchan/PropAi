"""Schema gate — validates unit records against the UnitRecord Pydantic model.

Two paths:
  1. Strict: record has unit_id, rent, all required fields -> accept.
  2. Soft: record missing unit_id -> call compute_fallback_unit_id (v2);
     if fallback returns an id, accept with inferred_id=True; else reject.

``is_substantive`` is preserved as a back-compat shim but now delegates to
``ma_poc.validation.unit_validity.is_valid_unit`` — the single source of
truth for "is this row a unit?" introduced in Stage 1 of the 2026-05-11
regression fix. Callers that previously imported ``is_substantive`` keep
working; their bar is now consistent with adapter-level ``post_process``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ma_poc.core.identity import compute_fallback_unit_id
from ma_poc.validation.unit_validity import is_valid_unit

log = logging.getLogger(__name__)

_MAX_RENT = 50_000
_MAX_SQFT = 20_000

# F1: substantive-field quality gate — v2 canonical names and v1 legacy aliases.
SUBSTANTIVE_FIELDS: tuple[str, ...] = ("beds", "rent_low", "floor_plan_name", "area")
_LEGACY_SUBSTANTIVE_FIELDS: tuple[str, ...] = (
    "bedrooms",
    "asking_rent",
    "market_rent_low",
    "sqft",
    "floor_plan_type",
)


def _is_present(value: Any) -> bool:
    """Return True when a field carries a real value (not None, empty, or -1 sentinel)."""
    if value is None:
        return False
    if value == -1:  # area sentinel used when sqft is absent
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def is_substantive(unit: dict[str, Any]) -> bool:
    """Back-compat shim — delegates to ``unit_validity.is_valid_unit``.

    Historical semantics (pre-Stage-1): accepted ANY one of
    [beds, rent_low, floor_plan_name, area] as substantive — including a
    row with only ``floor_plan_name="Hoboken"`` (the Skyline at Kessler
    regression shape). That bar was inconsistent with the other five gates
    surveyed in the 2026-05-11 design audit.

    Post-Stage-1: delegates to ``is_valid_unit``, which requires at least
    one numeric physical dimension (beds / baths / area), dropping
    identity-text-only and rent-only rows.

    The function name is preserved for back-compat (external test files
    and downstream callers). New code should call ``is_valid_unit``
    directly to make intent explicit.
    """
    return is_valid_unit(unit)


def property_passes_quality_gate(units: list[dict[str, Any]], threshold: float = 0.5) -> bool:
    """Return True when >=threshold fraction of units are substantive.

    An empty list always fails — a property with no units has nothing to
    evaluate. A 50 % default allows a few legitimately sparse units alongside
    full records without triggering a false alarm.
    """
    if not units:
        return False
    good = sum(1 for u in units if is_substantive(u))
    return (good / len(units)) >= threshold


@dataclass(frozen=True)
class SchemaGateResult:
    """Result of validating one unit record."""

    accepted: dict[str, Any] | None = None  # Populated on accept
    rejection_reasons: list[str] = field(default_factory=list)
    inferred_id: bool = False


# H5: a record qualifies as having a physical signal when ANY of these
# v2-canonical fields (or v1 aliases) carries a real (non-empty, non -1) value.
_PHYSICAL_SIGNAL_FIELDS: tuple[str, ...] = (
    "floor_plan_name",
    "floor_plan_type",
    "floorplan_name",
    "floor_plan_id",
    "beds",
    "bedrooms",
    "_bedrooms",
    "baths",
    "bathrooms",
    "_bathrooms",
    "sqft",
    "area",
    "_sqft",
    "asking_rent",
    "market_rent_low",
    "market_rent_high",
    "rent_low",
    "rent_high",
    "rent",
)


def _has_physical_signal(record: dict[str, Any]) -> bool:
    """H5: True when at least one identity-bearing physical field is present.

    A record carrying nothing but ``unit_id`` is rejected by ``check`` —
    a unit number alone is not enough identity to merge confidently.
    """
    return any(_is_present(record.get(k)) for k in _PHYSICAL_SIGNAL_FIELDS)


def check(record: dict[str, Any], property_id: str) -> SchemaGateResult:
    """Validate a single unit record against the schema.

    Args:
        record: Raw unit record dict from L3 extraction.
        property_id: The property the record belongs to. REQUIRED — the v2
            fallback hashes this into the inferred unit_id so two physically
            different units in different properties cannot collide. Pass the
            real property identifier; never an empty string.

    Returns:
        SchemaGateResult with accepted record or rejection reasons.

    Raises:
        ValueError: when property_id is empty. Earlier drafts allowed an
            empty default for transition; that's been removed because an
            empty namespace produces colliding fallback IDs across
            properties — exactly the bug F1 was meant to eliminate.
    """
    if not property_id:
        raise ValueError(
            "schema_gate.check() requires a non-empty property_id; "
            "v2 fallback IDs depend on it for cross-property uniqueness"
        )
    reasons: list[str] = []

    # F2: Rent validation. v1 names retain priority for back-compat (H15);
    # v2 canonical names (rent_low/rent_high) appended so v2-strict DB rows
    # do not bypass the absurd/negative checks.
    rent = (
        record.get("asking_rent")
        or record.get("market_rent_low")
        or record.get("rent")
        or record.get("rent_low")
        or record.get("rent_high")
    )
    if rent is not None:
        try:
            rent_val = float(rent)
            if rent_val < 0:
                reasons.append("INVALID_RENT_NEGATIVE")
            elif rent_val > _MAX_RENT:
                reasons.append("INVALID_RENT_ABSURD")
        except (ValueError, TypeError):
            reasons.append("INVALID_RENT_NEGATIVE")

    # F3: Sqft validation. v1 names first (sqft/square_feet), v2 canonical
    # `area` last. The -1 sentinel ("unknown sqft") is preserved across all
    # name aliases — treated as null rather than triggering INVALID_SQFT_NEGATIVE.
    sqft = record.get("sqft")
    if sqft in (None, "", -1, "-1"):
        sqft = record.get("square_feet")
    if sqft in (None, "", -1, "-1"):
        sqft = record.get("area")
    if sqft not in (None, "", -1, "-1"):
        try:
            sqft_val = float(sqft)
            if sqft_val == -1:
                pass  # sentinel → treat as null
            elif sqft_val < 0:
                reasons.append("INVALID_SQFT_NEGATIVE")
            elif sqft_val > _MAX_SQFT:
                reasons.append("INVALID_SQFT_ABSURD")
        except (ValueError, TypeError):
            pass

    # F4: Date validation. Unparseable strings re-route as placeholder
    # pass-through (null both aliases, stash original in _date_placeholder,
    # emit telemetry). Non-string corrupted types still reject.
    avail_date = record.get("availability_date") or record.get("available_date")
    if avail_date is not None and isinstance(avail_date, str):
        stripped = avail_date.strip()
        if not stripped:
            # Empty / whitespace-only string is "absent", not a placeholder.
            # Pre-F4 behavior was the same; treat it like None.
            pass
        else:
            parsed = False
            try:
                datetime.fromisoformat(stripped.replace("Z", "+00:00"))
                parsed = True
            except ValueError:
                try:
                    date.fromisoformat(stripped)
                    parsed = True
                except ValueError:
                    parsed = False
            if not parsed:
                record = dict(record)
                record["_date_placeholder"] = stripped
                record["available_date"] = None
                record["availability_date"] = None
                try:
                    from ma_poc.observability.events import EventKind, emit
                    emit(
                        EventKind.DATE_PLACEHOLDER_OBSERVED,
                        property_id,
                        placeholder_value=stripped[:64],
                    )
                except (ImportError, AttributeError):
                    # Event module not yet upgraded with new EventKind.
                    # Anything else (TypeError, OSError) must surface.
                    pass
    elif avail_date is not None and not isinstance(avail_date, str):
        # H7: non-string date values (corrupted types, ints) still reject.
        reasons.append("INVALID_DATE_FORMAT")

    # F1: Unit ID — if missing, try v2 fallback. Stable across rent/date
    # changes (rent and available_date deliberately excluded from the hash).
    unit_id = record.get("unit_id") or record.get("unit_number")
    inferred = False
    if not unit_id:
        fallback_id = compute_fallback_unit_id(record, property_id)
        if fallback_id:
            record = dict(record)  # Don't mutate original
            record["unit_id"] = fallback_id
            inferred = True
        else:
            reasons.append("IDENTITY_FALLBACK_INSUFFICIENT")
    else:
        # H5: a record carrying ONLY unit_id (no rent, no beds, no plan name,
        # no sqft, no anything else identity-bearing) cannot be merged
        # confidently. Reject before the IDENTITY_FALLBACK_INSUFFICIENT path
        # so the failure is diagnosable.
        if not _has_physical_signal(record):
            reasons.append("IDENTITY_REQUIRES_PHYSICAL_SIGNAL")

    if reasons:
        return SchemaGateResult(accepted=None, rejection_reasons=reasons)

    return SchemaGateResult(accepted=record, inferred_id=inferred)
