"""CAPTCHA detection — inspects response body for known challenge page patterns.

Pure function, no I/O. Only inspects the first ~4KB of body bytes.

Sources consulted:
- Cloudflare challenge page HTML (https://developers.cloudflare.com)
- reCAPTCHA v2/v3 integration docs (https://developers.google.com/recaptcha)
- hCaptcha integration docs (https://docs.hcaptcha.com)
- PerimeterX bot detection patterns (public analysis)
- SiteGround SGCAPTCHA patterns (observed in the wild, 2026-05-18)
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Byte-literal fingerprints — fast path, used by looks_like_captcha().
# Ordered by specificity so earlier entries win on ambiguous pages.
_FINGERPRINTS: dict[str, list[bytes]] = {
    "cloudflare": [
        b"challenge-platform",
        b"__cf_chl_",
        b"Just a moment...",
    ],
    "recaptcha": [
        b"g-recaptcha",
        b"www.google.com/recaptcha",
    ],
    "hcaptcha": [
        b"hcaptcha.com",
        b"h-captcha",
    ],
    "perimeterx": [
        b"_pxhd",
        b"PerimeterX",
    ],
    # SGCaptcha — HTTP 202 response; final_url redirects to
    # /.well-known/sgcaptcha/.  Body is a JS interstitial wrapper.
    # Primary detection is via final_url (see fetcher.py), but the body
    # fingerprint catches cases where the redirect didn't update page.url.
    "sgcaptcha": [
        b"sgcaptcha",
        b"/.well-known/sgcaptcha/",
        b"sg-captcha",
    ],
}

# Regex patterns — used by detect_provider().  More specific than the byte
# literals above: anchored substrings, dotall not needed (we slice to 4KB),
# and patterns that require word-boundary or path context to avoid false
# positives (e.g. "g-recaptcha" vs "g-recaptcha-response" in a submit form).
_PROVIDER_PATTERNS: dict[str, tuple[str, ...]] = {
    "cloudflare": (
        r"cdn-cgi/challenge-platform",
        r"__cf_chl_managed_tk__",
        r"Just a moment\.\.\.",
    ),
    "sgcaptcha": (
        r"sgcaptcha\.com",
        r"siteground\.com.*?security",
    ),
    "perimeterx": (
        r"_pxAppId",
        r"px-captcha\.com",
    ),
    "hcaptcha": (
        r"hcaptcha\.com/captcha",
        r"data-hcaptcha-sitekey",
    ),
    "recaptcha": (
        r"www\.google\.com/recaptcha/api\.js",
        r"g-recaptcha",
    ),
}


def detect_provider(body: bytes) -> str | None:
    """Identify the WAF / CAPTCHA provider from a response body.

    Uses regex patterns that are more specific than the byte-literal
    fingerprints in ``looks_like_captcha`` — anchored substrings prevent
    false positives from pages that contain provider names in comments or
    script references without actually serving a challenge.

    Inspects only the first 4 KB; challenge payloads always surface their
    provider tokens near the top of the document.

    Args:
        body: Raw response body bytes.

    Returns:
        Provider name (e.g. ``"cloudflare"``, ``"sgcaptcha"``), or ``None``
        when no known provider matched.
    """
    if not body:
        return None
    try:
        text = body[:4096].decode("utf-8", "ignore")
    except Exception:
        return None
    for provider, patterns in _PROVIDER_PATTERNS.items():
        if any(re.search(p, text, re.IGNORECASE) for p in patterns):
            return provider
    return None


def looks_like_captcha(body: bytes) -> tuple[bool, str | None]:
    """Detect whether a response body contains a CAPTCHA challenge.

    Tries the regex-based ``detect_provider`` first (more specific), then
    falls back to byte-literal fingerprints for byte patterns that don't
    have a regex equivalent.

    Args:
        body: Raw response body bytes (first ~4KB is sufficient).

    Returns:
        Tuple of (is_captcha, provider_name_or_none).
        Provider is one of: cloudflare, recaptcha, hcaptcha, perimeterx,
        sgcaptcha.
    """
    if not body:
        return False, None

    # Regex path — preferred because patterns are more specific.
    provider = detect_provider(body)
    if provider:
        return True, provider

    # Byte-literal fallback for patterns not yet ported to regex.
    try:
        head = body[:4096]
    except Exception:
        return False, None

    for provider_name, patterns in _FINGERPRINTS.items():
        for pattern in patterns:
            if pattern in head:
                return True, provider_name

    return False, None
