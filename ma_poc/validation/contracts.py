"""Shared contracts for L4 — Validation layer.

ValidatedRecords is the output contract: validated unit records ready for
state_store and output emission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


@dataclass(slots=True, frozen=True)
class RejectedRecord:
    """A record that failed validation."""

    raw: dict[str, Any]
    reasons: list[str]  # Machine-readable reason codes
    human_message: str


@dataclass(slots=True, frozen=True)
class FlaggedRecord:
    """A record that passed validation but is suspicious."""

    unit: Any  # UnitRecord at runtime
    flags: list[str]  # e.g. ["rent_swing_>50pct"]


@dataclass(slots=True, frozen=True)
class ValidatedRecords:
    """Immutable output of L4 validation, consumed by state_store."""

    property_id: str
    accepted: list[Any]  # list[UnitRecord] at runtime
    rejected: list[RejectedRecord]
    flagged: list[FlaggedRecord]
    next_tier_requested: bool  # True if >50% of records were rejected
    source_extract: Any  # ExtractResult back-reference for L5 replay
    identity_fallback_used_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for event emission."""
        return {
            "property_id": self.property_id,
            "accepted_count": len(self.accepted),
            "rejected_count": len(self.rejected),
            "flagged_count": len(self.flagged),
            "next_tier_requested": self.next_tier_requested,
            "identity_fallback_used_count": self.identity_fallback_used_count,
        }
