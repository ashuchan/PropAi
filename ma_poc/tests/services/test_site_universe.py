"""Tests for site_universe.gather_universe — the gather phase."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ma_poc.services.site_universe import (
    ApiCandidate,
    LinkCandidate,
    apply_llm_scores,
    candidate_summary_for_ranking,
    candidates_by_url,
    gather_universe,
)


@dataclass
class _BlockedEntry:
    url_pattern: str


@dataclass
class _ApiHints:
    blocked_endpoints: list[Any] = field(default_factory=list)


@dataclass
class _FakeProfile:
    api_hints: _ApiHints = field(default_factory=_ApiHints)


_BASIC_HTML = """
<html><body>
<a href="/floor-plans">View Floor Plans</a>
<a href="/availability">Check Availability</a>
<a href="/contact">Contact Us</a>
<a href="https://example.entrata.com/leasing">Lease Online</a>
<a href="https://www.facebook.com/properties/123">Facebook</a>
<a href="https://maps.googleapis.com/maps">map</a>
<a href="tel:+15551234567">Call</a>
<a href="#hero">Anchor</a>
<a href="/floor-plans">Duplicate Floor Plans</a>
</body></html>
"""


def test_gather_universe_returns_empty_universe_for_blank_inputs() -> None:
    u = gather_universe(html=None, base_url="https://example.com", api_responses=None)
    assert u.api_candidates == []
    assert u.internal_links == []
    assert u.external_links == []
    assert u.source_page_url == "https://example.com"


def test_internal_vs_external_link_split() -> None:
    u = gather_universe(html=_BASIC_HTML, base_url="https://example.com/", api_responses=[])
    internal_urls = {c.url for c in u.internal_links}
    external_urls = {c.url for c in u.external_links}
    assert "https://example.com/floor-plans" in internal_urls
    assert "https://example.com/availability" in internal_urls
    assert "https://example.entrata.com/leasing" in internal_urls
    assert not any("facebook.com" in url for url in external_urls)
    assert not any("googleapis.com" in url for url in external_urls)


def test_skip_patterns_drop_tel_mailto_and_anchor_only_links() -> None:
    u = gather_universe(html=_BASIC_HTML, base_url="https://example.com/", api_responses=[])
    all_urls = {c.url for c in u.internal_links + u.external_links}
    assert "tel:+15551234567" not in all_urls
    assert "#hero" not in all_urls
    assert u.skipped_link_count >= 4


def test_duplicate_links_collapsed() -> None:
    u = gather_universe(html=_BASIC_HTML, base_url="https://example.com/", api_responses=[])
    floor_plan_hits = [c for c in u.internal_links if c.url.endswith("/floor-plans")]
    assert len(floor_plan_hits) == 1


def test_link_score_rewards_anchor_and_path_keywords() -> None:
    u = gather_universe(html=_BASIC_HTML, base_url="https://example.com/", api_responses=[])
    by_url = {c.url: c for c in u.internal_links}
    floor_plans = by_url["https://example.com/floor-plans"]
    availability = by_url["https://example.com/availability"]
    assert floor_plans.base_score > 0
    assert availability.base_score > 0
    assert availability.base_score >= floor_plans.base_score


def test_portal_link_marked_as_portal_and_internal() -> None:
    u = gather_universe(html=_BASIC_HTML, base_url="https://example.com/", api_responses=[])
    portals = [c for c in u.internal_links if c.is_portal]
    assert any("entrata.com" in c.url for c in portals)
    assert all(c.is_internal or c.is_portal for c in portals)


def test_api_candidate_built_with_unit_signals_detected() -> None:
    api_responses = [
        {
            "url": "https://example.com/api/units",
            "body": {
                "data": [
                    {"unit_id": "101", "rent": 1500, "bedrooms": 1, "sqft": 700},
                    {"unit_id": "102", "rent": 1700, "bedrooms": 2, "sqft": 950},
                ]
            },
        }
    ]
    u = gather_universe(html=None, base_url="https://example.com", api_responses=api_responses)
    assert len(u.api_candidates) == 1
    c = u.api_candidates[0]
    assert c.has_unit_signals is True
    assert c.base_score >= 60
    assert "unit_id" in c.preview
    assert c.body_bytes > 0


def test_api_candidate_without_unit_signals_still_captured() -> None:
    # A signal-less but JSON-shaped response is still kept in the universe
    # (the LLM ranker will score it low, but the gather phase preserves
    # any plausible JSON in case the URL or anchor pattern says otherwise).
    api_responses = [
        {
            "url": "https://example.com/api/analytics",
            "body": {"event": "pageview", "user": "abc"},
            "content_type": "application/json",
        }
    ]
    u = gather_universe(html=None, base_url="https://example.com", api_responses=api_responses)
    assert len(u.api_candidates) == 1
    assert u.api_candidates[0].has_unit_signals is False


def test_non_json_api_responses_filtered_out() -> None:
    """HTML pages, JS bundles, CSS, images, tracking pixels — none of these
    carry unit data. They burn ranker tokens AND per-item LLM budget if
    they enter the universe, so they're dropped at gather time."""
    api_responses = [
        # Should be DROPPED — non-JSON content types.
        {"url": "https://example.com/", "body": "<html>...</html>", "content_type": "text/html"},
        {"url": "https://cdn.example.com/script.js", "body": "var x=1;", "content_type": "application/javascript"},
        {"url": "https://cdn.example.com/style.css", "body": "body{}", "content_type": "text/css"},
        {"url": "https://cdn.example.com/logo.svg", "body": "<svg/>", "content_type": "image/svg+xml"},
        {"url": "https://t.example.com/pixel.gif", "body": b"GIF89a...", "content_type": "image/gif"},
        # Should be KEPT — JSON content types and bodies.
        {"url": "https://example.com/api/units", "body": {"data": [{"unit": "1"}]}, "content_type": "application/json"},
        {"url": "https://example.com/api/floor", "body": [{"id": 1}], "content_type": "application/vnd.api+json"},
    ]
    u = gather_universe(html=None, base_url="https://example.com", api_responses=api_responses)
    urls = {c.url for c in u.api_candidates}
    assert urls == {
        "https://example.com/api/units",
        "https://example.com/api/floor",
    }
    # Dropped responses count toward blocked_api_count for telemetry visibility.
    assert u.blocked_api_count == 5


