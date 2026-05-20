"""L1 — Fetch & Fleet layer.

Exposes one public function: fetch(task, profile=None) -> FetchResult.
Everything else in this package is private implementation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .contracts import FetchOutcome, FetchResult, RenderMode

if TYPE_CHECKING:
    from ..discovery.contracts import CrawlTask
    from ..models.scrape_profile import ScrapeProfile

__all__ = ["FetchOutcome", "FetchResult", "RenderMode", "fetch"]


async def fetch(
    task: CrawlTask,
    profile: ScrapeProfile | None = None,
) -> FetchResult:
    """Top-level entry point. Delegates to the default Fetcher singleton.

    When *profile* is supplied AND ``ENABLE_TIER_ESCALATION`` is True,
    the inner Fetcher delegates to ``fetch_with_escalation`` which runs
    the full DIRECT → DC_PROXY → RESIDENTIAL → FLARESOLVERR → UNLOCKER
    ladder. Without a profile, the fetch is single-tier (DIRECT only)
    even if the env var is set — see cluster #4 (no_body_short_circuit)
    root-cause analysis 2026-05-20.

    Callers in scraping runners should pass the property's profile —
    bootstrap one (``profile_store.bootstrap(...)``) for first-run
    properties so Cloudflare-walled / bot-blocked sites get a chance
    at residential-proxy escalation rather than failing on a single
    DIRECT 403.
    """
    from .fetcher import get_default_fetcher

    return await get_default_fetcher().fetch(task, profile=profile)
