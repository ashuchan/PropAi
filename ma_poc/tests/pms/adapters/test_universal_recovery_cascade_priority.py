"""Universal recovery cascade priority + fallback step-wise tests (2026-05-24).

Pins the FULL recovery chain order. The cascade in
``recover_universal_embed`` is:

    1. appfolio_embed         (most specific — AppFolio iframe in body)
    2. leaseleads_embed       (LeaseLeads embed)
    3. pms_portal_hop         (ResMan / SecureCafe portal anchor)
    4. generic_dom_floorplans (live DOM Track A, then static HTML Track B)
    5. sightmap_subpage       (2026-05-24 — /floorplans/ embed)
    6. g5_recovery            (2026-05-24 — g5-cl URN present)

These tests pin that order: when ALL recoveries could match, the
earlier one wins; when an earlier returns empty, the next fires.
Stops at the first winner — never tries downstream paths.

Each test mocks one or more recovery functions with controlled
return values and asserts both the WINNING tier label and which
recoveries were called.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.pms.adapters._universal_recovery import recover_universal_embed


def _ctx(body: bytes = b"<html><body>generic</body></html>"):
    """Minimal AdapterContext with an immutable-ish fetch_result."""
    import dataclasses
    @dataclasses.dataclass
    class _FR:
        body: bytes | None
        final_url: str
    ctx = MagicMock()
    ctx.fetch_result = _FR(body=body, final_url="https://example.com/")
    ctx.base_url = "https://example.com/"
    ctx.property_id = "TEST-001"
    # Recovery uses ``ctx._embed_recovery_attempted`` for idempotency.
    if hasattr(ctx, "_embed_recovery_attempted"):
        delattr(ctx, "_embed_recovery_attempted")
    return ctx


def _mock_all_recoveries(
    *,
    appfolio: list | None = None,
    leaseleads: list | None = None,
    portal_hop: list | None = None,
    generic_dom: tuple | None = None,
    sightmap: list | None = None,
    g5: list | None = None,
):
    """Patch all 6 recovery functions with controlled returns.

    Each kwarg is the value to return; None defaults to empty.
    Returns a dict of the patches keyed by recovery name so callers
    can inspect ``.called`` / ``.call_count`` after the test.
    """
    patches = {}

    patches["appfolio"] = patch(
        "ma_poc.pms.adapters._appfolio_embed.recover_appfolio_embed",
        AsyncMock(return_value=appfolio or []),
    )
    patches["leaseleads"] = patch(
        "ma_poc.pms.adapters._leaseleads_embed.recover_leaseleads_embed",
        AsyncMock(return_value=leaseleads or []),
    )
    patches["portal_hop"] = patch(
        "ma_poc.pms.adapters._pms_portal_hop.recover_pms_portal",
        AsyncMock(return_value=portal_hop or []),
    )
    patches["generic_dom"] = patch(
        "ma_poc.pms.adapters._generic_dom_floorplans.recover_generic_floorplans",
        AsyncMock(return_value=generic_dom or ([], "")),
    )
    patches["sightmap"] = patch(
        "ma_poc.pms.adapters._sightmap_subpage_recovery.recover_sightmap_subpage",
        AsyncMock(return_value=sightmap or []),
    )
    patches["g5"] = patch(
        "ma_poc.pms.adapters._g5_recovery.recover_g5",
        AsyncMock(return_value=g5 or []),
    )
    return patches


# ── PRIORITY ORDER (each step wins when earlier steps return empty) ────


@pytest.mark.asyncio
async def test_step1_appfolio_wins_when_first() -> None:
    """When appfolio returns units, no downstream recovery is tried."""
    patches = _mock_all_recoveries(
        appfolio=[{"unit_number": "A1", "extraction_tier": "TIER_1_DOM_APPFOLIO_SSR"}],
        # leave others empty — they shouldn't even be called
    )
    with patches["appfolio"] as appfolio_m, \
         patches["leaseleads"] as leaseleads_m, \
         patches["portal_hop"] as portal_hop_m, \
         patches["generic_dom"] as gen_m, \
         patches["sightmap"] as sm_m, \
         patches["g5"] as g5_m:
        units, tier, name = await recover_universal_embed(None, _ctx())
    assert units and len(units) == 1
    assert name == "appfolio_embed"
    assert tier == "TIER_1_DOM_APPFOLIO_SSR"
    # Downstream paths must NOT have been called
    leaseleads_m.assert_not_called()
    portal_hop_m.assert_not_called()
    gen_m.assert_not_called()
    sm_m.assert_not_called()
    g5_m.assert_not_called()


@pytest.mark.asyncio
async def test_step2_leaseleads_wins_when_appfolio_empty() -> None:
    patches = _mock_all_recoveries(
        leaseleads=[{"unit_number": "L1"}],
    )
    with patches["appfolio"], patches["leaseleads"], patches["portal_hop"] as ph, \
         patches["generic_dom"] as gd, patches["sightmap"] as sm, patches["g5"] as g5:
        units, tier, name = await recover_universal_embed(None, _ctx())
    assert name == "leaseleads_embed"
    assert tier == "TIER_1_API_LEASELEADS"
    ph.assert_not_called()
    gd.assert_not_called()
    sm.assert_not_called()
    g5.assert_not_called()


@pytest.mark.asyncio
async def test_step3_portal_hop_wins_when_first_two_empty() -> None:
    patches = _mock_all_recoveries(
        portal_hop=[{"unit_number": "P1", "extraction_tier": "TIER_1_API_RESMAN"}],
    )
    with patches["appfolio"], patches["leaseleads"], patches["portal_hop"], \
         patches["generic_dom"] as gd, patches["sightmap"] as sm, patches["g5"] as g5:
        units, tier, name = await recover_universal_embed(None, _ctx())
    assert name == "pms_portal_hop"
    # Portal-hop preserves the per-unit extraction_tier if present
    assert tier == "TIER_1_API_RESMAN"
    gd.assert_not_called()
    sm.assert_not_called()
    g5.assert_not_called()


@pytest.mark.asyncio
async def test_step4_generic_dom_wins_when_first_three_empty() -> None:
    patches = _mock_all_recoveries(
        generic_dom=([{"unit_number": "G1"}], "/floor-plans"),
    )
    with patches["appfolio"], patches["leaseleads"], patches["portal_hop"], \
         patches["generic_dom"], patches["sightmap"] as sm, patches["g5"] as g5:
        units, tier, name = await recover_universal_embed(None, _ctx())
    assert name == "generic_dom"
    assert tier == "TIER_3_DOM_GENERIC"
    sm.assert_not_called()
    g5.assert_not_called()


@pytest.mark.asyncio
async def test_step5_sightmap_wins_when_first_four_empty() -> None:
    """The SightMap subpage recovery (NEW 2026-05-24) fires as the 5th
    path, AFTER all the DOM-based recoveries have returned empty."""
    patches = _mock_all_recoveries(
        sightmap=[{"unit_number": "S1"}],
    )
    with patches["appfolio"], patches["leaseleads"], patches["portal_hop"], \
         patches["generic_dom"], patches["sightmap"], patches["g5"] as g5:
        units, tier, name = await recover_universal_embed(None, _ctx())
    assert name == "sightmap_subpage"
    assert tier == "TIER_1_API_SIGHTMAP_SUBPAGE_RECOVERY"
    g5.assert_not_called()


@pytest.mark.asyncio
async def test_step6_g5_wins_when_first_five_empty() -> None:
    """The G5 recovery (NEW 2026-05-24) is the 6th and final path —
    fires only when nothing earlier matched."""
    patches = _mock_all_recoveries(
        g5=[{"unit_number": "G1"}],
    )
    with patches["appfolio"], patches["leaseleads"], patches["portal_hop"], \
         patches["generic_dom"], patches["sightmap"], patches["g5"]:
        units, tier, name = await recover_universal_embed(None, _ctx())
    assert name == "g5_recovery"
    assert tier == "TIER_1_API_G5_RECOVERY"


@pytest.mark.asyncio
async def test_total_miss_returns_empty_with_attempted_flag() -> None:
    """All 6 recoveries return empty → ([], '', '') and the ctx flag
    is set so the syndication adapters don't re-run the chain inline."""
    patches = _mock_all_recoveries()
    ctx = _ctx()
    with patches["appfolio"], patches["leaseleads"], patches["portal_hop"], \
         patches["generic_dom"], patches["sightmap"], patches["g5"]:
        units, tier, name = await recover_universal_embed(None, ctx)
    assert units == []
    assert tier == ""
    assert name == ""
    # Idempotency flag must be set even on total miss
    assert getattr(ctx, "_embed_recovery_attempted", False)


