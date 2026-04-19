# Jugnu Shared Contracts

> The dataclasses and protocols that cross layer boundaries. Every phase
> imports from the modules defined here. Change the shape of any of these
> with intent — a silent field add ripples across all five layers.

These live under `ma_poc/<layer>/contracts.py`. Each phase file points
back here for the authoritative shape. When a phase needs to *extend*
one of these (e.g. add a field), do it here first, update the phase's
producer to populate it, then every consumer.

Each contract:

- is a `@dataclass(slots=True, frozen=True)` by default — frozen so
  passing one between layers cannot mutate the upstream producer's copy.
- uses **simple types** — no Pydantic for the hot path (Pydantic is for
  validation at L4 only).
- exposes one `to_dict()` method returning a plain `dict` for event
  emission to L5.

---

## 1. `FetchResult` — L1 output → L2/L3 input

**Module:** `ma_poc/fetch/contracts.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

class RenderMode(str, Enum):
    HEAD = "HEAD"              # cheap change probe
    GET = "GET"                # static HTML / JSON
    RENDER = "RENDER"          # Playwright with network capture

class FetchOutcome(str, Enum):
    OK = "OK"                  # 2xx, body available
    NOT_MODIFIED = "NOT_MODIFIED"   # 304, use carry-forward
    BOT_BLOCKED = "BOT_BLOCKED"     # CAPTCHA / 403 pattern
    RATE_LIMITED = "RATE_LIMITED"   # 429 with Retry-After
    TRANSIENT = "TRANSIENT"         # 5xx, timeout, retriable
    HARD_FAIL = "HARD_FAIL"         # SSL, DNS, 4xx non-retriable
    PROXY_ERROR = "PROXY_ERROR"     # 407, proxy exhausted

@dataclass(slots=True, frozen=True)
class FetchResult:
    url: str
    outcome: FetchOutcome
    status: int | None                  # HTTP status (None if no response)
    body: bytes | None                  # Raw body; None for HEAD or failures
    headers: dict[str, str]             # Lowercased header names
    render_mode: RenderMode
    final_url: str                      # After redirects
    attempts: int                       # Total attempts made (≥1)
    elapsed_ms: int
    # Present only when render_mode == RENDER
    network_log: list[dict] = field(default_factory=list)
    # Populated by the conditional-GET layer when we would have sent
    # If-None-Match / If-Modified-Since on next fetch
    etag: str | None = None
    last_modified: str | None = None
    # Populated by response_classifier when this is a retriable outcome
    error_signature: str | None = None  # e.g. "ERR_SSL_PROTOCOL_ERROR"
    proxy_used: str | None = None       # Redacted (no credentials)

    def ok(self) -> bool:
        return self.outcome == FetchOutcome.OK

    def should_carry_forward(self) -> bool:
        return self.outcome in (
            FetchOutcome.NOT_MODIFIED,
            FetchOutcome.TRANSIENT,
            FetchOutcome.BOT_BLOCKED,
        )

    def to_dict(self) -> dict[str, Any]: ...
```

**Invariant:** `FetchResult` is **never** raised as an exception. L1
catches all transient and hard errors and returns a `FetchResult` with
the appropriate `outcome`. This is the centrepiece of the never-fail
contract.

---

## 2. `CrawlTask` — L2 output → L1 input

**Module:** `ma_poc/discovery/contracts.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum

class TaskReason(str, Enum):
    SCHEDULED = "SCHEDULED"          # daily run, routine
    CARRY_FORWARD_CHECK = "CARRY_FORWARD_CHECK"   # cheap HEAD to probe
    RETRY = "RETRY"                  # retry after transient fail
    SITEMAP_DISCOVERED = "SITEMAP_DISCOVERED"     # url found in sitemap.xml
    DLQ_REVIVE = "DLQ_REVIVE"        # hourly retry of parked property
    MANUAL = "MANUAL"                # human-triggered

@dataclass(slots=True, frozen=True)
class CrawlTask:
    url: str
    property_id: str                  # canonical_id
    priority: int                     # 0 = highest
    budget_ms: int                    # wall-clock budget for the fetch
    reason: TaskReason
    render_mode: "RenderMode"         # HEAD / GET / RENDER
    parent_task_id: str | None = None  # For retries and redirect chains
    expected_pms: str | None = None   # From profile.api_hints.api_provider
    # Conditional GET hints — L1 sends these as If-None-Match etc.
    etag: str | None = None
    last_modified: str | None = None
    # Per-host cookies / session token if the profile stickied one
    session_key: str | None = None

    def to_dict(self) -> dict: ...
```

