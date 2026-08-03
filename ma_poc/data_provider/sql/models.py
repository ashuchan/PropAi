"""SQLAlchemy 2.0 models for the data-provider SQL backend (v2 schema strict).

Column names on `properties` and `units` follow the V2 output schema
defined in `scripts/schema_v2.py` — `proj_name`, `zip_code`, `beds`,
`baths`, `area`, `rent_low`, `rent_high`, etc. State-tracking columns
(`first_seen_date`, `last_seen_at`, `carryforward_days`,
`disappeared_since`, etc.) are operational and have no v2 equivalent;
they are kept so the SQL provider can replicate the FS provider's diff +
carry-forward behaviour. Anything the writer passes that is neither a
v2 data field nor a state-tracking column lands in the `extra` JSON
column.

Types are chosen for portability (String/Integer/Float/Boolean/JSON/
DateTime) so the same DDL runs on Postgres and SQLite. Dialect-specific
UPSERT is handled in `stores.py` via `dialect_insert()`.

Naming:
  - `properties`         — current-state row per canonical_id (V2 columns)
  - `units`              — current-state row per (canonical_id, unit_id) (V2 columns)
  - `property_snapshots` — per-run V2 payload (mirrors runs/{date}/properties.json)
  - `run_reports`        — one row per run_date (mirrors runs/{date}/report.json)
  - `run_issues`         — JSONL rows for runs/{date}/issues.jsonl
  - `run_ledger`         — JSONL rows for runs/{date}/ledger.jsonl
  - `scrape_events`      — append-only audit log
  - `extraction_results` — per (run_date, property_id) extraction output
  - `scrape_profiles`    — per-property learning profile (JSON payload)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base — shared across all data-provider tables."""


# ── Current-state tables ─────────────────────────────────────────────────────


