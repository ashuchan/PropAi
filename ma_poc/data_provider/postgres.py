"""Postgres DataProvider — thin wrapper over `SqlDataProvider`.

Instantiation builds a `postgresql+psycopg://` engine and creates any missing
tables via `Base.metadata.create_all`. For production you should manage
schema through Alembic (see `data_provider/alembic/`), but `create_all` is
idempotent and safe to leave on during bring-up.

This file used to stub `NotImplementedError` — Phase 4 replaces that with
a real implementation that reuses every store in `data_provider/sql/`.
"""

from __future__ import annotations

from data_provider.sql.provider import SqlDataProvider


class PostgresDataProvider(SqlDataProvider):
    """SqlDataProvider pinned to a Postgres URL.

    Accepts the same `url` arg; if omitted, falls through to DATABASE_URL
    env var (see `data_provider.sql.engine.resolve_database_url`). Use a
    `postgresql+psycopg://user:pass@host:5432/dbname` connection string.
    """

    def __init__(self, url: str | None = None, *, create_schema: bool = True) -> None:
        super().__init__(url=url, create_schema=create_schema, provider_name="postgres")
