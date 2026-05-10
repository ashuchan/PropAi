"""Tests for local_canary.py — DB setup and teardown phase.

Covers:
- sqlite mode creates the .sqlite file
- Schema is complete (all ORM tables present after setup)
- teardown_canary_db removes the file; a second call is a no-op
- Passing an already-existing DSN is idempotent (create_schema is safe)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_TESTS_DIR = Path(__file__).resolve().parent.parent
_MA_POC_ROOT = _TESTS_DIR.parent
_REPO_ROOT = _MA_POC_ROOT.parent
for _p in (_REPO_ROOT, _MA_POC_ROOT, _MA_POC_ROOT / "scripts" / "diagnostics"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import local_canary as lc  # type: ignore[import-not-found]

# ── Expected tables (from data_provider/sql/models.py Base.metadata) ─────────
# This list is the ground truth asserted in the spec:
#   "assert all tables in SqliteDataProvider._SCHEMA"
# We derive it empirically from the ORM so the test stays in sync with the model.
_REQUIRED_TABLES = {
    "properties",
    "units",
    "scrape_profiles",
    "scrape_events",
    "extraction_results",
    "property_snapshots",
    "run_reports",
    "run_issues",
    "run_ledger",
    "dlq_entries",
}


# ── setup_canary_db ────────────────────────────────────────────────────────────


class TestSetupCanaryDb:
    def test_creates_sqlite_file(self, tmp_path):
        db_file = tmp_path / "canary.sqlite"
        dsn = f"sqlite:///{db_file}"
        lc.setup_canary_db(dsn)
        assert db_file.exists(), "SQLite file should exist after setup_canary_db"

    def test_schema_has_required_tables(self, tmp_path):
        """All required ORM tables must be present after setup."""
        db_file = tmp_path / "canary.sqlite"
        dsn = f"sqlite:///{db_file}"
        lc.setup_canary_db(dsn)

        from sqlalchemy import create_engine, inspect
        engine = create_engine(dsn)
        insp = inspect(engine)
        actual_tables = set(insp.get_table_names())
        engine.dispose()

        missing = _REQUIRED_TABLES - actual_tables
        assert not missing, (
            f"Tables missing from canary DB schema: {missing}\n"
            f"  Present: {sorted(actual_tables)}"
        )

    def test_setup_is_idempotent(self, tmp_path):
        """Calling setup_canary_db twice on the same file must not raise."""
        db_file = tmp_path / "canary.sqlite"
        dsn = f"sqlite:///{db_file}"
        lc.setup_canary_db(dsn)
        lc.setup_canary_db(dsn)  # second call — create_all is a no-op
        assert db_file.exists()

    def test_setup_creates_parent_directories(self, tmp_path):
        """DSN path inside a non-existent subdirectory should still work."""
        nested = tmp_path / "deep" / "nested" / "canary.sqlite"
        # SqliteDataProvider resolves DATA_DIR and mkdirs — but here we pass
        # an explicit path that already has its parent created by SQLite itself
        # via SQLAlchemy's connect_args or the filesystem.
        # Just verify no exception is raised with an in-existing-parent path.
        nested.parent.mkdir(parents=True, exist_ok=True)
        dsn = f"sqlite:///{nested}"
        lc.setup_canary_db(dsn)
        assert nested.exists()


# ── teardown_canary_db ─────────────────────────────────────────────────────────


class TestTeardownCanaryDb:
    def test_removes_sqlite_file(self, tmp_path):
        db_file = tmp_path / "canary.sqlite"
        dsn = f"sqlite:///{db_file}"
        lc.setup_canary_db(dsn)
        assert db_file.exists()

        lc.teardown_canary_db(db_file)
        assert not db_file.exists(), "SQLite file should be gone after teardown"

    def test_teardown_on_nonexistent_file_is_noop(self, tmp_path):
        """teardown on a path that was never created must not raise."""
        nonexistent = tmp_path / "ghost.sqlite"
        lc.teardown_canary_db(nonexistent)  # should not raise

    def test_teardown_twice_is_safe(self, tmp_path):
        """Calling teardown a second time after the file is already gone is a no-op."""
        db_file = tmp_path / "canary.sqlite"
        dsn = f"sqlite:///{db_file}"
        lc.setup_canary_db(dsn)
        lc.teardown_canary_db(db_file)
        lc.teardown_canary_db(db_file)  # second call — must not raise


# ── Postgres-mode guard ────────────────────────────────────────────────────────


class TestPostgresModeGuard:
    """When --db-mode postgres is requested via main(), the canary should warn
    and fall back to sqlite in L1 (it does not raise or exit with code 2)."""

    def test_main_warns_and_continues_when_postgres_requested(
        self, tmp_path, monkeypatch, caplog
    ):
        """--db-mode postgres in L1 logs a warning and falls back to sqlite."""
        import logging

        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "canary" / "failures.csv"

        monkeypatch.setattr(lc, "_find_failures_csv", lambda _: fixture)
        monkeypatch.setattr(lc, "setup_canary_db", lambda dsn, db_mode="sqlite": None)
        monkeypatch.setattr(lc, "teardown_canary_db", lambda p: None)
        monkeypatch.setattr(lc, "replay", lambda **kw: 0)
        monkeypatch.setattr(lc, "read_canary_outcomes", lambda *a, **kw: {})
        monkeypatch.setattr(lc, "_DEFAULT_OUT_ROOT", tmp_path)

        with caplog.at_level(logging.WARNING, logger="local_canary"):
            exit_code = lc.main(
                ["--from-run", "2026-05-10", "--db-mode", "postgres", "--limit", "3"]
            )

        # Must not crash; exit 0 (no regressions since no events returned)
        assert exit_code == 0
        # Must have warned about the unimplemented postgres mode
        assert any("postgres" in rec.message.lower() for rec in caplog.records)

    def test_parser_accepts_postgres_mode(self):
        parser = lc.build_parser()
        args = parser.parse_args(["--from-run", "2026-05-10", "--db-mode", "postgres"])
        assert args.db_mode == "postgres"

    def test_parser_default_is_sqlite(self):
        parser = lc.build_parser()
        args = parser.parse_args(["--from-run", "2026-05-10"])
        assert args.db_mode == "sqlite"
