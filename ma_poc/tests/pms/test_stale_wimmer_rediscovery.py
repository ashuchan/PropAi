"""Fail-closed recovery for Wimmer's retired missing-state property URLs."""

from __future__ import annotations

from unittest.mock import patch

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_module
from ma_poc.pms.detector import DetectedPMS
from ma_poc.pms.scraper import (
    _rediscover_stale_wimmer_floorplans_url,
    _try_link_hop,
    _wimmer_floorplans_identity_matches,
)

_OLD = (
    "https://www.wimmercommunities.com/apartments/menomonee-falls/"
    "riverwalk-on-the-falls/"
)
_CURRENT = (
    "https://www.wimmercommunities.com/apartments/wi/menomonee-falls/"
    "riverwalk-on-the-falls/floorplans"
)
_ROW = {
    "name": "Riverwalk on the Falls I",
    "city": "Menomonee Falls",
    "state": "WI",
    "zip": "53051",
}


def _current_html(
    *,
    name: str = "RiverWalk on the Falls",
    city: str = "Menomonee Falls",
    zip_code: str = "53051",
    canonical: str = _CURRENT,
) -> str:
    return (
        "<html><head>"
        f"<title>Floor Plans of {name} in {city}, WI</title>"
        f"<link rel='canonical' href='{canonical}'>"
        "</head><body>"
        f"<h1>{name}</h1><address>W165N8910 Grand Avenue, {city}, WI "
        f"{zip_code}</address>"
        + (" exact-property-content" * 1_500)
        + "</body></html>"
    )


def _fetch(url: str, html: str) -> FetchResult:
    return FetchResult(
        url=url,
        outcome=FetchOutcome.OK,
        status=200,
        body=html.encode(),
        headers={"content-type": "text/html"},
        render_mode=RenderMode.RENDER,
        final_url=url,
        attempts=1,
        elapsed_ms=1,
    )


def test_exact_missing_state_shape_builds_current_floorplans_url() -> None:
    assert _rediscover_stale_wimmer_floorplans_url(
        _OLD, "<html>branded portfolio 404</html>", _ROW
    ) == _CURRENT


def test_transform_rejects_live_or_unrelated_boundaries() -> None:
    # The old-shape page is still live when it contains configured identity.
    assert _rediscover_stale_wimmer_floorplans_url(
        _OLD, "RiverWalk on the Falls", _ROW
    ) == ""
    assert _rediscover_stale_wimmer_floorplans_url(
        _OLD, "portfolio 404", {**_ROW, "state": "IL"}
    ) == ""
    assert _rediscover_stale_wimmer_floorplans_url(
        _CURRENT, "portfolio 404", _ROW
    ) == ""
    assert _rediscover_stale_wimmer_floorplans_url(
        "https://other.example/apartments/menomonee-falls/riverwalk-on-the-falls/",
        "portfolio 404",
        _ROW,
    ) == ""


def test_candidate_requires_exact_identity_and_canonical_boundary() -> None:
    assert _wimmer_floorplans_identity_matches(
        _CURRENT, _fetch(_CURRENT, _current_html()), _ROW
    )
    assert not _wimmer_floorplans_identity_matches(
        _CURRENT,
        _fetch(_CURRENT, _current_html(name="Oakton Beach")),
        _ROW,
    )
    assert not _wimmer_floorplans_identity_matches(
        _CURRENT,
        _fetch(_CURRENT, _current_html(zip_code="53072")),
        _ROW,
    )
    assert not _wimmer_floorplans_identity_matches(
        _CURRENT,
        _fetch(
            _CURRENT,
            _current_html(
                canonical=(
                    "https://www.wimmercommunities.com/apartments/wi/pewaukee/"
                    "oakton-beach/floorplans"
                )
            ),
        ),
        _ROW,
    )


