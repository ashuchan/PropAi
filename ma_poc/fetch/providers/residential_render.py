"""ResidentialRenderProvider — the clean "2a" render tier.

A REAL, unmodified browser (Playwright Chromium via the standard
:class:`BrowserContextPool`) rendering the target page through a
residential proxy. Its whole purpose is to reach the walled cohort the way
an ordinary visitor does, *without* the third-party challenge-solving
services (BrightData Web Unlocker, FlareSolverr) that this tier replaces.

Legal posture — read before changing anything here
---------------------------------------------------
This provider deliberately stays on the defensible side of the line:

  * **No fingerprint spoofing.** It launches **vanilla Playwright**
    (``driver="playwright"``) — NOT the ``patchright`` stealth fork the
    other bot-blocked render path uses — with the standard browser context
    (real UA, viewport, timezone) and NO anti-detection injection: it does
    not patch ``navigator.webdriver``, canvas, WebGL, or any fingerprinting
    surface. (Stealth browsers — Camoufox / patchright — are the separate,
    higher-risk "2b" path and are intentionally NOT used here.) The
    legitimate levers for pass rate are being a *more genuinely real*
    browser — headful, the stock Chrome channel, or a Firefox engine (all
    env-configurable) — never spoofing.
  * **It passes JS challenges only by WAITING.** On a Cloudflare
    JS/managed interstitial it renders the page and lets the site's own
    JavaScript clear the challenge — exactly what every human visitor's
    browser does. It never runs a solver.
  * **It ABORTS on interactive CAPTCHAs.** The moment an hCaptcha /
    reCAPTCHA / PerimeterX / Sucuri challenge (or a JS challenge that does
    not auto-clear within the wait budget) is seen, it returns
    ``BOT_BLOCKED`` with ``captcha_detected=True`` and does NOT click,
    solve, or otherwise interact. Reaching one means the site is gating
    with a real access control and we go out of scope — by design.

``clearance_cookies`` harvested here are the ordinary cookies the browser
receives after a JS challenge auto-clears (the same ones a visitor's
browser holds) — not a bypass artifact.

Honest realism levers (present) vs. the excluded evasion lever
--------------------------------------------------------------
The tier makes its own attributes *consistent with its real connection* —
never fabricates a signal:

  * **Geo consistency** — a realistic fixed 1080p ``screen`` (headless
    reports a tiny/absent screen), US ``locale`` + ``timezone_id`` from the
    identity, and (opt-in, ``RESIDENTIAL_RENDER_MATCH_GEO``) targeting the
    residential exit IP to the identity's timezone region so IP-offset ==
    browser-offset.
  * **Natural waiting only** — it waits for the page/JS challenge to settle
    and clear, exactly as a real browser does.

**Deliberately EXCLUDED: simulated human behavior.** This tier does NOT
inject fake mouse movements, randomized "think-time", or scroll jitter to
look human to a *behavioral* detector. That fabricates a human signal that
isn't there — the same category as fingerprint spoofing, at the behavior
layer — and belongs to the higher-risk "2b" path, not here. Keep it out.

Provider contract: ``fetch()`` never raises; it always returns a
``FetchResult`` with ``fetch_tier_used`` set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ma_poc.fetch.captcha_detect import ChallengeKind, classify_challenge
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.stealth import IdentityPool
from ma_poc.models.fetch_tier import FetchTier

if TYPE_CHECKING:
    from ma_poc.discovery.contracts import CrawlTask
    from ma_poc.models.scrape_profile import ScrapeProfile

log = logging.getLogger(__name__)

_TIER = FetchTier.RESIDENTIAL_RENDER
_IDENTITIES = IdentityPool()

# Browser engine/mode for the clean render tier. These control how genuinely
# "a real browser" we present as — the LEGITIMATE lever for pass rate (a real
# browser passes CF challenges better) WITHOUT any fingerprint spoofing:
#   ENGINE   = "chromium" | "firefox"  (Firefox draws less CF scrutiny; its UA
#              is matched to a Firefox identity so engine and UA agree)
#   HEADLESS = "false"/"headful" → headful window (needs a display/xvfb on
#              headless hosts; passes CF far better) | anything else → headless
#   CHANNEL  = e.g. "chrome" → drive the STOCK installed Chrome instead of the
#              bundled Chromium (Chromium only; more genuinely a real browser)
# The driver is ALWAYS vanilla Playwright here (never patchright) — the 2a
# posture must NOT ship an anti-detection browser.
_ENGINE = os.environ.get("RESIDENTIAL_RENDER_ENGINE", "chromium").strip().lower()
_HEADLESS = os.environ.get("RESIDENTIAL_RENDER_HEADLESS", "true").strip().lower() not in (
    "false",
    "headful",
    "0",
)
_CHANNEL = os.environ.get("RESIDENTIAL_RENDER_CHANNEL", "").strip() or None

# Geo consistency (honest fix #1): target the residential exit IP to the US
# region whose UTC offset matches the sticky identity's timezone, so the IP
# offset and the browser timezone AGREE (a real visitor from that IP has that
# timezone). Country-level US↔US-timezone consistency already holds via the
# identity; this closes the finer state/offset gap. OPT-IN (default off)
# because BrightData state targeting needs a plan that supports it — enable
# after confirming, and it fails safe (a proxy error is caught by fetch()).
_MATCH_GEO = os.environ.get("RESIDENTIAL_RENDER_MATCH_GEO", "false").strip().lower() in (
    "true",
    "1",
)

# Total time to let a Cloudflare JS/managed challenge auto-clear before we
# give up (and abort — never interact). CF JS challenges typically clear in
# 3–8s; 12s leaves margin without hanging the run.
_CHALLENGE_WAIT_MS = int(os.environ.get("RESIDENTIAL_RENDER_CHALLENGE_WAIT_MS", "12000"))
# Poll interval while waiting for the challenge to clear.
_POLL_MS = int(os.environ.get("RESIDENTIAL_RENDER_POLL_MS", "1000"))
# Settle time after navigation before the first challenge check (lets SPA
# JS + any CF interstitial begin executing).
_SETTLE_MS = int(os.environ.get("RESIDENTIAL_RENDER_SETTLE_MS", "2000"))
# Hard cap on navigation.
_NAV_TIMEOUT_MS = int(os.environ.get("RESIDENTIAL_RENDER_NAV_TIMEOUT_MS", "30000"))


async def resolve_challenge(
    get_html: Callable[[], Awaitable[bytes]],
    *,
    wait_ms: int = _CHALLENGE_WAIT_MS,
    poll_ms: int = _POLL_MS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[ChallengeKind, bytes, str | None]:
    """Render-outcome decision loop — the testable core of the 2a policy.

    Fetches the current rendered HTML via ``get_html`` and classifies it:

      * ``NONE``        → return immediately (real content).
      * ``INTERACTIVE`` → return immediately (caller ABORTS; never solve).
      * ``PASSABLE_JS`` → poll up to ``wait_ms`` for the site's JS to clear
        it. Returns ``NONE`` the moment it clears, ``INTERACTIVE`` if it
        turns into a human-captcha mid-wait, or ``PASSABLE_JS`` if it never
        cleared (caller treats a never-cleared challenge as blocked and
        still does NOT interact).

    Pure w.r.t. the browser: ``get_html`` and ``sleep`` are injected so this
    is exercised in tests without a real page. Never interacts with the page.
    """
    html = await get_html()
    kind, provider = classify_challenge(html)
    if kind is not ChallengeKind.PASSABLE_JS:
        return kind, html, provider

    # Wait for the site's own JS to clear the interstitial. We only ever
    # re-read the DOM — we never click or solve.
    waited = 0
    poll_s = max(poll_ms, 0) / 1000.0
    while waited < wait_ms:
        await sleep(poll_s)
        waited += poll_ms if poll_ms > 0 else wait_ms
        html = await get_html()
        kind, provider = classify_challenge(html)
        if kind is not ChallengeKind.PASSABLE_JS:
            return kind, html, provider
    return ChallengeKind.PASSABLE_JS, html, provider


class ResidentialRenderProvider:
    """FetchProvider: real-browser render through a residential proxy.

    Passes JS challenges by waiting; aborts on interactive captchas; never
    uses a stealth browser or a challenge solver. See module docstring for
    the legal posture. ``fetch()`` never raises.
    """

    tier_name: str = "RESIDENTIAL_RENDER"

    def __init__(self, *, pool: object | None = None, proxy_provider: object | None = None) -> None:
        """Args:
            pool: Optional injected browser-context pool (tests). Production
                lazily builds a ``BrowserContextPool``.
            proxy_provider: Optional injected residential proxy provider
                (tests). Production lazily builds ``BrightDataProvider``.
        """
        self._pool = pool
        self._proxy_provider = proxy_provider

    def _get_pool(self) -> object:
        if self._pool is None:
            from ma_poc.fetch.browser_pool import BrowserContextPool

            max_ctx = int(os.environ.get("RESIDENTIAL_RENDER_MAX_CONTEXTS", "4"))
            # driver="playwright" (vanilla, NOT patchright) is the 2a
            # requirement — this tier must not ship an anti-detection browser.
            self._pool = BrowserContextPool(
                max_contexts=max_ctx,
                driver="playwright",
                engine=_ENGINE,
                headless=_HEADLESS,
                channel=_CHANNEL,
            )
        return self._pool

    def _pick_identity(self, sticky_key: str) -> object:
        """Pick a UA identity that MATCHES the launched engine (Firefox UA for
        a Firefox engine, Chrome UA for Chromium) so engine and UA agree."""
        if _ENGINE == "firefox":
            return _IDENTITIES.pick_family(("firefox",), sticky_key)
        return _IDENTITIES.pick_chrome_only(sticky_key)

    def _residential_proxy(self, canonical_id: str, identity: object | None = None) -> object:
        if self._proxy_provider is None:
            from ma_poc.fetch.proxy.brightdata import BrightDataProvider

            self._proxy_provider = BrightDataProvider()
        from ma_poc.fetch.proxy.base import ProxyTier

        # Honest fix #1: when enabled, target the exit IP's region to the
        # sticky identity's timezone offset so IP and browser timezone agree.
        state: str | None = None
        if _MATCH_GEO and identity is not None:
            from ma_poc.fetch.stealth import brightdata_state_for_timezone

            tz = getattr(identity, "timezone_id", "") or ""
            state = brightdata_state_for_timezone(tz)
        return self._proxy_provider.get_config(  # type: ignore[attr-defined]
            tier=ProxyTier.RESIDENTIAL, canonical_id=canonical_id, state=state
        )

    async def fetch(
        self,
        task: CrawlTask,
        profile: ScrapeProfile,
    ) -> FetchResult:
        start_ms = _now_ms()
        pool = self._get_pool()
        identity = self._pick_identity(task.property_id)
        proxy_cfg = self._residential_proxy(task.property_id, identity)

        page = None
        try:
            page = await pool.acquire(identity, proxy=proxy_cfg)  # type: ignore[attr-defined]
            network_log: list[dict[str, object]] = []
            _attach_network_capture(page, network_log)

            await page.goto(task.url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            # Let the SPA + any CF interstitial start executing before the
            # first challenge check.
            await asyncio.sleep(_SETTLE_MS / 1000.0)

            async def _current_html() -> bytes:
                try:
                    return (await page.content()).encode("utf-8", "replace")
                except Exception:
                    return b""

            kind, html, provider = await resolve_challenge(_current_html)
            final_url = _safe_url(page, task.url)
            clearance = await _harvest_clearance(page)

            if kind is ChallengeKind.NONE:
                return _stamp(
                    _ok_result(task, html, final_url, network_log, clearance, start_ms),
                    attempts=[int(_TIER)],
                )

            # INTERACTIVE captcha, or a JS challenge that never auto-cleared
            # → we do NOT interact. Return BOT_BLOCKED so the orchestrator
            # carries forward / marks out-of-scope.
            log.info(
                "residential_render.abort property=%s kind=%s provider=%s url=%s",
                task.property_id, kind.value, provider, task.url,
            )
            return _stamp(
                _blocked_result(task, html, final_url, provider, start_ms),
                attempts=[int(_TIER)],
            )
        except Exception as exc:  # never raise across the provider boundary
            outcome = (
                FetchOutcome.TRANSIENT
                if _is_transient(exc)
                else FetchOutcome.HARD_FAIL
            )
            log.warning(
                "residential_render.error property=%s url=%s %s: %s",
                task.property_id, task.url, type(exc).__name__, exc,
            )
            return _stamp(
                FetchResult(
                    url=task.url,
                    outcome=outcome,
                    status=None,
                    body=None,
                    headers={},
                    render_mode=RenderMode.RENDER,
                    final_url=task.url,
                    attempts=1,
                    elapsed_ms=_now_ms() - start_ms,
                    error_signature=f"render:{type(exc).__name__}",
                ),
                attempts=[int(_TIER)],
            )
        finally:
            if page is not None:
                try:
                    await pool.release(page)  # type: ignore[attr-defined]
                except Exception as exc:
                    log.warning("residential_render.release_error: %s", exc)


# ── result builders ─────────────────────────────────────────────────────────


def _ok_result(
    task: CrawlTask,
    html: bytes,
    final_url: str,
    network_log: list[dict[str, object]],
    clearance: dict[str, str],
    start_ms: int,
) -> FetchResult:
    return FetchResult(
        url=task.url,
        outcome=FetchOutcome.OK,
        status=200,
        body=html,
        headers={"content-type": "text/html; charset=utf-8"},
        render_mode=RenderMode.RENDER,
        final_url=final_url,
        attempts=1,
        elapsed_ms=_now_ms() - start_ms,
        network_log=network_log,
        clearance_cookies=clearance,
    )


def _blocked_result(
    task: CrawlTask,
    html: bytes,
    final_url: str,
    provider: str | None,
    start_ms: int,
) -> FetchResult:
    return FetchResult(
        url=task.url,
        outcome=FetchOutcome.BOT_BLOCKED,
        status=403,
        body=html or None,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url=final_url,
        attempts=1,
        elapsed_ms=_now_ms() - start_ms,
        captcha_detected=True,
        block_signature=f"render_abort:{provider}" if provider else "render_abort",
    )


# ── browser helpers ─────────────────────────────────────────────────────────

_JSONISH_RE = re.compile(r"application/(?:json|.*\+json)|text/json", re.IGNORECASE)
_MAX_CAPTURED = 40
_MAX_BODY_BYTES = 2_000_000


def _attach_network_capture(page: object, sink: list[dict[str, object]]) -> None:
    """Record JSON XHR/fetch responses so Tier-1 API interception still works
    on the rendered page. Defensive — any capture error is swallowed and
    never affects the fetch."""
    # Hold strong references to in-flight capture tasks. Without this, the
    # event loop can garbage-collect a fire-and-forget task before it runs
    # (the documented ``asyncio.ensure_future`` footgun), silently dropping
    # captures. Tasks discard themselves on completion.
    pending: set[object] = set()

    async def _on_response(resp: object) -> None:
        try:
            if len(sink) >= _MAX_CAPTURED:
                return
            headers = await resp.all_headers()  # type: ignore[attr-defined]
            ct = headers.get("content-type", "")
            if not _JSONISH_RE.search(ct):
                return
            body = await resp.body()  # type: ignore[attr-defined]
            if not body or len(body) > _MAX_BODY_BYTES:
                return
            sink.append(
                {
                    "url": resp.url,  # type: ignore[attr-defined]
                    "status": resp.status,  # type: ignore[attr-defined]
                    "content_type": ct,
                    "body": body.decode("utf-8", "replace"),
                }
            )
        except Exception:
            return

    def _spawn(r: object) -> None:
        task = asyncio.ensure_future(_on_response(r))
        pending.add(task)
        task.add_done_callback(pending.discard)

    try:
        page.on("response", _spawn)  # type: ignore[attr-defined]
    except Exception as exc:
        log.debug("residential_render.network_capture_install_failed: %s", exc)


async def _harvest_clearance(page: object) -> dict[str, str]:
    """Return the ordinary post-challenge cookies the browser now holds
    (cf_clearance / __cf_bm / datadome / __ddg* / incap*). Same-run,
    same-IP reuse only — never raises."""
    wanted = ("cf_clearance", "__cf_bm", "datadome", "__ddg", "incap_ses", "visid_incap")
    out: dict[str, str] = {}
    try:
        cookies = await page.context.cookies()  # type: ignore[attr-defined]
        for c in cookies:
            name = c.get("name", "")
            if any(name.startswith(w) for w in wanted):
                out[name] = c.get("value", "")
    except Exception:
        return {}
    return out


def _safe_url(page: object, fallback: str) -> str:
    try:
        return page.url or fallback  # type: ignore[attr-defined]
    except Exception:
        return fallback


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return (
        "timeout" in name
        or "timeout" in msg
        or "err_connection" in msg
        or "err_network" in msg
        or "err_timed_out" in msg
    )


def _stamp(result: FetchResult, attempts: list[int]) -> FetchResult:
    import dataclasses

    return dataclasses.replace(
        result, fetch_tier_used=int(_TIER), fetch_tier_attempts=attempts
    )


def _now_ms() -> int:
    return int(time.time() * 1000)
