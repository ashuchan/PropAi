"""SQLAlchemy engine factory + dialect-aware UPSERT helper.

We resolve the URL in this priority order:
  1. explicit argument to `make_engine(url=...)`
  2. DATABASE_URL env var
  3. sqlite:///{DATA_DIR}/propai.db  — last-resort local fallback

Postgres expects `postgresql+psycopg://user:pass@host:port/dbname`. The
`psycopg` driver (v3) is the current recommendation.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, Insert, create_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger(__name__)


def resolve_database_url(url: str | None = None) -> str:
    """Return the URL to connect to. Never raises."""
    if url:
        return url
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        return env_url
    # Last-resort: local SQLite in the DATA_DIR.
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    if not data_dir.is_absolute():
        # Match the factory's rooting — relative to ma_poc/ root.
        ma_poc_root = Path(__file__).resolve().parent.parent.parent
        data_dir = (ma_poc_root / data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{data_dir / 'propai.db'}"


def make_engine(url: str | None = None, **kwargs: Any) -> Engine:
    """Create a SQLAlchemy Engine with sensible defaults.

    SQLite gets `check_same_thread=False` so multi-thread test harnesses
    work; Postgres gets a small connection pool suitable for the scraper
    pool sizes we use in production.
    """
    resolved = resolve_database_url(url)
    connect_args: dict[str, Any] = {}
    engine_kwargs: dict[str, Any] = {"future": True}

    if resolved.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    else:
        # Reasonable PG defaults for a concurrent scraper fleet.
        engine_kwargs.setdefault("pool_size", 5)
        engine_kwargs.setdefault("max_overflow", 10)
        engine_kwargs.setdefault("pool_pre_ping", True)

    engine_kwargs["connect_args"] = connect_args
    engine_kwargs.update(kwargs)

    log.info("Creating SQLAlchemy engine: %s", _redact(resolved))
    return create_engine(resolved, **engine_kwargs)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def dialect_insert(engine: Engine, table: Any) -> Insert:
    """Return the dialect-specific `insert()` so callers can `.on_conflict_do_update`.

    Supported dialects: postgresql, sqlite. Other dialects raise
    NotImplementedError — we don't silently fall back because the upsert
    semantics wouldn't match.
    """
    name = engine.dialect.name
    if name == "postgresql":
        return pg_insert(table)
    if name == "sqlite":
        return sqlite_insert(table)
    raise NotImplementedError(f"UPSERT not implemented for dialect {name!r}. Use postgresql or sqlite.")


def _redact(url: str) -> str:
    """Strip password from a DB URL for log output."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" not in rest:
        return url
    creds, tail = rest.rsplit("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{tail}"
    return url
