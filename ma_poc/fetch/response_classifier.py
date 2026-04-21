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
from socket import gaierror

from .captcha_detect import looks_like_captcha
from .contracts import FetchOutcome

log = logging.getLogger(__name__)

# Exceptions that indicate DNS resolution failure
_DNS_ERRORS = (gaierror, OSError)

# Playwright's TimeoutError is its own hierarchy: playwright._impl._errors.Error
# → Exception. It does NOT inherit from asyncio.TimeoutError or the builtin
# TimeoutError, so `isinstance(exc, TimeoutError)` misses it and the classifier
# falls through to a generic "TimeoutError" signature instead of "timeout".
# Downstream retry logic keys on "TIMEOUT" in the signature and happened to work
# by accident, but diagnostic tooling and retry-after behavior break. Import
# defensively so the classifier remains usable without Playwright installed.
try:  # pragma: no cover — import-time only
    from playwright._impl._errors import TimeoutError as _PlaywrightTimeoutError
except Exception:  # pragma: no cover
    _PlaywrightTimeoutError = None  # type: ignore[assignment,misc]


def classify(
    status: int | None,
    headers: dict[str, str],
    body_head: bytes | None,
    exception: Exception | None = None,
) -> tuple[FetchOutcome, str | None]:
    """Classify an HTTP response into a FetchOutcome.

    Args:
        status: HTTP status code, or None if no response received.
        headers: Response headers with lowercased keys.
        body_head: First ~4KB of the response body.
        exception: Exception raised during the request, if any.

    Returns:
        Tuple of (FetchOutcome, error_signature_or_none).
    """
    # Exception-based classification first
    if exception is not None:
        if isinstance(exception, (ssl.SSLError, ssl.SSLCertVerificationError)):
            return FetchOutcome.HARD_FAIL, "ERR_SSL_PROTOCOL_ERROR"
        if isinstance(exception, _DNS_ERRORS):
            err_msg = str(exception).lower()
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
        is_captcha, provider = looks_like_captcha(body_head or b"")
        if is_captcha:
            return (
                FetchOutcome.BOT_BLOCKED,
                "CF_CHALLENGE" if provider == "cloudflare" else f"CAPTCHA_{(provider or 'unknown').upper()}",
            )
        return FetchOutcome.HARD_FAIL, "HTTP_403"

    if 500 <= status < 600:
        return FetchOutcome.TRANSIENT, f"HTTP_{status}"

    if 200 <= status < 300:
        return FetchOutcome.OK, None

    if 400 <= status < 500:
        return FetchOutcome.HARD_FAIL, f"HTTP_{status}"

    # Fallback for unusual status codes
    return FetchOutcome.TRANSIENT, f"HTTP_{status}"
