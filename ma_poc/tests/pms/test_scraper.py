"""Phase 5 — scraper orchestrator tests.

Uses mock pages and monkeypatched adapters to verify the detect -> resolve -> adapt
pipeline without requiring Playwright or real network access.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.detector import DetectedPMS
from ma_poc.pms.resolver import ResolvedTarget
from ma_poc.pms.scraper import (
    backfill_sqft_from_public_plan_context,
    promote_verified_unit_rows,
    scrape,
)

# ---------------------------------------------------------------------------
# Network seam — see ma_poc/conftest.py
# ---------------------------------------------------------------------------
# scrape() reaches the internet through ``_probe.probe_get`` (a sync curl_cffi
# call) in the Step-4b detection rescue: whenever detection is unknown/custom it
# re-fetches ``/``, ``/floorplans/``, ``/floor-plans/`` … looking for a PMS
# marker the rendered HTML hid. Patching detect_pms / resolve_target /
# get_adapter does not stop that fetch, so these tests were silently hitting
# example.com. Every test here asserts on the *orchestrator* wiring, never on a
# rescued detection, so the seam is stubbed with an inert 200 page carrying no
# PMS marker — the same "nothing useful came back" outcome the live fetch of
# example.com produced, minus the packets.

_INERT_HTML = (
    "<html><head><title>Test page</title></head>"
    "<body><p>No PMS markers, no floor plans, no rents.</p></body></html>"
)


class _InertProbeResponse:
    """Minimal curl_cffi-response stand-in (``.status_code/.text/.content``).

    Mirrors the attribute surface ``ma_poc/pms/scraper.py`` reads off a
    ``probe_get`` result. Deliberately boring content so no extraction,
    detection or enrichment path can latch onto it.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.status_code = 200
        self.text = _INERT_HTML
        self.content = _INERT_HTML.encode("utf-8")
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"

    def json(self) -> Any:
        """Match curl_cffi/requests semantics for a non-JSON body."""
        raise ValueError("inert probe response is not JSON")


