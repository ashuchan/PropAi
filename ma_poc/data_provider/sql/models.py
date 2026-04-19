"""SQLAlchemy 2.0 models for the data-provider SQL backend.

One `Base` subclass per logical store — 9 tables total. Types are chosen for
portability (String/Integer/Float/Boolean/JSON/DateTime) so the same DDL
works on Postgres and SQLite. Dialect-specific UPSERT is handled in
`stores.py` via `_upsert()` helper.

Naming:
  - `properties`         — current-state row per canonical_id (mirrors property_index.json)
  - `units`              — current-state row per (canonical_id, unit_id) (mirrors unit_index.json)
  - `property_snapshots` — per-run 46-key payload (mirrors runs/{date}/properties.json)
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
    """Current state per canonical_id. Mirrors data/state/property_index.json."""

    __tablename__ = "properties"

    canonical_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(512))
    website: Mapped[str | None] = mapped_column(String(1024))
    address: Mapped[str | None] = mapped_column(String(512))
    city: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str | None] = mapped_column(String(64))
    zip: Mapped[str | None] = mapped_column(String(32))
    first_seen_date: Mapped[str | None] = mapped_column(String(16))
    last_seen_date: Mapped[str | None] = mapped_column(String(16))
    last_seen_at: Mapped[str | None] = mapped_column(String(64))
    last_scrape_status: Mapped[str | None] = mapped_column(String(64))
    last_units_count: Mapped[int | None] = mapped_column(Integer)
    # Catch-all for fields the scraper writes but the schema hasn't formalised.
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class UnitRow(Base):
    """Current state per (canonical_id, unit_id). Mirrors data/state/unit_index.json."""

    __tablename__ = "units"

    canonical_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    unit_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    unit_number: Mapped[str | None] = mapped_column(String(128))
    market_rent_low: Mapped[float | None] = mapped_column(Float)
    market_rent_high: Mapped[float | None] = mapped_column(Float)
    available_date: Mapped[str | None] = mapped_column(String(32))
    bedrooms: Mapped[float | None] = mapped_column(Float)
    bathrooms: Mapped[float | None] = mapped_column(Float)
    sqft: Mapped[int | None] = mapped_column(Integer)
    floor_plan_name: Mapped[str | None] = mapped_column(String(256))
    availability_status: Mapped[str | None] = mapped_column(String(32))
    first_seen_date: Mapped[str | None] = mapped_column(String(16))
    last_seen_date: Mapped[str | None] = mapped_column(String(16))
    last_seen_at: Mapped[str | None] = mapped_column(String(64))
    carryforward_days: Mapped[int] = mapped_column(Integer, default=0)
    disappeared_since: Mapped[str | None] = mapped_column(String(16))
    last_absent_date: Mapped[str | None] = mapped_column(String(16))
    concessions: Mapped[Any | None] = mapped_column(JSON)
    changed_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
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

    __table_args__ = (
        Index("ix_property_snapshots_run_ord", "run_date", "ordinal"),
    )


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

    __table_args__ = (
        Index("ix_run_issues_run_seq", "run_date", "seq"),
    )


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

    __table_args__ = (
        Index("ix_run_ledger_run_seq", "run_date", "seq"),
    )


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
