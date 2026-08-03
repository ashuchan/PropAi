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
from unittest.mock import patch

import pytest

from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.pms.detector import DetectedPMS
from ma_poc.pms.scraper import _try_link_hop


class _FakeProbeResponse:
    """curl_cffi-response stand-in for the ``_probe`` seam.

    ``_try_link_hop`` runs every candidate URL through the cheap-GET gate
    (``scraper._crawl_get_gate_should_skip`` → ``probe_get``) before the
    RENDER sub-fetch. A 200 with a body carrying no unit/PMS signal is the
    neutral answer: the gate only retires ``404``/``410`` + <10 KB bodies,
    so every candidate still reaches the mocked fetcher and the visit
    ordering these tests assert on is unchanged.
    """

    __slots__ = ("status_code", "text", "content", "headers", "url")

    def __init__(self, url: str) -> None:
        self.url = url
        self.status_code = 200
        self.text = "<html><body></body></html>"
        self.content = self.text.encode("utf-8")
        self.headers: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _stub_probe_get_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the link-hop cheap-GET gate off the live network.

    Overrides the repo-level guard in ``ma_poc/conftest.py`` for this module
    only. These are ranking/ordering tests — they care about WHICH URLs get
    tried, not about page content — so one neutral response for every URL is
    enough.
    """

    def _fake_probe_get(url: str, *args: Any, **kwargs: Any) -> _FakeProbeResponse:
        return _FakeProbeResponse(url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _fake_probe_get)


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


async def _capture_started_candidates(
    *,
    entry_url: str,
    entry_page_html: str,
    detected: DetectedPMS,
    profile: ScrapeProfile | None,
    llm_navigation_hints: list[str] | None = None,
    embedded_portal_hints: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Return the final ranked candidates emitted by ``_try_link_hop``."""
    from ma_poc.observability import events as obs_events

    candidate_records: list[list[dict[str, Any]]] = []
    visit_log: list[str] = []
    stub = await _stubbed_fetch_factory(visit_log)

    async def _no_units_scrape(**kwargs: Any) -> dict[str, Any]:
        return {"units": [], "extraction_tier_used": None, "errors": []}

    def _spy_emit(kind: Any, property_id: str, **payload: Any) -> None:
        kind_value = getattr(kind, "value", str(kind))
        if "link_hop_started" in kind_value.lower():
            candidate_records.append(payload.get("candidates", []))

    with patch("ma_poc.fetch.fetch", new=stub), patch(
        "ma_poc.pms.scraper.scrape", new=_no_units_scrape
    ), patch.object(obs_events, "emit", new=_spy_emit):
        await _try_link_hop(
            entry_url=entry_url,
            entry_page_html=entry_page_html,
            detected=detected,
            profile=profile,
            expected_total_units=None,
            property_id="candidate-order",
            csv_row=None,
            max_hops=7,
            llm_navigation_hints=llm_navigation_hints,
            embedded_portal_hints=embedded_portal_hints,
            visited_urls={entry_url},
        )

    assert candidate_records, "LINK_HOP_STARTED must expose the ranked candidates"
    return candidate_records[0]


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
async def test_wpu_score_outranks_embedded_portal_and_llm_hint() -> None:
    """A proven WPU stays ahead of two fresh 10,000-point suggestions.

    SecureCafe URLs receive a 120-point host/path bonus from SourceRanker. The
    trusted source score must be preserved, or that bonus turns an untested
    ``floorplans.aspx`` portal hint into 10,120 and sends it ahead of the
    profile's previously proven 10,001 route.
    """
    profile = ScrapeProfile(canonical_id="trusted-score-order")
    profile.navigation.winning_page_url = "https://x.com/proven-good"
    candidates = await _capture_started_candidates(
        entry_url="https://x.com/",
        entry_page_html=_entry_html_with_links(),
        detected=DetectedPMS(pms="rentcafe", confidence=0.9),
        profile=profile,
        llm_navigation_hints=["https://hint.example/availability"],
        embedded_portal_hints=[
            (
                "https://property.securecafe.com/onlineleasing/content3/floorplans.aspx",
                "securecafe",
            )
        ],
    )

    trusted = [
        candidate
        for candidate in candidates
        if candidate["anchor"].startswith(
            ("profile:winning", "embedded-portal:", "llm-hint:")
        )
    ]
    assert [(candidate["anchor"].split(":", 1)[0], candidate["score"]) for candidate in trusted] == [
        ("profile", 10_001),
        ("embedded-portal", 10_000),
        ("llm-hint", 10_000),
    ]


