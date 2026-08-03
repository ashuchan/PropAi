"""Switchable Hyperbrowser fetch backend (task #46).

Pins: (1) the FETCH_BACKEND switch routes _make_provider to HB only when on,
default BrightData unchanged; (2) the provider emits a drop-in FetchResult
(OK, body bytes, RENDER, HYPERBROWSER tier, empty clearance, network_log in the
exact promotion shape); (3) never-raise + always-stop-session; (4) CF-stuck →
BOT_BLOCKED; (5) the per-property cost cap. No network — a fake session/page is
injected exactly like the residential_render tests do.
"""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest

from ma_poc.discovery.contracts import CrawlTask, RenderMode, TaskReason
from ma_poc.fetch import hyperbrowser_backend as hb
from ma_poc.fetch.contracts import FetchOutcome
from ma_poc.fetch.hyperbrowser_backend import (
    HyperbrowserProvider,
    _hb_try_reserve_property,
    _published_rentmanager_inventory_url,
    hb_raw_get,
    hyperbrowser_property_call_count,
    reset_hyperbrowser_property_counts,
)
from ma_poc.fetch.tier_escalator import _make_provider
from ma_poc.models.fetch_tier import FetchTier


def _task(url: str = "https://walled.example/", pid: str = "p-1") -> CrawlTask:
    return CrawlTask(
        url=url,
        property_id=pid,
        priority=0,
        reason=TaskReason.SCHEDULED,
        render_mode=RenderMode.RENDER,
        budget_ms=180_000,
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
    def __init__(
        self, html: str, responses: list[_FakeResp], title: str = "", url: str | None = None
    ) -> None:
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
        responses=[
            _FakeResp("https://x/api/v1/units", 200, "application/json", b'{"units":[{"rent":1499}]}')
        ],
        title="Real Property",
        url="https://walled.example/final",
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


def test_rentmanager_inventory_hop_requires_exact_same_origin_publication() -> None:
    root = """
    <a href="https://gmc.twa.rentmanager.com/">Resident Portal</a>
    <a href="/unit-availability/">View Live Apartment Availability</a>
    """
    assert (
        _published_rentmanager_inventory_url(
            root,
            "https://legacy.example/",
        )
        == "https://legacy.example/unit-availability/"
    )
    assert (
        _published_rentmanager_inventory_url(
            root.replace("/unit-availability/", "https://sibling.example/unit-availability/"),
            "https://legacy.example/",
        )
        == ""
    )
    assert (
        _published_rentmanager_inventory_url(
            '<a href="/unit-availability/">Availability</a>',
            "https://legacy.example/",
        )
        == ""
    )


@pytest.mark.asyncio
async def test_provider_reuses_session_for_published_rentmanager_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hb, "_SETTLE_MS", 5)
    monkeypatch.setenv("HYPERBROWSER_MAX_CALLS_PER_PROPERTY", "1")
    reset_hyperbrowser_property_counts()
    root = """
    <html><body>
      <a href="https://gmc.twa.rentmanager.com/">Resident Portal</a>
      <a href="/unit-availability/">View Live Apartment Availability</a>
    </body></html>
    """
    roster = """
    <div class="rmwb_listing-wrapper">
      <a href="details/?uid=928">Details</a>
      <span class="rmwb_info-title">Rent</span>
    </div>
    """
    requested: list[str] = []

    class _InventoryPage(_FakePage):
        async def evaluate(self, _js: str, relative: str) -> dict[str, object]:
            requested.append(relative)
            return {"status": 200, "body": roster}

    page = _InventoryPage(
        html=root,
        responses=[],
        title="Legacy Apartments",
        url="https://legacy.example/",
    )
    session = _FakeSession(page)
    result = await HyperbrowserProvider(session_factory=lambda: session).fetch(
        _task(url="https://legacy.example/", pid="legacy-rm"), None
    )

    assert result.outcome == FetchOutcome.OK
    assert result.final_url == "https://legacy.example/unit-availability/"
    assert result.body == roster.encode()
    assert requested == ["/unit-availability/"]
    assert hyperbrowser_property_call_count("legacy-rm") == 1
    assert session.closed


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