async def test_link_hop_recovers_only_exact_transformed_page(monkeypatch) -> None:
    foreign = "".join(
        f"<a href='https://www.wimmercommunities.com/apartments/wi/x/p{i}/floorplans'>"
        "Floor Plans</a>"
        for i in range(6)
    )
    fetched: list[str] = []

    async def fake_fetch(task):
        fetched.append(task.url)
        return _fetch(task.url, _current_html())

    async def fake_scrape(**_kwargs):
        return {
            "units": [
                {
                    "unit_number": "324",
                    "floor_plan_name": "Phase 1 - 1 Bed 1 Bath (D)",
                    "market_rent_low": 2024,
                    "source_api_url": (
                        "https://sightmap.com/app/api/v1/y8px5ljmv19/"
                        "sightmaps/100325"
                    ),
                }
            ],
            "plan_summaries": [],
            "extraction_tier_used": "TIER_1_API_SIGHTMAP_IFRAME",
            "_embedded_floorplan_subpage_hints": [],
        }

    monkeypatch.setattr(scraper_module, "scrape", fake_scrape)
    monkeypatch.setattr(
        "ma_poc.config.feature_flags.ENABLE_CRAWL_GET_GATE", False
    )
    with patch("ma_poc.fetch.fetch", fake_fetch, create=True):
        result = await _try_link_hop(
            entry_url=_OLD,
            entry_page_html=f"<html>portfolio 404{foreign}</html>",
            detected=DetectedPMS(pms="rentcafe", confidence=0.7),
            profile=None,
            expected_total_units=None,
            property_id="71534",
            csv_row=_ROW,
        )

    assert result is not None
    assert result["units"][0]["unit_number"] == "324"
    assert result["_link_hop_anchor"] == "rediscovery:wimmer_state_path"
    assert result["_wimmer_stale_path_recovery"] == {
        "entry_url": _OLD,
        "exact_floorplans_url": _CURRENT,
        "identity_match": True,
        "portfolio_fallbacks_disabled": True,
    }
    assert fetched == [_CURRENT]


async def test_zero_units_fail_closed_without_fetching_portfolio_links(
    monkeypatch,
) -> None:
    foreign_urls = [
        f"https://www.wimmercommunities.com/apartments/wi/greenfield/p{i}/floorplans"
        for i in range(6)
    ]
    entry_html = "<html>portfolio 404" + "".join(
        f"<a href='{url}'>Floor Plans</a>" for url in foreign_urls
    ) + "</html>"
    fetched: list[str] = []

    async def fake_fetch(task):
        fetched.append(task.url)
        return _fetch(task.url, _current_html())

    async def fake_scrape(**_kwargs):
        return {
            "units": [],
            "plan_summaries": [{"floor_plan_name": "Phase 1 A"}],
            "_embedded_floorplan_subpage_hints": [
                (foreign_urls[0], "foreign-plan")
            ],
        }

    monkeypatch.setattr(scraper_module, "scrape", fake_scrape)
    monkeypatch.setattr(
        "ma_poc.config.feature_flags.ENABLE_CRAWL_GET_GATE", False
    )
    with patch("ma_poc.fetch.fetch", fake_fetch, create=True):
        result = await _try_link_hop(
            entry_url=_OLD,
            entry_page_html=entry_html,
            detected=DetectedPMS(pms="rentcafe", confidence=0.7),
            profile=None,
            expected_total_units=None,
            property_id="71534",
            csv_row=_ROW,
        )

    assert result is not None
    assert result["_units_empty"] is True
    assert result["plan_summaries"] == [{"floor_plan_name": "Phase 1 A"}]
    assert fetched == [_CURRENT]


async def test_identity_mismatch_stops_before_extraction(monkeypatch) -> None:
    fetched: list[str] = []

    async def fake_fetch(task):
        fetched.append(task.url)
        return _fetch(task.url, _current_html(name="Oakton Beach"))

    async def must_not_scrape(**_kwargs):
        raise AssertionError("identity-rejected Wimmer body must not be extracted")

    monkeypatch.setattr(scraper_module, "scrape", must_not_scrape)
    monkeypatch.setattr(
        "ma_poc.config.feature_flags.ENABLE_CRAWL_GET_GATE", False
    )
    with patch("ma_poc.fetch.fetch", fake_fetch, create=True):
        result = await _try_link_hop(
            entry_url=_OLD,
            entry_page_html="<html>portfolio 404</html>",
            detected=DetectedPMS(pms="rentcafe", confidence=0.7),
            profile=None,
            expected_total_units=None,
            property_id="71534",
            csv_row=_ROW,
        )

    assert result is not None
    assert result["_wimmer_stale_path_identity_rejected"] is True
    assert fetched == [_CURRENT]
