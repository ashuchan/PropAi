"""Centralised curl_cffi probe used by the securecafe / ResMan / Entrata-
subpage / detection-rescue paths.

2026-05-17 iter-13: these lightweight endpoint fetches (~0.1-1.2 MB)
are the ONLY thing that needs a residential proxy to clear Cloudflare
GCP-IP blocking — NOT the 100 MB patchright render. Routing just the
probe through ``PROBE_PROXY_URL`` keeps proxy bandwidth at ~0.6 MB/site
(~$2.5/run for the full RentCafe+ResMan pool @ $4/GB) instead of ~$850
if the full render were proxied.

``PROBE_PROXY_URL`` form: ``http://user:pass@host:port`` (BrightData
residential zone). Unset ⇒ direct (current proxy-less behaviour;
safe default, no functional change off-canary).

2026-05-17 iter-14: the ``#2`` subset (prospectportal / Entrata-
``/conventional/``) is fronted by a Cloudflare *managed challenge*
(Turnstile-class) that defeats even the residential proxy + patchright
+ camoufox. BrightData Web Unlocker (api.brightdata.com/request,
$1.50/CPM) auto-solves it — 3-req validation 3/3 real_data, $0.0045.
``probe_get`` now COST-GATES an escalation: it only spends a Web
Unlocker request when the cheap proxied curl_cffi fetch still comes
back a CF challenge shell or a hard block (403/429/503). On success it
never fires, so steady-state spend stays ≈ the #2-subset size only
(~500 sites × ~2 req ≈ $1.50 one-time). Gated on ``WEB_UNLOCKER_KEY``;
unset ⇒ no escalation (safe default, no functional change off-canary).
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import threading
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

_DEFAULTS = {"impersonate": "chrome120", "timeout": 25, "allow_redirects": True}

# Cookie-mint reuse (option b, 2026-05-18). The fetcher's patchright
# render solves the CF/DataDome challenge and harvests the clearance
# cookies onto FetchResult.clearance_cookies; scraper.scrape() installs
# them here for the duration of one property's adapter dispatch.
# ContextVar ⇒ per-asyncio-task isolation: concurrent property scrapes
# never share clearance. Empty by default ⇒ no behaviour change off the
# cookie-mint path.
#
# 2026-05-26 (blockwall-v2 A/B, 300 props): cookie reuse is now OPT-IN
# per host. The 300-property A/B (2026-05-25) found: helped=0, hurt=10.
# Root cause: patchright Chromium UA mints cf_clearance; curl_cffi
# chrome120 presents it with a different User-Agent → CF sees a
# stolen-cookie replay → triggers fresh IUAM bot-fight on sites that
# were previously accessible without cookies. Default: don't attach
# minted clearance cookies unless the host is in the allowlist.
_clearance_cookies: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "probe_clearance_cookies", default=None
)

# Allowlist of hosts where cookie-mint reuse is known to help.
# Starts empty per the 300-property A/B verdict. Add entries ONLY when
# a new A/B confirms that attaching cf_clearance unblocks that host AND
# does not trigger bot-fight on sibling paths (i.e. the Chromium UA
# that minted the cookie matches the chrome120 impersonation UA).
_CLEARANCE_REUSE_HOST_ALLOWLIST: frozenset[str] = frozenset()


def set_clearance_cookies(
    cookies: dict[str, str] | None,
) -> contextvars.Token[dict[str, str] | None]:
    """Install clearance cookies for the current task; returns a reset token.

    Caller MUST ``reset_clearance_cookies(token)`` in a ``finally`` so the
    cookies don't bleed into the next property scraped on this task.
    """
    return _clearance_cookies.set(dict(cookies) if cookies else {})


def reset_clearance_cookies(token: contextvars.Token[dict[str, str] | None]) -> None:
    """Restore the clearance-cookie contextvar to its prior value."""
    try:
        _clearance_cookies.reset(token)
    except (ValueError, LookupError):
        pass


def _with_clearance(opts: dict[str, Any], url: str | None = None) -> dict[str, Any]:
    """Merge task-scoped clearance cookies — opt-in per host.

    Explicit per-call ``cookies=`` values always pass through unchanged.
    Minted CF clearance cookies are only injected when the target host
    appears in ``_CLEARANCE_REUSE_HOST_ALLOWLIST`` (empty by default).

    Background: the 300-property A/B (2026-05-25) showed that attaching
    patchright-minted cf_clearance to curl_cffi chrome120 requests helped
    0 properties and hurt 10 by triggering CF bot-fight via UA-binding
    mismatch.  The allowlist lets us re-enable per-host if future evidence
    shows a specific host benefits.
    """
    minted = _clearance_cookies.get() or {}
    if not minted:
        return opts
    # Host-based opt-in gate.  When url is None the host is unknowable,
    # so treat as "not in allowlist" and do not attach.
    if url is None:
        return opts
    try:
        from urllib.parse import urlparse as _up
        host = _up(url).netloc.lower()
    except Exception:
        return opts
    if host not in _CLEARANCE_REUSE_HOST_ALLOWLIST:
        return opts  # default: don't attach minted cookies
    explicit = opts.get("cookies") or {}
    if isinstance(explicit, dict):
        opts["cookies"] = {**minted, **explicit}
    return opts
_WU_API = "https://api.brightdata.com/request"
_WU_BLOCK_STATUS = {403, 429, 503}


def probe_proxies() -> dict[str, str]:
    """``{"http":url,"https":url}`` from PROBE_PROXY_URL, or ``{}``."""
    u = os.getenv("PROBE_PROXY_URL", "").strip()
    return {"http": u, "https": u} if u else {}


def web_unlocker_key() -> str:
    """BrightData Web Unlocker API token from WEB_UNLOCKER_KEY, or ''."""
    return os.getenv("WEB_UNLOCKER_KEY", "").strip()


def web_unlocker_zone() -> str:
    """Web Unlocker zone name (WEB_UNLOCKER_ZONE), default web_unlocker1."""
    return os.getenv("WEB_UNLOCKER_ZONE", "web_unlocker1").strip() or "web_unlocker1"


# ----------------------------------------------------------------------
# Web Unlocker hard call-cap (2026-05-24)
# ----------------------------------------------------------------------
# Per-shard budget guard. The jugnu-unlocker-test-3886351-fl9gv canary
# fired 3,180 Unlocker calls before user cancellation (target ceiling
# was $2-3; actual ~$4.65-9.30 depending on BD tier rate). Without a
# hard cap, a single shard that hits a long tail of CF-walled
# ProspectPortal hosts can balloon spend an order of magnitude past
# the per-property gate (``_probe_prospectportal`` already gates per-
# property, but the orchestrator runs many props per shard).
#
# Reads ``WEB_UNLOCKER_MAX_CALLS_PER_JOB`` at call time so tests +
# canary configs can flip it without restart. 0 / unset / non-numeric
# = no cap (preserves existing behaviour for legacy configs).
# Recommended production setting: 500 (~$0.75-$1.50/shard worst case).
#
# Counter is module-level + per-process. Cloud Run jobs spawn one
# process per task (shard), so each shard has its own 0-initialised
# counter — the cap is per-shard, not per-job. If a job has 32 shards
# at cap=500 the worst case is 32 × 500 × $3/1000 = $48; tighten the
# cap further (or shard-count further) for cost-sensitive runs.
_WU_LOCK: threading.Lock  # initialised below; defer import for module-load speed
_wu_call_count: int = 0
_wu_cap_warned: bool = False  # log the first cap-hit per shard only


def _wu_cap() -> int:
    """Read ``WEB_UNLOCKER_MAX_CALLS_PER_JOB`` env var. 0 = no cap."""
    raw = os.getenv("WEB_UNLOCKER_MAX_CALLS_PER_JOB", "").strip()
    if not raw:
        return 0
    try:
        v = int(raw)
        return v if v > 0 else 0
    except ValueError:
        return 0


def reset_web_unlocker_call_count() -> None:
    """Reset the per-process counter. For tests / per-job init only —
    never call in production scraper code."""
    global _wu_call_count, _wu_cap_warned
    with _WU_LOCK:
        _wu_call_count = 0
        _wu_cap_warned = False


def web_unlocker_call_count() -> int:
    """Read the current per-process Unlocker call count. Useful for
    cost-ledger telemetry + tests."""
    with _WU_LOCK:
        return _wu_call_count


def _wu_try_reserve_call() -> bool:
    """Reserve one Unlocker call slot. Returns True if allowed, False
    if the per-process cap is exhausted. Atomic under the module
    lock — safe for the async pool's worker threads.

    Logs a single warning the first time the cap blocks a call, so
    operators see it without log spam."""
    global _wu_call_count, _wu_cap_warned
    cap = _wu_cap()
    with _WU_LOCK:
        if cap and _wu_call_count >= cap:
            if not _wu_cap_warned:
                log.warning(
                    "web_unlocker.cap_reached cap=%d count=%d "
                    "— further calls return empty (set "
                    "WEB_UNLOCKER_MAX_CALLS_PER_JOB higher to raise)",
                    cap,
                    _wu_call_count,
                )
                _wu_cap_warned = True
            return False
        _wu_call_count += 1
        return True


_WU_LOCK = threading.Lock()


class _WUResponse:
    """Minimal curl_cffi-response shim (``.status_code/.text/.url``)."""

    __slots__ = ("status_code", "text", "url")

    def __init__(self, status_code: int, text: str, url: str) -> None:
        self.status_code = status_code
        self.text = text
        self.url = url


def _wu_safe_url(url: str) -> str:
    """Percent-encode reserved characters that BrightData Web Unlocker's
    URL validator rejects with HTTP 400.

    2026-05-24 (jugnu-unlocker-test-3886351-fl9gv): 78 web_unlocker.error
    occurrences, all on Entrata ProspectPortal ``view_unit_spaces``
    URLs with un-encoded square brackets in the query
    (``property[id]=1125701&property_floorplan[id]=1125372``). BD's
    gateway returns ``HTTPError: HTTP Error 400: Bad Request`` for
    these — billed or not, they burn the
    ``_probe_prospectportal`` retry budget without ever clearing CF.

    Fix: split the URL into components and percent-encode each piece
    with a tight safe-set. ``[`` → ``%5B``, ``]`` → ``%5D`` while
    standard query separators (``&``, ``=``, ``?``) and path slashes
    stay literal. Idempotent — already-encoded URLs survive unchanged
    because ``quote(...)`` with the right ``safe`` skips ``%``.
    """
    if not url:
        return url
    from urllib.parse import quote, urlsplit, urlunsplit

    parts = urlsplit(url)
    # Path: keep ``/`` plus the standard RFC 3986 pchar set; encode
    # ``[`` ``]`` ``{`` ``}`` if they ever leak into a path.
    safe_path = "/-._~!$&'()*+,;=:@%"
    # Query: keep ``&`` ``=`` ``?`` ``+`` literal; encode brackets and
    # other reserved gen-delims that the BD gateway is strict about.
    safe_query = "=&?+-._~!$'()*,;:@/%"
    new_path = quote(parts.path, safe=safe_path)
    new_query = quote(parts.query, safe=safe_query)
    new_fragment = quote(parts.fragment, safe=safe_query)
    return urlunsplit(
        (parts.scheme, parts.netloc, new_path, new_query, new_fragment)
    )


def web_unlocker_get(url: str, timeout: int = 120) -> _WUResponse:
    """Fetch *url* through the BrightData Web Unlocker API (raw HTML).

    Returns a ``_WUResponse`` with ``status_code`` 200 on a solved page,
    or 0 on transport/auth error (callers treat non-200 as empty). The
    Web Unlocker auto-solves Cloudflare managed challenges server-side.

    Budget gate: respects ``WEB_UNLOCKER_MAX_CALLS_PER_JOB`` (see
    ``_wu_try_reserve_call`` for semantics). When the cap is hit
    further calls return ``_WUResponse(0, "", url)`` without ever
    contacting BD — caller sees the same shape as an unconfigured
    key, so the cascade falls through to the next rung naturally.
    """
    # Compliance kill-switch (RealPage legal 2026-07-22): under COMPLIANCE_MODE
    # the Web Unlocker is a no-fly zone. Return the same empty shape callers
    # already handle for missing-key / cap / transport-error, so every
    # adapter-internal unlocker path (rentcafe attempt-3, onesite rescue,
    # entrata static-fetch, probe_get(unlocker=True)) falls through to the
    # legal cascade with zero special-casing. This is the single chokepoint.
    from ma_poc.config.feature_flags import web_unlocker_allowed

    if not web_unlocker_allowed():
        return _WUResponse(0, "", url)
    key = web_unlocker_key()
    if not key:
        return _WUResponse(0, "", url)
    if not _wu_try_reserve_call():
        # Cap-blocked. Return the same empty shape callers already
        # handle for missing-key / transport-error cases — no special
        # path needed downstream.
        return _WUResponse(0, "", url)
    safe_url = _wu_safe_url(url)
    body = json.dumps(
        {"zone": web_unlocker_zone(), "url": safe_url, "format": "raw"}
    ).encode()
    req = urllib.request.Request(
        _WU_API,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", "replace")
        return _WUResponse(200, html, url)
    except Exception as exc:  # noqa: BLE001 — caller treats non-200 as empty
        log.warning(
            "web_unlocker.error url=%s err=%s: %s",
            url,
            type(exc).__name__,
            str(exc)[:120],
        )
        return _WUResponse(0, "", url)


def _looks_blocked(resp: Any) -> bool:
    """True if *resp* is a CF challenge shell or a hard bot-block status.

    Cost gate for the Web Unlocker escalation — only spend a paid
    request when the cheap proxied curl_cffi fetch clearly failed.
    """
    status = getattr(resp, "status_code", None)
    if status in _WU_BLOCK_STATUS:
        return True
    try:
        body = getattr(resp, "content", None)
        if body is None:
            body = (getattr(resp, "text", "") or "").encode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return False
    from ma_poc.fetch.captcha_detect import looks_like_captcha

    is_captcha, provider = looks_like_captcha(body or b"")
    return bool(is_captcha and provider == "cloudflare")


def probe_get(url: str, *, unlocker: bool = True, **kw: Any) -> Any:
    """curl_cffi GET with chrome impersonation + optional probe proxy.

    Raises ImportError if curl_cffi is unavailable (callers already guard
    for that). Proxy + TLS-verify-relaxation only applied when
    PROBE_PROXY_URL is set (BrightData terminates TLS at its edge).

    Cost-gated Web Unlocker escalation: if WEB_UNLOCKER_KEY is set and
    the proxied fetch comes back a Cloudflare challenge shell or a
    403/429/503 block, retry once via the Web Unlocker API. WU is never
    spent on a successful fetch.

    Args:
        url: Target URL.
        unlocker: If False, skip Web Unlocker escalation entirely for this
            call. Use for secondary/per-plan fetches inside an adapter loop
            where WU budget should be reserved for the primary landing page
            (e.g., Entrata per-floorplan unit drills after landing succeeds).
            Default True preserves existing behaviour.
        **kw: Forwarded to curl_cffi.requests.get.

    2026-05-26 cost audit: LLM-tier re-fetches drove 52% of WU spend
    (~7,682/14,655 calls/day). Callers that don't need WU escalation should
    pass ``unlocker=False`` to avoid the cap being eaten by low-value fetches.
    """
    from curl_cffi import requests as _creq

    from ma_poc.config.feature_flags import web_unlocker_allowed
    from ma_poc.fetch.host_throttle import throttle as _host_throttle

    # Compliance (RealPage legal): never escalate to the Web Unlocker. The
    # web_unlocker_get chokepoint already no-ops, but drop the attempt here too
    # so telemetry doesn't log a spurious unlock escalation.
    if unlocker and not web_unlocker_allowed():
        unlocker = False
    opts: dict[str, Any] = _with_clearance({**_DEFAULTS, **kw}, url=url)
    px = probe_proxies()
    if px:
        opts.setdefault("proxies", px)
        opts.setdefault("verify", False)  # BrightData edge TLS termination
    # Per-shared-host politeness gate: keys on the registrable domain of the
    # DRILL url (securecafe.com / sightmap.com / myresman.com / realpage.com …)
    # so N concurrent property scrapes don't hammer one PMS backend. No-op
    # unless RATELIMIT_* is configured (safe generous defaults).
    try:
        with _host_throttle(url):
            resp = _creq.get(url, **opts)
    except Exception:
        if unlocker and web_unlocker_key():
            wu = web_unlocker_get(url, timeout=int(opts.get("timeout") or 25) + 95)
            if wu.status_code == 200:
                log.info("web_unlocker.rescue url=%s via=transport_error", url)
                return wu
        raise

    # 2026-05-26 (blockwall-v2 A/B): if we attached cookies (via the
    # allowlist or explicit kw) and the response is a CF IUAM block,
    # retry once without cookies. UA-binding mismatch between the
    # patchright Chromium UA (that minted cf_clearance) and curl_cffi
    # chrome120 (that presents it) triggers a fresh bot-fight challenge
    # on sites that would otherwise be accessible without cookies.
    if _looks_blocked(resp) and opts.get("cookies"):
        opts_no_cookies = {k: v for k, v in opts.items() if k != "cookies"}
        try:
            resp2 = _creq.get(url, **opts_no_cookies)
            if not _looks_blocked(resp2):
                log.info(
                    "probe_get.cookie_strip_retry url=%s status=%s→%s",
                    url, getattr(resp, "status_code", "?"),
                    getattr(resp2, "status_code", "?"),
                )
                return resp2
        except Exception:
            pass  # fall through to original resp / Web Unlocker escalation

    if unlocker and web_unlocker_key() and _looks_blocked(resp):
        wu = web_unlocker_get(url, timeout=int(opts.get("timeout") or 25) + 95)
        if wu.status_code == 200 and not _looks_blocked(wu):
            log.info("web_unlocker.rescue url=%s via=cf_shell_or_block", url)
            return wu
    return resp


def probe_post(url: str, data: Any = None, **kw: Any) -> Any:
    """curl_cffi POST with chrome impersonation + optional probe proxy.

    Mirrors :func:`probe_get` (same proxy / TLS-relax behaviour when
    ``PROBE_PROXY_URL`` is set) for endpoints that require POST — e.g.
    the repli360 ``/admin/getUnitListByFloor`` data API. ``data`` is
    passed straight to curl_cffi (dict ⇒ form-encoded). No Web-Unlocker
    escalation: the repli360 endpoint is not bot-walled, and WU's GET-
    only semantics don't fit a POST body.

    Raises ImportError if curl_cffi is unavailable (callers guard).
    """
    from curl_cffi import requests as _creq

    from ma_poc.fetch.host_throttle import throttle as _host_throttle

    opts: dict[str, Any] = _with_clearance({**_DEFAULTS, **kw}, url=url)
    px = probe_proxies()
    if px:
        opts.setdefault("proxies", px)
        opts.setdefault("verify", False)  # BrightData edge TLS termination
    with _host_throttle(url):
        return _creq.post(url, data=data, **opts)


# ── page=None recovery helpers (task #37 Track 1, 2026-07-19) ──


async def probe_fetch_status(url: str, *, timeout: int = 20) -> tuple[int, str]:
    """curl_cffi fallback for in-session ``page.evaluate(fetch(url))`` when there
    is no live page (production dispatches L3 with page=None). Runs off the event
    loop via ``asyncio.to_thread`` so a per-property probe can't stall the shared
    AsyncPool loop. Returns ``(status, body)``; body is ``""`` on any non-2xx or
    error so callers never parse a bot-wall payload. Never raises.
    ``unlocker=False`` keeps it on the cheap tier."""

    def _run() -> tuple[int, str]:
        try:
            resp = probe_get(url, unlocker=False, timeout=timeout)
        except Exception:
            return 0, ""
        try:
            status = int(getattr(resp, "status_code", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        if not (200 <= status < 300):
            return status, ""
        body = getattr(resp, "text", "") or ""
        return status, body if isinstance(body, str) else ""

    try:
        return await asyncio.to_thread(_run)
    except Exception:
        return 0, ""


def body_html_from_ctx(ctx: Any) -> str:
    """Decode the L1-fetched body from an AdapterContext (bytes or str); ``""``
    when absent. Lets page-gated recoveries scan the already-fetched RENDER body
    for their embed/portal markers at page=None (the marker is in the body)."""
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None)
    if isinstance(body, bytes):
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return ""
    return body if isinstance(body, str) else ""
