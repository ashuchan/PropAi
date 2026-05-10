"""Tests for local_canary.py — profile seeding phase (L2).

Covers:
- seed_profiles_from_prod seeds the expected rows into the canary DB
- Only property IDs present in the source are copied (ID scoping)
- Call is idempotent (second call does not duplicate rows)
- Empty property_ids list returns 0 without touching the DB
- Missing PROD_DATABASE_URL raises RuntimeError
- Source DB with no matching rows returns 0
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_TESTS_DIR = Path(__file__).resolve().parent.parent
_MA_POC_ROOT = _TESTS_DIR.parent
_REPO_ROOT = _MA_POC_ROOT.parent
for _p in (_REPO_ROOT, _MA_POC_ROOT, _MA_POC_ROOT / "scripts" / "diagnostics"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import local_canary as lc  # type: ignore[import-not-found]


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_profile_row(canonical_id: str) -> dict:
    return {
        "canonical_id": canonical_id,
        "version": 1,
        "schema_version": "v1",
        "created_at": datetime(2026, 1, 1),
        "updated_at": datetime(2026, 1, 2),
        "updated_by": "TEST",
        "payload": {"hint": "test"},
    }


def _bootstrap_db(dsn: str, rows: list[dict]) -> None:
    """Create schema and seed rows into a fresh SQLite DB."""
    lc.setup_canary_db(dsn)
    from data_provider.sql.models import ScrapeProfileRow

    engine = create_engine(dsn)
    with engine.begin() as conn:
        for row in rows:
            conn.execute(ScrapeProfileRow.__table__.insert().values(**row))
    engine.dispose()


def _count_profiles(dsn: str) -> int:
    from data_provider.sql.models import ScrapeProfileRow

    engine = create_engine(dsn)
    with engine.connect() as conn:
        count = conn.execute(
            select(ScrapeProfileRow)
        ).all()
    engine.dispose()
    return len(count)


def _profile_ids(dsn: str) -> set[str]:
    from data_provider.sql.models import ScrapeProfileRow

    engine = create_engine(dsn)
    with engine.connect() as conn:
        rows = conn.execute(select(ScrapeProfileRow)).mappings().all()
    engine.dispose()
    return {r["canonical_id"] for r in rows}


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestSeedProfilesFromProd:
    def test_seeds_matching_rows_into_canary(self, tmp_path):
        """Profiles for the requested IDs are copied to the canary DB."""
        src_db = tmp_path / "source.sqlite"
        dst_db = tmp_path / "canary.sqlite"
        src_dsn = f"sqlite:///{src_db}"
        dst_dsn = f"sqlite:///{dst_db}"

        _bootstrap_db(src_dsn, [_make_profile_row("P001"), _make_profile_row("P002")])
        lc.setup_canary_db(dst_dsn)

        seeded = lc.seed_profiles_from_prod(["P001"], dst_dsn, source_dsn=src_dsn)
        assert seeded == 1
        assert _profile_ids(dst_dsn) == {"P001"}

    def test_does_not_copy_unselected_ids(self, tmp_path):
        """Profiles not in the property_ids list are not copied."""
        src_db = tmp_path / "source.sqlite"
        dst_db = tmp_path / "canary.sqlite"
        src_dsn = f"sqlite:///{src_db}"
        dst_dsn = f"sqlite:///{dst_db}"

        _bootstrap_db(
            src_dsn,
            [_make_profile_row("P001"), _make_profile_row("P002"), _make_profile_row("P003")],
        )
        lc.setup_canary_db(dst_dsn)

        seeded = lc.seed_profiles_from_prod(["P001", "P003"], dst_dsn, source_dsn=src_dsn)
        assert seeded == 2
        assert _profile_ids(dst_dsn) == {"P001", "P003"}

    def test_idempotent_second_call(self, tmp_path):
        """Calling seed twice does not duplicate rows."""
        src_db = tmp_path / "source.sqlite"
        dst_db = tmp_path / "canary.sqlite"
        src_dsn = f"sqlite:///{src_db}"
        dst_dsn = f"sqlite:///{dst_db}"

        _bootstrap_db(src_dsn, [_make_profile_row("P001")])
        lc.setup_canary_db(dst_dsn)

        lc.seed_profiles_from_prod(["P001"], dst_dsn, source_dsn=src_dsn)
        lc.seed_profiles_from_prod(["P001"], dst_dsn, source_dsn=src_dsn)

        assert _count_profiles(dst_dsn) == 1

    def test_empty_ids_returns_zero(self, tmp_path):
        """Empty property_ids list returns 0 without touching the DB."""
        dst_db = tmp_path / "canary.sqlite"
        dst_dsn = f"sqlite:///{dst_db}"
        lc.setup_canary_db(dst_dsn)

        seeded = lc.seed_profiles_from_prod([], dst_dsn, source_dsn="sqlite:///nonexistent.db")
        assert seeded == 0

    def test_source_without_matching_rows_returns_zero(self, tmp_path):
        """Source DB exists but has no profiles for the requested IDs → 0."""
        src_db = tmp_path / "source.sqlite"
        dst_db = tmp_path / "canary.sqlite"
        src_dsn = f"sqlite:///{src_db}"
        dst_dsn = f"sqlite:///{dst_db}"

        # Source DB has profiles but not for the requested IDs.
        _bootstrap_db(src_dsn, [_make_profile_row("OTHER_001")])
        lc.setup_canary_db(dst_dsn)

        seeded = lc.seed_profiles_from_prod(["P001"], dst_dsn, source_dsn=src_dsn)
        assert seeded == 0
        assert _count_profiles(dst_dsn) == 0

    def test_missing_prod_database_url_raises(self, tmp_path, monkeypatch):
        """RuntimeError when no source DSN or PROD_DATABASE_URL is given."""
        monkeypatch.delenv("PROD_DATABASE_URL", raising=False)
        dst_db = tmp_path / "canary.sqlite"
        dst_dsn = f"sqlite:///{dst_db}"
        lc.setup_canary_db(dst_dsn)

        with pytest.raises(RuntimeError, match="PROD_DATABASE_URL"):
            lc.seed_profiles_from_prod(["P001"], dst_dsn)

    def test_seeds_multiple_rows_in_one_call(self, tmp_path):
        """All requested IDs (that exist) are copied in a single call."""
        src_db = tmp_path / "source.sqlite"
        dst_db = tmp_path / "canary.sqlite"
        src_dsn = f"sqlite:///{src_db}"
        dst_dsn = f"sqlite:///{dst_db}"

        _bootstrap_db(
            src_dsn,
            [_make_profile_row(f"P{i:03d}") for i in range(5)],
        )
        lc.setup_canary_db(dst_dsn)

        seeded = lc.seed_profiles_from_prod(
            ["P000", "P002", "P004"], dst_dsn, source_dsn=src_dsn
        )
        assert seeded == 3
        assert _profile_ids(dst_dsn) == {"P000", "P002", "P004"}

    def test_seed_from_prod_flag_in_main_triggers_seed(self, tmp_path, monkeypatch):
        """--seed-from-prod in main() calls seed_profiles_from_prod."""
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "canary" / "failures.csv"

        seeded_ids: list[list[str]] = []

        def fake_seed(ids, dsn, source_dsn=None):
            seeded_ids.append(ids)
            return len(ids)

        monkeypatch.setattr(lc, "_find_failures_csv", lambda _: fixture)
        monkeypatch.setattr(lc, "setup_canary_db", lambda dsn, db_mode="sqlite": None)
        monkeypatch.setattr(lc, "teardown_canary_db", lambda p: None)
        monkeypatch.setattr(lc, "seed_profiles_from_prod", fake_seed)
        monkeypatch.setattr(lc, "replay", lambda **kw: 0)
        monkeypatch.setattr(lc, "read_canary_outcomes", lambda *a, **kw: {})
        monkeypatch.setattr(lc, "_DEFAULT_OUT_ROOT", tmp_path)

        exit_code = lc.main(
            ["--from-run", "2026-05-10", "--seed-from-prod", "--limit", "3"]
        )

        assert exit_code == 0
        assert len(seeded_ids) == 1
        assert len(seeded_ids[0]) == 3  # fixture has 3 rows
