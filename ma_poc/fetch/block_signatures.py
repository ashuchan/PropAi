"""Identify which anti-bot system blocked a response.

The signature is informational (logged, persisted to profile, surfaces in
reports) AND functional (Phase E4 uses it to skip known-doomed tiers).

Order matters: more specific patterns first. cf_turnstile must match before
cf_challenge because turnstile pages also include challenge markers.
"""

from __future__ import annotations

from typing import Final

BLOCK_SIGNATURES: Final[list[tuple[str, list[bytes]]]] = [
    # Cloudflare Turnstile (newer, harder)
    ("cf_turnstile", [
        b"challenges.cloudflare.com/turnstile",
        b"cf-turnstile-response",
    ]),
    # Cloudflare Managed Challenge / JS challenge
    ("cf_challenge", [
        b"Just a moment",
        b"cf-chl-bypass",
        b"cdn-cgi/challenge-platform",
        b"__cf_chl_",
    ]),
    # PerimeterX
    ("px_block", [
        b"px-captcha",
        b"_pxhd",
        b"PerimeterX",
        b"px-cdn.net",
    ]),
    # DataDome
    ("datadome", [
        b"datadome",
        b"dd_cookie",
        b"geo.captcha-delivery.com",
    ]),
    # Akamai Bot Manager
    ("akamai_bm", [
        b"_abck",
        b"ak_bmsc",
        b"reference #18.",  # akamai error reference pattern prefix
    ]),
    # hCaptcha (often layered on Cloudflare)
    ("hcaptcha", [
        b"hcaptcha.com/captcha",
        b"h-captcha",
    ]),
    # reCAPTCHA
    ("recaptcha", [
        b"recaptcha/api2",
        b"g-recaptcha",
    ]),
    # Imperva / Incapsula
    ("imperva", [
        b"_Incapsula_Resource",
        b"visid_incap_",
    ]),
    # Generic 403 with no recognized signature — fallback last
    ("generic_403", []),
]


def match_block_signature(
    body: bytes | None,
    headers: dict[str, str] | None,
    status_code: int,
) -> str | None:
    """Return the signature name that matches, or None if no block detected.

    Examines body bytes and select headers (CF-RAY, X-DataDome).
    Never raises — returns None on any error.

    Args:
        body: Raw response body bytes or None.
        headers: Response headers (any case) or None.
        status_code: HTTP status code.

    Returns:
        Signature name string or None.
    """
    try:
        if status_code != 403 and not _looks_like_challenge_page(body, status_code):
            return None

        body_bytes = body or b""
        # Cap body scan at first 64KB — challenge pages put markers up top
        scan = body_bytes[:65536]
        headers_lower = {k.lower(): v for k, v in (headers or {}).items()}

        for name, patterns in BLOCK_SIGNATURES:
            if name == "generic_403":
                continue  # fallback, handled below

            if patterns and any(p in scan for p in patterns):
                return name

            # Header-based matches for select signatures
            if name == "cf_challenge":
                if "cf-ray" in headers_lower and status_code in (403, 503):
                    return name
            elif name == "datadome":
                if "x-datadome" in headers_lower:
                    return name

        # Fallback: 403 with nothing recognized
        if status_code == 403:
            return "generic_403"
        return None
    except Exception:
        return None


def _looks_like_challenge_page(body: bytes | None, status: int) -> bool:
    """Some challenges return 200 with a JS-redirect interstitial."""
    if status != 200 or not body:
        return False
    head = body[:4096].lower()
    return b"challenge" in head and (b"javascript" in head or b"redirect" in head)
