"""widen_units_text_columns: widen units.unit_id and units.floor_plan_name.

Revision ID: 0009_widen_units_text_columns
Revises: 0008_floor_plan_comparisons
Create Date: 2026-05-04

Background
----------
The 2026-05-01 production scheduled run (jfhc9) failed every shard with
``pg8000.exceptions.DatabaseError: 22001 value too long for type
character varying(256)`` from ``upsert_units`` in
``ma_poc/data_provider/sql/stores.py``. The offending column is
``units.floor_plan_name``: LLM-extracted plan names from the new
OpenRouter cascade now include marketing copy ("The Grand Vista at
Sunset Ridge — 1 Bed Den + Study"), which exceeds 256 chars on a
non-trivial fraction of properties.

``unit_id`` is also widened to 512 chars: a few PMS systems emit
composite IDs (``building-floor-unit-suffix``) that crowd 256.

Both columns are TEXT-equivalent in PG (varchar(N) and text have
identical storage); widening is metadata-only and runs in milliseconds
even on large tables.

Down-migration is a best-effort narrow back. If existing rows already
exceed 256 chars the down() will fail — this is acceptable because the
forward direction is the only one we ship to prod.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_widen_units_text_columns"
down_revision: str | None = "0008_floor_plan_comparisons"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "units",
        "floor_plan_name",
        existing_type=sa.String(256),
        type_=sa.String(1024),
        existing_nullable=True,
    )
    op.alter_column(
        "units",
        "unit_id",
        existing_type=sa.String(256),
        type_=sa.String(512),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "units",
        "unit_id",
        existing_type=sa.String(512),
        type_=sa.String(256),
        existing_nullable=False,
    )
    op.alter_column(
        "units",
        "floor_plan_name",
        existing_type=sa.String(1024),
        type_=sa.String(256),
        existing_nullable=True,
    )
