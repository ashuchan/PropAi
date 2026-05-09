"""units.floor_plan_id: deterministic plan grouping key.

Revision ID: 0012_units_fpid
Revises: 0011_fpc_extras
Create Date: 2026-05-07

Adds a nullable ``floor_plan_id`` column to ``units``. Computed
upstream as a 12-char prefix of SHA-256 over
``canonical_id|lower(strip(floor_plan_name))|beds|baths``. Two units
that share a plan name + bed/bath share the id, regardless of per-unit
sqft drift, so analytics that previously over-counted distinct plans
(35% of properties had ≥95% unique-plan-per-unit ratio) can collapse on
this key.

Nullable on creation so the column is safe to land before the v2
transform writes them — the backfill script (``scripts/
backfill_floor_plan_id.py``) populates existing rows. New writes after
Phase 3 stamp the column at upsert time. An index on the column
supports the planned ``floor_plans`` view.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_units_fpid"
down_revision: str | None = "0011_fpc_extras"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "units",
        sa.Column("floor_plan_id", sa.String(16), nullable=True),
    )
    op.create_index(
        "ix_units_floor_plan_id",
        "units",
        ["canonical_id", "floor_plan_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_units_floor_plan_id", table_name="units")
    op.drop_column("units", "floor_plan_id")
