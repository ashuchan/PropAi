"""L2 — Discovery & Scheduling layer.

Public API:
  - build_tasks_for_run: Scheduler that yields CrawlTasks
  - record_task_outcome: Post-scrape outcome handler
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .contracts import CrawlTask, TaskReason
from .scheduler import Scheduler

__all__ = ["CrawlTask", "TaskReason", "build_tasks_for_run", "record_task_outcome"]


async def build_tasks_for_run(
    csv_rows: list[dict[str, Any]],
    scheduler: Scheduler,
) -> AsyncIterator[CrawlTask]:
    """Build CrawlTasks for a daily run.

    Args:
        csv_rows: Property records from CSV.
        scheduler: Configured Scheduler instance.

    Yields:
        CrawlTask objects in priority order.
    """
    async for task in scheduler.build_tasks(csv_rows):
        yield task


def record_task_outcome(
    task: CrawlTask,
    fetch_outcome: str,
    frontier: Any,
    dlq: Any,
    consecutive_failures: int = 0,
) -> None:
    """Record the outcome of a completed task.

    Updates frontier and DLQ based on the outcome.

    Args:
        task: The completed CrawlTask.
        fetch_outcome: The FetchOutcome value string.
        frontier: The Frontier instance.
        dlq: The DLQ instance.
        consecutive_failures: Number of consecutive fetch failures.
    """
    from ..fetch.contracts import FetchOutcome

    outcome = FetchOutcome(fetch_outcome)
    frontier.mark_attempt(task.url, outcome)

    if outcome in (FetchOutcome.HARD_FAIL, FetchOutcome.BOT_BLOCKED, FetchOutcome.PROXY_ERROR):
        if consecutive_failures >= 3:
            dlq.park(task.property_id, "consecutive_unreachable", fetch_outcome)
            frontier.park(task.property_id)
    elif outcome in (FetchOutcome.OK, FetchOutcome.NOT_MODIFIED):
        if dlq.is_parked(task.property_id):
            dlq.unpark(task.property_id)
            frontier.unpark(task.property_id)
