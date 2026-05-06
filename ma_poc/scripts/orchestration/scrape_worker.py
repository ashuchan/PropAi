"""
scripts/orchestration/scrape_worker.py
=======================================
Per-property scrape helpers used by the daily runner thread pool.

Extracted from scripts/daily_runner.py (lines 327-427).
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

# Make sibling script modules importable regardless of invocation cwd.
_HERE = Path(__file__).resolve().parent.parent  # scripts/
_PROJECT_ROOT = _HERE.parent  # ma_poc/
for _p in (_HERE, _PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from entrata import scrape  # noqa: E402

log = logging.getLogger("daily_runner")


async def _scrape_one(
    url: str,
    proxy: str | None,
    timeout_s: int,
    profile: Any = None,
    expected_total_units: int | None = None,
    property_city: str | None = None,
) -> dict:
    """Run scrape() with a hard timeout so a stuck page can never hang the run."""
    try:
        return await asyncio.wait_for(
            scrape(
                url,
                proxy=proxy,
                profile=profile,
                expected_total_units=expected_total_units,
                property_city=property_city,
            ),
            timeout=timeout_s,
        )
    except TimeoutError:
        return {"errors": [f"scrape timeout after {timeout_s}s"], "base_url": url, "_timeout": True}


def _close_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Shutdown a worker event loop cleanly.

    Cancels any tasks still pending, waits for them to finish (best-effort),
    shuts down async generators, then closes the loop. This prevents
    ``RuntimeError: Event loop is closed`` noise from httpx's garbage-collected
    ``AsyncClient`` instances whose ``aclose()`` coroutines would otherwise be
    scheduled on an already-closed loop.
    """
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True),
            )
        loop.run_until_complete(loop.shutdown_asyncgens())
    except Exception:
        # Best-effort cleanup — never let drain errors mask the scrape result.
        pass
    finally:
        loop.close()


def _scrape_in_thread(
    url: str,
    proxy: str | None,
    timeout_s: int,
    property_id: str = "unknown",
    profile: Any = None,
    expected_total_units: int | None = None,
    property_city: str | None = None,
) -> dict:
    """
    Run a single scrape in its own thread with its own event loop.

    Each thread gets an independent asyncio event loop and Playwright
    instance, giving true OS-level parallelism instead of single-threaded
    async concurrency.

    ``property_id`` is injected into the result so LLM interaction records
    (Tier 6 / Tier 7) carry the canonical ID for cost accounting.
    ``profile`` is the ScrapeProfile used for tier-skip routing.
    """
    if not url:
        return {"errors": ["no URL"], "base_url": "", "_property_id": property_id, "_llm_interactions": []}
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            _scrape_one(
                url,
                proxy,
                timeout_s,
                profile=profile,
                expected_total_units=expected_total_units,
                property_city=property_city,
            ),
        )
        # Stamp the canonical property ID so entrata.py's LLM tiers can
        # reference it when building interaction records.
        if isinstance(result, dict):
            result["_property_id"] = property_id
        return result
    except Exception as e:
        return {
            "errors": [str(e)],
            "base_url": url,
            "_exception": e,
            "_property_id": property_id,
            "_llm_interactions": [],
        }
    finally:
        # Drain pending tasks (e.g., Playwright/httpx internal aclose coroutines)
        # before closing the loop. Without this, background ``AsyncClient.aclose()``
        # tasks fire after ``loop.close()`` and spam "Event loop is closed" errors.
        _close_event_loop(loop)
