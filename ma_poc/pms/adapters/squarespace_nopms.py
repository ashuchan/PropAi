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
  - Squarespace is a website builder, not a PMS. Properties using
    Squarespace typically have no structured unit data accessible via API.
  - A sizable minority *do* embed a real PMS one nav-hop deep:
    AppFolio listings iframe / LeaseLeads iframe / ResMan-RentCafe portal
    link / generic SSR plan grid. ``recover_universal_embed`` tries all
    four in priority order before we declare ``SYNDICATION_ONLY_*``.
  - When all recoveries miss, return empty so downstream knows no PMS
    backend is reachable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


class SquarespaceNoPmsAdapter:
    """Squarespace (no PMS) adapter — runs universal embed-recovery first."""

    pms_name: str = "squarespace_nopms"
    _fingerprints: list[str] = ["squarespace.com", "static1.squarespace.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Try the universal embed-recovery chain (AppFolio iframe →
        LeaseLeads iframe → PMS portal hop → generic SSR plan grid) before
        declaring syndication_only.
        """
        from ma_poc.extraction.post_process import post_process
        from ma_poc.pms.adapters._universal_recovery import (
            recover_universal_embed,
        )

        units, tier, _winner = await recover_universal_embed(page, ctx)
        if units:
            pp = post_process(units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                return AdapterResult(
                    units=pp.admitted,
                    plan_summaries=pp.plan_summaries,
                    tier_used=tier,
                    confidence=min(0.95, 0.7 + 0.04 * pp.n_admitted),
                )

        return AdapterResult(
            tier_used="SYNDICATION_ONLY_SQUARESPACE",
            confidence=0.0,
            errors=["Squarespace site detected — no PMS backend, syndication_only strategy"],
        )

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
