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
    "PropertyToScrape",
    "CatalogFilters",
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
    floor_plan_id: str | None = None
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


class CatalogFilters(BaseModel):
    """Narrow filter set passed to IPropertyCatalogSource.list_active().

    Empty values mean "no filter". Filters AND together — passing both
    `pms_platforms` and `canonical_ids` returns rows matching both.
    """

    model_config = ConfigDict(extra="forbid")

    canonical_ids: list[str] | None = None
    pms_platforms: list[str] | None = None
    property_types: list[str] | None = None  # STABILISED | LEASE_UP
    shard_index: int | None = None  # zero-based; requires shard_count
    shard_count: int | None = None  # total number of shards
    start_index: int | None = None  # zero-based row offset (post-filter)


class PropertyToScrape(BaseModel):
    """One row in the input catalog — a property the runner should scrape.

    Carries V2-canonical fields (`proj_name`, `website`, `zip_code`, …) plus
    a `raw` passthrough dict so consumers that do dict.get() fallbacks
    against many column-name variants ("Property Name", "name", "proj_name")
    keep working without per-call refactors.

    Use `as_csv_row()` to get a flat dict shaped like the legacy CSV row.
    """

    model_config = ConfigDict(extra="allow")

    canonical_id: str
    url: str

    # V2-canonical enrichment (mirrors `properties` table columns).
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
    apartment_id: int | None = None
    property_type: str | None = None  # STABILISED | LEASE_UP | other

    # Anything else the source surfaced — CSV columns we don't model
    # explicitly, JSON `extra` from the SQL row, etc. Passed through to
    # consumers that look up by string key.
    raw: dict[str, Any] = Field(default_factory=dict)

    def as_csv_row(self) -> dict[str, Any]:
        """Flatten to the dict shape `_format_v1` / `_format_v2` consume.

        Populates *both* the V2 keys (`proj_name`, `website`, `zip_code`)
        and the Title-Case CSV aliases (`Property Name`, `Website`,
        `ZIP Code`) so existing fallback lookups keep hitting. `raw` is
        layered first so explicit fields win on collision.
        """
        d: dict[str, Any] = dict(self.raw)
        # Always-present normalised keys.
        d["property_id"] = self.canonical_id
        d["url"] = self.url
        # Aliases keyed off each V2 field. setdefault so an existing
        # value in `raw` (e.g. the original CSV cell) takes precedence.
        if self.proj_name:
            d.setdefault("name", self.proj_name)
            d.setdefault("Name", self.proj_name)
            d.setdefault("Property Name", self.proj_name)
            d.setdefault("proj_name", self.proj_name)
        if self.address:
            d.setdefault("address", self.address)
            d.setdefault("Address", self.address)
            d.setdefault("Property Address", self.address)
        if self.city:
            d.setdefault("city", self.city)
            d.setdefault("City", self.city)
        if self.state:
            d.setdefault("state", self.state)
            d.setdefault("State", self.state)
        if self.zip_code:
            d.setdefault("zip", self.zip_code)
            d.setdefault("Zip", self.zip_code)
            d.setdefault("ZIP Code", self.zip_code)
            d.setdefault("zip_code", self.zip_code)
        if self.phone:
            d.setdefault("phone", self.phone)
            d.setdefault("Phone", self.phone)
        if self.website:
            d.setdefault("website", self.website)
            d.setdefault("Website", self.website)
        if self.pmc:
            d.setdefault("pmc", self.pmc)
            d.setdefault("Management Company", self.pmc)
        if self.apartment_id is not None:
            d.setdefault("apartmentid", self.apartment_id)
            d.setdefault("apartment_id", self.apartment_id)
            d.setdefault("Unique ID", self.apartment_id)
        if self.property_type:
            d.setdefault("type", self.property_type)
            d.setdefault("Type", self.property_type)
            d.setdefault("Property Type", self.property_type)
        return d


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
