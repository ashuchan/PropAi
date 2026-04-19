"""DualWriteDataProvider tests — writes land in both, reads come from primary,
secondary failures don't propagate."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from data_provider import (
    DualWriteDataProvider,
    FileSystemDataProvider,
    IssueEntry,
    RunReport,
    RunSummary,
    SqliteDataProvider,
)
from data_provider.contracts import IPropertyStateStore
from models.scrape_event import ScrapeEvent, ScrapeOutcome


@pytest.fixture
def primary(tmp_path: Path) -> FileSystemDataProvider:
    p = FileSystemDataProvider(
        base_dir=tmp_path / "data_primary",
        config_dir=tmp_path / "config_primary",
    )
    yield p
    p.close()


@pytest.fixture
def secondary(tmp_path: Path) -> SqliteDataProvider:
    s = SqliteDataProvider(url=f"sqlite:///{tmp_path / 'secondary.db'}")
    yield s
    s.close()


@pytest.fixture
def dual(primary, secondary) -> DualWriteDataProvider:
    return DualWriteDataProvider(primary=primary, secondary=secondary)


def test_writes_land_in_both(dual, primary, secondary) -> None:
    with dual.transaction():
        dual.property_state.upsert("D-1", {"name": "Dual"}, "2026-04-19")
    assert primary.property_state.exists("D-1") is True
    assert secondary.property_state.exists("D-1") is True


def test_reads_come_from_primary_only(dual, primary, secondary) -> None:
    # Write directly to secondary — dual's read should NOT see it.
    with secondary.transaction():
        secondary.property_state.upsert("SECRET", {"name": "x"}, "2026-04-19")
    assert dual.property_state.exists("SECRET") is False
    assert dual.property_state.get("SECRET") is None


def test_run_store_writes_propagate(dual, primary, secondary) -> None:
    dual.runs.write_properties("2026-04-19", [{"Property Name": "A"}])
    dual.runs.append_issue(
        "2026-04-19",
        IssueEntry(severity="INFO", code="TEST", message="hello"),
    )
    report = RunReport(
        run_date="2026-04-19",
        generated_at=datetime.now(UTC).isoformat(),
        totals=RunSummary(properties=1, succeeded=1),
    )
    dual.runs.write_report("2026-04-19", report)

    assert len(primary.runs.read_properties("2026-04-19")) == 1
    assert len(secondary.runs.read_properties("2026-04-19")) == 1
    assert primary.runs.read_report("2026-04-19") is not None
    assert secondary.runs.read_report("2026-04-19") is not None
    assert len(list(primary.runs.read_issues("2026-04-19"))) == 1
    assert len(list(secondary.runs.read_issues("2026-04-19"))) == 1


def test_scrape_events_and_profiles_dual_written(dual, primary, secondary) -> None:
    ev = ScrapeEvent(
        event_id=str(uuid.uuid4()),
        property_id="D-1",
        scrape_timestamp=datetime.now(UTC),
        scrape_outcome=ScrapeOutcome.SUCCESS,
    )
    dual.scrape_events.append(ev)
    assert len(list(primary.scrape_events.read_all())) == 1
    assert len(list(secondary.scrape_events.read_all())) == 1


class _BrokenStore(IPropertyStateStore):
    """Every method raises — used to verify secondary failures don't propagate."""

    def get(self, canonical_id): raise RuntimeError("boom")
    def exists(self, canonical_id): raise RuntimeError("boom")
    def upsert(self, canonical_id, snapshot, run_date): raise RuntimeError("boom")
    def all_canonical_ids(self): raise RuntimeError("boom")


def test_secondary_write_failure_does_not_break_primary(dual, primary) -> None:
    """Swap the secondary's property_state for a broken one mid-run."""
    dual.property_state._s = _BrokenStore()  # type: ignore[attr-defined]
    # Primary should succeed despite secondary raising.
    with dual.transaction():
        is_new = dual.property_state.upsert("RESILIENT", {"name": "Y"}, "2026-04-19")
    assert is_new is True
    assert primary.property_state.exists("RESILIENT") is True


def test_factory_builds_dual_provider(monkeypatch, tmp_path) -> None:
    from data_provider import get_data_provider, reset_cached_provider
    monkeypatch.setenv("DATA_PROVIDER", "dual")
    monkeypatch.setenv("DUAL_PRIMARY", "filesystem")
    monkeypatch.setenv("DUAL_SECONDARY", "sqlite")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'dual.db'}")
    reset_cached_provider()
    p = get_data_provider(force_new=True)
    assert p.name.startswith("dual(")
    assert "filesystem" in p.name
    assert "sqlite" in p.name
    p.close()
