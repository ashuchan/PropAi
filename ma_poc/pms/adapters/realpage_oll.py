"""
RealPage OLL (non-OneSite) adapter.

Research log
------------
Web sources consulted:
  - https://www.realpage.com/ — RealPage platform overview (accessed 2026-04-17)
  - RealPage API patterns documented in scripts/entrata.py and scrape_properties.py
Real payloads inspected (from data/runs/*/raw_api/):
  - 293707 — api.ws.realpage.com/v2/property/7824595/floorplans (shared with OneSite)
  - No distinct RealPage OLL payloads captured in current data set; this adapter
    handles the non-OneSite RealPage portal hop pattern
Key findings:
  - API endpoint: api.ws.realpage.com/v2/property/{id}/floorplans (same as OneSite)
  - RealPage OLL is the generic RealPage Online Leasing portal that serves properties
    not on the OneSite subdomain pattern ({id}.onlineleasing.realpage.com)
  - Strategy is portal_hop: navigate to the RealPage portal and extract from there
  - Shares the same parser as OneSite for the floorplans response format
  - Known gotchas: must navigate to the portal first (resolver Phase 4 handles this)
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._daily_runner_parsers import (
    realpage_units_to_adapter_shape as _dr_realpage_units,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.onesite import (
    _is_realpage_units_response,
    parse_realpage_floorplans,
)

if TYPE_CHECKING:
    from playwright.async_api import Page


class RealPageOllAdapter:
    """RealPage OLL (non-OneSite) PMS adapter.

    Uses the same parser as OneSite since the API response format is identical.
    The difference is in the detection/resolution path (portal_hop strategy).
    """

    pms_name: str = "realpage_oll"
    _fingerprints: list[str] = ["realpage.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from RealPage API responses."""
        result = AdapterResult(tier_used="TIER_1_API_REALPAGE_OLL")
        all_units: list[dict[str, str]] = []

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        for resp in api_responses:
            body = resp.get("body")
            url = resp.get("url", "")
            if isinstance(body, dict) and isinstance(body.get("response"), dict) \
                    and "floorplans" in body["response"]:
                units = parse_realpage_floorplans(body, url)
                if units:
                    for u in units:
                        u["extraction_tier"] = "TIER_1_API_REALPAGE_OLL"
                    all_units.extend(units)
                    result.api_responses.append(resp)
            elif _is_realpage_units_response(body, url):
                try:
                    units = _dr_realpage_units(body, url) or []
                except Exception as exc:
                    units = []
                    result.errors.append(f"realpage-units-parse-error: {exc}")
                if units:
                    for u in units:
                        u["extraction_tier"] = "TIER_1_API_REALPAGE_OLL"
                    all_units.extend(units)
                    result.api_responses.append(resp)

        if all_units:
            result.units = all_units
            result.winning_url = result.api_responses[0].get("url") if result.api_responses else None
            result.confidence = min(0.90, 0.7 + 0.05 * len(all_units))
        else:
            result.confidence = 0.0
            result.errors.append("No RealPage OLL data found in captured API responses")

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
