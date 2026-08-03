from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.mri_prospectconnect import (
    MriProspectConnectAdapter,
    MriSearchResponse,
    extract_mri_property_route,
    mri_property_identity_matches,
    parse_mri_search_units,
)
from ma_poc.pms.detector import DetectedPMS, detect_pms
from ma_poc.pms.scraper import scrape_jugnu

INDEX_URL = "https://smg.mriprospectconnect.com/Search/Index/CCA"
SEARCH_URL = "https://smg.mriprospectconnect.com/Search/Search"
INDEX_HTML = """
<html><body>
  <input name="__RequestVerificationToken" value="token-1">
  <main data-propertyid="cca">
    <h1>CHARTER CLUB APARTMENT HOMES</h1>
    <p>1040 Windward Drive, London, OH 43140</p>
  </main>
</body></html>
"""
SEARCH_HTML = """
<div class="pc-card">
  <h3 class="pc-card-title">1BD DOWNSTAIRS UNIT <span class="label">1 available</span></h3>
  <h4 class="pc-card-subtitle">1 Bed / 1 Bath</h4>
  <table><tbody><tr class="pc-row-unit">
    <td data-th="Unit">42-B</td>
    <td data-th="Building">A01</td>
    <td data-th="Sqft">750</td>
    <td data-th="Available">8/1/2026</td>
    <td data-th="Rent Range" data-rent-range="1,099.00">1,099.00</td>
    <td><button data-unitid="42-B" data-bldgid="A01"
      data-available-date="2026-08-01" data-available-end-date="2026-08-30"
      data-term="12" data-unit-address="1042 B SEACOVE CIRCLE">Select</button></td>
  </tr></tbody></table>
</div>
"""


def _ctx(**overrides: object) -> AdapterContext:
    values: dict[str, object] = {
        "base_url": "https://smg.mriprospectconnect.com/cca",
        "detected": DetectedPMS(
            pms="mri_prospectconnect",
            confidence=0.95,
            evidence=["test"],
            recommended_strategy="api_first",
        ),
        "profile": None,
        "expected_total_units": None,
        "property_id": "74523",
        "fetch_result": SimpleNamespace(
            body=INDEX_HTML.encode(),
            final_url="https://smg.mriprospectconnect.com/cca",
        ),
        "property_name": "Charter Club",
        "address": "1040 Windward Dr",
        "city": "London",
        "state": "OH",
        "zip_code": "43140",
    }
    values.update(overrides)
    return AdapterContext(**values)  # type: ignore[arg-type]


def _response(index_html: str = INDEX_HTML) -> MriSearchResponse:
    return MriSearchResponse(
        index_url=INDEX_URL,
        final_index_url=INDEX_URL,
        community="CCA",
        index_status=200,
        index_html=index_html,
        search_url=SEARCH_URL,
        search_status=200,
        search_html=SEARCH_HTML,
    )


def test_extract_mri_property_route_accepts_published_root_and_index() -> None:
    assert extract_mri_property_route("https://smg.mriprospectconnect.com/cca") == (INDEX_URL, "CCA")
    assert extract_mri_property_route(INDEX_URL) == (INDEX_URL, "CCA")


def test_extract_mri_property_route_fails_closed_on_foreign_or_ambiguous() -> None:
    assert extract_mri_property_route("https://example.com/cca") == ("", "")
    assert extract_mri_property_route(
        "https://smg.mriprospectconnect.com/cca",
        "https://smg.mriprospectconnect.com/other",
    ) == ("", "")


def test_mri_property_identity_requires_provider_code_and_full_address() -> None:
    assert mri_property_identity_matches(INDEX_HTML, _ctx(), "CCA")
    assert not mri_property_identity_matches(
        INDEX_HTML.replace('data-propertyid="cca"', 'data-propertyid="XYZ"'),
        _ctx(),
        "CCA",
    )
    assert not mri_property_identity_matches(
        INDEX_HTML.replace("1040 Windward Drive", "999 Other Road"),
        _ctx(),
        "CCA",
    )


