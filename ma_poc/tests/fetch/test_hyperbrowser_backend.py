"""Switchable Hyperbrowser fetch backend (task #46).

Pins: (1) the FETCH_BACKEND switch routes _make_provider to HB only when on,
default BrightData unchanged; (2) the provider emits a drop-in FetchResult
(OK, body bytes, RENDER, HYPERBROWSER tier, empty clearance, network_log in the
exact promotion shape); (3) never-raise + always-stop-session; (4) CF-stuck →
BOT_BLOCKED; (5) the per-property cost cap. No network — a fake session/page is
injected exactly like the residential_render tests do.
"""

from __future__ import annotations

import importlib
import json

import pytest

from ma_poc.discovery.contracts import CrawlTask, RenderMode, TaskReason
from ma_poc.fetch import hyperbrowser_backend as hb
from ma_poc.fetch.contracts import FetchOutcome
from ma_poc.fetch.hyperbrowser_backend import (
    HyperbrowserProvider,
    reset_hyperbrowser_property_counts,
)
from ma_poc.fetch.tier_escalator import _make_provider
from ma_poc.models.fetch_tier import FetchTier


def _task(url: str = "https://walled.example/", pid: str = "p-1") -> CrawlTask:
    return CrawlTask(
        url=url, property_id=pid, priority=0,
        reason=TaskReason.SCHEDULED, render_mode=RenderMode.RENDER, budget_ms=180_000,
    )


class _FakeResp:
    def __init__(self, url: str, status: int, ct: str, body: bytes) -> None:
        self.url = url
        self.status = status
        self._ct = ct
        self._body = body

    async def all_headers(self) -> dict[str, str]:
        return {"content-type": self._ct}

    async def body(self) -> bytes:
        return self._body


class _FakePage:
    def __init__(self, html: str, responses: list[_FakeResp], title: str = "", url: str | None = None) -> None:
        self._html = html
        self._responses = responses
        self._title = title
        self._url = url
        self._handlers: list = []

    async def route(self, pattern: str, handler) -> None:
        return None

    def on(self, event: str, handler) -> None:
        if event == "response":
            self._handlers.append(handler)

    async def goto(self, url: str, **kw) -> None:
        if self._url is None:
            self._url = url
        for r in self._responses:
            for h in self._handlers:
                h(r)  # _spawn schedules an ensure_future task

    async def content(self) -> str:
        return self._html

    async def title(self) -> str:
        return self._title

    @property
    def url(self) -> str | None:
        return self._url


class _FakeSession:
    def __init__(self, page: _FakePage | None, *, raise_on_open: Exception | None = None) -> None:
        self._page = page
        self._raise = raise_on_open
        self.closed = False
        self.opened = False
        self.session_id = "sid-fake"

    async def open(self) -> _FakePage:
        self.opened = True
        if self._raise is not None:
            raise self._raise
        assert self._page is not None
        return self._page


    async def close(self) -> None:
        self.closed = True


# ── the switch (both seams route through _make_provider for HEAD/GET) ─────────


def test_switch_helpers_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from ma_poc.config.feature_flags import fetch_backend, hb_enabled, hb_tiers

    monkeypatch.delenv("FETCH_BACKEND", raising=False)
    assert fetch_backend() == "brightdata" and hb_enabled() is False
    monkeypatch.setenv("FETCH_BACKEND", "hyperbrowser")
    assert hb_enabled() is True and hb_tiers() == frozenset({"UNLOCKER"})


def test_switch_off_does_not_route_to_hb(monkeypatch: pytest.MonkeyPatch) -> None:
    # RESIDENTIAL_RENDER constructs cheaply (no BrightData env needed), so it
    # proves flag-off leaves the rung on its BrightData provider, not HB.
    monkeypatch.delenv("FETCH_BACKEND", raising=False)
    assert type(_make_provider(FetchTier.RESIDENTIAL_RENDER)).__name__ == "ResidentialRenderProvider"


def test_switch_on_routes_unlocker_to_hb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FETCH_BACKEND", "hyperbrowser")
    monkeypatch.delenv("HYPERBROWSER_TIERS", raising=False)  # default UNLOCKER only
    p = _make_provider(FetchTier.UNLOCKER)
    assert isinstance(p, HyperbrowserProvider) and p.mode == "unlock"
    # RESIDENTIAL_RENDER NOT in default tiers → stays BrightData 2a.
    assert type(_make_provider(FetchTier.RESIDENTIAL_RENDER)).__name__ == "ResidentialRenderProvider"


