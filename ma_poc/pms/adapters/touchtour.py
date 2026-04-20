"""
TouchTour / Ovation adapter.

Research log
------------
Web sources consulted:
  - https://www.mytouchtour.com (product marketing — confirmed platform brand,
    accessed 2026-04-20)
  - https://www.liveovation.com (Ovation Property Management parent portfolio
    site, accessed 2026-04-20)
  - 2026-04-20 failure-analysis of 3 Vegas properties (24928 Summer Winds,
    26151 Madera, 27595 Positano) — all served from
    ``lasvegasliving.mytouchtour.com/community/floorplans/<slug>``.
Real payloads inspected:
  - Research-blocked. TouchTour is a proprietary platform with no public API
    docs. The 04-20 dataset does not carry any TouchTour raw_api/ captures
    (the SightMap misrouting swallowed them). Real captures from the three
    Vegas properties above are required before parser work proceeds.
Key findings (preliminary, pending real captures):
  - Host pattern: ``<market>.mytouchtour.com`` (e.g. lasvegasliving, not a
    property-specific subdomain) plus the Ovation parent portal at
    liveovation.com.
  - Strategy: cascade (DOM-first). TouchTour appears to server-render the
    floorplans list into HTML rather than exposing a clean XHR/fetch inventory
    endpoint; Change 2's ``confirm_detection`` demotes any URL-based
    detection that lacks a matching body, which is correct for DOM-only
    adapters — they opt out of demotion by omitting ``matches_response_body``.
  - Known gotchas: Do not confuse ApartmentHomeLiving.com syndication listings
    (e.g. "Apartment Unit: 1128, Apartment Model: 1D F - Clove" for Madera)
    with the source-of-truth TouchTour data. Those are 3rd-party rollups,
    not TouchTour payloads.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


_TIER_BASE = "TIER_1_API_TOUCHTOUR"
_TIER_NO_RESPONSE = f"{_TIER_BASE}_NO_RESPONSE"
_TIER_RESEARCH_BLOCKED = f"{_TIER_BASE}_RESEARCH_BLOCKED"


class TouchTourAdapter:
    """TouchTour / Ovation PMS adapter — research-blocked stub.

    Implementation waits on at least 2 real captures from the 04-20 Vegas
    failure list (Summer Winds 24928 / Madera 26151 / Positano 27595). Until
    then the adapter runs a structured-failure stamp so downstream reporting
    can distinguish a pending-research TouchTour failure from the pre-Change-1
    undifferentiated SightMap bucket these properties were previously routed
    into.

    Note: this adapter intentionally does NOT implement
    ``matches_response_body``. Omitting the method is how DOM-only adapters
    opt out of ``detector.confirm_detection`` demotion — the detection must
    rely on URL / HTML fingerprints (which are strong for ``mytouchtour.com``)
    because TouchTour appears to server-render inventory rather than serve
    it via XHR.
    """

    pms_name: str = "touchtour"
    _fingerprints: list[str] = [
        "mytouchtour.com",
        "liveovation.com",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER_BASE)
        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])

        if not api_responses:
            result.tier_used = _TIER_NO_RESPONSE
            result.errors.append(
                "TOUCHTOUR_NO_RESPONSE: no network responses captured during "
                "page load; TouchTour appears to server-render inventory — "
                "a DOM parser is required (research-blocked on ≥2 captures)"
            )
            result.confidence = 0.0
            return result

        result.tier_used = _TIER_RESEARCH_BLOCKED
        result.errors.append(
            "TOUCHTOUR_RESEARCH_BLOCKED: adapter stub only. Need ≥2 real "
            "captures from Summer Winds (24928), Madera (26151), or Positano "
            "(27595) to reverse-engineer inventory shape before parser ships."
        )
        result.confidence = 0.0
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
