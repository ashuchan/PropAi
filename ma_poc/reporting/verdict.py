"""Verdict computation — derives a property-level verdict from pipeline results.

Decision rules (first match wins):
1. carry_forward_applied → CARRY_FORWARD
2. fetch hard-fail → FAILED_UNREACHABLE
3. extract empty → FAILED_NO_DATA
4. majority rejected → PARTIAL
5. else → SUCCESS
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

log = logging.getLogger(__name__)


class Verdict(str, Enum):
    """Property-level outcome verdict."""

    SUCCESS = "SUCCESS"
    FAILED_UNREACHABLE = "FAILED_UNREACHABLE"
    FAILED_NO_DATA = "FAILED_NO_DATA"
    CARRY_FORWARD = "CARRY_FORWARD"
    PARTIAL = "PARTIAL"


@dataclass(frozen=True)
class VerdictResult:
    """Immutable verdict for a property scrape."""

    verdict: Verdict
    reason: str
    source: str  # "fetch", "extract", "validate", "carry_forward"


def compute(
    fetch_outcome: str | None = None,
    extract_result: Any = None,
    validated: Any = None,
    carry_forward_applied: bool = False,
    units_hollow: bool = False,
) -> VerdictResult:
    """Compute the verdict for a property scrape.

    Args:
        fetch_outcome: FetchOutcome value string.
        extract_result: ExtractResult or dict with records.
        validated: ValidatedRecords or None.
        carry_forward_applied: Whether carry-forward was used.

    Returns:
        VerdictResult with verdict, reason, and source.
    """
    # F7 fix: carry-forward is always checked first, before any other signal.
    if carry_forward_applied:
        return VerdictResult(Verdict.CARRY_FORWARD, "carry_forward_applied", "carry_forward")

    if fetch_outcome and fetch_outcome not in ("OK", "NOT_MODIFIED"):
        return VerdictResult(
            Verdict.FAILED_UNREACHABLE,
            f"fetch outcome: {fetch_outcome}",
            "fetch",
        )

    # Check extract result
    if extract_result is not None:
        records = getattr(extract_result, "records", None)
        if records is None and isinstance(extract_result, dict):
            records = extract_result.get("records", extract_result.get("units", []))
        if not records:
            return VerdictResult(Verdict.FAILED_NO_DATA, "no records extracted", "extract")
    else:
        return VerdictResult(Verdict.FAILED_NO_DATA, "no extract result", "extract")

    # Check validation
    if validated is not None:
        rejected_count = len(getattr(validated, "rejected", []))
        accepted_count = len(getattr(validated, "accepted", []))
        if rejected_count > accepted_count and (rejected_count + accepted_count) > 0:
            return VerdictResult(Verdict.PARTIAL, "majority rejected by validation", "validate")

    # F1: if units were extracted but all are hollow and no rescue tier helped
    if units_hollow:
        return VerdictResult(Verdict.FAILED_NO_DATA, "units_hollow_all_tiers", "validate")

    return VerdictResult(Verdict.SUCCESS, "all checks passed", "extract")
