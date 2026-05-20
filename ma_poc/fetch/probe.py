"""Stealth-aware HTTP probe helper for adapter-side hops.

The L1 :func:`ma_poc.fetch.fetch` entry point is the canonical fetcher
and applies the full identity + proxy + retry + conditional-cache + captcha-
detect stack. It is GET-only (``RenderMode.HEAD`` / ``GET`` / ``RENDER``)
and accepts no custom request headers because the ``CrawlTask`` contract
is intentionally narrow.

Some adapter-side hops can't use that path because they need:

* a custom request header (e.g. RealPage CWS's ``x-ws-authkey`` derived
  from page HTML), or
* a non-GET method (e.g. Beacon Management's WordPress
  ``admin-ajax.php`` POST), or
* a fire-and-forget short-timeout probe that should not trigger the
  full retry-on-bot-block loop.

For those, :func:`stealth_probe` provides a smaller surface that still
applies the stealth identity headers, an optional proxy from the
default :class:`ProxyPool` (if reachable), and **captcha detection on
the response body**. It deliberately omits:

* the conditional-GET cache (ETag) — adapter probes are one-shot
* the rate limiter — adapter probes piggy-back on the L1 budget
* the retry / identity-rotation loop — fail-closed is preferred for
  best-effort probes

This keeps the call surface tiny while restoring stealth + captcha
parity on hops that previously used raw ``httpx`` with no identity.
"""

from __future__ import annotations

import logging
from typing import Any

from ma_poc.fetch.captcha_detect import looks_like_captcha
from ma_poc.fetch.headers import chrome_header_set
from ma_poc.fetch.stealth import IdentityPool

log = logging.getLogger(__name__)

# Module-singleton identity pool — same lifetime as the application
# process. Sticky-key (``property_id``) means a property always sees
# the same browser identity across the entry-page fetch AND any
# adapter-side probes that hit downstream endpoints for the same
# property. That consistency matters: a WAF that profiles UA + TLS
# fingerprint per source IP per second sees a coherent "single user
# session" instead of a Frankenstein mix.
_DEFAULT_IDENTITY_POOL: IdentityPool | None = None


def _get_identity_pool() -> IdentityPool:
    global _DEFAULT_IDENTITY_POOL
    if _DEFAULT_IDENTITY_POOL is None:
        _DEFAULT_IDENTITY_POOL = IdentityPool()
    return _DEFAULT_IDENTITY_POOL


async def stealth_probe(
    url: str,
    *,
    method: str = "GET",
    property_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
    data: Any = None,
    timeout_seconds: float = 10.0,
    follow_redirects: bool = True,
    telemetry_context: str = "other",
) -> tuple[bytes | None, int | None, str | None]:
    """Issue a stealth-aware HTTP request and run captcha detection.

    Parameters
    ----------
    url:
        Absolute URL to fetch.
    method:
        HTTP method. ``GET`` / ``POST`` / etc. Defaults to ``GET``.
    property_id:
        Sticky-key for identity selection. Same property gets the same
        Chrome identity across entry-page and adapter-side probes,
        which keeps a coherent "single user session" footprint.
    extra_headers:
        Caller-supplied headers (e.g. ``x-ws-authkey``,
        ``X-Requested-With``). Merged on top of the stealth header set
        so caller wins on collision — adapter-specific auth headers
        take precedence over the default Chrome navigation set.
    data:
        Body for non-GET methods. Passed straight through to httpx.
    timeout_seconds:
        Per-request total timeout. Defaults to 10 s.
    follow_redirects:
        Whether to follow 3xx. Defaults to True.

    Returns
    -------
    ``(body, status, captcha_provider)`` — three-tuple where:

    * ``body`` is the raw response bytes (or ``None`` on any
      exception / network failure).
    * ``status`` is the HTTP status code (or ``None`` if no response).
    * ``captcha_provider`` is the name returned by
      :func:`looks_like_captcha` when the body matches a CAPTCHA
      challenge (``"cloudflare"`` / ``"recaptcha"`` / ``"hcaptcha"`` /
      ``"perimeterx"`` / ``"sgcaptcha"``), otherwise ``None``.

    The caller must inspect ``captcha_provider`` before parsing the
    body — feeding a CAPTCHA HTML page into a downstream extractor
    is the upstream bug this helper is designed to prevent.
    """
    try:
        import httpx
    except ImportError:  # pragma: no cover — httpx is a hard dep
        return None, None, None

    identity = _get_identity_pool().pick(sticky_key=property_id)
    # cold_visit=True because adapter-side probes are essentially a
    # fresh navigation from the perspective of the bot-management
    # edge — the entry-page session cookies don't carry over.
    headers = chrome_header_set(identity, cold_visit=True)
    if extra_headers:
        headers.update(extra_headers)

    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=follow_redirects,
            headers=headers,
        ) as client:
            resp = await client.request(method.upper(), url, data=data)
    except Exception as exc:  # noqa: BLE001
        log.debug("stealth_probe %s %s failed: %s", method, url, exc)
        return None, None, None

    status = resp.status_code
    body: bytes
    try:
        body = resp.content  # bytes — works for GET and POST
    except Exception:
        body = b""

    captcha_provider: str | None = None
    try:
        is_captcha, provider = looks_like_captcha(body)
        if is_captcha:
            captcha_provider = provider or "unknown"
            log.info(
                "stealth_probe captcha detected url=%s provider=%s property_id=%s context=%s",
                url,
                captcha_provider,
                property_id,
                telemetry_context,
            )
            # Emit a counter event so production telemetry can measure
            # hop-captcha rate per context (specials_probe /
            # realpage_cws_probe / beacon_ajax_probe / other) without
            # URL-pattern filtering on the FETCH_CAPTCHA_DETECTED firehose.
            try:
                from ma_poc.observability.events import EventKind, emit

                emit(
                    EventKind.HOP_CAPTCHA_DETECTED,
                    property_id or "unknown",
                    url=url,
                    provider=captcha_provider,
                    context=telemetry_context,
                    status=status,
                )
            except Exception:
                # Observability is best-effort — never block the probe.
                pass
    except Exception:
        # captcha detection is best-effort — never block the probe.
        pass

    return body, status, captcha_provider
