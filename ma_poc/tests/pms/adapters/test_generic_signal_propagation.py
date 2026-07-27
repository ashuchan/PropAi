"""LAYER 2 (PROPAGATE) — pin the adapter cross-tier hint aggregator.

The GenericAdapter runs up to four LLM sub-tiers (api_targeted /
dom_targeted / monolithic / vision). Each tier can volunteer its own
ancillary signals (navigation_hint, platform_guess,
api_urls_with_data, property_amenities, drift hashes). The previous
behaviour surfaced ONLY the winning tier's hints — every other tier's
output was discarded even though the cost was already paid.

The new aggregator (``_merge_hint_extras`` closure inside ``extract``)
collects every signal across every tier and folds it into
``result._llm_hints`` regardless of which tier ultimately won.

Test strategy: the adapter runs ~6 deterministic sub-tiers BEFORE the
LLM tiers (JSON-LD, embedded JSON, narrow API parser, broad API
parser, DOM cascade). To exercise the LLM-tier aggregator behaviour
in isolation, we patch every deterministic path to return empty so
the cascade falls through to the LLM tiers. Then we mock each LLM
extractor's response to control which signals each tier produces.

Fixture notes:
  * HTML must be ≥1024 bytes AND contain rent tokens to pass the LLM
    gate (``services.llm_gate.page_has_meaningful_body``).
  * API response bodies must contain a list of unit-shaped dicts at a
    known list-key where each item carries ≥2 keys from
    ``_UNIT_SIGNAL_KEYS`` (rent / sqft / bedrooms / …) so
    ``find_unit_list`` + ``has_unit_signals`` admit them.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.generic import GenericAdapter
from ma_poc.pms.detector import detect_pms


class _FakeProbeResponse:
    """Minimal curl_cffi-response shim (``.status_code`` / ``.text``)."""

    __slots__ = ("status_code", "text", "content", "headers", "url")

    def __init__(self, url: str, status_code: int = 404, text: str = "") -> None:
        self.url = url
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _stub_probe_get(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the ``_probe`` seam for every test in this module.

    ``_suppress_deterministic_tiers`` forces ``parse_generic_plan_text``
    to return ``[]``, which sends ``GenericPlanTextAdapter.extract``
    into its ``/floorplans/`` … ``/apartments/`` subpage sweep against
    ``example.com`` — reached via ``scrape()`` in the surface-step test.
    These tests assert on LLM-hint propagation, all of it mocked, so the
    sweep must find nothing: a 404 makes that deterministic.
    """

    def _fake_probe_get(url: str, **_kw: object) -> _FakeProbeResponse:
        return _FakeProbeResponse(url)

    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get", _fake_probe_get
    )


class _DummyPage:
    """Stand-in for Playwright Page. ``screenshot`` is intentionally
    absent so the Tier-5 vision branch's ``hasattr`` check returns
    False and the test stays in the text-LLM path."""


