"""HyperbrowserProvider — the Hyperbrowser cloud-browser fetch backend.

A drop-in ``FetchProvider`` that swaps in BEHIND the RESIDENTIAL_RENDER /
UNLOCKER rungs when ``FETCH_BACKEND=hyperbrowser`` (see
``config.feature_flags.hb_enabled``). It renders the target page in a
Hyperbrowser session (basic stealth + residential proxy — **CAPTCHA solving
hard-disabled**, see ``_session_options``), captures the XHR/fetch JSON into
``network_log`` in the exact shape the Tier-1 API-interception path reads, and
returns the same ``FetchResult`` contract every other provider does.

Compliance posture (RealPage legal, 2026-07-22): this is a CLEAN residential-
render backend only — it passes JS challenges by *waiting* like any visitor's
browser and returns BOT_BLOCKED on an unsolved challenge; it NEVER solves a
CAPTCHA or otherwise overcomes a barrier to access.

Design notes (see the integration map, task #46):
  * **Never raises** (Protocol contract). Every failure → a FetchResult.
  * **Always stops the session** in ``finally`` — a missed stop leaks a paid
    session. Billing settles async, so credit accounting is OUT of band: we
    emit the session id on a telemetry event, not block the FetchResult.
  * **Resource-blocked**: aborts image/media/font/stylesheet in the remote
    browser BEFORE the request hits the proxy (~-70% proxy MB, measured).
    NEVER blocks document/xhr/fetch/JSON — that is the network_log.
  * **Empty ``clearance_cookies``**: HB owns the exit IP; the local curl_cffi
    clearance-reuse runs on the GCP worker IP, so HB cookies are inert-to-
    counterproductive across the egress boundary. Emit {} deliberately.
  * **Per-property cost cap** (``HYPERBROWSER_MAX_CALLS_PER_PROPERTY``),
    mirroring the Web-Unlocker cap — the link-hop crawl fires multiple
    fetches/property and HB is billed per session.

The API key (``HYPERBROWSER_API_KEY``) is read lazily and never logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from typing import TYPE_CHECKING, Any

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.models.fetch_tier import FetchTier

if TYPE_CHECKING:
    from ma_poc.discovery.contracts import CrawlTask
    from ma_poc.models.scrape_profile import ScrapeProfile

log = logging.getLogger(__name__)

_TIER = FetchTier.HYPERBROWSER
_API_BASE = os.environ.get("HYPERBROWSER_API_BASE", "https://api.hyperbrowser.ai/api").rstrip("/")

# Resource types aborted in the remote browser (never document/xhr/fetch).
_BLOCK_TYPES = frozenset({"image", "media", "font", "stylesheet"})
# JSON/text content types promoted to network_log (Tier-1 API surface). Mirror
# residential_render's JSON-ish gate; caps match the canonical fetcher.
_JSONISH_RE = re.compile(r"application/(?:json|.*\+json)|text/json", re.IGNORECASE)
_CAPTURE_CT_RE = re.compile(r"json|xml|html|text", re.IGNORECASE)
_MAX_CAPTURED = 40
_MAX_JSON_BYTES = 512_000
_CF_TITLE_RE = re.compile(r"just a moment|verify you are human|checking your browser", re.IGNORECASE)

_NAV_TIMEOUT_MS = int(os.environ.get("HB_NAV_TIMEOUT_MS", "40000"))
_SETTLE_MS = int(os.environ.get("HB_SETTLE_MS", "5000"))
_CONNECT_TIMEOUT_MS = int(os.environ.get("HB_CONNECT_TIMEOUT_MS", "60000"))


# ── per-property call cap (mirrors unlocker._wu_* cost guard) ────────────────
_HB_PROP_LOCK = threading.Lock()
_hb_prop_counts: dict[str, int] = {}


def _hb_prop_cap() -> int:
    """``HYPERBROWSER_MAX_CALLS_PER_PROPERTY`` — 0/unset/invalid = no cap."""
    raw = os.getenv("HYPERBROWSER_MAX_CALLS_PER_PROPERTY", "").strip()
    if not raw:
        return 0
    try:
        v = int(raw)
        return v if v > 0 else 0
    except ValueError:
        return 0


def _hb_try_reserve_property(property_id: str) -> bool:
    """Reserve one HB session for *property_id*. False when the per-property
    cap is exhausted. Atomic; empty property_id is never capped."""
    cap = _hb_prop_cap()
    if not cap or not property_id:
        return True
    with _HB_PROP_LOCK:
        n = _hb_prop_counts.get(property_id, 0)
        if n >= cap:
            return False
        _hb_prop_counts[property_id] = n + 1
        return True


def reset_hyperbrowser_property_counts() -> None:
    """Clear the per-property counter. For tests / per-job init only."""
    with _HB_PROP_LOCK:
        _hb_prop_counts.clear()


def hyperbrowser_property_call_count(property_id: str) -> int:
    """Current per-property HB session count (telemetry / tests)."""
    with _HB_PROP_LOCK:
        return _hb_prop_counts.get(property_id, 0)


def _hb_api_key() -> str:
    """Hyperbrowser API token from ``HYPERBROWSER_API_KEY``, or ''. Never log."""
    return os.environ.get("HYPERBROWSER_API_KEY", "").strip()


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _session_options(mode: str) -> dict[str, bool]:
    """POST /api/session body.

    ``solveCaptchas`` is HARD-DISABLED (not env-configurable). RealPage legal +
    engineering ruled CAPTCHA solving a "no-go zone — violates the rules of the
    scrape" (email thread 2026-07-22, Chris Housewright). Overcoming a barrier
    to access (CAPTCHA/login/clickwrap) is out of scope. HB is used ONLY as a
    clean residential-render backend: real browser + residential proxy that
    passes JS challenges by *waiting* (what any visitor's browser does), never
    a solver. Do NOT re-add an enable path without written client sign-off.

    ``useStealth`` = HB *basic* stealth (a normal real-browser fingerprint,
    which legal deemed acceptable) — NOT account-level advanced/anti-detection
    stealth. Proxy defaults on (residential is a legally-available product);
    ``HB_USE_PROXY`` can turn it off for non-walled render tasks.
    """
    return {
        "solveCaptchas": False,  # compliance: hard no-go, never enable
        "useStealth": _bool_env("HB_USE_STEALTH", True),
        "useProxy": _bool_env("HB_USE_PROXY", True),
        "adblock": _bool_env("HB_ADBLOCK", True),
    }


class _HbSession:
    """A single Hyperbrowser cloud-browser session: create → connect CDP →
    (caller renders) → stop. ``session_id`` is set the instant create returns
    so ``close()`` can always stop it, even if the CDP connect later fails."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.session_id: str | None = None
        self._pw: Any = None
        self._browser: Any = None

    async def open(self) -> Any:
        """Create the HB session, connect Playwright over CDP, return a page."""
        import httpx
        from playwright.async_api import async_playwright

        key = _hb_api_key()
        if not key:
            raise RuntimeError("HYPERBROWSER_API_KEY not set")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_API_BASE}/session",
                headers={"x-api-key": key, "Content-Type": "application/json"},
                json=_session_options(self.mode),
            )
            resp.raise_for_status()
            data = resp.json()
        self.session_id = data.get("id")
        ws = data.get("wsEndpoint")
        if not self.session_id or not ws:
            raise RuntimeError("hyperbrowser session missing id/wsEndpoint")
        token = data.get("token")
        if token and "token=" not in ws:
            ws += ("&" if "?" in ws else "?") + "token=" + token
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(ws, timeout=_CONNECT_TIMEOUT_MS)
        ctx = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        return page

    async def close(self) -> None:
        """Close the browser/Playwright and ALWAYS stop the HB session. Each
        step best-effort so one failure never blocks the paid-session stop."""
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception as exc:
            log.debug("hb.browser_close_error: %s", exc)
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception as exc:
            log.debug("hb.playwright_stop_error: %s", exc)
        if self.session_id:
            try:
                import httpx

                async with httpx.AsyncClient(timeout=20.0) as client:
                    await client.put(
                        f"{_API_BASE}/session/{self.session_id}/stop",
                        headers={"x-api-key": _hb_api_key()},
                    )
            except Exception as exc:
                log.warning("hb.session_stop_failed id=%s: %s", self.session_id, exc)


