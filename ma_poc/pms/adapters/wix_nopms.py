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
  - Wix is a website builder, not a PMS. Properties using Wix have no structured
    unit data in the Wix platform itself
  - Captured payloads are all site configuration / analytics — no unit data
  - Strategy is syndication_only, same as Squarespace
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


class WixNoPmsAdapter:
    """Wix (no PMS) adapter. Returns empty — syndication_only strategy."""

    pms_name: str = "wix_nopms"
    _fingerprints: list[str] = ["wix.com", "static.parastorage.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Wix sites have no structured unit data to extract.

        Returns empty result with informative error.
        """
        return AdapterResult(
            tier_used="SYNDICATION_ONLY_WIX",
            confidence=0.0,
            errors=["Wix site detected — no PMS backend, syndication_only strategy"],
        )

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
