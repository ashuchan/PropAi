"""
Wix (no PMS) adapter.

Research log
------------
Web sources consulted:
  - https://www.wix.com/ — Wix website builder (accessed 2026-04-17)
  - Wix does not provide apartment management features
Real payloads inspected (from data/runs/*/raw_api/):
  - 7227, 260116, 305316 — embedded:json-block:wix-* payloads containing only
    Wix site configuration (wix-essential-viewer-model, wix-fedops, wix-viewer-model)
    with no unit/pricing data
Key findings:
  - Wix is a website builder, not a PMS. Properties using Wix have no
    structured unit data in the Wix platform itself.
  - A sizable minority *do* embed a real PMS one nav-hop deep — same
    pattern as Squarespace. ``recover_universal_embed`` tries the four
    paths (AppFolio iframe → LeaseLeads → PMS portal hop → generic SSR)
    before we declare ``SYNDICATION_ONLY_WIX``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


class WixNoPmsAdapter:
    """Wix (no PMS) adapter — runs universal embed-recovery first."""

    pms_name: str = "wix_nopms"
    _fingerprints: list[str] = ["wix.com", "static.parastorage.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Try the universal embed-recovery chain before declaring
        syndication_only.
        """
        from ma_poc.extraction.post_process import post_process
        from ma_poc.pms.adapters._universal_recovery import (
            recover_universal_embed,
        )

        units, tier, _winner = await recover_universal_embed(page, ctx)
        fallback_result: AdapterResult | None = None
        if units:
            pp = post_process(units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                candidate = AdapterResult(
                    units=pp.admitted,
                    plan_summaries=pp.plan_summaries,
                    tier_used=tier,
                    confidence=min(0.95, 0.7 + 0.04 * pp.n_admitted),
                    winning_url=str(pp.admitted[0].get("source_api_url") or ""),
                    api_responses=list(
                        getattr(ctx, "_embed_recovery_api_responses", []) or []
                    ),
                    unit_source_provenance=list(
                        getattr(ctx, "_embed_recovery_unit_source_provenance", [])
                        or []
                    ),
                )
                if pp.n_unit_level > 0:
                    return candidate
                fallback_result = candidate

        # Universal recovery deliberately keeps broad generic plan text as a
        # fallback. Before accepting that lossy output, give the Wix-authored
        # CMS/card parser the same source. Provider-backed physical inventory
        # above always has priority.
        from ma_poc.pms.adapters.wix_floor_plans import WixFloorPlansAdapter

        authored = await WixFloorPlansAdapter().extract(page, ctx)
        if authored.units:
            return authored
        if fallback_result is not None:
            return fallback_result

        return AdapterResult(
            tier_used="SYNDICATION_ONLY_WIX",
            confidence=0.0,
            errors=["Wix site detected — no PMS backend, syndication_only strategy"],
        )

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
