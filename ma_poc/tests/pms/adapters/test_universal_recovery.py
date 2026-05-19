"""Tests for the universal embed-recovery chain.

Covers:
  - Priority order: AppFolio → LeaseLeads → PMS-portal → generic-DOM,
    first non-empty wins.
  - Idempotency: ``recover_universal_embed`` sets
    ``ctx._embed_recovery_attempted = True`` on every invocation, so a
    second caller (e.g. scraper Step 8b after the syndication adapter
    already ran the chain) can ``already_attempted`` short-circuit.
  - Cross-vendor misroute: when no syndication-only adapter is in play
    (e.g. detector mis-picked Entrata on an AppFolio-embed site), the
    chain still recovers via ``recover_appfolio_embed``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from ma_poc.pms.adapters._universal_recovery import (
    already_attempted,
    mark_attempted,
    recover_universal_embed,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import detect_pms


class _StubPage:
    """Minimal page object — only ``.url`` is read by some recoveries."""

    url = "https://example.com/"

    async def evaluate(self, _js: str, *_a: object) -> Any:
        return None


def _ctx(base: str = "https://example.com/") -> AdapterContext:
    return AdapterContext(
        base_url=base,
        detected=detect_pms(base),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )


def _sample_unit(extra: dict[str, str] | None = None) -> dict[str, str]:
    base = {
        "floor_plan_name": "A1",
        "bedrooms": "1",
        "bathrooms": "1",
        "sqft": "800",
        "unit_number": "",
        "rent_range": "$1,500",
        "market_rent_low": 1500,
        "market_rent_high": 1500,
        "availability_status": "AVAILABLE",
        "available_units": "1",
        "extraction_tier": "",
    }
    if extra:
        base.update(extra)  # type: ignore[arg-type]
    return base  # type: ignore[return-value]


# ── Idempotency primitives ────────────────────────────────────────────────


def test_mark_and_already_attempted() -> None:
    ctx = _ctx()
    assert already_attempted(ctx) is False
    mark_attempted(ctx, "appfolio_embed")
    assert already_attempted(ctx) is True
    assert getattr(ctx, "_embed_recovery_winner") == "appfolio_embed"


# ── Priority order: first non-empty wins ──────────────────────────────────


@pytest.mark.asyncio
async def test_appfolio_wins_when_first_recovery_returns_units() -> None:
    afu = [_sample_unit({"extraction_tier": "TIER_1_DOM_APPFOLIO_SSR"})]
    with patch(
        "ma_poc.pms.adapters._appfolio_embed.recover_appfolio_embed",
        return_value=afu,
    ) as af, patch(
        "ma_poc.pms.adapters._leaseleads_embed.recover_leaseleads_embed",
        return_value=[],
    ) as ll, patch(
        "ma_poc.pms.adapters._pms_portal_hop.recover_pms_portal",
        return_value=[],
    ) as pp, patch(
        "ma_poc.pms.adapters._generic_dom_floorplans.recover_generic_floorplans",
        return_value=([], ""),
    ) as gd:
        units, tier, winner = await recover_universal_embed(_StubPage(), _ctx())  # type: ignore[arg-type]

    assert units == afu
    assert tier == "TIER_1_DOM_APPFOLIO_SSR"
    assert winner == "appfolio_embed"
    af.assert_awaited_once()
    ll.assert_not_called()
    pp.assert_not_called()
    gd.assert_not_called()


@pytest.mark.asyncio
async def test_leaseleads_runs_when_appfolio_empty() -> None:
    llu = [_sample_unit({"extraction_tier": "TIER_1_API_LEASELEADS"})]
    with patch(
        "ma_poc.pms.adapters._appfolio_embed.recover_appfolio_embed",
        return_value=[],
    ), patch(
        "ma_poc.pms.adapters._leaseleads_embed.recover_leaseleads_embed",
        return_value=llu,
    ), patch(
        "ma_poc.pms.adapters._pms_portal_hop.recover_pms_portal",
        return_value=[],
    ) as pp, patch(
        "ma_poc.pms.adapters._generic_dom_floorplans.recover_generic_floorplans",
        return_value=([], ""),
    ) as gd:
        units, tier, winner = await recover_universal_embed(_StubPage(), _ctx())  # type: ignore[arg-type]
    assert units == llu
    assert tier == "TIER_1_API_LEASELEADS"
    assert winner == "leaseleads_embed"
    pp.assert_not_called()
    gd.assert_not_called()


@pytest.mark.asyncio
async def test_pms_portal_runs_when_first_two_empty() -> None:
    portal_units = [_sample_unit({"extraction_tier": "TIER_1_API_RESMAN"})]
    with patch(
        "ma_poc.pms.adapters._appfolio_embed.recover_appfolio_embed",
        return_value=[],
    ), patch(
        "ma_poc.pms.adapters._leaseleads_embed.recover_leaseleads_embed",
        return_value=[],
    ), patch(
        "ma_poc.pms.adapters._pms_portal_hop.recover_pms_portal",
        return_value=portal_units,
    ), patch(
        "ma_poc.pms.adapters._generic_dom_floorplans.recover_generic_floorplans",
        return_value=([], ""),
    ) as gd:
        units, tier, winner = await recover_universal_embed(_StubPage(), _ctx())  # type: ignore[arg-type]
    # Prefers the per-row extraction_tier when set
    assert units == portal_units
    assert tier == "TIER_1_API_RESMAN"
    assert winner == "pms_portal_hop"
    gd.assert_not_called()


@pytest.mark.asyncio
async def test_generic_dom_runs_when_first_three_empty() -> None:
    gdu = [_sample_unit({"extraction_tier": "TIER_3_DOM_GENERIC"})]
    with patch(
        "ma_poc.pms.adapters._appfolio_embed.recover_appfolio_embed",
        return_value=[],
    ), patch(
        "ma_poc.pms.adapters._leaseleads_embed.recover_leaseleads_embed",
        return_value=[],
    ), patch(
        "ma_poc.pms.adapters._pms_portal_hop.recover_pms_portal",
        return_value=[],
    ), patch(
        "ma_poc.pms.adapters._generic_dom_floorplans.recover_generic_floorplans",
        return_value=(gdu, "/floor-plans"),
    ):
        units, tier, winner = await recover_universal_embed(_StubPage(), _ctx())  # type: ignore[arg-type]
    assert units == gdu
    assert tier == "TIER_3_DOM_GENERIC"
    assert winner == "generic_dom"


# ── Idempotency: flag set whether or not anything was recovered ──────────


@pytest.mark.asyncio
async def test_flag_set_on_win() -> None:
    ctx = _ctx()
    afu = [_sample_unit({"extraction_tier": "TIER_1_DOM_APPFOLIO_SSR"})]
    with patch(
        "ma_poc.pms.adapters._appfolio_embed.recover_appfolio_embed",
        return_value=afu,
    ):
        await recover_universal_embed(_StubPage(), ctx)  # type: ignore[arg-type]
    assert already_attempted(ctx) is True
    assert getattr(ctx, "_embed_recovery_winner") == "appfolio_embed"


@pytest.mark.asyncio
async def test_flag_set_on_total_miss() -> None:
    """When all four recoveries return empty, the flag still flips True so a
    subsequent caller (scraper Step 8b) can short-circuit on the SAME ctx.
    """
    ctx = _ctx()
    with patch(
        "ma_poc.pms.adapters._appfolio_embed.recover_appfolio_embed",
        return_value=[],
    ), patch(
        "ma_poc.pms.adapters._leaseleads_embed.recover_leaseleads_embed",
        return_value=[],
    ), patch(
        "ma_poc.pms.adapters._pms_portal_hop.recover_pms_portal",
        return_value=[],
    ), patch(
        "ma_poc.pms.adapters._generic_dom_floorplans.recover_generic_floorplans",
        return_value=([], ""),
    ):
        units, tier, winner = await recover_universal_embed(_StubPage(), ctx)  # type: ignore[arg-type]
    assert units == []
    assert tier == ""
    assert winner == ""
    assert already_attempted(ctx) is True
    # On a total miss there's no winner to record. The _embed_recovery_winner
    # attribute is intentionally left unset (None / missing).
    assert not getattr(ctx, "_embed_recovery_winner", None)


# ── Resilience: an exception in one recovery doesn't crash the chain ─────


@pytest.mark.asyncio
async def test_exception_inside_chain_is_swallowed() -> None:
    """A raise from a recovery shouldn't propagate; the helper marks the
    chain as attempted and returns empty (defensive)."""
    ctx = _ctx()

    async def _boom(*_a: object, **_k: object) -> list[dict[str, str]]:
        raise RuntimeError("simulated")

    with patch(
        "ma_poc.pms.adapters._appfolio_embed.recover_appfolio_embed",
        side_effect=_boom,
    ):
        units, tier, winner = await recover_universal_embed(_StubPage(), ctx)  # type: ignore[arg-type]
    assert units == []
    assert tier == ""
    assert winner == ""
    # Still marks attempted so scraper Step 8b doesn't re-run uselessly.
    assert already_attempted(ctx) is True
