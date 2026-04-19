"""SqlDataProvider — DataProvider backed by SQLAlchemy (Postgres or SQLite).

Instantiate with either a URL or a pre-built engine. `transaction()` opens
a single session used by every store for the duration of the block;
commit happens on clean exit, rollback on any exception.

Callers (Postgres, SQLite, dual-write) should not instantiate this directly
outside of wiring — use `data_provider.get_data_provider()` with
`DATA_PROVIDER=postgres` (or `sqlite`) + `DATABASE_URL`.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine

from data_provider.contracts import DataProvider
from data_provider.sql.engine import make_engine, make_session_factory
from data_provider.sql.models import Base
from data_provider.sql.stores import (
    SqlExtractionResultStore,
    SqlProfileStore,
    SqlPropertyStateStore,
    SqlRunStore,
    SqlScrapeEventStore,
    SqlUnitStateStore,
    _SessionHolder,
)

log = logging.getLogger(__name__)


class SqlDataProvider(DataProvider):
    """Dialect-agnostic SQL-backed DataProvider."""

    def __init__(
        self,
        url: str | None = None,
        *,
        engine: Engine | None = None,
        create_schema: bool = True,
        provider_name: str = "postgres",
    ) -> None:
        self._engine: Engine = engine if engine is not None else make_engine(url)
        if create_schema:
            # Idempotent — safe on every startup. Alembic is the preferred
            # mechanism for production migrations; this stays for local/CI.
            Base.metadata.create_all(self._engine)

        session_factory = make_session_factory(self._engine)
        self._holder = _SessionHolder(session_factory)
        self._name = provider_name
        self._closed = False

        self.property_state = SqlPropertyStateStore(self._holder)
        self.unit_state = SqlUnitStateStore(self._holder)
        self.runs = SqlRunStore(self._holder)
        self.scrape_events = SqlScrapeEventStore(self._holder)
        self.profiles = SqlProfileStore(self._holder)
        self.extraction_results = SqlExtractionResultStore(self._holder)

    @property
    def name(self) -> str:
        return self._name

    @property
    def engine(self) -> Engine:
        return self._engine

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Bind a single session to every store for the duration.

        Nested transactions reuse the outermost session — the outer block
        decides commit/rollback. Uses a SAVEPOINT via `begin_nested()` so
        inner-block errors can roll back without killing the outer txn.
        """
        outermost = self._holder.active is None
        if outermost:
            session = self._holder._factory()
            self._holder.active = session
            try:
                yield
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
                self._holder.active = None
        else:
            # Nested: run inside a SAVEPOINT.
            nested = self._holder.active.begin_nested()
            try:
                yield
                nested.commit()
            except Exception:
                nested.rollback()
                raise

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._engine.dispose()
        finally:
            self._closed = True
