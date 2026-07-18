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


# ─────────────────────────────────────────────────────────────────────
# 2026-05-20 Path C extensions — rent and area signal predicates.
#
# Per investigations/2026-05-20-path-b-design + the
# project_jsonld_recovery_2026-05-20 memo: ``property_passes_quality_gate``
# only requires a numeric DIMENSION (beds/baths/sqft). That admits the
# 1,031-prop inflated-SUCCESS bucket — JSON-LD / inferred_id adapters
# emit rows with ``beds=1, sqft=750, rent=None`` and the gate "passes".
# Those are plan-level rows, not real units.
#
# The retry mechanism needs to distinguish "we got real unit data" from
# "we got a plan-level skeleton with no rent and/or no area". These two
# predicates surface that:
#
#   property_has_rent_signal — does ≥threshold of the rows have a usable
#     numeric rent value? When False AND units list is non-empty, the
#     property is rent-deficient (the JSON-LD-inflated-SUCCESS shape).
#
#   property_has_area_signal — does ≥threshold of the rows have a usable
#     sqft/area value? When False AND rent is also missing, the rows are
#     name-only stubs.
#
# Both are AND'd with the existing ``property_passes_quality_gate`` in
# the Path C trigger, so failing EITHER triggers retry.
# ─────────────────────────────────────────────────────────────────────


def _is_positive_numeric(value: Any) -> bool:
    """Return True when *value* is a positive numeric (>0) OR a string
    that parses to one. Used by the rent/area presence predicates."""
    if value is None:
        return False
    if isinstance(value, bool):  # bool is a subclass of int — exclude
        return False
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        s = value.strip().replace(",", "").replace("$", "")
        if not s:
            return False
        # Strip a trailing decimal so "$1,500.00" parses.
        try:
            return float(s) > 0
        except ValueError:
            return False
    return False


def _rent_gap_documented(unit: dict[str, Any]) -> bool:
    """True when an adapter has verified-and-flagged that the OPERATOR
    does not publish rent for this unit.

    Parallel to :func:`_sqft_gap_documented`. The flag is set by an
    adapter only when 100% of extracted units have no rent value AND
    the structure is clearly unit-level (≥3 units with unit_id +
    area + plan_name). The strongest signal in the wild is SightMap's
    ``show_pricing=true`` config with every unit's ``price: null`` —
    the operator enabled the price column in the widget but didn't
    populate prices, often because they prefer "rent on contact"
    leasing flow.

    Treating a documented gap as rent-present here stops the
    ``no_rent`` retry from firing and stops the verdict layer from
    downgrading to SUCCESS_PLAN_LEVEL — the unit ships as honest
    SUCCESS with transparent ``data_gaps=["rent"]``.
    """
    gaps = unit.get("data_gaps")
    if isinstance(gaps, (list, tuple)) and "rent" in gaps:
        return True
    flag = str(unit.get("data_quality_flag") or "").upper()
    return flag == "RENT_NOT_PUBLISHED"


def _has_rent(unit: dict[str, Any]) -> bool:
    """True when the unit carries at least one numeric rent value
    OR carries a documented rent data gap (operator did not publish)."""
    if any(_is_positive_numeric(unit.get(k)) for k in _RENT_FIELDS):
        return True
    return _rent_gap_documented(unit)


_AREA_FIELDS: tuple[str, ...] = ("sqft", "area", "_sqft", "size", "square_feet")


def _sqft_gap_documented(unit: dict[str, Any]) -> bool:
    """True when an adapter has verified-and-flagged that the OPERATOR
    does not publish sqft for this unit.

    The flag is set by an adapter only after exhausting all enrichment
    paths (e.g. for SecureCafe: plan-name regex, WP-cards, apts247 API
    all returned nothing). Treating a documented gap as area-present
    here stops the ``no_area`` retry from firing on legitimately-
    incomplete-but-extracted units — the property ships as SUCCESS with
    transparent ``data_gaps=["sqft"]`` instead of being marketed as
    SUCCESS_PLAN_LEVEL when the operator simply isn't publishing it.
    """
    gaps = unit.get("data_gaps")
    if isinstance(gaps, (list, tuple)) and "sqft" in gaps:
        return True
    flag = str(unit.get("data_quality_flag") or "").upper()
    return flag == "SQFT_NOT_PUBLISHED"


