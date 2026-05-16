"""Retry + per-row error isolation primitives for SQL writes.

Two failure modes shaped this module:

1. **Transient infrastructure errors** — `OperationalError` /
   `DisconnectionError` / `InterfaceError`. These usually come from a
   blip in Cloud SQL (failover, connection reset, deadlock retry) and
   typically clear on a second attempt. The right response is bounded
   exponential-backoff retry of the *whole* operation. Wrapping a
   transactional unit in `with_db_retry()` covers this.

2. **Per-row data errors** — `DataError` (e.g. sqlstate 22001 "value too
   long for type character varying(N)"), `IntegrityError` from a bad
   foreign-key, etc. These are logic bugs in the writer or upstream
   extractor; retrying does nothing. Worse, when a single bad row
   participates in a shared transaction with 498 good rows, the bad row
   rolls back the entire shard. The right response is to isolate each
   row in a SAVEPOINT, log the failure, and keep going.

   Cloud Run incident 2026-04-29 → 05-01: a 455-char marketing blurb
   landed in `units.floor_plan_name` (declared `String(256)`). The
   shard's Stage 1 transaction rolled back; 499 properties × 3 days of
   shard 0 data went missing from the DB despite the runner having
   scraped them successfully. ``isolate_row_writes()`` exists to make
   that class of bug a per-row data-quality warning instead of a
   shard-wide outage.

Both helpers are dialect-agnostic (Postgres + SQLite). SQLite supports
SAVEPOINTs through ``Session.begin_nested()`` the same way Postgres
does, so the same code path tests cleanly on both.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from sqlalchemy.exc import (
    DataError,
    DBAPIError,
    DisconnectionError,
    IntegrityError,
    InterfaceError,
    OperationalError,
)
from sqlalchemy.orm import Session
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

# Errors we treat as transient — retry the whole call.
# `DisconnectionError` is the explicit signal; `OperationalError` and
# `InterfaceError` cover the cases pg8000 / psycopg actually raise on
# Cloud SQL failover / connection reset / pool eviction. SQLite raises
# OperationalError for "database is locked" which also benefits from
# backoff in concurrent-test scenarios.
TRANSIENT_DB_ERRORS: tuple[type[Exception], ...] = (
    OperationalError,
    DisconnectionError,
    InterfaceError,
)

# Errors we treat as permanent for a given row — never retry, isolate
# and skip. `DataError` is the 22001 / 22003 family; `IntegrityError`
# is FK / unique violations. These are logic bugs in the writer or
# upstream pipeline and a retry would produce the same failure.
#
# Note: StatementError is NOT listed here even though it covers
# Python-level type coercion errors (e.g. float('')). StatementError is
# a superclass of DBAPIError — adding it here would catch OperationalError
# as permanent instead of transient. The right fix is upstream coercion
# (see _read_first in stores.py treating '' as None).
PERMANENT_ROW_ERRORS: tuple[type[Exception], ...] = (
    DataError,
    IntegrityError,
)

# Postgres SQLSTATE classes that mean "the row is broken, don't retry":
#   22 — data exception (invalid input syntax, numeric overflow, etc.)
#   23 — integrity constraint violation
# We classify by SQLSTATE in addition to type because pg8000 collapses
# every server error into the generic ``DatabaseError`` rather than the
# specific subclass — a 22P02 ``invalid input syntax for type integer``
# arrives wrapped as ``sqlalchemy.exc.DatabaseError`` (not ``DataError``)
# and slips past the type-based filter. See the 2026-05-16 shard-48
# incident where one bad property nuked the whole shard's PG sync.
_PERMANENT_SQLSTATE_PREFIXES: tuple[str, ...] = ("22", "23")


def _pg_sqlstate(exc: BaseException) -> str | None:
    """Best-effort extraction of a Postgres SQLSTATE from a DBAPI / SQLAlchemy
    exception. Returns the 5-char code or None.

    Handles the two driver shapes we see in production:
      • pg8000 — the original ``pg8000.exceptions.DatabaseError`` is
        constructed with a single ``dict`` argument carrying
        ``{'C': '<sqlstate>', ...}``. SQLAlchemy nests that exception
        under ``DBAPIError.orig``.
      • psycopg2/3 — expose ``.sqlstate`` / ``.pgcode`` directly on the
        driver exception.

    Never raises — a classification helper that crashes the savepoint
    loop would be worse than the bug we are trying to fix.
    """
    candidates: list[BaseException] = [exc]
    orig = getattr(exc, "orig", None)
    if orig is not None and orig is not exc:
        candidates.append(orig)

    for cand in candidates:
        for attr in ("sqlstate", "pgcode"):
            code = getattr(cand, attr, None)
            if isinstance(code, str) and len(code) == 5:
                return code
        args = getattr(cand, "args", None) or ()
        for a in args:
            if isinstance(a, dict):
                code = a.get("C") or a.get("sqlstate")
                if isinstance(code, str) and len(code) == 5:
                    return code
    return None


def is_permanent_row_error(
    exc: BaseException,
    *,
    permanent_types: tuple[type[Exception], ...] = PERMANENT_ROW_ERRORS,
) -> bool:
    """Classify ``exc`` as a permanent per-row failure that ``isolate_row_writes``
    should skip rather than re-raise.

    Returns True when:
      • ``exc`` is one of ``permanent_types`` (DataError, IntegrityError
        by default), OR
      • ``exc`` carries a Postgres SQLSTATE whose 2-char class is in
        ``_PERMANENT_SQLSTATE_PREFIXES`` (``22`` data exception or
        ``23`` integrity violation).

    The SQLSTATE branch is what fixes the 2026-05-16 incident: pg8000
    raises plain ``DatabaseError`` for SQLSTATE 22P02, so the type-based
    check alone misses it and the savepoint loop bubbles the error up,
    aborting Stage 1 of the shard's PG sync.
    """
    if isinstance(exc, permanent_types):
        return True
    if isinstance(exc, DBAPIError):
        code = _pg_sqlstate(exc)
        if code and code[:2] in _PERMANENT_SQLSTATE_PREFIXES:
            return True
    return False


def with_db_retry(
    *,
    attempts: int = 3,
    initial_wait_s: float = 0.5,
    max_wait_s: float = 8.0,
    transient: tuple[type[Exception], ...] = TRANSIENT_DB_ERRORS,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator that retries on transient DB errors with exponential backoff.

    Defaults: 3 attempts (initial + 2 retries), 0.5s → 1s → 2s → 4s
    capped at 8s. The expected wall-clock cost on a clean run is
    zero — tenacity only sleeps on a caught exception. Permanent errors
    propagate immediately.

    Usage:
        @with_db_retry()
        def upsert_thing(...): ...

    Or one-shot:
        with_db_retry()(fn)(*args)
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        wrapped = retry(
            retry=retry_if_exception_type(transient),
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=initial_wait_s, max=max_wait_s),
            reraise=True,
            before_sleep=lambda rs: log.warning(
                "DB retry %d/%d on %s: %s",
                rs.attempt_number,
                attempts,
                type(rs.outcome.exception()).__name__ if rs.outcome else "?",
                rs.outcome.exception() if rs.outcome else "",
            ),
        )(fn)
        return wrapped  # type: ignore[return-value]

    return decorator


@contextmanager
def row_savepoint(session: Session, *, label: str = "") -> Iterator[None]:
    """SAVEPOINT scope around a single row's writes.

    Rolls the savepoint back on any exception, logging it. Re-raises so
    the caller decides whether to swallow (per-row isolation) or bubble.
    Use ``isolate_row_writes()`` when you want swallow-and-continue
    semantics by default.
    """
    sp = session.begin_nested()
    try:
        yield
        sp.commit()
    except Exception:
        sp.rollback()
        if label:
            log.warning("savepoint %s rolled back", label)
        raise


def isolate_row_writes(
    session: Session,
    rows: Iterable[T],
    apply_row: Callable[[T], None],
    *,
    label_for: Callable[[T], str] | None = None,
    permanent_errors: tuple[type[Exception], ...] = PERMANENT_ROW_ERRORS,
    transient_errors: tuple[type[Exception], ...] = TRANSIENT_DB_ERRORS,
    transient_attempts: int = 2,
) -> tuple[int, list[tuple[str, Exception]]]:
    """Apply ``apply_row`` to each row in its own SAVEPOINT.

    Returns ``(success_count, failures)`` where ``failures`` is a list
    of ``(row_label, exception)`` for skipped rows. The session is left
    in a writeable state — all good rows that committed before the bad
    one stay committed, and all good rows after the bad one still get
    written.

    Behaviour by exception class:
      • ``permanent_errors`` (DataError, IntegrityError) — the savepoint
        rolls back, the row is recorded as a failure, the loop continues.
        These are the ones that used to nuke entire shards.
      • ``transient_errors`` (OperationalError, etc.) — retried up to
        ``transient_attempts`` times within the same savepoint scope.
        If the row still fails after retries it's recorded and skipped.
      • Anything else — the savepoint rolls back and the exception
        propagates. Don't silently swallow unknown errors.
    """
    success = 0
    failures: list[tuple[str, Exception]] = []

    for row in rows:
        label = label_for(row) if label_for else str(row)[:80]
        attempt = 0
        last_exc: Exception | None = None
        while True:
            attempt += 1
            sp = session.begin_nested()
            try:
                apply_row(row)
                sp.commit()
                success += 1
                last_exc = None
                break
            except Exception as exc:
                sp.rollback()
                # Order matters. SQLSTATE-based permanent detection runs
                # before the transient-type check because pg8000 collapses
                # data errors into the generic ``DatabaseError`` — leaving
                # the type check first would either misclassify them as
                # unknown (and re-raise) or, worse, waste retries on a row
                # that can never succeed.
                if is_permanent_row_error(exc, permanent_types=permanent_errors):
                    log.warning(
                        "isolate_row_writes: permanent error on %s — skipping. %s: %s",
                        label,
                        type(exc).__name__,
                        str(exc).splitlines()[0] if str(exc) else "",
                    )
                    last_exc = exc
                    break
                if isinstance(exc, transient_errors):
                    last_exc = exc
                    if attempt >= transient_attempts:
                        log.warning(
                            "isolate_row_writes: transient error on %s after %d attempts — skipping. %s: %s",
                            label,
                            attempt,
                            type(exc).__name__,
                            str(exc).splitlines()[0] if str(exc) else "",
                        )
                        break
                    log.info(
                        "isolate_row_writes: transient error on %s (attempt %d) — retrying. %s",
                        label,
                        attempt,
                        type(exc).__name__,
                    )
                    continue
                # Unknown error — propagate so silent bugs surface.
                raise

        if last_exc is not None:
            failures.append((label, last_exc))

    if failures:
        log.warning(
            "isolate_row_writes: %d/%d rows failed (%d succeeded)",
            len(failures),
            success + len(failures),
            success,
        )
    return success, failures


__all__ = [
    "TRANSIENT_DB_ERRORS",
    "PERMANENT_ROW_ERRORS",
    "is_permanent_row_error",
    "with_db_retry",
    "row_savepoint",
    "isolate_row_writes",
    "RetryError",
]
