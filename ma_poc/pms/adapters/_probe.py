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
    """Deprecated direct-env reader. **DO NOT CALL FROM NEW CODE.**

    Retained for the diagnostic script ``scripts/diagnostics/asn_ipv6_probe.py``
    which deliberately tests "what would happen if proxy were unconditionally
    used". All adapter-side probes go through :mod:`ma_poc.fetch.proxy_gate`.

    Behaviour: returns ``{}`` (i.e. NEVER uses proxy) when called from any
    code path other than the explicitly allowlisted diagnostic. This is the
    strict-allow contract: anything that doesn't ask the gate doesn't get
    the proxy.
    """
    # The diagnostic explicitly imports and calls this name; everything else
    # must use proxy_gate.decide(). Strict-allow: no caller below this line
    # gets a non-empty dict from this function.
    return {}


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


def web_unlocker_get(url: str, timeout: int = 120) -> _WUResponse:
    """Fetch *url* through the BrightData Web Unlocker API (raw HTML).

    Returns a ``_WUResponse`` with ``status_code`` 200 on a solved page,
    or 0 on transport/auth error (callers treat non-200 as empty). The
    Web Unlocker auto-solves Cloudflare managed challenges server-side.
    """
    key = web_unlocker_key()
    if not key:
        return _WUResponse(0, "", url)
    body = json.dumps(
        {"zone": web_unlocker_zone(), "url": url, "format": "raw"}
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


# ─── Gate-routed probe entry points (2026-05-23) ──────────────────────
#
# Every probe call now resolves whether to use the proxy via
# ``fetch.proxy_gate.decide``. Callers MUST pass ``ctx`` and ``stage``
# kwargs; missing either is treated as ctx=None/stage=None which
# fail-closes to direct (no proxy). This is the strict-allow contract:
# you cannot accidentally route a probe through the proxy by forgetting
# to update a caller. The grep test in
# ``tests/fetch/test_proxy_gate_no_leaks.py`` enforces that no other
# code path reads PROBE_PROXY_URL directly.
#
# Telemetry: every probe attempt emits a ``proxy_decision`` event
# carrying the reason code from the gate, so analyzer queries can
# attribute spend per (stage, host, decision_reason) without parsing
# free-form text.


def _emit_proxy_decision_telemetry(
    *,
    pid: str,
    stage: str | None,
    url: str,
    decision_reason: str,
    via_proxy: bool,
    response_bytes: int,
    response_status: int | None,
    elapsed_ms: int | None,
) -> None:
    """Emit a ``proxy_decision`` event. Best-effort; never raises."""
    try:
        from ma_poc.observability.events import EventKind, emit

        emit(
            EventKind.PROXY_DECISION,
            pid or "unknown",
            url=_redact_url(url),
            stage=stage or "unknown",
            decision_reason=decision_reason,
            via_proxy=via_proxy,
            response_bytes=response_bytes,
            response_status=response_status,
            elapsed_ms=elapsed_ms,
        )
    except Exception:
        # observability is best-effort; never block a probe
        pass


def _redact_url(url: str) -> str:
    """Strip query and fragment from a URL for logging (keeps host + path)."""
    try:
        from urllib.parse import urlparse, urlunparse

        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
    except Exception:
        return url[:200]


def _response_byte_count(resp: Any) -> int:
    """Best-effort size of a response body (bytes). Returns 0 on failure."""
    if resp is None:
        return 0
    try:
        body = getattr(resp, "content", None)
        if body is not None:
            return len(body)
    except Exception:
        pass
    try:
        text = getattr(resp, "text", "") or ""
        return len(text)
    except Exception:
        return 0


def probe_get(url: str, *, ctx: Any | None = None, stage: str | None = None, **kw: Any) -> Any:
    """curl_cffi GET with chrome impersonation, gate-routed proxy.

    The gate (:func:`ma_poc.fetch.proxy_gate.decide`) decides whether to
    route this request through ``PROBE_PROXY_URL`` based on:

    * env (PROBE_PROXY_URL set?)
    * per-property hop budget (max 3 paid-egress hops per property)
    * same-origin vs cross-origin
    * static host allowlist (securecafe/prospectportal/iloveleasing/realpage OLL)
    * profile-learned per-host block memory
    * detection confidence ≥ 0.80 AND stage in eligible list

    Callers MUST pass ``ctx`` and ``stage`` for any chance of routing
    through proxy. Without them the gate fail-closes to direct.

    Parameters
    ----------
    url:
        Absolute URL to fetch.
    ctx:
        AdapterContext from the caller. Used for budget + profile.
    stage:
        Adapter telemetry stage name (e.g. ``"sc_probe"``,
        ``"wp_probe"``, ``"prospectportal_probe"``). Matches the
        ``tier_key`` suffix in ``_adapter_telemetry``.
    **kw:
        Passed through to ``curl_cffi.requests.get`` (timeout, headers).

    Returns
    -------
    A curl_cffi response object (or a ``_WUResponse`` shim when Web
    Unlocker escalation fired). Raises whatever curl_cffi raises on
    irrecoverable failure (callers wrap in try/except).

    Web Unlocker escalation
    -----------------------
    When the (proxied) fetch returns a CF challenge / 403-429-503,
    AND ``WEB_UNLOCKER_KEY`` is set, AND the property still has budget
    headroom, the gate is consulted again with stage suffixed by
    ``_unlocker`` and on approval the Web Unlocker is invoked. The
    escalation counts against the same per-property hop budget.
    """
    from curl_cffi import requests as _creq
    from ma_poc.fetch.proxy_gate import decide, record_use, mark_host_needs_proxy

    decision = decide(url, ctx, stage)
    opts: dict[str, Any] = _with_clearance({**_DEFAULTS, **kw})
    px = decision.proxies()
    if px:
        opts.setdefault("proxies", px)
        opts.setdefault("verify", False)  # BrightData edge TLS termination

    import time
    t0 = time.monotonic()
    resp: Any = None
    transport_exc: Exception | None = None
    try:
        resp = _creq.get(url, **opts)
    except Exception as exc:
        transport_exc = exc

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    response_bytes = _response_byte_count(resp) if resp is not None else 0
    response_status = getattr(resp, "status_code", None) if resp is not None else None
    pid = getattr(ctx, "property_id", "") or "unknown"

    # Account for proxy bandwidth/hop use before any escalation decision.
    record_use(ctx, decision, response_bytes=response_bytes)

    _emit_proxy_decision_telemetry(
        pid=pid,
        stage=stage,
        url=url,
        decision_reason=decision.reason.value,
        via_proxy=decision.allow,
        response_bytes=response_bytes,
        response_status=response_status,
        elapsed_ms=elapsed_ms,
    )

    # Transport-error path: try Web Unlocker (still gate-routed).
    if transport_exc is not None:
        wu_resp = _maybe_escalate_to_web_unlocker(
            url, ctx, stage,
            initial_reason=decision.reason.value,
            initial_status=None,
            initial_response=None,
            timeout_hint=int(opts.get("timeout") or 25),
        )
        if wu_resp is not None:
            return wu_resp
        raise transport_exc

    # Block-shaped response path: try Web Unlocker if eligible AND
    # remember the host as needing proxy next run (Layer 3 self-learn).
    if _looks_blocked(resp):
        host = decision.host
        if host:
            mark_host_needs_proxy(ctx, host)
        wu_resp = _maybe_escalate_to_web_unlocker(
            url, ctx, stage,
            initial_reason=decision.reason.value,
            initial_status=response_status,
            initial_response=resp,
            timeout_hint=int(opts.get("timeout") or 25),
        )
        if wu_resp is not None:
            return wu_resp
    return resp


def _maybe_escalate_to_web_unlocker(
    url: str,
    ctx: Any | None,
    stage: str | None,
    *,
    initial_reason: str,
    initial_status: int | None,
    initial_response: Any | None,
    timeout_hint: int,
) -> Any | None:
    """Web Unlocker escalation path, gate-routed.

    Returns the WU response only when:
      * ``WEB_UNLOCKER_KEY`` is set, AND
      * the property still has paid-egress budget (gate approves), AND
      * the WU response itself is a 200 that isn't a CF challenge.

    Otherwise returns ``None`` — caller keeps the original response.
    """
    if not web_unlocker_key():
        return None

    from ma_poc.fetch.proxy_gate import decide, record_use

    # Re-consult the gate with a "_unlocker" stage suffix so the budget
    # counter sees this as an additional paid hop. The decision uses the
    # same allowlist/profile/quality logic but accounts the hop against
    # the per-property cap. If the property already used its 3 hops,
    # this returns False and we don't spend a WU credit.
    wu_stage = (stage or "") + "_unlocker" if stage else "_unlocker"
    wu_decision = decide(url, ctx, wu_stage)
    if not wu_decision.allow:
        return None

    wu = web_unlocker_get(url, timeout=timeout_hint + 95)
    if wu.status_code != 200 or _looks_blocked(wu):
        # Failed escalation — still counts as a paid attempt; bump budget.
        record_use(ctx, wu_decision, response_bytes=_response_byte_count(wu))
        return None

    log.info(
        "web_unlocker.rescue url=%s via=after_%s(status=%s)",
        _redact_url(url), initial_reason, initial_status,
    )
    record_use(ctx, wu_decision, response_bytes=_response_byte_count(wu))
    pid = getattr(ctx, "property_id", "") or "unknown"
    _emit_proxy_decision_telemetry(
        pid=pid,
        stage=wu_stage,
        url=url,
        decision_reason=wu_decision.reason.value,
        via_proxy=True,
        response_bytes=_response_byte_count(wu),
        response_status=wu.status_code,
        elapsed_ms=None,
    )
    return wu


def probe_post(url: str, data: Any = None, *, ctx: Any | None = None, stage: str | None = None, **kw: Any) -> Any:
    """curl_cffi POST with chrome impersonation, gate-routed proxy.

    Mirrors :func:`probe_get` decision-tree. No Web-Unlocker escalation:
    the WU REST API is GET-only and the existing POST callers (repli360
    ``/admin/getUnitListByFloor``, irvine search) are not CF-walled in
    production.

    Callers MUST pass ``ctx`` and ``stage`` for proxy routing.
    """
    from curl_cffi import requests as _creq
    from ma_poc.fetch.proxy_gate import decide, record_use

    decision = decide(url, ctx, stage)
    opts: dict[str, Any] = _with_clearance({**_DEFAULTS, **kw})
    px = decision.proxies()
    if px:
        opts.setdefault("proxies", px)
        opts.setdefault("verify", False)

    import time
    t0 = time.monotonic()
    try:
        resp = _creq.post(url, data=data, **opts)
    finally:
        elapsed_ms = int((time.monotonic() - t0) * 1000)

    response_bytes = _response_byte_count(resp)
    record_use(ctx, decision, response_bytes=response_bytes)
    pid = getattr(ctx, "property_id", "") or "unknown"
    _emit_proxy_decision_telemetry(
        pid=pid,
        stage=stage,
        url=url,
        decision_reason=decision.reason.value,
        via_proxy=decision.allow,
        response_bytes=response_bytes,
        response_status=getattr(resp, "status_code", None),
        elapsed_ms=elapsed_ms,
    )
    return resp