def _has_area(unit: dict[str, Any]) -> bool:
    """True when the unit carries at least one numeric sqft/area value
    OR carries a documented sqft data gap (operator did not publish)."""
    if any(_is_positive_numeric(unit.get(k)) for k in _AREA_FIELDS):
        return True
    return _sqft_gap_documented(unit)


def _is_plan_presence_marker(unit: dict[str, Any]) -> bool:
    """True when *unit* is a catalogue-presence MARKER row, not a real unit.

    2026-07-11 quality sweep. ``sightmap.parse_sightmap_payload`` (2026-06-27
    chip) emits one row per floor plan that has NO units in ``units[]`` —
    sold-out / not-yet-released plans — marked UNAVAILABLE / available_units=0
    / ``data_quality_flag="SIGHTMAP_PLAN_PRESENCE"`` so the downstream
    catalogue diff sees the full plan inventory. Those rows carry no rent BY
    DESIGN. Counting them in the rent/area-signal DENOMINATORS made a
    property whose every actually-available unit HAS rent+sqft+uid look like
    it "has no rent signal" whenever sold-out plans outnumbered available
    units (e.g. thecharlesslc: 21 real priced units + 26 sold-out plan
    markers → 21/47 = 45% < 0.5). That false negative (a) fired the Path-B
    no_rent/no_area retry for nothing, and (b) stamped the tier
    ``*_PLAN_LEVEL`` + verdict SUCCESS_PLAN_LEVEL — mislabelling genuinely
    unit-level extractions as plan-level (109 props / 1,750 real priced unit
    rows in the 2026-07-11 canary).

    Two shapes, kept tight:
    - explicit: ``data_quality_flag == "SIGHTMAP_PLAN_PRESENCE"``
    - generic sold-out marker: no real unit identity (unit_id/unit_number
      empty or ``inferred_*``) AND explicitly UNAVAILABLE AND zero/absent
      available_units AND no numeric rent. All four must hold — a real
      no-rent unit row keeps its identity or availability and stays in the
      denominator.
    """
    flag = str(unit.get("data_quality_flag") or "").upper()
    if flag == "SIGHTMAP_PLAN_PRESENCE":
        return True
    uid = str(unit.get("unit_id") or unit.get("unit_number") or "").strip()
    if uid and not uid.startswith("inferred_"):
        return False
    if str(unit.get("availability_status") or "").upper() != "UNAVAILABLE":
        return False
    avail_n = str(unit.get("available_units") or "").strip()
    if avail_n not in ("", "0", "0.0", "None"):
        return False
    return not any(_is_positive_numeric(unit.get(k)) for k in _RENT_FIELDS)


def _is_unavailable_rentless(unit: dict[str, Any]) -> bool:
    """True for an UNAVAILABLE row carrying no numeric rent — even with a
    real (non-inferred) backend unit id.

    2026-07-18 verdict-hygiene. SightMap (and similar map-cell) extractions
    emit one row per unit INCLUDING occupied/unavailable cells that carry a
    real ``sightmap_unit_id`` but no published rent. Those are NOT plan
    markers (``_is_plan_presence_marker`` keeps real-uid rows in), yet they
    dilute the rent-signal denominator exactly like markers do — a property
    with 33 priced available units + 100 UNAVAILABLE real-uid map cells reads
    33/133 = 25% and is wrongly demoted to plan-level. Rent-coverage should be
    measured over the units that COULD carry a rent (available / real-anchor);
    an UNAVAILABLE row with no numeric rent cannot, so it is excluded. The
    ``real if real else units`` fallback below still (correctly) reads a
    property whose EVERY row is unavailable-rentless as no-signal / plan-level.
    """
    if str(unit.get("availability_status") or "").upper() != "UNAVAILABLE":
        return False
    return not any(_is_positive_numeric(unit.get(k)) for k in _RENT_FIELDS)


