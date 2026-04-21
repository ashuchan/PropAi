"""000 — initial schema (10 tables)

Revision ID: 000_initial_schema
Revises:
Create Date: 2026-04-21

Creates every table defined on data_provider.sql.models.Base via
Base.metadata.create_all() so the DDL stays authoritative in a single
place (the SQLAlchemy model file). Subsequent schema changes should add
new revision files with explicit op.* calls (autogenerate-friendly).

Tables created:
  - properties         current state per canonical_id
  - units              current state per (canonical_id, unit_id)
  - runs               run-date registry
  - property_snapshots per-run 46-key payload
  - run_reports        per-run summary
  - run_issues         per-run issues (JSONL rows)
  - run_ledger         per-run cost/status ledger
  - scrape_events      append-only audit log
  - extraction_results per (run_date, property_id) extraction output
  - scrape_profiles    per-property self-learning profile
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence, Union

from alembic import op

# Ensure ma_poc/ is on sys.path when the migration file is imported directly.
_here = Path(__file__).resolve().parent
_ma_poc = _here.parent.parent.parent / "ma_poc"
if str(_ma_poc) not in sys.path:
    sys.path.insert(0, str(_ma_poc))

from data_provider.sql.models import Base  # noqa: E402

revision: str = "000_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
