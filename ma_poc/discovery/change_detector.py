"""Change detector — decides RenderMode for a URL based on profile/frontier state.

Pure function. No I/O. Decision rules applied in order, first match wins.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from ..fetch.contracts import RenderMode

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChangeDecision:
    """Immutable decision about how to fetch a URL."""

    render_mode: RenderMode
    reason: str
    use_cond_headers: bool


def decide(
    profile_maturity: str | None,
    frontier_entry: dict[str, object] | None,
    sitemap_lastmod: datetime | None,
    days_since_full_render: int | None,
    force_full: bool = False,
) -> ChangeDecision:
    """Decide the RenderMode for a property URL.

    Decision rules (first match wins):
    1. force_full=True → RENDER
    2. days_since_full_render is None or > 7 → RENDER (stale)
    3. profile is HOT and days < 1 → GET (cheap probe, still captures body)
    4. sitemap_lastmod older than last scrape → GET
    5. profile is WARM and days < 3 → GET
    6. default → RENDER

    Why never HEAD: the L1 fetcher has no HEAD→GET escalation path. A HEAD
    response with status 200 returns ``body=None``, and every downstream
    adapter needs a body to extract from. Promoting a profile to HOT
    (three consecutive successes) used to flip its next run to HEAD and
    produce an immediate FAILED_NO_DATA — a self-perpetuating regression.
    GET gives us a cheap probe that still yields HTML for extraction.

    Args:
        profile_maturity: 'COLD', 'WARM', 'HOT', or None.
        frontier_entry: Frontier state dict for this URL.
        sitemap_lastmod: Last modification from sitemap.xml.
        days_since_full_render: Days since last full Playwright render.
        force_full: Whether to force a full render.

    Returns:
        ChangeDecision with render_mode, reason, and cond_headers flag.
    """
    if force_full:
        return ChangeDecision(RenderMode.RENDER, "manual_force", False)

    if days_since_full_render is None or days_since_full_render > 7:
        return ChangeDecision(RenderMode.RENDER, "stale_render_7d", False)

    if profile_maturity == "HOT" and days_since_full_render < 1:
        return ChangeDecision(RenderMode.GET, "hot_profile_fresh", True)

    if sitemap_lastmod is not None and frontier_entry is not None:
        last_attempted = frontier_entry.get("last_attempted")
        if last_attempted and isinstance(last_attempted, str):
            try:
                last_dt = datetime.fromisoformat(last_attempted)
                if sitemap_lastmod < last_dt:
                    return ChangeDecision(RenderMode.GET, "sitemap_unchanged", True)
            except ValueError:
                pass

    if profile_maturity == "WARM" and days_since_full_render is not None and days_since_full_render < 3:
        return ChangeDecision(RenderMode.GET, "warm_profile_static", True)

    return ChangeDecision(RenderMode.RENDER, "default_render", False)
