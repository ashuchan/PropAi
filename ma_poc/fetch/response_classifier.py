"""Response classifier — maps (status, headers, body, exception) to FetchOutcome.

Pure function, no I/O. Used by the fetcher to decide what happened.

Sources consulted:
- Cloudflare challenge page patterns (developer docs)
- Standard HTTP status code semantics (RFC 9110)
"""

from __future__ import annotations

import asyncio
import logging
import re
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


# ── Soft-404 content fingerprints ─────────────────────────────────────────────
# A "soft 404" serves HTTP 200 but the page IS a not-found / error placeholder:
# the property's specific path is dead (or its whole domain now redirects to a
# management-company "notfound" host), yet the origin returns 200 so the HTTP
# layer sees a success. The page then flows into extraction as junk, yields no
# units, and is logged as a generic FAILED_NO_DATA — indistinguishable from a
# real property with zero inventory, and re-fetched/escalated every run for a
# resource that no longer exists.
#
# Precision is critical: a false positive marks a LIVE property DEAD_URL,
# dropping it from the success-rate denominator and the retry queue. We anchor
# the match to the page's own <title>/<h1> (its authoritative headline) rather
# than a body substring — "404" appears incidentally in asset hashes, analytics
# and script paths on real pages, so a raw body scan over-fires. Deliberately
# EXCLUDES "coming soon" / "under construction": those are legitimate lease-up
# (LEASE_UP) properties whose site is a preview, not dead URLs.
#
# Validated 2026-07-16: 11/11 soft-404s in the FAILED cohort caught; 0 false
# positives across 60 GOLD (SUCCESS) property pages + 40 reached-but-no-data
# pages. See project_full_nongold_audit_2026-07-15 (#28).
_SOFT_404_TITLE_PHRASES: tuple[str, ...] = (
    "page not found",
    "page cannot be found",
    "page could not be found",
    "page doesn't exist",
    "page does not exist",
    "page can't be found",
    "page not available",
    "this page isn't available",
    "this page is not available",
    "404 not found",
    "404 error",
    "error 404",
    "http 404",
    "404 page",
    "site not found",
    "account suspended",
    "nothing found",
)

# Exact-title matches for terse error pages whose <title> is just the code /
# phrase (kept separate so the substring list above can't over-match a longer
# legitimate title that happens to contain one of these short strings).
_SOFT_404_TITLE_EXACT: frozenset[str] = frozenset(
    {"404", "not found", "404 not found", "page not found"}
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)


def _tag_text(pattern: re.Pattern[str], text: str) -> str:
    """Extract and normalise the inner text of the first tag match (lowercased)."""
    m = pattern.search(text)
    if not m:
        return ""
    inner = re.sub(r"<[^>]+>", " ", m.group(1))
    return re.sub(r"\s+", " ", inner).strip().lower()


def _is_soft_404(body_head: bytes) -> bool:
    """Return True if body_head is an HTTP-200 not-found / error placeholder.

    High precision by design (see ``_SOFT_404_TITLE_PHRASES``): only fires when
    the page's own ``<title>`` or ``<h1>`` headline names it a not-found/error
    page. Never raises — decodes as latin-1 like ``_is_parked_domain``.
    """
    if not body_head:
        return False
    try:
        text = body_head[:4096].decode("latin-1")
    except Exception:
        return False
    title = _tag_text(_TITLE_RE, text)
    h1 = _tag_text(_H1_RE, text)
    if title in _SOFT_404_TITLE_EXACT or h1 in _SOFT_404_TITLE_EXACT:
        return True
    return any(p in title or p in h1 for p in _SOFT_404_TITLE_PHRASES)

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


# ── Exception signatures ─────────────────────────────────────────────────────
# 2026-07-27 RCA: the 267 fetches that a dead datacentre proxy failed all
# recorded the bare signature ``"Error"``. That is not a placeholder — Playwright's
# *base exception class is literally named* ``Error``
# (``playwright._impl._errors.Error``), so ``type(exc).__name__`` renders every
# Chromium navigation failure as the single least informative string possible,
# while the message — which carried ``net::ERR_TUNNEL_CONNECTION_FAILED``, the
# exact fingerprint of a black-holing proxy — was discarded. Eight days of
# opaque telemetry followed. Preserve the message, but as a *bounded token*:
# ``error_signature`` is group-by'd in analyze_cloud_run.py and stored in a
# String(512) column, so raw messages (URLs, IPs, ports) would explode
# cardinality and make the aggregates useless in the other direction.

#: Transport error codes worth preserving verbatim — Chromium ``net::ERR_*``,
#: bare ``ERR_*``, curl ``CURLE_*``, and OpenSSL-style ``SSL_ERROR_*``.
_ERROR_CODE_RE = re.compile(
    r"\b(net::ERR_[A-Z0-9_]+|ERR_[A-Z0-9_]+|CURLE_[A-Z0-9_]+|SSL_ERROR_[A-Z0-9_]+)"
)
#: Noise that would blow up cardinality: URLs, host:port pairs, bare IPs.
_NOISE_RE = re.compile(
    r"https?://\S+|\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b|\b[\w.-]+\.[a-z]{2,}(?::\d+)?\b",
    re.IGNORECASE,
)
_MAX_TOKEN_CHARS = 48


def exception_signature(exception: BaseException, prefix: str | None = None) -> str:
    """Build a bounded, greppable signature preserving exception type *and* cause.

    Mirrors ``hyperbrowser_backend``'s ``f"hb:{type(exc).__name__}"`` convention
    but keeps the message, because the type alone is useless for the exception
    classes that matter most here (Playwright's is named ``Error``).

    Args:
        exception: The exception raised during the request.
        prefix: Optional namespace prepended to the type, e.g. ``"hb"`` or
            ``"render"``. The type name is always kept — a namespace alone
            would reintroduce exactly the ambiguity this function removes.

    Returns:
        ``"[<prefix>:]<type>[:<token>]"`` — e.g.
        ``"Error:net::ERR_TUNNEL_CONNECTION_FAILED"`` or
        ``"hb:Error:net::ERR_PROXY_CONNECTION_FAILED"``. Falls back to the bare
        type name when the exception carries no usable message.
    """
    head = type(exception).__name__
    if prefix:
        head = f"{prefix}:{head}"
    try:
        message = str(exception).strip()
    except Exception:  # pragma: no cover - pathological __str__
        message = ""
    if not message:
        return head

    # 1. A recognised transport code is the most useful token — keep it verbatim.
    code = _ERROR_CODE_RE.search(message)
    if code:
        return f"{head}:{code.group(1)}"

    # 2. Otherwise normalise the first line to a low-cardinality slug.
    first_line = message.splitlines()[0]
    scrubbed = _NOISE_RE.sub(" ", first_line)
    slug = re.sub(r"[^a-z0-9]+", "_", scrubbed.lower()).strip("_")
    if not slug:
        return head
    return f"{head}:{slug[:_MAX_TOKEN_CHARS].rstrip('_')}"


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
            return FetchOutcome.TRANSIENT, exception_signature(exception)
        # Unknown exception with no status
        if status is None:
            return FetchOutcome.TRANSIENT, exception_signature(exception)

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
        # Content-based soft-404 detection. Runs AFTER the captcha / 200-block
        # checks so a bot-block interstitial still escalates tiers rather than
        # being mislabelled terminal-dead. A 200 whose <title>/<h1> declares
        # itself a not-found page is a dead URL: terminal, excluded from the
        # success-rate denominator, routed to re-discovery (same as HTTP 404).
        if body_head and _is_soft_404(body_head):
            return FetchOutcome.DEAD_URL, "SOFT_404"
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