def test_mri_property_identity_normalizes_cir_to_circle() -> None:
    html = (
        INDEX_HTML.replace("CHARTER CLUB APARTMENT HOMES", "Village Park at Paladin")
        .replace(
            "1040 Windward Drive, London, OH 43140",
            "101 Clifton Park Circle, Wilmington, DE 19802",
        )
        .replace('data-propertyid="cca"', 'data-propertyid="475PV"')
    )
    assert mri_property_identity_matches(
        html,
        _ctx(
            property_name="Village Park at Paladin",
            address="101 Clifton Park Cir",
            city="Wilmington",
            state="DE",
            zip_code="19802",
        ),
        "475PV",
    )


def test_mri_property_identity_allows_bound_phase_roman_suffix() -> None:
    """Bridgepoint I may match Bridgepoint only inside the full identity gate."""
    html = (
        INDEX_HTML.replace("CHARTER CLUB APARTMENT HOMES", "Bridgepoint")
        .replace(
            "1040 Windward Drive, London, OH 43140",
            "1500 Monument Road, Jacksonville, FL 32225",
        )
        .replace('data-propertyid="cca"', 'data-propertyid="BRI"')
    )
    assert mri_property_identity_matches(
        html,
        _ctx(
            property_name="Bridgepoint I",
            address="1500 Monument Rd",
            city="Jacksonville",
            state="FL",
            zip_code="32225",
        ),
        "BRI",
    )
    assert not mri_property_identity_matches(
        html,
        _ctx(
            property_name="Bridgepoint South I",
            address="1500 Monument Rd",
            city="Jacksonville",
            state="FL",
            zip_code="32225",
        ),
        "BRI",
    )
def test_parse_mri_search_units_preserves_native_row_and_dimensions() -> None:
    units = parse_mri_search_units(
        SEARCH_HTML,
        community="CCA",
        source_url=SEARCH_URL,
    )
    assert len(units) == 1
    assert units[0]["unit_number"] == "42-B"
    assert units[0]["building"] == "A01"
    assert units[0]["provider_native_unit_id"] == "A01:42-B"
    assert units[0]["source_ids"] == {"mri_unit_id": "A01:42-B"}
    assert units[0]["_floor_plan_name_provenance"] == "mri.pc-card-title"
    assert units[0]["source_property_id"] == "CCA"
    assert units[0]["sqft"] == "750"
    assert units[0]["market_rent_low"] == 1099
    assert units[0]["market_rent_high"] == 1099
    assert units[0]["rent_range_source_field"] == "data-rent-range"
    assert units[0]["availability_date"] == "2026-08-01"
    assert units[0]["available_end_date"] == "2026-08-30"
    assert units[0]["source_api_url"] == SEARCH_URL


def test_parse_mri_search_units_rejects_unpriced_or_dimensionless_rows() -> None:
    assert not parse_mri_search_units(
        SEARCH_HTML.replace('data-rent-range="1,099.00"', 'data-rent-range="0"'),
        community="CCA",
        source_url=SEARCH_URL,
    )


def test_parse_mri_search_units_preserves_labeled_range_endpoints() -> None:
    elmtree = SEARCH_HTML.replace(
        'data-rent-range="1,099.00"',
        'data-rent-range="$695.00 – $865.00"',
    ).replace(">1,099.00</td>", ">$695.00 – $865.00</td>")

    units = parse_mri_search_units(
        elmtree,
        community="ELM",
        source_url=SEARCH_URL,
    )

    assert len(units) == 1
    assert units[0]["market_rent_low"] == 695
    assert units[0]["market_rent_high"] == 865

    from ma_poc.scripts.runners.jugnu import _format_v2_unit

    final = _format_v2_unit(
        units[0],
        datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        "235871",
    )
    assert final["rent_low"] == 695
    assert final["rent_high"] == 865


def test_parse_mri_search_units_rejects_inverted_range() -> None:
    inverted = SEARCH_HTML.replace(
        'data-rent-range="1,099.00"',
        'data-rent-range="$1,305.00 – $1,005.00"',
    )
    assert not parse_mri_search_units(
        inverted,
        community="ELM",
        source_url=SEARCH_URL,
    )
    assert not parse_mri_search_units(
        SEARCH_HTML.replace('<td data-th="Sqft">750</td>', '<td data-th="Sqft">-</td>'),
        community="CCA",
        source_url=SEARCH_URL,
    )