@pytest.fixture(autouse=True)
def _stub_probe_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve every ``probe_get`` in this module an inert local page."""

    def _fake_probe_get(url: str, **_kw: Any) -> _InertProbeResponse:
        return _InertProbeResponse(url)

    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get", _fake_probe_get, raising=True
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_page(
    *,
    url: str = "https://example.com/",
    content: str = "<html></html>",
    content_raises: Exception | None = None,
) -> AsyncMock:
    """Create a mock page with controllable .url and .content()."""
    page = AsyncMock()
    page.url = url
    if content_raises:
        page.content = AsyncMock(side_effect=content_raises)
    else:
        page.content = AsyncMock(return_value=content)

    # resolve_target calls page.evaluate; default to empty results
    async def _evaluate(script: str) -> list:
        return []

    page.evaluate = AsyncMock(side_effect=_evaluate)
    return page


def _make_detection(pms: str = "entrata", confidence: float = 0.90) -> DetectedPMS:
    return DetectedPMS(
        pms=pms,  # type: ignore[arg-type]
        confidence=confidence,
        evidence=["test"],
        recommended_strategy="api_first",
    )


def _make_resolved(
    url: str = "https://example.com/",
    pms: str = "entrata",
    method: str = "no_hop",
) -> ResolvedTarget:
    return ResolvedTarget(
        original_url=url,
        resolved_url=url,
        hop_path=[url],
        final_detection=_make_detection(pms),
        method=method,  # type: ignore[arg-type]
    )


def _make_adapter_result(
    units: list | None = None,
    tier: str = "TIER_1_API",
    errors: list | None = None,
) -> AdapterResult:
    return AdapterResult(
        units=units or [],
        tier_used=tier,
        errors=errors or [],
        confidence=0.85 if units else 0.0,
    )


def test_promote_verified_unit_rows_keeps_only_native_id_plus_rent() -> None:
    """A stale plan tier cannot bury real units or promote a plan surrogate."""
    result = AdapterResult(
        tier_used="TIER_1_API_ENTRATA_PLAN_LEVEL",
        units=[
            {
                "unit_number": "5216",
                "asking_rent": 904,
                "beds": 1,
                "baths": 1,
                "floor_plan_name": "A1",
                "floor_plan_id": "a1-plan",
                "is_floor_plan_level": True,
                "data_quality_flag": "PLAN_LEVEL_NO_UNIT_ANCHOR",
                "extraction_tier": "TIER_1_API_ENTRATA_PLAN_LEVEL",
            },
            {
                # Looks priced, but its supposed id is the floor-plan id.
                "unit_id": "b2-plan",
                "floor_plan_id": "b2-plan",
                "source_ids": {"entrata_floor_plan_id": "b2-plan"},
                "floor_plan_name": "B2",
                "asking_rent": 1200,
                "beds": 2,
                "baths": 2,
            },
        ],
    )

    promoted = promote_verified_unit_rows(result, property_id="27790")

    assert promoted == 1
    assert result.tier_used == "TIER_1_API_ENTRATA"
    assert [row["unit_number"] for row in result.units] == ["5216"]
    assert result.units[0]["is_floor_plan_level"] is False
    assert "PLAN_LEVEL" not in str(result.units[0]["data_quality_flag"])
    assert len(result.plan_summaries) == 1
    assert result.plan_summaries[0]["unit_id"] == "b2-plan"


def test_promotion_keeps_partial_units_and_routes_unanchored_rows() -> None:
    """Missing price/sqft stays unit-level; an inferred plan card does not."""
    result = AdapterResult(
        tier_used="TIER_1_API",
        units=[
            {
                "unit_number": "201",
                "floor_plan_name": "A1",
                "area": 700,
                "beds": 1,
                "baths": 1,
            },
            {
                "unit_number": "202",
                "floor_plan_name": "A1",
                "asking_rent": 1500,
                "area": -1,
                "beds": 1,
                "baths": 1,
            },
            {
                "floor_plan_name": "B1",
                "asking_rent": 1450,
                "area": 700,
                "beds": 1,
                "baths": 1,
            },
        ],
    )

    promoted = promote_verified_unit_rows(result, property_id="partial-test")

    assert promoted == 2
    assert [row["unit_number"] for row in result.units] == ["201", "202"]
    assert "UNIT_LEVEL_PRICING_MISSING" in result.units[0]["data_quality_flag"]
    assert "UNIT_LEVEL_PARTIAL_MISSING_SQFT" in result.units[1]["data_quality_flag"]
    assert len(result.plan_summaries) == 1
    assert "UNIT_ROUTE_UNVERIFIED" in result.plan_summaries[0]["data_quality_flag"]


def test_public_plan_sqft_backfill_requires_exact_plan_scalar() -> None:
    """Plan-page sqft fills a matching real unit, never a range/mismatch."""
    result = AdapterResult(
        units=[
            {
                "unit_number": "08-B",
                "floor_plan_name": "One Bed One Bath Garden",
                "asking_rent": 1199,
                "sqft": "",
            },
            {
                # A real unit with an unrelated plan must not borrow A1's
                # area merely because both appear in the same response.
                "unit_number": "09-C",
                "floor_plan_name": "Two Bed Townhome",
                "asking_rent": 1500,
                "area": -1,
            },
            {
                # Public plan context — no unit anchor, exact scalar sqft.
                "floor_plan_name": "one-bed one-bath garden",
                "sqft": "763 sq ft",
                "asking_rent": 1199,
            },
            {
                # A range is deliberately unusable even for an exact name.
                "floor_plan_name": "Two Bed Townhome",
                "sqft": "900 - 1,050 sq ft",
                "asking_rent": 1500,
            },
        ]
    )

    assert backfill_sqft_from_public_plan_context(result) == 1
    assert result.units[0]["sqft"] == "763"
    assert result.units[0]["_sqft_backfill_source"] == "public_plan_context_exact"
    assert result.units[1]["area"] == -1


def test_public_plan_sqft_backfill_rejects_conflicting_exact_context() -> None:
    """Conflicting plan metadata remains missing rather than guessed."""
    result = AdapterResult(
        units=[
            {
                "unit_number": "101",
                "floor_plan_name": "A1",
                "asking_rent": 1200,
                "area": -1,
            },
            {"floor_plan_name": "A1", "sqft": "700"},
            {"floor_plan_name": "A1", "sqft": "750"},
        ]
    )

    assert backfill_sqft_from_public_plan_context(result) == 0
    assert result.units[0]["area"] == -1


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_detects_then_calls_correct_adapter() -> None:
    """Detection identifies entrata -> entrata adapter.extract() is called."""
    page = _make_page(content="<html>entrata.com widget</html>")
    expected_units = [{"unit_number": "101", "asking_rent": "1500"}]

    mock_adapter = AsyncMock()
    mock_adapter.pms_name = "entrata"
    mock_adapter.extract = AsyncMock(return_value=_make_adapter_result(units=expected_units))

    with (
        patch("ma_poc.pms.scraper.detect_pms", return_value=_make_detection("entrata")),
        patch("ma_poc.pms.scraper.resolve_target", return_value=_make_resolved(pms="entrata")),
        patch("ma_poc.pms.scraper.get_adapter", return_value=mock_adapter),
    ):
        result = await scrape("http://example.com/", page=page)

    assert result["units"] == expected_units
    assert result["_adapter_used"] == "entrata"
    assert "entrata" in result["_fallback_chain"]
    mock_adapter.extract.assert_awaited_once()


@pytest.mark.asyncio
async def test_dom_first_adapter_is_not_demoted_by_api_envelope_confirmation() -> None:
    """A public DOM roster must run even when unrelated network bodies exist.

    RentVision sites expose units on per-plan SSR detail pages.  Cypress
    Grove and Loch Raven also load third-party widgets, so using those widget
    responses as an API-envelope veto used to force the generic adapter and
    lose their real unit rows.
    """
    page = _make_page(content="<html>Website created by RentVision</html>")
    rentvision = DetectedPMS(
        pms="rentvision",
        confidence=0.85,
        evidence=["RentVision marker"],
        recommended_strategy="dom_first",
    )
    resolved = ResolvedTarget(
        original_url="https://example.com/",
        resolved_url="https://example.com/",
        hop_path=["https://example.com/"],
        final_detection=rentvision,
        method="no_hop",
    )
    expected_units = [{"unit_number": "24-B", "asking_rent": "1500"}]
    mock_adapter = AsyncMock()
    mock_adapter.pms_name = "rentvision"
    mock_adapter.extract = AsyncMock(
        return_value=_make_adapter_result(units=expected_units, tier="TIER_3_DOM_RENTVISION")
    )

    with (
        patch("ma_poc.pms.scraper.detect_pms", return_value=rentvision),
        patch("ma_poc.pms.scraper.resolve_target", return_value=resolved),
        patch("ma_poc.pms.scraper.get_adapter", return_value=mock_adapter),
        patch(
            "ma_poc.pms.scraper.confirm_detection",
            side_effect=AssertionError("DOM-first adapter must not be API-confirmed"),
        ),
    ):
        result = await scrape(
            "https://example.com/",
            page=page,
            api_responses=[{"url": "https://widgets.example/x", "body": {"noise": 1}}],
        )

    assert result["units"] == expected_units
    assert result["_adapter_used"] == "rentvision"
    assert result["_detection_confirmed"]["confirmed"] is True
    assert result["_detection_confirmed"]["evidence"][-1] == (
        "api-envelope-confirmation-skipped:dom_first"
    )
    mock_adapter.extract.assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_falls_through_to_generic_when_adapter_empty() -> None:
    """When the PMS adapter returns no units, orchestrator falls through to generic."""
    page = _make_page()
    fallback_units = [{"unit_number": "201", "asking_rent": "1200"}]

    pms_adapter = AsyncMock()
    pms_adapter.pms_name = "rentcafe"
    pms_adapter.extract = AsyncMock(return_value=_make_adapter_result(units=[]))

    generic_adapter = AsyncMock()
    generic_adapter.pms_name = "generic"
    generic_adapter.extract = AsyncMock(return_value=_make_adapter_result(units=fallback_units))

    call_count = 0

    def _get_adapter(pms: str) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return pms_adapter
        return generic_adapter

    with (
        patch("ma_poc.pms.scraper.detect_pms", return_value=_make_detection("rentcafe")),
        patch("ma_poc.pms.scraper.resolve_target", return_value=_make_resolved(pms="rentcafe")),
        patch("ma_poc.pms.scraper.get_adapter", side_effect=_get_adapter),
        # Change 2 introduces confirm_detection between adapter selection
        # and adapter.extract(); with no api_responses in this test the
        # router would demote rentcafe→unknown, which would rewrite the
        # fallback chain the test asserts on. Patch the call to a
        # passthrough so the test exercises the *adapter-empty → generic*
        # fallback path it was written for.
        patch(
            "ma_poc.pms.scraper.confirm_detection",
            side_effect=lambda det, _responses: det,
        ),
    ):
        result = await scrape("https://example.com/", page=page)

    assert result["units"] == fallback_units
    assert result["_adapter_used"] == "generic"
    assert result["_fallback_chain"] == ["rentcafe", "generic"]


@pytest.mark.asyncio
async def test_orchestrator_runs_llm_only_for_unknown_pms() -> None:
    """When pms is 'unknown', generic adapter gets ctx with pms='unknown'
    so it knows LLM is allowed."""
    page = _make_page()
    units = [{"unit_number": "301"}]

    mock_adapter = AsyncMock()
    mock_adapter.pms_name = "generic"
    mock_adapter.extract = AsyncMock(return_value=_make_adapter_result(units=units))

    captured_ctx: list[AdapterContext] = []

    async def _capture_extract(p: object, ctx: AdapterContext) -> AdapterResult:
        captured_ctx.append(ctx)
        return _make_adapter_result(units=units)

    mock_adapter.extract = AsyncMock(side_effect=_capture_extract)

    with (
        patch("ma_poc.pms.scraper.detect_pms", return_value=_make_detection("unknown", 0.0)),
        patch("ma_poc.pms.scraper.resolve_target", return_value=_make_resolved(pms="unknown")),
        patch("ma_poc.pms.scraper.get_adapter", return_value=mock_adapter),
    ):
        result = await scrape("https://mystery-site.com/", page=page)

    assert result["units"] == units
    # The context passed to generic should have pms="unknown", meaning LLM is allowed
    assert len(captured_ctx) == 1
    assert captured_ctx[0].detected.pms == "unknown"


@pytest.mark.asyncio
async def test_orchestrator_never_runs_llm_for_detected_pms_failure() -> None:
    """When a known PMS adapter fails, generic fallback gets the original PMS
    in its context, so it skips LLM."""
    page = _make_page()

    pms_adapter = AsyncMock()
    pms_adapter.pms_name = "entrata"
    pms_adapter.extract = AsyncMock(return_value=_make_adapter_result(units=[]))

    captured_ctx: list[AdapterContext] = []

    async def _capture_generic(p: object, ctx: AdapterContext) -> AdapterResult:
        captured_ctx.append(ctx)
        return _make_adapter_result(units=[])

    generic_adapter = AsyncMock()
    generic_adapter.pms_name = "generic"
    generic_adapter.extract = AsyncMock(side_effect=_capture_generic)

    call_count = 0

    def _get_adapter(pms: str) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return pms_adapter
        return generic_adapter

    with (
        patch("ma_poc.pms.scraper.detect_pms", return_value=_make_detection("entrata")),
        patch("ma_poc.pms.scraper.resolve_target", return_value=_make_resolved(pms="entrata")),
        patch("ma_poc.pms.scraper.get_adapter", side_effect=_get_adapter),
    ):
        result = await scrape("https://example.com/", page=page)

    # Generic got the original 'entrata' detection, so it knows to skip LLM
    assert len(captured_ctx) == 1
    assert captured_ctx[0].detected.pms == "entrata"
    assert result["_fallback_chain"] == ["entrata", "generic"]


@pytest.mark.asyncio
async def test_orchestrator_skips_everything_on_ssl_error() -> None:
    """SSL error during page.content() -> return FAILED_UNREACHABLE immediately."""
    page = _make_page(content_raises=Exception("net::ERR_SSL_PROTOCOL_ERROR at https://example.com/"))

    with patch("ma_poc.pms.scraper.detect_pms", return_value=_make_detection("unknown", 0.0)):
        result = await scrape("https://bad-ssl.example.com/", page=page)

    assert any("FAILED_UNREACHABLE" in e for e in result["errors"])
    assert result["units"] == []


@pytest.mark.asyncio
async def test_orchestrator_skips_everything_on_dns_error() -> None:
    """DNS resolution failure -> return FAILED_UNREACHABLE immediately."""
    page = _make_page(content_raises=Exception("net::ERR_NAME_NOT_RESOLVED"))

    with patch("ma_poc.pms.scraper.detect_pms", return_value=_make_detection("unknown", 0.0)):
        result = await scrape("https://nonexistent.example.com/", page=page)

    assert any("FAILED_UNREACHABLE" in e for e in result["errors"])
    assert result["units"] == []


@pytest.mark.asyncio
async def test_orchestrator_hop_to_pms_subdomain() -> None:
    """Resolver hops from vanity domain to PMS subdomain -> adapter uses resolved URL."""
    page = _make_page(url="https://vanity.example.com/")
    units = [{"unit_number": "401"}]

    pms_url = "https://8756399.onlineleasing.realpage.com/"

    mock_adapter = AsyncMock()
    mock_adapter.pms_name = "onesite"

    captured_ctx: list[AdapterContext] = []

    async def _capture_extract(p: object, ctx: AdapterContext) -> AdapterResult:
        captured_ctx.append(ctx)
        return _make_adapter_result(units=units)

    mock_adapter.extract = AsyncMock(side_effect=_capture_extract)

    resolved = ResolvedTarget(
        original_url="https://vanity.example.com/",
        resolved_url=pms_url,
        hop_path=["https://vanity.example.com/", pms_url],
        final_detection=_make_detection("onesite", 0.95),
        method="cta_link",
    )

    with (
        patch("ma_poc.pms.scraper.detect_pms", return_value=_make_detection("unknown", 0.0)),
        patch("ma_poc.pms.scraper.resolve_target", return_value=resolved),
        patch("ma_poc.pms.scraper.get_adapter", return_value=mock_adapter),
    ):
        result = await scrape("https://vanity.example.com/", page=page)

    # Adapter should receive the resolved PMS URL, not the vanity URL
    assert len(captured_ctx) == 1
    assert captured_ctx[0].base_url == pms_url
    assert result["units"] == units
    assert result["_resolved_target"]["method"] == "cta_link"
    assert result["_resolved_target"]["resolved_url"] == pms_url


@pytest.mark.asyncio
async def test_orchestrator_preserves_legacy_result_keys() -> None:
    """All legacy keys must be present in the returned dict."""
    page = _make_page()

    mock_adapter = AsyncMock()
    mock_adapter.pms_name = "generic"
    mock_adapter.extract = AsyncMock(return_value=_make_adapter_result())

    with (
        patch("ma_poc.pms.scraper.detect_pms", return_value=_make_detection("unknown", 0.0)),
        patch("ma_poc.pms.scraper.resolve_target", return_value=_make_resolved(pms="unknown")),
        patch("ma_poc.pms.scraper.get_adapter", return_value=mock_adapter),
    ):
        result = await scrape("https://example.com/", page=page)

    expected_keys = {
        "scraped_at",
        "property_name",
        "base_url",
        "links_found",
        "property_links_crawled",
        "api_calls_intercepted",
        "units",
        "extraction_tier_used",
        "errors",
        "_property_id",
        "_llm_interactions",
        "_detected_pms",
        "_resolved_target",
        "_adapter_used",
        "_fallback_chain",
    }
    assert expected_keys.issubset(set(result.keys())), f"Missing keys: {expected_keys - set(result.keys())}"


@pytest.mark.asyncio
async def test_orchestrator_adds_new_detection_keys() -> None:
    """New keys (_detected_pms, _resolved_target, _adapter_used, _fallback_chain)
    are populated with structured data."""
    page = _make_page()
    units = [{"unit_number": "501"}]

    mock_adapter = AsyncMock()
    mock_adapter.pms_name = "appfolio"
    mock_adapter.extract = AsyncMock(return_value=_make_adapter_result(units=units))

    with (
        patch("ma_poc.pms.scraper.detect_pms", return_value=_make_detection("appfolio", 0.90)),
        patch("ma_poc.pms.scraper.resolve_target", return_value=_make_resolved(pms="appfolio")),
        patch("ma_poc.pms.scraper.get_adapter", return_value=mock_adapter),
    ):
        result = await scrape("https://myplace.appfolio.com/listings", page=page)

    # _detected_pms is a dict with expected keys
    assert isinstance(result["_detected_pms"], dict)
    assert result["_detected_pms"]["pms"] == "appfolio"
    assert result["_detected_pms"]["confidence"] == 0.90

    # _resolved_target is a dict with expected keys
    assert isinstance(result["_resolved_target"], dict)
    assert "resolved_url" in result["_resolved_target"]
    assert "method" in result["_resolved_target"]

    # _adapter_used is a string
    assert result["_adapter_used"] == "appfolio"

    # _fallback_chain is a list
    assert isinstance(result["_fallback_chain"], list)
    assert "appfolio" in result["_fallback_chain"]

    # base_url got normalized to https
    assert result["base_url"].startswith("https://")


# ---------------------------------------------------------------------------
# F0.1 — _refresh_cost_cap_for_hop (link-hop budget refresh)
# ---------------------------------------------------------------------------


def test_refresh_cost_cap_for_hop_grants_bonus_within_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # F0.1: with $1.50 base + $0.50 bonus, a budget at $1.50 should rise
    # to $2.00 after one hop refresh.
    from ma_poc.pms.scraper import _refresh_cost_cap_for_hop

    monkeypatch.setenv("PROPERTY_LLM_COST_CAP_USD", "1.50")
    monkeypatch.setenv("PROPERTY_LLM_COST_CAP_HOP_BONUS_USD", "0.50")
    budget: dict = {"_cost_cap_usd": 1.50}
    _refresh_cost_cap_for_hop(budget)
    assert budget["_cost_cap_usd"] == pytest.approx(2.00)


def test_refresh_cost_cap_for_hop_respects_3x_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # F0.1: the helper must clamp at base × 3 even when the budget dict
    # already carries a cap above the ceiling. Belt-and-suspenders against
    # a misconfigured PROPERTY_LLM_COST_CAP_HOP_BONUS_USD.
    from ma_poc.pms.scraper import _refresh_cost_cap_for_hop

    monkeypatch.setenv("PROPERTY_LLM_COST_CAP_USD", "1.50")
    monkeypatch.setenv("PROPERTY_LLM_COST_CAP_HOP_BONUS_USD", "10.00")
    budget: dict = {"_cost_cap_usd": 4.00}
    _refresh_cost_cap_for_hop(budget)
    # base 1.50 × 3 = 4.50 ceiling — even huge bonus cannot exceed.
    assert budget["_cost_cap_usd"] == pytest.approx(4.50)


def test_refresh_cost_cap_for_hop_no_op_at_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # F0.1: idempotent — once the cap reaches the ceiling, subsequent
    # refreshes are no-ops (not "set back down" or "rolled over").
    from ma_poc.pms.scraper import _refresh_cost_cap_for_hop

    monkeypatch.setenv("PROPERTY_LLM_COST_CAP_USD", "1.50")
    monkeypatch.setenv("PROPERTY_LLM_COST_CAP_HOP_BONUS_USD", "0.50")
    budget: dict = {"_cost_cap_usd": 4.50}  # already at ceiling
    _refresh_cost_cap_for_hop(budget)
    assert budget["_cost_cap_usd"] == pytest.approx(4.50)


def test_refresh_cost_cap_for_hop_handles_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # F0.1: if the budget dict has no _cost_cap_usd key (legacy callers),
    # the helper should treat it as starting from the env-driven base.
    from ma_poc.pms.scraper import _refresh_cost_cap_for_hop

    monkeypatch.setenv("PROPERTY_LLM_COST_CAP_USD", "1.50")
    monkeypatch.setenv("PROPERTY_LLM_COST_CAP_HOP_BONUS_USD", "0.50")
    budget: dict = {"llm_api_calls": 3}  # no _cost_cap_usd
    _refresh_cost_cap_for_hop(budget)
    assert budget["_cost_cap_usd"] == pytest.approx(2.00)


def test_refresh_cost_cap_for_hop_handles_malformed_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # F0.1: a non-numeric stored cap (defensive — shouldn't happen but
    # the dict is mutated in place from many sites) must not crash.
    from ma_poc.pms.scraper import _refresh_cost_cap_for_hop

    monkeypatch.setenv("PROPERTY_LLM_COST_CAP_USD", "1.50")
    monkeypatch.setenv("PROPERTY_LLM_COST_CAP_HOP_BONUS_USD", "0.50")
    budget: dict = {"_cost_cap_usd": "not-a-number"}
    _refresh_cost_cap_for_hop(budget)
    # Falls back to base 1.50 + bonus 0.50 = 2.00.
    assert budget["_cost_cap_usd"] == pytest.approx(2.00)


# ---------------------------------------------------------------------------
# Bug 5 alignment (2026-05-09) — _link_hop_is_rich richness predicate
# ---------------------------------------------------------------------------


class _StubFetch:
    """Minimal stand-in for FetchResult — only the body field is read."""

    def __init__(self, body: bytes | str | None) -> None:
        self.body = body


def test_bug5_rich_hop_requires_min_body_size() -> None:
    """A 49KB body never qualifies as rich, even with rent tokens."""
    from ma_poc.pms.scraper import _link_hop_is_rich

    body = ("$1500 " * 200).encode("utf-8")  # plenty of $ tokens but small
    assert len(body) < 50_000
    assert _link_hop_is_rich(_StubFetch(body)) is False


def test_bug5_rich_hop_jsonld_marker_qualifies() -> None:
    """Body ≥50KB AND containing 'FloorPlan' qualifies as rich."""
    from ma_poc.pms.scraper import _link_hop_is_rich

    payload = '{"@type":"FloorPlan","name":"A"}' + ("x" * 60_000)
    assert _link_hop_is_rich(_StubFetch(payload.encode("utf-8"))) is True


def test_bug5_rich_hop_apartment_complex_marker_qualifies() -> None:
    from ma_poc.pms.scraper import _link_hop_is_rich

    payload = '{"@type":"ApartmentComplex"}' + ("x" * 60_000)
    assert _link_hop_is_rich(_StubFetch(payload.encode("utf-8"))) is True


def test_bug5_rich_hop_rent_tokens_qualify_at_threshold() -> None:
    """≥5 distinct $XXX tokens AND body ≥50KB qualifies."""
    from ma_poc.pms.scraper import _link_hop_is_rich

    payload = (
        "$1200 $1300 $1400 $1500 $1600 $1700 "
        + ("noise " * 12_000)  # padding to push >50KB
    )
    assert len(payload) > 50_000
    assert _link_hop_is_rich(_StubFetch(payload.encode("utf-8"))) is True


def test_bug5_rich_hop_below_rent_threshold_with_no_jsonld_is_not_rich() -> None:
    """Big body with NO content markers — pure text-of-disclaimers — is not
    rich. Filters out cookie banners / ToS pages with large bodies."""
    from ma_poc.pms.scraper import _link_hop_is_rich

    payload = "blah " * 20_000  # ~100KB but no $rent and no JSON-LD markers
    assert _link_hop_is_rich(_StubFetch(payload.encode("utf-8"))) is False


def test_bug5_rich_hop_handles_none_body() -> None:
    from ma_poc.pms.scraper import _link_hop_is_rich

    assert _link_hop_is_rich(_StubFetch(None)) is False
    assert _link_hop_is_rich(None) is False


def test_bug5_rich_hop_handles_str_body() -> None:
    """Body comes in as str on some code paths — equivalent treatment."""
    from ma_poc.pms.scraper import _link_hop_is_rich

    payload = '{"@type":"FloorPlan"}' + ("x" * 60_000)
    assert _link_hop_is_rich(_StubFetch(payload)) is True
