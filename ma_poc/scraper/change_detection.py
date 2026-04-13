"""
Change-detection gate. Runs BEFORE Playwright launches.

Acceptance criteria (CLAUDE.md PR-02):
- Three independent mechanisms: ETag/Last-Modified, sitemap lastmod, API hash
- Skip ONLY when ALL available mechanisms return UNCHANGED
- Override skip if days_since_full_scrape >= 7 (forced full scrape)
- State tracked per property in data/change_detection_state.json
- All state file writes protected by asyncio.Lock (no torn writes from concurrent workers)
- carryforward_days increments on skip; resets to 0 on fresh scrape success
- NEVER use Playwright for change checks — defeats the purpose of the gate
- Bug-hunt #8: send BOTH If-None-Match AND If-Modified-Since headers
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from models.scrape_event import ChangeDetectionResult


@dataclass
class PropertyState:
    last_etag: str | None = None
    last_lastmodified: str | None = None
    last_sitemap_lastmod: str | None = None
    last_api_hash: str | None = None
    last_full_scrape_date: str | None = None  # ISO date
    carryforward_days: int = 0


@dataclass
class ChangeDetectionDecision:
    etag_result: ChangeDetectionResult
    sitemap_result: ChangeDetectionResult
    api_result: ChangeDetectionResult
    skip: bool
    forced_full_scrape: bool
    notes: list[str] = field(default_factory=list)

    @property
    def overall(self) -> ChangeDetectionResult:
        if self.skip:
            return ChangeDetectionResult.UNCHANGED
        if any(r == ChangeDetectionResult.CHANGED for r in self._all):
            return ChangeDetectionResult.CHANGED
        return ChangeDetectionResult.INCONCLUSIVE

    @property
    def _all(self) -> tuple[ChangeDetectionResult, ChangeDetectionResult, ChangeDetectionResult]:
        return (self.etag_result, self.sitemap_result, self.api_result)


class StateStore:
    """Async-safe JSON state file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._cache: dict[str, dict[str, Any]] | None = None

    async def _load(self) -> dict[str, dict[str, Any]]:
        if self._cache is not None:
            return self._cache
        if not self.path.exists():
            self._cache = {}
            return self._cache
        try:
            text = self.path.read_text(encoding="utf-8")
            self._cache = json.loads(text) if text.strip() else {}
        except (OSError, json.JSONDecodeError):
            self._cache = {}
        return self._cache

    async def get(self, property_id: str) -> PropertyState:
        async with self._lock:
            data = await self._load()
            raw = data.get(property_id, {})
            fields = {k: raw.get(k) for k in PropertyState.__dataclass_fields__ if k in raw}
            return PropertyState(**fields)  # type: ignore[arg-type]

    async def put(self, property_id: str, state: PropertyState) -> None:
        # Bug-hunt #4: lock guards the read-modify-write so concurrent workers
        # cannot produce torn writes.
        async with self._lock:
            data = await self._load()
            data[property_id] = {k: getattr(state, k) for k in PropertyState.__dataclass_fields__}
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            self._cache = data


