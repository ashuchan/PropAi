"""Tests for universe_explorer — the gather → rank → explore loop.

LLM and fetch are injected as callables so the tests don't depend on
network or provider config. The ranker is patched at the module level so
the explorer's internal call into ``rank_universe`` / ``rerank_for_availability``
returns deterministic data.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import ma_poc.services.universe_explorer as ue
from ma_poc.services.site_universe import (
    ApiCandidate,
    LinkCandidate,
    SiteUniverse,
)
from ma_poc.services.universe_explorer import (
    ExplorationBudget,
    ExplorationOutcome,
    explore_universe,
)


_PROPERTY_CONTEXT = {
    "property_name": "Test Apartments",
    "city": "Scottsdale",
    "state": "AZ",
    "pmc": "Mark-Taylor",
    "website": "https://example.com",
}


def _make_universe_with_two_apis_and_link() -> SiteUniverse:
    return SiteUniverse(
        source_page_url="https://example.com/",
        api_candidates=[
            ApiCandidate(
                url="https://example.com/api/units",
                body={"data": [{"unit_id": "1", "rent": 1500}]},
                base_score=80,
                has_unit_signals=True,
                preview="...",
                body_bytes=200,
            ),
            ApiCandidate(
                url="https://example.com/api/analytics",
                body={"event": "pv"},
                base_score=0,
                has_unit_signals=False,
                preview="...",
                body_bytes=20,
            ),
        ],
        internal_links=[
            LinkCandidate(
                url="https://example.com/floor-plans",
                anchor="Floor Plans",
                base_score=185,
                is_internal=True,
            )
        ],
    )


def _full_unit(unit_id: str) -> dict[str, Any]:
    return {
        "unit_id": unit_id,
        "beds": 1,
        "baths": 1,
        "sqft": 700,
        "rent_low": 1500,
        "available_date": "2026-05-01",
        "_confidence": 0.9,
    }


def _minimum_only_unit(unit_id: str) -> dict[str, Any]:
    return {
        "unit_id": unit_id,
        "beds": 1,
        "baths": 1,
        "sqft": 700,
        "_confidence": 0.85,
    }


def test_budget_blocks_when_exhausted() -> None:
    b = ExplorationBudget(
        llm_calls_remaining=1,
        links_remaining=1,
        external_links_remaining=1,
        deadline_monotonic=ue._time.monotonic() + 60,
    )
    assert b.can_call_llm() is True
    b.consume_llm()
    assert b.can_call_llm() is False


def test_budget_external_links_separate_counter() -> None:
    b = ExplorationBudget(
        llm_calls_remaining=10,
        links_remaining=5,
        external_links_remaining=1,
        deadline_monotonic=ue._time.monotonic() + 60,
    )
    internal = LinkCandidate(url="https://example.com/x", is_internal=True)
    external = LinkCandidate(url="https://other.com/y", is_internal=False, is_portal=False)

    assert b.can_explore_link(external) is True
    b.consume_link(external)
    assert b.can_explore_link(external) is False
    assert b.can_explore_link(internal) is True


def test_budget_timed_out() -> None:
    b = ExplorationBudget(
        llm_calls_remaining=10,
        links_remaining=10,
        external_links_remaining=10,
        deadline_monotonic=ue._time.monotonic() - 1,
    )
    assert b.timed_out() is True
    assert b.can_call_llm() is False


def test_effective_score_uses_llm_score_with_api_boost() -> None:
    # API_WEIGHT_BOOST applies only when the API has unit signals — that's
    # the "cheap to extract, body looks unit-shaped" case. An API without
    # signals gets no boost (deliberate — see _effective_score docstring).
    api = ApiCandidate(url="https://x", body={}, llm_score=0.5, has_unit_signals=True)
    link = LinkCandidate(url="https://y", llm_score=0.5, is_internal=True)
    assert ue._effective_score(api) > ue._effective_score(link)


def test_effective_score_unsigned_api_does_not_outrank_link() -> None:
    """A signal-less API must NOT outrank a link with the same LLM score —
    the link at least claims (via anchor) to lead to floor plans, the
    blob doesn't."""
    unsigned_api = ApiCandidate(url="https://x", body={}, llm_score=0.5, has_unit_signals=False)
    link = LinkCandidate(url="https://y", llm_score=0.5, is_internal=True)
    assert ue._effective_score(unsigned_api) == ue._effective_score(link)


def test_effective_score_falls_back_to_base_score() -> None:
    cand = LinkCandidate(url="https://x", base_score=100, is_internal=True)
    s = ue._effective_score(cand)
    assert 0.0 < s <= 0.6


