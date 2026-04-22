"""Scheduler — assembles CrawlTasks from CSV rows, profiles, DLQ, and frontier.

Yields tasks in priority order:
1. DLQ_REVIVE (due for retry)
2. RENDER (cold/warm profiles, new properties)
3. HEAD/GET (hot profiles, recent renders)
4. SITEMAP_DISCOVERED
"""

from __future__ import annotations

import logging
import random
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse

from ..fetch.contracts import RenderMode
from .change_detector import ChangeDecision, decide
from .contracts import CrawlTask, TaskReason
from .dlq import Dlq
from .frontier import Frontier
from .sitemap import SitemapConsumer

log = logging.getLogger(__name__)


class Scheduler:
    """Assembles CrawlTasks from inputs in priority order.

    Args:
        frontier: Persistent URL frontier.
        dlq: Dead-letter queue.
        sitemap: Sitemap consumer.
        profile_store: Profile store (has get_profile method).
        change_detector_fn: Change detection decision function.
    """

    def __init__(
        self,
        frontier: Frontier,
        dlq: Dlq,
        sitemap: SitemapConsumer,
        profile_store: Any,
        change_detector_fn: Any = None,
    ) -> None:
        self._frontier = frontier
        self._dlq = dlq
        self._sitemap = sitemap
        self._profile_store = profile_store
        self._change_fn = change_detector_fn or decide

    async def build_tasks(
        self,
        csv_rows: list[dict[str, Any]],
        run_date: date | None = None,
    ) -> AsyncIterator[CrawlTask]:
        """Build and yield CrawlTasks in priority order.

        Args:
            csv_rows: Property records from CSV.
            run_date: Date of this run (default: today).

        Yields:
            CrawlTask objects in priority order.
        """
        if run_date is None:
            run_date = date.today()

        # Bucket tasks by priority
        dlq_tasks: list[CrawlTask] = []
        render_tasks: list[CrawlTask] = []
        light_tasks: list[CrawlTask] = []

        # 1. DLQ revive tasks
        now = datetime.now(UTC)
        for dlq_entry in self._dlq.due_for_retry(now):
            # Find URL from frontier
            urls = self._frontier.property_urls(dlq_entry.property_id)
            if urls:
                url = urls[0]["url"]
                dlq_tasks.append(
                    CrawlTask(
                        url=str(url),
                        property_id=dlq_entry.property_id,
                        priority=0,
                        budget_ms=180_000,
                        reason=TaskReason.DLQ_REVIVE,
                        render_mode=RenderMode.RENDER,
                    )
                )

        # 2. Scheduled tasks from CSV
        for row in csv_rows:
            pid = str(row.get("property_id", row.get("Property ID", row.get("Unique ID", ""))))
            url = str(row.get("url", row.get("Website", "")))
            if not url or not pid:
                continue
            url = _normalize_url(url)
            if not url:
                continue

            # Skip parked properties
            if self._dlq.is_parked(pid):
                from ..observability.events import EventKind, emit

                emit(EventKind.TASK_SKIPPED_DLQ, pid, url=url)
                continue

            # Register in frontier
            self._frontier.upsert_url(url, pid, depth=0, source="csv")

            # Get profile maturity
            profile_maturity = self._get_profile_maturity(pid)

            # Get frontier entry for change detection
            frontier_entry = self._frontier.get_entry(url)

            # Compute days since last render
            days_since = self._days_since_render(frontier_entry)

            # Change detection decision
            decision: ChangeDecision = self._change_fn(
                profile_maturity=profile_maturity,
                frontier_entry=frontier_entry,
                sitemap_lastmod=None,  # sitemap checked separately
                days_since_full_render=days_since,
            )

            # Get cached etag/last_modified
            etag = None
            last_modified = None
            if decision.use_cond_headers and frontier_entry:
                # Would come from the cond cache in practice
                pass

            task = CrawlTask(
                url=url,
                property_id=pid,
                priority=1 if decision.render_mode == RenderMode.RENDER else 2,
                budget_ms=180_000,
                reason=TaskReason.SCHEDULED,
                render_mode=decision.render_mode,
                expected_pms=self._get_expected_pms(pid),
                etag=etag,
                last_modified=last_modified,
            )

            if decision.render_mode == RenderMode.RENDER:
                render_tasks.append(task)
            else:
                light_tasks.append(task)

        # Shuffle within priority buckets by host
        _shuffle_by_host(dlq_tasks)
        _shuffle_by_host(render_tasks)
        _shuffle_by_host(light_tasks)

        # Yield in priority order
        for task in dlq_tasks:
            yield task
        for task in render_tasks:
            yield task
        for task in light_tasks:
            yield task

    def _get_profile_maturity(self, property_id: str) -> str | None:
        """Get the profile maturity for a property.

        Args:
            property_id: The canonical property ID.

        Returns:
            Maturity string ('COLD', 'WARM', 'HOT') or None.
        """
        try:
            if hasattr(self._profile_store, "get_profile"):
                profile = self._profile_store.get_profile(property_id)
                if profile and hasattr(profile, "confidence"):
                    return getattr(profile.confidence, "maturity", None)
                if isinstance(profile, dict):
                    maturity = profile.get("confidence", {}).get("maturity")
                    return str(maturity) if maturity is not None else None
        except Exception:
            pass
        return None

    def _get_expected_pms(self, property_id: str) -> str | None:
        """Get the expected PMS platform from the profile.

        Args:
            property_id: The canonical property ID.

        Returns:
            PMS name string or None.
        """
        try:
            if hasattr(self._profile_store, "get_profile"):
                profile = self._profile_store.get_profile(property_id)
                if profile and hasattr(profile, "api_hints"):
                    return getattr(profile.api_hints, "api_provider", None)
                if isinstance(profile, dict):
                    provider = profile.get("api_hints", {}).get("api_provider")
                    return str(provider) if provider is not None else None
        except Exception:
            pass
        return None

    def _days_since_render(self, frontier_entry: dict[str, object] | None) -> int | None:
        """Calculate days since last successful render.

        Args:
            frontier_entry: Frontier entry dict.

        Returns:
            Days since last render, or None if never rendered.
        """
        if frontier_entry is None:
            return None
        last = frontier_entry.get("last_attempted")
        if not last or not isinstance(last, str):
            return None
        try:
            last_dt = datetime.fromisoformat(last)
            now = datetime.now(UTC)
            return (now - last_dt).days
        except ValueError:
            return None