class PropertyRow(Base):
    """Current state per canonical_id. Columns mirror the V2 property schema
    (`scripts/schema_v2.build_v2_property`) plus internal state-tracking."""

    __tablename__ = "properties"

    # Internal stable id (same string used as PK in property_index / unit_index).
    canonical_id: Mapped[str] = mapped_column(String(256), primary_key=True)

    # ── V2 data fields ──────────────────────────────────────────────────
    apartment_id: Mapped[int | None] = mapped_column(Integer, index=True)
    proj_name: Mapped[str | None] = mapped_column(String(512))
    address: Mapped[str | None] = mapped_column(String(512))
    city: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str | None] = mapped_column(String(64))
    zip_code: Mapped[str | None] = mapped_column(String(16))
    country: Mapped[str | None] = mapped_column(String(64), server_default="US")
    phone: Mapped[str | None] = mapped_column(String(64))
    email_address: Mapped[str | None] = mapped_column(String(256))
    website: Mapped[str | None] = mapped_column(String(1024))
    pmc: Mapped[str | None] = mapped_column(String(256))
    website_design: Mapped[str | None] = mapped_column(String(128))
    concessions: Mapped[str | None] = mapped_column(Text)

    # ── State-tracking (no v2 equivalent — used for diff + carry-forward) ─
    first_seen_date: Mapped[str | None] = mapped_column(String(16))
    # last_seen_at is the full ISO timestamp of the most recent upsert.
    # Take `left(last_seen_at, 10)` if you need a YYYY-MM-DD string.
    last_seen_at: Mapped[str | None] = mapped_column(String(64))
    last_scrape_status: Mapped[str | None] = mapped_column(String(64))
    last_units_count: Mapped[int | None] = mapped_column(Integer)

    # Catch-all for fields the writer passes that don't fit any column above.
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class UnitRow(Base):
    """Current state per (canonical_id, unit_id). Columns mirror the V2 unit
    schema (`scripts/schema_v2._format_v2_unit`) plus internal state-tracking."""

    __tablename__ = "units"

    canonical_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    unit_id: Mapped[str] = mapped_column(String(512), primary_key=True)

    # ── V2 data fields ──────────────────────────────────────────────────
    beds: Mapped[int | None] = mapped_column(Integer)
    baths: Mapped[float | None] = mapped_column(Float)
    # LLM-extracted floor plan names can be marketing copy ("The Grand Vista
    # at Sunset Ridge — 1 Bed Den"); 256 chars overflowed in production.
    floor_plan_name: Mapped[str | None] = mapped_column(String(1024))
    # Deterministic SHA-256 of (canonical_id, normalised floor_plan_name,
    # beds, baths). Two units with the same plan share the same id, so
    # GROUP BY floor_plan_id collapses unit-level rows back to plan-level
    # rows for analytics — fixes the 35% of properties where unit-level
    # variation was inflating the apparent plan count. Nullable for the
    # backfill window; populated at write time after Phase 3.
    floor_plan_id: Mapped[str | None] = mapped_column(String(16))
    # Provider/display identity before the collision-safe ``unit_id`` rewrite.
    # Keeping both makes the output auditable without weakening the composite
    # (property, unit_id) state key already used in production.
    source_unit_id: Mapped[str | None] = mapped_column(String(512))
    canonical_unit_id: Mapped[str | None] = mapped_column(String(512))
    unit_name: Mapped[str | None] = mapped_column(String(1024))
    floor: Mapped[str | None] = mapped_column(String(128))
    building: Mapped[str | None] = mapped_column(String(1024))
    building_id: Mapped[str | None] = mapped_column(String(512))
    building_id_source: Mapped[str | None] = mapped_column(String(256))
    floor_plan_name_provenance: Mapped[str | None] = mapped_column(String(128))
    area: Mapped[int | None] = mapped_column(Integer)
    # Null-native area companion; legacy ``area=-1`` remains unchanged.
    area_sqft: Mapped[int | None] = mapped_column(Integer)
    area_is_published: Mapped[bool | None] = mapped_column(Boolean)
    area_low: Mapped[int | None] = mapped_column(Integer)
    area_high: Mapped[int | None] = mapped_column(Integer)
    area_range: Mapped[str | None] = mapped_column(String(64))
    area_range_raw: Mapped[str | None] = mapped_column(Text)
    area_value_type: Mapped[str | None] = mapped_column(String(32))
    area_provenance: Mapped[str | None] = mapped_column(String(128))
    area_source_url: Mapped[str | None] = mapped_column(Text)
    rent_low: Mapped[float | None] = mapped_column(Float)
    rent_high: Mapped[float | None] = mapped_column(Float)
    rent_range: Mapped[str | None] = mapped_column(String(128))
    rent_range_raw: Mapped[str | None] = mapped_column(Text)
    rent_is_range: Mapped[bool | None] = mapped_column(Boolean)
    rent_provenance: Mapped[str | None] = mapped_column(String(64))
    date_captured: Mapped[str | None] = mapped_column(String(32))
    available_date: Mapped[str | None] = mapped_column(String(32))
    available_date_raw: Mapped[str | None] = mapped_column(Text)
    availability_date_provenance: Mapped[str | None] = mapped_column(String(64))
    availability_status: Mapped[str | None] = mapped_column(String(64))
    lease_term: Mapped[int | None] = mapped_column(Integer)
    move_in_date: Mapped[str | None] = mapped_column(String(32))
    extraction_tier: Mapped[str | None] = mapped_column(String(128))
    source_ids: Mapped[Any | None] = mapped_column(JSON)
    source_response_sha256: Mapped[str | None] = mapped_column(String(64))
    source_response_url: Mapped[str | None] = mapped_column(Text)
    source_record_locator: Mapped[str | None] = mapped_column(Text)
    source_parent_record_locator: Mapped[str | None] = mapped_column(Text)
    source_asset_url: Mapped[str | None] = mapped_column(Text)
    source_asset_sha256: Mapped[str | None] = mapped_column(String(64))
    identity_quality: Mapped[str | None] = mapped_column(String(64))
    unit_id_aliases: Mapped[Any | None] = mapped_column(JSON)
    unit_id_alias_sources: Mapped[Any | None] = mapped_column(JSON)

    # Versioned, property-scoped day-to-day join key. It is deliberately not
    # the primary key yet: the affected-386 run proves within-run uniqueness,
    # while a two-day replay is still required before changing merge identity.
    unit_history_key: Mapped[str | None] = mapped_column(String(72), index=True)
    unit_history_key_basis: Mapped[str | None] = mapped_column(Text)
    unit_history_key_quality: Mapped[str | None] = mapped_column(String(64))
    unit_history_key_version: Mapped[str | None] = mapped_column(String(16))

    # ── State-tracking (no v2 equivalent) ───────────────────────────────
    first_seen_date: Mapped[str | None] = mapped_column(String(16))
    # ISO timestamp of the most recent upsert for this (canonical_id, unit_id).
    # Re-stamped by ``upsert_units`` on every appearance, so the first 10
    # chars are the "day this unit was last seen" — usable directly as a
    # day-level filter via ``substring(last_seen_at, 1, 10) = '<run_date>'``.
    last_seen_at: Mapped[str | None] = mapped_column(String(64))
    carryforward_days: Mapped[int] = mapped_column(Integer, default=0)
    disappeared_since: Mapped[str | None] = mapped_column(String(16))
    last_absent_date: Mapped[str | None] = mapped_column(String(16))
    concessions: Mapped[Any | None] = mapped_column(JSON)
    # Per-unit amenities — list of normalised lowercase strings. Produced
    # by deterministic adapters and by LLM extraction (per-unit ``amenities``
    # array on every prompt). Nullable so a row that never extracted
    # amenities is distinguishable from one that explicitly returned [].
    amenities: Mapped[Any | None] = mapped_column(JSON)
    changed_fields: Mapped[list[str]] = mapped_column(JSON, default=list)

    # Informational SHA256 of the full incoming unit dict at upsert time.
    # Never compared between rows for identity / merge / dedup — this is
    # purely a drift-diagnostic fingerprint surfaced in the merge analysis.
    # 64 hex chars = 32 bytes; column kept nullable for backfill compat.
    data_sha256: Mapped[str | None] = mapped_column(String(64))

    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# ── Per-run tables ───────────────────────────────────────────────────────────