class ChangeDetector:
    """
    Runs the three-mechanism gate. Holds an httpx.AsyncClient for HEAD/GET probes.
    Does not touch Playwright.
    """

    FORCED_RESCAN_DAYS = 7

    def __init__(
        self,
        state: StateStore,
        api_catalogue: dict[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.state = state
        self.api_catalogue = api_catalogue or {}
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> ChangeDetector:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                follow_redirects=True,
                headers={"User-Agent": "ma-poc-change-detector/1.0"},
            )
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def evaluate(self, property_id: str, url: str) -> tuple[ChangeDetectionDecision, PropertyState]:
        state = await self.state.get(property_id)

        # Forced rescan after N days even if all mechanisms say UNCHANGED.
        forced = self._is_forced(state)

        etag = await self._check_etag(url, state)
        sitemap = await self._check_sitemap(url, state)
        api = await self._check_api_hash(property_id, url, state)

        results = (etag, sitemap, api)
        available = [r for r in results if r != ChangeDetectionResult.INCONCLUSIVE]
        all_unchanged = bool(available) and all(r == ChangeDetectionResult.UNCHANGED for r in available)

        skip = all_unchanged and not forced
        decision = ChangeDetectionDecision(
            etag_result=etag,
            sitemap_result=sitemap,
            api_result=api,
            skip=skip,
            forced_full_scrape=forced,
        )
        if forced and all_unchanged:
            decision.notes.append("forced_rescan_after_7d")
        return decision, state

    def _is_forced(self, state: PropertyState) -> bool:
        if state.last_full_scrape_date is None:
            return True
        try:
            last = date.fromisoformat(state.last_full_scrape_date)
        except ValueError:
            return True
        return (date.today() - last).days >= self.FORCED_RESCAN_DAYS

    async def _check_etag(self, url: str, state: PropertyState) -> ChangeDetectionResult:
        assert self._client is not None
        headers: dict[str, str] = {}
        # Bug-hunt #8: send BOTH headers.
        if state.last_etag:
            headers["If-None-Match"] = state.last_etag
        if state.last_lastmodified:
            headers["If-Modified-Since"] = state.last_lastmodified
        try:
            resp = await self._client.head(url, headers=headers)
        except httpx.HTTPError:
            return ChangeDetectionResult.INCONCLUSIVE

        if resp.status_code == 304:
            return ChangeDetectionResult.UNCHANGED

        new_etag = resp.headers.get("etag")
        new_lm = resp.headers.get("last-modified")
        if not new_etag and not new_lm:
            return ChangeDetectionResult.INCONCLUSIVE

        if new_etag and state.last_etag and new_etag == state.last_etag:
            return ChangeDetectionResult.UNCHANGED
        if new_lm and state.last_lastmodified and new_lm == state.last_lastmodified:
            return ChangeDetectionResult.UNCHANGED
        return ChangeDetectionResult.CHANGED

    async def _check_sitemap(self, url: str, state: PropertyState) -> ChangeDetectionResult:
        assert self._client is not None
        parsed = urlparse(url)
        sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
        try:
            resp = await self._client.get(sitemap_url)
        except httpx.HTTPError:
            return ChangeDetectionResult.INCONCLUSIVE
        if resp.status_code != 200 or not resp.text:
            return ChangeDetectionResult.INCONCLUSIVE

        path_needle = parsed.path or "/"
        body = resp.text
        # Cheap text scan: locate matching <url> block then its <lastmod>.
        url_blocks = re.findall(r"<url>(.*?)</url>", body, flags=re.DOTALL | re.IGNORECASE)
        lastmod: str | None = None
        for block in url_blocks:
            loc_match = re.search(r"<loc>(.*?)</loc>", block, flags=re.IGNORECASE)
            if not loc_match:
                continue
            loc = loc_match.group(1).strip()
            if path_needle and (loc.endswith(path_needle) or loc == url):
                lm_match = re.search(r"<lastmod>(.*?)</lastmod>", block, flags=re.IGNORECASE)
                if lm_match:
                    lastmod = lm_match.group(1).strip()
                break
        if lastmod is None:
            return ChangeDetectionResult.INCONCLUSIVE
        if state.last_sitemap_lastmod and lastmod == state.last_sitemap_lastmod:
            return ChangeDetectionResult.UNCHANGED
        return ChangeDetectionResult.CHANGED

    async def _check_api_hash(
        self, property_id: str, url: str, state: PropertyState
    ) -> ChangeDetectionResult:
        assert self._client is not None
        api_url = (self.api_catalogue.get("discovered", {}) or {}).get(property_id)
        if not api_url:
            return ChangeDetectionResult.INCONCLUSIVE
        try:
            resp = await self._client.get(api_url)
        except httpx.HTTPError:
            return ChangeDetectionResult.INCONCLUSIVE
        if resp.status_code != 200:
            return ChangeDetectionResult.INCONCLUSIVE
        new_hash = hashlib.sha256(resp.content).hexdigest()
        if state.last_api_hash and new_hash == state.last_api_hash:
            return ChangeDetectionResult.UNCHANGED
        return ChangeDetectionResult.CHANGED

    async def record_skip(self, property_id: str, state: PropertyState) -> None:
        state.carryforward_days += 1
        await self.state.put(property_id, state)

    async def record_full_scrape(
        self,
        property_id: str,
        state: PropertyState,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        sitemap_lastmod: str | None = None,
        api_hash: str | None = None,
    ) -> None:
        state.carryforward_days = 0
        state.last_full_scrape_date = date.today().isoformat()
        if etag is not None:
            state.last_etag = etag
        if last_modified is not None:
            state.last_lastmodified = last_modified
        if sitemap_lastmod is not None:
            state.last_sitemap_lastmod = sitemap_lastmod
        if api_hash is not None:
            state.last_api_hash = api_hash
        await self.state.put(property_id, state)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
