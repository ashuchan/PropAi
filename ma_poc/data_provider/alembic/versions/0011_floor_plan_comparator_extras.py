"""floor_plan_comparator_extras: name_only/db_empty buckets + review queue.

Revision ID: 0011_floor_plan_comparator_extras
Revises: 0010_units_data_sha256
Create Date: 2026-05-07

Two unrelated-but-adjacent schema additions for the floor-plan comparator:

  1. Two new int columns on ``floor_plan_comparison_runs``:
       - ``name_only_match_count``  — fuzzy-name match where DB lost
         beds/baths during extraction (rescue tier d).
       - ``db_empty_count``         — apartmentid resolved but
         ``units`` is empty (scrape failure, not a match defect).
     Both default to 0; existing rows backfill cleanly. The per-row
     ``floor_plan_comparison_rows.match_method`` column is already
     ``String(32)`` and accepts the new ``name_only`` / ``db_empty``
     enum values without DDL change.

  2. New table ``floor_plan_disagreement_review`` — manual-review queue
     for genuine CSV/DB disagreements (Phase 5 of the floor-plan gap
     plan). Populated by ``scripts/floor_plans/export_disagreements.py``
     from the per-row comparator output. Reviewer notes survive
     re-exports because only ``status='pending'`` rows are wiped.

Both changes are additive: existing comparison runs and per-row
records continue to read/write correctly through the gap.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_fpc_extras"
down_revision: str | None = "0010_units_data_sha256"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Part 1: extra columns on the runs summary table ──────────────────
    op.add_column(
        "floor_plan_comparison_runs",
        sa.Column(
            "name_only_match_count",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "floor_plan_comparison_runs",
        sa.Column(
            "db_empty_count",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )

    # ── Part 2: manual-review queue table ────────────────────────────────
    op.create_table(
        "floor_plan_disagreement_review",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("canonical_id", sa.String(256), nullable=True),
        # CSV side, copied verbatim from floor_plan_comparison_rows.
        sa.Column("csv_apartment_id", sa.Integer, nullable=True),
        sa.Column("csv_floorplan_number", sa.String(64), nullable=True),
        sa.Column("csv_description", sa.String(512), nullable=True),
        sa.Column("csv_bed", sa.Integer, nullable=True),
        sa.Column("csv_bath", sa.Float, nullable=True),
        sa.Column("csv_area", sa.Integer, nullable=True),
        # DB side — the plan that fuzzy-matched on name.
        sa.Column("db_floor_plan_name", sa.String(256), nullable=True),
        sa.Column("db_beds", sa.Integer, nullable=True),
        sa.Column("db_baths", sa.Float, nullable=True),
        sa.Column("db_area", sa.Integer, nullable=True),
        sa.Column("name_match_score", sa.Float, nullable=True),
        # Reviewer state.
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("reviewer_note", sa.Text, nullable=True),
        sa.Column("reviewed_by", sa.String(128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_fpdr_run_status",
        "floor_plan_disagreement_review",
        ["run_id", "status"],
    )
    op.create_index(
        "ix_fpdr_canonical",
        "floor_plan_disagreement_review",
        ["canonical_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fpdr_canonical", table_name="floor_plan_disagreement_review"
    )
    op.drop_index(
        "ix_fpdr_run_status", table_name="floor_plan_disagreement_review"
    )
    op.drop_table("floor_plan_disagreement_review")
    op.drop_column("floor_plan_comparison_runs", "db_empty_count")
    op.drop_column("floor_plan_comparison_runs", "name_only_match_count")
