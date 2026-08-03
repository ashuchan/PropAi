"""Precision gate for retired GSC property URLs in failed-no-data recovery."""

from __future__ import annotations

import json
from unittest.mock import patch

from ma_poc.discovery.rediscovery import (
    RediscoveryMethod,
    RediscoveryResult,
    RediscoveryStatus,
)
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.detector import DetectedPMS
from ma_poc.pms.scraper import (
    _rediscover_stale_gsc_property_url,
    _try_link_hop,
)

_OLD = "http://www.gscapts.com/apartments/Bradenton_FL/zip_34210/gsc/15388"
_NEW = (
    "https://gscapts.com/apartments/florida/"
    "harbour-pointe-apartment-homes/"
)


class _FakeEngine:
    def __init__(self, result: RediscoveryResult) -> None:
        self.result = result
        self.entries = []

    async def rediscover(self, entry):
        self.entries.append(entry)
        return self.result


async def test_exact_gsc_legacy_shape_accepts_confident_sitemap_result(
    monkeypatch,
) -> None:
    result = RediscoveryResult(
        property_id="6477",
        original_url=_OLD,
        status=RediscoveryStatus.RESOLVED,
        rediscovered_url=_NEW,
        method=RediscoveryMethod.MGMT_SITEMAP,
        confidence=1.0,
    )
    engine = _FakeEngine(result)
    monkeypatch.setattr(
        "ma_poc.discovery.rediscovery.RediscoveryEngine", lambda: engine
    )
    url = await _rediscover_stale_gsc_property_url(
        _OLD,
        DetectedPMS(pms="encoreskyline_template", confidence=0.85),
        "6477",
        {"proj_name": "Harbour Pointe", "city": "Bradenton", "state": "FL"},
    )
    assert url == _NEW
    assert engine.entries[0].name == "Harbour Pointe"


async def test_gate_rejects_unrelated_host_or_path_without_rediscovery(
    monkeypatch,
) -> None:
    class _MustNotConstruct:
        def __init__(self):
            raise AssertionError("rediscovery must stay behind the exact GSC gate")

    monkeypatch.setattr(
        "ma_poc.discovery.rediscovery.RediscoveryEngine", _MustNotConstruct
    )
    detected = DetectedPMS(pms="encoreskyline_template", confidence=0.85)
    row = {"proj_name": "Harbour Pointe"}
    assert await _rediscover_stale_gsc_property_url(
        "https://other.example/apartments/X/zip_34210/gsc/15388",
        detected,
        "6477",
        row,
    ) == ""
    assert await _rediscover_stale_gsc_property_url(
        "https://gscapts.com/apartments/florida/harbour-pointe/",
        detected,
        "6477",
        row,
    ) == ""


async def test_gate_withholds_ambiguous_or_cross_host_result(monkeypatch) -> None:
    detected = DetectedPMS(pms="encoreskyline_template", confidence=0.85)
    row = {"proj_name": "Harbour Pointe"}
    ambiguous = RediscoveryResult(
        property_id="6477",
        original_url=_OLD,
        status=RediscoveryStatus.AMBIGUOUS,
    )
    monkeypatch.setattr(
        "ma_poc.discovery.rediscovery.RediscoveryEngine",
        lambda: _FakeEngine(ambiguous),
    )
    assert await _rediscover_stale_gsc_property_url(
        _OLD, detected, "6477", row
    ) == ""

    cross_host = RediscoveryResult(
        property_id="6477",
        original_url=_OLD,
        status=RediscoveryStatus.RESOLVED,
        rediscovered_url="https://wrong.example/harbour-pointe/",
        method=RediscoveryMethod.MGMT_SITEMAP,
        confidence=1.0,
    )
    monkeypatch.setattr(
        "ma_poc.discovery.rediscovery.RediscoveryEngine",
        lambda: _FakeEngine(cross_host),
    )
    assert await _rediscover_stale_gsc_property_url(
        _OLD, detected, "6477", row
    ) == ""


async def test_stale_url_hop_reaches_static_jonah_units_end_to_end(
    monkeypatch,
) -> None:
    """Combined local path: stale URL -> sitemap hint -> adapter unit JSON."""
    from ma_poc.pms import scraper as scraper_module

    index = _NEW + "floorplans/"
    plan = index + "cayman/"
    resource = {
        "type": "floorplan",
        "title": "Cayman",
        "bedrooms": "1",
        "bathrooms": "1",
        "square_feet": "700",
        "units": [
            {
                "type": "unit",
                "apartment_number": "016",
                "availability_count": 1,
                "floorplan_title": "Cayman",
                "bedrooms": "1",
                "bathrooms": "1",
                "square_feet": "700",
                "price_entity": {
                    "date": "2026-08-10",
                    "adjusted": {"low_no_fees": "1345", "high_no_fees": "1345"},
                },
                "engrain_data": {"unit_id": "4133820"},
            }
        ],
    }
    plan_html = (
        "<script>jonahdigital</script>"
        "<script id='jd-fp-data-script-resource' type='application/json'>"
        + json.dumps(resource)
        + "</script>"
    )

    async def _fake_rediscovery(*_args, **_kwargs):
        return _NEW

    async def _fake_public_html(url: str):
        if url == index:
            return f"<a href='{plan}'>Cayman</a>", url
        if url == plan:
            return plan_html, url
        return "", url

    fetch_calls: list[str] = []

    async def _fake_fetch(task):
        fetch_calls.append(task.url)
        return FetchResult(
            url=task.url,
            outcome=FetchOutcome.OK,
            status=200,
            body=(
                b"<html><head><meta name='generator' content='Jonah Systems, LLC'>"
                b"</head><body><script src='https://cdn.jonahdigital.com/app.js'>"
                b"</script>Harbour Pointe Bradenton FL</body></html>"
            ),
            headers={"content-type": "text/html"},
            render_mode=RenderMode.RENDER,
            final_url=task.url,
            attempts=1,
            elapsed_ms=1,
        )

    monkeypatch.setattr(
        scraper_module, "_rediscover_stale_gsc_property_url", _fake_rediscovery
    )
    monkeypatch.setattr(
        "ma_poc.pms.adapters.encoreskyline_template._probe_public_html",
        _fake_public_html,
    )
    monkeypatch.setattr(
        scraper_module, "_crawl_get_gate_should_skip", lambda _url: False
    )
    with patch("ma_poc.fetch.fetch", _fake_fetch, create=True):
        result = await _try_link_hop(
            entry_url=_OLD,
            entry_page_html=(
                "<html><script src='https://cdn.jonahdigital.com/app.js'></script>"
                "portfolio homepage</html>"
            ),
            detected=DetectedPMS(
                pms="encoreskyline_template", confidence=0.85
            ),
            profile=None,
            expected_total_units=None,
            property_id="6477",
            csv_row={
                "proj_name": "Harbour Pointe",
                "city": "Bradenton",
                "state": "FL",
            },
            max_hops=3,
        )

    assert result is not None
    assert result["units"][0]["unit_number"] == "016"
    assert result["units"][0]["floor_plan_name"] == "Cayman"
    assert result["units"][0]["market_rent_low"] == 1345
    assert result["units"][0]["availability_date"] == "2026-08-10"
    assert fetch_calls[0] == _NEW