class RunRow(Base):
    """Registry of run_dates. Any per-run write touches this row so that
    `list_runs()` returns dates even when the properties list is empty."""

    __tablename__ = "runs"

    run_date: Mapped[str] = mapped_column(String(16), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PropertySnapshotRow(Base):
    """One row per property per run — preserves full 46-key payload + list order."""

    __tablename__ = "property_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_date: Mapped[str] = mapped_column(String(16), index=True)
    canonical_id: Mapped[str | None] = mapped_column(String(256), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)

    __table_args__ = (Index("ix_property_snapshots_run_ord", "run_date", "ordinal"),)


class RunReportRow(Base):
    """One row per run_date. Mirrors data/runs/{date}/report.json."""

    __tablename__ = "run_reports"

    run_date: Mapped[str] = mapped_column(String(16), primary_key=True)
    generated_at: Mapped[str] = mapped_column(String(64))
    totals: Mapped[dict[str, Any]] = mapped_column(JSON)
    tier_distribution: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    cost: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    slo_violations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RunIssueRow(Base):
    """Append-only per-run issues. Mirrors data/runs/{date}/issues.jsonl."""

    __tablename__ = "run_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_date: Mapped[str] = mapped_column(String(16), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    severity: Mapped[str] = mapped_column(String(16))
    code: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    canonical_id: Mapped[str | None] = mapped_column(String(256))
    row_index: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    timestamp: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (Index("ix_run_issues_run_seq", "run_date", "seq"),)


class RunLedgerRow(Base):
    """Append-only per-run cost/status ledger. Mirrors runs/{date}/ledger.jsonl."""

    __tablename__ = "run_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_date: Mapped[str] = mapped_column(String(16), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    canonical_id: Mapped[str | None] = mapped_column(String(256))
    row_index: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(32))
    units_count: Mapped[int | None] = mapped_column(Integer)
    carry_forward_used: Mapped[bool | None] = mapped_column(Boolean)
    scrape_failed: Mapped[bool | None] = mapped_column(Boolean)
    error_count: Mapped[int | None] = mapped_column(Integer)
    warning_count: Mapped[int | None] = mapped_column(Integer)
    url: Mapped[str | None] = mapped_column(String(1024))
    timestamp: Mapped[str | None] = mapped_column(String(64))
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    __table_args__ = (Index("ix_run_ledger_run_seq", "run_date", "seq"),)


# ── Audit + profiles ─────────────────────────────────────────────────────────


