"""Strict BetterNOI public-unit recovery tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters._betternoi_public import recover_betternoi_public
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.detector import DetectedPMS
from ma_poc.pms.source_provenance import context_unit_source_provenance

CLIENT = "01a0e491-f0fd-4d03-9529-00d881128a10"
FP1 = "c508d086-25cf-493b-a3d8-cf90c7fb9a9e"
FP2 = "f2cbe777-360d-4364-9166-c578663a831d"
PAGE_URL = "https://westwood.example/en/floor-plans/"


def _html(*pairs: tuple[str, str]) -> str:
    fragments = "".join(
        '\'<a class="individual-plan-button" '
        f'data-property="{client}" data-fpcode="{floorplan}" '
        'data-fpname="A1">View Availability</a>'
        for client, floorplan in pairs
    )
    return (
        "<html><body><h1>Westwood Village</h1>"
        "<p>2203 Beck Avenue, Panama City, FL 32405</p>"
        f"<script>const cards=[{fragments!r}];</script></body></html>"
    )


def _ctx(html: str) -> AdapterContext:
    return AdapterContext(
        base_url=PAGE_URL,
        detected=DetectedPMS(pms="encoreskyline_template", confidence=0.85),
        profile=None,
        expected_total_units=None,
        property_id="42571",
        fetch_result=SimpleNamespace(body=html, final_url=PAGE_URL),
        property_name="Westwood Village",
        address="2203 Beck Ave",
        city="Panama City",
        state="FL",
        zip_code="32405",
    )


def _row(
    unit_number: str = "C-06",
    uid: str = "a127a64c-31d4-473a-a50e-eb0290f63a6b",
    *,
    client: str = CLIENT,
    address: str = "2203 Beck Avenue",
    floorplan: str = FP1,
    zip_code: str = "32405",
) -> dict[str, Any]:
    return {
        "uuid": uid,
        "id": 748514,
        "client_uuid": client,
        "floor_plan": {"uuid": floorplan, "name": "1 Bedrooms, 1 Bathrooms"},
        "unit_number": unit_number,
        "min_square_feet": "750.00",
        "max_square_feet": "750.00",
        "min_rent": "1230.00",
        "max_rent": "1230.00",
        "bathroom_count": "1.00",
        "bedroom_count": "1.00",
        "adjusted_available_date": "2026-10-02",
        "building_address": address,
        "building_city": "Panama City",
        "building_postal_code": zip_code,
        "building_state": "FL",
        "availability_status": "available",
        "unit_to_skip": False,
    }


@pytest.mark.asyncio
async def test_exact_published_client_recovers_native_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters import _betternoi_public as module

    async def fake_fetch(url: str, referer: str):
        assert f"client_uuid={CLIENT}" in url
        assert referer == PAGE_URL
        return {"results": [_row()], "next": None}, url

    monkeypatch.setattr(module, "_fetch_betternoi_page", fake_fetch)
    ctx = _ctx(_html((CLIENT, FP1), (CLIENT, FP2)))
    rows = await recover_betternoi_public(ctx)

    assert len(rows) == 1
    assert rows[0]["unit_number"] == "C-06"
    assert rows[0]["market_rent_low"] == 1230
    assert rows[0]["availability_date"] == "2026-10-02"
    assert rows[0]["source_ids"]["betternoi_unit_uuid"]
    assert rows[0]["source_property_id"] == CLIENT
    assert rows[0]["source_property_provenance"] == ("exact_property_page_published_betternoi_client")
    provenance = context_unit_source_provenance(ctx)
    assert len(provenance) == 1
    assert provenance[0]["provider"] == "betternoi"
    assert provenance[0]["unit_count"] == 1
    assert provenance[0]["identity"]["status"] == "MATCH"
    assert provenance[0]["identity"]["betternoi_client_uuid"] == CLIENT


@pytest.mark.asyncio
async def test_multiple_published_clients_fail_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters import _betternoi_public as module

    async def forbidden(*_args: Any, **_kwargs: Any):
        raise AssertionError("ambiguous client set must not be fetched")

    monkeypatch.setattr(module, "_fetch_betternoi_page", forbidden)
    other = "11111111-2222-3333-4444-555555555555"
    rows = await recover_betternoi_public(_ctx(_html((CLIENT, FP1), (other, FP2))))
    assert rows == []


@pytest.mark.asyncio
async def test_foreign_payload_address_rejects_entire_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters import _betternoi_public as module

    async def fake_fetch(url: str, _referer: str):
        return {
            "results": [
                _row(),
                _row(
                    "X-01",
                    "bbbbbbbb-1111-2222-3333-cccccccccccc",
                    address="999 Sibling Road",
                ),
            ],
            "next": None,
        }, url

    monkeypatch.setattr(module, "_fetch_betternoi_page", fake_fetch)
    assert await recover_betternoi_public(_ctx(_html((CLIENT, FP1)))) == []


@pytest.mark.asyncio
async def test_duplicate_visible_unit_number_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters import _betternoi_public as module

    async def fake_fetch(url: str, _referer: str):
        return {
            "results": [
                _row(),
                _row("C-06", "bbbbbbbb-1111-2222-3333-cccccccccccc"),
            ],
            "next": None,
        }, url

    monkeypatch.setattr(module, "_fetch_betternoi_page", fake_fetch)
    assert await recover_betternoi_public(_ctx(_html((CLIENT, FP1)))) == []


@pytest.mark.asyncio
async def test_unrelated_zip_prefix_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters import _betternoi_public as module

    async def fake_fetch(url: str, _referer: str):
        return {"results": [_row(zip_code="99999")], "next": None}, url

    monkeypatch.setattr(module, "_fetch_betternoi_page", fake_fetch)
    assert await recover_betternoi_public(_ctx(_html((CLIENT, FP1)))) == []


@pytest.mark.asyncio
async def test_fetch_only_scrape_runs_exact_betternoi_bridge_with_body_resolver_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The native recovery must remain reachable on a link-hop sub-page.

    ``_try_link_hop`` deliberately calls ``scrape(page=None)`` to prevent
    recursive navigation, and production keeps the broad body resolver off.
    The exact BetterNOI marker/identity path is narrower and must still run.
    """
    from ma_poc.config import feature_flags
    from ma_poc.pms import scraper as scraper_module
    from ma_poc.pms.adapters import _betternoi_public as module

    async def fake_fetch(url: str, referer: str):
        assert f"client_uuid={CLIENT}" in url
        assert referer == PAGE_URL
        return {"results": [_row()], "next": None}, url

    class EmptyAdapter:
        def __init__(self, pms_name: str) -> None:
            self.pms_name = pms_name

        async def extract(self, _page: object, _ctx: AdapterContext) -> AdapterResult:
            return AdapterResult(errors=[f"{self.pms_name} returned no rows"])

        def static_fingerprints(self) -> list[str]:
            return []

    monkeypatch.setattr(module, "_fetch_betternoi_page", fake_fetch)
    monkeypatch.setattr(
        scraper_module,
        "get_adapter",
        lambda pms_name: EmptyAdapter(str(pms_name)),
    )
    monkeypatch.setattr(feature_flags, "ENABLE_BODY_RESOLVER", False)

    # ``meetelise`` matches the real Westwood template and prevents the
    # unrelated unknown-detection refetch seam from participating in this test.
    html = _html((CLIENT, FP1)).replace("<body>", "<body><script>window.meetelise = true;</script>")
    fetch_result = FetchResult(
        url=PAGE_URL,
        outcome=FetchOutcome.OK,
        status=200,
        body=html.encode(),
        headers={"content-type": "text/html"},
        render_mode=RenderMode.GET,
        final_url=PAGE_URL,
        attempts=1,
        elapsed_ms=1,
    )
    result = await scraper_module.scrape(
        PAGE_URL,
        page=None,
        fetch_result=fetch_result,
        property_id="42571",
        csv_row={
            "apartmentid": "42571",
            "name": "Westwood Village",
            "address": "2203 Beck Ave",
            "city": "Panama City",
            "state": "FL",
            "zip": "32405",
            "website": PAGE_URL,
        },
        shared_budget={
            "llm_api_calls": 0,
            "llm_dom_calls": 0,
            "llm_monolithic": 0,
            "link_hop": 0,
            "_cost_cap_usd": 0,
        },
    )

    assert result["_adapter_used"] == "betternoi_public"
    assert result["extraction_tier_used"] == "TIER_1_PUBLIC_BETTERNOI_API"
    assert len(result["units"]) == 1
    assert result["units"][0]["unit_number"] == "C-06"
    assert result["units"][0]["market_rent_low"] == 1230
    assert len(result["_unit_source_provenance"]) == 1
    assert result["_unit_source_provenance"][0]["provider"] == "betternoi"
    assert "page_published_native:betternoi_public" in result["_fallback_chain"]
