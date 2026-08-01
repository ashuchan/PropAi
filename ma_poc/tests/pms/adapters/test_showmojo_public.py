"""Fail-closed official-manager ShowMojo recovery tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ma_poc.pms.adapters._showmojo_public import recover_showmojo_public
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.detector import DetectedPMS

CONFIGURED_URL = "https://www.parknorthsiderva.com/"
MANAGER_URL = "https://dobrinpropertymanagement.com/"
LISTINGS_URL = (
    "https://dobrinpropertymanagement.com/richmond-va-property-listings/"
)
EMBED_URL = "https://showmojo.com/fea92db007/listings/mapsearch"
SITE_ID = "44261A"


def _configured_html(*, manager_links: str | None = None) -> str:
    manager_markup = manager_links or (
        '<div title="Dobrin Properties Logo - Website Link">'
        f'<a href="{MANAGER_URL}"><img alt="Dobrin Properties Logo"></a>'
        "</div>"
    )
    return (
        "<html><body><h1>Park Northside</h1>"
        "<p>1601 Roane St, Richmond, VA 23222</p>"
        "<div><p>Managed by Dobrin Properties</p></div>"
        f"{manager_markup}"
        '<a href="https://www.rhris.com/ApplyNowRHR/ApplyNowRHR.cfm?'
        f'siteID={SITE_ID}&amp;OriginalURL=">Apply</a>'
        "</body></html>"
    )


def _manager_html(*, reciprocal: bool = True) -> str:
    property_link = (
        f'<a href="{CONFIGURED_URL}">Park Northside</a>' if reciprocal else ""
    )
    return (
        "<html><body>"
        f"{property_link}"
        f'<a href="{LISTINGS_URL}">All Properties</a>'
        "</body></html>"
    )


def _listings_html(*, iframe_urls: tuple[str, ...] = (EMBED_URL,)) -> str:
    iframes = "".join(f'<iframe src="{url}"></iframe>' for url in iframe_urls)
    return (
        "<html><body>"
        f'<a href="{CONFIGURED_URL}">Park Northside</a>'
        f"{iframes}</body></html>"
    )


def _card(
    uid: str,
    address: str,
    city_state_zip: str,
    highlights: str,
    *,
    availability: str = "Available now",
    rent: str = "$1,450",
) -> str:
    slug = "-".join(address.casefold().replace(".", "").split())
    return f"""
    <div class="cnt_box" id="uid_{uid}" data-listing-uid="{uid}">
      <div data-recheck-url="https://showmojo.com/promo_banner_check_v1?uid={uid}"></div>
      <div class="picture">
        <a class="schedule-a-showing" href="/l/{uid}?g=1&amp;sd=true">Photo</a>
      </div>
      <ul class="price_rooms">
        <li class="rent"><b>{rent}</b></li>
        <li class="br"><b>2</b> BR</li>
        <li class="ba"><b>1</b> BA</li>
        <li><b>725</b> SF</li>
      </ul>
      <div class="ss_btn">
        <a class="schedule-a-showing"
           href="/l/{uid}/{slug}-richmond-va-23222?g=1">Schedule</a>
      </div>
      <ul class="options"><li>{availability}</li><li>Apartment</li></ul>
      <div class="address"><p>{address}</p><p>{city_state_zip}</p></div>
      <div class="listing_highlights">{highlights}</div>
      <a class="apply_btn"
         href="https://www.rhris.com/ApplyNowRHR/ApplyNowRHR.cfm?siteID={SITE_ID}&amp;OriginalURL=">Apply</a>
    </div>
    """


def _roster(*cards: str) -> str:
    return f"<html><body>{''.join(cards)}</body></html>"


def _ctx(html: str | None = None) -> AdapterContext:
    return AdapterContext(
        base_url=CONFIGURED_URL,
        detected=DetectedPMS(pms="rentmanager", confidence=0.90),
        profile=None,
        expected_total_units=None,
        property_id="38378",
        fetch_result=SimpleNamespace(
            body=html if html is not None else _configured_html(),
            final_url=CONFIGURED_URL,
        ),
        property_name="Park Northside",
        address="1601 Roane St",
        city="Richmond",
        state="VA",
        zip_code="23222",
    )


def _responses(page_one: str, *, manager_html: str | None = None) -> dict[str, str]:
    return {
        MANAGER_URL: manager_html if manager_html is not None else _manager_html(),
        LISTINGS_URL: _listings_html(),
        f"{EMBED_URL}?page=1": page_one,
        f"{EMBED_URL}?page=2": _roster(),
    }


def _install_fetch(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[str, str],
) -> list[str]:
    from ma_poc.pms.adapters import _showmojo_public as module

    calls: list[str] = []

    async def fake_fetch(url: str, _referer: str):
        calls.append(url)
        if url not in responses:
            raise AssertionError(f"unexpected direct fetch: {url}")
        return responses[url], url

    monkeypatch.setattr(module, "_fetch_direct_html", fake_fetch)
    return calls


@pytest.mark.asyncio
async def test_exact_chain_accepts_property_and_rejects_three_same_roster_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted_uid = "e7c39f1061"
    graystone_uid = "2ae5ea2026"
    lakeview_uid = "097b680090"
    thomas_uid = "e3afa4f0bf"
    blank_availability_uid = "68cf877053"
    page = _roster(
        _card(
            accepted_uid,
            "1617 Brookfield St",
            "Richmond, VA 23222",
            "Recently updated units at Park Northside.",
            availability="Available September 7th",
            rent="$1,295",
        ),
        # Control 1: same account and valid native shape, wrong brand + ZIP.
        _card(
            graystone_uid,
            "2200 Lynhaven Ave",
            "Richmond, VA 23224",
            "Graystone Place Apartments",
        ),
        # Control 2: same account and valid native shape, wrong brand + ZIP.
        _card(
            lakeview_uid,
            "1919 Lakeview Ave - 1",
            "Richmond, VA 23220",
            "Come live in the Lakeview Place community.",
        ),
        # Control 3: misleading Park Northside template text, wrong ZIP.
        _card(
            thomas_uid,
            "1826 Thomas St",
            "Richmond, VA 23220",
            "Discover comfort at Park Northside Apartments.",
        ),
        # Exact brand/ZIP still does not pass without provider availability.
        _card(
            blank_availability_uid,
            "1700 Roane St",
            "Richmond, VA 23222",
            "Park Northside Apartments",
            availability="",
        ),
    )
    calls = _install_fetch(monkeypatch, _responses(page))
    ctx = _ctx()

    rows = await recover_showmojo_public(ctx)

    assert len(rows) == 1
    row = rows[0]
    assert row["unit_number"] == "1617 Brookfield St"
    assert row["market_rent_low"] == 1295
    assert row["availability_text"] == "Available September 7th"
    assert row["availability_date"] == ""
    assert row["floor_plan_name"] == ""
    assert row["source_ids"] == {
        "showmojo_account": "fea92db007",
        "showmojo_listing_uid": accepted_uid,
        "rhr_application_site_id": SITE_ID,
    }
    assert row["source_property_provenance"].startswith(
        "exact_configured_identity_managed_by_reciprocal_manager"
    )
    assert calls == [
        MANAGER_URL,
        LISTINGS_URL,
        f"{EMBED_URL}?page=1",
        f"{EMBED_URL}?page=2",
    ]

    telemetry = getattr(ctx, "_showmojo_official_chain")
    assert telemetry["portfolio_rows"] == 5
    assert telemetry["accepted_rows"] == 1
    rejected = {
        item["provider_listing_uid"]: item["reasons"]
        for item in telemetry["rejected_rows"]
    }
    assert "canonical_property_name_absent" in rejected[graystone_uid]
    assert "canonical_city_state_zip_mismatch" in rejected[graystone_uid]
    assert "canonical_property_name_absent" in rejected[lakeview_uid]
    assert "canonical_city_state_zip_mismatch" in rejected[lakeview_uid]
    assert "canonical_property_name_absent" not in rejected[thomas_uid]
    assert "canonical_city_state_zip_mismatch" in rejected[thomas_uid]
    assert rejected[blank_availability_uid] == [
        "no_explicit_provider_availability"
    ]


@pytest.mark.asyncio
async def test_ambiguous_managed_by_origins_fail_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters import _showmojo_public as module

    manager_markup = (
        '<div><a href="https://dobrinpropertymanagement.com/">'
        "Dobrin Properties</a></div>"
        '<div><a href="https://dobrin.example/">Dobrin Properties</a></div>'
    )

    async def forbidden(*_args: Any, **_kwargs: Any):
        raise AssertionError("ambiguous manager boundary must not be fetched")

    monkeypatch.setattr(module, "_fetch_direct_html", forbidden)
    assert await recover_showmojo_public(
        _ctx(_configured_html(manager_links=manager_markup))
    ) == []


@pytest.mark.asyncio
async def test_missing_manager_reciprocal_link_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fetch(
        monkeypatch,
        _responses(_roster(), manager_html=_manager_html(reciprocal=False)),
    )
    ctx = _ctx()

    assert await recover_showmojo_public(ctx) == []
    assert calls == [MANAGER_URL]
    assert getattr(ctx, "_showmojo_official_chain")["failure_reason"] == (
        "manager_reciprocal_or_roster_missing"
    )


@pytest.mark.asyncio
async def test_multiple_showmojo_iframe_accounts_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _responses(_roster())
    responses[LISTINGS_URL] = _listings_html(
        iframe_urls=(
            EMBED_URL,
            "https://showmojo.com/1111111111/listings/mapsearch",
        )
    )
    calls = _install_fetch(monkeypatch, responses)
    ctx = _ctx()

    assert await recover_showmojo_public(ctx) == []
    assert calls == [MANAGER_URL, LISTINGS_URL]
    assert getattr(ctx, "_showmojo_official_chain")["failure_reason"] == (
        "showmojo_iframe_boundary_failed"
    )


@pytest.mark.asyncio
async def test_duplicate_native_uid_across_pages_rejects_entire_roster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _card(
        "e7c39f1061",
        "1617 Brookfield St",
        "Richmond, VA 23222",
        "Park Northside Apartments",
    )
    responses = _responses(_roster(card))
    responses[f"{EMBED_URL}?page=2"] = _roster(card)
    responses[f"{EMBED_URL}?page=3"] = _roster()
    _install_fetch(monkeypatch, responses)
    ctx = _ctx()

    assert await recover_showmojo_public(ctx) == []
    assert getattr(ctx, "_showmojo_official_chain")["failure_reason"] == (
        "duplicate_showmojo_uid"
    )


@pytest.mark.asyncio
async def test_direct_fetch_explicitly_disables_proxy_and_unlocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters import _probe
    from ma_poc.pms.adapters import _showmojo_public as module

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
    result = await module._fetch_direct_html(MANAGER_URL, CONFIGURED_URL)

    assert result is not None
    assert observed["unlocker"] is False
    assert observed["proxies"] == {}
    assert observed["retries"] == 0


@pytest.mark.asyncio
async def test_narrow_scraper_bridge_preserves_plan_catalogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms import scraper as scraper_module
    from ma_poc.pms.adapters import _showmojo_public as module
    from ma_poc.pms.adapters._parsing import make_unit_dict

    async def fake_recovery(_ctx: AdapterContext):
        row = make_unit_dict(
            floor_plan_name="",
            bedrooms="2",
            bathrooms="1",
            sqft="725",
            unit_number="1617 Brookfield St",
            rent_low=1295,
            rent_high=1295,
            availability_status="AVAILABLE",
            source_api_url=f"{EMBED_URL}?page=1",
            extraction_tier="TIER_1_PUBLIC_SHOWMOJO_OFFICIAL_MANAGER_CHAIN",
            source_ids={"showmojo_listing_uid": "e7c39f1061"},
        )
        row["source_portal_url"] = EMBED_URL
        return [row]

    monkeypatch.setattr(module, "recover_showmojo_public", fake_recovery)
    plan = make_unit_dict(
        floor_plan_name="2 Bedroom / 1 Bath",
        bedrooms="2",
        bathrooms="1",
        rent_low=1195,
        rent_high=1195,
        source_api_url=f"{CONFIGURED_URL}floorplans",
        extraction_tier="GENERIC_PLAN_TEXT_PLAN_LEVEL",
    )
    # Legacy primary adapters can still place plan-only rows in ``units``;
    # the native bridge must upgrade the property without erasing that
    # first-party catalogue.
    previous = AdapterResult(units=[plan], tier_used="GENERIC_PLAN_TEXT")

    recovered = await scraper_module._try_page_published_native_recovery(
        _ctx(), previous
    )

    assert recovered is not None
    result, adapter_name = recovered
    assert adapter_name == "showmojo_public"
    assert result.tier_used == "TIER_1_PUBLIC_SHOWMOJO_OFFICIAL_MANAGER_CHAIN"
    assert len(result.units) == 1
    assert result.units[0]["unit_number"] == "1617 Brookfield St"
    assert result.plan_summaries == [plan]