class ScrapeEventRow(Base):
    """Append-only audit log of every scrape attempt."""

    __tablename__ = "scrape_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    property_id: Mapped[str] = mapped_column(String(256), index=True)
    scrape_timestamp: Mapped[datetime] = mapped_column(DateTime)
    extraction_tier: Mapped[int | None] = mapped_column(Integer)
    change_detection_result: Mapped[str | None] = mapped_column(String(32))
    scrape_outcome: Mapped[str] = mapped_column(String(32))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    page_load_ms: Mapped[int | None] = mapped_column(Integer)
    proxy_used: Mapped[bool] = mapped_column(Boolean, default=False)
    proxy_provider: Mapped[str | None] = mapped_column(String(128))
    vision_fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    banner_capture_attempted: Mapped[bool] = mapped_column(Boolean, default=False)
    banner_concession_found: Mapped[bool] = mapped_column(Boolean, default=False)
    accuracy_sample_selected: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_html_path: Mapped[str | None] = mapped_column(String(1024))
    screenshot_path: Mapped[str | None] = mapped_column(String(1024))
    confidence_score: Mapped[float | None] = mapped_column(Float)


class ExtractionResultRow(Base):
    """Per (run_date, property_id) extraction output + tier + confidence."""

    __tablename__ = "extraction_results"

    run_date: Mapped[str] = mapped_column(String(16), primary_key=True)
    property_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    tier: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    raw_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    field_confidences: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    low_confidence_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    error_message: Mapped[str | None] = mapped_column(Text)


class ScrapeProfileRow(Base):
    """Per-property self-learning profile. Full body stored as JSON payload."""

    __tablename__ = "scrape_profiles"

    canonical_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    updated_by: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


# ── Run artifacts (migrated from runs/{date}/property_reports/, llm_report*, …) ─


class PropertyReportRow(Base):
    """Per-property markdown report (runs/{date}/property_reports/{cid}.md)."""

    __tablename__ = "property_reports"

    run_date: Mapped[str] = mapped_column(String(16), primary_key=True)
    canonical_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    markdown: Mapped[str] = mapped_column(Text)
    written_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_property_reports_run", "run_date"),)


class LlmReportRow(Base):
    """Aggregated LLM report for a run (runs/{date}/llm_report.json)."""

    __tablename__ = "llm_reports"

    run_date: Mapped[str] = mapped_column(String(16), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    written_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class LlmPropertyDetailRow(Base):
    """Per-property LLM detail (runs/{date}/llm_report/{property_id}.json)."""

    __tablename__ = "llm_property_details"

    run_date: Mapped[str] = mapped_column(String(16), primary_key=True)
    property_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    written_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_llm_property_details_run", "run_date"),)


class LlmDiagnosticRow(Base):
    """Per-property diagnostic blob. `kind` distinguishes multiple artifact
    types per property per run (e.g. 'field_recovery', 'tier_trace')."""

    __tablename__ = "llm_diagnostics"

    run_date: Mapped[str] = mapped_column(String(16), primary_key=True)
    property_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    written_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_llm_diagnostics_run_prop", "run_date", "property_id"),)


# ── Dead-letter queue (global state, not per-run) ────────────────────────────


class DlqEntryRow(Base):
    """Live state of the dead-letter queue.

    Mirrors the post-compaction view of ``data/state/dlq.jsonl``. The
    JSONL file is an append-only log with an ``unparked`` tombstone
    convention; this table only holds currently-parked entries (one row
    per property_id). Unparking a property DELETEs the row rather than
    writing a tombstone — DB semantics already give us "not present" as
    the absence signal.

    ``retry_at`` is indexed because the retry runner's hot query is
    ``WHERE retry_at <= now()`` and even a few hundred parked properties
    make that a seq scan without it.
    """

    __tablename__ = "dlq_entries"

    property_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    parked_at: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(128))
    last_error_signature: Mapped[str] = mapped_column(String(512))
    retry_at: Mapped[str] = mapped_column(String(64))
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_dlq_entries_retry_at", "retry_at"),)


# ── Floor plan comparison (CSV ↔ DB) ────────────────────────────────────────


