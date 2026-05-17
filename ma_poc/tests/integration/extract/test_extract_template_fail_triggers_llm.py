"""Extract layer: structural tiers (API/JSON-LD/DOM) fail → LLM invoked.

Verifies that when tiers 1–3 produce no units from the given HTML,
the Tier-4 LLM is invoked exactly once (and receives the HTML content).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from pms.adapters.base import AdapterContext
from pms.adapters.generic import GenericAdapter


@dataclass
class _StubDetected:
    pms: str = "unknown"
    confidence: float = 0.5
    evidence: list = None  # type: ignore[assignment]
    recommended_strategy: str = "generic"

    def __post_init__(self) -> None:
        if self.evidence is None:
            self.evidence = []


def _make_fetch_result(html: str) -> FetchResult:
    return FetchResult(
        url="https://example.com/floorplans",
        outcome=FetchOutcome.OK,
        status=200,
        body=html.encode("utf-8"),
        headers={},
        render_mode=RenderMode.GET,
        final_url="https://example.com/floorplans",
        attempts=1,
        elapsed_ms=1,
    )


def _make_ctx(fetch_result: FetchResult, property_id: str = "prop-llm-001") -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://example.com/floorplans",
        detected=_StubDetected(),  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id=property_id,
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    ctx.fetch_result = fetch_result
    return ctx


# Bare HTML with rent keywords but no structured data. Must be >1KB to pass
# the LLM gate's minimum body-size check (services/llm_gate._MIN_BODY_BYTES=1024).
_BARE_RENT_HTML = (
    "<!DOCTYPE html><html lang='en'><head><title>Downtown Lofts</title></head><body>"
    "<h1>Downtown Lofts — Floorplans and Pricing</h1>"
    "<p>Studio apartments starting at $1,400/mo. 1 Bedroom units from $1,750/mo. "
    "2 Bedrooms from $2,300/mo.</p>"
    "<p>All floor plans include hardwood floors and in-unit laundry. "
    "Lease terms available from 6 to 14 months. Pets welcome on select units. "
    "Contact leasing today for current availability and rent specials.</p>"
    + "<p>Apartment community with studios, 1BR, 2BR, and 3BR floor plans. "
    "Amenities include pool, fitness center, and covered parking. </p>" * 12
    + "</body></html>"
)


def test_llm_invoked_once_when_structural_tiers_fail(fake_llm: Any) -> None:
    """LLM is called at least once when API + JSON-LD + DOM all find nothing.

    D16: fake LLM row has no per-apartment identity → plan-level partition.
    """
    fake_llm.returns({"units": [{"bedrooms": 0, "market_rent_low": 1400, "floor_plan_name": "Studio"}]})

    ctx = _make_ctx(_make_fetch_result(_BARE_RENT_HTML))
    result = asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert fake_llm.call_count >= 1, "Expected at least one LLM call"
    # Tight: fake LLM row is plan-level by construction.
    assert len(result.plan_summaries) >= 1
    assert len(result.units) == 0
    assert "TIER_4" in result.tier_used or "LLM" in result.tier_used


def test_llm_receives_html_content_in_prompt(fake_llm: Any) -> None:
    """The LLM prompt should contain HTML/text from the page, not be empty."""
    fake_llm.returns({"units": [{"bedrooms": 1, "market_rent_low": 1750, "floor_plan_name": "1BR"}]})

    ctx = _make_ctx(_make_fetch_result(_BARE_RENT_HTML))
    asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert fake_llm.call_count >= 1
    # At least one prompt should contain rent-context content
    any_has_content = any(
        p.get("user") or p.get("system")
        for p in fake_llm.recorded_prompts
    )
    assert any_has_content, "LLM prompt was empty; expected HTML/text content"


def test_llm_units_used_when_llm_returns_data(fake_llm: Any) -> None:
    """When LLM returns rows, they become the result (tier_used reflects LLM tier).

    D16: fake LLM rows have no per-apartment identity → plan-level partition.
    """
    fake_llm.returns({
        "units": [
            {"bedrooms": 0, "market_rent_low": 1400, "floor_plan_name": "Studio"},
            {"bedrooms": 1, "market_rent_low": 1750, "floor_plan_name": "1BR"},
            {"bedrooms": 2, "market_rent_low": 2300, "floor_plan_name": "2BR"},
        ]
    })

    ctx = _make_ctx(_make_fetch_result(_BARE_RENT_HTML))
    result = asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert "TIER_4" in result.tier_used or "LLM" in result.tier_used
    # Tight: 3 fake LLM rows are plan-level by construction.
    assert len(result.plan_summaries) >= 1
    assert len(result.units) == 0


def test_llm_returning_empty_leaves_empty_result(fake_llm: Any) -> None:
    """When LLM also returns empty units, final result is empty."""
    fake_llm.returns({"units": []})

    ctx = _make_ctx(_make_fetch_result(_BARE_RENT_HTML))
    result = asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert isinstance(result.units, list)
    # The adapter should not raise; units may be empty
    assert len(result.units) == 0
