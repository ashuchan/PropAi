"""
Structured validation and issue logging.
=========================================
Every validation check emits a ValidationIssue with:
  - severity:  ERROR | WARNING | INFO
  - code:      machine-readable enum identifier
  - message:   short human-readable summary
  - details:   free-form dict for debugging (field values, expected ranges, etc.)
  - property canonical_id and row_index when known

The orchestrator collects every issue, writes them to issues.jsonl, and
summarises them in the daily report (grouped by code).

Issue codes — kept short so they aggregate cleanly in reports.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# ── Issue codes (machine-readable) ────────────────────────────────────────────

# Identity
IDENTITY_UNRESOLVED   = "IDENTITY_UNRESOLVED"
IDENTITY_LOW_CONFIDENCE = "IDENTITY_LOW_CONFIDENCE"
DUPLICATE_IDENTITY    = "DUPLICATE_IDENTITY"          # two rows → same canonical_id
SOFT_DUPLICATE_ADDRESS = "SOFT_DUPLICATE_ADDRESS"     # same address, different canonical_id
SOFT_DUPLICATE_GEO    = "SOFT_DUPLICATE_GEO"          # same lat/lng, different canonical_id

# CSV input
CSV_MISSING_URL       = "CSV_MISSING_URL"
CSV_MISSING_REQUIRED  = "CSV_MISSING_REQUIRED"

# Scrape
SCRAPE_FAILED         = "SCRAPE_FAILED"
SCRAPE_NO_APIS        = "SCRAPE_NO_APIS"
SCRAPE_TIMEOUT        = "SCRAPE_TIMEOUT"

# Unit extraction
UNITS_EMPTY           = "UNITS_EMPTY"                 # scrape succeeded, 0 units found
UNIT_INVALID_SCHEMA   = "UNIT_INVALID_SCHEMA"
UNIT_INVALID_RENT     = "UNIT_INVALID_RENT"
UNIT_INVALID_DATE     = "UNIT_INVALID_DATE"
UNIT_MISSING_ID       = "UNIT_MISSING_ID"
UNIT_DUPLICATE_ID     = "UNIT_DUPLICATE_ID"

# Carry-forward
UNITS_CARRIED_FORWARD = "UNITS_CARRIED_FORWARD"
UNITS_DISAPPEARED     = "UNITS_DISAPPEARED"
PROPERTY_DISAPPEARED  = "PROPERTY_DISAPPEARED"        # in state yesterday, not in today's CSV
PROPERTY_NEW          = "PROPERTY_NEW"                # not in state, first time seeing it

# Pipeline
PIPELINE_EXCEPTION    = "PIPELINE_EXCEPTION"

# Rent sanity bounds (USD/month) — used to flag extraction drift.
RENT_MIN_USD = 200
RENT_MAX_USD = 50000

# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class ValidationIssue:
    severity:    str                                  # ERROR | WARNING | INFO
    code:        str
    message:     str
    canonical_id: Optional[str] = None
    row_index:   Optional[int]  = None
    details:     dict[str, Any] = field(default_factory=dict)
    timestamp:   str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

# Convenience constructors — keeps orchestrator code readable.
def _issue(severity: str, code: str, message: str, **kw) -> ValidationIssue:
    return ValidationIssue(severity=severity, code=code, message=message, **kw)

def error(code: str, message: str, **kw) -> ValidationIssue:
    return _issue("ERROR", code, message, **kw)

def warning(code: str, message: str, **kw) -> ValidationIssue:
    return _issue("WARNING", code, message, **kw)

def info(code: str, message: str, **kw) -> ValidationIssue:
    return _issue("INFO", code, message, **kw)

# ── Unit-level validation ─────────────────────────────────────────────────────

REQUIRED_UNIT_KEYS = {
    "unit_id", "market_rent_low", "market_rent_high",
    "available_date", "lease_link", "concessions", "amenities",
}

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def validate_unit(unit: dict, canonical_id: str, idx: int) -> list[ValidationIssue]:
    """
    Validate a single unit dict against the target schema. Returns a list of
    issues; empty list means the unit is clean. Critical issues (missing id,
    bad schema) are ERROR; off-range values are WARNING so the record can still
    be kept in the output for human review.
    """
    issues: list[ValidationIssue] = []

    # Schema: every required key must be present, no unknown keys.
    if not isinstance(unit, dict):
        issues.append(error(
            UNIT_INVALID_SCHEMA,
            f"unit at index {idx} is not a dict",
            canonical_id=canonical_id,
            details={"unit_index": idx, "type": type(unit).__name__},
        ))
        return issues

    missing = REQUIRED_UNIT_KEYS - set(unit.keys())
    extra   = set(unit.keys()) - REQUIRED_UNIT_KEYS
    if missing or extra:
        issues.append(error(
            UNIT_INVALID_SCHEMA,
            f"unit {unit.get('unit_id')} has schema mismatch",
            canonical_id=canonical_id,
            details={"unit_index": idx, "missing": sorted(missing), "extra": sorted(extra)},
        ))

    uid = unit.get("unit_id")
    if not uid or not str(uid).strip():
        issues.append(error(
            UNIT_MISSING_ID,
            f"unit at index {idx} has empty unit_id",
            canonical_id=canonical_id,
            details={"unit_index": idx},
        ))

    lo = unit.get("market_rent_low")
    hi = unit.get("market_rent_high")
    for label, v in (("market_rent_low", lo), ("market_rent_high", hi)):
        if v is None:
            continue
        if not isinstance(v, (int, float)):
            issues.append(warning(
                UNIT_INVALID_RENT,
                f"{label} is not numeric: {v!r}",
                canonical_id=canonical_id,
                details={"unit_id": uid, "field": label, "value": v},
            ))
        elif v < RENT_MIN_USD or v > RENT_MAX_USD:
            issues.append(warning(
                UNIT_INVALID_RENT,
                f"{label}={v} is outside apartment rent range [{RENT_MIN_USD}, {RENT_MAX_USD}]",
                canonical_id=canonical_id,
                details={"unit_id": uid, "field": label, "value": v},
            ))
    if (isinstance(lo, (int, float)) and isinstance(hi, (int, float))
            and lo > hi):
        issues.append(warning(
            UNIT_INVALID_RENT,
            f"market_rent_low ({lo}) > market_rent_high ({hi})",
            canonical_id=canonical_id,
            details={"unit_id": uid, "low": lo, "high": hi},
        ))

    d = unit.get("available_date")
    if d is not None and not (isinstance(d, str) and _ISO_DATE.match(d)):
        issues.append(warning(
            UNIT_INVALID_DATE,
            f"available_date is not YYYY-MM-DD: {d!r}",
            canonical_id=canonical_id,
            details={"unit_id": uid, "value": d},
        ))

    return issues

def validate_units(units: list[dict], canonical_id: str) -> list[ValidationIssue]:
    """Run per-unit validation plus cross-unit duplicate-id detection."""
    issues: list[ValidationIssue] = []
    seen_ids: dict[str, int] = {}
    for i, u in enumerate(units):
        issues.extend(validate_unit(u, canonical_id, i))
        uid = str(u.get("unit_id") or "")
        if uid:
            if uid in seen_ids:
                issues.append(error(
                    UNIT_DUPLICATE_ID,
                    f"duplicate unit_id {uid} within property",
                    canonical_id=canonical_id,
                    details={"unit_id": uid, "first_index": seen_ids[uid], "second_index": i},
                ))
            else:
                seen_ids[uid] = i
    return issues

# ── Issue aggregation helpers ─────────────────────────────────────────────────

def summarise_issues(issues: list[ValidationIssue]) -> dict:
    """Group issues by severity and code for the run report."""
    by_severity: dict[str, int] = {"ERROR": 0, "WARNING": 0, "INFO": 0}
    by_code: dict[str, int] = {}
    for iss in issues:
        by_severity[iss.severity] = by_severity.get(iss.severity, 0) + 1
        by_code[iss.code] = by_code.get(iss.code, 0) + 1
    return {
        "total":       len(issues),
        "by_severity": by_severity,
        "by_code":     dict(sorted(by_code.items(), key=lambda kv: -kv[1])),
    }