def test_no_content_type_falls_back_to_body_shape() -> None:
    """When content_type is missing/empty (test fixtures often omit it),
    the gather phase should still keep dict/list bodies — they're
    obviously JSON-shaped."""
    api_responses = [
        {"url": "https://example.com/api/a", "body": {"x": 1}},  # no content_type, dict body
        {"url": "https://example.com/api/b", "body": [{"id": 1}]},  # no content_type, list body
        {"url": "https://example.com/api/c", "body": '{"x": 1}'},  # no content_type, JSON string
        {"url": "https://example.com/api/d", "body": "plain text"},  # no content_type, plain text — drop
    ]
    u = gather_universe(html=None, base_url="https://example.com", api_responses=api_responses)
    urls = {c.url for c in u.api_candidates}
    assert "https://example.com/api/a" in urls
    assert "https://example.com/api/b" in urls
    assert "https://example.com/api/c" in urls
    assert "https://example.com/api/d" not in urls


def test_api_preview_truncated_to_preview_bytes() -> None:
    big_body = {"items": [{"unit_id": str(i), "rent": 1000} for i in range(200)]}
    api_responses = [{"url": "https://example.com/api/big", "body": big_body}]
    u = gather_universe(
        html=None,
        base_url="https://example.com",
        api_responses=api_responses,
        preview_bytes=300,
    )
    c = u.api_candidates[0]
    assert c.preview.endswith("...")
    assert len(c.preview) <= 303
    assert c.body_bytes > 1000