@pytest.mark.asyncio
async def test_internal_link_ranking_unchanged_by_trusted_score_preservation() -> None:
    """Ordinary page-authored links retain their existing 5,600 ordering."""
    candidates = await _capture_started_candidates(
        entry_url="https://x.com/",
        entry_page_html=_entry_html_with_links(),
        detected=DetectedPMS(pms="unknown", confidence=0.0),
        profile=None,
    )

    internal = [
        (candidate["url"], candidate["score"], candidate["anchor"])
        for candidate in candidates
        if candidate["anchor"] in {"availability", "floor plans"}
    ]
    assert internal == [
        ("https://x.com/availability", 5_600, "availability"),
        ("https://x.com/floor-plans", 5_600, "floor plans"),
    ]


_BROWNSTONE_ENTRY = "https://www.brownstonetx.com/"
_BROWNSTONE_PLAN_LEAF = (
    "https://www.brownstonetx.com/floorplans/uvalde-TX/"
    "brownstone-apartments/3-bedroom-2-bath-c1-55-1/"
)
_BROWNSTONE_INDEX = (
    "https://www.brownstonetx.com/uvalde/brownstone-apartments/conventional/"
)


def _brownstone_entry_html(*, with_inventory_index: bool = True) -> str:
    index = (
        '<a href="/uvalde/brownstone-apartments/conventional/">Floor Plans</a>'
        if with_inventory_index
        else '<a href="/amenities/">Amenities</a>'
    )
    return f"<html><body><nav>{index}</nav></body></html>"


async def _brownstone_visit_order(
    *, winning_page_url: str, with_inventory_index: bool
) -> list[str]:
    profile = ScrapeProfile(canonical_id="brownstone-routing")
    profile.navigation.winning_page_url = winning_page_url
    visit_log: list[str] = []
    stub = await _stubbed_fetch_factory(visit_log)

    async def _no_units_scrape(**kwargs: Any) -> dict[str, Any]:
        return {"units": [], "extraction_tier_used": None, "errors": []}

    with patch("ma_poc.fetch.fetch", new=stub), patch(
        "ma_poc.pms.scraper.scrape", new=_no_units_scrape
    ):
        await _try_link_hop(
            entry_url=_BROWNSTONE_ENTRY,
            entry_page_html=_brownstone_entry_html(
                with_inventory_index=with_inventory_index
            ),
            detected=DetectedPMS(pms="entrata", confidence=0.95),
            profile=profile,
            expected_total_units=None,
            property_id="brownstone-routing",
            csv_row=None,
            max_hops=3,
            visited_urls={_BROWNSTONE_ENTRY},
        )
    return visit_log


@pytest.mark.asyncio
async def test_entrata_explicit_inventory_index_outranks_stale_plan_leaf_wpu() -> None:
    """Brownstone's explicit property index must precede yesterday's C1 leaf."""
    visit_log = await _brownstone_visit_order(
        winning_page_url=_BROWNSTONE_PLAN_LEAF,
        with_inventory_index=True,
    )

    assert visit_log[:2] == [_BROWNSTONE_INDEX, _BROWNSTONE_PLAN_LEAF]

    profile = ScrapeProfile(canonical_id="brownstone-score-order")
    profile.navigation.winning_page_url = _BROWNSTONE_PLAN_LEAF
    candidates = await _capture_started_candidates(
        entry_url=_BROWNSTONE_ENTRY,
        entry_page_html=_brownstone_entry_html(with_inventory_index=True),
        detected=DetectedPMS(pms="entrata", confidence=0.95),
        profile=profile,
    )
    assert [
        (candidate["url"], candidate["score"])
        for candidate in candidates[:2]
    ] == [
        (_BROWNSTONE_INDEX, 10_002),
        (_BROWNSTONE_PLAN_LEAF, 10_001),
    ]


@pytest.mark.asyncio
async def test_entrata_plan_leaf_wpu_stays_first_without_explicit_index() -> None:
    """No page-authored inventory index means the proven leaf remains first."""
    visit_log = await _brownstone_visit_order(
        winning_page_url=_BROWNSTONE_PLAN_LEAF,
        with_inventory_index=False,
    )

    assert visit_log[0] == _BROWNSTONE_PLAN_LEAF


@pytest.mark.asyncio
async def test_entrata_property_wide_wpu_stays_first_over_explicit_index() -> None:
    """Ordinary property-wide navigation memory keeps the global WPU contract."""
    property_wide_wpu = "https://www.brownstonetx.com/floorplans/"
    visit_log = await _brownstone_visit_order(
        winning_page_url=property_wide_wpu,
        with_inventory_index=True,
    )

    assert visit_log[0] == property_wide_wpu


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

    # Neither dead-end URL was visited:
    assert "https://x.com/availability" not in visit_log
    assert "https://x.com/floor-plans" not in visit_log


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
