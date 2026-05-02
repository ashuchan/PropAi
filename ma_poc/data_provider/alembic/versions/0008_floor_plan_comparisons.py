"""floor_plan_comparisons: per-property CSV-vs-DB floor plan match results.

Revision ID: 0008_floor_plan_comparisons
Revises: 0007_grant_worker_access
Create Date: 2026-05-02

Two tables, one comparison run = one CSV invocation:

  floor_plan_comparison_runs
    A summary row per (run_id, canonical_id). Holds the per-property scores
    (csv rows total, matched_total, name_match, bed_bath_sqft_match,
    bed_bath_only_match, sqft_within_buffer, missing_in_db, extra_in_db).

  floor_plan_comparison_rows
    One row per CSV input line. Holds the apartment_id from the CSV, the
    resolved canonical_id (nullable when the apartment_id couldn't be
    matched to a property), the CSV-side fields, the matched DB unit
    fingerprint (when a match was found), the match method
    (name_semantic | bed_bath_sqft | bed_bath_only | unmatched), the
    fuzzy score (0-100, only set for name_semantic), and the sqft delta.

Both tables are upsert-only at the (run_id, ...) granularity — re-running
the util with the same run_id replaces results for that run. There is no
retention sweep; comparison runs are infrequent and small.

The frontend reads the most recent run_id by default
(`select max(run_id)`), and exposes summary + per-property drill-down.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_floor_plan_comparisons"
down_revision: str | None = "0007_grant_worker_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "floor_plan_comparison_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("canonical_id", sa.String(256), primary_key=True),
        sa.Column("apartment_id", sa.Integer, nullable=True, index=True),
        sa.Column("proj_name", sa.String(512), nullable=True),
        sa.Column("csv_rows_total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("db_floor_plans_total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("matched_total", sa.Integer, nullable=False, server_default="0"),
        # Match category counts (a single CSV row contributes to exactly one).
        sa.Column("name_match_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("bed_bath_sqft_match_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("bed_bath_only_match_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("unmatched_count", sa.Integer, nullable=False, server_default="0"),
        # Independent stat: of all rows that found ANY match, how many also
        # had sqft within the buffer? Useful for data-quality analysis.
        sa.Column("sqft_within_buffer_count", sa.Integer, nullable=False, server_default="0"),
        # DB floor plans that no CSV row matched to.
        sa.Column("missing_in_csv_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_fpc_runs_run_id",
        "floor_plan_comparison_runs",
        ["run_id"],
    )

    op.create_table(
        "floor_plan_comparison_rows",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("canonical_id", sa.String(256), nullable=True),
        # CSV-side fields, captured exactly as read.
        sa.Column("csv_apartment_id", sa.Integer, nullable=True),
        sa.Column("csv_floorplan_number", sa.String(64), nullable=True),
        sa.Column("csv_description", sa.String(512), nullable=True),
        sa.Column("csv_bed", sa.Integer, nullable=True),
        sa.Column("csv_bath", sa.Float, nullable=True),
        sa.Column("csv_area", sa.Integer, nullable=True),
        # Match outcome.
        # match_method: name_semantic | bed_bath_sqft | bed_bath_only |
        #   unmatched | property_not_found
        sa.Column("match_method", sa.String(32), nullable=False),
        sa.Column("match_score", sa.Float, nullable=True),
        sa.Column("sqft_within_buffer", sa.Boolean, nullable=True),
        sa.Column("sqft_delta", sa.Integer, nullable=True),
        # DB-side matched fingerprint (denormalised so the UI doesn't need
        # to round-trip the units table for every row).
        sa.Column("db_floor_plan_name", sa.String(256), nullable=True),
        sa.Column("db_beds", sa.Integer, nullable=True),
        sa.Column("db_baths", sa.Float, nullable=True),
        sa.Column("db_area", sa.Integer, nullable=True),
        sa.Column("db_unit_count", sa.Integer, nullable=True),
    )
    op.create_index(
        "ix_fpc_rows_run_canonical",
        "floor_plan_comparison_rows",
        ["run_id", "canonical_id"],
    )
    op.create_index(
        "ix_fpc_rows_run_method",
        "floor_plan_comparison_rows",
        ["run_id", "match_method"],
    )


def downgrade() -> None:
    op.drop_index("ix_fpc_rows_run_method", table_name="floor_plan_comparison_rows")
    op.drop_index("ix_fpc_rows_run_canonical", table_name="floor_plan_comparison_rows")
    op.drop_table("floor_plan_comparison_rows")
    op.drop_index("ix_fpc_runs_run_id", table_name="floor_plan_comparison_runs")
    op.drop_table("floor_plan_comparison_runs")
