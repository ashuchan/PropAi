"""Distributed (cross-task) per-host token-bucket rate limiter.

.. warning::

   **DESIGN-REVIEW OUTCOME (2026-07-18): do NOT wire this at 100K scale.**
   An architecture review found the token-bucket math correct but the
   SUBSTRATE wrong for this workload: a fleet-wide ``SELECT … FOR UPDATE``
   hot row on the primary OLTP Cloud SQL, plus a second 15-connection pool
   per task across hundreds of tasks, can exhaust ``max_connections`` and
   take down the data plane; the ``max_wait`` cap + full-deficit reservation
   + unconditional fire converts a smooth stream into synchronized
   ~30s-boundary bursts once backlog builds (the opposite of the politeness
   goal); and fail-open silently disables the cap under exactly the
   contention this design creates. **Prefer the coordination-free static
   partition** (``host_throttle`` ``RATELIMIT_DIVIDE_BY_TASKS`` + strided
   sharding — sum of per-task rates is provably ≤ budget with zero shared
   state). Reach for a Memorystore/Redis atomic token bucket — which
   tolerates hot keys — only if divide-by-task's over-politeness on
   concentrated hosts must be reclaimed; never this Postgres hot row. Kept
   in-tree as a reference for what "distributed coordination" would entail
   and the pitfalls it carries.


``fetch/host_throttle.py`` bounds ONE process. At 100K-property scale the
fleet runs hundreds of parallel Cloud Run tasks — each a separate process
— all hitting the same single-hostname PMS backends (``sightmap.com``,
``doorway-api.knockrentals.com``, ``www.rentcafe.com``,
``nestiolistings.com``). No in-process limiter can bound that aggregate:
each task paces itself politely while 300 tasks together hammer the host.

This module bounds the AGGREGATE initiation rate to each registrable host
across every task, using a shared token bucket persisted in the existing
Cloud SQL / Postgres database (same engine as the data provider,
``data_provider/sql/engine.py``). All tasks contend on one row per host
via ``SELECT … FOR UPDATE``, so the combined rate to a backend is provably
capped regardless of task count.

STATUS: **built but NOT WIRED** into the fetch path. ``throttle(url)`` /
``acquire(host)`` mirror ``fetch/host_throttle`` and are ready to drop into
``probe_get`` + the knock/rentcafe drills when the 100K run needs provable
aggregate caps. Until then it is dormant — the schema isn't created and no
DB traffic is issued (``throttle`` is a no-op unless
``RATELIMIT_DISTRIBUTED_ENABLED=1``).

Algorithm — virtual-scheduling token bucket. Under the per-host row lock we
refill by ``elapsed × rps`` (capped at ``capacity`` burst) and either grant
a token immediately or reserve a future slot by advancing the bucket's
baseline, so concurrent waiters across ALL tasks queue at exactly
``1/rps`` spacing. The **DB clock** (``clock_timestamp()``) is the single
time source, so cross-task clock skew cannot corrupt the bucket. On sqlite
(tests) ``FOR UPDATE`` is a no-op and the Python clock is used — the token
math is identical, only the true multi-process guarantee needs Postgres.

Fail-open: any DB error returns ``wait=0`` (never block a scrape on the
limiter's own outage), logged once per error.

Config (env), all under ``RATELIMIT_DISTRIBUTED_``:

=================================  ==========  ==================================
Env var                            Default     Meaning
=================================  ==========  ==================================
``RATELIMIT_DISTRIBUTED_ENABLED``  ``"0"``     ``throttle()`` is a no-op unless ``1``
``RATELIMIT_DISTRIBUTED_DEFAULT_RPS`` ``5.0``  **AGGREGATE** rps per host (all tasks)
``RATELIMIT_DISTRIBUTED_CAPACITY`` (= rps)     burst token capacity per host
``RATELIMIT_DISTRIBUTED_MAX_WAIT_S`` ``30.0``  cap one caller's sleep (load-sheds above)
``RATELIMIT_DISTRIBUTED_PER_HOST_RPS`` (unset) JSON overrides e.g. ``{"sightmap.com":3}``
=================================  ==========  ==================================
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator

from sqlalchemy import Column, Float, MetaData, String, Table, select, text, update
from sqlalchemy.engine import Engine

from ma_poc.fetch.host_throttle import throttle_key

log = logging.getLogger(__name__)

_METADATA = MetaData()

# One row per registrable backend host. Self-contained metadata (NOT the
# data-provider Base) so this module adds zero footprint to models.py /
# Alembic until it is actually wired — ensure_schema() creates it on demand.
HOST_RATE_BUCKETS = Table(
    "host_rate_buckets",
    _METADATA,
    Column("host", String(256), primary_key=True),
    Column("tokens", Float, nullable=False),
    Column("updated_at", Float, nullable=False),  # epoch seconds (DB clock)
    Column("rps", Float, nullable=False),
    Column("capacity", Float, nullable=False),
)


# --------------------------------------------------------------------------- #
# Config (read live from env)                                                  #
# --------------------------------------------------------------------------- #

def _enabled() -> bool:
    return os.getenv("RATELIMIT_DISTRIBUTED_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _default_rps() -> float:
    try:
        return max(0.01, float(os.getenv("RATELIMIT_DISTRIBUTED_DEFAULT_RPS", "5")))
    except (TypeError, ValueError):
        return 5.0


def _capacity(rps: float) -> float:
    raw = os.getenv("RATELIMIT_DISTRIBUTED_CAPACITY", "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            pass
    return max(1.0, rps)


def _max_wait_s() -> float:
    try:
        return max(0.0, float(os.getenv("RATELIMIT_DISTRIBUTED_MAX_WAIT_S", "30")))
    except (TypeError, ValueError):
        return 30.0


def _per_host_rps(host: str) -> float | None:
    raw = os.getenv("RATELIMIT_DISTRIBUTED_PER_HOST_RPS", "").strip()
    if not raw:
        return None
    try:
        mapping = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(mapping, dict):
        return None
    val = mapping.get(host)
    if val is None:
        return None
    try:
        return max(0.01, float(val))
    except (TypeError, ValueError):
        return None


def _rps_for(host: str) -> float:
    return _per_host_rps(host) or _default_rps()


# --------------------------------------------------------------------------- #
# Limiter                                                                      #
# --------------------------------------------------------------------------- #

class DistributedHostRateLimiter:
    """Cross-task per-host token bucket backed by a shared SQL row.

    Args:
        engine: SQLAlchemy Engine. Defaults to ``make_engine()`` (Cloud SQL
            in prod, DATABASE_URL / sqlite fallback locally).
        clock: Optional monotonic-free wall clock for tests. When set, it
            overrides the DB clock (single-process tests have no skew).
    """

    def __init__(
        self,
        engine: Engine | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        # Lazy import (matches shard_entry.py): a dormant/unwired limiter
        # shouldn't drag the DB layer into every fetch import.
        from ma_poc.data_provider.sql.engine import make_engine, make_session_factory

        self._engine = engine if engine is not None else make_engine()
        self._session_factory = make_session_factory(self._engine)
        self._clock = clock
        self._schema_ready = False
        self._lock = threading.Lock()

    def ensure_schema(self) -> None:
        """Create the ``host_rate_buckets`` table if absent (idempotent)."""
        with self._lock:
            if self._schema_ready:
                return
            _METADATA.create_all(self._engine, checkfirst=True)
            self._schema_ready = True

    def _now(self, session: object) -> float:
        if self._clock is not None:
            return self._clock()
        bind = getattr(session, "bind", None)
        dialect = getattr(getattr(bind, "dialect", None), "name", "")
        if dialect == "postgresql":
            val = session.execute(  # type: ignore[attr-defined]
                text("SELECT EXTRACT(EPOCH FROM clock_timestamp())")
            ).scalar()
            return float(val) if val is not None else time.time()
        return time.time()

    def acquire(self, host: str) -> float:
        """Reserve one token for *host* from the shared bucket.

        Returns the number of seconds the caller should sleep before
        issuing the request (0.0 if a token was immediately available). The
        DB row lock is held only for the brief token math — never during the
        sleep. Never raises: any DB error returns 0.0 (fail-open), logged.
        """
        if not host:
            return 0.0
        rps = _rps_for(host)
        capacity = _capacity(rps)
        max_wait = _max_wait_s()
        try:
            from ma_poc.data_provider.sql.engine import dialect_insert

            self.ensure_schema()
            with self._session_factory() as session:
                with session.begin():
                    now = self._now(session)
                    # Ensure the row exists (idempotent; avoids the insert
                    # race between two tasks that both see it missing).
                    ins = dialect_insert(self._engine, HOST_RATE_BUCKETS).values(
                        host=host,
                        tokens=capacity,
                        updated_at=now,
                        rps=rps,
                        capacity=capacity,
                    )
                    # dialect_insert() is typed as the base Insert; the
                    # dialect-specific on_conflict_* lives on the pg/sqlite
                    # subclass returned at runtime (same gap the data-provider
                    # upsert callers hit).
                    ins = ins.on_conflict_do_nothing(  # type: ignore[attr-defined]
                        index_elements=["host"]
                    )
                    session.execute(ins)

                    row = session.execute(
                        select(
                            HOST_RATE_BUCKETS.c.tokens,
                            HOST_RATE_BUCKETS.c.updated_at,
                        )
                        .where(HOST_RATE_BUCKETS.c.host == host)
                        .with_for_update()
                    ).one()
                    cur_tokens = float(row[0])
                    cur_updated = float(row[1])

                    tokens = min(capacity, cur_tokens + (now - cur_updated) * rps)
                    if tokens >= 1.0:
                        new_tokens = tokens - 1.0
                        new_updated = now
                        wait = 0.0
                    else:
                        wait = (1.0 - tokens) / rps
                        # Reserve the FULL deficit so other tasks queue behind
                        # this slot even when this caller's sleep is capped.
                        new_tokens = 0.0
                        new_updated = now + wait
                    session.execute(
                        update(HOST_RATE_BUCKETS)
                        .where(HOST_RATE_BUCKETS.c.host == host)
                        .values(
                            tokens=new_tokens,
                            updated_at=new_updated,
                            rps=rps,
                            capacity=capacity,
                        )
                    )
            # min() so a single caller never blocks longer than max_wait; the
            # reservation above still keeps the *queue* correctly spaced.
            return min(wait, max_wait)
        except Exception as exc:  # noqa: BLE001 — fail-open, never block a scrape
            log.warning(
                "distributed_throttle.db_error host=%s err=%s: %s — failing open",
                host,
                type(exc).__name__,
                str(exc)[:160],
            )
            return 0.0


_LIMITER: DistributedHostRateLimiter | None = None
_LIMITER_LOCK = threading.Lock()


def get_distributed_limiter() -> DistributedHostRateLimiter:
    global _LIMITER
    with _LIMITER_LOCK:
        if _LIMITER is None:
            _LIMITER = DistributedHostRateLimiter()
        return _LIMITER


@contextlib.contextmanager
def throttle(url_or_host: str) -> Iterator[None]:
    """Block (in the calling thread) until the shared per-host aggregate
    budget allows a request to the registrable host of *url_or_host*.

    No-op unless ``RATELIMIT_DISTRIBUTED_ENABLED=1``. Sleeps OUTSIDE the DB
    transaction (the row lock is released before the sleep).
    """
    if not _enabled():
        yield
        return
    host = throttle_key(url_or_host)
    if not host:
        yield
        return
    wait = get_distributed_limiter().acquire(host)
    if wait > 0.0:
        time.sleep(wait)
    yield


def _reset_for_tests() -> None:
    global _LIMITER
    with _LIMITER_LOCK:
        _LIMITER = None
