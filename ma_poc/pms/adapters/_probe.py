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

import contextvars
import json
import logging
import os
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

_DEFAULTS = {"impersonate": "chrome120", "timeout": 25, "allow_redirects": True}

# Cookie-mint reuse (option b, 2026-05-18). The fetcher's patchright
# render solves the CF/DataDome challenge and harvests the clearance
# cookies onto FetchResult.clearance_cookies; scraper.scrape() installs
# them here for the duration of one property's adapter dispatch. The
# probes below auto-attach them so the cheap curl_cffi active-fetch
# (MAAC/Irvine/Cortland/Essex/Equity/SightMap-iframe) reuses the solved
# clearance instead of hitting the wall again. ContextVar ⇒ per-asyncio-
# task isolation: concurrent property scrapes never share clearance.
# Empty by default ⇒ no behaviour change off the cookie-mint path.
_clearance_cookies: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "probe_clearance_cookies", default=None
)


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


def _with_clearance(opts: dict[str, Any]) -> dict[str, Any]:
    """Merge task-scoped clearance cookies under any explicit ``cookies=``.

    Explicit per-call cookies win on key collision (the adapter knows its
    own endpoint better than a generic CF cookie). No-op when no clearance
    has been minted for this task.
    """
    minted = _clearance_cookies.get() or {}
    if not minted:
        return opts
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
    """
    key = web_unlocker_key()
    if not key:
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


def probe_get(url: str, **kw: Any) -> Any:
    """curl_cffi GET with chrome impersonation + optional probe proxy.

    Raises ImportError if curl_cffi is unavailable (callers already guard
    for that). Proxy + TLS-verify-relaxation only applied when
    PROBE_PROXY_URL is set (BrightData terminates TLS at its edge).

    Cost-gated Web Unlocker escalation: if WEB_UNLOCKER_KEY is set and
    the proxied fetch comes back a Cloudflare challenge shell or a
    403/429/503 block, retry once via the Web Unlocker API. WU is never
    spent on a successful fetch.
    """
    from curl_cffi import requests as _creq

    opts: dict[str, Any] = _with_clearance({**_DEFAULTS, **kw})
    px = probe_proxies()
    if px:
        opts.setdefault("proxies", px)
        opts.setdefault("verify", False)  # BrightData edge TLS termination
    try:
        resp = _creq.get(url, **opts)
    except Exception:
        if web_unlocker_key():
            wu = web_unlocker_get(url, timeout=int(opts.get("timeout") or 25) + 95)
            if wu.status_code == 200:
                log.info("web_unlocker.rescue url=%s via=transport_error", url)
                return wu
        raise

    if web_unlocker_key() and _looks_blocked(resp):
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

    opts: dict[str, Any] = _with_clearance({**_DEFAULTS, **kw})
    px = probe_proxies()
    if px:
        opts.setdefault("proxies", px)
        opts.setdefault("verify", False)  # BrightData edge TLS termination
    return _creq.post(url, data=data, **opts)