def _passing_html() -> str:
    """Build HTML that passes ``page_has_meaningful_body``: ≥1024 bytes
    and at least one rent-token match. Padding text is unit-bearing so
    ``_extract_rent_dom_section`` finds something to feed DOM-LLM."""
    units_html = "\n".join(
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
    Studio, 1 bedroom, 2 bedroom layouts available with various square footage.</p>
    <section id="pricing">
    {units_html}
    </section>
    <p>For more details about availability dates, lease terms, and move-in
    specials, please contact our leasing office. We have spacious floor plans
    with modern finishes, ample sqft, and competitive rent pricing in the area.</p>
    </main></body></html>"""


def _unit_shaped_api_body() -> dict[str, Any]:
    """Body that passes ``find_unit_list`` + qualifier so ``analyze_api_with_llm``
    is actually invoked.  Keys satisfy the floor_plan_bed_bath_name cross-group
    combination (bedrooms + bathrooms + floor_plan_name)."""
    return {
        "data": {
            "units": [
                {
                    "unit_id": "U1",
                    "rent": 1500,
                    "sqft": 700,
                    "bedrooms": 1,
                    "bathrooms": 1,
                    "floor_plan_name": "A",
                },
                {
                    "unit_id": "U2",
                    "rent": 1700,
                    "sqft": 850,
                    "bedrooms": 2,
                    "bathrooms": 2,
                    "floor_plan_name": "B",
                },
            ]
        }
    }


def _make_ctx(
    api_responses: list[dict[str, Any]] | None = None,
    profile: Any = None,
    html: str | None = None,
    pms: str = "unknown",
) -> AdapterContext:
    from types import SimpleNamespace
    body_html = html if html is not None else _passing_html()
    fetch_result = SimpleNamespace(body=body_html.encode("utf-8"))
    ctx = AdapterContext(
        base_url="https://example.com/",
        detected=detect_pms(f"https://example.com/?pms={pms}"),
        profile=profile,
        expected_total_units=None,
        property_id="TEST",
        fetch_result=fetch_result,
    )
    ctx._api_responses = api_responses or []  # type: ignore[attr-defined]
    return ctx


@contextmanager
def _suppress_deterministic_tiers():
    """Patch every deterministic sub-tier in GenericAdapter to return
    empty so the LLM tiers fire. Lets us test cross-tier hint
    propagation in isolation from parser logic.

    Patches the module-level bindings the adapter uses (these are
    bound at module import via ``from ... import name``, so patching
    the source module wouldn't be observed).

    2026-05-24: also suppress GenericPlanTextAdapter's
    ``parse_generic_plan_text`` — pre-this-date the adapter
    hard-required a live Playwright page so a stub ``page`` made it
    no-op silently. The static-body fallback shipped 2026-05-24 now
    runs against ``ctx.fetch_result.body`` even with a stub page, so
    on the unit-bearing ``_passing_html()`` it would extract plans and
    pre-empt the LLM path these tests exercise. Patching it back to
    empty keeps the test scope on LLM-hint propagation."""
    with patch(
        "ma_poc.pms.adapters.generic.parse_generic_api",
        return_value=[],
    ), patch(
        "ma_poc.pms.adapters.generic._dr_parse_api_responses",
        return_value=[],
    ), patch(
        "ma_poc.pms.adapters.generic.extract_units_from_dom",
        return_value=([], "none"),
    ), patch(
        "ma_poc.pms.adapters.generic.extract_jsonld_from_html",
        return_value=[],
    ), patch(
        "ma_poc.pms.adapters.generic.extract_embedded_blobs_from_html",
        return_value=[],
    ), patch(
        "ma_poc.pms.adapters.generic_plan_text.parse_generic_plan_text",
        return_value=[],
    ):
        yield


def _llm_unit() -> dict[str, Any]:
    """Minimum unit dict that survives ``_normalize_units``."""
    return {
        "unit_id": "U1",
        "floor_plan_name": "A",
        "bedrooms": 1,
        "bathrooms": 1.0,
        "sqft": 700,
        "market_rent_low": 1500,
        "market_rent_high": 1500,
    }


# ── DOM-LLM win path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dom_llm_win_merges_aggregator_with_css_selectors() -> None:
    """When DOM-LLM wins, ``result._llm_hints`` carries css_selectors
    AND every ancillary signal (nav_hint, platform_guess, api_urls).
    Previously the win path emitted a selectors-only dict, dropping
    everything else."""
    dom_units = [_llm_unit()]
    dom_hints = {
        "css_selectors": {"container": ".unit-card"},
        "navigation_hint": "/floorplans",
        "platform_guess": "rentcafe",
        "api_urls_with_data": ["https://api.example.com/units"],
        "property_amenities": ["pool", "gym"],
        "dom_structure_hash": "abc123def456abcd",
    }

    with _suppress_deterministic_tiers(), patch(
        "ma_poc.services.llm_extractor.analyze_dom_with_llm",
        new=AsyncMock(return_value=(dom_units, dom_hints, None)),
    ), patch(
        "ma_poc.services.llm_extractor.analyze_api_with_llm",
        new=AsyncMock(return_value=([], None, False, None)),
    ), patch(
        # Selector self-validation re-extracts via ``extract_with_hints``;
        # we mock that to match the LLM's claim so quality_score stays
        # high and css_selectors aren't dropped on the validation gate.
        "ma_poc.pms.adapters.generic.extract_with_hints",
        return_value=dom_units,
    ):
        adapter = GenericAdapter()
        ctx = _make_ctx()
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]

    assert result.tier_used == "TIER_4_LLM_DOM"
    hints = getattr(result, "_llm_hints", {})
    assert hints.get("css_selectors", {}).get("container") == ".unit-card"
    # Every ancillary signal made the round trip:
    assert hints.get("navigation_hint") == "/floorplans"
    assert hints.get("platform_guess") == "rentcafe"
    assert "https://api.example.com/units" in (hints.get("api_urls_with_data") or [])
    assert "dom_structure_hash" in hints
    # Property amenities surfaced separately for the scraper aggregator:
    prop_amen = getattr(result, "_property_amenities", [])
    assert "pool" in prop_amen and "gym" in prop_amen
    # Navigation hints exposed for link-hop:
    nav_hints = getattr(result, "_llm_navigation_hints", [])
    assert "/floorplans" in nav_hints


@pytest.mark.asyncio
async def test_dom_llm_win_quality_below_threshold_clamps_selectors() -> None:
    """When the LLM's selectors don't reproduce against their own source HTML
    (quality < REPLAY_FLOOR = 0.4), ENABLE_DEGRADED_DOM_PERSIST (default ON)
    saves them at the clamped floor rather than dropping entirely.  The
    css_selectors_quality field is set to the floor so the replay-side gate
    admits the hint as soft-fail.  The rest of the aggregator (nav_hint,
    platform_guess, etc.) MUST also survive — those signals are independent
    of the selector quality."""
    dom_units = [_llm_unit()]
    dom_hints = {
        "css_selectors": {"container": ".unit-card"},
        "navigation_hint": "/floorplans",
        "platform_guess": "rentcafe",
    }

    with _suppress_deterministic_tiers(), patch(
        "ma_poc.services.llm_extractor.analyze_dom_with_llm",
        new=AsyncMock(return_value=(dom_units, dom_hints, None)),
    ), patch(
        "ma_poc.services.llm_extractor.analyze_api_with_llm",
        new=AsyncMock(return_value=([], None, False, None)),
    ), patch(
        # Replay returns 0 units while LLM claimed 1 → quality 0/1 = 0.
        # With degraded persistence ON (default), quality is clamped to
        # REPLAY_FLOOR (0.4) rather than dropped.
        "ma_poc.pms.adapters.generic.extract_with_hints",
        return_value=[],
    ):
        adapter = GenericAdapter()
        ctx = _make_ctx()
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]

    from ma_poc.services.profile_updater import DOM_HINT_QUALITY_REPLAY_FLOOR

    hints = getattr(result, "_llm_hints", {})
    # Degraded persistence: css_selectors present but quality clamped to floor.
    assert hints.get("css_selectors", {}).get("container") == ".unit-card"
    assert hints.get("css_selectors_quality") == DOM_HINT_QUALITY_REPLAY_FLOOR
    # Ancillary signals survived regardless of quality gate:
    assert hints.get("navigation_hint") == "/floorplans"
    assert hints.get("platform_guess") == "rentcafe"


# ── API-LLM win path + noise verdict flow ────────────────────────────────────


@pytest.mark.asyncio
async def test_api_llm_win_surfaces_aggregator_alongside_mappings() -> None:
    """When API-LLM finds units, the mapping triplet flows into
    ``_llm_field_mappings`` for replay AND ancillary signals reach
    ``_llm_hints`` for profile persistence."""
    api_units = [_llm_unit()]
    api_hints = {
        "api_url_pattern": "https://example.com/api/units",
        "json_paths": {"rent": "rent", "unit_id": "id"},
        "response_envelope": "data.units",
        "navigation_hint": "/availability",
        "platform_guess": "knock",
        "property_amenities": ["clubhouse"],
        "api_schema_signature": "0123456789abcdef",
    }
    api_responses = [
        {
            "url": "https://example.com/api/units",
            "body": _unit_shaped_api_body(),
        }
    ]

    with _suppress_deterministic_tiers(), patch(
        "ma_poc.services.llm_extractor.analyze_api_with_llm",
        new=AsyncMock(return_value=(api_units, api_hints, False, None)),
    ):
        adapter = GenericAdapter()
        ctx = _make_ctx(api_responses=api_responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]

    assert result.tier_used == "TIER_4_LLM_API"
    mappings = getattr(result, "_llm_field_mappings", [])
    assert len(mappings) == 1
    assert mappings[0]["api_url_pattern"] == "https://example.com/api/units"
    assert mappings[0]["json_paths"] == {"rent": "rent", "unit_id": "id"}
    hints = getattr(result, "_llm_hints", {})
    assert hints.get("navigation_hint") == "/availability"
    assert hints.get("platform_guess") == "knock"
    verdicts = getattr(result, "_llm_analysis_results", {})
    assert verdicts.get("https://example.com/api/units") == mappings[0]


@pytest.mark.asyncio
async def test_api_noise_verdict_flows_with_specific_reason() -> None:
    """An LLM noise verdict for a specific URL must:
      - skip mapping persistence (no llm_field_mapping for that URL)
      - tag ``_llm_analysis_results[url] = "noise:<reason>"`` so
        profile_updater records the reason in BlockedEndpoint
      - still merge ancillary signals (a noise verdict often comes
        with a navigation_hint to where the real data lives)."""
    noise_hints = {
        "noise_reason": "chatbot_config",
        "navigation_hint": "/api/v2/units",
        "platform_guess": "custom",
    }
    api_responses = [
        {"url": "https://example.com/api/chat", "body": _unit_shaped_api_body()},
    ]

    with _suppress_deterministic_tiers(), patch(
        "ma_poc.services.llm_extractor.analyze_api_with_llm",
        new=AsyncMock(return_value=([], noise_hints, True, None)),
    ), patch(
        "ma_poc.services.llm_extractor.analyze_dom_with_llm",
        new=AsyncMock(return_value=([], None, None)),
    ), patch(
        "ma_poc.services.llm_extractor.extract_with_llm",
        new=AsyncMock(return_value=([], {}, "raw", None)),
    ):
        adapter = GenericAdapter()
        ctx = _make_ctx(api_responses=api_responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]

    verdicts = getattr(result, "_llm_analysis_results", {})
    chat_verdict = verdicts.get("https://example.com/api/chat")
    assert isinstance(chat_verdict, str), f"expected noise: string, got {chat_verdict!r}"
    assert chat_verdict.startswith("noise:")
    assert "chatbot_config" in chat_verdict
    # Ancillary signals from the noise verdict still flow through:
    hints = getattr(result, "_llm_hints", {})
    nav_hints = getattr(result, "_llm_navigation_hints", [])
    assert (
        hints.get("navigation_hint") == "/api/v2/units"
        or "/api/v2/units" in nav_hints
    )


# ── No-tier-win path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aggregator_surfaces_when_no_tier_wins() -> None:
    """Even when every LLM sub-tier returns empty, the aggregated
    signals must reach result._llm_hints so profile_updater can
    persist what was learned. Without this, an "all-empty" property
    re-pays for the same diagnostics next run."""
    noise_hints = {
        "noise_reason": "analytics_pixel",
        "navigation_hint": "/floor-plans",
        "platform_guess": "yardi",
    }
    dom_hints = {
        "css_selectors": {},  # no container — selectors won't be persisted
        "property_amenities": ["pool", "fitness center"],
        "dom_structure_hash": "abc123def456abcd",
    }
    mono_hints = {
        "platform_guess": "rentcafe",  # later tier overrides earlier
        "field_mapping_notes": "looks like a marketing shell",
    }
    api_responses = [
        {"url": "https://example.com/api/analytics", "body": _unit_shaped_api_body()}
    ]

    with _suppress_deterministic_tiers(), patch(
        "ma_poc.services.llm_extractor.analyze_api_with_llm",
        new=AsyncMock(return_value=([], noise_hints, True, None)),
    ), patch(
        "ma_poc.services.llm_extractor.analyze_dom_with_llm",
        new=AsyncMock(return_value=([], dom_hints, None)),
    ), patch(
        "ma_poc.services.llm_extractor.extract_with_llm",
        new=AsyncMock(return_value=([], mono_hints, "raw", None)),
    ):
        adapter = GenericAdapter()
        ctx = _make_ctx(api_responses=api_responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]

    assert not result.units
    hints = getattr(result, "_llm_hints", {})
    assert hints.get("navigation_hint") == "/floor-plans"
    # Later tier (monolithic) overrode earlier (api) on platform_guess:
    assert hints.get("platform_guess") == "rentcafe"
    assert hints.get("field_mapping_notes") == "looks like a marketing shell"
    prop_amen = getattr(result, "_property_amenities", [])
    assert "pool" in prop_amen and "fitness center" in prop_amen
    nav_hints = getattr(result, "_llm_navigation_hints", [])
    assert "/floor-plans" in nav_hints


# ── Aggregator dedup + ordering semantics ────────────────────────────────────


@pytest.mark.asyncio
async def test_property_amenities_accumulate_across_tiers() -> None:
    """Different tiers see different parts of the page. The DOM tier
    might pick up unit-level amenities; the monolithic call sees the
    full page including the amenities tab. The aggregator combines
    BOTH without duplicates."""
    dom_hints = {"property_amenities": ["pool", "gym"]}
    mono_hints = {"property_amenities": ["pool", "dog park", "clubhouse"]}
    api_hints = {"property_amenities": ["clubhouse", "pet friendly"]}
    api_responses = [
        {"url": "https://example.com/api/x", "body": _unit_shaped_api_body()}
    ]

    with _suppress_deterministic_tiers(), patch(
        "ma_poc.services.llm_extractor.analyze_api_with_llm",
        new=AsyncMock(return_value=([], api_hints, True, None)),
    ), patch(
        "ma_poc.services.llm_extractor.analyze_dom_with_llm",
        new=AsyncMock(return_value=([], dom_hints, None)),
    ), patch(
        "ma_poc.services.llm_extractor.extract_with_llm",
        new=AsyncMock(return_value=([], mono_hints, "raw", None)),
    ):
        adapter = GenericAdapter()
        ctx = _make_ctx(api_responses=api_responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]

    prop_amen = getattr(result, "_property_amenities", [])
    assert sorted(prop_amen) == sorted(
        ["pool", "gym", "dog park", "clubhouse", "pet friendly"]
    )


@pytest.mark.asyncio
async def test_navigation_hints_dedup_across_tiers() -> None:
    """When two tiers volunteer the same /floorplans hint, it's captured once.

    RC3 (defer-monolithic rule) defers the monolithic LLM fallback to the
    link-hop pass when nav hints are already present — so the monolithic tier
    does not run during the main extraction pass and its /availability hint
    never accumulates here.  The dedup invariant is verified using the two
    tiers (API-LLM and DOM-LLM) that DO run."""
    api_hints = {"navigation_hint": "/floorplans"}
    dom_hints = {"navigation_hint": "/floorplans"}
    api_responses = [
        {"url": "https://example.com/api/x", "body": _unit_shaped_api_body()}
    ]

    with _suppress_deterministic_tiers(), patch(
        "ma_poc.services.llm_extractor.analyze_api_with_llm",
        new=AsyncMock(return_value=([], api_hints, True, None)),
    ), patch(
        "ma_poc.services.llm_extractor.analyze_dom_with_llm",
        new=AsyncMock(return_value=([], dom_hints, None)),
    ), patch(
        "ma_poc.services.llm_extractor.extract_with_llm",
        new=AsyncMock(return_value=([], {"navigation_hint": "/availability"}, "raw", None)),
    ):
        adapter = GenericAdapter()
        ctx = _make_ctx(api_responses=api_responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]

    nav_hints = getattr(result, "_llm_navigation_hints", [])
    # API-LLM and DOM-LLM both emit /floorplans — dedup keeps exactly one.
    assert nav_hints.count("/floorplans") == 1
    # RC3 defers monolithic to hop when nav hints already present, so
    # /availability from the mono tier is not collected in this pass.
    assert "/availability" not in nav_hints


# ── Scraper surface step ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_property_amenities_surfaced_to_result_dict() -> None:
    """``adapter_result._property_amenities`` set by the adapter must
    flow to ``result["property_amenities"]`` via the scraper's surface
    block — that's the key the existing aggregator at
    ``aggregate_property_amenities`` reads as ``explicit``."""
    from types import SimpleNamespace

    from ma_poc.pms.scraper import scrape

    dom_units = [_llm_unit()]
    dom_hints = {
        "css_selectors": {"container": ".unit-card"},
        "property_amenities": ["pool", "gym"],
    }

    fetch_result = SimpleNamespace(
        body=_passing_html().encode("utf-8"),
        outcome=SimpleNamespace(value="OK"),
    )

    with _suppress_deterministic_tiers(), patch(
        "ma_poc.services.llm_extractor.analyze_dom_with_llm",
        new=AsyncMock(return_value=(dom_units, dom_hints, None)),
    ), patch(
        "ma_poc.services.llm_extractor.analyze_api_with_llm",
        new=AsyncMock(return_value=([], None, False, None)),
    ), patch(
        "ma_poc.pms.adapters.generic.extract_with_hints",
        return_value=dom_units,
    ):
        result = await scrape(
            base_url="https://example.com/",
            fetch_result=fetch_result,
            property_id="TEST",
        )

    assert "property_amenities" in result, f"keys: {list(result.keys())}"
    assert "pool" in result["property_amenities"]
    assert "gym" in result["property_amenities"]
