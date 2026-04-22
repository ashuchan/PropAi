"""drop redundant last_seen_date column from properties + units.

Revision ID: 0005_drop_lsdate
Revises: 0004_country_us
Create Date: 2026-04-21

`last_seen_date` (YYYY-MM-DD) was always the date-part of `last_seen_at`
(full ISO timestamp) — both get written to the same `run_date` on every
upsert. Keeping both just produces two columns that can drift and
confuse readers. Dropping `last_seen_date`; callers that need a day-
level value should take `left(last_seen_at::text, 10)` or
`last_seen_at::date`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_drop_lsdate"
down_revision: str | None = "0004_country_us"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("properties", "last_seen_date")
    op.drop_column("units", "last_seen_date")


def downgrade() -> None:
    op.add_column("properties", sa.Column("last_seen_date", sa.String(16)))
    op.add_column("units", sa.Column("last_seen_date", sa.String(16)))
    # Best-effort backfill from last_seen_at.
    op.execute("UPDATE properties SET last_seen_date = LEFT(last_seen_at, 10) WHERE last_seen_at IS NOT NULL")
    op.execute("UPDATE units SET last_seen_date = LEFT(last_seen_at, 10) WHERE last_seen_at IS NOT NULL")
