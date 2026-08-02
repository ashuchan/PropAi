"""Strict authored-route recovery for Squarespace shell properties."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.squarespace_nopms import (
    SquarespaceAuthoredPage,
    SquarespaceNoPmsAdapter,
    discover_squarespace_inventory_routes,
    fetch_squarespace_authored_page,
)
from ma_poc.pms.detector import detect_pms

_FIXDIR = Path(__file__).resolve().parents[2] / "fixtures" / "avail_table"

_THIRTYSIXTY_ROUTE = """
<html><head><title>Availability — 30Sixty</title></head><body>
<script>document.write("//carterres.appfolio.com/javascripts/listing.js")</script>
<script>Appfolio.Listing({
  hostUrl: 'carterres.appfolio.com',
  propertyGroup: '30Sixty Apts'
});</script>
</body></html>
"""

_THIRTYSIXTY_SSR = """
<html><body>
<div class="listing-item result js-listing-item" id="listing_554">
  <div class="listing-item__figure-container">
    <img class="listing-item__image" alt="3060 W. Olympic Blvd., # 522, Los Angeles, CA 90006">
  </div>
  <div class="listing-item__body">
    <div class="sidebar__price rent-banner__text js-listing-blurb-rent">$3,100</div>
    <span class="rent-banner__text js-listing-blurb-bed-bath">2 bd / 2 ba</span>
    <span class="u-space-rm js-listing-square-feet">Square Feet: 883</span>
    <span class="js-listing-available">Available Now</span>
    <span class="u-pad-rm js-listing-address">3060 W. Olympic Blvd., # 522, Los Angeles, CA 90006</span>
    <a class="js-listing-map-view-link" data-listing-id="554">Map</a>
  </div>
</div>
</body></html>
"""


def _ctx(body: str, base_url: str, *, property_id: str = "P_TEST") -> AdapterContext:
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url, page_html=body),
        profile=None,
        expected_total_units=None,
        property_id=property_id,
        fetch_result=SimpleNamespace(
            body=body.encode("utf-8"),
            final_url=base_url,
            network_log=[],
        ),
    )


def test_discovery_requires_visible_inventory_label_and_same_host() -> None:
    body = """
    <a href="/apartments">Apartments</a>
    <a href="/availability-copy">Availability</a>
    <a href="https://evil.example/availability">Check Availability</a>
    <a href="/pricing">Pricing</a>
    <a href="/all-floor-plans">All Floor Plans</a>
    <a href="javascript:openAvailability()">Availability</a>
    """
    assert discover_squarespace_inventory_routes(
        body,
        "https://www.30sixtyapts.com/",
    ) == [
        "https://www.30sixtyapts.com/availability-copy",
        "https://www.30sixtyapts.com/pricing",
    ]


def test_discovery_is_bounded_and_deduplicates_www_alias() -> None:
    body = """
    <a href="https://example.test/availability">Availability</a>
    <a href="/availability">Check Availability</a>
    <a href="/pricing">Pricing</a>
    <a href="/floor-plans">Floor Plans</a>
    """
    routes = discover_squarespace_inventory_routes(body, "https://www.example.test/")
    assert len(routes) == 2
    assert routes == [
        "https://example.test/availability",
        "https://www.example.test/pricing",
    ]


@pytest.mark.asyncio
async def test_fetch_is_direct_and_rejects_cross_host_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []

    def _get(_url: str, **kwargs: object) -> object:
        seen.append(kwargs)
        return SimpleNamespace(
            status_code=200,
            url="https://portfolio.example/availability",
            text="<html>foreign portfolio</html>",
        )

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _get)
    page = await fetch_squarespace_authored_page(
        "https://property.example/availability",
        entry_url="https://property.example/",
    )
    assert page is None
    assert seen == [{"unlocker": False, "retries": 1, "timeout": 20}]


@pytest.mark.asyncio
async def test_30sixty_authored_route_recovers_scoped_appfolio_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = '<html><body><p>Price: $1,940 a month</p><a href="/availability-copy">Availability</a></body></html>'
    ctx = _ctx(root, "https://www.30sixtyapts.com/", property_id="68505")
    ctx.property_name = "30Sixty Apartments"
    ctx.address = "3060 W Olympic Blvd"
    ctx.city = "Los Angeles"
    ctx.state = "CA"
    ctx.zip_code = "90006"

    async def _authored(_url: str, *, entry_url: str) -> SquarespaceAuthoredPage:
        assert entry_url == "https://www.30sixtyapts.com/"
        return SquarespaceAuthoredPage(
            url="https://www.30sixtyapts.com/availability-copy",
            body=_THIRTYSIXTY_ROUTE,
        )

    fetched: list[tuple[str, dict[str, object]]] = []

    def _get(url: str, **kwargs: object) -> object:
        fetched.append((url, kwargs))
        return SimpleNamespace(status_code=200, url=url, text=_THIRTYSIXTY_SSR)

    monkeypatch.setattr(
        "ma_poc.pms.adapters.squarespace_nopms.fetch_squarespace_authored_page",
        _authored,
    )
    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _get)

    result = await SquarespaceNoPmsAdapter().extract(None, ctx)  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_DOM_APPFOLIO_SSR"
    assert len(result.units) == 1
    [unit] = result.units
    assert unit["unit_number"] == "522"
    assert unit["source_ids"] == {"appfolio_listing_id": "554"}
    assert unit["market_rent_low"] == 3100
    assert unit["sqft"] == "883"
    assert unit["availability_date"] == "Available Now"
    assert all(u.get("floor_plan_name") != "Labeled Property Rent" for u in result.units)
    assert "filters%5Bproperty_list%5D=30Sixty%20Apts" in fetched[0][0]
    assert fetched[0][1].get("unlocker") is False


@pytest.mark.asyncio
async def test_authored_cricket_roster_outranks_generic_plan_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = """
    <html><body>
      <a href="/availability">Available Apartments</a>
      <div>One Bedroom from $2,800</div>
      <div>Two Bedroom from $3,600</div>
    </body></html>
    """
    cricket = (_FIXDIR / "cricket_flats.html").read_text(encoding="utf-8")
    ctx = _ctx(root, "https://cricketflats.com/", property_id="241432")

    async def _authored(_url: str, *, entry_url: str) -> SquarespaceAuthoredPage:
        assert entry_url == "https://cricketflats.com/"
        return SquarespaceAuthoredPage(
            url="https://cricketflats.com/availability",
            body=cricket,
        )

    monkeypatch.setattr(
        "ma_poc.pms.adapters.squarespace_nopms.fetch_squarespace_authored_page",
        _authored,
    )
    result = await SquarespaceNoPmsAdapter().extract(None, ctx)  # type: ignore[arg-type]

    assert len(result.units) == 8
    assert {unit["unit_number"] for unit in result.units} == {
        "406",
        "514",
        "303",
        "517",
        "217",
        "213",
        "318",
        "212",
    }
    assert result.plan_summaries == []
    assert result.unit_source_provenance[0]["source_url"].endswith("/availability")