def test_detector_and_registry_route_mri_prospectconnect() -> None:
    detected = detect_pms("https://smg.mriprospectconnect.com/cca")
    assert detected.pms == "mri_prospectconnect"
    assert detected.confidence == 0.95
    assert detected.recommended_strategy == "api_first"

    from ma_poc.pms.adapters.registry import get_adapter

    assert get_adapter("mri_prospectconnect").pms_name == "mri_prospectconnect"


@pytest.mark.asyncio
async def test_mri_adapter_property_scoped_session_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ma_poc.pms.adapters.mri_prospectconnect._fetch_mri_search",
        lambda _url, _community: _response(),
    )
    result = await MriProspectConnectAdapter().extract(
        None,
        _ctx(),  # type: ignore[arg-type]
    )
    assert result.tier_used == "TIER_1_API_MRI_PROSPECTCONNECT"
    assert len(result.units) == 1
    assert result.units[0]["provider_native_unit_id"] == "A01:42-B"
    assert result.winning_url == SEARCH_URL


@pytest.mark.asyncio
async def test_mri_adapter_rejects_sibling_property_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sibling_html = INDEX_HTML.replace("CHARTER CLUB", "TRAFALGAR SQUARE")
    monkeypatch.setattr(
        "ma_poc.pms.adapters.mri_prospectconnect._fetch_mri_search",
        lambda _url, _community: _response(sibling_html),
    )
    result = await MriProspectConnectAdapter().extract(
        None,
        _ctx(),  # type: ignore[arg-type]
    )
    assert not result.units
    assert result.tier_used.endswith("PROPERTY_IDENTITY_REJECTED")


@pytest.mark.asyncio
async def test_mri_provider_route_full_scraper_e2e(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ma_poc.pms.adapters.mri_prospectconnect._fetch_mri_search",
        lambda _url, _community: _response(),
    )
    provider_url = "https://smg.mriprospectconnect.com/cca"
    fetch_result = FetchResult(
        url=provider_url,
        outcome=FetchOutcome.OK,
        status=200,
        body=INDEX_HTML.encode(),
        headers={},
        render_mode=RenderMode.GET,
        final_url=provider_url,
        attempts=1,
        elapsed_ms=0,
    )
    task = CrawlTask(
        url=provider_url,
        property_id="74523",
        priority=0,
        budget_ms=30_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )
    result = await scrape_jugnu(
        task,
        fetch_result,
        page=None,
        profile=None,
        csv_row={
            "apartmentid": "74523",
            "name": "Charter Club",
            "address": "1040 Windward Dr",
            "city": "London",
            "state": "OH",
            "zip": "43140",
            "website": "http://www.charterclubapts.com/",
        },
    )
    assert result["_adapter_used"] == "mri_prospectconnect"
    assert result["extraction_tier_used"] == "TIER_1_API_MRI_PROSPECTCONNECT"
    assert len(result["units"]) == 1


def test_page_published_exact_mri_route_gets_portal_priority() -> None:
    from ma_poc.pms.scraper import (
        _EMBEDDED_PORTAL_ANCHOR_PREFIX,
        _EMBEDDED_PORTAL_SCORE,
        _rank_internal_links,
    )

    portal = "https://residebpg.mriprospectconnect.com/475PV"
    html = f"""
    <html><body>
      <a href="/floorplans">Floor Plans</a>
      <a href="/availability">Availability</a>
      <a href="{portal}">Apply Online</a>
    </body></html>
    """
    ranked = _rank_internal_links(html, "https://pettinaro.com/village-park-paladin/", limit=10)
    mri = next(item for item in ranked if item[0] == portal)
    assert mri[1] >= _EMBEDDED_PORTAL_SCORE
    assert mri[2].startswith(_EMBEDDED_PORTAL_ANCHOR_PREFIX)


