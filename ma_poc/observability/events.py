"""Event definitions and emission for L5 — Observability.

Every layer emits events through emit(). In J1-J4, this is a stub that logs
to the standard logger. J5 replaces the implementation with a real ledger writer.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class EventKind(StrEnum):
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
    DATE_PLACEHOLDER_OBSERVED = "validate.date_placeholder_observed"
    IDENTITY_GAP = "validate.identity_gap"
    TENANT_OFFBOARDED = "fetch.tenant_offboarded"

    # Output
    PROPERTY_EMITTED = "output.property_emitted"
    PROFILE_UPDATED = "output.profile_updated"
    PROFILE_DRIFT = "output.profile_drift_detected"

    # F2: LLM rescue events
    LLM_RESCUE_ATTEMPTED = "extract.llm_rescue_attempted"
    LLM_RESCUE_SUCCEEDED = "extract.llm_rescue_succeeded"
    LLM_RESCUE_FAILED = "extract.llm_rescue_failed"
    # F1.2 (2026-05-09): rescue gate fired but rescue was skipped — e.g.
    # captcha_detected on the fetch_result. Distinct from FAILED so the
    # run report can separate "tried and got nothing" from "didn't try
    # because the input was poisoned".
    LLM_RESCUE_SKIPPED = "extract.llm_rescue_skipped"
    # Bug 5 alignment (2026-05-09): emitted when _refresh_cost_cap_for_hop
    # decides a link-hop body is rich enough to warrant a fresh LLM
    # rescue budget. Lets us measure how often the predicate fires.
    LINK_HOP_BUDGET_REFRESH = "planner.link_hop_budget_refresh"

    # Fetch-tier escalation events (Phase E3+)
    FETCH_TIER_ESCALATED = "fetch.tier_escalated"
    FETCH_TIER_PERSISTED = "fetch.tier_persisted"
    FETCH_TIER_DEMOTED = "fetch.tier_demoted"
    FETCH_LADDER_EXHAUSTED = "fetch.ladder_exhausted"
    FETCH_TIER_PROBE_SUCCESS = "fetch.tier_probe_success"
    FETCH_TIER_PROBE_FAILED = "fetch.tier_probe_failed"
    FETCH_LADDER_BUDGET_EXHAUSTED = "fetch.ladder_budget_exhausted"

    # Cross-source + self-learning events (CLAUDE_XSOURCE_AND_LEARNING)
    # Phase 6 / 7 / 8
    MAPPING_DRIFT_DETECTED = "mapping.drift_detected"
    MAPPING_REPLAY_EMPTY = "mapping.replay_empty"
    MAPPING_EVICTED = "mapping.evicted"
    DOM_HINTS_MISS = "dom_hints.miss"
    DOM_HINTS_EVICTED = "dom_hints.evicted"
    # RC-TRACE (2026-05-15 PM): per-response key-classification trace so
    # offline aggregation can surface vendor key variants the FIELD_ALIASES
    # table is missing. Emitted once per API/JSON-LD/embedded-JSON response
    # considered by the unit-signal qualifier, sampled (default 10% of
    # properties via SIGNAL_INSPECTION_SAMPLE_RATE env). Payload includes
    # the response URL, source_kind, observed keys, normalized matches, and
    # unmatched-but-unit-shaped keys (the alias-table miss candidates).
    # A weekly aggregation over `keys_unmatched_unit_shape` produces the
    # canonical "alias misses" report to feed the alias table.
    SIGNAL_INSPECTION = "extract.signal_inspection"
    # PR 6 (2026-05-10): emitted when self-validation < 0.4 but the
    # ENABLE_DEGRADED_DOM_PERSIST flag let the selectors save anyway.
    # Pair with DOM_HINTS_MISS counts to track how much the loosened gate
    # contributes — and to spot regressions where the flag accidentally
    # flips off (DEGRADED_SAVED → 0 while MISS spikes).
    DOM_HINTS_DEGRADED_SAVED = "dom_hints.degraded_saved"
    # LLM-tax avoidance signal — emitted from generic.py's profile_replay
    # sub-tier whenever a property reached a tier without paying for an
    # LLM call. Pair with MAPPING_REPLAY_EMPTY / DOM_HINTS_MISS to compute
    # an "avoidance rate" per run, which is the canonical regression
    # signal for the self-learning loop. Without this metric, the
    # 462/462 silent-skip regression would re-appear invisibly. See
    # validate_outputs.py / slo_watcher.py for aggregation.
    PROFILE_REPLAY_HIT = "profile.replay_hit"
    PROFILE_REPLAY_MISS_WITH_SAVED = "profile.replay_miss_with_saved"
    FIELD_PATCH_HIT = "field_patch.hit"
    FIELD_PATCH_DRIFT = "field_patch.drift"
    FIELD_PATCH_EVICTED = "field_patch.evicted"
    # PR 1 (2026-05-10) — persistence-channel telemetry. The 2026-05-09 → 2026-05-10
    # regression made the writer-side drops invisible: 125+ daily LLM-API extractions
    # produced mappings that never reached the DB (3 rows total across 5,054 profiles)
    # because three coordinated guards on empty json_paths short-circuited at the
    # producer, the surfacing site, and the persistence call. Without these counters
    # the next instance of the same shape — a writer that runs but never writes — is
    # visible only after a manual DB query.
    MAPPING_SAVE_DROPPED = "mapping.save_dropped"
    PROFILE_UPDATE_FAILED = "profile.update_failed"
    STARTUP_PROBE_FAILED = "startup.probe_failed"
    STARTUP_PROBE_OK = "startup.probe_ok"
    # Phase 5 / 9
    IDENTITY_FUZZY_LINK = "identity.fuzzy_link"
    PLANNER_DECISION = "planner.decision"
    SOURCE_CONTRIBUTED = "source.contributed"
    SOURCES_MERGED = "sources.merged"
    # Phase 12
    CLUSTER_MAPPING_HIT = "cluster.mapping_hit"
    # Phase 13 — periodic
    SLO_REPORT = "slo.report"

    # CLAUDE_PROMPTS_MERGE_RESILIENCE — Phase 8 telemetry
    EXTRACT_FLOOR_PLAN_SNAP = "extract.floor_plan_snap"
    EXTRACT_PHYSICAL_ATTRIBUTE_CONFLICT = "extract.physical_attribute_conflict"
    EXTRACT_AMBIGUOUS_MERGE_FAIL_CLOSED = "extract.ambiguous_merge_fail_closed"
    EXTRACT_AMENITIES_OBSERVED = "extract.amenities_observed"
    EXTRACT_CONCESSION_OBSERVED = "extract.concession_observed"
    EXTRACT_AVAILABILITY_QUANTITY = "extract.availability_quantity_observed"
    # LLM API analysis classified an endpoint as noise (chatbot config,
    # analytics, gallery metadata, etc.). Carries the LLM's free-text
    # reason so analysers can cluster and tune the static blocklist.
    LLM_API_NOISE = "extract.llm_api_noise"
    # 2026-05-15: emitted when the open-by-default unknown-portal scan
    # discovers a cross-origin iframe whose host is NOT on the hardcoded
    # `_PORTAL_URL_PATTERNS` list AND NOT on the infra blacklist. Each
    # event carries the host and URL so cross-run aggregation can surface
    # trending unknown vendors (frequency, first-seen date, properties
    # affected). Hosts with high frequency + successful unit emission
    # are promotion candidates to the hardcoded allow-list. See
    # data/canary/local_runs/fix-validation-v4-wix-2026-05-15/ for the
    # architectural rationale (closed lists block runtime learning).
    EMBEDDED_PORTAL_UNKNOWN_HOST_SEEN = "embedded_portal.unknown_host_seen"


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
