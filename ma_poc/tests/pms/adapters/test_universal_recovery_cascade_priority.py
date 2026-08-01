"""Priority contract for the universal recovery cascade.

Every unit-capable recovery gets first refusal.  The generic DOM arm is a
plan-level catchall and therefore runs last:

    AppFolio -> LeaseLeads -> portal -> Knock DNI -> BetterNOI
    -> embedded availability -> Elise -> SightMap -> Rently -> G5 -> generic DOM

These tests deliberately make generic DOM return plan rows while another arm
can return real units.  That is the regression shape: if generic moves earlier,
the unit roster becomes unreachable even though it is published.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.pms.adapters._universal_recovery import recover_universal_embed

_TARGETS = {
    "appfolio": "ma_poc.pms.adapters._appfolio_embed.recover_appfolio_embed",
    "leaseleads": "ma_poc.pms.adapters._leaseleads_embed.recover_leaseleads_embed",
    "portal_hop": "ma_poc.pms.adapters._pms_portal_hop.recover_pms_portal",
    "knock_dni": ("ma_poc.pms.adapters._knock_dni_recovery.recover_knock_dni_config"),
    "betternoi": ("ma_poc.pms.adapters._betternoi_recovery.recover_betternoi_units"),
    "funnel_spaces": "ma_poc.pms.adapters.funnel.recover_funnel_spaces",
    "avail_table": "ma_poc.pms.adapters._avail_table_recovery.recover_avail_table",
    "elise": (
        "ma_poc.pms.adapters._elise_applications_recovery.recover_elise_applications"
    ),
    "rentvision": "ma_poc.pms.adapters.rentvision.recover_rentvision_crossroute",
    "sightmap": ("ma_poc.pms.adapters._sightmap_subpage_recovery.recover_sightmap_subpage"),
    "rently": "ma_poc.pms.adapters.rently.recover_rently",
    "g5": "ma_poc.pms.adapters._g5_recovery.recover_g5",
    "generic_dom": ("ma_poc.pms.adapters._generic_dom_floorplans.recover_generic_floorplans"),
}

_UNIT_ROUTES = (
    "appfolio",
    "leaseleads",
    "portal_hop",
    "knock_dni",
    "betternoi",
    "funnel_spaces",
    "avail_table",
    "elise",
    "rentvision",
    "sightmap",
    "rently",
    "g5",
)


def _ctx(body: bytes = b"<html><body>generic</body></html>") -> MagicMock:
    """Return the minimal context read by the recovery arms."""

    @dataclasses.dataclass
    class _FetchResult:
        body: bytes | None
        final_url: str

    ctx = MagicMock()
    ctx.fetch_result = _FetchResult(body=body, final_url="https://example.com/")
    ctx.base_url = "https://example.com/"
    ctx.property_id = "TEST-001"
    if hasattr(ctx, "_embed_recovery_attempted"):
        delattr(ctx, "_embed_recovery_attempted")
    return ctx


def _unit(number: str, tier: str = "") -> dict[str, str]:
    return {"unit_number": number, "extraction_tier": tier}


def _plan(tier: str = "TIER_3_DOM_GENERIC") -> dict[str, str]:
    return {
        "unit_number": "",
        "floor_plan_name": "A1",
        "extraction_tier": tier,
    }


@contextmanager
def _mock_recoveries(
    *,
    appfolio: list[dict[str, str]] | None = None,
    leaseleads: list[dict[str, str]] | None = None,
    portal_hop: list[dict[str, str]] | None = None,
    knock_dni: list[dict[str, str]] | None = None,
    betternoi: list[dict[str, str]] | None = None,
    funnel_spaces: list[dict[str, str]] | None = None,
    avail_table: list[dict[str, str]] | None = None,
    elise: list[dict[str, str]] | None = None,
    rentvision: list[dict[str, str]] | None = None,
    sightmap: list[dict[str, str]] | None = None,
    rently: list[dict[str, str]] | None = None,
    g5: list[dict[str, str]] | None = None,
    generic_dom: tuple[list[dict[str, str]], str] | None = None,
) -> Iterator[dict[str, AsyncMock]]:
    """Patch every listed arm so priority tests never perform real I/O."""
    values: dict[str, Any] = {
        "appfolio": appfolio or [],
        "leaseleads": leaseleads or [],
        "portal_hop": portal_hop or [],
        "knock_dni": knock_dni or [],
        "betternoi": betternoi or [],
        "funnel_spaces": funnel_spaces or [],
        "avail_table": avail_table or [],
        "elise": elise or [],
        "rentvision": rentvision or [],
        "sightmap": sightmap or [],
        "rently": rently or [],
        "g5": g5 or [],
        "generic_dom": generic_dom or ([], ""),
    }
    with ExitStack() as stack:
        mocks: dict[str, AsyncMock] = {}
        for name, target in _TARGETS.items():
            mock = AsyncMock(return_value=values[name])
            stack.enter_context(patch(target, new=mock))
            mocks[name] = mock
        yield mocks


@pytest.mark.asyncio
async def test_specific_recovery_stops_the_cascade() -> None:
    """The first specific unit roster wins and no downstream arm runs."""
    with _mock_recoveries(appfolio=[_unit("A1")]) as mocks:
        units, tier, name = await recover_universal_embed(None, _ctx())

    assert units == [_unit("A1")]
    assert tier == "TIER_1_DOM_APPFOLIO_SSR"
    assert name == "appfolio_embed"
    for downstream in (*_UNIT_ROUTES[1:], "generic_dom"):
        mocks[downstream].assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "expected_name", "row_tier", "expected_tier"),
    (
        (
            "knock_dni",
            "knock_dni_config",
            "TIER_1_API_KNOCK_DNI_CONFIG",
            "TIER_1_API_KNOCK_DNI_CONFIG",
        ),
        (
            "betternoi",
            "betternoi",
            "TIER_1_API_BETTERNOI",
            "TIER_1_API_BETTERNOI",
        ),
        (
            "funnel_spaces",
            "funnel_spaces",
            "TIER_1_DOM_FUNNEL_SPACES",
            "TIER_1_DOM_FUNNEL_SPACES",
        ),
        (
            "avail_table",
            "avail_table",
            "TIER_1_EMBEDDED_AVAIL_TABLE",
            "TIER_1_EMBEDDED_AVAIL_TABLE",
        ),
        (
            "elise",
            "elise_applications",
            "TIER_1_API_ELISE_APPLICATIONS",
            "TIER_1_API_ELISE_APPLICATIONS",
        ),
        (
            "rentvision",
            "rentvision_crossroute",
            "TIER_3_DOM_RENTVISION_UNIT_LEVEL",
            "TIER_3_DOM_RENTVISION_UNIT_LEVEL",
        ),
        (
            "sightmap",
            "sightmap_subpage",
            "TIER_1_API_SIGHTMAP_DIRECT",
            "TIER_1_API_SIGHTMAP_DIRECT",
        ),
        ("rently", "rently", "TIER_1_API_RENTLY", "TIER_1_API_RENTLY"),
        (
            "g5",
            "g5_recovery",
            "TIER_2_API_G5_APOLLO",
            "TIER_2_API_G5_APOLLO",
        ),
    ),
    ids=(
        "knock-dni",
        "betternoi",
        "funnel-spaces",
        "avail-table",
        "elise-applications",
        "rentvision",
        "sightmap",
        "rently",
        "g5",
    ),
)
async def test_generic_plan_cannot_preempt_unit_route(
    route: str,
    expected_name: str,
    row_tier: str,
    expected_tier: str,
) -> None:
    """A generic plan match must not hide a reachable unit-level result."""
    kwargs = {
        route: [_unit("UNIT-101", row_tier)],
        "generic_dom": ([_plan()], "/floorplans"),
    }
    with _mock_recoveries(**kwargs) as mocks:
        units, tier, name = await recover_universal_embed(None, _ctx())

    assert units[0]["unit_number"] == "UNIT-101"
    assert tier == expected_tier
    assert name == expected_name
    mocks[route].assert_awaited_once()
    mocks["generic_dom"].assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan_route", "plan_tier"),
    (
        ("leaseleads", "TIER_1_API_LEASELEADS_PLAN_LEVEL"),
        ("portal_hop", "TIER_1_API_RESMAN_PLAN_LEVEL"),
    ),
    ids=("leaseleads-plan", "resman-portal-plan"),
)
async def test_earlier_plan_only_arm_cannot_preempt_later_unit_route(
    plan_route: str,
    plan_tier: str,
) -> None:
    """A vendor-native plan catalogue is fallback data, not a cascade win."""
    sightmap_units = [_unit("UNIT-202", "TIER_1_API_SIGHTMAP_DIRECT")]
    kwargs = {
        plan_route: [_plan(plan_tier)],
        "sightmap": sightmap_units,
    }
    with _mock_recoveries(**kwargs) as mocks:
        units, tier, name = await recover_universal_embed(None, _ctx())

    assert units == sightmap_units
    assert tier == "TIER_1_API_SIGHTMAP_DIRECT"
    assert name == "sightmap_subpage"
    mocks[plan_route].assert_awaited_once()
    mocks["sightmap"].assert_awaited_once()
    mocks["rently"].assert_not_called()


@pytest.mark.asyncio
async def test_richest_earlier_plan_catalogue_survives_total_unit_miss() -> None:
    """Later unit arms still run, then the best plan fallback is returned."""
    leaseleads_plans = [
        _plan("TIER_1_API_LEASELEADS_PLAN_LEVEL"),
        {
            **_plan("TIER_1_API_LEASELEADS_PLAN_LEVEL"),
            "floor_plan_name": "B2",
        },
    ]
    generic_plans = [_plan()]
    with _mock_recoveries(
        leaseleads=leaseleads_plans,
        generic_dom=(generic_plans, "/floorplans"),
    ) as mocks:
        units, tier, name = await recover_universal_embed(None, _ctx())

    assert units == leaseleads_plans
    assert tier == "TIER_1_API_LEASELEADS_PLAN_LEVEL"
    assert name == "leaseleads_embed"
    for route in _UNIT_ROUTES:
        mocks[route].assert_awaited_once()
    mocks["generic_dom"].assert_awaited_once()


@pytest.mark.asyncio
async def test_generic_dom_runs_only_after_every_unit_route_declines() -> None:
    """Generic plan rows remain available as the final fallback."""
    plans = [_plan()]
    with _mock_recoveries(generic_dom=(plans, "/floorplans")) as mocks:
        units, tier, name = await recover_universal_embed(None, _ctx())

    assert units == plans
    assert tier == "TIER_3_DOM_GENERIC"
    assert name == "generic_dom"
    for route in _UNIT_ROUTES:
        mocks[route].assert_awaited_once()
    mocks["generic_dom"].assert_awaited_once()


@pytest.mark.asyncio
async def test_generic_dom_preserves_row_extraction_tier() -> None:
    """A unit-grid tier stamped by the generic parser must not be relabelled."""
    rows = [_unit("204", "TIER_2_DOM_UNIT_GRID")]
    with _mock_recoveries(generic_dom=(rows, "/availability")):
        units, tier, name = await recover_universal_embed(None, _ctx())

    assert units == rows
    assert tier == "TIER_2_DOM_UNIT_GRID"
    assert name == "generic_dom"


@pytest.mark.asyncio
async def test_all_empty_arms_run_in_strict_order() -> None:
    call_log: list[str] = []

    def record(name: str, result: Any):
        async def _record(*_args: object, **_kwargs: object) -> Any:
            call_log.append(name)
            return result

        return _record

    with _mock_recoveries() as mocks:
        for route in _UNIT_ROUTES:
            mocks[route].side_effect = record(route, [])
        mocks["generic_dom"].side_effect = record("generic_dom", ([], ""))
        await recover_universal_embed(None, _ctx())

    assert call_log == [*_UNIT_ROUTES, "generic_dom"]


@pytest.mark.asyncio
async def test_total_miss_marks_chain_attempted() -> None:
    ctx = _ctx()
    with _mock_recoveries():
        units, tier, name = await recover_universal_embed(None, ctx)

    assert (units, tier, name) == ([], "", "")
    assert getattr(ctx, "_embed_recovery_attempted", False)


@pytest.mark.asyncio
async def test_body_only_miss_skips_navigation_arms_and_preserves_full_retry() -> None:
    """The early pass must be cheap and must not consume the late fallback."""
    ctx = _ctx()
    page = MagicMock(name="live-page-must-not-reach-body-arms")
    with _mock_recoveries(leaseleads=[_plan("TIER_1_API_LEASELEADS_PLAN_LEVEL")]) as mocks:
        units, tier, name = await recover_universal_embed(
            page,
            ctx,
            body_only=True,
        )

    assert (units, tier, name) == ([], "", "")
    assert not getattr(ctx, "_embed_recovery_attempted", False)
    mocks["sightmap"].assert_not_called()
    mocks["generic_dom"].assert_not_called()
    # Page-capable body arms receive None, preventing browser navigation and
    # making the already-fetched response the sole discovery surface.
    assert mocks["appfolio"].await_args.args[0] is None
    assert mocks["leaseleads"].await_args.args[0] is None
    assert mocks["portal_hop"].await_args.args[0] is None
    assert mocks["g5"].await_args.args[0] is None


@pytest.mark.asyncio
async def test_body_only_unit_win_marks_full_chain_attempted() -> None:
    """A canonical apartment win is final even in the cheap early pass."""
    ctx = _ctx()
    recovered = [_unit("UNIT-701", "TIER_1_API_KNOCK_DNI_CONFIG")]
    with _mock_recoveries(knock_dni=recovered) as mocks:
        units, tier, name = await recover_universal_embed(
            MagicMock(),
            ctx,
            body_only=True,
        )

    assert units == recovered
    assert tier == "TIER_1_API_KNOCK_DNI_CONFIG"
    assert name == "knock_dni_config"
    assert getattr(ctx, "_embed_recovery_attempted", False)
    mocks["sightmap"].assert_not_called()
    mocks["generic_dom"].assert_not_called()


@pytest.mark.asyncio
async def test_exception_in_one_arm_does_not_hide_later_unit_route() -> None:
    """A broken SightMap arm must not make Rently and later arms unreachable."""
    rently_units = [_unit("HOME-1", "TIER_1_API_RENTLY")]
    with _mock_recoveries(rently=rently_units) as mocks:
        mocks["sightmap"].side_effect = RuntimeError("simulated SightMap failure")
        units, tier, name = await recover_universal_embed(None, _ctx())

    assert units == rently_units
    assert tier == "TIER_1_API_RENTLY"
    assert name == "rently"
    mocks["rently"].assert_awaited_once()
    mocks["g5"].assert_not_called()
    mocks["generic_dom"].assert_not_called()


@pytest.mark.asyncio
async def test_exception_in_last_unit_arm_still_reaches_generic_fallback() -> None:
    plans = [_plan()]
    with _mock_recoveries(generic_dom=(plans, "/floorplans")) as mocks:
        mocks["g5"].side_effect = RuntimeError("simulated G5 failure")
        units, tier, name = await recover_universal_embed(None, _ctx())

    assert units == plans
    assert tier == "TIER_3_DOM_GENERIC"
    assert name == "generic_dom"
    mocks["generic_dom"].assert_awaited_once()


@pytest.mark.asyncio
async def test_specific_route_tiers_are_preserved() -> None:
    portal_units = [_unit("P1", "TIER_1_API_RESMAN_PORTAL")]
    with _mock_recoveries(portal_hop=portal_units):
        units, tier, name = await recover_universal_embed(None, _ctx())

    assert units == portal_units
    assert tier == "TIER_1_API_RESMAN_PORTAL"
    assert name == "pms_portal_hop"