---

## 3. `ExtractResult` — L3 output → L4 input

**Module:** `ma_poc/pms/contracts.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass(slots=True, frozen=True)
class ExtractResult:
    property_id: str
    records: list[dict]               # Unit-shaped dicts — not yet validated
    tier_used: str                    # e.g. "ADAPTER_ENTRATA", "GENERIC_TIER_3_DOM"
    adapter_name: str                 # e.g. "entrata", "generic"
    winning_url: str | None           # URL/endpoint that produced data
    confidence: float                 # 0–1, adapter's own confidence
    # Cost accounting (L5 consumes this)
    llm_cost_usd: float = 0.0
    vision_cost_usd: float = 0.0
    llm_calls: int = 0
    vision_calls: int = 0
    # Profile-learning payload (L3 → profile writer at end of scrape)
    profile_hints: "ProfileHints | None" = None
    # Non-fatal errors collected during extraction
    errors: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return len(self.records) == 0

    def to_dict(self) -> dict: ...


@dataclass(slots=True, frozen=True)
class ProfileHints:
    """What the extractor learned. Consumed by the profile_updater."""
    api_endpoints_with_data: list[tuple[str, str]] = field(default_factory=list)   # (url, provider)
    api_endpoints_blocked: list[tuple[str, str]] = field(default_factory=list)     # (url, reason)
    llm_field_mappings: list[dict] = field(default_factory=list)                   # LlmFieldMapping dicts
    css_selectors: dict[str, str] = field(default_factory=dict)
    platform_detected: str | None = None
    winning_page_path: str | None = None

    def to_dict(self) -> dict: ...
```

---

## 4. `ValidatedRecords` — L4 output → state_store input

**Module:** `ma_poc/validation/contracts.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass(slots=True, frozen=True)
class RejectedRecord:
    raw: dict
    reasons: list[str]                # Machine-readable reason codes
    human_message: str

@dataclass(slots=True, frozen=True)
class FlaggedRecord:
    unit: "UnitRecord"                # Passed validation, but suspicious
    flags: list[str]                  # e.g. ["rent_swing_>50pct"]

@dataclass(slots=True, frozen=True)
class ValidatedRecords:
    property_id: str
    accepted: list["UnitRecord"]
    rejected: list[RejectedRecord]
    flagged: list[FlaggedRecord]
    next_tier_requested: bool         # True if >50% of records were rejected
    source_extract: ExtractResult     # back-reference for L5 replay
    identity_fallback_used_count: int = 0

    def to_dict(self) -> dict: ...
```

`next_tier_requested` is the control signal L4 sends back up to L3 to
say "your extraction was mostly bad, try the next tier." L3's
orchestrator reads this and loops.

---

## 5. `Event` — emitted by every layer → L5 consumes

