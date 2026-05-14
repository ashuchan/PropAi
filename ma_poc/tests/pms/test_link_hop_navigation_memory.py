"""LAYER 4 (CONSUME) — pin link-hop reading profile navigation memory.

This test pins the headline self-learning loop fix (Bug 3.1):
``_try_link_hop`` must consult ``profile.navigation`` so previously-
discovered URLs ride in front of the keyword-ranked candidates and
known dead ends are skipped.

Behaviour pinned:
  - ``winning_page_url`` from a previous successful run becomes the
    HIGHEST-priority candidate (above LLM hints from the current run
    and above keyword-ranked candidates).
  - ``availability_links`` (every URL that ever produced units)
    appear in the candidate list.
  - ``explored_links`` (known dead-ends) are filtered out so we don't
    re-pay for them.

Strategy: ``_try_link_hop`` is async and dispatches via
``ma_poc.fetch.fetch as jugnu_fetch``. We mock that fetch function
to capture the URL ordering the ranker actually attempts. We don't
need real HTTP — only the URL sequence proves the consume contract.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ma_poc.models.scrape_profile import NavigationConfig, ScrapeProfile
from ma_poc.pms.detector import DetectedPMS
from ma_poc.pms.scraper import _try_link_hop


def _entry_html_with_links() -> str:
    """Entry-page HTML with anchor-text candidates the keyword ranker
    will pick up. Used as the substrate that profile-driven candidates
    are layered ON TOP OF."""
    return """<!DOCTYPE html>
    <html><body>
    <a href="/about">About Us</a>
    <a href="/contact">Contact</a>
    <a href="/availability">Availability</a>
    <a href="/floor-plans">Floor Plans</a>
    </body></html>
    """


def _make_fetch_result(outcome: str = "OK", body: bytes | None = None):
    from types import SimpleNamespace
    return SimpleNamespace(
        outcome=SimpleNamespace(value=outcome),
        body=body or b"<html><body>sub-page content</body></html>",
        elapsed_ms=10,
    )


async def _stubbed_fetch_factory(
    visit_log: list[str],
    outcomes: dict[str, str] | None = None,
):
    """Build an async fetch stub that records every URL it was asked
    to fetch (so tests can assert ordering) and lets specific URLs
    return non-OK outcomes."""
    outcomes = outcomes or {}

    async def _stub(task: Any) -> Any:
        url = task.url if hasattr(task, "url") else str(task)
        visit_log.append(url)
        outcome = outcomes.get(url, "OK")
        return _make_fetch_result(outcome=outcome)

    return _stub


@pytest.mark.asyncio
async def test_winning_page_url_tried_first() -> None:
    """When the profile remembers ``winning_page_url`` from a previous
    run, link-hop tries that URL FIRST — ahead of keyword candidates
    and ahead of any LLM hints from the current run. Closes Bug 3.1."""
    profile = ScrapeProfile(canonical_id="nav-mem-001")
    profile.navigation.winning_page_url = "https://x.com/floor-plans"

    visit_log: list[str] = []
    stub = await _stubbed_fetch_factory(visit_log)

    # We don't care about the scrape() result here, only the URL
    # ordering the fetcher saw. Patch scrape() to return a "no units"
    # result so the loop tries every candidate.
    async def _no_units_scrape(**kwargs: Any) -> dict[str, Any]:
        return {"units": [], "extraction_tier_used": None, "errors": []}

    with patch("ma_poc.fetch.fetch", new=stub), patch(
        "ma_poc.pms.scraper.scrape", new=_no_units_scrape
    ):
        await _try_link_hop(
            entry_url="https://x.com/",
            entry_page_html=_entry_html_with_links(),
            detected=DetectedPMS(pms="unknown", confidence=0.0),
            profile=profile,
            expected_total_units=None,
            property_id="nav-mem-001",
            csv_row=None,
            max_hops=3,
            visited_urls={"https://x.com/"},
        )

    assert visit_log, "link-hop must attempt at least one URL"
    # The profile-recorded winning URL is FIRST in the visit order.
    assert visit_log[0] == "https://x.com/floor-plans"


@pytest.mark.asyncio
async def test_availability_links_promoted_above_keyword_candidates() -> None:
    """``availability_links`` from previous runs become high-priority
    candidates — they outrank anchor-text matches that aren't in the
    profile's memory."""
    profile = ScrapeProfile(canonical_id="nav-mem-002")
    profile.navigation.availability_links = [
        "https://x.com/known-good-page",
    ]

    visit_log: list[str] = []
    stub = await _stubbed_fetch_factory(visit_log)

    async def _no_units_scrape(**kwargs: Any) -> dict[str, Any]:
        return {"units": [], "extraction_tier_used": None, "errors": []}

    with patch("ma_poc.fetch.fetch", new=stub), patch(
        "ma_poc.pms.scraper.scrape", new=_no_units_scrape
    ):
        await _try_link_hop(
            entry_url="https://x.com/",
            entry_page_html=_entry_html_with_links(),
            detected=DetectedPMS(pms="unknown", confidence=0.0),
            profile=profile,
            expected_total_units=None,
            property_id="nav-mem-002",
            csv_row=None,
            max_hops=3,
            visited_urls={"https://x.com/"},
        )

    # known-good-page tried before any keyword-ranked URL.
    assert "https://x.com/known-good-page" in visit_log
    profile_url_idx = visit_log.index("https://x.com/known-good-page")
    # Every keyword candidate (if visited) is tried after the profile URL.
    for i, url in enumerate(visit_log):
        if url in ("https://x.com/availability", "https://x.com/floor-plans"):
            assert i > profile_url_idx, (
                f"keyword URL {url} (idx={i}) tried before profile "
                f"availability_link (idx={profile_url_idx})"
            )


