"""Response classifier — maps (status, headers, body, exception) to FetchOutcome.

Pure function, no I/O. Used by the fetcher to decide what happened.

Sources consulted:
- Cloudflare challenge page patterns (developer docs)
- Standard HTTP status code semantics (RFC 9110)
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import Mapping
from socket import gaierror

from .block_signatures import looks_like_200_block
from .captcha_detect import looks_like_captcha
from .contracts import FetchOutcome

log = logging.getLogger(__name__)

# Exceptions that indicate DNS resolution failure
_DNS_ERRORS = (gaierror, OSError)

# ── Parked-domain content fingerprints ───────────────────────────────────────
# Domain registrars serve HTTP 200 for parked / for-sale domains.  We detect
# them by matching the first 4KB of the response body against known title and
# body phrases.  Phrases are lowercased; body_head is decoded as latin-1
# (never raises) so binary noise doesn't crash the check.
_PARKED_DOMAIN_PHRASES: tuple[str, ...] = (
    # Generic parked / for-sale copy
    "this domain is for sale",
    "domain is for sale",
    "buy this domain",
    "this domain may be for sale",
    "domain name for sale",
    "domain has been registered",
    "this domain is available",
    "domain is available",
    # Common registrar / parking service indicators
    "godaddy.com/domainforsale",
    "dan.com/buy-domain",
    "sedo.com",
    "afternic.com",
    "parking.namecheap.com",
    "hugedomains.com",
    "bodis.com",
    "searchhound",          # searchhounds.com parking page
    "this web page is parked",
    "parked free courtesy of",
    "web page is parked free",
)


def _is_parked_domain(body_head: bytes) -> bool:
    """Return True if body_head looks like a domain-parking / for-sale page."""
    try:
        text = body_head[:4096].decode("latin-1").lower()
    except Exception:
        return False
    return any(phrase in text for phrase in _PARKED_DOMAIN_PHRASES)

# F3 — Cloudflare-edge response markers. ``server: cloudflare`` is the
# canonical marker; ``cf-ray`` / ``cf-mitigated`` / ``cf-cache-status``
# show up on edge-served responses even when the origin server header is
# masked. Used by ``_is_silent_block`` to upgrade a 403 with no useful
# body to BOT_BLOCKED so tier escalation fires.
_CLOUDFLARE_HEADER_TOKENS: tuple[str, ...] = ("cf-ray", "cf-mitigated", "cf-cache-status")

# Body length below which a 403 is considered "silent" (no login form,
# no error page text, no CAPTCHA markup — just an empty or near-empty
# wall). Set at 64 bytes which comfortably below any real login wall
# but above the few-byte responses some edge services return.
_SILENT_BLOCK_BODY_THRESHOLD = 64


def _has_cloudflare_signature(headers: Mapping[str, str] | None) -> bool:
    """True if *headers* show a Cloudflare-edge response.

    Case-insensitive on both keys and the ``server`` value. Empty or
    None headers return False.
    """
    if not headers:
        return False
    lower = {k.lower(): v for k, v in headers.items()}
    if lower.get("server", "").lower() == "cloudflare":
        return True
    return any(t in lower for t in _CLOUDFLARE_HEADER_TOKENS)


def _is_silent_block(
    status_code: int | None,
    headers: Mapping[str, str] | None,
    body: bytes | str | None,
) -> bool:
    """A 403 with empty/short body OR Cloudflare-header signature is a silent bot block.

    Discriminator (H14): legitimate 403 login walls have substantive
    body content and no Cloudflare header — they fall through and get
    classified as plain HTTP_403. Only ``status_code == 403`` triggers
    this path; non-403 statuses are handled elsewhere.
    """
    if status_code != 403:
        return False
    if _has_cloudflare_signature(headers):
        return True
    if body is None:
        return True
    if isinstance(body, bytes):
        return len(body) < _SILENT_BLOCK_BODY_THRESHOLD
    # str body — strip whitespace before measuring so a body of "   "
    # counts as silent.
    return len(body.strip()) < _SILENT_BLOCK_BODY_THRESHOLD

# patchright (our chosen shim) uses the same internal error hierarchy as playwright.
# TimeoutError does NOT inherit from asyncio.TimeoutError or the builtin TimeoutError,
# so `isinstance(exc, TimeoutError)` misses it and the classifier falls through to a
# generic signature. Import defensively so the classifier is usable without patchright.
try:  # pragma: no cover — import-time only
    from patchright._impl._errors import TimeoutError as _PlaywrightTimeoutError
except Exception:  # pragma: no cover
    _PlaywrightTimeoutError = None  # type: ignore[assignment,misc]


def classify(
    status: int | None,
    headers: dict[str, str],
    body_head: bytes | None,
    exception: Exception | None = None,
    body_size: int | None = None,
) -> tuple[FetchOutcome, str | None]:
    """Classify an HTTP response into a FetchOutcome.

    Args:
        status: HTTP status code, or None if no response received.
        headers: Response headers with lowercased keys.
        body_head: First ~4KB of the response body.
        exception: Exception raised during the request, if any.
        body_size: Total body size in bytes (NOT just the head slice).
            Forwarded to ``looks_like_captcha`` so widget-dual-use
            captcha fingerprints (``g-recaptcha`` / ``hcaptcha.com``)
            are skipped on large real pages. Without this, embedded
            captcha widgets in contact forms cause false-positive
            BOT_BLOCKED classifications. See ``looks_like_captcha``.

    Returns:
        Tuple of (FetchOutcome, error_signature_or_none).
        Use match_block_signature() separately when outcome is BOT_BLOCKED
        to get the block_signature (cf_turnstile, px_block, etc.).
    """
    # Exception-based classification first
    if exception is not None:
        if isinstance(exception, (ssl.SSLError, ssl.SSLCertVerificationError)):
            return FetchOutcome.HARD_FAIL, "ERR_SSL_PROTOCOL_ERROR"
        if isinstance(exception, _DNS_ERRORS):
            err_msg = str(exception).lower()
            # Stage 3 (2026-05-12): NXDOMAIN / "name not known" → terminal
            # dead URL. Distinct from a transient DNS hiccup ("temporary
            # failure in name resolution") which stays HARD_FAIL.
            if (
                "nxdomain" in err_msg
                or "name or service not known" in err_msg
                or "no such host" in err_msg
                or "name not resolved" in err_msg
                or "err_name_not_resolved" in err_msg
            ):
                return FetchOutcome.DEAD_URL, "ERR_DNS_NXDOMAIN"
            if "getaddrinfo" in err_msg or "name or service" in err_msg:
                return FetchOutcome.HARD_FAIL, "ERR_DNS"
        if isinstance(exception, (asyncio.TimeoutError, TimeoutError)):
            return FetchOutcome.TRANSIENT, "timeout"
        if _PlaywrightTimeoutError is not None and isinstance(exception, _PlaywrightTimeoutError):
            return FetchOutcome.TRANSIENT, "timeout"
        if isinstance(exception, ConnectionError):
            return FetchOutcome.TRANSIENT, f"connection_{type(exception).__name__}"
        # Unknown exception with no status
        if status is None:
            return FetchOutcome.TRANSIENT, type(exception).__name__

    # Status-based classification
    if status is None:
        return FetchOutcome.TRANSIENT, "no_response"

    if status == 304:
        return FetchOutcome.NOT_MODIFIED, None

    if status == 407:
        return FetchOutcome.PROXY_ERROR, "HTTP_407"

    if status == 429:
        return FetchOutcome.RATE_LIMITED, "HTTP_429"

    if status == 403:
        is_captcha, provider = looks_like_captcha(body_head or b"", body_size=body_size)
        if is_captcha:
            sig = "CF_CHALLENGE" if provider == "cloudflare" else f"CAPTCHA_{(provider or 'unknown').upper()}"
            return FetchOutcome.BOT_BLOCKED, sig
        # F3 — silent-403 / Cloudflare-header detection BEFORE the generic
        # HTTP_403 fallthrough. Silent blocks (empty body or CF headers)
        # carry the BOT_BLOCKED signature so escalation reports + telemetry
        # can distinguish them from substantive 403s (login walls etc.).
        # Run AFTER ``_looks_like_captcha`` so 200-status interstitials are
        # still classified as their CAPTCHA variant.
        if _is_silent_block(status, headers, body_head):
            return FetchOutcome.BOT_BLOCKED, "BOT_BLOCKED"
        # All 403s are bot-blocked — use match_block_signature at call site
        return FetchOutcome.BOT_BLOCKED, "HTTP_403"

    if 500 <= status < 600:
        return FetchOutcome.TRANSIENT, f"HTTP_{status}"

    if 200 <= status < 300:
        # Content-based parked-domain detection.  Domain registrars serve
        # HTTP 200 when a domain has been purchased but not yet pointed at a
        # real site, or when the domain is actively for sale.  These pages
        # look like success to the HTTP layer but are useless for extraction.
        # Classify them as DEAD_URL so the verdict layer can exclude them from
        # the success-rate denominator and route them to re-discovery.
        if body_head and _is_parked_domain(body_head):
            return FetchOutcome.DEAD_URL, "PARKED_DOMAIN"
        # 2026-05-19: bot-block/challenge served WITH a 2xx status.
        # Previously this branch returned OK unconditionally — a
        # DataDome/Akamai 200 interstitial was counted as a successful
        # fetch, flowed into extraction as junk, became the
        # ``unit_count>1`` "success" that self-reinforced a misroute,
        # and the profile never learned it was blocked. High-precision
        # checks only (block-page-only literals) so a legit 200 page is
        # never flipped to BOT_BLOCKED.
        if body_head:
            _cap, _prov = looks_like_captcha(body_head, body_size=body_size)
            if _cap:
                _sig = (
                    "CF_CHALLENGE"
                    if _prov == "cloudflare"
                    else f"CAPTCHA_{(_prov or 'unknown').upper()}"
                )
                return FetchOutcome.BOT_BLOCKED, _sig
        _b200 = looks_like_200_block(body_head, headers)
        if _b200:
            return FetchOutcome.BOT_BLOCKED, f"{_b200.upper()}_200"
        return FetchOutcome.OK, None

    # Stage 3 (2026-05-12): explicit "this resource is gone" statuses are
    # terminal — distinct from other 4xx (HARD_FAIL). The site has either
    # explicitly declared the resource gone (404 / 410) or legally
    # unavailable (451). Reporting excludes these from the success-rate
    # denominator and routes them to a re-discovery queue rather than
    # the standard DLQ retry escalation.
    if status in (404, 410, 451):
        return FetchOutcome.DEAD_URL, f"HTTP_{status}"

    if 400 <= status < 500:
        return FetchOutcome.HARD_FAIL, f"HTTP_{status}"

    # Fallback for unusual status codes
    return FetchOutcome.TRANSIENT, f"HTTP_{status}"
