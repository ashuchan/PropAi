"""Event definitions and emission for L5 — Observability.

Every layer emits events through emit(). In J1-J4, this is a stub that logs
to the standard logger. J5 replaces the implementation with a real ledger writer.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class EventKind(str, Enum):
    """All event types emitted across the five layers."""

    # Fetch (L1)
    FETCH_STARTED = "fetch.started"
    FETCH_COMPLETED = "fetch.completed"
    FETCH_CACHE_HIT = "fetch.cache_hit"
    FETCH_RETRY = "fetch.retry"
    FETCH_ROTATED_IDENTITY = "fetch.rotated_identity"
    FETCH_BOT_BLOCKED = "fetch.bot_blocked"
    FETCH_CAPTCHA_DETECTED = "fetch.captcha_detected"

    # Discovery (L2)
    TASK_ENQUEUED = "discovery.task_enqueued"
    TASK_SKIPPED_DLQ = "discovery.task_skipped_dlq"
    SITEMAP_FETCHED = "discovery.sitemap_fetched"
    CARRY_FORWARD_APPLIED = "discovery.carry_forward_applied"

    # Extraction (L3)
    PMS_DETECTED = "extract.pms_detected"
    DETECTOR_SIGNALS = "extract.detector_signals"
    HTML_CHARACTERIZED = "extract.html_characterized"
    ADAPTER_SELECTED = "extract.adapter_selected"
    TIER_STARTED = "extract.tier_started"
    TIER_ATTEMPTED = "extract.tier_attempted"
    TIER_WON = "extract.tier_won"
    TIER_FAILED = "extract.tier_failed"
    LLM_CALLED = "extract.llm_called"
    VISION_CALLED = "extract.vision_called"
    LLM_GATE_RELAXED = "extract.llm_gate_relaxed"
    LINK_HOP_STARTED = "extract.link_hop_started"
    LINK_HOP_FETCHED = "extract.link_hop_fetched"
    LINK_HOP_RECOVERED = "extract.link_hop_recovered"

    # Validation (L4)
    RECORD_ACCEPTED = "validate.record_accepted"
    RECORD_REJECTED = "validate.record_rejected"
    RECORD_FLAGGED = "validate.record_flagged"
    IDENTITY_FALLBACK = "validate.identity_fallback"
    NEXT_TIER_REQUESTED = "validate.next_tier_requested"

    # Output
    PROPERTY_EMITTED = "output.property_emitted"
    PROFILE_UPDATED = "output.profile_updated"
    PROFILE_DRIFT = "output.profile_drift_detected"


@dataclass(slots=True, frozen=True)
class Event:
    """Immutable event record emitted by every layer, consumed by L5."""

    kind: EventKind
    property_id: str  # canonical_id; "" for run-level events
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    data: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    task_id: str | None = None

    def to_jsonl(self) -> str:
        """Serialise to a single-line JSON string for append-only ledger."""
        import json

        record = {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "property_id": self.property_id,
            "ts": self.ts.isoformat(),
            "run_id": self.run_id,
            "task_id": self.task_id,
            **self.data,
        }
        return json.dumps(record, default=str)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict."""
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "property_id": self.property_id,
            "ts": self.ts.isoformat(),
            "run_id": self.run_id,
            "task_id": self.task_id,
            "data": self.data,
        }


# --- Real emission (promoted from stub in J5) ---

import threading
from pathlib import Path

_run_id: str = ""
_ledger: Any = None  # EventLedger | None — typed Any to avoid import cycle at load
_ledger_lock = threading.Lock()


def set_run_id(run_id: str) -> None:
    """Set the run-level correlation ID for all subsequent events."""
    global _run_id
    _run_id = run_id


def configure(run_dir: Path, run_id: str) -> None:
    """Configure the event ledger. Called once at daily_runner startup.

    Args:
        run_dir: Path to today's run directory.
        run_id: Run-level correlation ID.
    """
    from .event_ledger import EventLedger

    global _ledger, _run_id
    with _ledger_lock:
        _run_id = run_id
        _ledger = EventLedger(run_dir / "events.jsonl", run_id)


def shutdown() -> None:
    """Flush and close the event ledger. Called at run end."""
    global _ledger
    with _ledger_lock:
        if _ledger is not None:
            _ledger.close()
            _ledger = None


def emit(kind: EventKind, property_id: str, **data: Any) -> Event:
    """Emit an event to the ledger. Never raises.

    Args:
        kind: The event type.
        property_id: Canonical property ID ("" for run-level events).
        **data: Event payload.

    Returns:
        The emitted Event object.
    """
    event = Event(
        kind=kind,
        property_id=property_id,
        data=data,
        run_id=_run_id,
    )

    if _ledger is not None:
        try:
            _ledger.append(event)
        except Exception:
            log.warning("emit failed for %s", kind.value, exc_info=True)
    else:
        log.info("EVENT %s pid=%s %s", kind.value, property_id, data)

    return event
