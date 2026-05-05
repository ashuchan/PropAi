"""Validation orchestrator — runs schema gate, identity fallback, cross-run sanity.

Pure function of its inputs. No hidden state, no global mutation.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from ..observability.events import EventKind, emit
from .contracts import FlaggedRecord, RejectedRecord, ValidatedRecords
from .cross_run_sanity import check as sanity_check
from .schema_gate import check as schema_check
from .schema_gate import property_passes_quality_gate

log = logging.getLogger(__name__)


_IDENTITY_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "unit_id": ("unit_id", "unit_number"),
    "floor_plan_name": ("floor_plan_name", "_floor_plan", "floor_plan_type", "floorplan_name"),
    "beds": ("beds", "bedrooms", "_bedrooms"),
    "baths": ("baths", "bathrooms", "_bathrooms"),
    "sqft": ("sqft", "_sqft", "area", "square_feet"),
    # F9: extend rent aliases to recognise v2 canonical names. Without these
    # the identity-gap dataset would mis-classify v2-strict records as
    # rent-absent when they actually carry rent_low/rent_high.
    "rent": ("asking_rent", "market_rent_low", "rent", "rent_low", "rent_high"),
    "available_date": ("available_date", "availability_date"),
}


def _normalize_for_signature(value: Any) -> Any:
    """Normalize a field value for the F9 signature key.

    Strings: lowercase + collapse whitespace so "Aspen 1BR" and "aspen  1br"
    hash to the same signature. Numbers / other types are stringified raw.
    Phase 1 cross-run matching depends on this stability — without it, the
    identity-gap dataset would fragment across cosmetic re-formatting on
    the source page.
    """
    if isinstance(value, str):
        return " ".join(value.strip().lower().split())
    return value


def _signature_components(record: dict[str, Any]) -> dict[str, Any]:
    """Compute a tentative identity signature from whatever identifying
    fields ARE present. Used by F9's validate.identity_gap event payload —
    no consumer in Phase 0; builds Phase 1 design dataset.
    """
    out: dict[str, Any] = {}
    for canonical, aliases in _IDENTITY_FIELD_ALIASES.items():
        if canonical in ("rent", "available_date"):
            continue  # never part of identity
        for alias in aliases:
            v = record.get(alias)
            if v is None or v == -1:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            out[canonical] = _normalize_for_signature(v)
            break
    return out


def _signature_key(property_id: str, components: dict[str, Any]) -> str:
    """SHA16 of (property_id, sorted-component-keys) for cross-run matching."""
    parts = [str(property_id)] + [
        f"{k}={components[k]}" for k in sorted(components.keys())
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _field_presence(record: dict[str, Any]) -> dict[str, bool]:
    """Return a flat {field: present?} map for the identity-relevant fields.

    Used to enrich validate.record_rejected events so post-hoc analysis can
    group rejections by field signature without needing the raw record.
    """
    out: dict[str, bool] = {}
    for canonical, aliases in _IDENTITY_FIELD_ALIASES.items():
        present = False
        for alias in aliases:
            v = record.get(alias)
            if v is None or v == -1:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            present = True
            break
        out[canonical] = present
    return out


def validate(
    extract_result: Any,
    history: dict[str, dict[str, Any]] | None = None,
) -> ValidatedRecords:
    """Validate extraction results against schema and history.

    Runs schema gate, identity fallback, and cross-run sanity on each
    record. Tallies results and decides whether next tier is needed.

    Emits events:
      - validate.record_accepted (per accept)
      - validate.record_rejected (per reject, with reasons)
      - validate.record_flagged (per flag set)
      - validate.identity_fallback (per inferred_id used)
      - validate.next_tier_requested (once, if triggered)

    Args:
        extract_result: ExtractResult from L3.
        history: Dict mapping unit_id to last known record.

    Returns:
        ValidatedRecords with accepted/rejected/flagged lists.
    """
    if history is None:
        history = {}

    # F1: schema_gate.check() requires a non-empty property_id (raises ValueError
    # otherwise). Coerce missing/empty to "unknown" so a malformed extract_result
    # never crashes the whole property's validation — the orchestrator's per-record
    # try/except below would otherwise swallow it as VALIDATION_EXCEPTION on every
    # row, hiding the real failure mode.
    property_id = (
        getattr(extract_result, "property_id", None) or "unknown"
    )
    records = extract_result.records if hasattr(extract_result, "records") else []

    accepted: list[dict[str, Any]] = []
    rejected: list[RejectedRecord] = []
    flagged: list[FlaggedRecord] = []
    identity_fallback_count = 0

    for record in records:
        try:
            gate_result = schema_check(record, property_id)

            if gate_result.accepted is None:
                rejected.append(
                    RejectedRecord(
                        raw=record,
                        reasons=gate_result.rejection_reasons,
                        human_message=", ".join(gate_result.rejection_reasons),
                    )
                )
                presence = _field_presence(record)
                emit(
                    EventKind.RECORD_REJECTED,
                    property_id,
                    reasons=gate_result.rejection_reasons,
                    field_presence=presence,
                )
                # F9: emit a parallel identity_gap event for every
                # IDENTITY_FALLBACK_INSUFFICIENT rejection. No consumer in
                # Phase 0 — builds the dataset Phase 1 (tiered output) needs
                # to design signature_key without re-deriving from logs.
                if "IDENTITY_FALLBACK_INSUFFICIENT" in (gate_result.rejection_reasons or []):
                    components = _signature_components(record)
                    sig_key = _signature_key(property_id, components)
                    emit(
                        EventKind.IDENTITY_GAP,
                        property_id,
                        signature_key=sig_key,
                        signature_components=components,
                        field_presence=presence,
                        reason="IDENTITY_FALLBACK_INSUFFICIENT",
                    )
                continue

            if gate_result.inferred_id:
                identity_fallback_count += 1
                emit(
                    EventKind.IDENTITY_FALLBACK, property_id, inferred_id=gate_result.accepted.get("unit_id")
                )

            # Cross-run sanity check
            unit_id = gate_result.accepted.get("unit_id", "")
            hist_record = history.get(unit_id)
            sanity = sanity_check(gate_result.accepted, hist_record)

            if sanity.flags:
                flagged.append(
                    FlaggedRecord(
                        unit=gate_result.accepted,
                        flags=sanity.flags,
                    )
                )
                emit(EventKind.RECORD_FLAGGED, property_id, unit_id=unit_id, flags=sanity.flags)

            accepted.append(gate_result.accepted)
            emit(EventKind.RECORD_ACCEPTED, property_id, unit_id=unit_id)

        except Exception as exc:
            # Never-fail: malformed records produce a rejection, not a crash
            rejected.append(
                RejectedRecord(
                    raw=record,
                    reasons=["VALIDATION_EXCEPTION"],
                    human_message=str(exc),
                )
            )
            log.warning("Validation exception for record: %s", exc)

    # Decide next_tier_requested
    total = len(accepted) + len(rejected)
    next_tier = False
    if total > 0 and len(rejected) > 0:
        reject_ratio = len(rejected) / total
        if reject_ratio > 0.5:
            next_tier = True
            emit(EventKind.NEXT_TIER_REQUESTED, property_id, reject_ratio=reject_ratio)

    # F1: quality gate — if accepted units are all hollow, request the next tier
    # even though the row count passed. This exposes silent-failure properties to
    # the LLM rescue path (F2) rather than marking them SUCCESS with no data.
    if accepted and not property_passes_quality_gate(accepted):
        next_tier = True
        emit(
            EventKind.NEXT_TIER_REQUESTED, property_id, reason="UNITS_HOLLOW_API", hollow_count=len(accepted)
        )
        log.info(
            "Quality gate: %d accepted units for %s are all hollow — next_tier_requested",
            len(accepted),
            property_id,
        )

    return ValidatedRecords(
        property_id=property_id,
        accepted=accepted,
        rejected=rejected,
        flagged=flagged,
        next_tier_requested=next_tier,
        source_extract=extract_result,
        identity_fallback_used_count=identity_fallback_count,
    )