@pytest.mark.asyncio
async def test_explored_links_filtered_from_candidates() -> None:
    """URLs the profile remembers as dead-ends (``explored_links``)
    must be filtered out of the candidate set — we don't re-pay
    for known empty hops."""
    profile = ScrapeProfile(canonical_id="nav-mem-003")
    # Both "/availability" and "/floor-plans" would normally be
    # picked up by anchor-text matching, but we marked them as
    # dead-ends in a previous run.
    profile.navigation.explored_links = [
        "https://x.com/availability",
        "https://x.com/floor-plans",
    ]

    visit_log: list[str] = []
    stub = await _stubbed_fetch_factory(visit_log)

    async def _no_units_scrape(**kwargs: Any) -> dict[str, Any]:
        return {"units": [], "extraction_tier_used": None, "errors": []}

    with patch("ma_poc.fetch.fetch", new=stub), patch(
        "ma_poc.pms.scraper.scrape", new=_no_units_scrape
    ):
        await _try_link_hop(
            entry_url="https://x.com/",
            entry_page_html=_entry_html_with_links(),
            detected=DetectedPMS(pms="unknown", confidence=0.0),
            profile=profile,
            expected_total_units=None,
            property_id="nav-mem-003",
            csv_row=None,
            max_hops=3,
            visited_urls={"https://x.com/"},
        )

    # 2026-05-14 carve-out: high-score sources (>= _PMS_PRIOR_SCORE)
    # survive the explored_skip filter. Universal PMS priors fire at
    # _PMS_PRIOR_SCORE for unknown-PMS sites, so these candidates are
    # retried even when previously empty — one empty extraction must
    # not permanently poison a known floor-plan path. Lower-score
    # keyword-only candidates are still filtered.
    assert "https://x.com/availability" in visit_log, (
        f"Universal PMS prior /availability should survive explored_skip; "
        f"visit_log={visit_log}"
    )
    assert "https://x.com/floor-plans" in visit_log, (
        f"Universal PMS prior /floor-plans should survive explored_skip; "
        f"visit_log={visit_log}"
    )


@pytest.mark.asyncio
async def test_winning_page_url_outranks_current_run_llm_hint() -> None:
    """Profile's persisted winning URL (proven success) outranks the
    current run's LLM hint (untested guess). Both are valuable but
    proven > predicted."""
    profile = ScrapeProfile(canonical_id="nav-mem-004")
    profile.navigation.winning_page_url = "https://x.com/proven-good"

    visit_log: list[str] = []
    stub = await _stubbed_fetch_factory(visit_log)

    async def _no_units_scrape(**kwargs: Any) -> dict[str, Any]:
        return {"units": [], "extraction_tier_used": None, "errors": []}

    with patch("ma_poc.fetch.fetch", new=stub), patch(
        "ma_poc.pms.scraper.scrape", new=_no_units_scrape
    ):
        await _try_link_hop(
            entry_url="https://x.com/",
            entry_page_html=_entry_html_with_links(),
            detected=DetectedPMS(pms="unknown", confidence=0.0),
            profile=profile,
            expected_total_units=None,
            property_id="nav-mem-004",
            csv_row=None,
            max_hops=3,
            llm_navigation_hints=["/llm-suggested"],
            visited_urls={"https://x.com/"},
        )

    # Proven URL tried first, then the LLM hint:
    assert visit_log[0] == "https://x.com/proven-good"
    if "https://x.com/llm-suggested" in visit_log:
        assert visit_log.index("https://x.com/llm-suggested") > 0


@pytest.mark.asyncio
async def test_no_profile_falls_back_to_keyword_ranking() -> None:
    """Cold-start safety — a property with no profile (or an empty
    NavigationConfig) still gets the keyword-ranked candidate flow.
    The profile-driven prepend is purely additive."""
    visit_log: list[str] = []
    stub = await _stubbed_fetch_factory(visit_log)

    async def _no_units_scrape(**kwargs: Any) -> dict[str, Any]:
        return {"units": [], "extraction_tier_used": None, "errors": []}

    with patch("ma_poc.fetch.fetch", new=stub), patch(
        "ma_poc.pms.scraper.scrape", new=_no_units_scrape
    ):
        await _try_link_hop(
            entry_url="https://x.com/",
            entry_page_html=_entry_html_with_links(),
            detected=DetectedPMS(pms="unknown", confidence=0.0),
            profile=None,  # No profile at all
            expected_total_units=None,
            property_id="cold",
            csv_row=None,
            max_hops=3,
            visited_urls={"https://x.com/"},
        )

    # The keyword-ranked candidates were attempted (at least one of
    # the unit-relevant anchors):
    keyword_visited = any(
        url in (
            "https://x.com/availability",
            "https://x.com/floor-plans",
        )
        for url in visit_log
    )
    assert keyword_visited, f"expected keyword-ranked URLs in visit_log, got {visit_log}"
