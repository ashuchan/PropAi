"""Alembic environment — reads connection from DATABASE_URL env var.

target_metadata is sourced from data_provider.sql.models.Base so that
autogenerate detects drift against the authoritative SQLAlchemy model file.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure ma_poc/ is on sys.path so data_provider.* resolves regardless of cwd.
_here = Path(__file__).resolve().parent          # infra/sql/
_ma_poc = _here.parent.parent / "ma_poc"        # <repo_root>/ma_poc/
if str(_ma_poc) not in sys.path:
    sys.path.insert(0, str(_ma_poc))

from data_provider.sql.models import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL is required.\n"
        "For local use: DATABASE_URL=postgresql://USER@localhost:5432/jugnu (via cloud-sql-proxy)\n"
        "For CI: set via environment from the GCP secret."
    )
config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout for review — used in CI's dry-run gate."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            transaction_per_migration=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
