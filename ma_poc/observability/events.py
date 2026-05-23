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
    # Captcha hit on a hop (adapter-side probe or /specials probe).
    # Distinct from ``FETCH_CAPTCHA_DETECTED`` (entry-page) so production
    # telemetry can measure the hop-captcha rate without URL filtering.
    # Payload carries ``context`` (``specials_probe`` |
    # ``realpage_cws_probe`` | ``beacon_ajax_probe`` | ``other``) and
    # ``provider`` (``cloudflare`` | ``recaptcha`` | ``hcaptcha`` |
    # ``perimeterx`` | ``sgcaptcha`` | ``unknown``) so a single counter
    # rollup answers "which hop class fights which WAF most often".
    HOP_CAPTCHA_DETECTED = "extract.hop.captcha_detected"
    # Once per property: terminal outcome of the /specials probe.
    # ``outcome`` is ``found`` (concession copy recovered) /
    # ``exhausted`` (no path returned a concession) /
    # ``all_blocked`` (every probed path returned captcha/non-OK).
    # ``paths_attempted`` is the count of paths actually fetched
    # (caps out at ``max_paths`` — useful for cap-tuning analysis).
    CONCESSION_PROBE_RESULT = "extract.concession_probe.result"
    # T2 (2026-05-20): per-property roll-up of date-shape signals seen
    # across every captured HTML body (entry + every hop) AND emitted-side
    # date-fill counts. Pair with extract.html_characterized.date_*_count
    # fields and aggregate in analyze_cloud_run.py to classify each
    # avail-date-only-gap property as Sub-cause A/B/C/D/E. Cheap — one
    # event per property; emitted right before output.property_emitted.
    DATE_PRESENCE_SUMMARY = "extract.date_presence_summary"
    # T5 (2026-05-20): emitted by the v2 formatter when a unit dict carries
    # a date-shaped value under a key the canonical AVAIL_DATE_KEYS alias
    # table doesn't know. Mirrors the existing extract.signal_inspection
    # event for rent keys — weekly aggregation surfaces "missing date
    # aliases" the way signal_inspection surfaces missing rent aliases.
    DATE_EXTRACTION_DROP = "extract.date_extraction_drop"
    # T1.A (2026-05-23): emitted by the v2 formatter when a non-None raw
    # producer date string fails BOTH ``format_loose_date`` AND
    # ``looks_date_like`` — i.e. the string is junk (plan name, marketing
    # copy, UI fragment) and would have leaked into ``available_date``
    # under the pre-T1.A unconditional fallback. Carries the rejected
    # raw value so weekly aggregation can either (a) confirm the rejected
    # value really was junk, or (b) surface a new date-shape variant the
    # looks_date_like predicate should learn. Canonical case: 34 rows of
    # bare ``"Available"`` from SecureCafe (canary 2026-05-23). See
    # docs/dom_quality_and_llm_reduction_playbook.md §T1.A.
    DATE_UNPARSED_SHAPE = "extract.date_unparsed_shape"
    # T2 cohort (2026-05-23): "raw signals for offline pattern learning"
    # — each event carries the offending raw value (truncated) so weekly
    # aggregation can cluster by template and drive per-cohort fixes
    # without re-fetching pages. Each is sampled to ONCE PER PROPERTY per
    # defect class — a 300-unit property with the same defect emits one
    # event, not 300. See docs/dom_quality_and_llm_reduction_playbook.md
    # T2 for the full cohort design.
    #
    # T2.D — property-level: emitted once per property whose units list
    # has ≥3 rows sharing an identical rent_low value AND none of those
    # rows has a real unit_id (all are inferred_*/None). Strong signal of
    # concession-leak or fee-leak (the canonical "from $675" hallucination
    # class). Payload: ``rent_value``, ``n_same_rent_units``,
    # ``has_real_unit_ids``, sample ``fpn_set`` (first 3 plan names).
    SAME_RENT_PROPERTY_OBSERVED = "extract.same_rent_property_observed"
    # T2.F — unit-level (sampled once per property): emitted when a
    # unit's ``market_rent_low`` integer value appears as a literal
    # dollar amount within the unit's ``concession_text``. Direct
    # evidence of the "concession copy mapped to rent" LLM hallucination
    # (PID 11727 risebedfordlake — 13 plans all $675 from "from $675"
    # banner). Payload: ``rent_value``, ``concession_excerpt`` (≤120 ch).
    CONCESSION_TO_RENT_LEAK = "extract.concession_to_rent_leak"
    # T2.E — unit-level (sampled): unit_id case-insensitively equals the
    # row's floor_plan_name. Plan-code masquerading as per-unit identity
    # (PID 229986 theadleylife → A1/A2/A3/B2/C1; PID 254187 →
    # SCH1-SCH4.1). Payload: ``unit_id``, ``floor_plan_name``.
    UNIT_ID_EQUALS_PLAN_NAME = "extract.unit_id_equals_plan_name"
    # T2.B — unit-level (sampled): floor_plan_name length > 35 chars AND
    # contains ≥2 ` - ` separators. LLM-concatenated plan+property+
    # community names ("A3 - Wellesley - Lenox Village & Regent"). Weekly
    # aggregation will surface per-template strip rules. Payload:
    # ``floor_plan_name`` (truncated to 140 ch).
    FLOOR_PLAN_NAME_LONG = "extract.floor_plan_name_long"
    # T2.C — unit-level (sampled): beds=0 AND floor_plan_name has no
    # studio/efficiency/sro/loft token. LLM defaulted bedrooms to 0
    # under uncertainty (PID 10182 fpn="A1", PID 19535 fpn="canterbury",
    # …). Payload: ``floor_plan_name``, ``unit_id_hint``.
    BEDS_ZERO_NON_STUDIO = "extract.beds_zero_non_studio"
    # F8b (2026-05-20): emitted when a marketing-site rent extraction
    # yielded null AND the SecureCafe hop got CF_CHALLENGE. Lets
    # analytics distinguish "rent not in DB because hidden behind CF"
    # from "rent not in DB because extractor missed it." Without this
    # split, all null-rent SUCCESS properties look the same in dashboards.
    RENT_GATED_BY_PORTAL = "extract.rent_gated_by_portal"
    # 2026-05-23 — emitted once per probe entry through ``_probe.probe_get``
    # / ``_probe.probe_post``. Carries the proxy_gate.decide result for
    # downstream cost attribution. Payload: ``url`` (redacted to host+
    # path), ``stage`` (adapter telemetry stage), ``decision_reason``
    # (one of :class:`ma_poc.fetch.proxy_gate.ProxyDecisionReason`),
    # ``via_proxy`` (bool), ``response_bytes``, ``response_status``,
    # ``elapsed_ms``. Strict-allow audit: this event is the canonical
    # ledger of every paid-egress hop in the platform.
    PROXY_DECISION = "fetch.proxy_decision"

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

    # Shard_84 fix follow-up (2026-05-16): wedge-rescue retry telemetry.
    # The runner's wedge-rescue pass (scripts/runners/jugnu.py) detects
    # properties whose RENDER-mode fetch wedged on Playwright IPC and
    # re-queues them with RenderMode.GET. These two events bracket each
    # retry so cross-run aggregation can compute:
    #   - rescue_attempt_rate    = STARTED count / shard-property count
    #   - rescue_recovery_rate   = SUCCEEDED count / STARTED count
    #   - bytes_saved            = (600s wallclock - actual retry seconds)
    #                              × STARTED count
    # WEDGE_RESCUE_RETRY_STARTED fires when the retry CrawlTask is built;
    # WEDGE_RESCUE_RETRY_RESOLVED fires after the retry completes (or is
    # pre-empted) carrying one of the following resolutions:
    #
    #   - ``UPGRADED_TO_SUCCESS``     — retry produced units; original record
    #                                   was upgraded in the result list.
    #   - ``RETRY_ALSO_FAILED``       — retry ran but yielded no units; the
    #                                   original partial-recovery record is
    #                                   preserved as-is.
    #   - ``CRASHED``                 — retry raised an unhandled exception.
    #   - ``SKIPPED_ENTRY_CAPTCHA``   — 2026-05-17: retry was not attempted
    #                                   because the original entry-fetch was
    #                                   captcha-blocked. A HTTP_ONLY retry of
    #                                   a captcha-blocked URL returns the
    #                                   same captcha stub (~200 bytes), which
    #                                   then trips ``LLM_GATE_NO_BODY`` and
    #                                   downgrades the correct
    #                                   ``FAILED_UNREACHABLE`` verdict to
    #                                   ``FAILED_NO_DATA``. Skipping the
    #                                   retry preserves the correct verdict.
    #
    # Until 2026-05-16 the retry started was piggybacked onto
    # PROPERTY_EMITTED with a custom verdict string — that conflated
    # retry telemetry with verdict telemetry and made the rescue rate
    # invisible to the analyzer. These distinct kinds fix that.
    WEDGE_RESCUE_RETRY_STARTED = "extract.wedge_rescue_retry_started"
    WEDGE_RESCUE_RETRY_RESOLVED = "extract.wedge_rescue_retry_resolved"


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
