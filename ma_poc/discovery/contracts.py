"""Shared contracts for L2 — Discovery layer.

CrawlTask is the single output contract produced by L2 and consumed by L1.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..fetch.contracts import RenderMode


class TaskReason(StrEnum):
    """Why this task was created."""

    SCHEDULED = "SCHEDULED"
    CARRY_FORWARD_CHECK = "CARRY_FORWARD_CHECK"
    RETRY = "RETRY"
    SITEMAP_DISCOVERED = "SITEMAP_DISCOVERED"
    DLQ_REVIVE = "DLQ_REVIVE"
    MANUAL = "MANUAL"


@dataclass(slots=True, frozen=True)
class CrawlTask:
    """Immutable work item produced by L2, consumed by L1."""

    url: str
    property_id: str  # canonical_id
    priority: int  # 0 = highest
    budget_ms: int  # wall-clock budget for the fetch
    reason: TaskReason
    render_mode: RenderMode  # HEAD / GET / RENDER
    parent_task_id: str | None = None  # For retries and redirect chains
    expected_pms: str | None = None  # From profile.api_hints.api_provider
    # Conditional GET hints
    etag: str | None = None
    last_modified: str | None = None
    # Per-host session token if profile stickied one
    session_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for event emission."""
        return {
            "url": self.url,
            "property_id": self.property_id,
            "priority": self.priority,
            "budget_ms": self.budget_ms,
            "reason": self.reason.value,
            "render_mode": self.render_mode.value,
            "parent_task_id": self.parent_task_id,
            "expected_pms": self.expected_pms,
            "etag": self.etag,
            "last_modified": self.last_modified,
        }
