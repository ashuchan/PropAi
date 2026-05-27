"""LeaseLeads-embed fallback wiring in EntrataAdapter.

2026-05-27 — When an upstream detector misroutes a LeaseLeads-shell
property (e.g. Lumina pid 72391, liveatlumina.com) to Entrata, the
primary Entrata path returns 0 units. The adapter now sniffs the
rendered page for an ``embed.leaseleads.co/<uuid>`` iframe (or the
static ``LeaseLeadsEmbed('<uuid>')`` init call) and, when present,
calls the shared ``recover_leaseleads_embed`` helper.

These tests pin the wiring — they do NOT re-test the recovery helper
itself (covered by its own unit suite). The helper is mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.entrata import EntrataAdapter
from ma_poc.pms.detector import detect_pms

FIXTURES = (
    Path(__file__).parent
    / "fixtures"
    / "entrata"
    / "leaseleads_fallback_lumina"
)


class _FallbackPage:
    """Stub Page exposing ``url`` + ``content()`` + ``evaluate()``.

    ``evaluate()`` returns ``None`` for every script — that simulates a
    Playwright session where the LeaseLeads helper's own JS sniff would
    find nothing. The test mocks ``recover_leaseleads_embed`` directly,
    so this never matters in practice; we just need a callable.
    """

    def __init__(self, html: str, url: str = "https://liveatlumina.com/pricing") -> None:
        self.url = url
        self._html = html

    async def content(self) -> str:
        return self._html

    async def evaluate(self, _script: str, *_args: object) -> object | None:
        return None


def _make_ctx(base_url: str, pid: str) -> AdapterContext:
    ctx = AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id=pid,
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    return ctx


def _ll_units(n: int = 3) -> list[dict[str, str]]:
    """Build N plausible LeaseLeads-shaped unit dicts that survive
    post_process (need at least a numeric dimension — beds or sqft)."""
    return [
        {
            "floor_plan_name": f"Plan-{i}",
            "bed_label": "1 BR",
            "bedrooms": "1",
            "bathrooms": "1",
            "sqft": str(700 + i * 25),
            "unit_number": "",
            "rent_range": f"${1800 + i * 50}",
            "rent_low": str(1800 + i * 50),
            "rent_high": str(1800 + i * 50),
            "availability_status": "AVAILABLE",
            "availability_date": "",
            "concession": "",
            "source_api_url": "https://api.leaseleads.co/api/v2/property/"
            "9e5e0a14-d118-40db-89df-b02a6176e804/floor-plans",
            "extraction_tier": "TIER_1_API_LEASELEADS",
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_entrata_leaseleads_fallback_fires_on_lumina_fixture() -> None:
    """Rendered-DOM HTML carrying the LeaseLeads iframe signature
    triggers a single call to ``recover_leaseleads_embed`` whose return
    value flows through as the adapter's units, with the LeaseLeads
    tier label."""
    html = (FIXTURES / "rendered_pricing.html").read_text(encoding="utf-8")
    assert "embed.leaseleads.co/9e5e0a14-d118-40db-89df-b02a6176e804" in html

    page = _FallbackPage(html=html)
    ctx = _make_ctx("https://liveatlumina.com/", pid="72391")

    fake_units = _ll_units(3)

    with patch(
        "ma_poc.pms.adapters._leaseleads_embed.recover_leaseleads_embed",
        new=AsyncMock(return_value=fake_units),
    ) as mocked:
        result = await EntrataAdapter().extract(page, ctx)  # type: ignore[arg-type]

    # Mock was called exactly once with the live page + ctx.
    assert mocked.await_count == 1
    args, _kwargs = mocked.call_args
    assert args[0] is page
    assert args[1] is ctx

    assert isinstance(result, AdapterResult)
    assert result.tier_used == "TIER_1_API_LEASELEADS"
    assert len(result.units) == 3
    # Mocked units flow through unmodified at the floor_plan_name level.
    assert {u["floor_plan_name"] for u in result.units} == {
        "Plan-0",
        "Plan-1",
        "Plan-2",
    }
    assert result.confidence >= 0.7


@pytest.mark.asyncio
async def test_entrata_leaseleads_fallback_not_called_without_signal() -> None:
    """Plain HTML with no LeaseLeads iframe / init call → the fallback
    helper is NEVER invoked; adapter falls through to its standard
    'no data' return."""
    html = (
        "<!doctype html><html><body>"
        "<h1>Marketing shell, no embed</h1>"
        "<p>Call us for pricing.</p>"
        "</body></html>"
    )
    page = _FallbackPage(html=html, url="https://example.com/")
    ctx = _make_ctx("https://example.com/", pid="P_NEGATIVE")

    with patch(
        "ma_poc.pms.adapters._leaseleads_embed.recover_leaseleads_embed",
        new=AsyncMock(return_value=_ll_units(5)),
    ) as mocked:
        result = await EntrataAdapter().extract(page, ctx)  # type: ignore[arg-type]

    assert mocked.await_count == 0, (
        "recover_leaseleads_embed must not be called when the HTML carries "
        "no LeaseLeads iframe signature"
    )
    # Standard 'no data' tail — tier label is downgraded when no
    # Entrata API was captured (see entrata.py:806–818).
    assert result.units == []
    assert result.confidence == 0.0
    # may13: empty-exit emits structured label (commit 6b30f18)
    assert result.tier_used == "TIER_1_API_ENTRATA_NO_RESPONSE"