def test_blocked_endpoints_filtered_at_gather_time() -> None:
    profile = _FakeProfile(
        api_hints=_ApiHints(blocked_endpoints=[_BlockedEntry(url_pattern="https://example.com/api/noise")])
    )
    api_responses = [
        {"url": "https://example.com/api/noise", "body": {"x": 1}},
        {"url": "https://example.com/api/units", "body": {"data": [{"unit_id": "101", "rent": 1500}]}},
    ]
    u = gather_universe(
        html=None,
        base_url="https://example.com",
        api_responses=api_responses,
        profile=profile,
    )
    urls = {c.url for c in u.api_candidates}
    assert "https://example.com/api/units" in urls
    assert "https://example.com/api/noise" not in urls
    assert u.blocked_api_count == 1


def test_api_dedupe_collapses_repeat_url_captures() -> None:
    api_responses = [
        {"url": "https://example.com/api/units", "body": {"data": [{"unit_id": "1", "rent": 1500}]}},
        {"url": "https://example.com/api/units", "body": {"data": [{"unit_id": "1", "rent": 1500}]}},
    ]
    u = gather_universe(html=None, base_url="https://example.com", api_responses=api_responses)
    assert len(u.api_candidates) == 1


def test_bytes_body_decoded_into_preview() -> None:
    api_responses = [
        {"url": "https://example.com/api/raw", "body": b'{"unit_id":"1","rent":1500,"beds":1,"sqft":700}'}
    ]
    u = gather_universe(html=None, base_url="https://example.com", api_responses=api_responses)
    assert "unit_id" in u.api_candidates[0].preview


def test_corrupt_profile_does_not_break_gather() -> None:
    @dataclass
    class _BadProfile:
        api_hints: Any = "not-an-object"

    api_responses = [
        {"url": "https://example.com/api/units", "body": {"data": [{"unit_id": "1", "rent": 1500}]}},
    ]
    u = gather_universe(
        html=None,
        base_url="https://example.com",
        api_responses=api_responses,
        profile=_BadProfile(),
    )
    assert len(u.api_candidates) == 1
    assert u.blocked_api_count == 0


def test_api_response_with_empty_url_skipped() -> None:
    api_responses = [{"url": "", "body": {"x": 1}}, {"url": None, "body": {"y": 2}}]
    u = gather_universe(html=None, base_url="https://example.com", api_responses=api_responses)
    assert u.api_candidates == []


def test_candidate_summary_lists_apis_first() -> None:
    api_responses = [{"url": "https://example.com/api/units", "body": {"data": [{"unit_id": "1", "rent": 1500}]}}]
    u = gather_universe(html=_BASIC_HTML, base_url="https://example.com/", api_responses=api_responses)
    summary = candidate_summary_for_ranking(u)
    assert summary[0]["kind"] == "api"


def test_apply_llm_scores_writes_back_to_candidates() -> None:
    u = gather_universe(html=_BASIC_HTML, base_url="https://example.com/", api_responses=[])
    target = u.internal_links[0]
    n = apply_llm_scores(u, [{"url": target.url, "confidence": 0.83}])
    assert n == 1
    assert target.llm_score == 0.83


def test_apply_llm_scores_ignores_unmatched_urls_and_bad_floats() -> None:
    u = gather_universe(html=_BASIC_HTML, base_url="https://example.com/", api_responses=[])
    n = apply_llm_scores(
        u,
        [
            {"url": "https://nowhere.example/foo", "confidence": 0.99},
            {"url": u.internal_links[0].url, "confidence": "not-a-number"},
        ],
    )
    assert n == 0


def test_candidates_by_url_indexes_apis_and_links() -> None:
    api_responses = [{"url": "https://example.com/api/units", "body": {"data": [{"unit_id": "1", "rent": 1500}]}}]
    u = gather_universe(html=_BASIC_HTML, base_url="https://example.com/", api_responses=api_responses)
    idx = candidates_by_url(u)
    assert "https://example.com/api/units" in idx
    assert any(url.endswith("/floor-plans") for url in idx)
    assert isinstance(idx["https://example.com/api/units"], ApiCandidate)
    floor_plans_url = next(url for url in idx if url.endswith("/floor-plans"))
    assert isinstance(idx[floor_plans_url], LinkCandidate)
