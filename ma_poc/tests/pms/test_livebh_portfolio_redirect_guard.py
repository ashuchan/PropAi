"""Fail closed when a retired LiveBH URL redirects to a city portfolio page."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_module
from ma_poc.pms.scraper import (
    _livebh_retired_property_redirect_identity_rejected,
    scrape_jugnu,
)


def _fetch(final_url: str, visible_html: str) -> FetchResult:
    return FetchResult(
        url="https://livebh.com/apartments/retired-property/",
        outcome=FetchOutcome.OK,
        status=200,
        body=visible_html.encode(),
        headers={"content-type": "text/html"},
        render_mode=RenderMode.GET,
        final_url=final_url,
        attempts=1,
        elapsed_ms=1,
    )


@pytest.mark.parametrize(
    ("entry_url", "final_url", "row"),
    [
        (
            "https://livebh.com/apartments/flintridge-apartment-homes/",
            "https://livebh.com/apartments-in/arlington-tx/",
            {
                "name": "Flintridge",
                "address": "708 Woodard Way",
                "city": "Arlington",
                "state": "TX",
                "zip": "76011",
            },
        ),
        (
            "https://livebh.com/apartments/the-crossings-at-bramblewood/",
            "https://livebh.com/apartments-in/richmond-va/",
            {
                "name": "The Crossings at Bramblewood",
                "address": "1401 Yellowpine Cir",
                "city": "Richmond",
                "state": "VA",
                "zip": "23225",
            },
        ),
    ],
)
def test_two_verified_city_redirect_contaminations_are_rejected(
    entry_url: str,
    final_url: str,
    row: dict[str, str],
) -> None:
    sibling_page = """
    <html><head><script>
      const retiredCampaignMayMentionConfiguredName = 'flintridge bramblewood';
    </script></head><body>
      <h1>Apartments in this city</h1>
      <article><h2>Mateo Apartment Homes</h2></article>
      <article><h2>Tuckahoe Creek Apartments</h2></article>
    </body></html>
    """

    assert _livebh_retired_property_redirect_identity_rejected(
        entry_url,
        _fetch(final_url, sibling_page),
        row,
    )


def test_active_property_page_and_visible_exact_identity_are_allowed() -> None:
    parc_url = "https://livebh.com/apartments/parc-plaza-apartments/"
    parc_row = {
        "name": "Parc Plaza",
        "address": "333 E Denton Dr",
        "city": "Euless",
        "state": "TX",
        "zip": "76039",
    }
    assert not _livebh_retired_property_redirect_identity_rejected(
        parc_url,
        _fetch(
            parc_url,
            "<h1>Parc Plaza</h1><address>333 E Denton Dr</address>",
        ),
        parc_row,
    )

    city_url = "https://livebh.com/apartments-in/euless-tx/"
    assert not _livebh_retired_property_redirect_identity_rejected(
        parc_url,
        _fetch(
            city_url,
            "<article><h2>Parc Plaza</h2><address>333 E Denton Dr</address></article>",
        ),
        parc_row,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("property_id", "entry_url", "final_url", "row"),
    [
        (
            "8789",
            "https://livebh.com/apartments/flintridge-apartment-homes/",
            "https://livebh.com/apartments-in/arlington-tx/",
            {"name": "Flintridge", "address": "708 Woodard Way"},
        ),
        (
            "47182",
            "https://livebh.com/apartments/the-crossings-at-bramblewood/",
            "https://livebh.com/apartments-in/richmond-va/",
            {
                "name": "The Crossings at Bramblewood",
                "address": "1401 Yellowpine Cir",
            },
        ),
    ],
)
async def test_scrape_jugnu_stops_before_adapter_and_link_hop(
    monkeypatch: pytest.MonkeyPatch,
    property_id: str,
    entry_url: str,
    final_url: str,
    row: dict[str, str],
) -> None:
    async def must_not_scrape(**_kwargs):
        raise AssertionError("identity-rejected city landing reached an adapter")

    async def must_not_link_hop(**_kwargs):
        raise AssertionError("identity-rejected city landing reached link-hop")

    monkeypatch.setattr(scraper_module, "scrape", must_not_scrape)
    monkeypatch.setattr(scraper_module, "_try_link_hop", must_not_link_hop)
    fetch = _fetch(
        final_url,
        "<html><body><h1>Apartments in this city</h1>"
        "<a href='/apartments/sibling/floorplans'>Floor Plans</a>"
        "</body></html>",
    )
    task = SimpleNamespace(url=entry_url, property_id=property_id)

    result = await scrape_jugnu(task, fetch, csv_row=row)

    assert result["units"] == []
    assert result["plan_summaries"] == []
    assert result["extraction_tier_used"] == (
        "generic:portfolio_redirect_identity_rejected"
    )
    assert result["_extract_result"].records == []
    assert result["_portfolio_redirect_identity_rejected"]["adapters_skipped"]
    assert result["_portfolio_redirect_identity_rejected"]["link_hop_skipped"]
    assert any(
        "PORTFOLIO_REDIRECT_IDENTITY_REJECTED" in error
        for error in result["errors"]
    )
