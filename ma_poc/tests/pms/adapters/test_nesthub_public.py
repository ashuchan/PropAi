"""Fail-closed exact-property NestHub SSR recovery tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters._nesthub_public import recover_nesthub_public
from ma_poc.pms.adapters._parsing import make_unit_dict
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.detector import DetectedPMS
from ma_poc.pms.source_provenance import context_unit_source_provenance

CONFIGURED_URL = (
    "https://www.augustarentalhomes.net/_system/listings/56/"
    "2905-Arrowhead-Drive---D3-Augusta-GA-30909-US"
)
COMMUNITY_URL = "https://www.augustarentalhomes.net/annabergs"
ROSTER_URL = "https://www.augustarentalhomes.net/augusta-homes-for-rent"
PAGE_2_URL = f"{ROSTER_URL}?pg=2"
TARGET_URL = (
    "https://www.augustarentalhomes.net/_system/listings/602/"
    "2905-Arrowhead-Drive---E7-Augusta-GA-30909-US"
)
CONTROL_601_URL = (
    "https://www.augustarentalhomes.net/_system/listings/601/"
    "1531-Abby-Way---1-Augusta-GA-30909-US"
)
CONTROL_606_URL = (
    "https://www.augustarentalhomes.net/_system/listings/606/"
    "104-Canary-Street-Thomson-GA-30824-US"
)


def _configured_html(*, community_links: tuple[str, ...] = (COMMUNITY_URL,)) -> str:
    links = "".join(
        f'<a href="{url}" aria-label="Annaberg Apartments">Annaberg Apartments</a>'
        for url in community_links
    )
    return f"""
    <html><head>
      <link rel="stylesheet" href="https://resources.nesthub.com/css/nhw.css">
      <link rel="canonical" href="{CONFIGURED_URL}">
      <title>2905 Arrowhead Drive - D3 Augusta, GA 30909</title>
    </head><body>{links}
      <div id="nesthub-property-detail-view" class="nhw-details">
        <section class="nhw-details__header">
          <h1>2905 Arrowhead Drive - D3</h1><h2>Augusta, GA 30909</h2>
          <p class="nhw-details__rented"><strong>This Property Is Not Available</strong></p>
        </section>
        <div class="key-detail price"><span class="value">$950</span></div>
        <div class="key-detail bedrooms"><span class="value">2</span></div>
        <div class="key-detail bathrooms"><span class="value">2</span></div>
        <div class="key-detail sqft"><span class="value">1051</span></div>
        <div class="key-detail rent"><span class="label">This Property Is Not Available</span></div>
        <div class="description">Annaberg Apartments is an Augusta community.</div>
      </div>
    </body></html>
    """


def _community_html(*, hard_filter: str = "search=ANNBRG") -> str:
    return f"""
    <html><head>
      <link rel="stylesheet" href="https://resources.nesthub.com/css/nhw-standard.css">
    </head><body>
      <h1>Welcome to Annaberg Apartments</h1>
      <address>2905 Arrowhead Dr Augusta, GA 30909</address>
      <h2>Available Units</h2>
      <div id="nh-props" data-ion="listing-widget" data-hard-filters="{hard_filter}"></div>
      <a href="/augusta-homes-for-rent" aria-label="Available Rentals">Available Rentals</a>
    </body></html>
    """


def _card(
    native_id: str,
    detail_url: str,
    location: str,
    *,
    rent: str = "$1,160/mo.",
    availability: str = "Available: 08-19-2026",
) -> str:
    return f"""
    <div class="nhw-list__item">
      <a href="{detail_url}" data-id="{native_id}">
        <div class="nhw-list__price">{rent}</div>
        <div class="nhw-list__details"><ul><li>Beds: 2</li><li>Baths: 2.5</li></ul></div>
        <div class="nhw-list__location">{location}</div>
        <div class="nhw-list__prop-type">Apartment</div>
        <div class="nhw-list__availability">{availability}</div>
      </a>
    </div>
    """


def _roster_page(*cards: str, page: int = 1) -> str:
    pagination = (
        '<div class="nhw-pagination"><a href="?pg=1">1</a>'
        '<a href="?pg=2">2</a></div>'
    )
    return f"""
    <html><body>
      <div id="nesthub-property-list-view" data-ion="listing-list">
        {''.join(cards)}{pagination}
      </div>
      <span data-current-page="{page}"></span>
    </body></html>
    """


def _target_detail(
    *,
    status: str = "For Rent",
    rent: str = "$1,160",
    date_available: str = "08-19-2026",
    description: str | None = None,
    canonical_url: str = TARGET_URL,
) -> str:
    scoped = description or (
        "Annaberg Apartments is located in Augusta. "
        "The Chesapeake is a 2 bedroom, 2.5 bath townhome located at "
        "2905 Arrowhead Drive #E7 Augusta, GA 30909."
    )
    return f"""
    <html><head>
      <link rel="stylesheet" href="https://resources.nesthub.com/css/nhw.css">
      <link rel="canonical" href="{canonical_url}">
    </head><body>
      <div id="nesthub-property-detail-view" class="nhw-details">
        <section class="nhw-details__header">
          <h1>2905 Arrowhead Drive - E7</h1><h2>Augusta, GA 30909</h2>
        </section>
        <div class="key-detail price"><span class="value">{rent}</span></div>
        <div class="key-detail bedrooms"><span class="value">2</span></div>
        <div class="key-detail bathrooms"><span class="value">2.5</span></div>
        <div class="key-detail sqft"><span class="value">1268</span></div>
        <div class="key-detail rent"><span class="label">{status}</span></div>
        <div class="sub-detail"><span class="sub-detail__label">Date Available:</span>
          <span class="sub-detail__value">{date_available}</span></div>
        <div class="description">{scoped}</div>
      </div>
    </body></html>
    """


def _ctx(html: str | None = None) -> AdapterContext:
    return AdapterContext(
        base_url=CONFIGURED_URL,
        detected=DetectedPMS(pms="generic_plan_text", confidence=0.75),
        profile=None,
        expected_total_units=None,
        property_id="1765",
        fetch_result=SimpleNamespace(
            body=html if html is not None else _configured_html(),
            final_url=CONFIGURED_URL,
            captcha_detected=False,
        ),
        property_name="Annaberg",
        address="2905 Arrowhead Dr",
        city="Augusta",
        state="GA",
        zip_code="30909",
    )


def _responses(*, target_detail: str | None = None) -> dict[str, tuple[str, str]]:
    return {
        COMMUNITY_URL: (_community_html(), COMMUNITY_URL),
        ROSTER_URL: (
            _roster_page(
                _card(
                    "602",
                    TARGET_URL,
                    "2905 Arrowhead Drive - E7, Augusta, GA 30909",
                ),
                # Control: same city/state/ZIP, wrong street and property.
                _card(
                    "601",
                    CONTROL_601_URL,
                    "1531 Abby Way - 1, Augusta, GA 30909",
                    rent="$1,465/mo.",
                    availability="Available: 08-01-2026",
                ),
            ),
            ROSTER_URL,
        ),
        PAGE_2_URL: (
            _roster_page(
                # Control: wrong street, city, ZIP, and property.
                _card(
                    "606",
                    CONTROL_606_URL,
                    "104 Canary Street, Thomson, GA 30824",
                    rent="$925/mo.",
                    availability="Available: 08-01-2026",
                ),
                page=2,
            ),
            PAGE_2_URL,
        ),
        TARGET_URL: (target_detail or _target_detail(), TARGET_URL),
    }


def _install_fetch(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[str, tuple[str, str]],
) -> list[str]:
    from ma_poc.pms.adapters import _nesthub_public as module

    calls: list[str] = []

    async def fake_fetch(url: str, _referer: str):
        calls.append(url)
        if url not in responses:
            raise AssertionError(f"unexpected direct fetch: {url}")
        return responses[url]

    monkeypatch.setattr(module, "_fetch_direct_html", fake_fetch)
    return calls


@pytest.mark.asyncio
async def test_exact_chain_emits_e7_and_excludes_stale_and_two_portfolio_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fetch(monkeypatch, _responses())
    ctx = _ctx()

    rows = await recover_nesthub_public(ctx)

    assert len(rows) == 1
    row = rows[0]
    assert row["unit_number"] == "E7"
    assert row["unit_name"] == "2905 Arrowhead Drive - E7"
    assert row["provider_unit_address"] == "2905 Arrowhead Drive - E7"
    assert row["floor_plan_name"] == "Chesapeake"
    assert row["floor_plan_name_provenance"].startswith("provider_detail_scoped")
    assert row["market_rent_low"] == row["market_rent_high"] == 1160
    assert row["availability_status"] == "AVAILABLE"
    assert row["availability_date"] == "2026-08-19"
    assert row["availability_text"] == "Available: 08-19-2026"
    assert row["source_ids"] == {"nesthub_listing_id": "602"}
    assert row["source_property_provenance"].startswith(
        "exact_configured_nesthub_detail_same_host_community"
    )
    assert calls == [COMMUNITY_URL, ROSTER_URL, PAGE_2_URL, TARGET_URL]

    telemetry = getattr(ctx, "_nesthub_official_chain")
    # Control 1: the configured exact-property ID is stale and cannot emit.
    assert telemetry["configured_listing_id"] == "56"
    assert telemetry["configured_status"] == "This Property Is Not Available"
    assert telemetry["configured_listing_must_not_emit"] is True
    assert telemetry["published_property_filter"] == "search=ANNBRG"
    assert telemetry["portfolio_rows"] == 3
    assert telemetry["exact_address_candidates"] == 1
    assert telemetry["accepted_rows"] == 1
    assert telemetry["native_listing_ids"] == ["602"]
    assert telemetry["pages"] == [
        {"page": 1, "url": ROSTER_URL, "rows": 2},
        {"page": 2, "url": PAGE_2_URL, "rows": 1},
    ]
    rejected = {
        item["provider_listing_id"]: item["reasons"]
        for item in telemetry["rejected_rows"]
    }
    # Control 2: same ZIP is insufficient when street/property differ.
    assert "canonical_street_and_native_unit_suffix_mismatch" in rejected["601"]
    assert "canonical_city_mismatch" not in rejected["601"]
    assert "canonical_zip_mismatch" not in rejected["601"]
    # Control 3: foreign community/city/ZIP is excluded before detail fetch.
    assert "canonical_street_and_native_unit_suffix_mismatch" in rejected["606"]
    assert "canonical_city_mismatch" in rejected["606"]
    assert "canonical_zip_mismatch" in rejected["606"]
    provenance = context_unit_source_provenance(ctx)
    assert len(provenance) == 1
    assert provenance[0]["provider"] == "nesthub"
    assert provenance[0]["response_kind"] == "unit_detail"
    assert provenance[0]["source_url"] == TARGET_URL
    assert provenance[0]["unit_count"] == 1
    assert provenance[0]["identity"]["status"] == "MATCH"
    assert provenance[0]["identity"]["portfolio_count"] == 3


@pytest.mark.asyncio
async def test_ambiguous_community_links_fail_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters import _nesthub_public as module

    async def forbidden(*_args: Any, **_kwargs: Any):
        raise AssertionError("ambiguous property community must not be fetched")

    monkeypatch.setattr(module, "_fetch_direct_html", forbidden)
    html = _configured_html(
        community_links=(COMMUNITY_URL, "https://www.augustarentalhomes.net/annaberg-two")
    )
    assert await recover_nesthub_public(_ctx(html)) == []


@pytest.mark.asyncio
async def test_invalid_property_filter_fails_before_portfolio_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _responses()
    responses[COMMUNITY_URL] = (_community_html(hard_filter=""), COMMUNITY_URL)
    calls = _install_fetch(monkeypatch, responses)
    ctx = _ctx()

    assert await recover_nesthub_public(ctx) == []
    assert calls == [COMMUNITY_URL]
    assert getattr(ctx, "_nesthub_official_chain")["failure_reason"] == (
        "community_property_or_roster_boundary_failed"
    )


@pytest.mark.asyncio
async def test_duplicate_native_id_across_pages_rejects_before_detail_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _responses()
    responses[PAGE_2_URL] = (
        _roster_page(
            _card(
                "602",
                TARGET_URL,
                "2905 Arrowhead Drive - E7, Augusta, GA 30909",
            ),
            page=2,
        ),
        PAGE_2_URL,
    )
    calls = _install_fetch(monkeypatch, responses)
    ctx = _ctx()

    assert await recover_nesthub_public(ctx) == []
    assert calls == [COMMUNITY_URL, ROSTER_URL, PAGE_2_URL]
    assert getattr(ctx, "_nesthub_official_chain")["failure_reason"] == (
        "property_identity_or_pagination_rejected"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target_detail",
    [
        _target_detail(status="This Property Is Not Available"),
        _target_detail(rent="$1,170"),
        _target_detail(date_available="08-20-2026"),
        _target_detail(
            description=(
                "Other Apartments. The Chesapeake is a 2 bedroom home at "
                "2905 Arrowhead Drive #E7 Augusta, GA 30909."
            )
        ),
        _target_detail(
            description=(
                "Annaberg Apartments home at 2905 Arrowhead Drive #E7 "
                "Augusta, GA 30909."
            )
        ),
        _target_detail(
            canonical_url=(
                "https://www.augustarentalhomes.net/_system/listings/999/"
                "2905-Arrowhead-Drive---E7-Augusta-GA-30909-US"
            )
        ),
    ],
    ids=[
        "unavailable",
        "rent-mismatch",
        "date-mismatch",
        "wrong-scoped-property",
        "floor-plan-name-absent",
        "native-id-mismatch",
    ],
)
async def test_candidate_detail_must_pass_every_revalidation_gate(
    monkeypatch: pytest.MonkeyPatch,
    target_detail: str,
) -> None:
    calls = _install_fetch(
        monkeypatch,
        _responses(target_detail=target_detail),
    )
    ctx = _ctx()

    assert await recover_nesthub_public(ctx) == []
    assert calls == [COMMUNITY_URL, ROSTER_URL, PAGE_2_URL, TARGET_URL]
    assert getattr(ctx, "_nesthub_official_chain")["failure_reason"] == (
        "no_unique_detail_revalidated_rows"
    )


@pytest.mark.asyncio
async def test_direct_fetch_explicitly_disables_proxy_and_unlocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters import _nesthub_public as module
    from ma_poc.pms.adapters import _probe

    observed: dict[str, Any] = {}

    def fake_probe(url: str, **kwargs: Any):
        observed.update({"url": url, **kwargs})
        return SimpleNamespace(
            status_code=200,
            url=url,
            content=b"<html><body>ordinary response</body></html>",
            text="",
        )

    monkeypatch.setattr(_probe, "probe_get", fake_probe)
    result = await module._fetch_direct_html(COMMUNITY_URL, CONFIGURED_URL)

    assert result is not None
    assert observed["unlocker"] is False
    assert observed["proxies"] == {}
    assert observed["retries"] == 0


@pytest.mark.asyncio
async def test_narrow_scraper_bridge_discards_stale_detail_deposit_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms import scraper as scraper_module
    from ma_poc.pms.adapters import _nesthub_public as module

    async def fake_recovery(_ctx: AdapterContext):
        row = make_unit_dict(
            floor_plan_name="Chesapeake",
            bedrooms="2",
            bathrooms="2.5",
            sqft="1268",
            unit_number="E7",
            rent_low=1160,
            rent_high=1160,
            availability_status="AVAILABLE",
            availability_date="2026-08-19",
            source_api_url=TARGET_URL,
            extraction_tier="TIER_1_PUBLIC_NESTHUB_SSR_EXACT_PROPERTY",
            source_ids={"nesthub_listing_id": "602"},
        )
        return [row]

    monkeypatch.setattr(module, "recover_nesthub_public", fake_recovery)
    bogus_deposit_plan = make_unit_dict(
        floor_plan_name="2 Bedroom / 2.0 Bath",
        bedrooms="2",
        bathrooms="2",
        rent_low=500,
        rent_high=500,
        source_api_url=CONFIGURED_URL,
        extraction_tier="GENERIC_PLAN_TEXT_PLAN_LEVEL",
    )
    previous = AdapterResult(
        units=[bogus_deposit_plan],
        tier_used="TIER_1_DOM_GENERIC_PLAN_TEXT",
    )

    recovered = await scraper_module._try_page_published_native_recovery(
        _ctx(),
        previous,
    )

    assert recovered is not None
    result, adapter_name = recovered
    assert adapter_name == "nesthub_public"
    assert result.tier_used == "TIER_1_PUBLIC_NESTHUB_SSR_EXACT_PROPERTY"
    assert len(result.units) == 1
    assert result.units[0]["unit_number"] == "E7"
    assert result.plan_summaries == []


@pytest.mark.asyncio
async def test_fetch_only_scrape_runs_nesthub_bridge_with_body_resolver_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.config import feature_flags
    from ma_poc.pms import scraper as scraper_module
    from ma_poc.pms.adapters import _nesthub_public as module
    from ma_poc.pms.adapters import _probe

    responses = _responses()

    async def fake_fetch(url: str, _referer: str):
        if url not in responses:
            raise AssertionError(f"unexpected NestHub direct fetch: {url}")
        return responses[url]

    class EmptyAdapter:
        def __init__(self, pms_name: str) -> None:
            self.pms_name = pms_name

        async def extract(self, _page: object, _ctx: AdapterContext) -> AdapterResult:
            return AdapterResult(errors=[f"{self.pms_name} returned no rows"])

        def static_fingerprints(self) -> list[str]:
            return []

    monkeypatch.setattr(module, "_fetch_direct_html", fake_fetch)
    inert_html = "<html><body>No additional PMS markers.</body></html>"
    monkeypatch.setattr(
        _probe,
        "probe_get",
        lambda url, **_kwargs: SimpleNamespace(
            status_code=200,
            url=url,
            text=inert_html,
            content=inert_html.encode(),
            headers={"content-type": "text/html"},
        ),
    )
    monkeypatch.setattr(
        scraper_module,
        "get_adapter",
        lambda pms_name: EmptyAdapter(str(pms_name)),
    )
    monkeypatch.setattr(feature_flags, "ENABLE_BODY_RESOLVER", False)

    html = _configured_html()
    fetch_result = FetchResult(
        url=CONFIGURED_URL,
        outcome=FetchOutcome.OK,
        status=200,
        body=html.encode(),
        headers={"content-type": "text/html"},
        render_mode=RenderMode.GET,
        final_url=CONFIGURED_URL,
        attempts=1,
        elapsed_ms=1,
    )
    result = await scraper_module.scrape(
        CONFIGURED_URL,
        page=None,
        fetch_result=fetch_result,
        property_id="1765",
        csv_row={
            "apartmentid": "1765",
            "name": "Annaberg",
            "address": "2905 Arrowhead Dr",
            "city": "Augusta",
            "state": "GA",
            "zip": "30909",
            "website": CONFIGURED_URL,
        },
        shared_budget={
            "llm_api_calls": 0,
            "llm_dom_calls": 0,
            "llm_monolithic": 0,
            "link_hop": 0,
            "_cost_cap_usd": 0,
        },
    )

    assert result["_adapter_used"] == "nesthub_public"
    assert result["extraction_tier_used"] == (
        "TIER_1_PUBLIC_NESTHUB_SSR_EXACT_PROPERTY"
    )
    assert len(result["units"]) == 1
    assert result["units"][0]["unit_number"] == "E7"
    assert result["units"][0]["floor_plan_name"] == "Chesapeake"
    assert result["units"][0]["availability_date"] == "2026-08-19"
    assert result["plan_summaries"] == []
    assert len(result["_unit_source_provenance"]) == 1
    assert result["_unit_source_provenance"][0]["provider"] == "nesthub"
    assert "page_published_native:nesthub_public" in result["_fallback_chain"]
    assert result["_nesthub_official_chain"]["native_listing_ids"] == ["602"]
