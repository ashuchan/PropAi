"""SQLite DataProvider — same `SqlDataProvider` with a sqlite:// URL.

Primary use cases:
  - Local development without a running Postgres
  - Tests (contract suite runs against `sqlite:///:memory:`)
  - CI gates

Not recommended for production: single-writer, weaker concurrency guarantees,
limited JSON operator support. Switch to `DATA_PROVIDER=postgres` before
running the real scraper at scale.
"""
from __future__ import annotations

import os
from pathlib import Path

from data_provider.sql.provider import SqlDataProvider


def _default_sqlite_url() -> str:
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    if not data_dir.is_absolute():
        ma_poc_root = Path(__file__).resolve().parent.parent
        data_dir = (ma_poc_root / data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{data_dir / 'propai.db'}"


class SqliteDataProvider(SqlDataProvider):
    def __init__(self, url: str | None = None, *, create_schema: bool = True) -> None:
        resolved = url or os.getenv("DATABASE_URL") or _default_sqlite_url()
        super().__init__(url=resolved, create_schema=create_schema, provider_name="sqlite")
