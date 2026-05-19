"""units.available_date_raw: producer-literal availability string.

Revision ID: 0014_units_avail_date_raw
Revises: 0013_units_amenities
Create Date: 2026-05-19

Adds a nullable ``available_date_raw`` column to ``units`` that
preserves the producer's literal availability string verbatim, alongside
the typed ISO ``available_date`` column.

Background — placeholder leak diagnosed 2026-05-19
--------------------------------------------------
The 2026-05-18 cloud run shipped ``available_date=null`` on 94.1 % of
the 80,663 units in the run, despite producers emitting parseable
strings on 21,477 of those rows. The L4 schema gate had a strict
``datetime.fromisoformat`` check that nulled anything non-ISO and
stashed the raw value into ``record["_date_placeholder"]``. The v2
formatter downstream read ``available_date`` (now ``None``) and so the
typed column landed empty. ``_date_placeholder`` was never surfaced to
the units table — 0 of 80,663 rows carried it through.

This migration adds the storage column. The companion code change
(:func:`ma_poc.scripts.runners.jugnu._format_v2_unit`,
:func:`ma_poc.core.schema_v2._format_v2_unit`) emits the raw string
from the gate-stashed placeholder, then the upsert path
(:meth:`data_provider.sql.stores._UnitStateStore.upsert_units`) writes
it into this column.

Field semantics
---------------
- ``available_date``      — typed YYYY-MM-DD or NULL. Driven by the
                            lenient :func:`format_loose_date` parser
                            against whichever raw signal the producer
                            emitted. Use this for typed range queries
                            and date arithmetic.
- ``available_date_raw``  — verbatim producer string (whitespace-
                            collapsed), e.g. ``"Available 7/24"``,
                            ``"Late August"``, ``"Available Now"``.
                            Use this when the typed column is null and
                            you still need to know what the website
                            actually displayed.

Width: 64 chars. Captures every observed shape in the 2026-05-18 run
(longest was a 41-char free-form fragment). Clipped at the write
boundary in ``_clip_to_column_limits`` so over-long DOM-scan junk
doesn't reject the row.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_units_avail_date_raw"
down_revision: str | None = "0013_units_amenities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "units",
        sa.Column("available_date_raw", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("units", "available_date_raw")
