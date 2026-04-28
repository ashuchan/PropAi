"""Tests for llm_ranker — universe ranking + availability re-rank.

The LLM provider is fully mocked. These tests verify the ranker's prompt
shaping, response parsing, and defensive handling of bad outputs — not
the LLM itself.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from ma_poc.services.llm_ranker import (
    _extract_ranked_array,
    _filter_universe_to,
    _serialize_for_prompt,
    rank_universe,
    rerank_for_availability,
)
from ma_poc.services.site_universe import (
    ApiCandidate,
    LinkCandidate,
    SiteUniverse,
)


def _make_universe() -> SiteUniverse:
    return SiteUniverse(
        source_page_url="https://example.com/",
        api_candidates=[
            ApiCandidate(
                url="https://example.com/api/units",
                body={"data": [{"unit_id": "1", "rent": 1500, "beds": 1}]},
                base_score=80,
                has_unit_signals=True,
                preview='[{"unit_id":"1","rent":1500}]',
                body_bytes=200,
            ),
            ApiCandidate(
                url="https://example.com/api/analytics",
                body={"event": "pv"},
                base_score=0,
                has_unit_signals=False,
                preview='{"event":"pv"}',
                body_bytes=20,
            ),
        ],
        internal_links=[
            LinkCandidate(
                url="https://example.com/floor-plans",
                anchor="View Floor Plans",
                base_score=185,
                is_internal=True,
            ),
            LinkCandidate(
                url="https://example.com/contact",
                anchor="Contact",
                base_score=0,
                is_internal=True,
            ),
        ],
        external_links=[
            LinkCandidate(
                url="https://leasing.realpage.com/p/123",
                anchor="Apply",
                base_score=120,
                is_internal=False,
                is_portal=True,
            )
        ],
    )


_PROPERTY_CONTEXT = {
    "property_name": "Test Apartments",
    "city": "Scottsdale",
    "state": "AZ",
    "pmc": "Mark-Taylor",
    "website": "https://example.com",
}


def _fake_provider(response_text: str) -> AsyncMock:
    p = AsyncMock()
    p.complete = AsyncMock(return_value=response_text)
    p._last_usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "model": "test-model",
        "provider": "test",
    }
    return p


def test_extract_ranked_handles_wrapped_response() -> None:
    parsed = {
        "ranked": [
            {"url": "https://a.com", "kind": "api", "confidence": 0.9, "reason": "yes"},
            {"url": "https://b.com", "kind": "internal_link", "confidence": 0.4, "reason": "maybe"},
        ]
    }
    out = _extract_ranked_array(parsed)
    assert len(out) == 2
    assert out[0]["confidence"] == 0.9


def test_extract_ranked_accepts_top_level_array() -> None:
    out = _extract_ranked_array(
        [{"url": "https://a.com", "kind": "api", "confidence": 0.7, "reason": ""}]
    )
    assert len(out) == 1


def test_extract_ranked_clamps_out_of_range_confidence() -> None:
    parsed = {
        "ranked": [
            {"url": "https://a.com", "confidence": 1.7},
            {"url": "https://b.com", "confidence": -0.3},
        ]
    }
    out = _extract_ranked_array(parsed)
    confs = [o["confidence"] for o in out]
    assert confs == [1.0, 0.0]


def test_extract_ranked_drops_entries_missing_url_or_confidence() -> None:
    parsed = {
        "ranked": [
            {"url": "", "confidence": 0.9},
            {"url": "https://a.com"},
            {"url": "https://b.com", "confidence": "not-a-number"},
            {"url": "https://c.com", "confidence": 0.5},
        ]
    }
    out = _extract_ranked_array(parsed)
    urls = [o["url"] for o in out]
    assert urls == ["https://c.com"]


def test_extract_ranked_truncates_long_reason_field() -> None:
    parsed = {"ranked": [{"url": "https://a.com", "confidence": 0.5, "reason": "x" * 1000}]}
    out = _extract_ranked_array(parsed)
    assert len(out[0]["reason"]) <= 240


def test_extract_ranked_empty_on_garbage() -> None:
    assert _extract_ranked_array({}) == []
    assert _extract_ranked_array({"ranked": "not a list"}) == []
    assert _extract_ranked_array({"ranked": [42, "string", None]}) == []


def test_serialize_lists_apis_before_links() -> None:
    u = _make_universe()
    serialised = _serialize_for_prompt(u, max_candidates=10)
    kinds = [s["kind"] for s in serialised]
    api_idx = kinds.index("api")
    link_idx = kinds.index("internal_link")
    assert api_idx < link_idx


def test_serialize_caps_at_max_candidates() -> None:
    u = _make_universe()
    serialised = _serialize_for_prompt(u, max_candidates=2)
    assert len(serialised) == 2


def test_serialize_internal_links_sorted_by_base_score() -> None:
    u = _make_universe()
    serialised = _serialize_for_prompt(u, max_candidates=20)
    internal = [s for s in serialised if s["kind"] == "internal_link"]
    assert internal[0]["url"].endswith("/floor-plans")


def test_filter_universe_keeps_only_listed_candidates() -> None:
    u = _make_universe()
    keep = [u.api_candidates[0], u.internal_links[0]]
    filtered = _filter_universe_to(u, keep)
    assert len(filtered.api_candidates) == 1
    assert len(filtered.internal_links) == 1
    assert len(filtered.external_links) == 0


@pytest.mark.asyncio
async def test_rank_universe_returns_parsed_ranking() -> None:
    u = _make_universe()
    llm_response = json.dumps(
        {
            "ranked": [
                {"url": "https://example.com/api/units", "kind": "api", "confidence": 0.95, "reason": "has units"},
                {"url": "https://example.com/floor-plans", "kind": "internal_link", "confidence": 0.7, "reason": "good path"},
                {"url": "https://example.com/api/analytics", "kind": "api", "confidence": 0.05, "reason": "noise"},
                {"url": "https://example.com/contact", "kind": "internal_link", "confidence": 0.0, "reason": "no"},
                {"url": "https://leasing.realpage.com/p/123", "kind": "external_link", "confidence": 0.85, "reason": "portal"},
            ]
        }
    )
    provider = _fake_provider(llm_response)
    with patch("llm.factory.get_text_provider", return_value=provider):
        ranked, interaction = await rank_universe(u, _PROPERTY_CONTEXT, "P001")

    assert len(ranked) == 5
    assert ranked[0]["url"] == "https://example.com/api/units"
    assert ranked[0]["confidence"] == 0.95
    assert interaction is not None
    # rank_universe is now chunked — first chunk's tier carries the
    # CHUNK_N_OF_M suffix; for a single-chunk run this is "1_OF_1".
    assert interaction["tier"].startswith("UNIVERSE_RANKING_CHUNK_")
    assert interaction["tokens_input"] == 100


@pytest.mark.asyncio
async def test_rank_universe_returns_empty_on_provider_error() -> None:
    u = _make_universe()
    provider = AsyncMock()
    provider.complete = AsyncMock(side_effect=RuntimeError("rate limit"))
    provider._last_usage = {}
    with patch("llm.factory.get_text_provider", return_value=provider):
        ranked, interaction = await rank_universe(u, _PROPERTY_CONTEXT, "P001")
    assert ranked == []
    assert interaction is not None
    assert interaction["success"] is False


@pytest.mark.asyncio
async def test_rank_universe_returns_empty_on_garbage_llm_output() -> None:
    u = _make_universe()
    provider = _fake_provider("definitely not json")
    with patch("llm.factory.get_text_provider", return_value=provider):
        ranked, _interaction = await rank_universe(u, _PROPERTY_CONTEXT, "P001")
    assert ranked == []


@pytest.mark.asyncio
async def test_rank_universe_prompt_includes_property_context() -> None:
    u = _make_universe()
    provider = _fake_provider('{"ranked": []}')
    with patch("llm.factory.get_text_provider", return_value=provider):
        await rank_universe(u, _PROPERTY_CONTEXT, "P001")

    user_prompt = provider.complete.call_args.args[1]
    assert "Test Apartments" in user_prompt
    assert "Scottsdale" in user_prompt
    assert "Mark-Taylor" in user_prompt
    assert "https://example.com/api/units" in user_prompt


@pytest.mark.asyncio
async def test_rerank_only_sends_unexplored_candidates() -> None:
    u = _make_universe()
    u.api_candidates[0].explored = True
    u.internal_links[0].explored = True

    provider = _fake_provider('{"ranked": []}')
    with patch("llm.factory.get_text_provider", return_value=provider):
        ranked, _interaction = await rerank_for_availability(
            universe=u,
            units_so_far=[{"beds": 1, "baths": 1, "sqft": 700}],
            property_context=_PROPERTY_CONTEXT,
            source_url="https://example.com/api/units",
            property_id="P001",
        )

    assert ranked == []
    user_prompt = provider.complete.call_args.args[1]
    # Check the candidates-JSON block specifically (not the source_url placeholder).
    assert '"url": "https://example.com/api/units"' not in user_prompt
    assert '"url": "https://example.com/floor-plans"' not in user_prompt
    assert (
        '"url": "https://example.com/api/analytics"' in user_prompt
        or '"url": "https://example.com/contact"' in user_prompt
    )


@pytest.mark.asyncio
async def test_rerank_returns_empty_when_nothing_left_to_explore() -> None:
    u = _make_universe()
    for c in u.all_candidates():
        c.explored = True

    provider = _fake_provider('{"ranked": []}')
    with patch("llm.factory.get_text_provider", return_value=provider):
        ranked, interaction = await rerank_for_availability(
            universe=u,
            units_so_far=[],
            property_context=_PROPERTY_CONTEXT,
            source_url="https://example.com",
            property_id="P001",
        )
    assert ranked == []
    assert interaction is None
    provider.complete.assert_not_called()


@pytest.mark.asyncio
async def test_rerank_prompt_carries_extraction_status() -> None:
    u = _make_universe()
    provider = _fake_provider('{"ranked": []}')
    units = [
        {"beds": 1, "baths": 1, "sqft": 700, "rent_low": 1500},
        {"beds": 2, "baths": 2, "sqft": 950},
        {"beds": 1, "baths": 1, "sqft": 700, "available_date": "2026-05-01"},
    ]
    with patch("llm.factory.get_text_provider", return_value=provider):
        await rerank_for_availability(
            universe=u,
            units_so_far=units,
            property_context=_PROPERTY_CONTEXT,
            source_url="https://example.com/api/units",
            property_id="P001",
        )
    prompt = provider.complete.call_args.args[1]
    assert "3" in prompt
    assert (
        "Source URL" in prompt
        or "source_url" in prompt
        or "https://example.com/api/units" in prompt
    )