class HyperbrowserProvider:
    """FetchProvider: render + XHR-capture via a Hyperbrowser cloud browser.

    ``mode`` is a label for cost attribution ("unlock" | "render"); both render
    and capture identically. ``fetch()`` never raises and always stops the
    session. Inject ``session_factory`` in tests to avoid the network.
    """

    tier_name: str = "HYPERBROWSER"

    def __init__(
        self,
        *,
        mode: str = "unlock",
        session_factory: Any = None,
    ) -> None:
        self.mode = mode
        # Default: a real HB session. Tests inject a fake with open()/close().
        self._session_factory = session_factory or (lambda: _HbSession(mode))

    async def fetch(
        self,
        task: CrawlTask,
        profile: ScrapeProfile | None,
    ) -> FetchResult:
        # profile may be None (the _try_unlocker_fallback seam passes None).
        start_ms = _now_ms()
        # Per-property cost cap: skip without opening a (paid) session.
        if not _hb_try_reserve_property(getattr(task, "property_id", "")):
            log.info("hb.prop_cap_exhausted property=%s", getattr(task, "property_id", ""))
            return _stamp(_capped_result(task, start_ms))

        session = self._session_factory()
        page: Any = None
        try:
            page = await session.open()
            network_log: list[dict[str, Any]] = []
            _install_block(page)
            _install_capture(page, network_log)

            await page.goto(task.url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            await asyncio.sleep(_SETTLE_MS / 1000.0)

            body_text = await page.content()
            title = ""
            try:
                title = await page.title()
            except Exception:
                title = ""
            final_url = _safe_url(page, task.url)

            body = (body_text or "").encode("utf-8", "replace")
            _emit_session_event(task, session, self.mode)

            # A persistent CF interstitial (title == "Just a moment…") with a
            # tiny body = unsolved challenge → BOT_BLOCKED so the cascade
            # carries forward. Otherwise OK (adapters read body + network_log).
            if _CF_TITLE_RE.search(title) and len(body) < 8000 and not network_log:
                return _stamp(_blocked_result(task, body, final_url, start_ms))
            return _stamp(_ok_result(task, body, final_url, network_log, start_ms))
        except Exception as exc:  # never raise across the provider boundary
            outcome = FetchOutcome.TRANSIENT if _is_transient(exc) else FetchOutcome.HARD_FAIL
            log.warning(
                "hb.error property=%s url=%s %s: %s",
                getattr(task, "property_id", ""), task.url, type(exc).__name__, exc,
            )
            return _stamp(_error_result(task, outcome, exc, start_ms))
        finally:
            try:
                await session.close()  # ALWAYS stop the paid session
            except Exception as exc:
                log.warning("hb.session_close_error: %s", exc)


# ── page instrumentation ────────────────────────────────────────────────────


def _install_block(page: Any) -> None:
    """Abort image/media/font/stylesheet in the remote browser (pre-proxy, so
    the bytes are never billed). Never blocks document/xhr/fetch/JSON."""
    async def _router(route: Any) -> None:
        try:
            if getattr(route.request, "resource_type", "") in _BLOCK_TYPES:
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    try:
        maybe = page.route("**/*", _router)
        if asyncio.iscoroutine(maybe):
            asyncio.ensure_future(maybe)
    except Exception as exc:
        log.debug("hb.block_install_failed: %s", exc)


def _install_capture(page: Any, sink: list[dict[str, Any]]) -> None:
    """Record JSON/text XHR responses into ``sink`` in the EXACT shape
    scraper.py promotes to ctx._api_responses: {url, status, content_type,
    body(str), captcha_detected}. Body must be str (json.loads'd downstream)."""
    pending: set[Any] = set()

    async def _on_response(resp: Any) -> None:
        try:
            if len(sink) >= _MAX_CAPTURED:
                return
            headers = await resp.all_headers()
            ct = headers.get("content-type", "")
            if not _CAPTURE_CT_RE.search(ct):
                return
            # Only JSON/XML get the small cap + are the Tier-1 surface; skip
            # bulky html/text (the main document is already in body).
            if not _JSONISH_RE.search(ct) and "xml" not in ct.lower():
                return
            body = await resp.body()
            if not body or len(body) > _MAX_JSON_BYTES:
                return
            sink.append(
                {
                    "url": resp.url,
                    "status": resp.status,
                    "content_type": ct,
                    "body": body.decode("utf-8", "replace"),
                    "captcha_detected": False,
                }
            )
        except Exception:
            return

    def _spawn(r: Any) -> None:
        fut = asyncio.ensure_future(_on_response(r))
        pending.add(fut)
        fut.add_done_callback(pending.discard)

    try:
        page.on("response", _spawn)
    except Exception as exc:
        log.debug("hb.capture_install_failed: %s", exc)


# ── result builders ─────────────────────────────────────────────────────────


def _ok_result(
    task: CrawlTask,
    body: bytes,
    final_url: str,
    network_log: list[dict[str, Any]],
    start_ms: int,
) -> FetchResult:
    return FetchResult(
        url=task.url,
        outcome=FetchOutcome.OK,
        status=200,
        body=body,
        headers={"content-type": "text/html; charset=utf-8"},
        render_mode=RenderMode.RENDER,
        final_url=final_url,
        attempts=1,
        elapsed_ms=_now_ms() - start_ms,
        network_log=network_log,
        clearance_cookies={},  # cross-egress: HB IP != local curl_cffi IP
    )


def _blocked_result(task: CrawlTask, body: bytes, final_url: str, start_ms: int) -> FetchResult:
    return FetchResult(
        url=task.url,
        outcome=FetchOutcome.BOT_BLOCKED,
        status=403,
        body=body or None,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url=final_url,
        attempts=1,
        elapsed_ms=_now_ms() - start_ms,
        captcha_detected=True,
        block_signature="hb_challenge_unsolved",
    )


def _error_result(task: CrawlTask, outcome: FetchOutcome, exc: Exception, start_ms: int) -> FetchResult:
    return FetchResult(
        url=task.url,
        outcome=outcome,
        status=None,
        body=None,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url=task.url,
        attempts=1,
        elapsed_ms=_now_ms() - start_ms,
        error_signature=f"hb:{type(exc).__name__}",
    )


def _capped_result(task: CrawlTask, start_ms: int) -> FetchResult:
    """Synthetic BOT_BLOCKED when the per-property HB budget is spent — no
    session opened, so no spend (mirrors the Unlocker cap behaviour)."""
    return FetchResult(
        url=task.url,
        outcome=FetchOutcome.BOT_BLOCKED,
        status=None,
        body=None,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url=task.url,
        attempts=0,
        elapsed_ms=_now_ms() - start_ms,
        block_signature="hb_property_cap",
    )


# ── helpers ─────────────────────────────────────────────────────────────────


def _emit_session_event(task: CrawlTask, session: _HbSession, mode: str) -> None:
    """Emit the session id for OUT-of-band credit reconciliation (HB bills
    settle async, so we never block the FetchResult on cost)."""
    try:
        from ma_poc.observability.events import EventKind, emit

        emit(
            EventKind.FETCH_TIER_ESCALATED,
            getattr(task, "property_id", "") or "unknown",
            tier="HYPERBROWSER",
            reason="hb_backend",
            hb_session_id=getattr(session, "session_id", None),
            hb_mode=mode,
        )
    except Exception:
        pass  # observability is best-effort


def _safe_url(page: Any, fallback: str) -> str:
    try:
        return page.url or fallback
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
        or "connect" in name
    )


