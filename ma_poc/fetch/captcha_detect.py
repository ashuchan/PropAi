"""CAPTCHA detection — inspects response body for known challenge page patterns.

Pure function, no I/O. Only inspects the first ~4KB of body bytes.

Sources consulted:
- Cloudflare challenge page HTML (https://developers.cloudflare.com)
- reCAPTCHA v2/v3 integration docs (https://developers.google.com/recaptcha)
- hCaptcha integration docs (https://docs.hcaptcha.com)
- PerimeterX bot detection patterns (public analysis)
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Fingerprints keyed by provider name
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
}


def looks_like_captcha(body: bytes) -> tuple[bool, str | None]:
    """Detect whether a response body contains a CAPTCHA challenge.

    Args:
        body: Raw response body bytes (first ~4KB is sufficient).

    Returns:
        Tuple of (is_captcha, provider_name_or_none).
        Provider is one of: cloudflare, recaptcha, hcaptcha, perimeterx.
    """
    if not body:
        return False, None
    # Guard against binary garbage — only inspect if it looks text-like
    # by checking the first 100 bytes for printable ASCII / UTF-8
    try:
        head = body[:4096]
    except Exception:
        return False, None

    for provider, patterns in _FINGERPRINTS.items():
        for pattern in patterns:
            if pattern in head:
                return True, provider

    return False, None