@pytest.mark.asyncio
async def test_configured_marketing_page_hops_to_exact_mri_property_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the real Village Park failure shape through ``scrape_jugnu``.

    The rendered marketing body publishes the sole property-scoped MRI URL,
    but seven 5,000-point guessed same-site priors previously crowded its
    145-point link out of the bounded hop queue.  The provider also spells
    ``Circle`` while the canonical row uses ``Cir``.
    """
    from ma_poc import fetch as fetch_module
    from ma_poc.config import feature_flags

    portal = "https://residebpg.mriprospectconnect.com/475PV"
    index_html = (
        INDEX_HTML.replace("CHARTER CLUB APARTMENT HOMES", "Village Park at Paladin")
        .replace(
            "1040 Windward Drive, London, OH 43140",
            "101 Clifton Park Circle, Wilmington, DE 19802",
        )
        .replace('data-propertyid="cca"', 'data-propertyid="475PV"')
    )
    search_html = (
        SEARCH_HTML.replace("42-B", "1000")
        .replace("A01", "2")
        .replace("1,099.00", "1,845.00")
        .replace('data-rent-range="1,099.00"', 'data-rent-range="1,845.00"')
    )
    response = MriSearchResponse(
        index_url=("https://residebpg.mriprospectconnect.com/Search/Index/475PV"),
        final_index_url=("https://residebpg.mriprospectconnect.com/Search/Index/475PV"),
        community="475PV",
        index_status=200,
        index_html=index_html,
        search_url=("https://residebpg.mriprospectconnect.com/Search/Search"),
        search_status=200,
        search_html=search_html,
    )
    monkeypatch.setattr(
        "ma_poc.pms.adapters.mri_prospectconnect._fetch_mri_search",
        lambda _url, _community: response,
    )
    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda url, **_kwargs: SimpleNamespace(
            url=url,
            status_code=404,
            text="",
            content=b"",
            headers={},
        ),
    )
    monkeypatch.setattr(feature_flags, "ENABLE_CRAWL_GET_GATE", False)

    root = "https://pettinaro.com/village-park-paladin/"
    root_html = (
        "<html><body><h1>Village Park at Paladin</h1>"
        "<p>101 Clifton Park Cir, Wilmington, DE 19802</p>"
        f'<a href="{portal}">Apply Online</a>'
        f"<p>{'property information ' * 40}</p></body></html>"
    )
    provider_fetch = FetchResult(
        url=portal,
        outcome=FetchOutcome.OK,
        status=200,
        body=index_html.encode(),
        headers={},
        render_mode=RenderMode.RENDER,
        final_url=portal,
        attempts=1,
        elapsed_ms=1,
    )
    fetch_calls: list[str] = []

    async def fake_fetch(task: CrawlTask, _profile: object = None) -> FetchResult:
        fetch_calls.append(task.url)
        assert task.url == portal
        return provider_fetch

    monkeypatch.setattr(fetch_module, "fetch", fake_fetch)
    root_fetch = FetchResult(
        url=root,
        outcome=FetchOutcome.OK,
        status=200,
        body=root_html.encode(),
        headers={},
        render_mode=RenderMode.RENDER,
        final_url=root,
        attempts=1,
        elapsed_ms=1,
    )
    task = CrawlTask(
        url=root,
        property_id="75314",
        priority=0,
        budget_ms=60_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.RENDER,
    )
    result = await scrape_jugnu(
        task,
        root_fetch,
        page=None,
        profile=None,
        csv_row={
            "apartmentid": "75314",
            "name": "Village Park at Paladin",
            "address": "101 Clifton Park Cir",
            "city": "Wilmington",
            "state": "DE",
            "zip": "19802",
            "website": root,
        },
    )

    assert fetch_calls == [portal]
    assert result["_adapter_used"] == "mri_prospectconnect"
    assert result["extraction_tier_used"] == "TIER_1_API_MRI_PROSPECTCONNECT"
    assert len(result["units"]) == 1
    assert result["units"][0]["unit_number"] == "1000"
    assert result["units"][0]["market_rent_low"] == 1845
    assert result["_link_hop_success"] is True