@pytest.mark.asyncio
async def test_local_close_hangs_are_bounded_before_remote_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged CDP close must not delay the paid-session stop indefinitely."""
    monkeypatch.setattr(hb, "_LOCAL_CLOSE_TIMEOUT_SECONDS", 0.01)
    calls: list[str] = []

    class _HungBrowser:
        async def close(self) -> None:
            calls.append("browser")
            await asyncio.Event().wait()

    class _HungPlaywright:
        async def stop(self) -> None:
            calls.append("playwright")
            await asyncio.Event().wait()

    session = hb._HbSession(mode="render")
    session._browser = _HungBrowser()
    session._pw = _HungPlaywright()
    # No remote id is needed here: reaching the end of close() after both
    # hung local resources proves neither can obstruct the following stop
    # section. The production path enters that section whenever id is set.
    await asyncio.wait_for(session.close(), timeout=0.2)

    assert calls == ["browser", "playwright"]


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


def test_priority_slot_is_reserved_without_increasing_total_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPERBROWSER_MAX_CALLS_PER_PROPERTY", "3")
    monkeypatch.setenv("HYPERBROWSER_RESERVED_PRIORITY_CALLS", "1")
    reset_hyperbrowser_property_counts()

    assert _hb_try_reserve_property("priority-cap", reason="discovery-1")
    assert _hb_try_reserve_property("priority-cap", reason="discovery-2")
    assert not _hb_try_reserve_property("priority-cap", reason="discovery-3")
    assert _hb_try_reserve_property(
        "priority-cap",
        priority=True,
        reason="exact-profile-route",
    )
    assert not _hb_try_reserve_property(
        "priority-cap",
        priority=True,
        reason="over-total-cap",
    )
    assert hyperbrowser_property_call_count("priority-cap") == 3


@pytest.mark.asyncio
async def test_raw_get_shares_per_property_cost_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct raw shortcuts must not bypass the paid-session cap."""
    monkeypatch.setenv("HYPERBROWSER_MAX_CALLS_PER_PROPERTY", "1")
    reset_hyperbrowser_property_counts()
    opens = {"n": 0}

    class _RawPage:
        async def goto(self, url: str, **kw) -> None:
            return None

        async def evaluate(self, js: str, rel: str) -> dict[str, object]:
            return {"status": 200, "body": "raw-ok"}

    def _factory() -> _FakeSession:
        opens["n"] += 1
        return _FakeSession(_RawPage())  # type: ignore[arg-type]

    first = await hb_raw_get("https://example.test/units", "raw-cap-x", session_factory=_factory)
    second = await hb_raw_get("https://example.test/units", "raw-cap-x", session_factory=_factory)

    assert first == (200, "raw-ok")
    assert second == (0, "")
    assert opens["n"] == 1, "capped raw GET must not open a paid session"
    assert hyperbrowser_property_call_count("raw-cap-x") == 1


@pytest.mark.asyncio
async def test_raw_get_then_reuses_one_session_for_same_origin_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPERBROWSER_MAX_CALLS_PER_PROPERTY", "1")
    reset_hyperbrowser_property_counts()
    opens = {"n": 0}
    requested_paths: list[str] = []

    class _RawChainPage:
        async def goto(self, url: str, **kw) -> None:
            return None

        async def evaluate(self, js: str, rel: str) -> dict[str, object]:
            requested_paths.append(rel)
            bodies = {
                "/floorplans": '<a href="/floorplans/a1">A1</a>',
                "/floorplans/a1": "native-unit-roster",
            }
            return {"status": 200, "body": bodies.get(rel, "")}

    def _factory() -> _FakeSession:
        opens["n"] += 1
        return _FakeSession(_RawChainPage())  # type: ignore[arg-type]

    first, followup = await hb.hb_raw_get_then(
        "https://example.test/floorplans",
        "raw-chain-x",
        lambda _body: "/floorplans/a1",
        session_factory=_factory,
    )

    assert first == (200, '<a href="/floorplans/a1">A1</a>')
    assert followup == (
        "https://example.test/floorplans/a1",
        200,
        "native-unit-roster",
    )
    assert requested_paths == ["/floorplans", "/floorplans/a1"]
    assert opens["n"] == 1
    assert hyperbrowser_property_call_count("raw-chain-x") == 1

    capped, capped_followup = await hb.hb_raw_get_then(
        "https://example.test/floorplans",
        "raw-chain-x",
        lambda _body: "/floorplans/a1",
        session_factory=_factory,
    )
    assert capped == (0, "") and capped_followup is None
    assert opens["n"] == 1


@pytest.mark.asyncio
async def test_raw_get_then_rejects_cross_origin_followup() -> None:
    reset_hyperbrowser_property_counts()
    requested_paths: list[str] = []

    class _RawChainPage:
        async def goto(self, url: str, **kw) -> None:
            return None

        async def evaluate(self, js: str, rel: str) -> dict[str, object]:
            requested_paths.append(rel)
            return {"status": 200, "body": "index"}

    session = _FakeSession(_RawChainPage())  # type: ignore[arg-type]
    first, followup = await hb.hb_raw_get_then(
        "https://example.test/floorplans",
        "raw-chain-cross-origin",
        lambda _body: "https://sibling.example/floorplans/a1",
        session_factory=lambda: session,
    )

    assert first == (200, "index")
    assert followup is None
    assert requested_paths == ["/floorplans"]
    assert session.closed


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
