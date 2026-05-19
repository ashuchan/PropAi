"""AppFolio-embed recovery for Wix/Squarespace shells (2026-05-19).

Verified live on brooksidejohnsoncreek.com (Squarespace) → /listings →
iframe https://illumepm.appfolio.com/listings (69 data-listing-id blocks).
The recovery scans the live page / probes known sub-paths, finds the
AppFolio iframe, fetches it in-session, and reuses parse_appfolio_listings_ssr.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters._appfolio_embed import recover_appfolio_embed
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.squarespace_nopms import SquarespaceNoPmsAdapter
from ma_poc.pms.adapters.wix_nopms import WixNoPmsAdapter
from ma_poc.pms.detector import detect_pms

# Production-shape AppFolio listings SSR (same js-listing-* shape the live
# illumepm.appfolio.com/listings page emits).
_APPFOLIO_SSR = """
<html><body>
<article class="listing-item js-listing-item" data-listing-id="1386">
  <div class="js-listing-blurb-rent">$1,695</div>
  <div class="js-listing-blurb-bed-bath">2 bd / 1 ba</div>
  <div class="js-listing-square-feet">Square Feet: 900</div>
  <div class="js-listing-available">6/01/26</div>
  <div class="js-listing-address"><span>10 Creek Rd</span></div>
</article>
<article class="listing-item js-listing-item" data-listing-id="1037">
  <div class="js-listing-blurb-rent">$1,350</div>
  <div class="js-listing-blurb-bed-bath">Studio / 1 ba</div>
  <div class="js-listing-square-feet">Square Feet: 540</div>
  <div class="js-listing-available">5/22/26</div>
  <div class="js-listing-address"><span>12 Creek Rd</span></div>
</article>
</body></html>
"""

_IFRAME_SRC = "https://illumepm.appfolio.com/listings?1234567890"
_SUBPAGE_HTML = (
    '<html><body><div class="sqs-block">'
    f'<iframe src="{_IFRAME_SRC}" width="100%"></iframe>'
    "</div></body></html>"
)


class _FakePage:
    """evaluate() dispatches: no-arg call = live AppFolio-src scan;
    1-arg call = in-session fetch(url) → body string."""

    def __init__(self, url: str, live: list[str], responses: dict[str, str]) -> None:
        self.url = url
        self._live = live
        self._responses = responses

    async def evaluate(self, _js: str, *args: object) -> object:
        if not args:
            return list(self._live)
        url = str(args[0])
        return self._responses.get(url, "")


def _ctx(base_url: str) -> AdapterContext:
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )


@pytest.mark.asyncio
async def test_recover_via_live_iframe_on_page() -> None:
    page = _FakePage(
        url="https://www.brooksidejohnsoncreek.com/listings",
        live=[_IFRAME_SRC],
        responses={_IFRAME_SRC: _APPFOLIO_SSR},
    )
    units = await recover_appfolio_embed(page, _ctx("https://www.brooksidejohnsoncreek.com/"))  # type: ignore[arg-type]
    assert len(units) == 2
    assert units[0]["extraction_tier"] == "TIER_1_DOM_APPFOLIO_SSR"
    assert units[0]["bedrooms"] == "2"
    assert units[1]["bedrooms"] == "0"  # Studio


@pytest.mark.asyncio
async def test_recover_via_subpath_probe() -> None:
    """No iframe on the current page → probe sub-paths, find it in /listings."""
    page = _FakePage(
        url="https://www.brooksidejohnsoncreek.com/",
        live=[],
        responses={
            "https://www.brooksidejohnsoncreek.com/listings": _SUBPAGE_HTML,
            _IFRAME_SRC: _APPFOLIO_SSR,
        },
    )
    units = await recover_appfolio_embed(page, _ctx("https://www.brooksidejohnsoncreek.com/"))  # type: ignore[arg-type]
    assert len(units) == 2
    assert units[0]["market_rent_low"] == 1695


@pytest.mark.asyncio
async def test_no_appfolio_returns_empty() -> None:
    """Genuine no-PMS shell: nothing matched → [] (no false positives)."""
    page = _FakePage(
        url="https://www.plainsquarespacesite.com/",
        live=[],
        responses={"https://www.plainsquarespacesite.com/listings": "<html><body>no widget</body></html>"},
    )
    units = await recover_appfolio_embed(page, _ctx("https://www.plainsquarespacesite.com/"))  # type: ignore[arg-type]
    assert units == []


@pytest.mark.asyncio
async def test_recover_handles_pageless_stub() -> None:
    """A page stub without evaluate() degrades to [] (never raises)."""

    class _Bare:
        url = "https://x.com/"

    units = await recover_appfolio_embed(_Bare(), _ctx("https://x.com/"))  # type: ignore[arg-type]
    assert units == []


@pytest.mark.asyncio
async def test_squarespace_adapter_recovers_appfolio_embed() -> None:
    page = _FakePage(
        url="https://www.brooksidejohnsoncreek.com/listings",
        live=[_IFRAME_SRC],
        responses={_IFRAME_SRC: _APPFOLIO_SSR},
    )
    result = await SquarespaceNoPmsAdapter().extract(page, _ctx("https://www.brooksidejohnsoncreek.com/"))  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_APPFOLIO_SSR"
    assert len(result.units) == 2
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_squarespace_adapter_still_deadends_when_no_embed() -> None:
    page = _FakePage(url="https://www.plain.com/", live=[], responses={})
    result = await SquarespaceNoPmsAdapter().extract(page, _ctx("https://www.plain.com/"))  # type: ignore[arg-type]
    assert result.tier_used == "SYNDICATION_ONLY_SQUARESPACE"
    assert result.units == []


@pytest.mark.asyncio
async def test_wix_adapter_recovers_appfolio_embed() -> None:
    page = _FakePage(
        url="https://www.villasonrock.com/",
        live=[],
        responses={
            "https://www.villasonrock.com/availability": _SUBPAGE_HTML,
            _IFRAME_SRC: _APPFOLIO_SSR,
        },
    )
    result = await WixNoPmsAdapter().extract(page, _ctx("https://www.villasonrock.com/"))  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_APPFOLIO_SSR"
    assert len(result.units) == 2


def test_detector_appfolio_iframe_beats_squarespace_shell() -> None:
    """Squarespace marketing HTML that embeds an AppFolio listings iframe
    routes to appfolio (strong pass-1), not squarespace_nopms."""
    html = (
        '<html><head><script src="https://static1.squarespace.com/x.js"></script>'
        '</head><body><iframe src="https://illumepm.appfolio.com/listings?123">'
        "</iframe></body></html>"
    )
    det = detect_pms("https://www.brooksidejohnsoncreek.com/listings", page_html=html)
    assert det.pms == "appfolio"


def test_detector_plain_squarespace_still_nopms() -> None:
    html = '<html><head><script src="https://static1.squarespace.com/x.js"></script></head><body></body></html>'
    det = detect_pms("https://www.plain.com/", page_html=html)
    assert det.pms == "squarespace_nopms"
