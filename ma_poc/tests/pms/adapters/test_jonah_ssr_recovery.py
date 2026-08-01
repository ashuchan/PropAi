"""Offline coverage for Jonah Systems SSR unit recovery.

The fixtures mirror July-31 cohort labels that were misrouted to different
primary adapters.  Every network-facing test replaces the plain fetch helper;
the suite never contacts a property site (or any proxy/unlocker service).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ma_poc.pms.adapters._encoreskyline_units import (
    JONAH_MAX_PLAN_URLS,
    JONAH_SSR_TIER,
    is_strong_jonah_generator_page,
    jonah_plan_urls_from_html,
    parse_jonah_ssr_units,
)
from ma_poc.pms.adapters._universal_recovery import (
    recover_jonah_ssr,
    recover_universal_embed,
)


def _unit_script(payload: dict[str, object], *, selector: str = "unit-data") -> str:
    return (
        '<script type="application/json" '
        f'data-jd-fp-selector="{selector}">{json.dumps(payload)}</script>'
    )


def _unit_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "unit",
        "id": 9828,
        "apartment_number": "508",
        "building": "",
        "floorplan_title": "A1",
        "bedrooms": "1",
        "bathrooms": "1",
        "square_feet": "820",
        "rent_min": "2100",
        "rent_max": "2150",
        "available_display": "Available Now",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def luma_generic_html() -> str:
    """LUMA was labelled generic_plan_text in the exact cohort."""
    return _unit_script(
        _unit_payload(
            apartment_number="4313",
            floorplan_title="Aria",
            price_entity={
                "date": "2026-08-05",
                "pricingReflectFees": True,
                "priceLow": 2710,
                "adjusted": {
                    "low_no_fees": "2485",
                    "high_no_fees": "2585",
                },
            },
        )
    )


@pytest.fixture
def julian_onesite_html() -> str:
    """The Julian was labelled OneSite but uses the same Jonah SSR shape."""
    return _unit_script(
        _unit_payload(
            apartment_number="4305",
            floorplan_title="Placid",
            rent_min="1405",
            rent_max="1405",
            available_date="1786856400",
        )
    )


@pytest.fixture
def cypress_knock_html() -> str:
    """Cypress Winds was labelled Knock and needs building+letter identity."""
    return _unit_script(
        _unit_payload(
            id=11111327,
            apartment_number="A",
            building="05",
            floorplan_title="Cypress",
            rent_min="1565",
            rent_max="1565",
        )
    )


@pytest.mark.parametrize(
    ("fixture_name", "unit_number", "rent"),
    [
        ("luma_generic_html", "4313", 2485),
        ("julian_onesite_html", "4305", 1405),
        ("cypress_knock_html", "05-A", 1565),
    ],
)
def test_parser_recovers_three_misrouted_labels(
    request: pytest.FixtureRequest,
    fixture_name: str,
    unit_number: str,
    rent: int,
) -> None:
    html = request.getfixturevalue(fixture_name)
    rows = parse_jonah_ssr_units(html, "https://example.test/floorplans/a1/")
    assert len(rows) == 1
    assert rows[0]["unit_number"] == unit_number
    assert rows[0]["market_rent_low"] == rent
    assert rows[0]["extraction_tier"] == JONAH_SSR_TIER


def test_parser_prefers_explicit_fee_free_base(luma_generic_html: str) -> None:
    row = parse_jonah_ssr_units(luma_generic_html, "https://example.test/a1/")[0]
    assert row["market_rent_low"] == 2485
    assert row["market_rent_high"] == 2585
    assert row["availability_date"] == "2026-08-05"


def test_parser_rejects_gross_price_only_unit() -> None:
    gross_only = _unit_payload(
        rent_min="2710",
        rent_max="2810",
        price_entity={
            "pricingReflectFees": True,
            "priceLow": 2710,
            "priceDisplay": "$2,710 including mandatory fees",
        },
    )
    assert parse_jonah_ssr_units(
        _unit_script(gross_only), "https://example.test/a1/"
    ) == []


def test_parser_rejects_malformed_plan_and_anchorless_rows() -> None:
    malformed = '<script data-jd-fp-selector="unit-data">{bad json</script>'
    plan = _unit_script(
        _unit_payload(type="floorplan", apartment_number="A1"),
    )
    anchorless = _unit_script(_unit_payload(apartment_number=""))
    wrong_selector = _unit_script(
        _unit_payload(apartment_number="999"), selector="floorplan-data"
    )
    assert parse_jonah_ssr_units(
        malformed + plan + anchorless + wrong_selector,
        "https://example.test/floorplans/",
    ) == []


def test_strong_generator_excludes_chat_widget_only() -> None:
    strong = '<meta content="Jonah Systems 8.4" name="generator">'
    chat_only = '<script>JonahWidget.meetelise({building: "abc"})</script>'
    assert is_strong_jonah_generator_page(strong)
    assert not is_strong_jonah_generator_page(chat_only)


def test_nested_plan_url_discovery_is_bounded() -> None:
    links = "".join(
        f'<a href="/apartments/florida/foo/floorplans/a-{i}/">A{i}</a>'
        for i in range(JONAH_MAX_PLAN_URLS + 5)
    )
    urls = jonah_plan_urls_from_html(links, "https://example.test/community/")
    assert len(urls) == JONAH_MAX_PLAN_URLS
    assert urls[0] == "https://example.test/apartments/florida/foo/floorplans/a-0/"


def _ctx(body: str, url: str = "https://luma.test/") -> SimpleNamespace:
    return SimpleNamespace(
        base_url=url,
        fetch_result=SimpleNamespace(body=body, final_url=url),
        property_id="P_JONAH",
    )


@pytest.mark.asyncio
async def test_recovery_synthesizes_index_and_drills_details_without_network(
    luma_generic_html: str,
) -> None:
    root = '<meta name="generator" content="Jonah Digital">'
    index = root + (
        '<a href="/floorplans/aria/">Aria</a>'
        '<a href="/floorplans/luna/">Luna</a>'
    )
    pages = {
        "https://luma.test/floorplans/": index,
        "https://luma.test/floorplans/aria/": luma_generic_html,
        "https://luma.test/floorplans/luna/": "<html>No current units</html>",
    }
    calls: list[list[str]] = []

    async def fake_fetch(urls: list[str]) -> list[tuple[str, int, str, str]]:
        calls.append(list(urls))
        return [(url, 200, pages.get(url, ""), url) for url in urls]

    with patch(
        "ma_poc.pms.adapters._universal_recovery._fetch_jonah_html_pages",
        side_effect=fake_fetch,
    ):
        rows = await recover_jonah_ssr(_ctx(root))  # type: ignore[arg-type]

    assert [row["unit_number"] for row in rows] == ["4313"]
    assert calls == [
        ["https://luma.test/floorplans/"],
        [
            "https://luma.test/floorplans/aria/",
            "https://luma.test/floorplans/luna/",
        ],
    ]


@pytest.mark.asyncio
async def test_recovery_zero_unit_pages_remain_empty() -> None:
    root = (
        '<meta name="generator" content="Jonah Systems">'
        '<a href="/floorplans/waitlist/">Waitlist</a>'
    )
    plan_only = _unit_script(
        _unit_payload(type="floorplan", apartment_number="A1")
    )

    async def fake_fetch(urls: list[str]) -> list[tuple[str, int, str, str]]:
        return [(url, 200, plan_only, url) for url in urls]

    with patch(
        "ma_poc.pms.adapters._universal_recovery._fetch_jonah_html_pages",
        side_effect=fake_fetch,
    ):
        assert await recover_jonah_ssr(_ctx(root)) == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_recovery_without_strong_marker_never_fetches() -> None:
    ctx = _ctx('<script src="https://chat.meetelise.com/widget.js"></script>')
    with patch(
        "ma_poc.pms.adapters._universal_recovery._fetch_jonah_html_pages",
        new_callable=AsyncMock,
    ) as fetch:
        assert await recover_jonah_ssr(ctx) == []  # type: ignore[arg-type]
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_unit_wins_without_running_jonah_recovery() -> None:
    native = [
        {
            "floor_plan_name": "A1",
            "unit_number": "101",
            "market_rent_low": 1500,
            "extraction_tier": "TIER_1_API_NATIVE",
        }
    ]
    ctx = SimpleNamespace()
    with patch(
        "ma_poc.pms.adapters._appfolio_embed.recover_appfolio_embed",
        new=AsyncMock(return_value=native),
    ), patch(
        "ma_poc.pms.adapters._universal_recovery.recover_jonah_ssr",
        new_callable=AsyncMock,
    ) as jonah:
        rows, _tier, winner = await recover_universal_embed(
            SimpleNamespace(), ctx  # type: ignore[arg-type]
        )
    assert rows == native
    assert winner == "appfolio_embed"
    jonah.assert_not_awaited()
