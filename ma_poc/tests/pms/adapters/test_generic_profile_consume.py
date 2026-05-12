"""LAYER 4 (CONSUME) — pin profile-driven endpoint probe + prior
observations injection in the GenericAdapter.

Two consume-side fixes are pinned here:

  Bug 3.2 / 3.6 — Sub-tier 0a profile-driven endpoint probe:
    When a Playwright page is available and the profile remembers
    ``known_endpoints`` / ``widget_endpoints`` from previous runs,
    deterministically fetch each via ``page.evaluate(fetch+credentials)``
    and feed the responses into the existing API-response pipeline.
    Without this, profile-recorded endpoints were one-way writes —
    persisted but never probed.

  Bug 3.7 — ``field_mapping_notes`` injected as PRIOR OBSERVATIONS:
    The adapter forwards stored notes through ``property_context`` so
    the LLM extractor's prompt builder includes them in the system
    prompt. Pre-fix the notes were dead writes (persisted, never read
    at scrape time).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.models.scrape_profile import (
    ApiEndpoint,
    ApiHints,
    LlmArtifacts,
    ScrapeProfile,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.generic import GenericAdapter
from ma_poc.pms.detector import detect_pms


def _passing_html() -> str:
    """Body that passes the LLM gate (≥1024 bytes + rent tokens).

    The gate (``services.llm_gate.page_has_meaningful_body``) requires
    1024+ UTF-8 bytes with at least one rent-context token. Padding
    text mentions rent / lease / floorplan vocabulary so the LLM gate
    + ``_extract_rent_dom_section`` both see a viable body."""
    units = "\n".join(
        f"""
        <div class="unit-card">
          <span class="floor-plan">Plan {i}</span>
          <span class="rent">${1500 + i * 100}/mo</span>
          <span class="size">{700 + i * 50} sqft</span>
          <span class="beds">{1 + (i % 2)} bed</span>
          <span class="baths">{1 + (i % 2)} bath</span>
          <span class="avail">Available now</span>
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
    <section id="pricing">
    {units}
    </section>
    <p>For more details about availability dates, lease terms, and move-in
    specials, please contact our leasing office. We have spacious floor
    plans with modern finishes, ample sqft, and competitive rent pricing
    in the area. Schedule a tour to see our apartment community in person
    and learn about our current move-in specials and lease incentives.</p>
    </main></body></html>"""


class _MockPage:
    """Playwright Page stand-in with an ``evaluate`` mock so we can
    capture and direct profile-driven probe behaviour."""

    def __init__(self, evaluate_returns: dict[str, Any] | None = None) -> None:
        self._returns = evaluate_returns or {}
        self.evaluate_calls: list[tuple[str, str]] = []

    async def evaluate(self, expr: str, url: str) -> Any:
        """Stub for the page.evaluate(fetch_js, url) pattern. Returns
        the body the test configured for that URL, or None for misses."""
        self.evaluate_calls.append((expr, url))
        return self._returns.get(url)


def _make_ctx(
    profile: Any,
    page: Any,
    api_responses: list[dict[str, Any]] | None = None,
) -> AdapterContext:
    from types import SimpleNamespace
    fetch_result = SimpleNamespace(body=_passing_html().encode("utf-8"))
    ctx = AdapterContext(
        base_url="https://example.com/",
        detected=detect_pms("https://example.com/"),
        profile=profile,
        expected_total_units=None,
        property_id="TEST",
        fetch_result=fetch_result,
    )
    ctx._api_responses = api_responses or []  # type: ignore[attr-defined]
    ctx._page = page  # type: ignore[attr-defined]
    return ctx


def _suppress_deterministic_tiers():
    """Stub the deterministic tiers so we can isolate the profile
    probe + prior observations behaviour."""
    return [
        patch("ma_poc.pms.adapters.generic.parse_generic_api", return_value=[]),
        patch("ma_poc.pms.adapters.generic._dr_parse_api_responses", return_value=[]),
        patch("ma_poc.pms.adapters.generic.extract_units_from_dom", return_value=([], "none")),
        patch("ma_poc.pms.adapters.generic.extract_jsonld_from_html", return_value=[]),
        patch("ma_poc.pms.adapters.generic.extract_embedded_blobs_from_html", return_value=[]),
    ]


# ── Profile-driven endpoint probe (Bug 3.2 / 3.6) ────────────────────────────


@pytest.mark.asyncio
async def test_known_endpoints_probed_when_page_available() -> None:
    """Profile remembers a unit-bearing API URL from a previous run.
    With a Playwright page available, the adapter probes that URL
    deterministically via ``page.evaluate(fetch+credentials)``."""
    profile = ScrapeProfile(canonical_id="probe-001")
    profile.api_hints = ApiHints(
        known_endpoints=[
            ApiEndpoint(url_pattern="https://api.example.com/units"),
        ]
    )
    page = _MockPage(
        evaluate_returns={
            "https://api.example.com/units": {
                "data": {"units": [{"unit_id": "U1", "rent": 1500}]}
            }
        }
    )

    patches = _suppress_deterministic_tiers()
    for p in patches:
        p.start()
    try:
        with patch(
            "ma_poc.services.llm_extractor.analyze_api_with_llm",
            new=AsyncMock(return_value=([], None, False, None)),
        ):
            adapter = GenericAdapter()
            ctx = _make_ctx(profile=profile, page=page)
            await adapter.extract(page, ctx)  # type: ignore[arg-type]
    finally:
        for p in patches:
            p.stop()

    # The profile-probe issued a page.evaluate call to the known URL:
    probed_urls = [url for _, url in page.evaluate_calls]
    assert "https://api.example.com/units" in probed_urls


@pytest.mark.asyncio
async def test_widget_endpoints_probed_alongside_known_endpoints() -> None:
    """Entrata-style widget endpoints stored on the profile are also
    probed — both lists feed the same probe loop."""
    profile = ScrapeProfile(canonical_id="probe-002")
    profile.api_hints = ApiHints(
        known_endpoints=[
            ApiEndpoint(url_pattern="https://api.example.com/v1/units"),
        ],
        widget_endpoints=[
            "https://entrata.com/widgets/abc/units",
        ],
    )
    page = _MockPage(evaluate_returns={})  # all probes return None — no body

    patches = _suppress_deterministic_tiers()
    for p in patches:
        p.start()
    try:
        with patch(
            "ma_poc.services.llm_extractor.analyze_api_with_llm",
            new=AsyncMock(return_value=([], None, False, None)),
        ):
            adapter = GenericAdapter()
            ctx = _make_ctx(profile=profile, page=page)
            await adapter.extract(page, ctx)  # type: ignore[arg-type]
    finally:
        for p in patches:
            p.stop()

    probed_urls = [url for _, url in page.evaluate_calls]
    assert "https://api.example.com/v1/units" in probed_urls
    assert "https://entrata.com/widgets/abc/units" in probed_urls


@pytest.mark.asyncio
async def test_already_captured_urls_not_re_probed() -> None:
    """When the network log already captured a profile-recorded URL
    via XHR interception, we don't redundantly probe it. Saves
    wall-clock + bandwidth without sacrificing coverage."""
    profile = ScrapeProfile(canonical_id="probe-003")
    profile.api_hints = ApiHints(
        known_endpoints=[
            ApiEndpoint(url_pattern="https://api.example.com/units"),
        ],
    )
    page = _MockPage(evaluate_returns={})

    # Pre-existing api_response shows the URL was already captured.
    api_responses = [
        {
            "url": "https://api.example.com/units",
            "body": {"data": {"units": []}},
        }
    ]

    patches = _suppress_deterministic_tiers()
    for p in patches:
        p.start()
    try:
        with patch(
            "ma_poc.services.llm_extractor.analyze_api_with_llm",
            new=AsyncMock(return_value=([], None, False, None)),
        ):
            adapter = GenericAdapter()
            ctx = _make_ctx(profile=profile, page=page, api_responses=api_responses)
            await adapter.extract(page, ctx)  # type: ignore[arg-type]
    finally:
        for p in patches:
            p.stop()

    probed_urls = [url for _, url in page.evaluate_calls]
    # The already-captured URL was filtered out before probing.
    assert "https://api.example.com/units" not in probed_urls


@pytest.mark.asyncio
async def test_probe_skipped_when_no_page_available() -> None:
    """Without a live Playwright page (e.g. fetch-only render path),
    the probe quietly skips — no crash, no event."""
    profile = ScrapeProfile(canonical_id="probe-004")
    profile.api_hints = ApiHints(
        known_endpoints=[
            ApiEndpoint(url_pattern="https://api.example.com/units"),
        ],
    )

    patches = _suppress_deterministic_tiers()
    for p in patches:
        p.start()
    try:
        with patch(
            "ma_poc.services.llm_extractor.analyze_api_with_llm",
            new=AsyncMock(return_value=([], None, False, None)),
        ):
            adapter = GenericAdapter()
            from types import SimpleNamespace
            fetch_result = SimpleNamespace(body=_passing_html().encode("utf-8"))
            ctx = AdapterContext(
                base_url="https://example.com/",
                detected=detect_pms("https://example.com/"),
                profile=profile,
                expected_total_units=None,
                property_id="TEST",
                fetch_result=fetch_result,
            )
            ctx._api_responses = []  # type: ignore[attr-defined]
            # No _page or page attribute → probe must skip cleanly.
            result = await adapter.extract(None, ctx)  # type: ignore[arg-type]
    finally:
        for p in patches:
            p.stop()

    # Result returned without crashing — that's the contract.
    assert result is not None


@pytest.mark.asyncio
async def test_probe_capped_at_five_endpoints() -> None:
    """When the profile remembers many endpoints (cluster-property
    accumulation), only up to 5 are probed per scrape so the
    per-property time budget stays bounded."""
    profile = ScrapeProfile(canonical_id="probe-005")
    profile.api_hints = ApiHints(
        known_endpoints=[
            ApiEndpoint(url_pattern=f"https://api.example.com/v{i}/units")
            for i in range(10)  # 10 endpoints in profile, cap should be 5
        ],
    )
    page = _MockPage(evaluate_returns={})

    patches = _suppress_deterministic_tiers()
    for p in patches:
        p.start()
    try:
        with patch(
            "ma_poc.services.llm_extractor.analyze_api_with_llm",
            new=AsyncMock(return_value=([], None, False, None)),
        ):
            adapter = GenericAdapter()
            ctx = _make_ctx(profile=profile, page=page)
            await adapter.extract(page, ctx)  # type: ignore[arg-type]
    finally:
        for p in patches:
            p.stop()

    probed_urls = [url for _, url in page.evaluate_calls]
    assert len(probed_urls) <= 5, (
        f"probe must cap at 5 endpoints to bound wall-clock; got {len(probed_urls)}"
    )


@pytest.mark.asyncio
async def test_probe_response_feeds_subsequent_tiers() -> None:
    """When the profile probe successfully retrieves a unit-bearing
    response, that body is added to ``api_responses`` and flows into
    the existing API-response pipeline (so the LLM-API tier or the
    deterministic parser can extract from it)."""
    profile = ScrapeProfile(canonical_id="probe-006")
    profile.api_hints = ApiHints(
        known_endpoints=[
            ApiEndpoint(url_pattern="https://api.example.com/v1/units"),
        ],
    )
    probe_body = {
        "data": {
            "units": [
                # bathrooms required for floor_plan_bed_bath_area qualifier
                {"unit_id": "P1", "rent": 1500, "sqft": 700, "bedrooms": 1, "bathrooms": 1},
            ]
        }
    }
    page = _MockPage(
        evaluate_returns={"https://api.example.com/v1/units": probe_body}
    )

    # Capture what analyze_api_with_llm was called with — it should
    # see the probed body, not just the empty network log.
    api_calls: list[dict[str, Any]] = []

    async def _capture_api(api_response, ctx_dict, prop_id, **kwargs):
        api_calls.append({"url": api_response.get("url"), "body": api_response.get("body")})
        return ([], None, False, None)

    patches = _suppress_deterministic_tiers()
    for p in patches:
        p.start()
    try:
        with patch(
            "ma_poc.services.llm_extractor.analyze_api_with_llm",
            new=_capture_api,
        ):
            adapter = GenericAdapter()
            ctx = _make_ctx(profile=profile, page=page)
            await adapter.extract(page, ctx)  # type: ignore[arg-type]
    finally:
        for p in patches:
            p.stop()

    # The LLM API tier saw the probed body:
    assert any(
        call["url"] == "https://api.example.com/v1/units" and call["body"] == probe_body
        for call in api_calls
    )


# ── PRIOR OBSERVATIONS injection (Bug 3.7) ───────────────────────────────────


@pytest.mark.asyncio
async def test_prior_field_mapping_notes_injected_into_property_context() -> None:
    """When the profile carries ``llm_artifacts.field_mapping_notes``
    from a previous successful run, the adapter must thread them into
    ``property_context["prior_observations"]`` so the LLM prompt
    builder can render the PRIOR OBSERVATIONS preamble. Without this
    the LLM rediscovers the same site organisation every run."""
    profile = ScrapeProfile(canonical_id="prior-001")
    profile.llm_artifacts = LlmArtifacts(
        field_mapping_notes="Units live in .pricing > .price; rent is in $NNN format"
    )

    captured_contexts: list[dict[str, Any]] = []

    async def _capture_dom(dom_html, page_url, property_context, *args, **kwargs):
        captured_contexts.append(dict(property_context))
        return ([], None, None)

    patches = _suppress_deterministic_tiers()
    for p in patches:
        p.start()
    try:
        with patch(
            "ma_poc.services.llm_extractor.analyze_dom_with_llm",
            new=_capture_dom,
        ), patch(
            "ma_poc.services.llm_extractor.analyze_api_with_llm",
            new=AsyncMock(return_value=([], None, False, None)),
        ):
            adapter = GenericAdapter()
            ctx = _make_ctx(profile=profile, page=None)
            await adapter.extract(None, ctx)  # type: ignore[arg-type]
    finally:
        for p in patches:
            p.stop()

    # The DOM analysis call received property_context with prior_observations:
    assert captured_contexts, "DOM analyzer should have been called"
    ctx_passed = captured_contexts[0]
    assert "prior_observations" in ctx_passed
    assert "Units live in .pricing" in ctx_passed["prior_observations"]


@pytest.mark.asyncio
async def test_no_prior_notes_omits_observations_key() -> None:
    """Cold profiles (no field_mapping_notes) should NOT inject an
    empty prior_observations key — let the placeholder substitution
    render as an empty preamble naturally."""
    profile = ScrapeProfile(canonical_id="prior-002")
    # No field_mapping_notes set.

    captured_contexts: list[dict[str, Any]] = []

    async def _capture_dom(dom_html, page_url, property_context, *args, **kwargs):
        captured_contexts.append(dict(property_context))
        return ([], None, None)

    patches = _suppress_deterministic_tiers()
    for p in patches:
        p.start()
    try:
        with patch(
            "ma_poc.services.llm_extractor.analyze_dom_with_llm",
            new=_capture_dom,
        ), patch(
            "ma_poc.services.llm_extractor.analyze_api_with_llm",
            new=AsyncMock(return_value=([], None, False, None)),
        ):
            adapter = GenericAdapter()
            ctx = _make_ctx(profile=profile, page=None)
            await adapter.extract(None, ctx)  # type: ignore[arg-type]
    finally:
        for p in patches:
            p.stop()

    assert captured_contexts
    # Either the key is absent, or its value is empty/None — both are
    # acceptable; what matters is that a cold profile doesn't pollute
    # the prompt with a stub PRIOR OBSERVATIONS block.
    ctx_passed = captured_contexts[0]
    prior = ctx_passed.get("prior_observations")
    assert not prior, f"cold profile leaked prior_observations: {prior!r}"


@pytest.mark.asyncio
async def test_prior_notes_capped_at_500_chars() -> None:
    """Bounded — overly long notes are truncated when threaded into
    property_context so a runaway LLM response can't blow the prompt
    budget on subsequent runs."""
    profile = ScrapeProfile(canonical_id="prior-003")
    profile.llm_artifacts = LlmArtifacts(
        field_mapping_notes="x" * 10_000  # 10KB of garbage
    )

    captured_contexts: list[dict[str, Any]] = []

    async def _capture_dom(dom_html, page_url, property_context, *args, **kwargs):
        captured_contexts.append(dict(property_context))
        return ([], None, None)

    patches = _suppress_deterministic_tiers()
    for p in patches:
        p.start()
    try:
        with patch(
            "ma_poc.services.llm_extractor.analyze_dom_with_llm",
            new=_capture_dom,
        ), patch(
            "ma_poc.services.llm_extractor.analyze_api_with_llm",
            new=AsyncMock(return_value=([], None, False, None)),
        ):
            adapter = GenericAdapter()
            ctx = _make_ctx(profile=profile, page=None)
            await adapter.extract(None, ctx)  # type: ignore[arg-type]
    finally:
        for p in patches:
            p.stop()

    assert captured_contexts
    prior = captured_contexts[0].get("prior_observations") or ""
    assert len(prior) <= 500, f"prior_observations not capped: len={len(prior)}"
