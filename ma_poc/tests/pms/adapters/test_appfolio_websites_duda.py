"""AppFolio Websites (Duda CMS) collection-API fallback (deep-probe 2026-05-25).

User-flagged Cluster C: AppFolio vanity sites where the existing adapter
visits ``<slug>.appfolio.com/listings`` (or finds no slug at all) and
returns 0 units, yet the marketing page actually serves AppFolio data
through Duda's public collections REST API at::

    /rts/collections/public/{site_id}/runtime/collection/appfolio-listings
        /query-data?pageSize=100&pageNumber={N}&query=()&language=ENGLISH

The marker is the ``cdn.appfoliowebsites.com/sites/resources/`` loader
that every AppFolio Websites page injects. Sample probes (8/80 random CSV
rows, ~10% of properties):

  * livescs (SCS Athens) — 256 listings, propertyGroup-filtered
  * parkviewspringhill — 37 listings
  * beaumontcove — 56 listings
  * pearlinvestment / wind chase — 17 listings

These tests pin the helpers (marker, site-id, property-group, parser)
and the end-to-end adapter path against live HTML fixtures from SCS
Athens + Beaumont Cove.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ma_poc.pms.adapters._appfolio_websites_duda import (
    collection_url,
    extract_appfolio_websites_property_group,
    extract_duda_site_id,
    is_appfolio_websites_cms,
    listing_matches_property_group,
    origin_from_url,
    parse_appfolio_websites_listing,
    parse_collection_payload,
)
from ma_poc.pms.adapters.appfolio import AppFolioAdapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import detect_pms

FIXTURES = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "appfolio_websites_duda"
)


@dataclass
class _StubFetchResult:
    body: bytes | str | None
    final_url: str = ""


class _DummyPage:
    """Adapter uses fetch_result.body — page is unused."""


# ─────────────────────────────────────────────────────────────────────
# is_appfolio_websites_cms — detects the AppFolio Websites loader.
# ─────────────────────────────────────────────────────────────────────


def test_marker_detected_on_scs_athens_live_fixture() -> None:
    html = (FIXTURES / "scs_athens_property.html").read_text()
    assert is_appfolio_websites_cms(html)


def test_marker_not_present_on_generic_html() -> None:
    html = (
        "<html><body><a href='https://www.appfolio.com/terms/listings'>"
        "Terms</a></body></html>"
    )
    assert not is_appfolio_websites_cms(html)


def test_marker_empty_input_returns_false() -> None:
    assert not is_appfolio_websites_cms("")
    assert not is_appfolio_websites_cms(None)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────
# extract_duda_site_id — pulls the hex site-id from cdn-website.com URLs.
# ─────────────────────────────────────────────────────────────────────


def test_site_id_extracted_from_scs_athens() -> None:
    html = (FIXTURES / "scs_athens_property.html").read_text()
    assert extract_duda_site_id(html) == "3885d159"


def test_site_id_extracted_from_lirp_subdomain() -> None:
    html = (
        "<img src='https://lirp.cdn-website.com/abc12345/dms3rep/multi/opt"
        "/photo.jpg'/>"
    )
    assert extract_duda_site_id(html) == "abc12345"


def test_site_id_none_when_no_duda_assets() -> None:
    html = "<html><body>No Duda here</body></html>"
    assert extract_duda_site_id(html) is None


# ─────────────────────────────────────────────────────────────────────
# extract_appfolio_websites_property_group — base64-decoded JSON config.
# ─────────────────────────────────────────────────────────────────────


def test_property_group_decoded_from_scs_athens_live_fixture() -> None:
    """SCS Athens property page encodes propertyGroup='SCS Athens' inside
    a base64 binding payload."""
    html = (FIXTURES / "scs_athens_property.html").read_text()
    assert extract_appfolio_websites_property_group(html) == "SCS Athens"


def test_property_group_none_on_site_wide_page() -> None:
    """The /availability site-wide page sets propertyGroup='' so we must
    return None (the filter is a no-op)."""
    cfg = json.dumps({"propertyGroup": "", "initialSort": "Most Recent"})
    blob = base64.b64encode(cfg.encode()).decode()
    # Pad to >= 64 chars so the regex picks it up
    html = f"<div data-binding='{blob}'></div>"
    if len(blob) < 64:
        # synthesise additional padding via a longer payload
        cfg = json.dumps({"propertyGroup": "", "padding": "x" * 80})
        blob = base64.b64encode(cfg.encode()).decode()
        html = f"<div data-binding='{blob}'></div>"
    assert extract_appfolio_websites_property_group(html) is None


def test_property_group_ignores_binding_path_shape() -> None:
    """Shape 2 — a binding LIST where ``value`` is a path like
    ``dynamic_page_collection.Property Groups`` — must NOT be returned as
    the propertyGroup (that's a Duda binding path, not a literal value).
    """
    binding = [
        {
            "bindingName": "propertyGroup",
            "value": "dynamic_page_collection.Property Groups",
        }
    ]
    blob = base64.b64encode(json.dumps(binding).encode()).decode()
    html = f"<div data-something='{blob}'></div>"
    assert extract_appfolio_websites_property_group(html) is None


def test_property_group_none_when_no_base64_blob() -> None:
    assert extract_appfolio_websites_property_group("<html></html>") is None
    assert extract_appfolio_websites_property_group("") is None


# ─────────────────────────────────────────────────────────────────────
# listing_matches_property_group — case-insensitive name match.
# ─────────────────────────────────────────────────────────────────────


def test_property_group_match_case_insensitive() -> None:
    data = {
        "property_lists": [
            {"id": 1, "name": "all"},
            {"id": 27, "name": "scs athens"},
        ]
    }
    assert listing_matches_property_group(data, "SCS Athens")
    assert listing_matches_property_group(data, "scs athens")
    assert listing_matches_property_group(data, "SCS ATHENS")


def test_property_group_no_match_filters_listing_out() -> None:
    data = {"property_lists": [{"id": 1, "name": "northern"}]}
    assert not listing_matches_property_group(data, "SCS Athens")


def test_property_group_none_passes_through_all() -> None:
    data = {"property_lists": [{"id": 1, "name": "anything"}]}
    assert listing_matches_property_group(data, None)
    assert listing_matches_property_group(data, "")


def test_property_group_no_property_lists_field() -> None:
    """Listings without property_lists are excluded from a filtered query
    but admitted when there's no filter."""
    assert listing_matches_property_group({}, None)
    assert not listing_matches_property_group({}, "SCS Athens")


# ─────────────────────────────────────────────────────────────────────
# parse_appfolio_websites_listing — single-record → unit_dict.
# ─────────────────────────────────────────────────────────────────────


def test_parse_listing_happy_path() -> None:
    data = {
        "data": {
            "full_address": "715 Caroline Street #49, Athens, WI 54411",
            "address_address2": "#49",
            "bedrooms": 3,
            "bathrooms": 2.0,
            "square_feet": 1186.0,
            "market_rent": 1595.0,
            "rent_range": [1595.0, 1595.0],
            "available": True,
            "available_date": "2026-06-15",
            "deposit": 1595.0,
            "listable_uid": "abc12345",
            "id": 11,
            "database_name": "scswiderski",
            "unit_template_name": "Willow 3 BR Lower",
        }
    }
    u = parse_appfolio_websites_listing(data, "https://api/x")
    assert u is not None
    assert u["bedrooms"] == "3"
    assert u["bathrooms"] == "2.0"
    assert u["sqft"] == "1186"
    assert u["unit_number"] == "49"
    assert u["market_rent_low"] == 1595
    assert u["market_rent_high"] == 1595
    assert u["availability_status"] == "AVAILABLE"
    assert u["availability_date"] == "2026-06-15"
    assert u["extraction_tier"] == "TIER_1_API_APPFOLIO_DUDA"
    assert u["source_ids"]["appfolio_listable_uid"] == "abc12345"
    assert u["source_ids"]["appfolio_database_name"] == "scswiderski"


def test_parse_listing_unavailable_status() -> None:
    data = {
        "data": {
            "full_address": "1 Test St",
            "bedrooms": 1,
            "bathrooms": 1.0,
            "available": False,
            "market_rent": 1000.0,
        }
    }
    u = parse_appfolio_websites_listing(data, "https://api/x")
    assert u is not None
    assert u["availability_status"] == "UNAVAILABLE"


def test_parse_listing_dropped_when_no_dimension_and_no_rent() -> None:
    """A row with neither beds/baths/sqft nor a rent is dim-less and would
    fail the downstream validity gate — drop it up-front."""
    data = {"data": {"full_address": "1 Test St"}}
    assert parse_appfolio_websites_listing(data, "https://x") is None


def test_parse_listing_returns_none_for_bad_input() -> None:
    assert parse_appfolio_websites_listing(None, "x") is None  # type: ignore[arg-type]
    assert parse_appfolio_websites_listing({}, "x") is None
    assert parse_appfolio_websites_listing({"data": "not-a-dict"}, "x") is None


# ─────────────────────────────────────────────────────────────────────
# parse_collection_payload — full page, with property-group filtering.
# ─────────────────────────────────────────────────────────────────────


def test_parse_collection_unfiltered_returns_all() -> None:
    """Site-wide /availability page has propertyGroup=None — every record
    with extractable dims becomes a unit."""
    payload = json.loads(
        (FIXTURES / "scs_collection_page0.json").read_text()
    )
    units, total_pages = parse_collection_payload(
        payload, "https://www.livescs.com/api/x", property_group=None
    )
    assert len(units) == 10
    assert total_pages == 1
    # All have AppFolio Duda tier
    assert all(u["extraction_tier"] == "TIER_1_API_APPFOLIO_DUDA" for u in units)


def test_parse_collection_filtered_to_scs_athens() -> None:
    """SCS Athens property page filters down to exactly the 4 Athens
    listings the fixture contains (715 Caroline Street suffixes)."""
    payload = json.loads(
        (FIXTURES / "scs_collection_page0.json").read_text()
    )
    units, _ = parse_collection_payload(
        payload, "https://www.livescs.com/api/x", property_group="SCS Athens"
    )
    assert len(units) == 4
    for u in units:
        # Every Athens unit is at the 715 Caroline address
        assert "Caroline" in u["floor_plan_name"] or u["bedrooms"]


def test_parse_collection_filter_no_matches() -> None:
    payload = json.loads(
        (FIXTURES / "scs_collection_page0.json").read_text()
    )
    units, _ = parse_collection_payload(
        payload,
        "https://x/api",
        property_group="A Group That Does Not Exist",
    )
    assert units == []


def test_parse_collection_handles_malformed_payload() -> None:
    assert parse_collection_payload(None, "x", None) == ([], 0)  # type: ignore[arg-type]
    assert parse_collection_payload({}, "x", None) == ([], 0)
    assert parse_collection_payload({"values": "not-a-list"}, "x", None) == ([], 0)


# ─────────────────────────────────────────────────────────────────────
# collection_url / origin_from_url
# ─────────────────────────────────────────────────────────────────────


def test_collection_url_construction() -> None:
    url = collection_url("https://www.livescs.com", "3885d159", page_number=0)
    assert url == (
        "https://www.livescs.com/rts/collections/public/3885d159/runtime/"
        "collection/appfolio-listings/query-data?pageSize=100&pageNumber=0"
        "&query=()&language=ENGLISH"
    )


def test_collection_url_strips_trailing_slash() -> None:
    url = collection_url("https://x.com/", "abc", 2)
    assert url.startswith("https://x.com/rts/")


def test_origin_from_url() -> None:
    assert origin_from_url("https://www.livescs.com/property/scs-athens") == (
        "https://www.livescs.com"
    )
    assert origin_from_url("") == ""
    assert origin_from_url("not-a-url") == ""


# ─────────────────────────────────────────────────────────────────────
# AppFolioAdapter.extract — end-to-end Duda CMS fallback.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_duda_fallback_fetches_and_parses_scs_athens(mocker) -> None:
    """End-to-end against the SCS Athens live HTML fixture. The adapter
    must detect the AppFolio Websites marker, extract site_id +
    propertyGroup, call the collection API (mocked), filter to Athens
    listings, and return Tier-1 unit dicts.
    """
    html = (FIXTURES / "scs_athens_property.html").read_text()
    collection_payload = json.loads(
        (FIXTURES / "scs_collection_page0.json").read_text()
    )

    expected_url = collection_url(
        "https://www.livescs.com", "3885d159", page_number=0
    )

    mock_response = mocker.Mock()
    mock_response.status_code = 200
    mock_response.json = mocker.Mock(return_value=collection_payload)

    calls: list[str] = []

    async def _mock_get(self, url, headers=None):  # noqa: ANN001
        calls.append(url)
        return mock_response

    mocker.patch("httpx.AsyncClient.get", _mock_get)

    ctx = AdapterContext(
        base_url="https://www.livescs.com/property/scs-athens",
        detected=detect_pms(
            "https://www.livescs.com/property/scs-athens", page_html=html
        ),
        profile=None,
        expected_total_units=None,
        property_id="284175",
        fetch_result=_StubFetchResult(body=html.encode("utf-8")),
    )
    result = await AppFolioAdapter().extract(_DummyPage(), ctx)

    assert result.tier_used == "TIER_1_API_APPFOLIO_DUDA"
    assert len(result.units) == 4  # 4 Athens-tagged listings in the fixture
    assert result.winning_url == expected_url
    assert result.confidence >= 0.7
    assert calls == [expected_url]


@pytest.mark.asyncio
async def test_adapter_duda_fallback_paginates(mocker) -> None:
    """When the API reports totalPages>1, the adapter must walk every
    page (up to the safety cap of 10)."""
    html = (FIXTURES / "scs_athens_property.html").read_text()

    # Three-page response: page 0 / 1 / 2, all returning 1 listing each.
    def _make_page(n: int) -> dict[str, Any]:
        return {
            "name": "appfolio-listings",
            "values": [
                {
                    "data": {
                        "full_address": f"Page {n} Listing",
                        "bedrooms": 2,
                        "bathrooms": 1.0,
                        "market_rent": 1000.0,
                        "available": True,
                        "address_address2": f"P{n}",
                        "listable_uid": f"uid{n}",
                        "id": 100 + n,
                        "property_lists": [{"id": 27, "name": "scs athens"}],
                    }
                }
            ],
            "page": {
                "pageSize": 100,
                "pageNumber": n,
                "totalPages": 3,
                "totalItems": 3,
            },
        }

    pages = [_make_page(0), _make_page(1), _make_page(2)]

    def _make_resp(payload):  # noqa: ANN001
        r = mocker.Mock()
        r.status_code = 200
        r.json = mocker.Mock(return_value=payload)
        return r

    responses = [_make_resp(p) for p in pages]
    call_count = {"n": 0}

    async def _mock_get(self, url, headers=None):  # noqa: ANN001
        i = call_count["n"]
        call_count["n"] += 1
        return responses[i]

    mocker.patch("httpx.AsyncClient.get", _mock_get)

    ctx = AdapterContext(
        base_url="https://www.livescs.com/property/scs-athens",
        detected=detect_pms(
            "https://www.livescs.com/property/scs-athens", page_html=html
        ),
        profile=None,
        expected_total_units=None,
        property_id="284175",
        fetch_result=_StubFetchResult(body=html.encode("utf-8")),
    )
    result = await AppFolioAdapter().extract(_DummyPage(), ctx)

    assert result.tier_used == "TIER_1_API_APPFOLIO_DUDA"
    assert call_count["n"] == 3
    assert len(result.units) == 3


@pytest.mark.asyncio
async def test_adapter_duda_fallback_skipped_when_no_marker(mocker) -> None:
    """A bare AppFolio vanity page (no Websites CMS loader) must NOT
    fire the Duda fetch — the existing slug-vanity path handles it.
    """
    bare_html = (
        '<html><body>'
        '<a href="https://carltonequities.appfolio.com/connect">Apply</a>'
        '</body></html>'
    )
    spy = mocker.patch("httpx.AsyncClient.get")
    # Set up a fake return so the slug-vanity path doesn't crash.
    fake = mocker.Mock(status_code=404, text="")

    async def _mock_get(self, url, headers=None):  # noqa: ANN001
        return fake

    mocker.patch("httpx.AsyncClient.get", _mock_get)

    ctx = AdapterContext(
        base_url="https://example.com/",
        detected=detect_pms("https://example.com/", page_html=bare_html),
        profile=None,
        expected_total_units=None,
        property_id="X",
        fetch_result=_StubFetchResult(body=bare_html.encode("utf-8")),
    )
    result = await AppFolioAdapter().extract(_DummyPage(), ctx)
    # No Duda tier emitted (the only fetch attempted is the slug-vanity
    # one, which 404s).
    assert result.tier_used != "TIER_1_API_APPFOLIO_DUDA"


@pytest.mark.asyncio
async def test_adapter_duda_fallback_logs_error_on_exception(mocker) -> None:
    """If httpx raises mid-fetch, the adapter records an error and
    returns 0 units — it must NOT crash."""
    html = (FIXTURES / "scs_athens_property.html").read_text()

    async def _mock_get(self, url, headers=None):  # noqa: ANN001
        raise RuntimeError("simulated network failure")

    mocker.patch("httpx.AsyncClient.get", _mock_get)

    ctx = AdapterContext(
        base_url="https://www.livescs.com/property/scs-athens",
        detected=detect_pms(
            "https://www.livescs.com/property/scs-athens", page_html=html
        ),
        profile=None,
        expected_total_units=None,
        property_id="284175",
        fetch_result=_StubFetchResult(body=html.encode("utf-8")),
    )
    result = await AppFolioAdapter().extract(_DummyPage(), ctx)
    assert result.units == []
    assert any(
        "appfolio-websites-duda-error" in e for e in result.errors
    )


@pytest.mark.asyncio
async def test_adapter_duda_fallback_beaumont_cove_live(mocker) -> None:
    """Second cohort sample: Beaumont Cove (Tulsa). Different operator,
    different Duda site_id (cbad8b42), same path. Pins the helper
    extraction against a non-SCS live HTML.
    """
    html = (FIXTURES / "beaumont_cove_home.html").read_text()
    collection_payload = json.loads(
        (FIXTURES / "beaumont_collection_page0.json").read_text()
    )
    assert is_appfolio_websites_cms(html)
    assert extract_duda_site_id(html) == "cbad8b42"

    expected_url = collection_url(
        "https://www.beaumontcove.net", "cbad8b42", 0
    )
    mock_response = mocker.Mock()
    mock_response.status_code = 200
    mock_response.json = mocker.Mock(return_value=collection_payload)

    async def _mock_get(self, url, headers=None):  # noqa: ANN001
        assert url == expected_url
        return mock_response

    mocker.patch("httpx.AsyncClient.get", _mock_get)

    ctx = AdapterContext(
        base_url="https://www.beaumontcove.net/",
        detected=detect_pms(
            "https://www.beaumontcove.net/", page_html=html
        ),
        profile=None,
        expected_total_units=None,
        property_id="224569",
        fetch_result=_StubFetchResult(body=html.encode("utf-8")),
    )
    result = await AppFolioAdapter().extract(_DummyPage(), ctx)

    assert result.tier_used == "TIER_1_API_APPFOLIO_DUDA"
    # Beaumont home page has no propertyGroup config (it's the site root)
    # so every listing in the fixture (5) becomes a unit.
    assert len(result.units) == 5
    assert all(
        u["source_ids"].get("appfolio_database_name") for u in result.units
    )
