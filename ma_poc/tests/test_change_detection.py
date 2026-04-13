"""Tests for scraper/change_detection.py — 8+ tests covering all required cases."""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from models.scrape_event import ChangeDetectionResult
from scraper.change_detection import ChangeDetector, PropertyState, StateStore

URL = "https://example.com/property"


@pytest.fixture
def state_store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "cd_state.json")


@pytest.fixture
def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))


@respx.mock
async def test_etag_304_returns_unchanged(state_store: StateStore) -> None:
    respx.head(URL).mock(return_value=httpx.Response(304))
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(404))
    state = PropertyState(last_etag='"abc123"', last_lastmodified="Mon, 01 Apr 2026 00:00:00 GMT",
                          last_full_scrape_date=date.today().isoformat())
    await state_store.put("P1", state)
    async with ChangeDetector(state_store) as cd:
        decision, _ = await cd.evaluate("P1", URL)
    assert decision.etag_result == ChangeDetectionResult.UNCHANGED
    assert decision.skip is True


@respx.mock
async def test_no_etag_or_lastmod_header_inconclusive(state_store: StateStore) -> None:
    respx.head(URL).mock(return_value=httpx.Response(200, headers={}))
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(404))
    state = PropertyState(last_full_scrape_date=date.today().isoformat())
    await state_store.put("P1", state)
    async with ChangeDetector(state_store) as cd:
        decision, _ = await cd.evaluate("P1", URL)
    assert decision.etag_result == ChangeDetectionResult.INCONCLUSIVE


@respx.mock
async def test_sitemap_lastmod_unchanged(state_store: StateStore) -> None:
    respx.head(URL).mock(return_value=httpx.Response(200, headers={}))
    sitemap_body = (
        '<?xml version="1.0"?><urlset>'
        f'<url><loc>{URL}</loc><lastmod>2026-04-01</lastmod></url>'
        '</urlset>'
    )
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(200, text=sitemap_body))
    state = PropertyState(last_sitemap_lastmod="2026-04-01", last_full_scrape_date=date.today().isoformat())
    await state_store.put("P1", state)
    async with ChangeDetector(state_store) as cd:
        decision, _ = await cd.evaluate("P1", URL)
    assert decision.sitemap_result == ChangeDetectionResult.UNCHANGED


@respx.mock
async def test_forced_full_scrape_after_7_days(state_store: StateStore) -> None:
    respx.head(URL).mock(return_value=httpx.Response(304))
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(404))
    old = (date.today() - timedelta(days=8)).isoformat()
    state = PropertyState(last_etag='"x"', last_full_scrape_date=old)
    await state_store.put("P1", state)
    async with ChangeDetector(state_store) as cd:
        decision, _ = await cd.evaluate("P1", URL)
    assert decision.forced_full_scrape is True
    assert decision.skip is False


@respx.mock
async def test_carryforward_days_increments(state_store: StateStore) -> None:
    respx.head(URL).mock(return_value=httpx.Response(304))
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(404))
    state = PropertyState(last_etag='"y"', last_full_scrape_date=date.today().isoformat())
    await state_store.put("P1", state)
    async with ChangeDetector(state_store) as cd:
        _, st = await cd.evaluate("P1", URL)
        await cd.record_skip("P1", st)
        _, st2 = await cd.evaluate("P1", URL)
        await cd.record_skip("P1", st2)
    final = await state_store.get("P1")
    assert final.carryforward_days == 2


@respx.mock
async def test_all_unchanged_triggers_skip(state_store: StateStore) -> None:
    respx.head(URL).mock(return_value=httpx.Response(304))
    sitemap_body = (
        '<?xml version="1.0"?><urlset>'
        f'<url><loc>{URL}</loc><lastmod>2026-04-01</lastmod></url>'
        '</urlset>'
    )
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(200, text=sitemap_body))
    state = PropertyState(last_etag='"e"', last_sitemap_lastmod="2026-04-01",
                          last_full_scrape_date=date.today().isoformat())
    await state_store.put("P1", state)
    async with ChangeDetector(state_store) as cd:
        decision, _ = await cd.evaluate("P1", URL)
    assert decision.skip is True
    assert decision.overall == ChangeDetectionResult.UNCHANGED


@respx.mock
async def test_any_changed_triggers_full_scrape(state_store: StateStore) -> None:
    respx.head(URL).mock(return_value=httpx.Response(200, headers={"etag": '"new"'}))
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(404))
    state = PropertyState(last_etag='"old"', last_full_scrape_date=date.today().isoformat())
    await state_store.put("P1", state)
    async with ChangeDetector(state_store) as cd:
        decision, _ = await cd.evaluate("P1", URL)
    assert decision.etag_result == ChangeDetectionResult.CHANGED
    assert decision.skip is False
    assert decision.overall == ChangeDetectionResult.CHANGED


async def test_state_file_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    s1 = StateStore(path)
    await s1.put("P1", PropertyState(last_etag='"abc"', carryforward_days=3))
    s2 = StateStore(path)
    loaded = await s2.get("P1")
    assert loaded.last_etag == '"abc"'
    assert loaded.carryforward_days == 3


async def test_concurrent_writes_do_not_corrupt(tmp_path: Path) -> None:
    """Bug-hunt #4: asyncio.Lock prevents torn writes."""
    path = tmp_path / "state.json"
    store = StateStore(path)

    async def writer(i: int) -> None:
        await store.put(f"P{i}", PropertyState(last_etag=f'"{i}"', carryforward_days=i))

    await asyncio.gather(*[writer(i) for i in range(20)])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data) == 20
    for i in range(20):
        assert data[f"P{i}"]["carryforward_days"] == i
