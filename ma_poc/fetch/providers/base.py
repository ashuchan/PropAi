"""Provider protocol — interface implemented by every tier's fetch backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ma_poc.discovery.contracts import CrawlTask
    from ma_poc.fetch.contracts import FetchResult
    from ma_poc.models.scrape_profile import ScrapeProfile


@runtime_checkable
class FetchProvider(Protocol):
    """Interface implemented by every tier's fetch backend.

    Each provider runs the existing retry_policy INTERNALLY for transient
    errors at its own tier. It never escalates; that's the escalator's job.
    """

    tier_name: str  # for logging — "DIRECT", "DC_PROXY", etc.

    async def fetch(
        self,
        task: "CrawlTask",
        profile: "ScrapeProfile",
    ) -> "FetchResult":
        """Return a FetchResult. Never raises. fetch_tier_used must be set."""
        ...
