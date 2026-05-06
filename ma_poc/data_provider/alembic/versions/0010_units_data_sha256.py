"""units_data_sha256: add nullable data_sha256 column to units.

Revision ID: 0010_units_data_sha256
Revises: 0009_widen_units_text_columns
Create Date: 2026-05-06

Background
----------
The merge-failure analysis (see ``services/merge_analysis.py``) showed
silent drift between extraction tiers — the same physical unit re-emitted
with subtly different field values across runs, which fragments
``inferred_*`` fallback ids and inflates the disappeared-units count.

To make that drift directly observable without changing any business
rule, every ``upsert_units`` call now computes and stores a SHA256 of
the full incoming unit dict in this new column. The hash is **never**
compared between rows for identity / merge / dedup — it is purely a
fingerprint surfaced in diagnostics and the daily merge analysis email.

Why a column and not the JSON ``extra`` blob:
- queryable from SQL without ``->>`` extraction (cheap drift counts).
- fixed-length, indexable later if drift counts become a hot query.

Nullable so existing rows survive the migration without a backfill;
older rows simply read ``data_sha256 IS NULL`` and are excluded from
drift counts. New writes will populate the column unconditionally.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_units_data_sha256"
down_revision: str | None = "0009_widen_units_text_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "units",
        sa.Column("data_sha256", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("units", "data_sha256")
