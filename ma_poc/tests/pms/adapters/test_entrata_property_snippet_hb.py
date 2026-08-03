"""Exact property-owned Entrata website-snippet Hyperbrowser recovery."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ma_poc.fetch.hyperbrowser_backend import reset_hyperbrowser_property_counts
from ma_poc.pms.adapters import _entrata_hb_recovery as recovery
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import DetectedPMS

_PARENT_URL = (
    "https://www.scullycompany.com/apartments/pennsylvania/"
    "philadelphia/center-city/avenir/"
)
_INDEX_URL = (
    "https://avenir.scullycompany.com/philadelphia/avenir/"
    "?is_responsive_snippet=1&snippet_type=website&occupancy_type=1"
    "&host_domain=www.scullycompany.com"
)
_DETAIL_URL = (
    "https://avenir.scullycompany.com/Apartments/module/property_floorplans/"
    "property%5Bid%5D/100002834/property_floorplan[id]/2050/"
    "is_premium_view/1/is_responsive_snippet/1/occupancy_type/conventional/"
    "is_collapsed/0/snippet_type/website/"
)


def _parent_html(*, property_id: str = "100002834", host: str = "avenir.scullycompany.com") -> str:
    return f"""
    <html><body>
      <h1>Avenir Apartments on Fifteenth</h1>
      <address>Philadelphia, PA 19102</address>
      <iframe id="website_{property_id}"
        src="//{host}/philadelphia/avenir/?is_responsive_snippet=1&amp;snippet_type=website&amp;occupancy_type=1"></iframe>
    </body></html>
    """


def _index_html(*, property_id: str = "100002834") -> str:
    detail = _DETAIL_URL.replace("100002834", property_id)
    return f"""
    <html><head><title>Avenir Check Availability</title></head><body>
      <div class="fp-card"><div class="inner-card-container">
        <h2 class="fp-title">Studio 1 B | 321</h2>
        <a href="{detail}">View Details</a>
      </div></div>
    </body></html>
    """


def _detail_html(*, title: str = "Avenir Apartment Rentals") -> str:
    return f"""
    <html><head><title>{title}</title></head><body>
      <div class="fp-details-container"><h1>Studio 1 B | 321</h1>
        <span>Studio / 1 Bath</span></div>
      <div class="option-row title"><div>Unit</div></div>
      <div class="option-row">
        <div class="detail first"><span class="mobile-text">Unit</span> 1610</div>
        <div class="detail second"><span class="mobile-text">Building</span> 1</div>
        <div class="detail block"><span class="mobile-text">Floor</span> 16</div>
        <div class="detail second unit-rent-cell">
          <span class="unit-rent"><span class="mobile-text">Rent</span> $1,865</span>
        </div>
        <div class="detail block unit-sqft-cell">
          <span class="mobile-text">Sq.ft.</span> 321
        </div>
        <div class="detail block"><span class="mobile-text">Deposit</span> $1,000</div>
        <div class="detail block"><span class="mobile-text">Available</span> Now</div>
        <div class="detail action"><button class="js-show-details"
          data-floorplan="2050" data-unit="5222" data-date="08/01/2026">
          See Details</button></div>
      </div>
    </body></html>
    """


def _ctx(
    parent_html: str | None = None,
    *,
    address: str = "",
    final_url: str = _PARENT_URL,
) -> AdapterContext:
    ctx = AdapterContext(
        base_url="http://avenirphilly.com/",
        detected=DetectedPMS(
            pms="entrata",
            confidence=0.92,
            recommended_strategy="api_first",
        ),
        profile=None,
        expected_total_units=None,
        property_id="63191",
        fetch_result=SimpleNamespace(
            body=(parent_html or _parent_html()).encode(),
            final_url=final_url,
        ),
        property_name="Avenir on Fifteenth",
        address=address,
        city="Philadelphia",
        state="PA",
        zip_code="19102",
    )
    setattr(ctx, "_api_responses", [])
    return ctx


def test_exact_address_allows_provider_brand_alias() -> None:
    html = """
    <html><body>
      <h1>Avenir Apartments</h1>
      <address>42 South 15th Street, Philadelphia, PA 19102</address>
      <iframe id="website_100002834"
        src="//avenir.scullycompany.com/philadelphia/avenir/?is_responsive_snippet=1&amp;snippet_type=website&amp;occupancy_type=1"></iframe>
    </body></html>
    """

    target = recovery._property_owned_snippet_target(
        _ctx(html, address="42 S 15th St")
    )

    assert target is not None
    assert target.property_id == "100002834"
    assert target.provider_name_tokens == ("avenir",)


def test_alias_without_exact_street_boundary_is_rejected() -> None:
    html = """
    <html><body>
      <h1>Avenir Apartments</h1>
      <address>999 Other Road, Philadelphia, PA 19102</address>
      <iframe id="website_100002834"
        src="//avenir.scullycompany.com/philadelphia/avenir/?is_responsive_snippet=1&amp;snippet_type=website&amp;occupancy_type=1"></iframe>
    </body></html>
    """

    assert (
        recovery._property_owned_snippet_target(
            _ctx(html, address="42 S 15th St")
        )
        is None
    )


def test_protocol_relative_snippet_on_http_parent_upgrades_to_https() -> None:
    target = recovery._property_owned_snippet_target(
        _ctx(
            _parent_html(),
            final_url="http://www.scullycompany.com/apartments/avenir/",
        )
    )

    assert target is not None
    assert target.url.startswith("https://avenir.scullycompany.com/")


class _Page:
    def __init__(self, *, index_html: str | None = None, detail_html: str | None = None) -> None:
        self.url = _INDEX_URL
        self.index_html = index_html or _index_html()
        self.detail_html = detail_html or _detail_html()
        self.goto_calls: list[str] = []
        self.fetch_paths: list[str] = []

    async def goto(self, url: str, **_: Any) -> None:
        self.goto_calls.append(url)
        self.url = url

    async def content(self) -> str:
        return self.index_html

    async def title(self) -> str:
        return "Avenir Check Availability"

    async def evaluate(self, _script: str, path: str) -> dict[str, Any]:
        self.fetch_paths.append(path)
        return {"status": 200, "oversized": False, "body": self.detail_html}


class _Session:
    def __init__(self, page: _Page) -> None:
        self.page = page
        self.opened = False
        self.closed = False

    async def open(self) -> _Page:
        self.opened = True
        return self.page

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_one_session_recovers_property_scoped_native_units(monkeypatch) -> None:
    monkeypatch.setattr(recovery, "_HB_SETTLE_SECONDS", 0)
    reset_hyperbrowser_property_counts()
    page = _Page()
    session = _Session(page)

    outcome = await recovery.recover_entrata_hb_conventional(
        _ctx(),
        session_factory=lambda: session,
    )

    assert outcome.attempted
    assert [row["unit_number"] for row in outcome.units] == ["1610"]
    unit = outcome.units[0]
    assert unit["market_rent_low"] == 1865
    assert unit["sqft"] == "321"
    assert unit["floor"] == "16"
    assert unit["building"] == "1"
    assert unit["source_ids"] == {"entrata_uid": "5222", "entrata_fpid": "2050"}
    assert unit["source_property_id"] == "100002834"
    assert unit["source_property_name"] == "Avenir on Fifteenth"
    assert unit["source_property_provenance"] == (
        "exact_operator_published_entrata_website_snippet"
    )
    assert page.goto_calls == [_INDEX_URL]
    assert len(page.fetch_paths) == 1
    assert session.opened and session.closed


@pytest.mark.asyncio
async def test_foreign_detail_property_id_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(recovery, "_HB_SETTLE_SECONDS", 0)
    reset_hyperbrowser_property_counts()
    page = _Page(index_html=_index_html(property_id="999999999"))
    session = _Session(page)

    outcome = await recovery.recover_entrata_hb_conventional(
        _ctx(),
        session_factory=lambda: session,
    )

    assert outcome.attempted
    assert outcome.units == []
    assert outcome.failure_reason == "SNIPPET_DETAIL_BOUNDARY_REJECTED"
    assert page.fetch_paths == []
    assert session.closed


@pytest.mark.asyncio
async def test_foreign_child_host_never_opens_browser(monkeypatch) -> None:
    monkeypatch.setattr(recovery, "_HB_SETTLE_SECONDS", 0)
    reset_hyperbrowser_property_counts()
    page = _Page()
    session = _Session(page)

    outcome = await recovery.recover_entrata_hb_conventional(
        _ctx(_parent_html(host="avenir.example.net")),
        session_factory=lambda: session,
    )

    assert not outcome.attempted
    assert outcome.units == []
    assert not session.opened and not session.closed


@pytest.mark.asyncio
async def test_wrong_provider_identity_rejects_detail_rows(monkeypatch) -> None:
    monkeypatch.setattr(recovery, "_HB_SETTLE_SECONDS", 0)
    reset_hyperbrowser_property_counts()
    page = _Page(detail_html=_detail_html(title="Sibling Apartment Rentals"))
    session = _Session(page)

    outcome = await recovery.recover_entrata_hb_conventional(
        _ctx(),
        session_factory=lambda: session,
    )

    assert outcome.attempted
    assert outcome.units == []
    assert outcome.failure_reason == "SNIPPET_DETAIL_IDENTITY_REJECTED"
    assert session.closed
