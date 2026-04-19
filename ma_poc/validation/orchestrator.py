"""Validation orchestrator — runs schema gate, identity fallback, cross-run sanity.

Pure function of its inputs. No hidden state, no global mutation.
"""
from __future__ import annotations

import logging
from typing import Any

from ..observability.events import EventKind, emit
from .contracts import FlaggedRecord, RejectedRecord, ValidatedRecords
from .cross_run_sanity import check as sanity_check
from .schema_gate import check as schema_check

log = logging.getLogger(__name__)


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

    property_id = extract_result.property_id if hasattr(extract_result, "property_id") else "unknown"
    records = extract_result.records if hasattr(extract_result, "records") else []

    accepted: list[dict[str, Any]] = []
    rejected: list[RejectedRecord] = []
    flagged: list[FlaggedRecord] = []
    identity_fallback_count = 0

    for record in records:
        try:
            gate_result = schema_check(record)

            if gate_result.accepted is None:
                rejected.append(RejectedRecord(
                    raw=record,
                    reasons=gate_result.rejection_reasons,
                    human_message=", ".join(gate_result.rejection_reasons),
                ))
                emit(EventKind.RECORD_REJECTED, property_id,
                     reasons=gate_result.rejection_reasons)
                continue

            if gate_result.inferred_id:
                identity_fallback_count += 1
                emit(EventKind.IDENTITY_FALLBACK, property_id,
                     inferred_id=gate_result.accepted.get("unit_id"))

            # Cross-run sanity check
            unit_id = gate_result.accepted.get("unit_id", "")
            hist_record = history.get(unit_id)
            sanity = sanity_check(gate_result.accepted, hist_record)

            if sanity.flags:
                flagged.append(FlaggedRecord(
                    unit=gate_result.accepted,
                    flags=sanity.flags,
                ))
                emit(EventKind.RECORD_FLAGGED, property_id,
                     unit_id=unit_id, flags=sanity.flags)

            accepted.append(gate_result.accepted)
            emit(EventKind.RECORD_ACCEPTED, property_id, unit_id=unit_id)

        except Exception as exc:
            # Never-fail: malformed records produce a rejection, not a crash
            rejected.append(RejectedRecord(
                raw=record,
                reasons=["VALIDATION_EXCEPTION"],
                human_message=str(exc),
            ))
            log.warning("Validation exception for record: %s", exc)

    # Decide next_tier_requested
    total = len(accepted) + len(rejected)
    next_tier = False
    if total > 0 and len(rejected) > 0:
        reject_ratio = len(rejected) / total
        if reject_ratio > 0.5:
            next_tier = True
            emit(EventKind.NEXT_TIER_REQUESTED, property_id,
                 reject_ratio=reject_ratio)

    return ValidatedRecords(
        property_id=property_id,
        accepted=accepted,
        rejected=rejected,
        flagged=flagged,
        next_tier_requested=next_tier,
        source_extract=extract_result,
        identity_fallback_used_count=identity_fallback_count,
    )
