"""DTOs for the data-provider layer.

Where existing Pydantic models cover a concept we re-export them (`UnitRecord`,
`ScrapeEvent`, `ExtractionResult`, `ScrapeProfile`). Where the codebase uses
raw dicts today (property-index entries, unit-index entries, run reports,
issues, ledger rows) we define explicit models so both FS and Postgres
providers can share a contract.

Unknown-key tolerance (`extra="allow"` where appropriate) preserves forward
compatibility with fields the scraper already writes but that aren't yet
formalised.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ── Re-exports so consumers import everything from `ma_poc.data_provider` ────
from models.extraction_result import (
    ExtractionResult,
    ExtractionStatus,
    ExtractionTier,
)
from models.scrape_event import (
    ChangeDetectionResult,
    ScrapeEvent,
    ScrapeOutcome,
)
from models.scrape_profile import ScrapeProfile
from models.unit_record import (
    AvailabilityStatus,
    DataQualityFlag,
    UnitRecord,
)

__all__ = [
    "ExtractionResult",
    "ExtractionStatus",
    "ExtractionTier",
    "ScrapeEvent",
    "ScrapeOutcome",
    "ChangeDetectionResult",
    "ScrapeProfile",
    "UnitRecord",
    "AvailabilityStatus",
    "DataQualityFlag",
    "PropertyIndexEntry",
    "UnitIndexEntry",
    "UnitDiff",
    "PropertyRecord",
    "RunSummary",
    "RunReport",
    "IssueEntry",
    "LedgerEntry",
]


class PropertyIndexEntry(BaseModel):
    """Canonical property-state DTO. Field names follow the V2 schema
    (`scripts/schema_v2.build_v2_property`); state-tracking fields
    (`first_seen_date`, etc.) have no V2 equivalent and are operational."""

    model_config = ConfigDict(extra="allow")

    canonical_id: str

    # V2 data fields
    apartment_id: int | None = None
    proj_name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    country: str | None = None
    phone: str | None = None
    email_address: str | None = None
    website: str | None = None
    pmc: str | None = None
    website_design: str | None = None
    concessions: str | None = None

    # State-tracking (derive a YYYY-MM-DD by slicing last_seen_at[:10] if needed)
    first_seen_date: str | None = None
    last_seen_at: str | None = None
    last_scrape_status: str | None = None
    last_units_count: int | None = None


class UnitIndexEntry(BaseModel):
    """Canonical unit-state DTO. Field names follow the V2 schema
    (`scripts/schema_v2._format_v2_unit`); state-tracking fields
    (`first_seen_date`, `carryforward_days`, etc.) are operational."""

    model_config = ConfigDict(extra="allow")

    unit_id: str

    # V2 data fields
    beds: int | None = None
    baths: float | None = None
    floor_plan_name: str | None = None
    area: int | None = None
    rent_low: float | int | None = None
    rent_high: float | int | None = None
    date_captured: str | None = None
    available_date: str | None = None
    lease_term: int | None = None
    move_in_date: str | None = None
    concessions: Any = None

    # State-tracking
    first_seen_date: str | None = None
    last_seen_at: str | None = None
    carryforward_days: int = 0
    disappeared_since: str | None = None
    last_absent_date: str | None = None
    changed_fields: list[str] = Field(default_factory=list)


class UnitDiff(BaseModel):
    """Output of IUnitStateStore.upsert_units — matches StateStore.upsert_units.

    The four list fields are the canonical merge buckets. The counter
    fields drive the post-merge yield gate (:func:`services.merge_yield.evaluate`)
    and are informational only — no business rule reads them directly.

    No-drop contract (2026-05): ``upsert_units`` never silently drops a
    record. When even the floor-plan-only fallback can't produce an id,
    ``synthesize_unkeyable_id`` mints one from the payload hash and the
    record is inserted. ``synthetic_key_used`` counts those rescues;
    ``skipped_no_identity`` is retained for back-compat and now stays
    at 0 in normal operation.
    """

    new: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    disappeared: list[str] = Field(default_factory=list)
    # Records inserted under a payload-hash synthetic id because no
    # natural / fingerprint / floor-plan anchor was available. They land
    # in ``new`` (or ``updated`` if the same payload was seen before)
    # but the counter exposes the rescue rate to the yield gate.
    synthetic_key_used: int = 0
    # Retained for back-compat. With the no-drop contract this is always
    # 0 in healthy operation; non-zero indicates a bug in the synthesis
    # path itself, not an extractor problem.
    skipped_no_identity: int = 0
    input_count: int = 0


class PropertyRecord(BaseModel):
    """One property row inside data/runs/{date}/properties.json.

    The 46-key schema is still implicit in the scraper — this DTO passes
    arbitrary keys through (`extra="allow"`) rather than formalising the
    column list. Phase 1 of the PG migration should tighten this.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # The scraper writes human-readable keys ("Property Name", "City", …) so
    # we don't declare required fields. This DTO is a typed wrapper around
    # the existing dict shape for read/write symmetry.


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    properties: int = 0
    succeeded: int = 0
    failed: int = 0
    carry_forward: int = 0
    success_rate_pct: float = 0.0


class RunReport(BaseModel):
    """data/runs/{date}/report.json.

    Tolerant of both the new reporting.run_report.build() shape (which emits
    `generated_at`) and the Jugnu runner shape (which emits
    `started_at`/`finished_at`). `generated_at` falls back to `finished_at`
    (or an empty string) during load so both payloads round-trip.
    """

    model_config = ConfigDict(extra="allow")

    run_date: str
    generated_at: str = ""
    totals: RunSummary = Field(default_factory=lambda: RunSummary())
    tier_distribution: dict[str, int] = Field(default_factory=dict)
    cost: dict[str, float] = Field(default_factory=dict)
    slo_violations: list[dict[str, Any]] = Field(default_factory=list)


class IssueEntry(BaseModel):
    """One line in data/runs/{date}/issues.jsonl."""

    model_config = ConfigDict(extra="allow")

    severity: str  # ERROR | WARNING | INFO
    code: str
    message: str
    canonical_id: str | None = None
    row_index: int | None = None
    details: dict[str, Any] | None = None
    timestamp: datetime | str | None = None


class LedgerEntry(BaseModel):
    """One line in data/runs/{date}/ledger.jsonl."""

    model_config = ConfigDict(extra="allow")

    canonical_id: str | None = None
    row_index: int | None = None
    status: str | None = None
    units_count: int | None = None
    carry_forward_used: bool | None = None
    scrape_failed: bool | None = None
    error_count: int | None = None
    warning_count: int | None = None
    url: str | None = None
    timestamp: datetime | str | None = None
