"""ResidentialProvider — Phase E4 stub.

Real implementation (BrightData residential with sticky sessions,
geo matching, and 0.5 RPS cap) is wired in Phase E4.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ma_poc.discovery.contracts import CrawlTask
    from ma_poc.fetch.contracts import FetchResult
    from ma_poc.models.scrape_profile import ScrapeProfile


class ResidentialProvider:
    """FetchProvider stub for the residential-proxy tier (Phase E4)."""

    tier_name: str = "RESIDENTIAL"

    async def fetch(
        self,
        task: "CrawlTask",
        profile: "ScrapeProfile",
    ) -> "FetchResult":
        raise NotImplementedError("ResidentialProvider is not implemented until Phase E4")
