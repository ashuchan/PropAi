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
from urllib.parse import urljoin, urlsplit

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.response_classifier import exception_signature
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
_RENTMANAGER_INVENTORY_PATH_RE = re.compile(
    r"/(?:unit-availability|available-units?)/?$",
    re.IGNORECASE,
)

_NAV_TIMEOUT_MS = int(os.environ.get("HB_NAV_TIMEOUT_MS", "40000"))
_SETTLE_MS = int(os.environ.get("HB_SETTLE_MS", "5000"))
_CONNECT_TIMEOUT_MS = int(os.environ.get("HB_CONNECT_TIMEOUT_MS", "60000"))
_LOCAL_CLOSE_TIMEOUT_SECONDS = float(os.environ.get("HB_LOCAL_CLOSE_TIMEOUT_SECONDS", "5"))


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


def _hb_try_reserve_property(
    property_id: str,
    *,
    cap_override: int | None = None,
) -> bool:
    """Reserve one HB session for *property_id*. False when the per-property
    cap is exhausted. Atomic; empty property_id is never capped.

    ``cap_override`` lets a stricter call site enforce its own ceiling without
    weakening the process-wide environment cap for ordinary callers.
    """
    configured_cap = _hb_prop_cap()
    if cap_override is None:
        cap = configured_cap
    elif configured_cap:
        cap = min(configured_cap, max(0, int(cap_override)))
    else:
        cap = max(0, int(cap_override))
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
    # Compliance runs also force HB's optional stealth layer off even if a
    # stale process environment requests it. CAPTCHA solving is independently
    # hard-disabled below and has no enable path.
    from ma_poc.config.feature_flags import compliance_mode

    return {
        "solveCaptchas": False,  # compliance: hard no-go, never enable
        "useStealth": False if compliance_mode() else _bool_env("HB_USE_STEALTH", True),
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
        step is time-bounded and best-effort so a wedged local CDP transport
        never blocks the paid-session stop call."""
        try:
            if self._browser is not None:
                await asyncio.wait_for(
                    self._browser.close(),
                    timeout=_LOCAL_CLOSE_TIMEOUT_SECONDS,
                )
        except TimeoutError:
            log.warning(
                "hb.browser_close_timeout id=%s timeout_seconds=%s",
                self.session_id,
                _LOCAL_CLOSE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            log.debug("hb.browser_close_error: %s", exc)
        try:
            if self._pw is not None:
                await asyncio.wait_for(
                    self._pw.stop(),
                    timeout=_LOCAL_CLOSE_TIMEOUT_SECONDS,
                )
        except TimeoutError:
            log.warning(
                "hb.playwright_stop_timeout id=%s timeout_seconds=%s",
                self.session_id,
                _LOCAL_CLOSE_TIMEOUT_SECONDS,
            )
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

            # RentManager sites behind a JS challenge often expose the exact
            # unit roster one published nav-hop from the homepage.  A normal
            # link-hop would need a *second* paid HB session and is correctly
            # denied by the per-property cap.  Reuse the already-open browser
            # instead: only follow an explicit same-origin
            # ``/unit-availability`` anchor on a page that also publishes a
            # RentManager fingerprint, and adopt the response only when it
            # contains the native-card shape.  No URL guessing, no extra
            # session, no CAPTCHA solver.
            inventory_url = _published_rentmanager_inventory_url(
                body_text or "",
                final_url,
            )
            if inventory_url:
                follow_status, follow_body = await _same_origin_raw_get(
                    page,
                    inventory_url,
                    final_url,
                )
                if (
                    200 <= follow_status < 300
                    and _has_rentmanager_native_cards(follow_body)
                ):
                    body_text = follow_body
                    final_url = inventory_url

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
                getattr(task, "property_id", ""),
                task.url,
                type(exc).__name__,
                exc,
            )
            return _stamp(_error_result(task, outcome, exc, start_ms))
        finally:
            try:
                await session.close()  # ALWAYS stop the paid session
            except Exception as exc:
                log.warning("hb.session_close_error: %s", exc)


# ── page instrumentation ────────────────────────────────────────────────────


def _published_rentmanager_inventory_url(html: str, current_url: str) -> str:
    """Exact same-origin RentManager unit-roster anchor from rendered HTML."""
    if not html or not current_url:
        return ""
    lower = html.casefold()
    if "rentmanager.com" not in lower and "rmwb_" not in lower:
        return ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        current = urlsplit(current_url)
    except Exception:
        return ""
    if current.scheme not in {"http", "https"} or not current.netloc:
        return ""
    expected_host = (current.hostname or "").casefold().removeprefix("www.")
    for anchor in soup.find_all("a", href=True):
        candidate = urljoin(current_url, str(anchor.get("href") or "").strip())
        try:
            parsed = urlsplit(candidate)
        except Exception:
            continue
        observed_host = (parsed.hostname or "").casefold().removeprefix("www.")
        if observed_host != expected_host:
            continue
        if not _RENTMANAGER_INVENTORY_PATH_RE.search(parsed.path or ""):
            continue
        return candidate.split("#", 1)[0]
    return ""


def _has_rentmanager_native_cards(html: str) -> bool:
    """Strong native-card shape required before replacing the fetched page."""
    lower = (html or "").casefold()
    return bool(
        "rmwb_listing-wrapper" in lower
        and re.search(r"[?&]uid=\d+", lower)
        and "rmwb_info-title" in lower
        and "rent" in lower
    )


async def _same_origin_raw_get(
    page: Any,
    target_url: str,
    current_url: str,
) -> tuple[int, str]:
    """In-page raw GET after enforcing exact scheme+authority equality."""
    try:
        target = urlsplit(target_url)
        current = urlsplit(current_url)
    except Exception:
        return 0, ""
    if (target.scheme, target.netloc) != (current.scheme, current.netloc):
        return 0, ""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return 0, ""
    relative = target.path + (("?" + target.query) if target.query else "")
    try:
        response = await evaluate(_INPAGE_FETCH_JS, relative)
        status = int((response or {}).get("status") or 0)
        body = str((response or {}).get("body") or "")
        return status, body
    except Exception:
        return 0, ""


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
        # 2026-07-27: type name alone renders every Playwright failure as
        # "hb:Error" (its base class is literally named ``Error``) — the same
        # blind spot that made the dead-proxy outage opaque. Keep the message.
        error_signature=exception_signature(exc, prefix="hb"),
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
    url: str,
    property_id: str = "?",
    *,
    session_factory: Any = None,
    max_calls_per_property: int | None = None,
) -> tuple[int, str]:
    """Fetch *url* as a RAW response through HB's residential proxy via an
    in-page same-origin fetch (see _INPAGE_FETCH_JS). Returns ``(status, body)``
    — ``(0, "")`` on any failure. Never raises; always stops the HB session.

    ``session_factory`` lets tests inject a fake _HbSession-shaped object.

    The raw shortcut shares the same per-property paid-session cap as
    :class:`HyperbrowserProvider`.  Before this guard, every direct shortcut
    (SecureCafe, SightMap, RealPage) bypassed
    ``HYPERBROWSER_MAX_CALLS_PER_PROPERTY`` entirely even though each call
    opens the same billable session.  A capped call returns ``(0, "")`` so all
    callers take their existing fail-open render path without spending.
    """
    from urllib.parse import urlsplit

    # ``?`` is the historical unknown-property sentinel.  Do not make every
    # unknown diagnostic share one global cap bucket; real runner calls always
    # supply a canonical property id and are bounded together with provider
    # calls through the module-level counter.
    cap_pid = str(property_id or "")
    if cap_pid == "?":
        cap_pid = ""
    if not _hb_try_reserve_property(
        cap_pid,
        cap_override=max_calls_per_property,
    ):
        log.info("hb.raw_get_prop_cap_exhausted property=%s", property_id)
        return 0, ""

    original_parts = urlsplit(url)
    original_rel = original_parts.path + (
        ("?" + original_parts.query) if original_parts.query else ""
    )
    sess = (session_factory or (lambda: _HbSession(mode="render")))()
    try:
        page = await sess.open()
        await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        # ``goto`` may redirect across origins (for example a legacy vanity
        # domain to the operator's exact property page).  The browser is now
        # on the landing origin, so fetching the *original* relative path can
        # silently return that origin's homepage/404.  Rebuild the same-origin
        # path from the actual landing URL, retaining the original path only
        # when the page does not expose a valid HTTP(S) URL.
        landing_parts = urlsplit(_safe_url(page, url))
        if landing_parts.scheme in {"http", "https"} and landing_parts.netloc:
            rel = landing_parts.path + (
                ("?" + landing_parts.query) if landing_parts.query else ""
            )
        else:
            rel = original_rel
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


async def hb_raw_get_then(
    url: str,
    property_id: str,
    followup_url_from_body: Any,
    *,
    session_factory: Any = None,
) -> tuple[tuple[int, str], tuple[str, int, str] | None]:
    """Fetch *url* and one body-derived same-origin follow-up in one session.

    This is the bounded two-hop counterpart to :func:`hb_raw_get`.  It exists
    for public pages where an index must be read before the exact detail URL is
    known (for example, RentCafe ``/floorplans`` ->
    ``/floorplans/<plan-slug>``).  The caller supplies a synchronous selector
    that receives the first raw body and returns the follow-up URL, or a falsey
    value to stop after the first response.

    One invocation reserves exactly one paid session against the shared
    per-property cap.  The follow-up is rejected unless its scheme + authority
    exactly match the first URL, so a page cannot turn this helper into an
    arbitrary cross-origin fetch.  CAPTCHA solving remains hard-disabled by
    :class:`_HbSession`; the session is always stopped.  Failures are returned
    as empty status/body values and never raised.
    """
    from urllib.parse import urljoin, urlsplit

    cap_pid = str(property_id or "")
    if cap_pid == "?":
        cap_pid = ""
    if not _hb_try_reserve_property(cap_pid):
        log.info("hb.raw_get_then_prop_cap_exhausted property=%s", property_id)
        return (0, ""), None

    first_parts = urlsplit(url)
    if first_parts.scheme not in {"http", "https"} or not first_parts.netloc:
        return (0, ""), None

    async def _raw_get(page: Any, target: str) -> tuple[int, str]:
        parts = urlsplit(target)
        if (parts.scheme, parts.netloc) != (first_parts.scheme, first_parts.netloc):
            return 0, ""
        rel = parts.path + (("?" + parts.query) if parts.query else "")
        response = await page.evaluate(_INPAGE_FETCH_JS, rel)
        status = int((response or {}).get("status") or 0)
        body = str((response or {}).get("body") or "")
        return (status, body) if status > 0 else (0, "")

    sess = (session_factory or (lambda: _HbSession(mode="render")))()
    try:
        page = await sess.open()
        await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        first = await _raw_get(page, url)
        if first[0] <= 0 or not first[1]:
            return first, None
        try:
            selected = str(followup_url_from_body(first[1]) or "").strip()
        except Exception as exc:
            log.debug("hb_raw_get_then selector failed for %s: %s", url, exc)
            return first, None
        if not selected:
            return first, None
        followup_url = urljoin(url, selected)
        followup_parts = urlsplit(followup_url)
        if (followup_parts.scheme, followup_parts.netloc) != (
            first_parts.scheme,
            first_parts.netloc,
        ):
            log.info(
                "hb.raw_get_then_cross_origin_rejected property=%s start=%s followup=%s",
                property_id,
                first_parts.netloc,
                followup_parts.netloc,
            )
            return first, None
        followup = await _raw_get(page, followup_url)
        return first, (followup_url, followup[0], followup[1])
    except Exception as exc:
        log.debug("hb_raw_get_then failed for %s (%s): %s", url, property_id, exc)
        return (0, ""), None
    finally:
        try:
            await sess.close()
        except Exception as exc:
            log.debug("hb_raw_get_then session close failed: %s", exc)
