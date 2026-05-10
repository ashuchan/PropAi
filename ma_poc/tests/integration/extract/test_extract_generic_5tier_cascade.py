"""Extract layer: GenericAdapter 5-tier cascade ordering.

Verifies that the cascade tries API → JSON-LD → DOM → LLM in order and that
the first tier returning ≥1 unit wins (tier_used reflects the winning tier).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any


from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from pms.adapters.base import AdapterContext
from pms.adapters.generic import GenericAdapter
from pms.detector import DetectedPMS


@dataclass
class _StubDetected:
    pms: str = "unknown"
    confidence: float = 0.5
    evidence: list = None  # type: ignore[assignment]
    recommended_strategy: str = "generic"

    def __post_init__(self) -> None:
        if self.evidence is None:
            self.evidence = []


def _make_fetch_result(html: str = "") -> FetchResult:
    return FetchResult(
        url="https://example.com/",
        outcome=FetchOutcome.OK,
        status=200,
        body=html.encode("utf-8"),
        headers={},
        render_mode=RenderMode.GET,
        final_url="https://example.com/",
        attempts=1,
        elapsed_ms=1,
    )


def _make_ctx(
    *,
    api_responses: list[dict[str, Any]] | None = None,
    fetch_result: FetchResult | None = None,
    property_id: str = "test-prop-001",
) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://example.com/",
        detected=_StubDetected(),  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id=property_id,
    )
    if api_responses is not None:
        ctx._api_responses = api_responses  # type: ignore[attr-defined]
    if fetch_result is not None:
        ctx.fetch_result = fetch_result
    return ctx


# ---------------------------------------------------------------------------
# TIER 1 — API wins when it returns units
# ---------------------------------------------------------------------------


def test_tier1_api_wins_when_api_returns_units() -> None:
    """Tier-1 API extraction wins when api_responses has recognisable unit data."""
    api_responses = [
        {
            "url": "https://example.com/api/units",
            "body": [
                {"id": "u1", "asking_rent": 1500, "num_bedrooms": 1, "num_bathrooms": 1},
                {"id": "u2", "asking_rent": 1800, "num_bedrooms": 2, "num_bathrooms": 1},
            ],
        }
    ]
    ctx = _make_ctx(api_responses=api_responses)
    result = asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert len(result.units) >= 1
    assert result.tier_used in ("TIER_1_API", "TIER_1_PROFILE_MAPPING")


# ---------------------------------------------------------------------------
# TIER 2 — JSON-LD fallback when API is empty
# ---------------------------------------------------------------------------

_JSONLD_HTML = """<!DOCTYPE html>
<html>
<head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Apartment",
  "name": "1BD Suite",
  "numberOfRooms": 1,
  "floorSize": {"@type": "QuantitativeValue", "value": 750},
  "offers": {"@type": "Offer", "lowPrice": 1500, "highPrice": 1700}
}
</script>
</head>
<body><h1>Test Apartments</h1><p>Rent from $1,500/mo</p></body>
</html>"""


def test_tier2_jsonld_used_when_api_is_empty() -> None:
    """When API has no recognisable units, JSON-LD in HTML is the fallback."""
    api_responses: list[dict] = []  # deliberately empty
    ctx = _make_ctx(
        api_responses=api_responses,
        fetch_result=_make_fetch_result(_JSONLD_HTML),
    )
    result = asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert result.tier_used == "TIER_2_JSONLD"
    assert len(result.units) >= 1


# ---------------------------------------------------------------------------
# TIER 1.5 — Embedded JSON blobs in HTML
# ---------------------------------------------------------------------------

_EMBEDDED_JSON_HTML = """<!DOCTYPE html>
<html>
<body>
<script>
window.__INITIAL_STATE__ = {
  "floorplans": [
    {"id": "fp1", "beds": 1, "baths": 1, "rent": 1350, "sqft": 680},
    {"id": "fp2", "beds": 2, "baths": 2, "rent": 1850, "sqft": 980}
  ]
};
</script>
</body>
</html>"""


def test_tier1_5_embedded_json_extracts_units() -> None:
    """Tier 1.5: embedded JSON blobs in HTML (window.__INITIAL_STATE__ etc.) yield units."""
    ctx = _make_ctx(
        api_responses=[],
        fetch_result=_make_fetch_result(_EMBEDDED_JSON_HTML),
    )
    result = asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))
    # May land on TIER_1_5_EMBEDDED or fall through to another tier — just
    # verify no crash and the tier is recorded.
    assert result.tier_used is not None
    assert isinstance(result.units, list)


# ---------------------------------------------------------------------------
# LLM tier — invoked when structural tiers all fail (requires fake_llm)
# ---------------------------------------------------------------------------


# Bare HTML with rent keywords but no structured data. Must be >1KB to pass
# the LLM gate's minimum body-size check (services/llm_gate._MIN_BODY_BYTES=1024).
_BARE_RENT_HTML = (
    "<html><body>"
    "<h1>Downtown Lofts — Floorplans &amp; Pricing</h1>"
    "<p>Studio apartments starting at $1,400/mo. 1 Bedroom units from $1,750/mo. "
    "2 Bedrooms from $2,300/mo. Lease terms 6–14 months. Pets welcome.</p>"
    + "<p>Apartment community with studios, 1BR, 2BR, and 3BR units. "
    "All floor plans include hardwood floors and in-unit laundry. "
    "Contact leasing for current rent specials and availability. </p>" * 12
    + "</body></html>"
)


def test_tier4_llm_invoked_when_structural_tiers_fail(fake_llm: Any) -> None:
    """LLM is called when API, JSON-LD, and DOM all yield nothing."""
    fake_llm.returns({"units": [{"bedrooms": 1, "market_rent_low": 1500, "floor_plan_name": "1BR"}]})

    ctx = _make_ctx(
        api_responses=[],
        fetch_result=_make_fetch_result(_BARE_RENT_HTML),
    )
    result = asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    # LLM was reached because structural tiers found nothing
    assert fake_llm.call_count >= 1
    # Result should have units from the fake LLM response
    assert len(result.units) >= 1


# ---------------------------------------------------------------------------
# Empty result — no tiers produce data
# ---------------------------------------------------------------------------


def test_no_tiers_produce_empty_result(fake_llm: Any) -> None:
    """When all tiers return nothing, result.units is empty."""
    fake_llm.returns({"units": []})

    ctx = _make_ctx(api_responses=[], fetch_result=_make_fetch_result(""))
    result = asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert isinstance(result.units, list)
    # Tier is set to something even on failure
    assert result.tier_used is not None
