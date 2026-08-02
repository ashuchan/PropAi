"""Regression coverage for the scraper's cost-free page-local recovery."""

from __future__ import annotations

import pytest

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.detector import detect_pms
from ma_poc.pms.scraper import _try_page_local_static_recovery

_TWO_PLAN_ROWS = """
<html><body>
  <section>1 Bedroom / 1 Bath 720 sq. ft. From $1,250 per month</section>
  <section>2 Bedroom / 2 Bath 980 sq. ft. From $1,650 per month</section>
</body></html>
"""


def _ctx(
    html: str,
    *,
    url: str = "https://apartments.example/floorplans/",
    captcha_detected: bool = False,
) -> AdapterContext:
    fetch_result = FetchResult(
        url=url,
        outcome=FetchOutcome.OK,
        status=200,
        body=html.encode("utf-8"),
        headers={"content-type": "text/html"},
        render_mode=RenderMode.RENDER,
        final_url=url,
        attempts=1,
        elapsed_ms=100,
        captcha_detected=captcha_detected,
    )
    return AdapterContext(
        base_url=url,
        detected=detect_pms(url, page_html=html),
        profile=None,
        expected_total_units=None,
        property_id="TEST-LOCAL-RECOVERY",
        fetch_result=fetch_result,
    )


def test_unknown_page_recovers_two_generic_plan_rows() -> None:
    recovered = _try_page_local_static_recovery(
        _ctx(_TWO_PLAN_ROWS),
        AdapterResult(errors=["primary returned no data"]),
    )

    assert recovered is not None
    result, adapter_name = recovered
    assert adapter_name == "generic_plan_text"
    # AdapterResult keeps admitted plan rows in ``units`` for the legacy
    # output contract while also exposing the canonical plan-only channel.
    assert len(result.units) == 2
    assert len(result.plan_summaries) == 2
    assert [row["bedrooms"] for row in result.plan_summaries] == ["1", "2"]
    assert [row["market_rent_low"] for row in result.plan_summaries] == [1250, 1650]
    assert result.tier_used == "TIER_1_DOM_GENERIC_PLAN_TEXT_EMPTY_FALLBACK"
    assert result.winning_url == "https://apartments.example/floorplans/"
    assert result.errors[0] == "primary returned no data"


def test_strong_but_empty_rentcafe_detection_does_not_hide_visible_plans() -> None:
    html = (
        '<div class="layout-tabs"><a href="https://example.securecafe.com/onlineleasing/">'
        "Apply now</a></div>"
        + _TWO_PLAN_ROWS
    )
    ctx = _ctx(html)
    assert ctx.detected.pms == "rentcafe"

    recovered = _try_page_local_static_recovery(
        ctx,
        AdapterResult(errors=["securecafe endpoint returned no rows"]),
    )

    assert recovered is not None
    result, adapter_name = recovered
    assert adapter_name == "generic_plan_text"
    assert len(result.plan_summaries) == 2
    assert "securecafe endpoint returned no rows" in result.errors


def test_rentmanager_wordpress_cards_win_over_flattened_plan_text() -> None:
    html = """
    <html><body>
      <script src="https://cdn.rentmanager.com/rm12filereader.js"></script>
      <a class="individual-item" data-bed="1" data-rent="1275"
         data-date="2026/08" href="/unit?uid=9001">
        <h2>The Ruby <span>#103</span></h2>
        <div>Bath 1</div>
        <div class="availableDate">Available 08/12/2026</div>
      </a>
      <a class="individual-item" data-bed="2" data-rent="1680"
         data-date="2026/09" href="/unit?uid=9002">
        <h2>The Sapphire <span>#114</span></h2>
        <div>Bath 2</div>
        <div class="availableDate">Available 09/01/2026</div>
      </a>
    </body></html>
    """

    recovered = _try_page_local_static_recovery(_ctx(html), AdapterResult())

    assert recovered is not None
    result, adapter_name = recovered
    assert adapter_name == "rentmanager"
    assert result.plan_summaries == []
    assert [row["unit_number"] for row in result.units] == ["103", "114"]
    assert [row["floor_plan_name"] for row in result.units] == [
        "The Ruby",
        "The Sapphire",
    ]
    assert result.units[0]["market_rent_low"] == 1275
    assert result.units[0]["availability_date"] == "2026-08-12"
    assert result.tier_used == "TIER_1_DOM_RENTMANAGER_WP_CARDS_EMPTY_FALLBACK"


