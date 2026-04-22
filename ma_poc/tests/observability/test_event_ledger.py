"""Tests for event_ledger — append-only JSONL writer."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from ma_poc.observability.event_ledger import EventLedger
from ma_poc.observability.events import Event, EventKind


def _make_event(pid: str = "p1") -> Event:
    return Event(kind=EventKind.FETCH_STARTED, property_id=pid, run_id="run1")


def test_ledger_appends_event_in_jsonl_format(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "events.jsonl", "run1", buffer_size=1)
    ledger.append(_make_event())
    ledger.close()
    lines = (tmp_path / "events.jsonl").read_text().strip().split("\n")
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["kind"] == "fetch.started"


def test_ledger_buffer_flushes_on_size(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "events.jsonl", "run1", buffer_size=4)
    for i in range(4):
        ledger.append(_make_event(f"p{i}"))
    # Buffer should have flushed
    lines = (tmp_path / "events.jsonl").read_text().strip().split("\n")
    assert len(lines) == 4
    ledger.close()


def test_ledger_buffer_flushes_on_close(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "events.jsonl", "run1", buffer_size=100)
    ledger.append(_make_event())
    ledger.append(_make_event())
    # Not flushed yet (buffer_size=100)
    ledger.close()
    lines = (tmp_path / "events.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2


def test_ledger_appends_from_multiple_threads(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "events.jsonl", "run1", buffer_size=8)

    def writer(start: int) -> None:
        for i in range(250):
            ledger.append(_make_event(f"t{start}_{i}"))

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ledger.close()

    lines = (tmp_path / "events.jsonl").read_text().strip().split("\n")
    assert len(lines) == 1000


def test_ledger_prepends_run_id_to_every_event(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "events.jsonl", "run_abc", buffer_size=1)
    event = Event(kind=EventKind.FETCH_STARTED, property_id="p1", run_id="run_abc")
    ledger.append(event)
    ledger.close()
    data = json.loads((tmp_path / "events.jsonl").read_text().strip())
    assert data["run_id"] == "run_abc"


def test_ledger_truncated_mid_line_can_be_reopened(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    ledger = EventLedger(path, "run1", buffer_size=1)
    ledger.append(_make_event())
    ledger.close()
    # Append a truncated line (simulate crash)
    with open(path, "a") as f:
        f.write('{"kind": "fetch.sta')
    # Reopen and read should skip the truncated line
    events = EventLedger(path, "run2", buffer_size=1).read_all()
    assert len(events) == 1
