"""artifacts in DB: property_reports, llm_reports, llm_property_details, llm_diagnostics.

Revision ID: 0003_artifacts
Revises: 0002_v2_strict
Create Date: 2026-04-21

Adds four tables so the frontend API can serve all run artifacts from Postgres
without falling back to the filesystem:

  property_reports        — per-property markdown report
                            (runs/{date}/property_reports/{cid}.md)
  llm_reports             — one aggregate LLM report per run
                            (runs/{date}/llm_report.json)
  llm_property_details    — per-property LLM detail
                            (runs/{date}/llm_report/{cid}.json)
  llm_diagnostics         — per-property diagnostic blobs (multiple kinds)
                            (runs/{date}/llm_diagnostics/{cid}_{kind}.json)

Profiles already live in `scrape_profiles` from `0001_initial`.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_artifacts"
down_revision: Union[str, None] = "0002_v2_strict"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "property_reports",
        sa.Column("run_date", sa.String(16), primary_key=True),
        sa.Column("canonical_id", sa.String(256), primary_key=True),
        sa.Column("markdown", sa.Text, nullable=False),
        sa.Column("written_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_property_reports_run", "property_reports", ["run_date"],
    )

    op.create_table(
        "llm_reports",
        sa.Column("run_date", sa.String(16), primary_key=True),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("written_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "llm_property_details",
        sa.Column("run_date", sa.String(16), primary_key=True),
        sa.Column("property_id", sa.String(256), primary_key=True),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("written_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_llm_property_details_run", "llm_property_details", ["run_date"],
    )

    op.create_table(
        "llm_diagnostics",
        sa.Column("run_date", sa.String(16), primary_key=True),
        sa.Column("property_id", sa.String(256), primary_key=True),
        sa.Column("kind", sa.String(64), primary_key=True),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("written_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_llm_diagnostics_run_prop", "llm_diagnostics",
        ["run_date", "property_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_diagnostics_run_prop", table_name="llm_diagnostics")
    op.drop_table("llm_diagnostics")
    op.drop_index("ix_llm_property_details_run", table_name="llm_property_details")
    op.drop_table("llm_property_details")
    op.drop_table("llm_reports")
    op.drop_index("ix_property_reports_run", table_name="property_reports")
    op.drop_table("property_reports")