**Module:** `ma_poc/observability/events.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from typing import Any
import uuid

class EventKind(str, Enum):
    # Fetch (L1)
    FETCH_STARTED = "fetch.started"
    FETCH_COMPLETED = "fetch.completed"
    FETCH_CACHE_HIT = "fetch.cache_hit"
    FETCH_RETRY = "fetch.retry"
    FETCH_ROTATED_IDENTITY = "fetch.rotated_identity"
    FETCH_BOT_BLOCKED = "fetch.bot_blocked"

    # Discovery (L2)
    TASK_ENQUEUED = "discovery.task_enqueued"
    TASK_SKIPPED_DLQ = "discovery.task_skipped_dlq"
    SITEMAP_FETCHED = "discovery.sitemap_fetched"
    CARRY_FORWARD_APPLIED = "discovery.carry_forward_applied"

    # Extraction (L3)
    PMS_DETECTED = "extract.pms_detected"
    ADAPTER_SELECTED = "extract.adapter_selected"
    TIER_STARTED = "extract.tier_started"
    TIER_WON = "extract.tier_won"
    TIER_FAILED = "extract.tier_failed"
    LLM_CALLED = "extract.llm_called"
    VISION_CALLED = "extract.vision_called"

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
    kind: EventKind
    property_id: str                  # canonical_id; "" for run-level events
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # Layer-specific payload — keep flat, serialise to one-line JSON
    data: dict[str, Any] = field(default_factory=dict)
    # Run-level correlation
    run_id: str = ""
    task_id: str | None = None

    def to_jsonl(self) -> str: ...
```

All events are **append-only** to `data/runs/<date>/events.jsonl`.
Appends are line-buffered, so a crash mid-run leaves a valid prefix.

---

## 6. `PmsAdapter` protocol — implemented by L3 adapters

**Module:** `ma_poc/pms/adapters/base.py`

This is already specified in detail in `claude_refactor.md` Phase 2. We
lift the contract here for cross-reference only; the authoritative
definition stays in the refactor doc.

```python
from typing import Protocol, runtime_checkable
from playwright.async_api import Page
from dataclasses import dataclass

@dataclass
class AdapterContext:
    base_url: str
    detected: "DetectedPMS"
    profile: "ScrapeProfile | None"
    expected_total_units: int | None
    property_id: str
    # NEW in Jugnu: the L1 fetch result is handed to the adapter so it
    # doesn't re-fetch. For adapters that need live Playwright (most),
    # the page is passed too; the FetchResult is a reference for replay.
    fetch_result: "FetchResult"

@dataclass
class AdapterResult:
    units: list[dict]
    tier_used: str
    winning_url: str | None
    api_responses: list[dict]
    blocked_endpoints: list[tuple[str, str]]
    llm_field_mappings: list[dict]
    errors: list[str]
    confidence: float

@runtime_checkable
class PmsAdapter(Protocol):
    pms_name: str
    async def extract(self, page: "Page", ctx: AdapterContext) -> AdapterResult: ...
    def static_fingerprints(self) -> list[str]: ...
```

**Delta from `claude_refactor.md`:** `AdapterContext` now carries
`fetch_result`. This is the only mandatory change to the adapter
protocol for Jugnu. Existing adapter implementations can ignore the
field — it's for the Generic adapter's cascade to consult.

---

## 7. `DetectedPMS` — detector output

**Module:** `ma_poc/pms/detector.py`

Unchanged from `claude_refactor.md` Phase 1. Lifted here for reference:

```python
@dataclass(frozen=True)
class DetectedPMS:
    pms: Literal[
        "rentcafe", "entrata", "appfolio", "onesite",
        "sightmap", "realpage_oll", "avalonbay",
        "squarespace_nopms", "wix_nopms", "custom", "unknown",
    ]
    confidence: float                  # 0.0–1.0
    evidence: list[str]
    pms_client_account_id: str | None  # cluster key
    recommended_strategy: Literal[
        "api_first", "jsonld_first", "dom_first",
        "portal_hop", "syndication_only", "cascade",
    ]
```

---

## 8. Versioning rule

When one of these contracts changes:

1. Bump the `CONTRACTS_VERSION` constant at the top of
   `ma_poc/__init__.py`.
2. Record the change in this file under a `## Changelog` section.
3. Any phase that consumes the changed contract must be re-gated.

Don't delete fields mid-refactor — deprecate first (`# DEPRECATED: …`
comment), remove in a follow-up PR. Unit tests from earlier phases
catch silent field removals.

---

## Changelog

- **v1.0 (this doc)** — initial contracts for Jugnu. Adds
  `fetch_result` to `AdapterContext` (delta from
  `claude_refactor.md`). All other contracts are new.

---

*Next: `PHASE_J0_BASELINE.md`.*
