"""UnlockerProvider — Phase E5 stub.

Real implementation (Zyte API / BrightData Web Unlocker) is wired in Phase E5.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ma_poc.discovery.contracts import CrawlTask
    from ma_poc.fetch.contracts import FetchResult
    from ma_poc.models.scrape_profile import ScrapeProfile


class UnlockerProvider:
    """FetchProvider stub for the web-unlocker tier (Phase E5)."""

    tier_name: str = "UNLOCKER"

    async def fetch(
        self,
        task: "CrawlTask",
        profile: "ScrapeProfile",
    ) -> "FetchResult":
        raise NotImplementedError("UnlockerProvider is not implemented until Phase E5")
