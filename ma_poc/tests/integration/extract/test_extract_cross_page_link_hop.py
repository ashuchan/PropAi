"""Phase 9 — cross-page merge in scraper.py::_try_link_hop.

Covers:
  - H5: visited_urls dedupes; max_hops bounded recursion
  - cross-page merge path (future-active when main has units AND sub-page hops)
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from ma_poc.pms.detector import DetectedPMS


class _FakeProbeResponse:
    """curl_cffi-response stand-in for the ``_probe`` seam.

    ``_try_link_hop`` gates every candidate URL with a cheap
    ``probe_get`` (``scraper._crawl_get_gate_should_skip``) before the RENDER
    sub-fetch, and the recursive ``scrape()`` curl-refetches sub-paths for its
    detection rescue. A 200 with an empty document is the neutral answer for
    both: the gate only retires ``404``/``410`` + <10 KB bodies, so every
    candidate still reaches the tracking fetcher these tests count — a
    404-shaped stub would silently gate the hops away and make the
    ``max_hops`` cap assertion pass vacuously.
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
    """Keep every ``probe_get`` in this module off the live network.

    Overrides the repo-level guard in ``ma_poc/conftest.py`` for this module
    only.
    """

    def _fake_probe_get(url: str, *args: Any, **kwargs: Any) -> _FakeProbeResponse:
        return _FakeProbeResponse(url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _fake_probe_get)


def _stub_playwright() -> None:
    """Install lightweight playwright stubs so scraper.py can be imported."""
    for name in ("playwright", "playwright.async_api"):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            mod.BrowserContext = object  # type: ignore[attr-defined]
            mod.Page = object  # type: ignore[attr-defined]
            mod.async_playwright = object  # type: ignore[attr-defined]
            sys.modules[name] = mod


def _ranked_links_stub(html: str, entry_url: str, limit: int = 3):
    """Deterministic 3 candidates for testing the bounded loop."""
    return [
        ("https://example.com/floor-plans", 0.9, "Floor Plans"),
        ("https://example.com/availability", 0.85, "Availability"),
        ("https://example.com/apply", 0.5, "Apply"),
    ]


def test_h5_visited_urls_dedupe() -> None:
    """If entry URL is in visited_urls, it never re-fetches itself.
    If a candidate appears in visited_urls, it's skipped."""
    _stub_playwright()
    from ma_poc.pms.scraper import _try_link_hop

    # Build a fake fetch outcome that always returns OK
    @dataclass
    class FakeFetch:
        outcome: str = "OK"
        body: bytes = b"<html></html>"
        elapsed_ms: int = 100

    async def fake_jugnu_fetch(task):
        return FakeFetch()

    async def fake_scrape(**kw):
        return {"units": [], "extraction_tier_used": "TIER_3_DOM"}

    # Pre-mark availability as visited. The explicit floor-plans link remains
    # eligible; universal recovery priors may legitimately use the other
    # bounded hop slots before the low-signal /apply link.
    visited = {"https://example.com/availability", "https://example.com"}

    detected = DetectedPMS(pms="unknown", confidence=0.5)

    fetched_urls = []

    async def tracking_fetch(task):
        fetched_urls.append(task.url)
        return FakeFetch()

    with patch("ma_poc.pms.scraper._rank_internal_links", _ranked_links_stub), \
         patch("ma_poc.fetch.fetch", tracking_fetch), \
         patch("ma_poc.pms.scraper.scrape", fake_scrape):
        asyncio.run(
            _try_link_hop(
                entry_url="https://example.com",
                entry_page_html="<html>...</html>",
                detected=detected,
                profile=None,
                expected_total_units=None,
                property_id="phase9-001",
                csv_row=None,
                max_hops=3,
                visited_urls=visited,
            )
        )

    # /availability was pre-visited → never fetched
    assert "https://example.com/availability" not in fetched_urls
    # entry URL never re-fetched by link-hop
    assert "https://example.com" not in fetched_urls
    # The strongest remaining page-authored candidate is still fetched.
    assert "https://example.com/floor-plans" in fetched_urls
    assert len(fetched_urls) <= 3


