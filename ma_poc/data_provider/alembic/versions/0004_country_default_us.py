"""properties.country defaults to 'US' and backfill existing NULLs.

Revision ID: 0004_country_us
Revises: 0003_artifacts
Create Date: 2026-04-21

The V2 schema has `country` as nullable because the scraper rarely
captures it from the website. In practice every property we track is
US-based, so defaulting to 'US' gives the DB tidier output and removes
a class of NULL values that isn't meaningful for our dataset.

This migration:
  - sets `properties.country` default to 'US' so future inserts fill in
  - backfills existing NULLs to 'US'
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_country_us"
down_revision: str | None = "0003_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "properties",
        "country",
        existing_type=sa.String(64),
        server_default=sa.text("'US'"),
        existing_nullable=True,
    )
    op.execute("UPDATE properties SET country = 'US' WHERE country IS NULL")


def downgrade() -> None:
    op.alter_column(
        "properties",
        "country",
        existing_type=sa.String(64),
        server_default=None,
        existing_nullable=True,
    )
    # Not un-setting values — backfill is idempotent but not reversible.
