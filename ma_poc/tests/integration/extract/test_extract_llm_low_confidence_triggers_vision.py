"""Extract layer: LLM text tiers return empty → Vision (Tier 5) runs.

When all text-based LLM tiers exhaust without producing units AND a page
object with screenshot capability is present, the vision tier fires.

The "low confidence → vision" path is exercised by having fake_llm return
no units (confidence = 0) so the adapter falls through to the screenshot tier.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any


from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from pms.adapters.base import AdapterContext
from pms.adapters.generic import GenericAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _StubDetected:
    pms: str = "unknown"
    confidence: float = 0.5
    evidence: list = None  # type: ignore[assignment]
    recommended_strategy: str = "generic"

    def __post_init__(self) -> None:
        if self.evidence is None:
            self.evidence = []


# Minimal valid 1×1 white PNG (67 bytes)
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _ScreenshotPage:
    """Minimal async page stub that returns a PNG from screenshot()."""

    def __init__(self, png_bytes: bytes = _TINY_PNG) -> None:
        self._png = png_bytes

    async def screenshot(self, **kwargs: Any) -> bytes:
        return self._png

    async def content(self) -> str:
        return "<html><body><p>Rent from $1,500/mo. Studio and 1 Bedroom available.</p></body></html>"


def _make_fetch_result(html: str = "") -> FetchResult:
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


# HTML with rent keywords (required for vision gate to open).
# Must be >1KB so the LLM gate's body-size check passes.
_RENT_KEYWORD_HTML = (
    "<html><body>"
    "<p>Studio from $1,400/mo. 1 Bedroom from $1,750/mo. "
    "2 Bedroom apartments from $2,300/mo. Floor plan details available.</p>"
    "<p>Apartment community with studio, 1BR, 2BR, and 3BR units. "
    "Lease terms from 6 to 14 months. Sqft: 550-1100. Pets welcome. "
    "Contact our leasing office today for rent specials and availability.</p>"
    + "<p>Available floor plans include studio, one bedroom, and two bedroom units. "
    "All apartments include hardwood floors, in-unit laundry, and stainless steel appliances. "
    "Pool, fitness center, and parking available on site. </p>" * 12
    + "</body></html>"
)


def _make_ctx(page_stub: Any | None = None) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://example.com/floorplans",
        detected=_StubDetected(),  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id="prop-vision-001",
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    ctx.fetch_result = _make_fetch_result(_RENT_KEYWORD_HTML)
    if page_stub is not None:
        ctx._page = page_stub  # type: ignore[attr-defined]
    return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_vision_called_when_llm_returns_empty(
    fake_llm: Any,
    fake_vision: Any,
    monkeypatch: Any,
) -> None:
    """When text LLM returns no units and a page with screenshot is present,
    the vision tier fires and its results become the final result."""
    monkeypatch.setenv("ENABLE_TIER5_VISION", "true")

    fake_llm.returns({"units": []})
    fake_vision.returns_units([{"bedrooms": 1, "market_rent_low": 1500, "floor_plan_name": "1BR"}])

    page_stub = _ScreenshotPage()
    ctx = _make_ctx(page_stub=page_stub)

    result = asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert fake_vision.call_count == 1, (
        f"Expected vision to be called once but call_count={fake_vision.call_count}"
    )
    assert len(result.units) >= 1


def test_vision_not_called_when_disabled(
    fake_llm: Any,
    fake_vision: Any,
    monkeypatch: Any,
) -> None:
    """When ENABLE_TIER5_VISION=false, vision tier is skipped entirely."""
    monkeypatch.setenv("ENABLE_TIER5_VISION", "false")

    fake_llm.returns({"units": []})

    page_stub = _ScreenshotPage()
    ctx = _make_ctx(page_stub=page_stub)

    asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert fake_vision.call_count == 0, (
        "Vision should not be called when ENABLE_TIER5_VISION=false"
    )


def test_vision_not_called_without_page_object(
    fake_llm: Any,
    fake_vision: Any,
    monkeypatch: Any,
) -> None:
    """Vision requires a page object with screenshot — without it, vision is skipped."""
    monkeypatch.setenv("ENABLE_TIER5_VISION", "true")

    fake_llm.returns({"units": []})

    ctx = _make_ctx(page_stub=None)  # no page object
    asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert fake_vision.call_count == 0, (
        "Vision should not fire when no page object is available"
    )


def test_vision_result_used_when_text_llm_yields_nothing(
    fake_llm: Any,
    fake_vision: Any,
    monkeypatch: Any,
) -> None:
    """Units from vision extractor are included in the final result."""
    monkeypatch.setenv("ENABLE_TIER5_VISION", "true")

    fake_llm.returns({"units": []})
    fake_vision.returns_units([
        {"bedrooms": 0, "market_rent_low": 1400, "floor_plan_name": "Studio"},
        {"bedrooms": 2, "market_rent_low": 2300, "floor_plan_name": "2BR"},
    ])

    page_stub = _ScreenshotPage()
    ctx = _make_ctx(page_stub=page_stub)

    result = asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert fake_vision.call_count == 1
    assert len(result.units) >= 1
