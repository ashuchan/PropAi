"""units.amenities: per-unit amenities captured from extraction.

Revision ID: 0013_units_amenities
Revises: 0012_units_fpid
Create Date: 2026-05-10

Adds a nullable JSON ``amenities`` column to ``units``. Per-unit amenity
arrays are produced by deterministic adapters and by LLM extraction (the
prompts already ask for amenity lists per record). Until now they
landed in the ``extra`` JSON catch-all, which is invisible to indexing,
the TS data-provider read path, and aggregate analytics. Promoting the
field to a first-class JSON column closes that gap.

The column lives only on ``units`` — property-level amenities are
aggregated from the union of unit amenities in the reporting layer
(``aggregate_property_amenities``) and have no dedicated column on
``properties`` for now.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_units_amenities"
down_revision: str | None = "0012_units_fpid"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "units",
        sa.Column("amenities", sa.JSON, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("units", "amenities")
