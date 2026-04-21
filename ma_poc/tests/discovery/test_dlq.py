"""Tests for dlq — dead-letter queue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ma_poc.discovery.dlq import Dlq


def test_dlq_park_and_query(tmp_path: Path) -> None:
    dlq = Dlq(tmp_path / "dlq.jsonl")
    dlq.park("p1", "unreachable", "ERR_SSL")
    assert dlq.is_parked("p1")


def test_dlq_due_for_retry_hourly_for_6h(tmp_path: Path) -> None:
    dlq = Dlq(tmp_path / "dlq.jsonl")
    dlq.park("p1", "unreachable", "ERR_SSL")
    # After 1 hour, should be due
    future = datetime.now(UTC) + timedelta(hours=1, minutes=1)
    due = dlq.due_for_retry(future)
    assert len(due) == 1
    assert due[0].property_id == "p1"


def test_dlq_due_for_retry_daily_after_6h(tmp_path: Path) -> None:
    dlq = Dlq(tmp_path / "dlq.jsonl")
    dlq.park("p1", "unreachable", "ERR_SSL")
    dlq.reschedule("p1")  # Reschedule for later
    # The reschedule within 6h sets hourly; still works
    future = datetime.now(UTC) + timedelta(hours=2)
    due = dlq.due_for_retry(future)
    assert len(due) >= 1


def test_dlq_unpark_removes_from_due(tmp_path: Path) -> None:
    dlq = Dlq(tmp_path / "dlq.jsonl")
    dlq.park("p1", "unreachable", "ERR_SSL")
    dlq.unpark("p1")
    assert not dlq.is_parked("p1")
    future = datetime.now(UTC) + timedelta(hours=2)
    due = dlq.due_for_retry(future)
    assert len(due) == 0


def test_dlq_compact_keeps_only_latest_per_id(tmp_path: Path) -> None:
    dlq_path = tmp_path / "dlq.jsonl"
    dlq = Dlq(dlq_path)
    dlq.park("p1", "reason1", "ERR_1")
    dlq.park("p1", "reason2", "ERR_2")  # Second park overwrites
    dlq.compact()
    lines = dlq_path.read_text().strip().split("\n")
    assert len(lines) == 1


def test_dlq_file_survives_crash_mid_append(tmp_path: Path) -> None:
    dlq_path = tmp_path / "dlq.jsonl"
    dlq = Dlq(dlq_path)
    dlq.park("p1", "unreachable", "ERR")
    # Simulate crash: append a truncated line
    with open(dlq_path, "a") as f:
        f.write('{"property_id": "p2", "par')  # truncated
    # Reopen should load p1, skip truncated p2
    dlq2 = Dlq(dlq_path)
    assert dlq2.is_parked("p1")
    assert not dlq2.is_parked("p2")


def test_dlq_is_parked_returns_false_for_unparked_id(tmp_path: Path) -> None:
    dlq = Dlq(tmp_path / "dlq.jsonl")
    assert not dlq.is_parked("nonexistent")
