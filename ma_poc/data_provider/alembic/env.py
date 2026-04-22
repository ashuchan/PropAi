"""Alembic environment for the data-provider SQL schema.

Resolves the DB URL at runtime from `DATABASE_URL` (via
`data_provider.sql.engine.resolve_database_url`) so the same alembic
command works against Postgres in prod and SQLite locally.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure ma_poc/ is on sys.path so `data_provider.*` resolves when alembic
# is invoked from any cwd.
_MA_POC_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_MA_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_MA_POC_ROOT))

from data_provider.sql.engine import resolve_database_url  # noqa: E402
from data_provider.sql.models import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Plug in our URL resolver so migrations target the right DB.
# Escape `%` -> `%%` because Alembic stores this in a ConfigParser, which
# treats `%` as interpolation syntax (URL-encoded passwords contain `%`).
config.set_main_option("sqlalchemy.url", resolve_database_url().replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit raw SQL (no DBAPI connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to the target DB and run migrations in a transaction."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
