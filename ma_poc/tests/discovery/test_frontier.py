"""Tests for frontier — persistent URL store."""

from __future__ import annotations

from pathlib import Path

from ma_poc.discovery.frontier import Frontier
from ma_poc.fetch.contracts import FetchOutcome


def test_frontier_upsert_idempotent(tmp_path: Path) -> None:
    f = Frontier(tmp_path / "f.db")
    f.upsert_url("https://a.com/1", "p1", 0, "csv")
    f.upsert_url("https://a.com/1", "p1", 0, "csv")
    urls = f.property_urls("p1")
    assert len(urls) == 1
    f.close()


def test_frontier_mark_attempt_increments_failures(tmp_path: Path) -> None:
    f = Frontier(tmp_path / "f.db")
    f.upsert_url("https://a.com/1", "p1", 0, "csv")
    f.mark_attempt("https://a.com/1", FetchOutcome.TRANSIENT)
    entry = f.get_entry("https://a.com/1")
    assert entry is not None
    assert entry["consecutive_failures"] == 1
    f.mark_attempt("https://a.com/1", FetchOutcome.TRANSIENT)
    entry = f.get_entry("https://a.com/1")
    assert entry is not None
    assert entry["consecutive_failures"] == 2
    f.close()


def test_frontier_success_resets_failures(tmp_path: Path) -> None:
    f = Frontier(tmp_path / "f.db")
    f.upsert_url("https://a.com/1", "p1", 0, "csv")
    f.mark_attempt("https://a.com/1", FetchOutcome.TRANSIENT)
    f.mark_attempt("https://a.com/1", FetchOutcome.OK)
    entry = f.get_entry("https://a.com/1")
    assert entry is not None
    assert entry["consecutive_failures"] == 0
    f.close()


def test_frontier_park_unpark_round_trip(tmp_path: Path) -> None:
    f = Frontier(tmp_path / "f.db")
    f.upsert_url("https://a.com/1", "p1", 0, "csv")
    f.park("p1")
    entry = f.get_entry("https://a.com/1")
    assert entry is not None
    assert entry["is_parked"] == 1
    f.unpark("p1")
    entry = f.get_entry("https://a.com/1")
    assert entry is not None
    assert entry["is_parked"] == 0
    f.close()


def test_frontier_by_host_groups_correctly(tmp_path: Path) -> None:
    f = Frontier(tmp_path / "f.db")
    f.upsert_url("https://a.com/1", "p1", 0, "csv")
    f.upsert_url("https://a.com/2", "p2", 0, "csv")
    f.upsert_url("https://b.com/1", "p3", 0, "csv")
    assert len(f.by_host("a.com")) == 2
    assert len(f.by_host("b.com")) == 1
    f.close()


def test_frontier_db_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    f = Frontier(db)
    f.upsert_url("https://a.com/1", "p1", 0, "csv")
    f.close()
    f2 = Frontier(db)
    urls = f2.property_urls("p1")
    assert len(urls) == 1
    f2.close()


def test_frontier_handles_unicode_urls(tmp_path: Path) -> None:
    f = Frontier(tmp_path / "f.db")
    url = "https://example.com/ünit-àpartments"
    f.upsert_url(url, "p1", 0, "csv")
    entry = f.get_entry(url)
    assert entry is not None
    assert entry["url"] == url
    f.close()


def test_frontier_no_sqlite_injection(tmp_path: Path) -> None:
    f = Frontier(tmp_path / "f.db")
    evil_url = "https://evil.com/'; DROP TABLE frontier; --"
    f.upsert_url(evil_url, "p1", 0, "csv")
    # Table should still exist and work
    f.upsert_url("https://safe.com", "p2", 0, "csv")
    assert len(f.property_urls("p2")) == 1
    f.close()
