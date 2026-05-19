"""
Squarespace (no PMS) adapter.

Research log
------------
Web sources consulted:
  - https://www.squarespace.com/ — Squarespace website builder (accessed 2026-04-17)
  - Squarespace does not provide apartment management features
Real payloads inspected (from data/runs/*/raw_api/):
  - No Squarespace-specific API payloads with unit data found in captures
  - Squarespace sites in the dataset are marketing-only (no PMS backend)
Key findings:
  - Squarespace is a website builder, not a PMS. Properties using Squarespace
    typically have no structured unit data accessible via API
  - Strategy is syndication_only: unit data must come from an external source
    or manual entry, not from scraping the Squarespace site
  - The adapter returns empty units with an informative error, signaling to the
    orchestrator that no extraction is possible from this site type
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


class SquarespaceNoPmsAdapter:
    """Squarespace (no PMS) adapter. Returns empty — syndication_only strategy."""

    pms_name: str = "squarespace_nopms"
    _fingerprints: list[str] = ["squarespace.com", "static1.squarespace.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Squarespace shells are usually no-PMS — but a sizable minority
        embed an AppFolio listings widget one labelled-nav hop deep. Try
        that recovery before declaring syndication_only.
        """
        from ma_poc.pms.adapters._appfolio_embed import recover_appfolio_embed

        units = await recover_appfolio_embed(page, ctx)
        if units:
            from ma_poc.extraction.post_process import post_process

            pp = post_process(units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                return AdapterResult(
                    units=pp.admitted,
                    plan_summaries=pp.plan_summaries,
                    tier_used="TIER_1_DOM_APPFOLIO_SSR",
                    confidence=min(0.95, 0.7 + 0.05 * pp.n_admitted),
                )

        return AdapterResult(
            tier_used="SYNDICATION_ONLY_SQUARESPACE",
            confidence=0.0,
            errors=["Squarespace site detected — no PMS backend, syndication_only strategy"],
        )

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
