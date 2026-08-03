"""Funnel/Engrain Spaces SSR cross-route recovery.

The live validation set for the generalized schema is Arrivé Seattle
(``spaces__unit``), Windsor Addison and Windsor Sugarloaf
(``spaces-unit``). These offline tests preserve the exact class/schema and
the authored same-origin route/property boundary.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters import funnel
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import DetectedPMS


def _unit_card(
    unit: str,
    *,
    css_class: str = "spaces__unit",
    unit_id: str = "3372874",
    plan: str = "Floorplan A-11",
    price: str = "3215",
) -> str:
    return f"""
    <article class="{css_class} price_3000-4000"
      data-spaces-id="{unit_id}" data-spaces-asset="3137"
      data-spaces-available="true" data-spaces-community="Arrivé Seattle"
      data-spaces-obj="unit" data-spaces-plan-id="188659"
      data-spaces-soonest="2099-08-25" data-spaces-sort-area="755"
      data-spaces-sort-bath="1" data-spaces-sort-bed="1"
      data-spaces-sort-plan-name="{plan}" data-spaces-sort-price="{price}"
      data-spaces-unit="{unit}" data-spaces-unit-id="{unit_id}">
      <span aria-label="Available Date">Available Aug 25, 2099</span>
    </article>
    """


def _ctx(body: str) -> AdapterContext:
    return AdapterContext(
        base_url="https://www.arrive.test/",
        detected=DetectedPMS(pms="encoreskyline_template", confidence=0.9),
        profile=None,
        expected_total_units=None,
        property_id="77558",
        fetch_result=SimpleNamespace(
            body=body.encode("utf-8"),
            final_url="https://www.arrive.test/",
        ),
        property_name="Arrivé Seattle",
        address="2116 4th Ave",
        city="Seattle",
        state="WA",
        zip_code="98121",
    )


@pytest.mark.parametrize("css_class", ("spaces-unit", "spaces__unit"))
def test_parser_accepts_only_the_two_verified_unit_classes(css_class: str) -> None:
    rows = funnel.parse_funnel_spaces_ssr(
        _unit_card("1115", css_class=css_class),
        "https://www.arrive.test/apartments/",
    )

    assert len(rows) == 1
    assert rows[0]["unit_number"] == "1115"
    assert rows[0]["floor_plan_name"] == "Floorplan A-11"
    assert rows[0]["market_rent_low"] == 3215
    assert rows[0]["availability_date"] == "2099-08-25"
    assert rows[0]["unit_id"] == "3372874"
    assert rows[0]["unit_name"] == "1115"
    assert rows[0]["source_ids"] == {
        "funnel_spaces_unit_id": "3372874",
        "funnel_spaces_plan_id": "188659",
        "funnel_spaces_asset_id": "3137",
    }
    assert rows[0]["source_property_id"] == "3137"
    assert rows[0]["source_property_name"] == "Arrivé Seattle"
    assert rows[0]["source_property_provenance"] == "funnel_spaces_ssr_article"
    assert unit_has_real_anchor(rows[0])


def test_parser_rejects_class_lookalikes_and_plan_cards() -> None:
    assert (
        funnel.parse_funnel_spaces_ssr(
            _unit_card("1115", css_class="spaces__unitized"),
            "https://www.arrive.test/apartments/",
        )
        == []
    )
    plan = _unit_card("1115").replace('data-spaces-obj="unit"', 'data-spaces-obj="plan"')
    assert funnel.parse_funnel_spaces_ssr(plan, "https://www.arrive.test/") == []


def test_inventory_route_requires_verified_marker_unique_authored_same_origin() -> None:
    source = """
      <script src="/wp-content/plugins/ecs-spaces/public/assets/spaces_scripts.js"></script>
      <a href="https://www.arrive.test/apartments/">Apartments</a>
      <a href="https://www.arrive.test/apartments/?spaces_tab=plans">Plans</a>
    """
    assert funnel._spaces_floorplans_url(source, "https://www.arrive.test/") == (
        "https://www.arrive.test/apartments/"
    )

    assert (
        funnel._spaces_floorplans_url(
            '<a href="https://www.arrive.test/apartments/">Apartments</a>',
            "https://www.arrive.test/",
        )
        is None
    )
    assert (
        funnel._spaces_floorplans_url(
            '<script src="/wp-content/plugins/ecs-spaces/x.js"></script>'
            '<a href="https://foreign.test/apartments/">Apartments</a>',
            "https://www.arrive.test/",
        )
        is None
    )
    assert (
        funnel._spaces_floorplans_url(
            '<script src="/wp-content/plugins/ecs-spaces/x.js"></script>'
            '<a href="/apartments/">One</a><a href="/floorplans/">Two</a>',
            "https://www.arrive.test/",
        )
        is None
    )


@pytest.mark.asyncio
async def test_crossroute_recovers_native_units_from_one_authored_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = """
      <script src="/wp-content/plugins/ecs-spaces/public/assets/spaces_scripts.js"></script>
      <a href="https://www.arrive.test/apartments/">Apartments</a>
    """
    inventory = _unit_card("1115") + _unit_card("3820", unit_id="3373187", plan="Floorplan X", price="4295")

    async def fake_fetch(url: str) -> tuple[str, str]:
        assert url == "https://www.arrive.test/apartments/"
        return inventory, url

    monkeypatch.setattr(funnel, "_fetch_spaces_inventory", fake_fetch)

    rows = await funnel.recover_funnel_spaces(_ctx(source))

    assert {row["unit_number"] for row in rows} == {"1115", "3820"}
    assert {row["unit_id"] for row in rows} == {"3372874", "3373187"}
    assert all(unit_has_real_anchor(row) for row in rows)
    assert all(row["source_api_url"] == "https://www.arrive.test/apartments/" for row in rows)


@pytest.mark.asyncio
async def test_unmarked_or_cross_origin_shell_never_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def fake_fetch(_url: str) -> None:
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(funnel, "_fetch_spaces_inventory", fake_fetch)
    rows = await funnel.recover_funnel_spaces(
        _ctx('<a href="https://foreign.test/apartments/">Apartments</a>')
    )

    assert rows == []
    assert called is False
