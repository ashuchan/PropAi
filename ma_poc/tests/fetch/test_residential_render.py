"""Tests for the clean "2a" residential-render fetch tier.

Covers the legal-boundary classifier, the wait-not-solve decision loop, the
provider (OK / abort-on-interactive / transient — with a fake browser that
has NO click/solve methods, so any attempt to interact would raise), and the
escalator ladder wiring.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ma_poc.fetch import tier_escalator
from ma_poc.fetch.captcha_detect import ChallengeKind, classify_challenge
from ma_poc.fetch.contracts import FetchOutcome, RenderMode
from ma_poc.fetch.providers import residential_render as rr
from ma_poc.models.fetch_tier import FetchTier

# ── classify_challenge — the legal boundary ──────────────────────────────────

def test_classify_none_on_real_page() -> None:
    kind, prov = classify_challenge(b"<html><body>Welcome to The Oaks apartments</body></html>")
    assert kind is ChallengeKind.NONE and prov is None


def test_classify_cloudflare_js_is_passable() -> None:
    for marker in (b"Just a moment...", b"challenge-platform", b"__cf_chl_opt"):
        kind, prov = classify_challenge(b"<html>" + marker + b"</html>")
        assert kind is ChallengeKind.PASSABLE_JS
        assert prov == "cloudflare"


@pytest.mark.parametrize(
    ("body", "provider"),
    [
        (b"<html>h-captcha hcaptcha.com</html>", "hcaptcha"),
        (b"<html>g-recaptcha www.google.com/recaptcha</html>", "recaptcha"),
        (b"<html>PerimeterX _pxhd</html>", "perimeterx"),
        (b"<html>Robot Challenge Screen sgchallenge</html>", "sucuri"),
    ],
)
def test_classify_interactive_captchas(body: bytes, provider: str) -> None:
    kind, prov = classify_challenge(body)
    assert kind is ChallengeKind.INTERACTIVE
    assert prov == provider


def test_classify_widget_on_large_real_page_is_none() -> None:
    # a real page that merely embeds a captcha widget in a form is NOT a
    # challenge (body-size guard) — must not be treated as interactive.
    body = b"g-recaptcha " + b"x" * 40_000
    kind, _ = classify_challenge(body, body_size=len(body))
    assert kind is ChallengeKind.NONE


# ── resolve_challenge — wait-not-solve loop ──────────────────────────────────

async def _noop_sleep(_s: float) -> None:
    return None


@pytest.mark.asyncio
async def test_resolve_returns_none_immediately() -> None:
    async def get_html() -> bytes:
        return b"<html>real content</html>"

    kind, html, prov = await rr.resolve_challenge(get_html, sleep=_noop_sleep)
    assert kind is ChallengeKind.NONE
    assert b"real content" in html


@pytest.mark.asyncio
async def test_resolve_aborts_on_interactive_immediately() -> None:
    async def get_html() -> bytes:
        return b"<html>h-captcha</html>"

    kind, _html, prov = await rr.resolve_challenge(get_html, sleep=_noop_sleep)
    assert kind is ChallengeKind.INTERACTIVE and prov == "hcaptcha"


@pytest.mark.asyncio
async def test_resolve_waits_then_clears() -> None:
    # JS challenge for the first two polls, then the site's JS clears it.
    seq = [b"Just a moment...", b"Just a moment...", b"<html>units here</html>"]
    calls = {"i": 0}

    async def get_html() -> bytes:
        h = seq[min(calls["i"], len(seq) - 1)]
        calls["i"] += 1
        return h

    kind, html, _ = await rr.resolve_challenge(
        get_html, wait_ms=10_000, poll_ms=1_000, sleep=_noop_sleep
    )
    assert kind is ChallengeKind.NONE
    assert b"units here" in html


@pytest.mark.asyncio
async def test_resolve_never_clears_stays_blocked() -> None:
    async def get_html() -> bytes:
        return b"Just a moment..."  # never clears

    kind, _html, _ = await rr.resolve_challenge(
        get_html, wait_ms=3_000, poll_ms=1_000, sleep=_noop_sleep
    )
    # stays PASSABLE_JS → caller treats as blocked; NEVER escalates to solving
    assert kind is ChallengeKind.PASSABLE_JS


@pytest.mark.asyncio
async def test_resolve_js_turns_interactive_mid_wait_aborts() -> None:
    seq = [b"Just a moment...", b"<html>h-captcha</html>"]
    calls = {"i": 0}

    async def get_html() -> bytes:
        h = seq[min(calls["i"], len(seq) - 1)]
        calls["i"] += 1
        return h

    kind, _html, prov = await rr.resolve_challenge(
        get_html, wait_ms=10_000, poll_ms=1_000, sleep=_noop_sleep
    )
    assert kind is ChallengeKind.INTERACTIVE and prov == "hcaptcha"


# ── provider — fake browser with NO click/solve methods ──────────────────────


class _FakeContext:
    def __init__(self, cookies: list[dict]) -> None:
        self._cookies = cookies

    async def cookies(self) -> list[dict]:
        return self._cookies


class _FakePage:
    """A page that can render, return content, and expose cookies — but has
    NO click / evaluate / mouse / solve surface. If the provider ever tried
    to interact with a challenge, it would AttributeError and fail the test."""

    def __init__(self, html: bytes | list[bytes], *, cookies=None, goto_exc=None) -> None:
        self._html = html
        self._i = 0
        self.url = "https://prop.example/after"
        self.context = _FakeContext(cookies or [])
        self._goto_exc = goto_exc

    async def goto(self, url: str, **_kw) -> None:
        if self._goto_exc is not None:
            raise self._goto_exc

    async def content(self) -> str:
        if isinstance(self._html, list):
            h = self._html[min(self._i, len(self._html) - 1)]
            self._i += 1
        else:
            h = self._html
        return h.decode() if isinstance(h, bytes) else h

    def on(self, _event: str, _handler) -> None:  # network capture no-op
        return None


class _FakePool:
    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.released = False

    async def acquire(self, identity, proxy=None):  # noqa: ANN001
        return self._page

    async def release(self, page) -> None:  # noqa: ANN001
        self.released = True


class _FakeProxyProvider:
    def get_config(self, tier, canonical_id, state=None):  # noqa: ANN001
        return object()  # opaque; the fake pool ignores it


def _task() -> SimpleNamespace:
    return SimpleNamespace(property_id="P1", url="https://prop.example/floorplans")


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch) -> None:
    # zero out the post-nav settle sleep so provider tests are instant
    monkeypatch.setattr(rr, "_SETTLE_MS", 0)


@pytest.mark.asyncio
async def test_provider_ok_when_no_challenge() -> None:
    page = _FakePage(b"<html>real listing with $1,500 units</html>",
                     cookies=[{"name": "cf_clearance", "value": "abc"}])
    pool = _FakePool(page)
    prov = rr.ResidentialRenderProvider(pool=pool, proxy_provider=_FakeProxyProvider())

    res = await prov.fetch(_task(), SimpleNamespace())
    assert res.outcome is FetchOutcome.OK
    assert res.render_mode is RenderMode.RENDER
    assert res.fetch_tier_used == int(FetchTier.RESIDENTIAL_RENDER)
    assert b"real listing" in res.body
    # clearance harvested (byproduct — the cookies the browser now holds)
    assert res.clearance_cookies.get("cf_clearance") == "abc"
    assert pool.released is True


@pytest.mark.asyncio
async def test_provider_aborts_on_interactive_captcha_never_interacts() -> None:
    # page shows an interactive captcha; the fake page has NO click surface,
    # so if the provider tried to solve it the test would error.
    page = _FakePage(b"<html>h-captcha hcaptcha.com</html>")
    pool = _FakePool(page)
    prov = rr.ResidentialRenderProvider(pool=pool, proxy_provider=_FakeProxyProvider())

    res = await prov.fetch(_task(), SimpleNamespace())
    assert res.outcome is FetchOutcome.BOT_BLOCKED
    assert res.captcha_detected is True
    assert res.fetch_tier_used == int(FetchTier.RESIDENTIAL_RENDER)
    assert (res.block_signature or "").startswith("render_abort")
    assert pool.released is True


@pytest.mark.asyncio
async def test_provider_never_raises_on_goto_error() -> None:
    page = _FakePage(b"", goto_exc=RuntimeError("net::ERR_TIMED_OUT"))
    pool = _FakePool(page)
    prov = rr.ResidentialRenderProvider(pool=pool, proxy_provider=_FakeProxyProvider())

    res = await prov.fetch(_task(), SimpleNamespace())
    assert res.outcome is FetchOutcome.TRANSIENT
    assert res.fetch_tier_used == int(FetchTier.RESIDENTIAL_RENDER)
    assert pool.released is True


# ── escalator ladder wiring ──────────────────────────────────────────────────

def test_render_tier_absent_by_default() -> None:
    assert FetchTier.RESIDENTIAL_RENDER not in tier_escalator._build_ladder(FetchTier.DIRECT)


def test_render_tier_present_and_before_solvers_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(tier_escalator, "ENABLE_RESIDENTIAL_RENDER_TIER", True)
    monkeypatch.setattr(tier_escalator, "ENABLE_RESIDENTIAL_TIER", True)
    monkeypatch.setattr(tier_escalator, "ENABLE_UNLOCKER_TIER", True)
    monkeypatch.setattr(tier_escalator, "ENABLE_FLARESOLVERR_TIER", True)
    monkeypatch.setattr(tier_escalator, "SKIP_RESIDENTIAL_WHEN_UNLOCKER", False)
    ladder = tier_escalator._build_ladder(FetchTier.DIRECT)
    assert FetchTier.RESIDENTIAL_RENDER in ladder
    idx = ladder.index(FetchTier.RESIDENTIAL_RENDER)
    # after http-residential, before the solver tiers
    assert idx > ladder.index(FetchTier.RESIDENTIAL)
    assert idx < ladder.index(FetchTier.FLARESOLVERR)
    assert idx < ladder.index(FetchTier.UNLOCKER)


def test_make_provider_builds_render_provider() -> None:
    prov = tier_escalator._make_provider(FetchTier.RESIDENTIAL_RENDER)
    assert isinstance(prov, rr.ResidentialRenderProvider)


def test_skip_rules_include_render_tier() -> None:
    assert FetchTier.RESIDENTIAL in tier_escalator.TIER_SKIP_RULES[FetchTier.RESIDENTIAL_RENDER]


# ── browser engine / mode configuration (a + b) ──────────────────────────────

def test_pool_defaults_are_backward_compatible() -> None:
    from ma_poc.fetch.browser_pool import BrowserContextPool

    pool = BrowserContextPool()
    assert pool._driver == "patchright"  # existing render path unchanged
    assert pool._engine == "chromium"
    assert pool._headless is True
    assert pool._channel is None


def test_pool_stores_render_config() -> None:
    from ma_poc.fetch.browser_pool import BrowserContextPool

    pool = BrowserContextPool(driver="playwright", engine="firefox", headless=False, channel="chrome")
    assert (pool._driver, pool._engine, pool._headless, pool._channel) == (
        "playwright", "firefox", False, "chrome",
    )


def test_render_provider_builds_vanilla_pool_from_env(monkeypatch) -> None:
    # the 2a tier must use vanilla playwright (never the stealth patchright)
    monkeypatch.setattr(rr, "_ENGINE", "firefox")
    monkeypatch.setattr(rr, "_HEADLESS", False)
    monkeypatch.setattr(rr, "_CHANNEL", "chrome")
    prov = rr.ResidentialRenderProvider()
    pool = prov._get_pool()
    assert pool._driver == "playwright"
    assert pool._engine == "firefox"
    assert pool._headless is False
    # channel is chromium-only; the pool stores it, launch ignores it for FF
    assert pool._channel == "chrome"


def test_identity_matches_engine() -> None:
    from ma_poc.fetch.stealth import IdentityPool

    ids = IdentityPool()
    ff = ids.pick_family(("firefox",), "P1")
    assert ff.browser_family == "firefox"


def test_provider_picks_firefox_identity_for_firefox_engine(monkeypatch) -> None:
    monkeypatch.setattr(rr, "_ENGINE", "firefox")
    prov = rr.ResidentialRenderProvider()
    assert prov._pick_identity("P1").browser_family == "firefox"


def test_provider_picks_chrome_identity_for_chromium_engine(monkeypatch) -> None:
    monkeypatch.setattr(rr, "_ENGINE", "chromium")
    prov = rr.ResidentialRenderProvider()
    assert prov._pick_identity("P1").browser_family in ("chrome", "edge")


# ── geo consistency (honest fix #1 + #2) ─────────────────────────────────────

def test_brightdata_state_for_timezone() -> None:
    from ma_poc.fetch.stealth import brightdata_state_for_timezone

    assert brightdata_state_for_timezone("America/Los_Angeles") == "california"
    assert brightdata_state_for_timezone("America/Chicago") == "illinois"
    assert brightdata_state_for_timezone("America/New_York") == "new_york"
    assert brightdata_state_for_timezone("America/Denver") == "colorado"
    # unknown tz → None (caller falls back to country-level targeting)
    assert brightdata_state_for_timezone("Europe/London") is None


def test_realistic_screen_is_desktop_sized() -> None:
    from ma_poc.fetch.stealth import REALISTIC_SCREEN

    w, h = REALISTIC_SCREEN
    assert w >= 1280 and h >= 720  # a real monitor, not a headless default


def test_get_config_state_targets_region(monkeypatch) -> None:
    for k in ("CUSTOMER_ID", "DC_ZONE", "DC_PASSWORD", "RESI_ZONE", "RESI_PASSWORD"):
        monkeypatch.setenv(f"BRIGHTDATA_{k}", "test")
    from ma_poc.fetch.proxy.base import ProxyTier
    from ma_poc.fetch.proxy.brightdata import BrightDataProvider

    prov = BrightDataProvider()
    cfg = prov.get_config(tier=ProxyTier.RESIDENTIAL, canonical_id="P1", state="california")
    assert "-state-california" in cfg.username
    # default (no state) omits the segment
    cfg2 = prov.get_config(tier=ProxyTier.RESIDENTIAL, canonical_id="P1")
    assert "-state-" not in cfg2.username


class _CapturingProxyProvider:
    """Records the kwargs passed to get_config so we can assert geo-targeting."""

    def __init__(self) -> None:
        self.last_kwargs: dict = {}

    def get_config(self, **kwargs):  # noqa: ANN003
        self.last_kwargs = kwargs
        return object()


def test_match_geo_off_targets_no_state(monkeypatch) -> None:
    monkeypatch.setattr(rr, "_MATCH_GEO", False)
    cap = _CapturingProxyProvider()
    prov = rr.ResidentialRenderProvider(proxy_provider=cap)
    ident = rr._IDENTITIES.pick_chrome_only("P1")
    prov._residential_proxy("P1", ident)
    assert cap.last_kwargs.get("state") is None


def test_match_geo_on_targets_identity_region(monkeypatch) -> None:
    monkeypatch.setattr(rr, "_MATCH_GEO", True)
    cap = _CapturingProxyProvider()
    prov = rr.ResidentialRenderProvider(proxy_provider=cap)

    class _Id:
        timezone_id = "America/Los_Angeles"

    prov._residential_proxy("P1", _Id())
    assert cap.last_kwargs.get("state") == "california"
