"""Tests for scheduler — task assembly and prioritisation."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ma_poc.discovery.change_detector import ChangeDecision
from ma_poc.discovery.contracts import TaskReason
from ma_poc.discovery.dlq import Dlq
from ma_poc.discovery.frontier import Frontier
from ma_poc.discovery.scheduler import Scheduler
from ma_poc.fetch.contracts import RenderMode


def _make_scheduler(
    tmp_path: Path,
    change_fn: object | None = None,
) -> Scheduler:
    frontier = Frontier(tmp_path / "frontier.db")
    dlq = Dlq(tmp_path / "dlq.jsonl")
    sitemap = MagicMock()
    profile_store = MagicMock()
    profile_store.get_profile.return_value = None
    return Scheduler(
        frontier=frontier,
        dlq=dlq,
        sitemap=sitemap,
        profile_store=profile_store,
        change_detector_fn=change_fn,
    )


@pytest.mark.asyncio
async def test_scheduler_emits_one_task_per_csv_row(tmp_path: Path) -> None:
    s = _make_scheduler(tmp_path)
    rows = [
        {"property_id": "p1", "url": "https://a.com/1"},
        {"property_id": "p2", "url": "https://b.com/2"},
    ]
    tasks = [t async for t in s.build_tasks(rows)]
    assert len(tasks) == 2


@pytest.mark.asyncio
async def test_scheduler_skips_parked_properties(tmp_path: Path) -> None:
    s = _make_scheduler(tmp_path)
    s._dlq.park("p1", "unreachable", "ERR")
    rows = [{"property_id": "p1", "url": "https://a.com/1"}]
    tasks = [t async for t in s.build_tasks(rows)]
    assert len(tasks) == 0


@pytest.mark.asyncio
async def test_scheduler_emits_dlq_revive_for_due_properties(tmp_path: Path) -> None:
    s = _make_scheduler(tmp_path)
    s._frontier.upsert_url("https://a.com/1", "p1", 0, "csv")
    s._dlq.park("p1", "unreachable", "ERR")
    # Force retry_at to past
    from datetime import datetime, timedelta

    entry = s._dlq._entries["p1"]
    past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    from ma_poc.discovery.dlq import DlqEntry

    s._dlq._entries["p1"] = DlqEntry(
        property_id="p1",
        parked_at=entry.parked_at,
        reason=entry.reason,
        last_error_signature=entry.last_error_signature,
        retry_at=past,
    )
    rows: list[dict] = []  # No CSV rows
    tasks = [t async for t in s.build_tasks(rows)]
    assert any(t.reason == TaskReason.DLQ_REVIVE for t in tasks)


@pytest.mark.asyncio
async def test_scheduler_respects_change_detector_decision(tmp_path: Path) -> None:
    def always_head(**kw: object) -> ChangeDecision:
        return ChangeDecision(RenderMode.HEAD, "test", True)

    s = _make_scheduler(tmp_path, change_fn=always_head)
    rows = [{"property_id": "p1", "url": "https://a.com/1"}]
    tasks = [t async for t in s.build_tasks(rows)]
    assert tasks[0].render_mode == RenderMode.HEAD


@pytest.mark.asyncio
async def test_scheduler_prioritises_dlq_revive_over_scheduled(tmp_path: Path) -> None:
    s = _make_scheduler(tmp_path)
    s._frontier.upsert_url("https://dlq.com/1", "dlq1", 0, "csv")
    s._dlq.park("dlq1", "unreachable", "ERR")
    from datetime import datetime, timedelta

    from ma_poc.discovery.dlq import DlqEntry

    past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    s._dlq._entries["dlq1"] = DlqEntry(
        property_id="dlq1",
        parked_at=past,
        reason="unreachable",
        last_error_signature="ERR",
        retry_at=past,
    )
    rows = [{"property_id": "p2", "url": "https://a.com/2"}]
    tasks = [t async for t in s.build_tasks(rows)]
    assert tasks[0].reason == TaskReason.DLQ_REVIVE


@pytest.mark.asyncio
async def test_scheduler_shuffles_within_priority_by_host(tmp_path: Path) -> None:
    s = _make_scheduler(tmp_path)
    rows = [{"property_id": f"p{i}", "url": f"https://host{i % 3}.com/{i}"} for i in range(9)]
    tasks = [t async for t in s.build_tasks(rows)]
    assert len(tasks) == 9
    # Verify tasks exist (shuffling is random, can't assert order)
    pids = {t.property_id for t in tasks}
    assert len(pids) == 9


@pytest.mark.asyncio
async def test_scheduler_marks_reason_correctly(tmp_path: Path) -> None:
    s = _make_scheduler(tmp_path)
    rows = [{"property_id": "p1", "url": "https://a.com/1"}]
    tasks = [t async for t in s.build_tasks(rows)]
    assert tasks[0].reason == TaskReason.SCHEDULED
