"""LAYER 4 (CONSUME) — runtime hint consumption end-to-end.

Pins the runtime contract for the two LLM hint flows the system is
designed to act on:

  1. **LLM nav_hint** → ``_try_link_hop`` ranks it first → BEFORE the
     sub-page ``scrape()`` is invoked, ``shared_budget["llm_monolithic"]``
     is reset 0 → 1 (high-confidence override). The sub-page sees the
     mutated budget when scrape() is called.

  2. **Keyword-ranked candidate** (no LLM hint) → no reset. The reset
     is gated on LLM provenance specifically.

  3. **Profile API hint** (``profile.api_hints.known_endpoints``) →
     ``GenericAdapter.extract()`` probes the URL via
     ``page.evaluate(fetch+credentials)`` at scrape time. Without
     this consume path, persisted endpoints would be one-way writes.

Existing isolated tests cover ``_refresh_monolithic_budget_for_llm_hint``
in unit-form and the ranker prepending profile/LLM URLs; this file
pins the **timing** contract: the reset must mutate the budget BEFORE
the inner ``scrape()`` is invoked, and the inner scrape must observe
the mutated state.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ma_poc.models.scrape_profile import ApiEndpoint, ApiHints, ScrapeProfile
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.generic import GenericAdapter
from ma_poc.pms.detector import DetectedPMS, detect_pms
from ma_poc.pms.scraper import _try_link_hop


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _passing_html() -> str:
    """≥1024 bytes + rent tokens so the LLM gate would admit this body
    if it ever ran on it. Used to satisfy adapter-side body checks."""
    units = "\n".join(
        f"""
        <div class="unit-card">
          <span class="floor-plan">Plan {i}</span>
          <span class="rent">${1500 + i * 100}/mo</span>
          <span class="size">{700 + i * 50} sqft</span>
          <span class="beds">{1 + (i % 2)} bed</span>
          <span class="baths">{1 + (i % 2)} bath</span>
        </div>
        """
        for i in range(5)
    )
    return f"""<!DOCTYPE html>
    <html><head><title>Apartments</title></head>
    <body><main>
    <h1>Available Apartments</h1>
    <p>Browse our floor plans and find your new home. Rent starts at $1,500/mo.
    Studio, 1 bedroom, 2 bedroom layouts available with various square footage.
    Our spacious floor plans offer modern finishes and ample sqft to suit
    every lifestyle. Lease terms range from 6 to 18 months with flexible
    move-in dates.</p>
    <section id="pricing">{units}</section>
    <p>For more details about availability dates, lease terms, and move-in
    specials, please contact our leasing office. We have spacious floor
    plans, modern finishes, and competitive rent in the area.</p>
    </main></body></html>"""


def _entry_html_with_decoy_keyword_links() -> str:
    """Entry-page HTML with anchor-text candidates the keyword ranker
    will pick up. The LLM hint must outrank these — visit ordering
    proves the LLM hint runs first."""
    return """<!DOCTYPE html>
    <html><body>
    <a href="/about">About Us</a>
    <a href="/contact">Contact</a>
    <a href="/availability">Availability</a>
    <a href="/floor-plans">Floor Plans</a>
    </body></html>"""


def _make_fetch_result(outcome: str = "OK", body: bytes | None = None):
    from types import SimpleNamespace
    return SimpleNamespace(
        outcome=SimpleNamespace(value=outcome),
        body=body or _passing_html().encode("utf-8"),
        elapsed_ms=10,
    )


# ── 1. LLM nav-hint consumption AT RUNTIME triggers monolithic reset ─────────


@pytest.mark.asyncio
async def test_llm_nav_hint_consumption_resets_monolithic_budget_before_subscrape() -> None:
    """The headline runtime contract:

      1. Entry page consumed the per-property monolithic budget (set
         to 0 to simulate post-entry-call state).
      2. The monolithic LLM emitted a ``navigation_hint`` so we hop
         to the suggested URL.
      3. ``_try_link_hop`` resets ``shared_budget["llm_monolithic"]``
         from 0 to 1 BEFORE invoking the inner ``scrape()`` on the
         hinted URL — the sub-page is supposed to use that fresh
         monolithic call.

    We assert the reset happened BEFORE the inner scrape by snapshotting
    ``shared_budget`` inside the mocked ``scrape()`` (deep-copied so
    later mutations don't poison the captured state). The snapshot
    proves the inner call observed the mutated budget."""
    shared_budget: dict[str, Any] = {
        "llm_api_calls": 1,
        "llm_dom_calls": 0,
        "llm_monolithic": 0,  # pre-consumed by the entry page
        "link_hop": 3,
        "_cost_cap_usd": 1.50,
    }

    visit_log: list[str] = []
    budget_observed_by_subscrape: list[dict[str, Any]] = []

    async def _stub_fetch(task: Any) -> Any:
        url = task.url if hasattr(task, "url") else str(task)
        visit_log.append(url)
        return _make_fetch_result()

    async def _capture_inner_scrape(**kwargs: Any) -> dict[str, Any]:
        """Snapshot ``shared_budget`` at scrape() entry. Must
        deep-copy — the budget dict is the same reference and keeps
        mutating after this call returns."""
        sb = kwargs.get("shared_budget") or {}
        budget_observed_by_subscrape.append(dict(sb))
        return {"units": [], "extraction_tier_used": None, "errors": []}

    # Cheap-GET gate off — it probe_get()s the LIVE network once per hop
    # candidate and retires the hop as DEAD_URL_GATED before the stubbed
    # fetch is reached. What is under test here is hint ranking + budget
    # reset, not the gate; stubbing it keeps the test hermetic.
    from ma_poc.pms import scraper as pms_scraper

    with patch("ma_poc.fetch.fetch", new=_stub_fetch), patch(
        "ma_poc.pms.scraper.scrape", new=_capture_inner_scrape
    ), patch.object(
        pms_scraper, "_crawl_get_gate_should_skip", lambda url: False
    ):
        await _try_link_hop(
            entry_url="https://x.com/",
            entry_page_html=_entry_html_with_decoy_keyword_links(),
            detected=DetectedPMS(pms="unknown", confidence=0.0),
            profile=None,
            expected_total_units=None,
            property_id="runtime-001",
            csv_row=None,
            max_hops=3,
            llm_navigation_hints=["/llm-suggested"],
            visited_urls={"https://x.com/"},
            shared_budget=shared_budget,
        )

    # The LLM-hinted URL was tried first — outranking keyword candidates:
    assert visit_log[0] == "https://x.com/llm-suggested", (
        f"LLM hint must rank first; visit_log={visit_log}"
    )
    assert budget_observed_by_subscrape, "inner scrape() must be invoked"
    # Critical timing: at the moment the sub-page scrape was called,
    # llm_monolithic had been reset 0 → 1. Without this, the sub-page
    # would inherit the entry-page-exhausted budget and never get a
    # monolithic LLM call on the URL the LLM specifically pointed at.
    assert budget_observed_by_subscrape[0]["llm_monolithic"] == 1, (
        f"shared_budget at sub-page scrape time was "
        f"{budget_observed_by_subscrape[0]} — reset must fire before scrape()"
    )
    # Mutation persists post-hop (same dict reference):
    assert shared_budget["llm_monolithic"] == 1


@pytest.mark.asyncio
async def test_keyword_ranked_hop_does_not_reset_monolithic_budget() -> None:
    """Negative pin: a keyword-ranked candidate (no LLM hint, no
    profile candidate) does NOT trigger the monolithic budget reset.
    The reset is gated on LLM provenance specifically — keyword
    guesses don't qualify because they're untrusted."""
    shared_budget: dict[str, Any] = {
        "llm_api_calls": 1,
        "llm_dom_calls": 0,
        "llm_monolithic": 0,
        "link_hop": 3,
        "_cost_cap_usd": 1.50,
    }

    budget_observed_by_subscrape: list[dict[str, Any]] = []

    async def _stub_fetch(task: Any) -> Any:
        return _make_fetch_result()

    async def _capture_inner_scrape(**kwargs: Any) -> dict[str, Any]:
        sb = kwargs.get("shared_budget") or {}
        budget_observed_by_subscrape.append(dict(sb))
        return {"units": [], "extraction_tier_used": None, "errors": []}

    # Cheap-GET gate off — it probe_get()s the LIVE network once per hop
    # candidate and retires the hop as DEAD_URL_GATED before the stubbed
    # fetch is reached. What is under test here is hint ranking + budget
    # reset, not the gate; stubbing it keeps the test hermetic.
    from ma_poc.pms import scraper as pms_scraper

    with patch("ma_poc.fetch.fetch", new=_stub_fetch), patch(
        "ma_poc.pms.scraper.scrape", new=_capture_inner_scrape
    ), patch.object(
        pms_scraper, "_crawl_get_gate_should_skip", lambda url: False
    ):
        await _try_link_hop(
            entry_url="https://x.com/",
            entry_page_html=_entry_html_with_decoy_keyword_links(),
            detected=DetectedPMS(pms="unknown", confidence=0.0),
            profile=None,
            expected_total_units=None,
            property_id="runtime-002",
            csv_row=None,
            max_hops=3,
            llm_navigation_hints=None,  # NO LLM hint — keyword-only path
            visited_urls={"https://x.com/"},
            shared_budget=shared_budget,
        )

    assert budget_observed_by_subscrape, "inner scrape() must be invoked"
    # Without an LLM-anchored hop, the monolithic counter stays at 0
    # for every sub-page scrape:
    for snapshot in budget_observed_by_subscrape:
        assert snapshot["llm_monolithic"] == 0, (
            f"keyword hop reset budget unexpectedly: {snapshot}"
        )
    assert shared_budget["llm_monolithic"] == 0


# ── 2. Profile API hint consumed AT RUNTIME via page.evaluate ────────────────


class _MockPage:
    """Playwright Page stand-in. Records ``evaluate`` calls so tests
    can assert the runtime probe actually fired."""

    def __init__(self, evaluate_returns: dict[str, Any] | None = None) -> None:
        self._returns = evaluate_returns or {}
        self.evaluate_calls: list[tuple[str, str]] = []

    async def evaluate(self, expr: str, url: str) -> Any:
        self.evaluate_calls.append((expr, url))
        return self._returns.get(url)


def _suppress_deterministic_tiers():
    """Stub deterministic tiers so the LLM tiers are reachable.

    Returns a list of patch context managers the caller starts/stops."""
    return [
        patch("ma_poc.pms.adapters.generic.parse_generic_api", return_value=[]),
        patch("ma_poc.pms.adapters.generic._dr_parse_api_responses", return_value=[]),
        patch("ma_poc.pms.adapters.generic.extract_units_from_dom", return_value=([], "none")),
        patch("ma_poc.pms.adapters.generic.extract_jsonld_from_html", return_value=[]),
        patch("ma_poc.pms.adapters.generic.extract_embedded_blobs_from_html", return_value=[]),
    ]


@pytest.mark.asyncio
async def test_profile_api_hint_probed_at_runtime_with_credentials() -> None:
    """Bug 3.2 runtime contract: when ``profile.api_hints.known_endpoints``
    carries a previously-discovered API URL AND a Playwright page is
    available at scrape time, ``GenericAdapter.extract()`` MUST issue
    a ``page.evaluate(fetch+credentials)`` for that URL.

    The probe is the deterministic equivalent of the entry page
    happening to fire the same XHR — without it, sites whose XHRs
    require interaction (modal-gated, click-required, etc.) lose
    their cached endpoint after the first run.

    We assert two things at runtime:
      1. ``page.evaluate`` was called with the suggested URL.
      2. The fetch JS includes ``credentials: 'include'`` so cookies
         travel with the probe (matches how the page itself would
         issue the XHR — required for session-gated APIs)."""
    profile = ScrapeProfile(canonical_id="runtime-api-001")
    profile.api_hints = ApiHints(
        known_endpoints=[
            ApiEndpoint(url_pattern="https://api.example.com/v1/units"),
        ],
    )
    page = _MockPage(evaluate_returns={})  # body=None — probe records the call regardless

    from types import SimpleNamespace
    fetch_result = SimpleNamespace(body=_passing_html().encode("utf-8"))
    ctx = AdapterContext(
        base_url="https://example.com/",
        detected=detect_pms("https://example.com/"),
        profile=profile,
        expected_total_units=None,
        property_id="runtime-api-001",
        fetch_result=fetch_result,
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    ctx._page = page  # type: ignore[attr-defined]

    patches = _suppress_deterministic_tiers()
    for p in patches:
        p.start()
    try:
        with patch(
            "ma_poc.services.llm_extractor.analyze_api_with_llm",
            new=AsyncMock(return_value=([], None, False, None)),
        ), patch(
            "ma_poc.services.llm_extractor.analyze_dom_with_llm",
            new=AsyncMock(return_value=([], None, None)),
        ), patch(
            "ma_poc.services.llm_extractor.extract_with_llm",
            new=AsyncMock(return_value=([], {}, "raw", None)),
        ):
            adapter = GenericAdapter()
            await adapter.extract(page, ctx)  # type: ignore[arg-type]
    finally:
        for p in patches:
            p.stop()

    # (1) The suggested URL was probed at runtime:
    probed_urls = [url for _, url in page.evaluate_calls]
    assert "https://api.example.com/v1/units" in probed_urls, (
        f"profile.api_hints.known_endpoints must be probed at runtime; "
        f"page.evaluate calls were {probed_urls}"
    )

    # (2) The probe sends cookies along (credentials: 'include') so
    # session-gated APIs respond as if the page itself issued the XHR.
    matching_calls = [
        (expr, url) for (expr, url) in page.evaluate_calls
        if url == "https://api.example.com/v1/units"
    ]
    assert matching_calls, "no evaluate call for the probed URL"
    expr, _ = matching_calls[0]
    assert "credentials" in expr and "include" in expr, (
        f"probe JS must request credentials: 'include'; got: {expr!r}"
    )


@pytest.mark.asyncio
async def test_full_runtime_loop_api_hint_probed_and_nav_hint_resets_budget() -> None:
    """The end-to-end self-learning loop runtime test.

    Setup:
      - Profile carries an API hint from a previous run
        (``known_endpoints``).
      - The current run's monolithic LLM emits a ``navigation_hint``
        and the entry page consumed monolithic budget on it.

    Runtime expectations:
      - The API hint is probed via ``page.evaluate`` during the
        adapter's extract step (consume side of the API hint).
      - ``_try_link_hop`` is invoked separately with the LLM nav_hint
        and the post-extract budget; the hop on the LLM-anchored URL
        resets ``llm_monolithic`` from 0 to 1 BEFORE the sub-page
        scrape (consume side of the navigation hint).

    This test exercises both flows in sequence to prove they
    cooperate — neither helper interferes with the other and both
    consume their respective stored or newly-emitted hints."""
    # ── Phase A: extract() probes the profile's API hint ─────────────
    profile = ScrapeProfile(canonical_id="full-loop-001")
    profile.api_hints = ApiHints(
        known_endpoints=[
            ApiEndpoint(url_pattern="https://api.example.com/v1/units"),
        ],
    )
    page = _MockPage(evaluate_returns={})

    from types import SimpleNamespace
    fetch_result = SimpleNamespace(body=_passing_html().encode("utf-8"))
    ctx = AdapterContext(
        base_url="https://example.com/",
        detected=detect_pms("https://example.com/"),
        profile=profile,
        expected_total_units=None,
        property_id="full-loop-001",
        fetch_result=fetch_result,
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    ctx._page = page  # type: ignore[attr-defined]

    patches = _suppress_deterministic_tiers()
    for p in patches:
        p.start()
    try:
        with patch(
            "ma_poc.services.llm_extractor.analyze_api_with_llm",
            new=AsyncMock(return_value=([], None, False, None)),
        ), patch(
            "ma_poc.services.llm_extractor.analyze_dom_with_llm",
            new=AsyncMock(return_value=([], None, None)),
        ), patch(
            "ma_poc.services.llm_extractor.extract_with_llm",
            new=AsyncMock(return_value=([], {}, "raw", None)),
        ):
            adapter = GenericAdapter()
            await adapter.extract(page, ctx)  # type: ignore[arg-type]
    finally:
        for p in patches:
            p.stop()

    probed_urls = [url for _, url in page.evaluate_calls]
    assert "https://api.example.com/v1/units" in probed_urls, (
        "Phase A — API hint probe didn't fire at extract time"
    )

    # ── Phase B: link-hop on LLM nav_hint resets monolithic budget ───
    shared_budget: dict[str, Any] = {
        "llm_api_calls": 1,
        "llm_dom_calls": 0,
        "llm_monolithic": 0,  # entry-page extract consumed it
        "link_hop": 3,
        "_cost_cap_usd": 1.50,
    }
    visit_log: list[str] = []
    budget_at_subscrape: list[dict[str, Any]] = []

    async def _stub_fetch(task: Any) -> Any:
        url = task.url if hasattr(task, "url") else str(task)
        visit_log.append(url)
        return _make_fetch_result()

    async def _capture_inner_scrape(**kwargs: Any) -> dict[str, Any]:
        sb = kwargs.get("shared_budget") or {}
        budget_at_subscrape.append(dict(sb))
        return {"units": [], "extraction_tier_used": None, "errors": []}

    # Cheap-GET gate off — it probe_get()s the LIVE network once per hop
    # candidate and retires the hop as DEAD_URL_GATED before the stubbed
    # fetch is reached. What is under test here is hint ranking + budget
    # reset, not the gate; stubbing it keeps the test hermetic.
    from ma_poc.pms import scraper as pms_scraper

    with patch("ma_poc.fetch.fetch", new=_stub_fetch), patch(
        "ma_poc.pms.scraper.scrape", new=_capture_inner_scrape
    ), patch.object(
        pms_scraper, "_crawl_get_gate_should_skip", lambda url: False
    ):
        await _try_link_hop(
            entry_url="https://example.com/",
            entry_page_html=_entry_html_with_decoy_keyword_links(),
            detected=DetectedPMS(pms="unknown", confidence=0.0),
            profile=profile,
            expected_total_units=None,
            property_id="full-loop-001",
            csv_row=None,
            max_hops=3,
            llm_navigation_hints=["/floorplans"],  # current-run LLM hint
            visited_urls={"https://example.com/"},
            shared_budget=shared_budget,
        )

    # Phase B assertion: nav hint URL is first AND budget was reset
    # before that hop's inner scrape.
    assert visit_log[0] == "https://example.com/floorplans", (
        f"Phase B — LLM nav_hint must rank first; visit_log={visit_log}"
    )
    assert budget_at_subscrape[0]["llm_monolithic"] == 1, (
        f"Phase B — monolithic budget must reset before sub-page scrape; "
        f"observed {budget_at_subscrape[0]}"
    )