def test_build_queue_orders_highest_first() -> None:
    u = _make_universe_with_two_apis_and_link()
    u.api_candidates[0].llm_score = 0.95
    u.api_candidates[1].llm_score = 0.05
    u.internal_links[0].llm_score = 0.7
    queue = ue._build_queue(u)
    assert queue[0].url.endswith("/api/units")
    assert queue[-1].url.endswith("/api/analytics")


def test_chunk_api_body_returns_single_chunk_when_small() -> None:
    cand = ApiCandidate(
        url="https://x",
        body={"data": [{"unit_id": "1"}]},
        body_bytes=100,
    )
    chunks = ue._chunk_api_body(cand)
    assert len(chunks) == 1


def test_chunk_api_body_splits_on_top_level_list() -> None:
    items = [{"unit_id": str(i), "rent": 1500} for i in range(50)]
    body = {"data": items}
    cand = ApiCandidate(
        url="https://x",
        body=body,
        body_bytes=ue.LLM_API_CHUNK_BYTES * 4,
        preview="x",
    )
    chunks = ue._chunk_api_body(cand)
    assert len(chunks) > 1
    assert all(isinstance(c, dict) and "data" in c for c in chunks)
    total = sum(len(c["data"]) for c in chunks)
    assert total == 50


def test_chunk_api_body_byte_slices_when_no_list_found() -> None:
    cand = ApiCandidate(
        url="https://x",
        body={"opaque": "blob"},
        body_bytes=ue.LLM_API_CHUNK_BYTES * 3,
        preview="x" * (ue.LLM_API_CHUNK_BYTES * 3),
    )
    chunks = ue._chunk_api_body(cand)
    assert len(chunks) >= 2


async def _api_extractor_returning(units: list[dict[str, Any]]) -> Any:
    """Build an api_extractor that always returns the given units."""

    async def _extract(api_response, ctx, pid):  # type: ignore[no-untyped-def]
        return units, {"api_url_pattern": api_response["url"], "json_paths": {}}, False, {"cost_usd": 0.01}

    return _extract


async def _dom_extractor_returning(units: list[dict[str, Any]]) -> Any:
    async def _extract(html, url, ctx, pid):  # type: ignore[no-untyped-def]
        return units, {"container": ".unit"}, {"cost_usd": 0.005}

    return _extract


@pytest.mark.asyncio
async def test_explore_stops_when_quality_met_after_one_api() -> None:
    u = _make_universe_with_two_apis_and_link()
    api_extractor = await _api_extractor_returning([_full_unit("101"), _full_unit("102")])
    dom_extractor = await _dom_extractor_returning([])

    rank_response = (
        [
            {"url": "https://example.com/api/units", "kind": "api", "confidence": 0.95},
            {"url": "https://example.com/floor-plans", "kind": "internal_link", "confidence": 0.7},
            {"url": "https://example.com/api/analytics", "kind": "api", "confidence": 0.05},
        ],
        {"cost_usd": 0.001},
    )

    with patch("ma_poc.services.universe_explorer.rank_universe", AsyncMock(return_value=rank_response)):
        outcome = await explore_universe(
            universe=u,
            property_context=_PROPERTY_CONTEXT,
            property_id="P001",
            api_extractor=api_extractor,
            dom_extractor=dom_extractor,
        )

    assert outcome.stop_reason == "quality_met"
    assert outcome.units != []
    assert outcome.quality is not None and outcome.quality.minimum_met
    assert outcome.quality.important_met
    assert outcome.winning_url == "https://example.com/api/units"
    assert outcome.explored_apis == 1
    assert outcome.explored_links == 0


@pytest.mark.asyncio
async def test_explore_skips_low_confidence_when_units_already_present() -> None:
    u = _make_universe_with_two_apis_and_link()
    api_extractor = await _api_extractor_returning([_full_unit("101")])
    dom_extractor = await _dom_extractor_returning([])

    rank_response = (
        [
            {"url": "https://example.com/api/units", "kind": "api", "confidence": 0.95},
            {"url": "https://example.com/api/analytics", "kind": "api", "confidence": 0.10},
        ],
        None,
    )

    with patch("ma_poc.services.universe_explorer.rank_universe", AsyncMock(return_value=rank_response)):
        outcome = await explore_universe(
            universe=u,
            property_context=_PROPERTY_CONTEXT,
            property_id="P001",
            api_extractor=api_extractor,
            dom_extractor=dom_extractor,
            threshold=0.7,
        )

    assert outcome.stop_reason in ("quality_met", "score_below_cutoff_with_units (score=0.25, cutoff=0.70)")