def test_h5_max_hops_caps_fetches() -> None:
    """No matter how many candidates the ranker returns, link-hop fetches
    at most `max_hops` of them."""
    _stub_playwright()
    from ma_poc.pms.scraper import _try_link_hop

    @dataclass
    class FakeFetch:
        outcome: str = "BAD"  # forces the loop to not early-return
        body: bytes = b""
        elapsed_ms: int = 50

    fetched_urls = []

    async def tracking_fetch(task):
        fetched_urls.append(task.url)
        return FakeFetch()

    def many_candidates(html, entry_url, limit=3):
        return [(f"https://example.com/p{i}", 0.5, f"p{i}") for i in range(20)]

    detected = DetectedPMS(pms="unknown", confidence=0.5)

    with patch("ma_poc.pms.scraper._rank_internal_links", many_candidates), \
         patch("ma_poc.fetch.fetch", tracking_fetch):
        asyncio.run(
            _try_link_hop(
                entry_url="https://example.com",
                entry_page_html="<html></html>",
                detected=detected,
                profile=None,
                expected_total_units=None,
                property_id="phase9-002",
                csv_row=None,
                max_hops=3,
                visited_urls=None,
            )
        )

    # Hard cap at max_hops=3 regardless of how many ranker offered
    assert len(fetched_urls) <= 3


def test_phase9_visited_urls_default_includes_entry_url() -> None:
    """If visited_urls is None, entry URL still gets added so it can't be
    re-fetched by a candidate that happens to equal it."""
    _stub_playwright()
    from ma_poc.pms.scraper import _try_link_hop

    @dataclass
    class FakeFetch:
        outcome: str = "BAD"
        body: bytes = b""
        elapsed_ms: int = 0

    fetched = []

    async def tracking_fetch(task):
        fetched.append(task.url)
        return FakeFetch()

    def candidates_include_entry(html, entry_url, limit=3):
        return [
            (entry_url, 0.99, "self"),  # adversarial — points back to entry
            ("https://example.com/floor-plans", 0.85, "fp"),
        ]

    detected = DetectedPMS(pms="unknown", confidence=0.5)

    with patch("ma_poc.pms.scraper._rank_internal_links", candidates_include_entry), \
         patch("ma_poc.fetch.fetch", tracking_fetch):
        asyncio.run(
            _try_link_hop(
                entry_url="https://example.com",
                entry_page_html="<html></html>",
                detected=detected,
                profile=None,
                expected_total_units=None,
                property_id="phase9-003",
                csv_row=None,
                max_hops=3,
                visited_urls=None,
            )
        )
    assert "https://example.com" not in fetched


def test_phase9_cross_page_merge_helper_imports_succeed() -> None:
    """Smoke test — the merge imports inside scrape_jugnu work in this env.
    The full E2E scrape_jugnu test requires a real fetch_result fixture."""
    from ma_poc.models.source import (
        ExtractedSource,
        SourceId,
        envelope_hash_of,
        from_legacy_unit,
        to_legacy_unit,
    )
    from ma_poc.services.source_merger import merge_sources

    main_units = [{"unit_number": "U1", "asking_rent": 1500}]
    sub_units = [{"unit_number": "U1", "sqft": 750}]
    main_h = envelope_hash_of(main_units)
    sub_h = envelope_hash_of(sub_units)
    main_src = ExtractedSource(
        source_id=SourceId.API_GENERIC_NARROW,
        source_url="https://example.com",
        envelope_hash=main_h,
        units=[from_legacy_unit(u, SourceId.API_GENERIC_NARROW, "https://example.com", main_h, 0.85) for u in main_units],
        has_unit_ids=True,
        is_floor_plan_level=False,
    )
    sub_src = ExtractedSource(
        source_id=SourceId.API_GENERIC_NARROW,
        source_url="https://example.com/avail",
        envelope_hash=sub_h,
        units=[from_legacy_unit(u, SourceId.API_GENERIC_NARROW, "https://example.com/avail", sub_h, 0.85) for u in sub_units],
        has_unit_ids=True,
        is_floor_plan_level=False,
    )
    merged = merge_sources([main_src, sub_src], "p")
    assert len(merged) == 1
    legacy = [to_legacy_unit(u) for u in merged]
    legacy[0].pop("_provenance", None)
    assert legacy[0]["unit_number"] == "U1"
    assert legacy[0]["asking_rent"] == 1500
    assert legacy[0]["sqft"] == 750