def test_exact_identity_static_residence_table_emits_units_before_plan_text() -> None:
    html = """
    <html><head><title>1515 Park Place in Crown Heights, Brooklyn</title></head>
    <body>
      <div>1515 Park Pl, Brooklyn, NY 11213</div>
      <div class="table">
        <div class="table-header">
          <div class="table-cell">Residence</div>
          <div class="table-cell">Bed/Bath</div>
          <div class="table-cell">Price</div>
          <div class="table-cell">Floorplan</div>
        </div>
        <div class="table-row">
          <div class="table-cell residence">205-805</div>
          <div class="table-cell bed">1 Bed/1Bath</div>
          <div class="table-cell price">$2,300 - $2,450</div>
          <div class="table-cell table-links"><a href="/plan.pdf">View</a></div>
        </div>
        <div class="table-row">
          <div class="table-cell residence">102</div>
          <div class="table-cell bed">2 Bed/2Bath</div>
          <div class="table-cell price">$3,000</div>
          <div class="table-cell table-links"><a href="/plan.pdf">View</a></div>
        </div>
        <div class="table-row">
          <div class="table-cell residence">101</div>
          <div class="table-cell bed">4 Bed/2Bath</div>
          <div class="table-cell price">$4,500</div>
          <div class="table-cell table-links"><a href="/plan.pdf">View</a></div>
        </div>
      </div>
    </body></html>
    """
    ctx = _ctx(
        html,
        url="https://www.1515parkplace.example/availability.html",
    )
    ctx.property_id = "261580"
    ctx.property_name = "1515 Park Place"
    ctx.address = "1515 Park Pl"
    ctx.city = "Brooklyn"
    ctx.state = "NY"
    ctx.zip_code = "11213"

    recovered = _try_page_local_static_recovery(ctx, AdapterResult())

    assert recovered is not None
    result, adapter_name = recovered
    assert adapter_name == "static_residence_table"
    assert [plan["floor_plan_name"] for plan in result.plan_summaries] == [
        "205-805"
    ]
    assert [row["unit_number"] for row in result.units] == ["102", "101"]
    assert [row["market_rent_low"] for row in result.units] == [3000, 4500]
    assert all(row["floor_plan_name"] == "" for row in result.units)
    assert result.tier_used == "TIER_1_DOM_STATIC_RESIDENCE_TABLE"
    assert len(result.unit_source_provenance) == 1
    assert result.unit_source_provenance[0]["identity"]["status"] == "MATCH"


def test_zero_rows_never_replace_previous_result() -> None:
    assert (
        _try_page_local_static_recovery(
            _ctx("<html><body>Amenities and contact information</body></html>"),
            AdapterResult(errors=["keep me"]),
        )
        is None
    )


def test_captcha_body_is_never_parsed_as_inventory() -> None:
    assert (
        _try_page_local_static_recovery(
            _ctx(_TWO_PLAN_ROWS, captcha_detected=True),
            AdapterResult(),
        )
        is None
    )


def test_portfolio_sized_generic_roster_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not turn a PMC-wide property directory into one property's plans."""
    from ma_poc.pms.adapters import generic_plan_text

    monkeypatch.setattr(
        generic_plan_text,
        "parse_generic_plan_text",
        lambda _body, _url: [{"bedrooms": "1", "market_rent_low": 1200}] * 25,
    )

    assert (
        _try_page_local_static_recovery(
            _ctx("<html><body>PMC-wide apartment directory</body></html>"),
            AdapterResult(),
        )
        is None
    )


@pytest.mark.asyncio
async def test_scrape_wires_page_local_recovery_after_empty_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise Step 8a through the public single-page orchestrator."""
    from ma_poc.config import feature_flags
    from ma_poc.pms import scraper as scraper_mod

    class EmptyAdapter:
        def __init__(self, pms_name: str) -> None:
            self.pms_name = pms_name

        async def extract(
            self, _page: object, _ctx: AdapterContext
        ) -> AdapterResult:
            return AdapterResult(errors=[f"{self.pms_name} returned no rows"])

        def static_fingerprints(self) -> list[str]:
            return []

    monkeypatch.setattr(
        scraper_mod,
        "get_adapter",
        lambda pms_name: EmptyAdapter(str(pms_name)),
    )
    # Keep the integration test strictly page-local. Step 8b is independently
    # covered and must not introduce an active-fetch seam here.
    monkeypatch.setattr(feature_flags, "ENABLE_BODY_RESOLVER", False)

    ctx = _ctx(_TWO_PLAN_ROWS)
    result = await scraper_mod.scrape(
        ctx.base_url,
        page=None,
        fetch_result=ctx.fetch_result,
        property_id=ctx.property_id,
        shared_budget={
            "llm_api_calls": 0,
            "llm_dom_calls": 0,
            "llm_monolithic": 0,
            "link_hop": 0,
            "_cost_cap_usd": 0,
        },
    )

    assert result["units"] == []
    assert len(result["plan_summaries"]) == 2
    assert result["_adapter_used"] == "generic_plan_text"
    assert result["extraction_tier_used"] == (
        "TIER_1_DOM_GENERIC_PLAN_TEXT_EMPTY_FALLBACK"
    )
    assert "page_local_static:generic_plan_text" in result["_fallback_chain"]
