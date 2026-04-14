"""
Profile-guided routing — determines extraction strategy from profile maturity.

HOT profiles skip directly to the known-good tier.
WARM profiles try the preferred tier first, then cascade.
COLD profiles run the full cascade.

Phase: claude-scrapper-arch.md Step 4.1
"""
from __future__ import annotations

from typing import Optional

from models.scrape_profile import ProfileMaturity, ScrapeProfile


class RouteDecision:
    """Extraction strategy decision based on profile maturity."""

    def __init__(
        self,
        skip_to_tier: Optional[int] = None,
        run_full_cascade: bool = True,
        custom_timeout_ms: Optional[int] = None,
        entry_url: Optional[str] = None,
        block_domains: list[str] | None = None,
    ) -> None:
        self.skip_to_tier = skip_to_tier
        self.run_full_cascade = run_full_cascade
        self.custom_timeout_ms = custom_timeout_ms
        self.entry_url = entry_url
        self.block_domains = block_domains or []


def route(profile: ScrapeProfile) -> RouteDecision:
    """Determine extraction strategy from profile maturity."""
    if profile.confidence.maturity == ProfileMaturity.HOT:
        return RouteDecision(
            skip_to_tier=profile.confidence.preferred_tier,
            run_full_cascade=False,
            custom_timeout_ms=profile.navigation.timeout_ms,
            entry_url=profile.navigation.entry_url,
            block_domains=profile.navigation.block_resource_domains,
        )

    if profile.confidence.maturity == ProfileMaturity.WARM:
        return RouteDecision(
            skip_to_tier=profile.confidence.preferred_tier,
            run_full_cascade=True,
            custom_timeout_ms=profile.navigation.timeout_ms,
            entry_url=profile.navigation.entry_url,
            block_domains=profile.navigation.block_resource_domains,
        )

    # COLD: full cascade, no shortcuts
    return RouteDecision(
        run_full_cascade=True,
        custom_timeout_ms=(
            profile.navigation.timeout_ms
            if profile.navigation.timeout_ms != 60000
            else None
        ),
        block_domains=profile.navigation.block_resource_domains,
    )
