"""initial data-provider schema (10 tables)

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-19

Creates every table defined on `data_provider.sql.models.Base`. We call
`Base.metadata.create_all()` inside the migration rather than hand-writing
10 `op.create_table()` blocks — less duplication, and the DDL stays in
one authoritative place (the SQLAlchemy model file). Subsequent schema
changes should use `alembic revision --autogenerate` which will emit
explicit op.* calls.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from data_provider.sql.models import Base

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