# ── FALLBACK ORDER (when earlier returns empty, next runs) ─────────────


@pytest.mark.asyncio
async def test_each_step_called_in_strict_order() -> None:
    """Walk the cascade: every step returns empty, must call all 6
    in declared order."""
    call_log: list[str] = []

    async def track(name, ret):
        async def _fn(*args, **kw):
            call_log.append(name)
            return ret
        return AsyncMock(side_effect=_fn)

    with (
        patch("ma_poc.pms.adapters._appfolio_embed.recover_appfolio_embed",
              AsyncMock(side_effect=lambda *a, **k: call_log.append("appfolio") or [])),
        patch("ma_poc.pms.adapters._leaseleads_embed.recover_leaseleads_embed",
              AsyncMock(side_effect=lambda *a, **k: call_log.append("leaseleads") or [])),
        patch("ma_poc.pms.adapters._pms_portal_hop.recover_pms_portal",
              AsyncMock(side_effect=lambda *a, **k: call_log.append("portal_hop") or [])),
        patch("ma_poc.pms.adapters._generic_dom_floorplans.recover_generic_floorplans",
              AsyncMock(side_effect=lambda *a, **k: (call_log.append("generic_dom") or ([], "")))),
        patch("ma_poc.pms.adapters._sightmap_subpage_recovery.recover_sightmap_subpage",
              AsyncMock(side_effect=lambda *a, **k: call_log.append("sightmap") or [])),
        patch("ma_poc.pms.adapters._g5_recovery.recover_g5",
              AsyncMock(side_effect=lambda *a, **k: call_log.append("g5") or [])),
    ):
        await recover_universal_embed(None, _ctx())

    assert call_log == [
        "appfolio", "leaseleads", "portal_hop",
        "generic_dom", "sightmap", "g5",
    ], f"cascade order drifted: {call_log}"


