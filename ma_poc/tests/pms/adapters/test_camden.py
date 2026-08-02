"""Camden exact public-detail roster tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from ma_poc.pms.adapters import camden as mod
from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.camden import (
    CAMDEN_DETAIL_TIER,
    CAMDEN_INCOMPLETE_TIER,
    CamdenAdapter,
    parse_camden_units,
)
from ma_poc.pms.detector import _STRATEGY_BY_PMS, detect_pms

BASE = "https://www.camdenliving.com/apartments/rockville-md/camden-fallsgrove"
CATALOGUE = f"{BASE}/available-apartments"
COMMUNITY = {
    "id": None,
    "name": "Camden Fallsgrove",
    "slug": "camden-fallsgrove",
    "realPageCommunityId": 1092761,
    "realPageParentCommunityId": 1092761,
    "address": "719 Fallsgrove Dr Rockville, MD 20850",
}


def _next_html(page_props: dict[str, Any]) -> str:
    payload = {"props": {"pageProps": page_props}}
    return (
        '<html><head><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></head><body>Camden</body></html>"
    )


def _plan(
    slug: str = "1_1e",
    name: str = "1.1E",
    floor_plan_id: int = 6,
    unit_number: str = "4140",
) -> dict[str, Any]:
    return {
        "availableUnits": 2,
        "availableUnitIds": [unit_number, "2041"],
        "realPageUnitId": 104,
        "realPageFloorPlanId": floor_plan_id,
        "bathrooms": "1",
        "bedrooms": "1",
        "name": name,
        "slug": slug,
        "squareFeet": 864,
        "leaseTerm": 12,
        "monthlyRent": 2189,
        "totalMonthlyRent": 2362.5,
        "moveInDate": "2026-08-03T00:00:00.000Z",
        "available": True,
        "unitName": unit_number,
        "unitNumber": unit_number,
    }


def _catalogue_html(
    plans: list[dict[str, Any]] | None = None,
    *,
    community: dict[str, Any] | None = None,
) -> str:
    return _next_html(
        {
            "citySlug": "rockville-md",
            "communitySlug": "camden-fallsgrove",
            "data": {
                "community": community or dict(COMMUNITY),
                "availableApartments": plans if plans is not None else [_plan()],
                "lastUpdate": "2026-08-02T00:54:13.701Z",
            },
        }
    )


def _unit(
    native_id: int,
    label: str,
    rent: int,
    *,
    community_id: int = 1092761,
    floor: int = 4,
    date: str = "2026-08-03T00:00:00.000Z",
) -> dict[str, Any]:
    return {
        "squareFeet": 864,
        "realPageCommunityId": community_id,
        "unitId": native_id,
        "unitName": label,
        "floorNumber": floor,
        "moveInDate": date,
        "leaseTerm": 12,
        "monthlyRent": rent,
        "totalMonthlyRent": None,
        "features": [],
    }


def _detail_html(
    plan: dict[str, Any] | None = None,
    units: list[dict[str, Any]] | None = None,
    *,
    community: dict[str, Any] | None = None,
) -> str:
    plan = plan or _plan()
    floor_plan = {
        "realPageFloorPlanId": plan["realPageFloorPlanId"],
        "name": plan["name"],
        "slug": plan["slug"],
        "squareFeet": plan["squareFeet"],
        "bedrooms": plan["bedrooms"],
        "bathrooms": plan["bathrooms"],
        "units": units
        if units is not None
        else [
            _unit(104, "4140", 2189),
            _unit(44, "2041", 2229),
        ],
    }
    detail_community = {
        **(community or COMMUNITY),
        "id": "4H3Z5bOu7pyt4rEgJDKoDG",
    }
    return _next_html(
        {
            "data": {
                "community": detail_community,
                "floorPlan": floor_plan,
                "floorPlans": [
                    {
                        "name": plan["name"],
                        "slug": plan["slug"],
                        "realPageFloorPlanId": plan["realPageFloorPlanId"],
                    }
                ],
                "citySlug": "rockville-md",
                "communitySlug": "camden-fallsgrove",
                "communityUrl": "/apartments/rockville-md/camden-fallsgrove",
                "floorPlanSlug": f"{plan['slug']}-floor-plan",
                "lastUpdate": "2026-08-02T00:54:13.701Z",
            }
        }
    )


def _detail_url(plan: dict[str, Any]) -> str:
    return (
        f"{CATALOGUE}/{plan['slug']}-floor-plan"
        f"?unit={plan['unitNumber']}&floor={plan['realPageFloorPlanId']}"
    )


class _Page:
    url = BASE


def _ctx(
    *,
    name: str = "Camden Fallsgrove",
    address: str = "719 Fallsgrove Dr",
) -> AdapterContext:
    return AdapterContext(
        base_url=f"{BASE}?utm_source=gmb#overview",
        detected=detect_pms(BASE),
        profile=None,
        expected_total_units=None,
        property_id="30997",
        property_name=name,
        address=address,
        city="Rockville",
        state="MD",
        zip_code="20850",
    )


def _fetched(url: str, body: str, status: int = 200) -> mod._FetchedPage:
    return mod._FetchedPage(url, url, status, body)


def _install_fetches(
    monkeypatch: pytest.MonkeyPatch,
    pages: dict[str, mod._FetchedPage],
) -> list[str]:
    calls: list[str] = []

    async def fake(url: str) -> mod._FetchedPage:
        calls.append(url)
        return pages.get(url, mod._FetchedPage(url, url, 404, ""))

    monkeypatch.setattr(mod, "_fetch_camden_page", fake)
    return calls


def test_landing_preview_parser_is_deliberately_disabled() -> None:
    assert parse_camden_units(_catalogue_html(), CATALOGUE) == []


def test_property_route_strips_tracking_and_binds_exact_property() -> None:
    route = mod._property_route(f"{BASE}?utm_source=gmb#overview")
    assert route is not None
    assert route.community_slug == "camden-fallsgrove"
    assert route.catalogue_url == CATALOGUE
    assert mod._property_route("https://camdenliving.com.evil.example/apartments/x/y") is None


@pytest.mark.asyncio
async def test_adapter_walks_exact_detail_and_preserves_unit_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    detail_url = _detail_url(plan)
    calls = _install_fetches(
        monkeypatch,
        {
            CATALOGUE: _fetched(CATALOGUE, _catalogue_html([plan])),
            detail_url: _fetched(detail_url, _detail_html(plan)),
        },
    )

    result = await CamdenAdapter().extract(_Page(), _ctx())  # type: ignore[arg-type]

    assert isinstance(result, AdapterResult)
    assert result.tier_used == CAMDEN_DETAIL_TIER
    assert len(result.units) == 2
    assert calls == [CATALOGUE, detail_url]
    first = next(row for row in result.units if row["unit_name"] == "4140")
    second = next(row for row in result.units if row["unit_name"] == "2041")
    assert first["unit_id"] == "camden_1092761_104"
    assert second["unit_id"] == "camden_1092761_44"
    assert first["source_ids"]["camden_community_unit_id"] == "1092761:104"
    assert first["floor_plan_name"] == "1.1E"
    assert first["_floor_plan_name_provenance"] == "camden.floorPlan.name"
    assert first["market_rent_low"] == first["market_rent_high"] == 2189
    assert second["market_rent_low"] == second["market_rent_high"] == 2229
    assert first["available_date"] == first["move_in_date"] == "2026-08-03"
    assert first["floor"] == "4"
    assert first["lease_term"] == "12"
    assert len(result.unit_source_provenance) == 1
    assert result.unit_source_provenance[0]["response_kind"] == "floor_plan_detail_union"
    assert result.unit_source_provenance[0]["unit_count"] == 2


@pytest.mark.asyncio
async def test_full_building_qualified_label_is_not_reduced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(unit_number="3D")
    detail_url = _detail_url(plan)
    _install_fetches(
        monkeypatch,
        {
            CATALOGUE: _fetched(CATALOGUE, _catalogue_html([plan])),
            detail_url: _fetched(
                detail_url,
                _detail_html(plan, [_unit(31, "8726                                               - 3D", 2199)]),
            ),
        },
    )
    result = await CamdenAdapter().extract(_Page(), _ctx())  # type: ignore[arg-type]
    assert len(result.units) == 1
    assert result.units[0]["unit_number"] == "8726 - 3D"
    assert result.units[0]["unit_name"] == "8726 - 3D"
    assert result.units[0]["building"] == "8726"


@pytest.mark.asyncio
async def test_community_qualified_ids_prevent_north_end_collisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_a = _plan("the-a1---contemporary", "The A1 - Contemporary", 2, "1236")
    plan_b = _plan("the-a1---modern", "The A1 - Modern", 2, "10209")
    urls = {_detail_url(plan_a), _detail_url(plan_b)}
    pages = {
        CATALOGUE: _fetched(CATALOGUE, _catalogue_html([plan_a, plan_b])),
        _detail_url(plan_a): _fetched(
            _detail_url(plan_a),
            _detail_html(plan_a, [_unit(100, "1236", 1700, community_id=4700479)]),
        ),
        _detail_url(plan_b): _fetched(
            _detail_url(plan_b),
            _detail_html(plan_b, [_unit(100, "10209", 1750, community_id=4282428)]),
        ),
    }
    calls = _install_fetches(monkeypatch, pages)
    result = await CamdenAdapter().extract(_Page(), _ctx())  # type: ignore[arg-type]
    assert len(result.units) == 2
    assert {row["unit_id"] for row in result.units} == {
        "camden_4700479_100",
        "camden_4282428_100",
    }
    assert set(calls[1:]) == urls


@pytest.mark.asyncio
async def test_configured_identity_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sibling = {
        **COMMUNITY,
        "name": "Camden Brookside",
        "address": "999 Different Ave Austin, TX 78701",
    }
    calls = _install_fetches(
        monkeypatch,
        {CATALOGUE: _fetched(CATALOGUE, _catalogue_html(community=sibling))},
    )
    result = await CamdenAdapter().extract(_Page(), _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0
    assert result.tier_used == CAMDEN_INCOMPLETE_TIER
    assert "identity mismatch" in result.errors[0]
    assert calls == [CATALOGUE]


@pytest.mark.asyncio
async def test_one_failed_detail_suppresses_entire_partial_roster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_a = _plan()
    plan_b = _plan("2_1", "2.1", 7, "6141")
    _install_fetches(
        monkeypatch,
        {
            CATALOGUE: _fetched(CATALOGUE, _catalogue_html([plan_a, plan_b])),
            _detail_url(plan_a): _fetched(_detail_url(plan_a), _detail_html(plan_a)),
            _detail_url(plan_b): _fetched(_detail_url(plan_b), "", status=503),
        },
    )
    result = await CamdenAdapter().extract(_Page(), _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.tier_used == CAMDEN_INCOMPLETE_TIER
    assert "incomplete exact detail walk (1/2 plans)" in result.errors[0]
    assert "2_1: detail fetch status=503" in result.errors[0]


def test_catalogue_over_bound_is_rejected_before_detail_fetch() -> None:
    plans = [_plan(f"p-{index}", f"Plan {index}", index + 1, str(index + 100)) for index in range(29)]
    route = mod._property_route(BASE)
    assert route is not None
    page = _fetched(CATALOGUE, _catalogue_html(plans))
    catalogue, error = mod._parse_catalogue(page, route, _ctx())
    assert catalogue is None
    assert "exceeds bounded maximum 28" in error


@pytest.mark.asyncio
async def test_source_to_final_keeps_exact_date_floor_term_and_base_rent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    detail_url = _detail_url(plan)
    _install_fetches(
        monkeypatch,
        {
            CATALOGUE: _fetched(CATALOGUE, _catalogue_html([plan])),
            detail_url: _fetched(detail_url, _detail_html(plan)),
        },
    )
    result = await CamdenAdapter().extract(_Page(), _ctx())  # type: ignore[arg-type]
    from ma_poc.scripts.runners.jugnu import _format_v2_unit

    final = _format_v2_unit(result.units[0], datetime(2026, 8, 2, tzinfo=UTC), "30997")
    assert final["unit_id"] == "camden_1092761_104"
    assert final["floor_plan_name"] == "1.1E"
    assert final["rent_low"] == final["rent_high"] == 2189.0
    assert final["available_date"] == "2026-08-03"
    assert final["availability_date_provenance"] == "explicit_future"
    assert final["floor"] == 4
    assert final["lease_term"] == 12


def test_detector_routes_camden_host_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_CAMDEN_ADAPTER", "true")
    detected = detect_pms(BASE)
    assert detected.pms == "camden"
    assert detected.confidence >= 0.90


def test_detector_remains_flag_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_CAMDEN_ADAPTER", "false")
    assert detect_pms(BASE).pms != "camden"


def test_strategy_and_registration() -> None:
    assert _STRATEGY_BY_PMS["camden"] == "dom_first"
    assert type(get_adapter("camden")).__name__ == "CamdenAdapter"