@pytest.mark.asyncio
async def test_explore_fires_rerank_when_minimum_met_but_important_missing() -> None:
    u = _make_universe_with_two_apis_and_link()

    call_state = {"n": 0}

    async def stateful_api_extractor(api_response, ctx, pid):  # type: ignore[no-untyped-def]
        call_state["n"] += 1
        if call_state["n"] == 1:
            return [_minimum_only_unit("101")], {"api_url_pattern": api_response["url"], "json_paths": {}}, False, {"cost_usd": 0.01}
        return [_full_unit("101"), _full_unit("102")], {"api_url_pattern": api_response["url"], "json_paths": {}}, False, {"cost_usd": 0.01}

    dom_extractor = await _dom_extractor_returning([])

    initial_rank = (
        [
            {"url": "https://example.com/api/units", "kind": "api", "confidence": 0.95},
            {"url": "https://example.com/api/analytics", "kind": "api", "confidence": 0.40},
        ],
        None,
    )
    rerank = (
        [
            {"url": "https://example.com/api/analytics", "kind": "api", "confidence": 0.85},
        ],
        {"cost_usd": 0.002},
    )

    with patch("ma_poc.services.universe_explorer.rank_universe", AsyncMock(return_value=initial_rank)):
        with patch(
            "ma_poc.services.universe_explorer.rerank_for_availability",
            AsyncMock(return_value=rerank),
        ) as rerank_mock:
            outcome = await explore_universe(
                universe=u,
                property_context=_PROPERTY_CONTEXT,
                property_id="P001",
                api_extractor=stateful_api_extractor,
                dom_extractor=dom_extractor,
            )

    rerank_mock.assert_called_once()
    assert outcome.rerank_fired is True
    assert outcome.quality is not None and outcome.quality.important_met


@pytest.mark.asyncio
async def test_explore_does_not_call_link_fetcher_when_none_provided() -> None:
    u = _make_universe_with_two_apis_and_link()
    api_extractor = await _api_extractor_returning([])
    dom_extractor = await _dom_extractor_returning([])

    rank_response = (
        [
            {"url": "https://example.com/floor-plans", "kind": "internal_link", "confidence": 0.95},
            {"url": "https://example.com/api/units", "kind": "api", "confidence": 0.50},
            {"url": "https://example.com/api/analytics", "kind": "api", "confidence": 0.05},
        ],
        None,
    )

    with patch("ma_poc.services.universe_explorer.rank_universe", AsyncMock(return_value=rank_response)):
        outcome = await explore_universe(
            universe=u,
            property_context=_PROPERTY_CONTEXT,
            property_id="P001",
            api_extractor=api_extractor,
            dom_extractor=dom_extractor,
            link_fetcher=None,
        )

    assert outcome.explored_links == 0
    assert outcome.explored_apis == 2


@pytest.mark.asyncio
async def test_explore_link_fetch_injects_subpage_apis_into_queue() -> None:
    u = SiteUniverse(
        source_page_url="https://example.com/",
        internal_links=[
            LinkCandidate(
                url="https://example.com/floor-plans",
                anchor="Floor Plans",
                base_score=185,
                is_internal=True,
            )
        ],
    )

    sub_page_html = "<html><body>Sub-page</body></html>"
    sub_page_apis = [
        {
            "url": "https://example.com/api/subpage-units",
            "body": {"data": [{"unit_id": "S1", "rent": 1700, "beds": 2, "sqft": 950}]},
        }
    ]

    fetch_calls: list[str] = []

    async def fake_link_fetcher(url, parent):  # type: ignore[no-untyped-def]
        fetch_calls.append(url)
        return sub_page_html, sub_page_apis

    api_extractor = await _api_extractor_returning([_full_unit("S1")])
    dom_extractor = await _dom_extractor_returning([])

    rank_response = (
        [{"url": "https://example.com/floor-plans", "kind": "internal_link", "confidence": 0.95}],
        None,
    )

    with patch("ma_poc.services.universe_explorer.rank_universe", AsyncMock(return_value=rank_response)):
        outcome = await explore_universe(
            universe=u,
            property_context=_PROPERTY_CONTEXT,
            property_id="P001",
            api_extractor=api_extractor,
            dom_extractor=dom_extractor,
            link_fetcher=fake_link_fetcher,
        )

    assert fetch_calls == ["https://example.com/floor-plans"]
    assert outcome.explored_apis >= 1
    assert outcome.units != []


@pytest.mark.asyncio
async def test_explore_external_link_skipped_when_below_floor() -> None:
    u = SiteUniverse(
        source_page_url="https://example.com/",
        external_links=[
            LinkCandidate(
                url="https://other.com/leasing",
                anchor="Lease",
                base_score=20,
                is_internal=False,
                is_portal=False,
            )
        ],
    )
    api_extractor = await _api_extractor_returning([])
    dom_extractor = await _dom_extractor_returning([])
    rank_response = (
        [{"url": "https://other.com/leasing", "kind": "external_link", "confidence": 0.50}],
        None,
    )

    with patch("ma_poc.services.universe_explorer.rank_universe", AsyncMock(return_value=rank_response)):
        outcome = await explore_universe(
            universe=u,
            property_context=_PROPERTY_CONTEXT,
            property_id="P001",
            api_extractor=api_extractor,
            dom_extractor=dom_extractor,
            link_fetcher=AsyncMock(),
            threshold=0.7,
        )
    assert outcome.explored_links == 0


