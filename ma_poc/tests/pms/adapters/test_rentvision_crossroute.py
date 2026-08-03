"""Narrow cross-route recovery for exact RentVision CMS pages."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
import pytest

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters import rentvision
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import detect_pms

_FIXTURE = Path(__file__).parent / "fixtures/rentvision/crossroute_live_signatures.json"
_LIVE_CASES = json.loads(_FIXTURE.read_text())
_CMS_MARKER = "<footer>Website created by RentVision</footer>"


def _ctx(source_url: str, property_id: str, body: str = _CMS_MARKER) -> AdapterContext:
    return AdapterContext(
        base_url=source_url,
        detected=detect_pms(source_url),
        profile=None,
        expected_total_units=None,
        property_id=property_id,
        fetch_result=SimpleNamespace(body=body, final_url=source_url),
    )


def _detail_html(rows: list[list[object]]) -> str:
    return "".join(
        (
            f'<tr><th class="left wrap">{unit_number}</th>'
            '<td class="standard identifiable-links right">'
            f"<span>${rent_value:,}</span></td>"
            '<td class="standard unit-availability">Available Now</td></tr>'
        )
        for unit_number, rent_value in rows
    )


@pytest.mark.parametrize(
    "case",
    _LIVE_CASES,
    ids=lambda case: f"pid-{case['property_id']}",
)
@pytest.mark.asyncio
async def test_three_live_signatures_recover_strict_unit_rosters(
    monkeypatch: pytest.MonkeyPatch,
    case: dict[str, object],
) -> None:
    """Replay the exact plan paths/unit+rent values probed on all 3 hits."""
    source_url = str(case["source_url"])
    origin = source_url.rstrip("/")
    plan_urls = list(case["plan_urls"])
    units_by_plan = dict(case["units_by_plan"])
    expected_count = sum(len(rows) for rows in units_by_plan.values())
    index_html = _CMS_MARKER + "".join(f'<a href="{path}">plan</a>' for path in plan_urls)

    async def fake_fetch(
        urls: list[str],
        allowed_host: str,
        **_kwargs: object,
    ) -> list[tuple[str, int, str, str]]:
        assert allowed_host == rentvision._normalized_host(source_url)
        out: list[tuple[str, int, str, str]] = []
        for url in urls:
            path = urlparse(url).path
            html = index_html if path == "/floorplans" else _detail_html(units_by_plan.get(path, []))
            out.append((url, 200, html, f"{origin}{path}"))
        return out

    monkeypatch.setattr(rentvision, "_fetch_rentvision_html_pages", fake_fetch)

    rows = await rentvision.recover_rentvision_crossroute(_ctx(source_url, str(case["property_id"])))

    assert len(rows) == expected_count
    assert all(unit_has_real_anchor(row) for row in rows)
    assert all(float(row["market_rent_low"]) > 0 for row in rows)
    assert {row["unit_number"] for row in rows} == {
        unit_number for plan_rows in units_by_plan.values() for unit_number, _ in plan_rows
    }


def test_marker_gate_is_exact_not_any_rentvision_link() -> None:
    assert rentvision.is_strong_rentvision_cms_html("Website created by RentVision")
    assert rentvision.is_strong_rentvision_cms_html("Website Powered By RentVision")
    assert rentvision.is_strong_rentvision_cms_html("websitePoweredByRentVision.png")
    assert not rentvision.is_strong_rentvision_cms_html(
        '<a href="https://www.rentvision.com/blog">vendor article</a>'
    )


@pytest.mark.asyncio
async def test_no_exact_marker_never_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden_fetch(*_args: object, **_kwargs: object) -> list[object]:
        raise AssertionError("cross-route fetch must remain marker gated")

    monkeypatch.setattr(rentvision, "_fetch_rentvision_html_pages", forbidden_fetch)
    ctx = _ctx(
        "https://example.com/",
        "negative-marker",
        '<a href="https://rentvision.com">RentVision</a>',
    )
    assert await rentvision.recover_rentvision_crossroute(ctx) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("no-rent", "cross-host"))
async def test_recovery_rejects_no_rent_and_cross_property_scope(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    source_url = "https://scope.example/"
    detail_url = "https://scope.example/floorplans/one-bedroom/a1"
    index_html = _CMS_MARKER + '<a href="/floorplans/one-bedroom/a1">A1</a>'

    async def fake_fetch(
        urls: list[str],
        _allowed_host: str,
        **_kwargs: object,
    ) -> list[tuple[str, int, str, str]]:
        if urls == ["https://scope.example/floorplans"]:
            return [(urls[0], 200, index_html, urls[0])]
        final_url = "https://other-property.example/floorplans/one-bedroom/a1"
        if failure == "no-rent":
            final_url = detail_url
        return [(detail_url, 200, _detail_html([["101", 0]]), final_url)]

    monkeypatch.setattr(rentvision, "_fetch_rentvision_html_pages", fake_fetch)
    assert await rentvision.recover_rentvision_crossroute(_ctx(source_url, failure)) == []


@pytest.mark.asyncio
async def test_plain_http_fetch_follows_only_same_property_redirects() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "example.com":
            return httpx.Response(
                301,
                headers={"location": "https://www.example.com/floorplans"},
            )
        return httpx.Response(200, text=_CMS_MARKER)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as client:
        result = await rentvision._fetch_rentvision_html_pages(
            ["http://example.com/floorplans"],
            "example.com",
            client=client,
        )

    assert requests == [
        "http://example.com/floorplans",
        "https://www.example.com/floorplans",
    ]
    assert result[0][1:] == (200, _CMS_MARKER, "https://www.example.com/floorplans")


@pytest.mark.asyncio
async def test_plain_http_fetch_does_not_request_cross_host_redirect() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://other-property.example/floorplans"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as client:
        result = await rentvision._fetch_rentvision_html_pages(
            ["https://example.com/floorplans"],
            "example.com",
            client=client,
        )

    assert requests == ["https://example.com/floorplans"]
    assert result[0][2] == ""
    assert result[0][3] == "https://other-property.example/floorplans"
