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
        that recovery first, then a PMS-portal-hop (ResMan / RentCafe
        SecureCafe) as a second-chance recovery, before declaring
        syndication_only.
        """
        from ma_poc.extraction.post_process import post_process
        from ma_poc.pms.adapters._appfolio_embed import recover_appfolio_embed
        from ma_poc.pms.adapters._generic_dom_floorplans import (
            recover_generic_floorplans,
        )
        from ma_poc.pms.adapters._leaseleads_embed import recover_leaseleads_embed
        from ma_poc.pms.adapters._pms_portal_hop import recover_pms_portal

        units = await recover_appfolio_embed(page, ctx)
        if units:
            pp = post_process(units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                return AdapterResult(
                    units=pp.admitted,
                    plan_summaries=pp.plan_summaries,
                    tier_used="TIER_1_DOM_APPFOLIO_SSR",
                    confidence=min(0.95, 0.7 + 0.05 * pp.n_admitted),
                )

        # Second-chance: LeaseLeads embedded iframe one nav-hop deep.
        # 2026-05-19 deep-probe finding (5 confirmed cases) — see
        # _leaseleads_embed.py. Public JSON API at api.leaseleads.co.
        ll_units = await recover_leaseleads_embed(page, ctx)
        if ll_units:
            pp = post_process(ll_units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                return AdapterResult(
                    units=pp.admitted,
                    plan_summaries=pp.plan_summaries,
                    tier_used="TIER_1_API_LEASELEADS",
                    confidence=min(0.95, 0.7 + 0.04 * pp.n_admitted),
                )

        # Third-chance: PMS portal one nav-hop deep (Resman/RentCafe SecureCafe).
        # 2026-05-19 deep-probe finding — see _pms_portal_hop.py docstring.
        portal_units = await recover_pms_portal(page, ctx)
        if portal_units:
            pp = post_process(
                portal_units, property_id=getattr(ctx, "property_id", None)
            )
            if pp.n_admitted > 0:
                tier = (
                    str(portal_units[0].get("extraction_tier"))
                    if portal_units[0].get("extraction_tier")
                    else "TIER_1_PMS_PORTAL_HOP"
                )
                return AdapterResult(
                    units=pp.admitted,
                    plan_summaries=pp.plan_summaries,
                    tier_used=tier,
                    confidence=min(0.95, 0.7 + 0.05 * pp.n_admitted),
                )

        # Last-chance: generic SSR-DOM floor-plan fallback. Catches the
        # custom-CMS long tail (one labelled nav-hop deep, repeated plan
        # cards w/ bd/ba/sqft/$). Plan-level only; conservative thresholds
        # against false positives.
        generic_units, _ = await recover_generic_floorplans(page, ctx)
        if generic_units:
            pp = post_process(
                generic_units, property_id=getattr(ctx, "property_id", None)
            )
            if pp.n_admitted > 0:
                return AdapterResult(
                    units=pp.admitted,
                    plan_summaries=pp.plan_summaries,
                    tier_used="TIER_3_DOM_GENERIC",
                    confidence=min(0.85, 0.6 + 0.03 * pp.n_admitted),
                )

        return AdapterResult(
            tier_used="SYNDICATION_ONLY_SQUARESPACE",
            confidence=0.0,
            errors=["Squarespace site detected — no PMS backend, syndication_only strategy"],
        )

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
