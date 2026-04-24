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

    When ``CLOUD_SQL_INSTANCE`` is set we route Postgres through the Cloud
    SQL Python Connector so Cloud Run tasks authenticate via short-lived
    OAuth tokens (``cloudsql.iam_authentication=on`` on the instance
    rejects passwords). Any explicit URL/DATABASE_URL is ignored for
    host+password in that case — only the database name, username, and
    scheme are taken from the URL.
    """
    if os.getenv("CLOUD_SQL_INSTANCE"):
        return _make_cloud_sql_engine(url, **kwargs)

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


def _make_cloud_sql_engine(url: str | None, **kwargs: Any) -> Engine:
    """Build an engine that authenticates to Cloud SQL via the connector.

    Reads ``CLOUD_SQL_INSTANCE`` (``project:region:instance``) and optional
    ``CLOUD_SQL_IP_TYPE`` (``PRIVATE`` | ``PUBLIC``; default ``PRIVATE``).
    The user and database name still come from ``DATABASE_URL`` so callers
    keep one config surface — only the transport and auth change.
    """
    # Lazy-imported so local dev / migrate.py do not need the connector
    # package installed when CLOUD_SQL_INSTANCE is unset.
    from google.cloud.sql.connector import Connector, IPTypes

    instance = os.environ["CLOUD_SQL_INSTANCE"]
    ip_raw = os.getenv("CLOUD_SQL_IP_TYPE", "PRIVATE").upper()
    ip_type = IPTypes.PUBLIC if ip_raw == "PUBLIC" else IPTypes.PRIVATE

    from sqlalchemy.engine.url import make_url

    parsed = make_url(resolve_database_url(url))
    # make_url coerces missing URL pieces to empty strings, not None — so
    # `postgresql://@host/` parses to username='' / database=''. Treat
    # both shapes as "missing" and fail fast; silently connecting as an
    # empty user is how we'd ship another zero-rows-in-prod regression.
    if not parsed.username or not parsed.database:
        raise RuntimeError(
            "DATABASE_URL must supply at least user + database when "
            "CLOUD_SQL_INSTANCE is set (got "
            f"{parsed.render_as_string(hide_password=True)!r})"
        )

    connector = Connector()

    def _connect() -> Any:
        # Connector manages the token refresh loop; driver is fixed to
        # psycopg v3 to match the rest of the project.
        return connector.connect(
            instance,
            "psycopg",
            user=parsed.username,
            db=parsed.database,
            enable_iam_auth=True,
            ip_type=ip_type,
        )

    engine_kwargs: dict[str, Any] = {
        "future": True,
        "creator": _connect,
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 10,
    }
    engine_kwargs.update(kwargs)

    log.info(
        "Creating Cloud SQL engine via connector: instance=%s user=%s db=%s ip=%s",
        instance,
        parsed.username,
        parsed.database,
        ip_raw,
    )
    # Use postgresql+psycopg:// dialect string; the creator supplies the
    # actual DBAPI connection so host/port in the URL are ignored.
    return create_engine("postgresql+psycopg://", **engine_kwargs)


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