def _normalize_url(url: str) -> str:
    """Ensure a fetchable URL. Returns "" if the input is unusable.

    Why: 12 properties in the 2026-04-19 run failed in ~2s with a generic
    "Error" outcome because the CSV listed the site as a bare domain
    ("www.foo.com") and Playwright rejected the navigation. Prepending a
    scheme recovers these fetches. Prefers https:// since production sites
    almost always support it — plain http:// triggers HSTS redirect stalls.
    """
    s = (url or "").strip()
    if not s:
        return ""
    if s.startswith(("http://", "https://")):
        return s
    # Strip any leading "//" some CSVs emit, then prepend https.
    s = s.lstrip("/")
    return "https://" + s


def _shuffle_by_host(tasks: list[CrawlTask]) -> None:
    """Shuffle tasks within the list, grouping by host to avoid hammering.

    Args:
        tasks: List of CrawlTasks to shuffle in-place.
    """
    if not tasks:
        return
    by_host: defaultdict[str, list[CrawlTask]] = defaultdict(list)
    for t in tasks:
        host = urlparse(t.url).netloc
        by_host[host].append(t)
    # Round-robin across hosts
    hosts = list(by_host.keys())
    random.shuffle(hosts)
    tasks.clear()
    while any(by_host[h] for h in hosts):
        for h in hosts:
            if by_host[h]:
                tasks.append(by_host[h].pop(0))