@pytest.mark.asyncio
async def test_sightmap_runs_after_generic_dom_empty() -> None:
    """generic_dom returning ([], 'winningPath') — empty units even with
    a winningPath — must NOT short-circuit; sightmap should still run."""
    call_log = []
    with (
        patch("ma_poc.pms.adapters._appfolio_embed.recover_appfolio_embed",
              AsyncMock(return_value=[])),
        patch("ma_poc.pms.adapters._leaseleads_embed.recover_leaseleads_embed",
              AsyncMock(return_value=[])),
        patch("ma_poc.pms.adapters._pms_portal_hop.recover_pms_portal",
              AsyncMock(return_value=[])),
        patch("ma_poc.pms.adapters._generic_dom_floorplans.recover_generic_floorplans",
              AsyncMock(side_effect=lambda *a, **k: (call_log.append("gd") or ([], "/some-path")))),
        patch("ma_poc.pms.adapters._sightmap_subpage_recovery.recover_sightmap_subpage",
              AsyncMock(side_effect=lambda *a, **k: call_log.append("sm") or [{"unit_number": "X"}])),
        patch("ma_poc.pms.adapters._g5_recovery.recover_g5",
              AsyncMock(return_value=[])),
    ):
        units, tier, name = await recover_universal_embed(None, _ctx())
    assert call_log == ["gd", "sm"]
    assert name == "sightmap_subpage"


# ── ADAPTER TIER PRESERVATION ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_portal_hop_unit_specific_tier_preserved() -> None:
    """When portal_hop's units carry their own ``extraction_tier`` (e.g.
    TIER_1_API_RESMAN), that label wins over the generic
    TIER_1_PMS_PORTAL_HOP — keeps cohort reporting accurate."""
    patches = _mock_all_recoveries(
        portal_hop=[
            {"unit_number": "1", "extraction_tier": "TIER_1_API_RESMAN_PORTAL"}
        ],
    )
    with patches["appfolio"], patches["leaseleads"], patches["portal_hop"], \
         patches["generic_dom"], patches["sightmap"], patches["g5"]:
        units, tier, name = await recover_universal_embed(None, _ctx())
    assert tier == "TIER_1_API_RESMAN_PORTAL"


@pytest.mark.asyncio
async def test_sightmap_unit_tier_preserved_over_generic_recovery_label() -> None:
    """SightMap units stamp TIER_1_API_SIGHTMAP_DIRECT when the direct-
    API probe ran; that more-specific label must survive the recovery
    wrapper."""
    patches = _mock_all_recoveries(
        sightmap=[
            {"unit_number": "1", "extraction_tier": "TIER_1_API_SIGHTMAP_DIRECT"}
        ],
    )
    with patches["appfolio"], patches["leaseleads"], patches["portal_hop"], \
         patches["generic_dom"], patches["sightmap"], patches["g5"]:
        units, tier, name = await recover_universal_embed(None, _ctx())
    assert tier == "TIER_1_API_SIGHTMAP_DIRECT"


@pytest.mark.asyncio
async def test_g5_unit_tier_preserved_over_recovery_label() -> None:
    patches = _mock_all_recoveries(
        g5=[{"unit_number": "1", "extraction_tier": "TIER_2_API_G5_APOLLO"}],
    )
    with patches["appfolio"], patches["leaseleads"], patches["portal_hop"], \
         patches["generic_dom"], patches["sightmap"], patches["g5"]:
        units, tier, name = await recover_universal_embed(None, _ctx())
    assert tier == "TIER_2_API_G5_APOLLO"


# ── EXCEPTION SAFETY ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exception_in_chain_is_swallowed_returns_empty() -> None:
    """A recovery raising must not propagate — outer try/except returns
    empty and the chain is marked attempted."""
    ctx = _ctx()
    with (
        patch("ma_poc.pms.adapters._appfolio_embed.recover_appfolio_embed",
              AsyncMock(side_effect=RuntimeError("oh no"))),
        patch("ma_poc.pms.adapters._leaseleads_embed.recover_leaseleads_embed",
              AsyncMock(return_value=[])),
        patch("ma_poc.pms.adapters._pms_portal_hop.recover_pms_portal",
              AsyncMock(return_value=[])),
        patch("ma_poc.pms.adapters._generic_dom_floorplans.recover_generic_floorplans",
              AsyncMock(return_value=([], ""))),
        patch("ma_poc.pms.adapters._sightmap_subpage_recovery.recover_sightmap_subpage",
              AsyncMock(return_value=[])),
        patch("ma_poc.pms.adapters._g5_recovery.recover_g5",
              AsyncMock(return_value=[])),
    ):
        units, tier, name = await recover_universal_embed(None, ctx)
    assert units == []
    assert getattr(ctx, "_embed_recovery_attempted", False)
