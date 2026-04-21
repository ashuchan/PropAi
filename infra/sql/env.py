"""Alembic environment — reads connection from DATABASE_URL env var.

autogenerate is explicitly disabled (target_metadata = None) to prevent
accidental migration generation via 'alembic revision --autogenerate'.
Every migration in this project is hand-written.
"""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No ORM metadata — autogenerate is disabled intentionally.
target_metadata = None

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
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
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
            # Transactional DDL: each migration runs in its own transaction.
            # Postgres supports this; MySQL would not.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
