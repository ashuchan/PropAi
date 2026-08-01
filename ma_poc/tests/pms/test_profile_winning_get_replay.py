"""Bounded replay of a profile-proven static API winning page."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.models.scrape_profile import LlmFieldMapping, ScrapeProfile
from ma_poc.pms.detector import DetectedPMS
from ma_poc.pms.scraper import (
    _hop_surface_key,
    _profile_wpu_prefers_get,
    _try_link_hop,
)

_ENTRY = "https://prometheusapartments.com/ca/mountain-view/the-tillery"
_WPU = (
    "https://shopping.prometheusapartments-prod-west2.com/4989446/"
    "available-units?date=2026-08-03"
)
_MAPPING = {
    "unit_number": "unitNumber",
    "rent_low": "rent",
    "rent_high": "rent",
    "floor_plan_name": "floorPlanName",
    "bedrooms": "bedrooms",
    "bathrooms": "bathrooms",
    "sqft": "area",
}


def _profile(*, quality: float = 1.0) -> ScrapeProfile:
    profile = ScrapeProfile(canonical_id="247411")
    profile.navigation.winning_page_url = _WPU
    profile.api_hints.llm_field_mappings = [
        LlmFieldMapping(
            api_url_pattern=(
                "shopping.prometheusapartments-prod-west2.com/4989446/"
                "available-units"
            ),
            json_paths=_MAPPING,
            quality_score=quality,
        )
    ]
    return profile


def _fetch_result(body: bytes) -> FetchResult:
    return FetchResult(
        url=_WPU,
        outcome=FetchOutcome.OK,
        status=200,
        body=body,
        headers={"content-type": "application/json; charset=utf-8"},
        render_mode=RenderMode.GET,
        final_url=_WPU,
        attempts=1,
        elapsed_ms=8,
    )


def _budget() -> dict[str, Any]:
    return {
        "link_hop": 1,
        "llm_api_calls": 0,
        "llm_dom_calls": 0,
        "llm_monolithic": 0,
        "_cost_cap_usd": 0.0,
    }


def test_profile_wpu_get_gate_requires_exact_high_quality_identity_and_rent() -> None:
    profile = _profile()
    assert _profile_wpu_prefers_get(profile, _WPU, "profile:winning_page_url")
    assert not _profile_wpu_prefers_get(profile, _WPU, "profile:availability_link")
    assert not _profile_wpu_prefers_get(_profile(quality=0.4), _WPU, "profile:winning_page_url")

    profile.api_hints.llm_field_mappings[0].json_paths = {
        "unit_number": "unitNumber",
        "floor_plan_name": "floorPlanName",
    }
    assert not _profile_wpu_prefers_get(profile, _WPU, "profile:winning_page_url")


def test_wpu_surface_identity_ignores_scheme_www_query_and_fragment() -> None:
    assert _hop_surface_key("http://www.example.com/floorplans/?utm=x#top") == (
        _hop_surface_key("https://example.com/floorplans")
    )


@pytest.mark.asyncio
async def test_profile_wpu_direct_get_replays_top_level_json_to_strict_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: queue -> DIRECT GET -> mapping replay -> canonical unit."""
    body = b'''[{"unitNumber":"247","rent":"5032.0000",\
"floorPlanName":"Plan 1K","bedrooms":"1","bathrooms":"1","area":"794"}]'''
    seen_modes: list[RenderMode] = []

    async def _fetch(task: Any) -> FetchResult:
        seen_modes.append(task.render_mode)
        return _fetch_result(body)

    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_CRAWL_GET_GATE", False)
    with patch("ma_poc.fetch.fetch", new=_fetch):
        result = await _try_link_hop(
            entry_url=_ENTRY,
            entry_page_html="<html><body>native plan-only page</body></html>",
            detected=DetectedPMS(pms="unknown", confidence=0.0),
            profile=_profile(),
            expected_total_units=None,
            property_id="247411",
            csv_row={"name": "The Tillery", "website": _ENTRY},
            max_hops=1,
            visited_urls={_ENTRY},
            shared_budget=_budget(),
        )

    assert result is not None
    assert seen_modes == [RenderMode.GET]
    assert len(result.get("units") or []) == 1
    unit = result["units"][0]
    assert unit_has_real_anchor(unit) is True
    assert unit["unit_id"] == "247"
    assert unit["market_rent_low"] == 5032.0
    assert result["extraction_tier_used"] == "TIER_1_PROFILE_MAPPING"


@pytest.mark.asyncio
async def test_profile_wpu_get_identity_miss_preserves_plan_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live price without a live apartment anchor remains plan evidence."""
    body = b'''[{"unitNumber":null,"rent":"5032.0000",\
"floorPlanName":"Plan 1K","bedrooms":"1","bathrooms":"1","area":"794"}]'''

    async def _fetch(task: Any) -> FetchResult:
        assert task.render_mode == RenderMode.GET
        return _fetch_result(body)

    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_CRAWL_GET_GATE", False)
    with patch("ma_poc.fetch.fetch", new=_fetch):
        result = await _try_link_hop(
            entry_url=_ENTRY,
            entry_page_html="<html><body>native plan-only page</body></html>",
            detected=DetectedPMS(pms="unknown", confidence=0.0),
            profile=_profile(),
            expected_total_units=None,
            property_id="247411",
            csv_row={"name": "The Tillery", "website": _ENTRY},
            max_hops=1,
            visited_urls={_ENTRY},
            shared_budget=_budget(),
        )

    assert result is not None
    assert result.get("_units_empty") is True
    assert not result.get("units")
    assert len(result.get("plan_summaries") or []) == 1
    assert result["plan_summaries"][0]["floor_plan_name"] == "Plan 1K"


@pytest.mark.asyncio
async def test_profile_wpu_get_rejects_identity_without_positive_numeric_rent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b'''[{"unitNumber":"247","rent":"Call for rent",\
"floorPlanName":"Plan 1K","bedrooms":"1","bathrooms":"1","area":"794"}]'''

    async def _fetch(task: Any) -> FetchResult:
        assert task.render_mode == RenderMode.GET
        return _fetch_result(body)

    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_CRAWL_GET_GATE", False)
    with patch("ma_poc.fetch.fetch", new=_fetch):
        result = await _try_link_hop(
            entry_url=_ENTRY,
            entry_page_html="<html><body>native plan-only page</body></html>",
            detected=DetectedPMS(pms="unknown", confidence=0.0),
            profile=_profile(),
            expected_total_units=None,
            property_id="247411",
            csv_row={"name": "The Tillery", "website": _ENTRY},
            max_hops=1,
            visited_urls={_ENTRY},
            shared_budget=_budget(),
        )

    assert result is not None
    assert result.get("_units_empty") is True
    assert not result.get("units")
