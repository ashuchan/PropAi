"""L4 — Validation & Schema layer.

Public API: validate(extract_result, history) -> ValidatedRecords
"""

from .contracts import FlaggedRecord, RejectedRecord, ValidatedRecords
from .orchestrator import validate

__all__ = ["FlaggedRecord", "RejectedRecord", "ValidatedRecords", "validate"]