def _signal_denominator(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows that count toward the rent/area-signal ratio.

    Plan-presence markers and UNAVAILABLE-rentless map cells are excluded —
    they carry no rent/area BY DESIGN, so they may not dilute the signal of
    the real available units they ride along with. When EVERY row is excluded,
    fall back to the full list so the property still (correctly) reads as
    no-signal / plan-level.
    """
    real = [
        u
        for u in units
        if not _is_plan_presence_marker(u) and not _is_unavailable_rentless(u)
    ]
    return real if real else units


def property_has_rent_signal(
    units: list[dict[str, Any]],
    threshold: float = 0.5,
) -> bool:
    """Return True when >=threshold of units carry a numeric rent value.

    The 1,031 inflated-SUCCESS JSON-LD bucket consists of properties
    where every row has dimensions but no rent — this predicate is the
    signal that catches that shape and lets the Path C retry mechanism
    fire. Empty list returns False (no units → no rent signal).

    Plan-presence marker rows (sold-out plans emitted for catalogue
    completeness) are excluded from the ratio — see
    :func:`_is_plan_presence_marker`.
    """
    if not units:
        return False
    rows = _signal_denominator(units)
    good = sum(1 for u in rows if _has_rent(u))
    return (good / len(rows)) >= threshold


def property_has_area_signal(
    units: list[dict[str, Any]],
    threshold: float = 0.5,
) -> bool:
    """Return True when >=threshold of units carry a numeric sqft/area value.

    Used alongside rent-signal to detect the "name+beds+baths only" shape
    that some JSON-LD and plan-card adapters emit. Empty list → False.

    Plan-presence marker rows are excluded from the ratio — see
    :func:`_is_plan_presence_marker`.
    """
    if not units:
        return False
    rows = _signal_denominator(units)
    good = sum(1 for u in rows if _has_area(u))
    return (good / len(rows)) >= threshold


@dataclass(frozen=True)
class SchemaGateResult:
    """Result of validating one unit record."""

    accepted: dict[str, Any] | None = None  # Populated on accept
    rejection_reasons: list[str] = field(default_factory=list)
    inferred_id: bool = False


# H5: a record qualifies as having a physical signal when it carries at least
# one of: a numeric dimension (beds/baths/sqft) OR a rent value.
#
# floor_plan_name is deliberately excluded — a row with only unit_number +
# floor_plan_name has identity-text but no measurable data. The 2026-05-11
# CSV showed rows like Sagestone Village (unit=11, fp=B2, no rent, no dims)
# and AppFolio listing IDs (fp="AppFolio listing 3918", no rent, no dims)
# passing the old broad check. Those are name-only stubs, not real units.
_DIMENSION_FIELDS: tuple[str, ...] = (
    "beds",
    "bedrooms",
    "_bedrooms",
    "baths",
    "bathrooms",
    "_bathrooms",
    "sqft",
    "area",
    "_sqft",
)
_RENT_FIELDS: tuple[str, ...] = (
    "asking_rent",
    "market_rent_low",
    "market_rent_high",
    "rent_low",
    "rent_high",
    "rent",
)


def _has_physical_signal(record: dict[str, Any]) -> bool:
    """H5: True when the record carries a numeric dimension OR a rent value.

    Rejected (returns False):
      * unit_id/unit_number alone — no data to merge on
      * unit_number + floor_plan_name only — name-text, not a measurement
      * AppFolio listing-ID floor plans with no rent/dims

    Accepted (returns True):
      * SightMap units: unit_number + rent_low (real price, just missing sqft)
      * Standard units: beds + baths + rent
      * LLM-DOM units that found rent but missed sqft
    """
    return any(_is_present(record.get(k)) for k in _DIMENSION_FIELDS) or any(
        _is_present(record.get(k)) for k in _RENT_FIELDS
    )


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
