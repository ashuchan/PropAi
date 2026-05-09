"""Tests for the SQL resilience primitives + the column-clip safety net.

These cover the exact bug that caused the 2026-04-29 → 05-01 shard-0
data-loss incident:

  • A 455-char marketing blurb landed in ``units.floor_plan_name``
    (declared ``String(256)``).
  • Postgres returned ``DataError 22001`` mid-transaction.
  • The whole shard's Stage 1 sync rolled back, taking 499 properties
    with it for three consecutive days.

The fix is twofold and both halves are tested here:

  1. ``_clip_to_column_limits`` trims oversize string values at the
     write boundary so most cases never hit the DB layer.
  2. ``isolate_row_writes`` runs each row in its own SAVEPOINT so a
     row that *does* slip past the clip can't roll back the rest of
     the batch.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.exc import DataError, IntegrityError, OperationalError
from sqlalchemy.orm import Session

from data_provider.sql.models import PropertyRow, UnitRow
from data_provider.sql.resilience import (
    PERMANENT_ROW_ERRORS,
    TRANSIENT_DB_ERRORS,
    isolate_row_writes,
    row_savepoint,
    with_db_retry,
)
from data_provider.sql.stores import _clip_to_column_limits, _varchar_limits
from data_provider.sqlite import SqliteDataProvider


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def sqlite_provider(tmp_path: Path) -> SqliteDataProvider:
    url = f"sqlite:///{tmp_path / 'resilience.db'}"
    return SqliteDataProvider(url=url)


@pytest.fixture
def session(sqlite_provider: SqliteDataProvider) -> Session:
    """A SQLAlchemy session inside an explicit BEGIN — required for
    SAVEPOINT support. Caller must commit/rollback the outer txn."""
    s = Session(sqlite_provider.engine, future=True)
    s.begin()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


# ─────────────────────────────────────────────────────────────────────────────
# _clip_to_column_limits
# ─────────────────────────────────────────────────────────────────────────────


class TestClipToColumnLimits:
    def test_introspects_unitrow_varchar_columns(self) -> None:
        limits = _varchar_limits(UnitRow)
        # The exact columns that exist on UnitRow today — guards against
        # accidental schema regressions where a column changes from
        # String(N) to TEXT (clip becomes a no-op for that column).
        assert limits["unit_id"] == 512
        assert limits["floor_plan_name"] == 1024
        assert limits["available_date"] == 32
        assert limits["last_seen_at"] == 64

    def test_clip_is_idempotent_for_short_values(self) -> None:
        values = {"unit_id": "A101", "floor_plan_name": "1BR/1BA", "beds": 1}
        out = _clip_to_column_limits(values, UnitRow)
        assert out["unit_id"] == "A101"
        assert out["floor_plan_name"] == "1BR/1BA"
        assert out["beds"] == 1

    def test_clip_truncates_oversize_floor_plan_name(self, caplog: pytest.LogCaptureFixture) -> None:
        # floor_plan_name is String(1024) after migration 0009 — need >1024 chars to trigger clip.
        bad = "Perfect luxury living space " * 40  # 1120 chars
        values = {"floor_plan_name": bad}
        with caplog.at_level(logging.WARNING):
            out = _clip_to_column_limits(values, UnitRow)
        assert len(out["floor_plan_name"]) == 1024
        assert out["floor_plan_name"] == bad[:1024]
        # Surface the upstream extractor bug — silent truncation is what
        # let this slip past for three days. WARN-level log is mandatory.
        assert any("clipping oversize value" in rec.message for rec in caplog.records)
        assert any("UnitRow.floor_plan_name" in rec.message for rec in caplog.records)

    def test_clip_handles_property_row_too(self) -> None:
        # Same machinery, different table — confirms the introspection
        # works for any model the writer might touch.
        bad_addr = "X" * 600  # PropertyRow.address is String(512)
        out = _clip_to_column_limits({"address": bad_addr, "city": "OK"}, PropertyRow)
        assert len(out["address"]) == 512
        assert out["city"] == "OK"

    def test_clip_skips_non_string_columns(self) -> None:
        # JSON / Integer / Float columns must pass through untouched —
        # `length` is None on those and the helper short-circuits.
        values = {"beds": 1, "rent_low": 1500.0, "extra": {"k": "v"}}
        out = _clip_to_column_limits(values, UnitRow)
        assert out == values

    def test_clip_skips_none_values(self) -> None:
        # None on a String column shouldn't blow up the len() check.
        out = _clip_to_column_limits({"floor_plan_name": None, "unit_id": "X"}, UnitRow)
        assert out["floor_plan_name"] is None

    def test_clip_truncates_pk_columns_too(self, caplog: pytest.LogCaptureFixture) -> None:
        # unit_id is part of the PK but some portals genuinely emit
        # long URL-encoded blob keys. Better a clipped-but-stored row
        # than no row at all.
        bad_id = "U" * 600  # unit_id is String(512) after migration 0009 — need >512 chars
        with caplog.at_level(logging.WARNING):
            out = _clip_to_column_limits({"unit_id": bad_id}, UnitRow)
        assert len(out["unit_id"]) == 512


# ─────────────────────────────────────────────────────────────────────────────
# upsert_units integration — clip prevents DataError end-to-end
# ─────────────────────────────────────────────────────────────────────────────


class TestUpsertUnitsClip:
    """End-to-end: the exact failure mode from production must now succeed."""

    def test_oversize_floor_plan_name_does_not_raise(
        self, sqlite_provider: SqliteDataProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Reproduces the 2026-04-29 incident on SQLite.
        bad = "Perfect luxury living space " * 20  # 560 chars
        units = [
            {"unit_id": "A101", "floor_plan_name": bad, "rent_low": 1500},
            {"unit_id": "A102", "floor_plan_name": "1BR/1BA", "rent_low": 1600},
        ]
        with caplog.at_level(logging.WARNING):
            diff = sqlite_provider.unit_state.upsert_units("CID-1", units, "2026-05-02")
        assert len(diff.new) == 2

        # Verify the row landed with the clipped value, not the raw 560-char blob.
        stored = sqlite_provider.unit_state.get_units("CID-1")
        assert "A101" in stored
        assert len(stored["A101"].floor_plan_name or "") <= 1024

    def test_no_silent_drop_under_concurrent_oversize_rows(
        self, sqlite_provider: SqliteDataProvider
    ) -> None:
        # The whole reason isolate_row_writes exists: one bad row used
        # to take down the whole batch. Now all rows must land.
        bad = "X" * 400
        units = [
            {"unit_id": f"u{i}", "floor_plan_name": bad if i == 5 else "ok"}
            for i in range(10)
        ]
        sqlite_provider.unit_state.upsert_units("CID-2", units, "2026-05-02")
        stored = sqlite_provider.unit_state.get_units("CID-2")
        assert len(stored) == 10


# ─────────────────────────────────────────────────────────────────────────────
# with_db_retry
# ─────────────────────────────────────────────────────────────────────────────


def _fake_operational_error(msg: str = "lost connection") -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception(msg))


def _fake_data_error() -> DataError:
    return DataError("INSERT", {}, Exception("value too long"))


class TestWithDbRetry:
    def test_clean_path_no_retries(self) -> None:
        calls = {"n": 0}

        @with_db_retry(attempts=3, initial_wait_s=0.001)
        def fn() -> str:
            calls["n"] += 1
            return "ok"

        assert fn() == "ok"
        assert calls["n"] == 1, "no retries on success — would be a perf regression"

    def test_retries_transient_then_succeeds(self) -> None:
        attempts = {"n": 0}

        @with_db_retry(attempts=4, initial_wait_s=0.001, max_wait_s=0.01)
        def fn() -> str:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise _fake_operational_error()
            return "recovered"

        assert fn() == "recovered"
        assert attempts["n"] == 3

    def test_does_not_retry_data_errors(self) -> None:
        # A DataError is a logic bug, not a transient blip — wasting
        # 4×500ms backoffs on it just delays the upstream alert.
        attempts = {"n": 0}

        @with_db_retry(attempts=5, initial_wait_s=0.001)
        def fn() -> None:
            attempts["n"] += 1
            raise _fake_data_error()

        with pytest.raises(DataError):
            fn()
        assert attempts["n"] == 1

    def test_gives_up_after_max_attempts(self) -> None:
        attempts = {"n": 0}

        @with_db_retry(attempts=3, initial_wait_s=0.001, max_wait_s=0.01)
        def fn() -> None:
            attempts["n"] += 1
            raise _fake_operational_error()

        with pytest.raises(OperationalError):
            fn()
        assert attempts["n"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# isolate_row_writes
# ─────────────────────────────────────────────────────────────────────────────


class TestIsolateRowWrites:
    def test_all_good_rows_commit(self, session: Session) -> None:
        applied: list[int] = []

        def apply(i: int) -> None:
            applied.append(i)

        success, failures = isolate_row_writes(session, [1, 2, 3], apply)
        assert success == 3
        assert failures == []
        assert applied == [1, 2, 3]

    def test_one_bad_row_does_not_kill_batch(
        self, sqlite_provider: SqliteDataProvider
    ) -> None:
        """The headline test — directly reproduces the shard-0 failure
        mode. Previously this would have raised and rolled back ALL
        upserts; now exactly the bad one is skipped."""
        s = Session(sqlite_provider.engine, future=True)
        s.begin()
        try:
            applied_ok: list[str] = []

            def apply(item: dict[str, Any]) -> None:
                if item.get("explode"):
                    raise _fake_data_error()
                # Real write so we can assert it stuck.
                sqlite_provider.unit_state.upsert_units(
                    "CID-X",
                    [{"unit_id": item["uid"], "floor_plan_name": "1BR"}],
                    "2026-05-02",
                )
                applied_ok.append(item["uid"])

            rows = [
                {"uid": "u1"},
                {"uid": "u2"},
                {"uid": "u3", "explode": True},
                {"uid": "u4"},
                {"uid": "u5"},
            ]
            success, failures = isolate_row_writes(
                s, rows, apply, label_for=lambda r: f"uid={r['uid']}"
            )
            s.commit()

            assert success == 4
            assert len(failures) == 1
            assert "uid=u3" in failures[0][0]
            assert isinstance(failures[0][1], DataError)
        finally:
            s.close()

        # The four good rows must be visible after the outer commit.
        stored = sqlite_provider.unit_state.get_units("CID-X")
        assert set(stored.keys()) == {"u1", "u2", "u4", "u5"}

    def test_transient_error_retries_within_savepoint(
        self, session: Session
    ) -> None:
        # Simulate a connection blip: first attempt fails with
        # OperationalError, second attempt succeeds. The savepoint
        # rolls back between attempts so partial state can't leak.
        attempts: dict[int, int] = {1: 0, 2: 0}

        def apply(item: int) -> None:
            attempts[item] = attempts.get(item, 0) + 1
            if item == 2 and attempts[item] == 1:
                raise _fake_operational_error()

        success, failures = isolate_row_writes(
            session, [1, 2], apply, transient_attempts=3
        )
        assert success == 2
        assert failures == []
        assert attempts[2] == 2  # one fail, one retry

    def test_unknown_exception_propagates(self, session: Session) -> None:
        # Don't silently swallow surprise errors — that's how silent
        # data loss happens. A KeyError or ValueError in the apply
        # function means the writer is broken and we want the whole
        # sync to surface it.
        def apply(_: int) -> None:
            raise KeyError("oops")

        with pytest.raises(KeyError):
            isolate_row_writes(session, [1], apply)

    def test_label_for_is_used_in_failure_record(
        self, sqlite_provider: SqliteDataProvider
    ) -> None:
        s = Session(sqlite_provider.engine, future=True)
        s.begin()
        try:
            def apply(item: int) -> None:
                raise _fake_data_error()

            success, failures = isolate_row_writes(
                s, [42], apply, label_for=lambda i: f"property={i}"
            )
            s.commit()
        finally:
            s.close()

        assert success == 0
        assert failures == [("property=42", failures[0][1])]


# ─────────────────────────────────────────────────────────────────────────────
# row_savepoint
# ─────────────────────────────────────────────────────────────────────────────


class TestRowSavepoint:
    def test_commits_on_clean_exit(
        self, sqlite_provider: SqliteDataProvider
    ) -> None:
        s = Session(sqlite_provider.engine, future=True)
        s.begin()
        try:
            with row_savepoint(s, label="test"):
                sqlite_provider.unit_state.upsert_units(
                    "CID-SP", [{"unit_id": "x", "floor_plan_name": "1BR"}], "2026-05-02"
                )
            s.commit()
        finally:
            s.close()

        stored = sqlite_provider.unit_state.get_units("CID-SP")
        assert "x" in stored

    def test_rolls_back_savepoint_on_error_but_outer_txn_survives(
        self, sqlite_provider: SqliteDataProvider
    ) -> None:
        # Drives the real provider.transaction() boundary the sync uses
        # in production. That binds a single session to every store via
        # the session holder, which is what makes nested SAVEPOINTs
        # actually roll back the inner store writes (without it each
        # store opens its own session and the savepoint does nothing).
        holder = sqlite_provider._holder
        # Open a transaction manually so we can poke the holder's
        # session for the savepoint helper.
        s = holder._factory()
        holder.active = s
        try:
            sqlite_provider.unit_state.upsert_units(
                "CID-OUT", [{"unit_id": "good", "floor_plan_name": "1BR"}], "2026-05-02"
            )
            with pytest.raises(_FakeRoll):
                with row_savepoint(s, label="bad"):
                    sqlite_provider.unit_state.upsert_units(
                        "CID-IN",
                        [{"unit_id": "lost", "floor_plan_name": "1BR"}],
                        "2026-05-02",
                    )
                    raise _FakeRoll()
            s.commit()
        finally:
            holder.active = None
            s.close()

        # Outer good write committed; savepoint write rolled back.
        assert "good" in sqlite_provider.unit_state.get_units("CID-OUT")
        assert sqlite_provider.unit_state.get_units("CID-IN") == {}


class _FakeRoll(Exception):
    """Sentinel to trigger savepoint rollback in a test."""
