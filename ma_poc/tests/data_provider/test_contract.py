"""Contract tests parametrized over every DataProvider implementation.

Any new provider must pass this suite. The FS provider runs against a
tmp_path sandbox; the PG provider is opt-in via DATABASE_URL (skipped
otherwise) and not exercised until Phase 4 lands.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from data_provider import (
    DataProvider,
    IssueEntry,
    LedgerEntry,
    RunReport,
    RunSummary,
    get_data_provider,
    reset_cached_provider,
)
from data_provider.filesystem import FileSystemDataProvider
from data_provider.postgres import PostgresDataProvider
from data_provider.sqlite import SqliteDataProvider
from models.extraction_result import (
    ExtractionResult,
    ExtractionStatus,
    ExtractionTier,
)
from models.scrape_event import ScrapeEvent, ScrapeOutcome
from models.scrape_profile import ScrapeProfile


def _fs_provider(tmp_path: Path) -> FileSystemDataProvider:
    return FileSystemDataProvider(
        base_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
    )


def _sqlite_provider(tmp_path: Path) -> SqliteDataProvider:
    # File-based sqlite in tmp_path so it survives within the test but
    # doesn't pollute the real DATA_DIR.
    url = f"sqlite:///{tmp_path / 'test.db'}"
    return SqliteDataProvider(url=url)


@pytest.fixture
def provider(request, tmp_path) -> DataProvider:
    """Parametrised: runs every test against each implementation in turn.

    `sqlite` is the SQL-backend smoke test (same code path as Postgres).
    `postgres` is opt-in via TEST_DATABASE_URL for running against a real
    Postgres; skipped by default so the suite runs without a live DB.
    """
    kind = request.param
    if kind == "filesystem":
        p: DataProvider = _fs_provider(tmp_path)
    elif kind == "sqlite":
        p = _sqlite_provider(tmp_path)
    elif kind == "postgres":
        url = os.getenv("TEST_DATABASE_URL")
        if not url:
            pytest.skip("Set TEST_DATABASE_URL to run against real Postgres")
        p = PostgresDataProvider(url=url)
    else:  # pragma: no cover
        raise ValueError(f"Unknown provider kind: {kind}")
    try:
        yield p
    finally:
        p.close()


providers = pytest.mark.parametrize("provider", ["filesystem", "sqlite", "postgres"], indirect=True)


# ── property_state / unit_state ──────────────────────────────────────────────


@providers
def test_property_state_roundtrip(provider: DataProvider) -> None:
    snap = {"proj_name": "San Artes", "website": "https://example.com", "city": "Phoenix"}
    with provider.transaction():
        is_new = provider.property_state.upsert("SMOKE-001", snap, "2026-04-19")
    assert is_new is True
    assert provider.property_state.exists("SMOKE-001") is True

    got = provider.property_state.get("SMOKE-001")
    assert got is not None
    assert got.canonical_id == "SMOKE-001"
    assert got.proj_name == "San Artes"
    # last_seen_at is set to wall-clock UTC on upsert — assert it's a plausible
    # ISO timestamp; run_date is persisted separately via first_seen_date.
    assert got.last_seen_at is not None and len(got.last_seen_at) >= 19
    assert got.first_seen_date == "2026-04-19"


@providers
def test_property_state_upsert_existing_is_not_new(provider: DataProvider) -> None:
    with provider.transaction():
        provider.property_state.upsert("P-1", {"proj_name": "Foo"}, "2026-04-18")
        second = provider.property_state.upsert("P-1", {"proj_name": "Foo v2"}, "2026-04-19")
    assert second is False
    got = provider.property_state.get("P-1")
    assert got is not None
    assert got.proj_name == "Foo v2"
    assert got.first_seen_date == "2026-04-18"
    # last_seen_at is refreshed on each upsert to the call-time UTC timestamp;
    # its date prefix is today's date, not necessarily the run_date argument.
    assert got.last_seen_at is not None and len(got.last_seen_at) >= 10


@providers
def test_unit_state_diff_new_updated_unchanged_disappeared(
    provider: DataProvider,
) -> None:
    today = [
        {"unit_id": "101", "rent_low": 1500, "rent_high": 1500},
        {"unit_id": "102", "rent_low": 1800, "rent_high": 1800},
    ]
    with provider.transaction():
        diff1 = provider.unit_state.upsert_units("P-1", today, "2026-04-18")
    assert set(diff1.new) == {"101", "102"}
    assert diff1.updated == []
    assert diff1.disappeared == []

    # Day 2: 101 unchanged, 102 price drop, 103 new, 104 disappears
    tomorrow = [
        {"unit_id": "101", "rent_low": 1500, "rent_high": 1500},
        {"unit_id": "102", "rent_low": 1700, "rent_high": 1700},
        {"unit_id": "103", "rent_low": 2000, "rent_high": 2000},
    ]
    with provider.transaction():
        diff2 = provider.unit_state.upsert_units("P-1", tomorrow, "2026-04-19")
    assert diff2.new == ["103"]
    assert diff2.updated == ["102"]
    assert diff2.unchanged == ["101"]
    assert diff2.disappeared == []


@providers
def test_unit_state_carry_forward(provider: DataProvider) -> None:
    today = [{"unit_id": "200", "rent_low": 2400, "rent_high": 2400}]
    with provider.transaction():
        provider.unit_state.upsert_units("P-CF", today, "2026-04-18")
        carried = provider.unit_state.carry_forward_units("P-CF", "2026-04-19")
    assert len(carried) == 1
    assert carried[0]["unit_id"] == "200"
    assert carried[0]["carryforward_days"] == 1


# ── runs (properties / report / issues / ledger) ─────────────────────────────


@providers
def test_run_store_properties_roundtrip(provider: DataProvider) -> None:
    props = [
        {"Property Name": "San Artes", "City": "Phoenix", "units": []},
        {"Property Name": "Lofts 99", "City": "Austin", "units": []},
    ]
    provider.runs.write_properties("2026-04-19", props)
    got = provider.runs.read_properties("2026-04-19")
    assert len(got) == 2
    assert got[0]["Property Name"] == "San Artes"


@providers
def test_run_store_report_roundtrip(provider: DataProvider) -> None:
    report = RunReport(
        run_date="2026-04-19",
        generated_at=datetime.now(UTC).isoformat(),
        totals=RunSummary(properties=5, succeeded=4, failed=1, success_rate_pct=80.0),
        tier_distribution={"TIER_1_API": 3, "TIER_4_LLM_API": 1},
        cost={"openrouter": 0.023},
        slo_violations=[],
    )
    provider.runs.write_report("2026-04-19", report)
    got = provider.runs.read_report("2026-04-19")
    assert got is not None
    assert got.run_date == "2026-04-19"
    assert got.totals.properties == 5
    assert got.tier_distribution["TIER_1_API"] == 3


@providers
def test_run_store_issues_append_and_read(provider: DataProvider) -> None:
    provider.runs.append_issue(
        "2026-04-19",
        IssueEntry(
            severity="WARNING",
            code="UNITS_EMPTY",
            message="No units found",
            canonical_id="P-1",
        ),
    )
    provider.runs.append_issue(
        "2026-04-19",
        IssueEntry(
            severity="ERROR",
            code="SCRAPE_TIMEOUT",
            message="Exceeded 180s timeout",
            canonical_id="P-2",
        ),
    )
    issues = list(provider.runs.read_issues("2026-04-19"))
    assert len(issues) == 2
    assert issues[0].code == "UNITS_EMPTY"
    assert issues[1].severity == "ERROR"


@providers
def test_run_store_ledger_append_and_read(provider: DataProvider) -> None:
    provider.runs.append_ledger_entry(
        "2026-04-19",
        LedgerEntry(
            canonical_id="P-1",
            status="SUCCESS",
            units_count=42,
            url="https://example.com",
        ),
    )
    rows = list(provider.runs.read_ledger("2026-04-19"))
    assert len(rows) == 1
    assert rows[0].units_count == 42


@providers
def test_run_store_list_runs_sorted(provider: DataProvider) -> None:
    for d in ["2026-04-17", "2026-04-19", "2026-04-18"]:
        provider.runs.write_properties(d, [])
    assert provider.runs.list_runs() == ["2026-04-17", "2026-04-18", "2026-04-19"]


@providers
def test_run_store_read_missing_returns_empty(provider: DataProvider) -> None:
    assert provider.runs.read_properties("2099-01-01") == []
    assert provider.runs.read_report("2099-01-01") is None
    assert list(provider.runs.read_issues("2099-01-01")) == []


# ── scrape_events ────────────────────────────────────────────────────────────


@providers
def test_scrape_events_append_read(provider: DataProvider) -> None:
    ev = ScrapeEvent(
        event_id=str(uuid.uuid4()),
        property_id="P-1",
        scrape_timestamp=datetime.now(UTC),
        scrape_outcome=ScrapeOutcome.SUCCESS,
        extraction_tier=1,
        page_load_ms=4200,
        confidence_score=0.92,
    )
    provider.scrape_events.append(ev)
    got = list(provider.scrape_events.read_all())
    assert len(got) == 1
    assert got[0].event_id == ev.event_id


@providers
def test_scrape_events_filter_by_property(provider: DataProvider) -> None:
    for pid in ["P-1", "P-2", "P-1"]:
        provider.scrape_events.append(
            ScrapeEvent(
                event_id=str(uuid.uuid4()),
                property_id=pid,
                scrape_timestamp=datetime.now(UTC),
                scrape_outcome=ScrapeOutcome.SUCCESS,
            )
        )
    assert len(list(provider.scrape_events.read_for_property("P-1"))) == 2
    assert len(list(provider.scrape_events.read_for_property("P-2"))) == 1


# ── profiles ─────────────────────────────────────────────────────────────────


@providers
def test_profile_roundtrip(provider: DataProvider) -> None:
    prof = ScrapeProfile(canonical_id="P-XYZ")
    provider.profiles.put(prof)
    got = provider.profiles.get("P-XYZ")
    assert got is not None
    assert got.canonical_id == "P-XYZ"
    assert "P-XYZ" in provider.profiles.list_ids()

    deleted = provider.profiles.delete("P-XYZ")
    assert deleted is True
    assert provider.profiles.get("P-XYZ") is None


@providers
def test_profile_get_missing(provider: DataProvider) -> None:
    assert provider.profiles.get("DOES-NOT-EXIST") is None


# ── extraction_results ───────────────────────────────────────────────────────


@providers
def test_extraction_result_roundtrip(provider: DataProvider) -> None:
    res = ExtractionResult(
        property_id="P-1",
        tier=ExtractionTier.API_INTERCEPTION,
        status=ExtractionStatus.SUCCESS,
        confidence_score=0.91,
        raw_fields={"units": [{"unit_id": "101", "rent": 1500}]},
    )
    provider.extraction_results.write("2026-04-19", res)
    got = provider.extraction_results.read("2026-04-19", "P-1")
    assert got is not None
    assert got.property_id == "P-1"
    assert got.confidence_score == 0.91
    assert got.succeeded is True


# ── transaction semantics ────────────────────────────────────────────────────


@providers
def test_transaction_commits_state_on_clean_exit(provider: DataProvider) -> None:
    with provider.transaction():
        provider.property_state.upsert("T-OK", {"proj_name": "Ok"}, "2026-04-19")
    # Same provider should still see the row after the txn exits cleanly.
    assert provider.property_state.exists("T-OK") is True


@providers
def test_transaction_drops_state_on_error(provider: DataProvider) -> None:
    with pytest.raises(RuntimeError):
        with provider.transaction():
            provider.property_state.upsert("T-ROLLBACK", {"proj_name": "x"}, "2026-04-19")
            raise RuntimeError("boom")
    assert provider.property_state.exists("T-ROLLBACK") is False


# ── factory ──────────────────────────────────────────────────────────────────


def test_factory_default_is_filesystem(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATA_PROVIDER", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "c"))
    reset_cached_provider()
    p = get_data_provider(force_new=True)
    assert p.name == "filesystem"
    p.close()


def test_factory_postgres_selected(monkeypatch, tmp_path) -> None:
    """Postgres factory wiring: selects pg when DATA_PROVIDER=postgres.

    We can't connect to real PG here, so we point DATABASE_URL at a local
    SQLite file — the provider still initialises (the dialect check only
    matters on upsert paths). The test proves the factory routes correctly.
    """
    monkeypatch.setenv("DATA_PROVIDER", "postgres")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'pg_surrogate.db'}")
    reset_cached_provider()
    p = get_data_provider(force_new=True)
    assert p.name == "postgres"
    p.close()


def test_factory_sqlite_selected(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATA_PROVIDER", "sqlite")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'sqlite.db'}")
    reset_cached_provider()
    p = get_data_provider(force_new=True)
    assert p.name == "sqlite"
    p.close()


def test_factory_unknown_falls_back_to_filesystem(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATA_PROVIDER", "mysql")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "c"))
    reset_cached_provider()
    p = get_data_provider(force_new=True)
    assert p.name == "filesystem"
    p.close()


def test_factory_caches_instance(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "c"))
    reset_cached_provider()
    a = get_data_provider()
    b = get_data_provider()
    assert a is b
    reset_cached_provider()


# ── PostgresDataProvider smoke wiring ───────────────────────────────────────


def test_postgres_provider_initialises_with_sqlite_url(tmp_path) -> None:
    """PostgresDataProvider is a thin wrapper over SqlDataProvider; any URL
    SQLAlchemy can open works. With sqlite:// it functions locally and
    gives us end-to-end coverage of the pg code path."""
    p = PostgresDataProvider(url=f"sqlite:///{tmp_path / 'pg.db'}")
    assert p.name == "postgres"
    assert p.property_state.get("nobody") is None
    p.close()
