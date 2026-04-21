"""000_initial_schema

Creates the four tables the Jugnu pipeline writes to:
  - properties   : one row per canonical_id; JSONB profile
  - units        : one row per (canonical_id, unit_id, run_date); 46-key record
  - run_ledger   : one row per run_date; tracks completion for retry logic
  - events       : append-only L5 observability log

Revision ID: 000_initial_schema
Revises:
Create Date: 2026-04-21 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "000_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── properties ──────────────────────────────────────────────────────────
    # One row per canonical_id; profile is the JSONB scrape profile blob.
    op.create_table(
        "properties",
        sa.Column("canonical_id", sa.Text, primary_key=True),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("pms", sa.Text),
        sa.Column("profile", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("proxy_tier", sa.Text, server_default="DATACENTER"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_properties_pms", "properties", ["pms"])

    # ── units ────────────────────────────────────────────────────────────────
    # One row per (canonical_id, unit_id, run_date); `record` is the 46-key JSON payload.
    op.create_table(
        "units",
        sa.Column("canonical_id", sa.Text, nullable=False),
        sa.Column("unit_id", sa.Text, nullable=False),
        sa.Column("run_date", sa.Date, nullable=False),
        sa.Column("record", postgresql.JSONB, nullable=False),
        sa.Column("written_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("canonical_id", "unit_id", "run_date"),
        sa.ForeignKeyConstraint(["canonical_id"], ["properties.canonical_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_units_run_date", "units", ["run_date"])
    op.create_index("ix_units_canonical_run", "units", ["canonical_id", "run_date"])

    # ── run_ledger ────────────────────────────────────────────────────────────
    # One row per run_date; used by trigger_retry.py to find the most recent run.
    op.create_table(
        "run_ledger",
        sa.Column("run_date", sa.Date, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),  # STARTED/COMPLETED/PARTIAL/FAILED
        sa.Column("shard_count", sa.Integer, nullable=False),
        sa.Column("shards_completed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("properties_total", sa.Integer),
        sa.Column("properties_succeeded", sa.Integer),
        sa.Column("properties_failed", sa.Integer),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True)),
    )

    # ── events ────────────────────────────────────────────────────────────────
    # Append-only L5 observability log; severity index covers WARN/ERROR queries.
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_date", sa.Date, nullable=False),
        sa.Column("shard_idx", sa.Integer),
        sa.Column("canonical_id", sa.Text),
        sa.Column("severity", sa.Text, nullable=False),  # DEBUG/INFO/WARN/ERROR
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("ts", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_events_run_date", "events", ["run_date"])
    op.create_index(
        "ix_events_severity_warn_error",
        "events",
        ["severity"],
        postgresql_where=sa.text("severity IN ('WARN', 'ERROR')"),
    )


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("run_ledger")
    op.drop_table("units")
    op.drop_table("properties")
