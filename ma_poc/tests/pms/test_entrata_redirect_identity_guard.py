"""Fail closed when an Entrata vanity URL redirects to another property."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_module
from ma_poc.pms.scraper import (
    _entrata_cross_property_redirect_identity_rejected,
    scrape_jugnu,
)


def _fetch(
    entry_url: str,
    final_url: str,
    visible_html: str,
    *,
    entrata_marker: bool = True,
) -> FetchResult:
    provider_script = (
        "<script>const provider = 'https://example.com/Apartments/module/';</script>"
        if entrata_marker
        else ""
    )
    return FetchResult(
        url=entry_url,
        outcome=FetchOutcome.OK,
        status=200,
        body=(f"<html><head>{provider_script}</head><body>{visible_html}</body></html>").encode(),
        headers={"content-type": "text/html"},
        render_mode=RenderMode.GET,
        final_url=final_url,
        attempts=1,
        elapsed_ms=1,
    )


def test_verified_revive_to_lakes_contamination_is_rejected() -> None:
    entry = "https://www.reviveapartments.com/"
    final = "https://www.lakesatfife.com/"
    row = {
        "name": "Revive Apartments",
        "address": "2341 58th Ave E",
        "city": "Fife",
        "state": "WA",
        "zip": "98424",
    }
    destination = (
        "<script>const staleCampaign = 'Revive Apartments 2341 58th';</script>"
        "<h1>The Lakes</h1><address>2301 58th Avenue East, Fife, WA 98424</address>"
        "<a href='/fife/the-lakes/conventional/'>Floor Plans</a>"
    )

    assert _entrata_cross_property_redirect_identity_rejected(
        entry,
        _fetch(entry, final, destination),
        row,
    )


@pytest.mark.parametrize(
    ("entry", "final", "row", "destination"),
    [
        (
            "https://www.elevatetosequoia.com/apartments/ca/rancho-cordova/"
            "reserve-at-capital-center-apartment-homes/",
            "https://www.elevatetoreserve.com/",
            {
                "name": "The Reserve At Capital Center",
                "address": "3466 Data Dr",
            },
            "<h1>The Reserve at Capital Center</h1><address>3466 Data Drive</address>",
        ),
        (
            "https://www.elevatetosequoia.com/apartments/ca/sacramento/"
            "shore-park-at-riverlake/",
            "https://www.elevatetoshorepark.com/",
            {
                "name": "Shore Park at Riverlake",
                "address": "7952 Pocket Rd",
            },
            "<h1>Shore Park at Riverlake</h1><address>7952 Pocket Road</address>",
        ),
        (
            "https://www.elevatetosequoia.com/apartments/ca/vacaville/"
            "river-oaks-apartment-homes/",
            "https://www.elevatetoriveroaks.com/",
            {"name": "River Oaks", "address": "1000 Allison Dr"},
            "<h1>River Oaks Apartments</h1><address>1000 Allison Drive</address>",
        ),
    ],
)
def test_three_verified_legitimate_domain_migrations_are_allowed(
    entry: str,
    final: str,
    row: dict[str, str],
    destination: str,
) -> None:
    assert not _entrata_cross_property_redirect_identity_rejected(
        entry,
        _fetch(entry, final, destination),
        row,
    )


def test_same_host_or_non_entrata_redirect_is_out_of_scope() -> None:
    row = {"name": "Revive Apartments", "address": "2341 58th Ave E"}
    entry = "https://www.reviveapartments.com/"
    assert not _entrata_cross_property_redirect_identity_rejected(
        entry,
        _fetch(entry, entry, "<h1>Different Property</h1>"),
        row,
    )

    fetch = _fetch(
        entry,
        "https://unrelated.example/",
        "<h1>Different Property</h1>",
        entrata_marker=False,
    )
    assert not _entrata_cross_property_redirect_identity_rejected(entry, fetch, row)


@pytest.mark.asyncio
async def test_scrape_jugnu_rejects_before_adapter_profile_or_link_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def must_not_scrape(**_kwargs):
        raise AssertionError("identity-rejected Entrata redirect reached an adapter")

    async def must_not_link_hop(**_kwargs):
        raise AssertionError("identity-rejected Entrata redirect reached link-hop")

    monkeypatch.setattr(scraper_module, "scrape", must_not_scrape)
    monkeypatch.setattr(scraper_module, "_try_link_hop", must_not_link_hop)
    entry = "https://www.reviveapartments.com/"
    fetch = _fetch(
        entry,
        "https://www.lakesatfife.com/",
        "<h1>The Lakes</h1><address>2301 58th Avenue East</address>"
        "<a href='/fife/the-lakes/conventional/'>Floor Plans</a>",
    )
    row = {"name": "Revive Apartments", "address": "2341 58th Ave E"}

    result = await scrape_jugnu(
        SimpleNamespace(url=entry, property_id="42085"),
        fetch,
        profile=SimpleNamespace(),
        csv_row=row,
    )

    assert result["units"] == []
    assert result["plan_summaries"] == []
    assert result["extraction_tier_used"] == (
        "generic:entrata_redirect_identity_rejected"
    )
    assert result["_extract_result"].records == []
    rejected = result["_entrata_redirect_identity_rejected"]
    assert rejected["adapters_skipped"]
    assert rejected["link_hop_skipped"]
    assert any(
        "ENTRATA_REDIRECT_IDENTITY_REJECTED" in error
        for error in result["errors"]
    )
