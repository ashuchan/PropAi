"""L1 — Fetch & Fleet layer.

Exposes one public function: fetch(task) -> FetchResult.
Everything else in this package is private implementation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .contracts import FetchOutcome, FetchResult, RenderMode

if TYPE_CHECKING:
    from ..discovery.contracts import CrawlTask

__all__ = ["FetchOutcome", "FetchResult", "RenderMode", "fetch"]


async def fetch(task: CrawlTask) -> FetchResult:
    """Top-level entry point. Delegates to the default Fetcher singleton."""
    from .fetcher import get_default_fetcher

    return await get_default_fetcher().fetch(task)
