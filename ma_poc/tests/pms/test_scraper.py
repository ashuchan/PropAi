"""Phase 5 — scraper orchestrator tests.

Uses mock pages and monkeypatched adapters to verify the detect -> resolve -> adapt
pipeline without requiring Playwright or real network access.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pms.adapters.base import AdapterContext, AdapterResult
from pms.detector import DetectedPMS
from pms.resolver import ResolvedTarget
from pms.scraper import scrape


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
        patch("pms.scraper.detect_pms", return_value=_make_detection("entrata")),
        patch("pms.scraper.resolve_target", return_value=_make_resolved(pms="entrata")),
        patch("pms.scraper.get_adapter", return_value=mock_adapter),
    ):
        result = await scrape("http://example.com/", page=page)

    assert result["units"] == expected_units
    assert result["_adapter_used"] == "entrata"
    assert "entrata" in result["_fallback_chain"]
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
    generic_adapter.extract = AsyncMock(
        return_value=_make_adapter_result(units=fallback_units)
    )

    call_count = 0

    def _get_adapter(pms: str) -> AsyncMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return pms_adapter
        return generic_adapter

    with (
        patch("pms.scraper.detect_pms", return_value=_make_detection("rentcafe")),
        patch("pms.scraper.resolve_target", return_value=_make_resolved(pms="rentcafe")),
        patch("pms.scraper.get_adapter", side_effect=_get_adapter),
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
    original_extract = mock_adapter.extract

    async def _capture_extract(p: object, ctx: AdapterContext) -> AdapterResult:
        captured_ctx.append(ctx)
        return _make_adapter_result(units=units)

    mock_adapter.extract = AsyncMock(side_effect=_capture_extract)

    with (
        patch("pms.scraper.detect_pms", return_value=_make_detection("unknown", 0.0)),
        patch("pms.scraper.resolve_target", return_value=_make_resolved(pms="unknown")),
        patch("pms.scraper.get_adapter", return_value=mock_adapter),
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
        patch("pms.scraper.detect_pms", return_value=_make_detection("entrata")),
        patch("pms.scraper.resolve_target", return_value=_make_resolved(pms="entrata")),
        patch("pms.scraper.get_adapter", side_effect=_get_adapter),
    ):
        result = await scrape("https://example.com/", page=page)

    # Generic got the original 'entrata' detection, so it knows to skip LLM
    assert len(captured_ctx) == 1
    assert captured_ctx[0].detected.pms == "entrata"
    assert result["_fallback_chain"] == ["entrata", "generic"]


@pytest.mark.asyncio
async def test_orchestrator_skips_everything_on_ssl_error() -> None:
    """SSL error during page.content() -> return FAILED_UNREACHABLE immediately."""
    page = _make_page(
        content_raises=Exception("net::ERR_SSL_PROTOCOL_ERROR at https://example.com/")
    )

    with patch("pms.scraper.detect_pms", return_value=_make_detection("unknown", 0.0)):
        result = await scrape("https://bad-ssl.example.com/", page=page)

    assert any("FAILED_UNREACHABLE" in e for e in result["errors"])
    assert result["units"] == []


@pytest.mark.asyncio
async def test_orchestrator_skips_everything_on_dns_error() -> None:
    """DNS resolution failure -> return FAILED_UNREACHABLE immediately."""
    page = _make_page(
        content_raises=Exception("net::ERR_NAME_NOT_RESOLVED")
    )

    with patch("pms.scraper.detect_pms", return_value=_make_detection("unknown", 0.0)):
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
        patch("pms.scraper.detect_pms", return_value=_make_detection("unknown", 0.0)),
        patch("pms.scraper.resolve_target", return_value=resolved),
        patch("pms.scraper.get_adapter", return_value=mock_adapter),
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
        patch("pms.scraper.detect_pms", return_value=_make_detection("unknown", 0.0)),
        patch("pms.scraper.resolve_target", return_value=_make_resolved(pms="unknown")),
        patch("pms.scraper.get_adapter", return_value=mock_adapter),
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
    assert expected_keys.issubset(set(result.keys())), (
        f"Missing keys: {expected_keys - set(result.keys())}"
    )


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
        patch("pms.scraper.detect_pms", return_value=_make_detection("appfolio", 0.90)),
        patch("pms.scraper.resolve_target", return_value=_make_resolved(pms="appfolio")),
        patch("pms.scraper.get_adapter", return_value=mock_adapter),
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