class FloorPlanComparisonRunRow(Base):
    """One summary row per (run_id, canonical_id). Re-running the util with
    the same run_id replaces the row via upsert."""

    __tablename__ = "floor_plan_comparison_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    canonical_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    apartment_id: Mapped[int | None] = mapped_column(Integer, index=True)
    proj_name: Mapped[str | None] = mapped_column(String(512))
    csv_rows_total: Mapped[int] = mapped_column(Integer, default=0)
    db_floor_plans_total: Mapped[int] = mapped_column(Integer, default=0)
    matched_total: Mapped[int] = mapped_column(Integer, default=0)
    name_match_count: Mapped[int] = mapped_column(Integer, default=0)
    bed_bath_sqft_match_count: Mapped[int] = mapped_column(Integer, default=0)
    bed_bath_only_match_count: Mapped[int] = mapped_column(Integer, default=0)
    # Rescue tier: fuzzy name match where DB lost beds-or-baths during
    # extraction. Counted as "matched" for the headline rate but kept
    # separate so analysts can downgrade on confidence.
    name_only_match_count: Mapped[int] = mapped_column(Integer, default=0)
    unmatched_count: Mapped[int] = mapped_column(Integer, default=0)
    # Property resolved but ``units`` is empty — scrape failure, not a
    # match defect. Excluded from the unmatched headline.
    db_empty_count: Mapped[int] = mapped_column(Integer, default=0)
    sqft_within_buffer_count: Mapped[int] = mapped_column(Integer, default=0)
    missing_in_csv_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_fpc_runs_run_id", "run_id"),)


class FloorPlanComparisonRowRow(Base):
    """One row per CSV input line — preserves the per-floor-plan match decision."""

    __tablename__ = "floor_plan_comparison_rows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64))
    canonical_id: Mapped[str | None] = mapped_column(String(256))
    csv_apartment_id: Mapped[int | None] = mapped_column(Integer)
    csv_floorplan_number: Mapped[str | None] = mapped_column(String(64))
    csv_description: Mapped[str | None] = mapped_column(String(512))
    csv_bed: Mapped[int | None] = mapped_column(Integer)
    csv_bath: Mapped[float | None] = mapped_column(Float)
    csv_area: Mapped[int | None] = mapped_column(Integer)
    match_method: Mapped[str] = mapped_column(String(32))
    match_score: Mapped[float | None] = mapped_column(Float)
    sqft_within_buffer: Mapped[bool | None] = mapped_column(Boolean)
    sqft_delta: Mapped[int | None] = mapped_column(Integer)
    db_floor_plan_name: Mapped[str | None] = mapped_column(String(256))
    db_beds: Mapped[int | None] = mapped_column(Integer)
    db_baths: Mapped[float | None] = mapped_column(Float)
    db_area: Mapped[int | None] = mapped_column(Integer)
    db_unit_count: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_fpc_rows_run_canonical", "run_id", "canonical_id"),
        Index("ix_fpc_rows_run_method", "run_id", "match_method"),
    )


class FloorPlanDisagreementReviewRow(Base):
    """Manual-review queue for genuine CSV-vs-DB floor-plan disagreements.

    Populated from ``floor_plan_comparison_rows`` by
    ``scripts/floor_plans/export_disagreements.py``. Surfaces rows where the
    fuzzy name matches but bed/bath conflict (the "real" disagreements
    after the matcher has done its job — see Phase 5 of the floor-plan gap
    plan).

    The reviewer updates ``status`` to one of ``pending`` (default),
    ``csv_correct`` (DB extraction is wrong; fix upstream), ``db_correct``
    (CSV is stale; flag with the data vendor), ``ambiguous`` (both
    plausible — record but take no action). Re-running the exporter is
    idempotent on (run_id, canonical_id, csv_floorplan_number): only
    pending rows are wiped and re-added; reviewer-marked rows are
    preserved across exports.
    """

    __tablename__ = "floor_plan_disagreement_review"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64))
    canonical_id: Mapped[str | None] = mapped_column(String(256))
    csv_apartment_id: Mapped[int | None] = mapped_column(Integer)
    csv_floorplan_number: Mapped[str | None] = mapped_column(String(64))
    csv_description: Mapped[str | None] = mapped_column(String(512))
    csv_bed: Mapped[int | None] = mapped_column(Integer)
    csv_bath: Mapped[float | None] = mapped_column(Float)
    csv_area: Mapped[int | None] = mapped_column(Integer)
    db_floor_plan_name: Mapped[str | None] = mapped_column(String(256))
    db_beds: Mapped[int | None] = mapped_column(Integer)
    db_baths: Mapped[float | None] = mapped_column(Float)
    db_area: Mapped[int | None] = mapped_column(Integer)
    name_match_score: Mapped[float | None] = mapped_column(Float)
    # Reviewer state.
    status: Mapped[str] = mapped_column(String(32), default="pending")
    reviewer_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_fpdr_run_status", "run_id", "status"),
        Index("ix_fpdr_canonical", "canonical_id"),
    )
