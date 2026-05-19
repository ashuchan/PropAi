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
from typing import Any

from ma_poc.core.identity import compute_fallback_unit_id
from ma_poc.extraction.dates import format_loose_date
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

    # F4: Date validation. Run the lenient shared parser
    # (:func:`ma_poc.extraction.dates.format_loose_date`) — it accepts every
    # producer shape observed in cloud-run telemetry (``"Available 7/4/26"``,
    # ``"Date: 5/22/2026"``, ``"Available Now"``, weekday prefixes, ordinal
    # suffixes, month-day without year, ...).
    #
    # Three outcomes:
    #   * ISO-normalisable        → write back the canonical YYYY-MM-DD
    #     into both ``available_date`` and ``availability_date`` so
    #     downstream consumers see one shape regardless of which key the
    #     producer used. Original is preserved in ``available_date_raw``
    #     so analytics can still see the as-seen string.
    #   * Recognised "absent" token (TBD/N-A/Call/...) → both keys null;
    #     no placeholder telemetry because the producer's intent is
    #     "no date", not "format mismatch".
    #   * Unparseable               → both keys null AND
    #     ``_date_placeholder`` stashed AND
    #     ``DATE_PLACEHOLDER_OBSERVED`` emitted. The placeholder also
    #     surfaces as ``available_date_raw`` on the way to the units
    #     table so producers' literal strings are never lost.
    #
    # Pre-2026-05-19 the gate used a bare ``datetime.fromisoformat``
    # check that nulled out 21K+ rows per day with parseable producer
    # strings. The 2026-05-18 lenient parser shipped in the v2
    # formatter *downstream* of this gate and so was inert in production.
    avail_date_raw = record.get("availability_date") or record.get("available_date")
    if avail_date_raw is not None and isinstance(avail_date_raw, str):
        stripped = avail_date_raw.strip()
        if stripped:
            parsed = format_loose_date(stripped)
            record = dict(record)
            # Always carry the raw producer string forward. The v2
            # formatter / state-store upsert reads this slot so a single
                # column in Postgres preserves "what the website actually
            # said" alongside the typed normalised form.
            record["available_date_raw"] = stripped
            if parsed is not None:
                # Normalised in place — downstream sees one canonical
                # shape regardless of producer.
                record["available_date"] = parsed
                record["availability_date"] = parsed
            else:
                # Producer string didn't match any supported format —
                # placeholder pass-through. Keep the typed columns null
                # so analytics that filter on ``available_date IS NOT NULL``
                # don't trip on un-normalised garbage.
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
        # else: empty / whitespace-only string is "absent", not a
        # placeholder. Leave the record alone.
    elif avail_date_raw is not None and not isinstance(avail_date_raw, str):
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
