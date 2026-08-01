"""ManageBuilding public-rentals index recovery.

The complete ``/Resident/public/rentals`` route publishes one
``a.featured-listing`` per active listing. ``/Resident/public/home`` uses the
same card markup for only a featured subset, so it must never promote those
cards or the link-hop planner will stop before reaching the full roster.
"""

from __future__ import annotations

import asyncio

import pytest

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.extraction.classify import classify
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters._html_extract import (
    extract_managebuilding_rentals_index,
    is_managebuilding_rentals_index_url,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.generic import GenericAdapter
from ma_poc.pms.detector import DetectedPMS


def _card(
    listing_id: str,
    *,
    bedrooms: str = "2",
    bathrooms: str = "2",
    rent: str = "1,229.00",
    sqft: str = "844",
    href: str | None = None,
) -> str:
    detail_href = href if href is not None else f"/Resident/public/rentals/{listing_id}"
    return f"""
      <a class="featured-listing accent-color-border-on-hover"
         href="{detail_href}"
         data-bedrooms="{bedrooms}"
         data-bathrooms="{bathrooms}"
         data-rent="{rent}"
         data-square-feet="{sqft}"
         data-type="MultiFamily">
        <p class="featured-listing__features">
          {bedrooms} Bed | {bathrooms} Bath | {sqft} sqft
        </p>
        <p class="featured-listing__price">${rent}</p>
      </a>
    """


_INDEX_HTML = f"""
<html><body><div id="rentals-container">
  {_card("113015", rent="1,289.00")}
  {_card("76796")}
  {_card("76796")} <!-- duplicate render of the same listing -->
  {_card("", href="/Resident/public/rentals/not-a-number")}
  {_card("999", href="https://other.managebuilding.com/Resident/public/rentals/999")}
  <a class="featured-listing"
     href="/Resident/public/rentals/555"
     data-bedrooms="1" data-bathrooms="1" data-rent="1129.00">
     Missing square feet
  </a>
</div></body></html>
"""

_COMPLETE_INDEX_URL = (
    "https://lemirageapts.managebuilding.com/Resident/public/rentals"
)
_HOME_URL = "https://lemirageapts.managebuilding.com/Resident/public/home"


@pytest.mark.parametrize(
    "url",
    [
        _COMPLETE_INDEX_URL,
        _COMPLETE_INDEX_URL + "/",
        "https://lemirageapts.managebuilding.com/resident/PUBLIC/Rentals?beds=2",
    ],
)
def test_index_url_gate_accepts_only_complete_route_variants(url: str) -> None:
    assert is_managebuilding_rentals_index_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        _HOME_URL,
        _COMPLETE_INDEX_URL + "/76796",
        "https://managebuilding.com/Resident/public/rentals",
        "https://managebuilding.com.evil.test/Resident/public/rentals",
        "https://example.com/Resident/public/rentals",
        "not a url",
    ],
)
def test_index_url_gate_rejects_wrong_host_or_incomplete_route(url: str) -> None:
    assert is_managebuilding_rentals_index_url(url) is False


def test_parser_extracts_unique_listing_rows_with_volatile_source_ids() -> None:
    units = extract_managebuilding_rentals_index(
        _INDEX_HTML, _COMPLETE_INDEX_URL
    )

    assert len(units) == 2
    by_listing = {
        unit["source_ids"]["managebuilding_listing_id"]: unit for unit in units
    }
    assert set(by_listing) == {"113015", "76796"}

    first = by_listing["113015"]
    assert first["bedrooms"] == "2"
    assert first["bathrooms"] == "2"
    assert first["sqft"] == "844"
    assert first["market_rent_low"] == 1289
    assert first["market_rent_high"] == 1289
    assert first["rent_range"] == "$1,289"
    assert first["availability_status"] == "AVAILABLE"
    assert first["source_api_url"].endswith("/Resident/public/rentals/113015")
    assert first["extraction_tier"] == "TIER_1_DOM_MANAGEBUILDING"
    assert not first.get("unit_id")
    assert not first.get("unit_number")
    assert unit_has_real_anchor(first) is True
    assert classify(first) == "unit"


def test_parser_preserves_fractional_bathrooms() -> None:
    html = f"<div>{_card('24680', bathrooms='2.5')}</div>"

    units = extract_managebuilding_rentals_index(html, _COMPLETE_INDEX_URL)

    assert len(units) == 1
    assert units[0]["bathrooms"] == "2.5"


def test_parser_does_not_promote_featured_subset_on_homepage() -> None:
    assert extract_managebuilding_rentals_index(_INDEX_HTML, _HOME_URL) == []


def test_parser_rejects_wrong_host_and_missing_numeric_listing_id() -> None:
    assert (
        extract_managebuilding_rentals_index(
            _INDEX_HTML, "https://example.com/Resident/public/rentals"
        )
        == []
    )
    missing_id_html = f"<div>{_card('', href='/Resident/public/rentals/')}</div>"
    assert (
        extract_managebuilding_rentals_index(
            missing_id_html, _COMPLETE_INDEX_URL
        )
        == []
    )


def _context(
    html: str,
    *,
    base_url: str,
    final_url: str,
) -> AdapterContext:
    fetch_result = FetchResult(
        url=base_url,
        outcome=FetchOutcome.OK,
        status=200,
        body=html.encode("utf-8"),
        headers={"content-type": "text/html"},
        render_mode=RenderMode.GET,
        final_url=final_url,
        attempts=1,
        elapsed_ms=1,
    )
    ctx = AdapterContext(
        base_url=base_url,
        detected=DetectedPMS(pms="unknown", confidence=0.0),
        profile=None,
        expected_total_units=None,
        property_id="18684",
        fetch_result=fetch_result,
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    return ctx


def test_generic_adapter_uses_resolved_index_url_and_keeps_all_listings() -> None:
    # The original request URL may be /home; the fetched HTML belongs to the
    # post-redirect final URL, which is the route the safety gate must inspect.
    ctx = _context(
        _INDEX_HTML,
        base_url=_HOME_URL,
        final_url=_COMPLETE_INDEX_URL,
    )

    result = asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert result.tier_used == "TIER_1_DOM_MANAGEBUILDING"
    assert result.winning_url == _COMPLETE_INDEX_URL
    assert len(result.units) == 2
    assert result.plan_summaries == []
    assert {
        row["source_ids"]["managebuilding_listing_id"] for row in result.units
    } == {"113015", "76796"}
    assert all(unit_has_real_anchor(row) for row in result.units)


def test_generic_adapter_does_not_promote_homepage_subset() -> None:
    home_html = f"<html><body><div>{_card('113015')}{_card('76796')}</div></body></html>"
    ctx = _context(home_html, base_url=_HOME_URL, final_url=_HOME_URL)

    result = asyncio.run(GenericAdapter().extract(page=None, ctx=ctx))

    assert result.tier_used != "TIER_1_DOM_MANAGEBUILDING"
    assert all(
        "managebuilding_listing_id" not in (row.get("source_ids") or {})
        for row in result.units
    )
    assert result.plan_summaries