@pytest.mark.asyncio
async def test_explore_returns_no_units_when_initial_rank_fails() -> None:
    u = _make_universe_with_two_apis_and_link()
    api_extractor = await _api_extractor_returning([])
    dom_extractor = await _dom_extractor_returning([])

    with patch(
        "ma_poc.services.universe_explorer.rank_universe",
        AsyncMock(return_value=([], None)),
    ):
        outcome = await explore_universe(
            universe=u,
            property_context=_PROPERTY_CONTEXT,
            property_id="P001",
            api_extractor=api_extractor,
            dom_extractor=dom_extractor,
        )

    assert outcome.units == []
    assert outcome.llm_calls_made >= 1


@pytest.mark.asyncio
async def test_explore_records_llm_interactions_for_cost_ledger() -> None:
    u = _make_universe_with_two_apis_and_link()
    api_extractor = await _api_extractor_returning([_full_unit("101")])
    dom_extractor = await _dom_extractor_returning([])

    rank_response = (
        [{"url": "https://example.com/api/units", "kind": "api", "confidence": 0.95}],
        {"cost_usd": 0.002, "tier": "UNIVERSE_RANKING"},
    )

    with patch("ma_poc.services.universe_explorer.rank_universe", AsyncMock(return_value=rank_response)):
        outcome = await explore_universe(
            universe=u,
            property_context=_PROPERTY_CONTEXT,
            property_id="P001",
            api_extractor=api_extractor,
            dom_extractor=dom_extractor,
        )

    assert len(outcome.llm_interactions) >= 2


@pytest.mark.asyncio
async def test_explore_falls_back_to_entry_html_dom_when_loop_finds_nothing() -> None:
    u = _make_universe_with_two_apis_and_link()
    api_extractor = await _api_extractor_returning([])
    dom_extractor = await _dom_extractor_returning([_full_unit("DOM-1")])

    rank_response = (
        [
            {"url": "https://example.com/api/units", "kind": "api", "confidence": 0.95},
            {"url": "https://example.com/api/analytics", "kind": "api", "confidence": 0.10},
        ],
        None,
    )

    with patch("ma_poc.services.universe_explorer.rank_universe", AsyncMock(return_value=rank_response)):
        outcome = await explore_universe(
            universe=u,
            property_context=_PROPERTY_CONTEXT,
            property_id="P001",
            api_extractor=api_extractor,
            dom_extractor=dom_extractor,
            entry_html="<html><body>price $1500</body></html>",
            link_fetcher=None,
        )

    assert outcome.units != []
    assert any(t["tier_key"] == "universe:entry_dom_fallback" for t in outcome.tier_attempts)


@pytest.mark.asyncio
async def test_explore_swallows_api_extractor_exceptions() -> None:
    u = _make_universe_with_two_apis_and_link()

    async def boom(api_response, ctx, pid):  # type: ignore[no-untyped-def]
        raise RuntimeError("provider 500")

    dom_extractor = await _dom_extractor_returning([])
    rank_response = (
        [{"url": "https://example.com/api/units", "kind": "api", "confidence": 0.95}],
        None,
    )

    with patch("ma_poc.services.universe_explorer.rank_universe", AsyncMock(return_value=rank_response)):
        outcome = await explore_universe(
            universe=u,
            property_context=_PROPERTY_CONTEXT,
            property_id="P001",
            api_extractor=boom,
            dom_extractor=dom_extractor,
        )

    assert outcome.units == []
    assert outcome.error is not None and "provider 500" in outcome.error


@pytest.mark.asyncio
async def test_explore_returns_outcome_dataclass_with_quality() -> None:
    u = _make_universe_with_two_apis_and_link()
    api_extractor = await _api_extractor_returning([_full_unit("1")])
    dom_extractor = await _dom_extractor_returning([])
    rank_response = (
        [{"url": "https://example.com/api/units", "kind": "api", "confidence": 0.95}],
        None,
    )

    with patch("ma_poc.services.universe_explorer.rank_universe", AsyncMock(return_value=rank_response)):
        outcome = await explore_universe(
            universe=u,
            property_context=_PROPERTY_CONTEXT,
            property_id="P001",
            api_extractor=api_extractor,
            dom_extractor=dom_extractor,
        )

    assert isinstance(outcome, ExplorationOutcome)
    assert outcome.quality is not None
    assert outcome.quality.unit_count == 1