def test_switch_on_can_scope_render_render(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FETCH_BACKEND", "hyperbrowser")
    monkeypatch.setenv("HYPERBROWSER_TIERS", "UNLOCKER,RESIDENTIAL_RENDER")
    p = _make_provider(FetchTier.RESIDENTIAL_RENDER)
    assert isinstance(p, HyperbrowserProvider) and p.mode == "render"


# ── FetchResult contract ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ok_result_is_drop_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hb, "_SETTLE_MS", 100)  # let the capture tasks drain
    reset_hyperbrowser_property_counts()
    page = _FakePage(
        html="<html><body>$1,499 1 bed</body></html>",
        responses=[_FakeResp("https://x/api/v1/units", 200, "application/json", b'{"units":[{"rent":1499}]}')],
        title="Real Property", url="https://walled.example/final",
    )
    sess = _FakeSession(page)
    prov = HyperbrowserProvider(mode="unlock", session_factory=lambda: sess)
    res = await prov.fetch(_task(), None)

    assert res.ok() and res.outcome == FetchOutcome.OK
    assert isinstance(res.body, bytes)
    assert res.render_mode == RenderMode.RENDER
    assert res.final_url == "https://walled.example/final"
    assert res.fetch_tier_used == int(FetchTier.HYPERBROWSER)
    assert res.clearance_cookies == {}  # cross-egress: deliberately empty
    assert res.network_log, "XHR JSON must be captured into network_log"
    entry = res.network_log[0]
    assert set(entry) >= {"url", "status", "content_type", "body", "captcha_detected"}
    assert isinstance(entry["body"], str) and json.loads(entry["body"])  # json.loads-able (scraper.py:1070)
    assert entry["captcha_detected"] is False
    assert sess.closed, "session must be stopped"


@pytest.mark.asyncio
async def test_cf_stuck_is_bot_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hb, "_SETTLE_MS", 10)
    reset_hyperbrowser_property_counts()
    page = _FakePage(html="<html>Just a moment...</html>", responses=[], title="Just a moment...")
    sess = _FakeSession(page)
    prov = HyperbrowserProvider(session_factory=lambda: sess)
    res = await prov.fetch(_task(), None)
    assert res.outcome == FetchOutcome.BOT_BLOCKED
    assert res.captcha_detected is True
    assert sess.closed


@pytest.mark.asyncio
async def test_never_raises_and_stops_session_on_open_failure() -> None:
    reset_hyperbrowser_property_counts()
    sess = _FakeSession(None, raise_on_open=RuntimeError("cdp connect died"))
    prov = HyperbrowserProvider(session_factory=lambda: sess)
    res = await prov.fetch(_task(), None)  # must NOT raise
    assert res.outcome in (FetchOutcome.TRANSIENT, FetchOutcome.HARD_FAIL)
    assert res.fetch_tier_used == int(FetchTier.HYPERBROWSER)
    assert sess.opened and sess.closed, "session opened then stopped in finally"


@pytest.mark.asyncio
async def test_timeout_is_transient() -> None:
    reset_hyperbrowser_property_counts()
    sess = _FakeSession(None, raise_on_open=TimeoutError("nav timeout"))
    prov = HyperbrowserProvider(session_factory=lambda: sess)
    res = await prov.fetch(_task(), None)
    assert res.outcome == FetchOutcome.TRANSIENT


# ── per-property cost cap ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_per_property_cost_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hb, "_SETTLE_MS", 5)
    monkeypatch.setenv("HYPERBROWSER_MAX_CALLS_PER_PROPERTY", "2")
    reset_hyperbrowser_property_counts()
    opens = {"n": 0}

    def _factory() -> _FakeSession:
        opens["n"] += 1
        return _FakeSession(_FakePage("<html>ok</html>", [], title="Real"))

    prov = HyperbrowserProvider(session_factory=_factory)
    r1 = await prov.fetch(_task(pid="cap-x"), None)
    r2 = await prov.fetch(_task(pid="cap-x"), None)
    r3 = await prov.fetch(_task(pid="cap-x"), None)  # over cap

    assert opens["n"] == 2, "third call must NOT open a (paid) session"
    assert r1.outcome == FetchOutcome.OK and r2.outcome == FetchOutcome.OK
    assert r3.outcome == FetchOutcome.BOT_BLOCKED
    assert r3.block_signature == "hb_property_cap"
    assert r3.attempts == 0


# ── HB preferred over BrightData (Ankur: HB free, BrightData backup) ──────────


def test_hb_rung_precedes_brightdata_when_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    # The tier flags gate on ENABLE_TIER_ESCALATION, so turn it (and the paid
    # BrightData rungs) on to exercise the ordering.
    monkeypatch.setenv("ENABLE_TIER_ESCALATION", "true")
    monkeypatch.setenv("ENABLE_RESIDENTIAL_TIER", "true")
    monkeypatch.setenv("ENABLE_RESIDENTIAL_RENDER_TIER", "true")
    monkeypatch.delenv("SKIP_RESIDENTIAL_WHEN_UNLOCKER", raising=False)
    import ma_poc.config.feature_flags as ff
    import ma_poc.fetch.tier_escalator as te

    importlib.reload(ff)
    importlib.reload(te)
    try:
        monkeypatch.setenv("FETCH_BACKEND", "hyperbrowser")
        ladder = te._build_ladder(FetchTier.DIRECT)
        assert FetchTier.HYPERBROWSER in ladder
        # HB is tried BEFORE the paid BrightData rungs (which REMAIN as backup).
        bd_rungs = [t for t in (FetchTier.RESIDENTIAL, FetchTier.RESIDENTIAL_RENDER) if t in ladder]
        assert bd_rungs, "need a BrightData rung present to check ordering"
        i_hb = ladder.index(FetchTier.HYPERBROWSER)
        assert all(i_hb < ladder.index(t) for t in bd_rungs)
        assert isinstance(te._make_provider(FetchTier.HYPERBROWSER), HyperbrowserProvider)
        # Off when not preferred → BrightData ladder unchanged.
        monkeypatch.setenv("FETCH_BACKEND", "brightdata")
        assert FetchTier.HYPERBROWSER not in te._build_ladder(FetchTier.DIRECT)
    finally:
        for k in ("ENABLE_TIER_ESCALATION", "ENABLE_RESIDENTIAL_TIER", "ENABLE_RESIDENTIAL_RENDER_TIER"):
            monkeypatch.delenv(k, raising=False)
        importlib.reload(ff)
        importlib.reload(te)