def _stamp(result: FetchResult) -> FetchResult:
    import dataclasses

    return dataclasses.replace(result, fetch_tier_used=int(_TIER), fetch_tier_attempts=[int(_TIER)])


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── in-page raw GET (task #21 warm=fast direct-GET shortcuts) ────────────────
# HB's residential proxy is BROWSER-ONLY (no standalone endpoint for curl). To
# get a RAW parseable response (JSON / raw server HTML) through it — NOT the
# mutated rendered DOM that page.content() returns, and which HB's network_log
# does not capture for a top-level navigation — navigate the HB browser to the
# resource's OWN origin (CF cleared by the residential proxy) then run a
# SAME-ORIGIN fetch() in-page. A cross-origin fetch throws "Failed to fetch"
# (CORS), so the goto MUST land on the resource origin and the fetch uses a
# same-origin relative path. Returns the raw body through HB's residential IP,
# free (no BrightData — Ankur's standing HB-over-BrightData preference).
_INPAGE_FETCH_JS = """async (p) => {
  try {
    const r = await fetch(p, {headers: {'Accept': 'application/json, text/html'}, credentials: 'include'});
    return {status: r.status, body: await r.text()};
  } catch (e) { return {status: -1, body: ''}; }
}"""


async def hb_raw_get(
    url: str, property_id: str = "?", *, session_factory: Any = None
) -> tuple[int, str]:
    """Fetch *url* as a RAW response through HB's residential proxy via an
    in-page same-origin fetch (see _INPAGE_FETCH_JS). Returns ``(status, body)``
    — ``(0, "")`` on any failure. Never raises; always stops the HB session.

    ``session_factory`` lets tests inject a fake _HbSession-shaped object.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    rel = parts.path + (("?" + parts.query) if parts.query else "")
    sess = (session_factory or (lambda: _HbSession(mode="render")))()
    try:
        page = await sess.open()
        await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        res = await page.evaluate(_INPAGE_FETCH_JS, rel)
        status = int((res or {}).get("status") or 0)
        body = (res or {}).get("body") or ""
        return (status, body) if status > 0 else (0, "")
    except Exception as exc:
        log.debug("hb_raw_get failed for %s (%s): %s", url, property_id, exc)
        return 0, ""
    finally:
        try:
            await sess.close()
        except Exception as exc:
            log.debug("hb_raw_get session close failed: %s", exc)
