"""CAPTCHA detection — inspects response body for known challenge page patterns.

Pure function, no I/O. Only inspects the first ~4KB of body bytes.

Sources consulted:
- Cloudflare challenge page HTML (https://developers.cloudflare.com)
- reCAPTCHA v2/v3 integration docs (https://developers.google.com/recaptcha)
- hCaptcha integration docs (https://docs.hcaptcha.com)
- PerimeterX bot detection patterns (public analysis)

2026-05-20 false-positive fix (cluster #4 shea-style 200-OK sub-pattern):
The reCAPTCHA / hCaptcha fingerprints (``g-recaptcha``, ``hcaptcha.com``,
etc.) are dual-use — they appear both on real captcha CHALLENGE
interstitials AND on legitimate pages that embed the captcha as a widget
in a contact / lease-inquiry form. Treating any presence as a challenge
flips 200-OK real pages to BOT_BLOCKED → no_body_short_circuit, dropping
the property. Live-verified false positive: sheaapartments.com/citylights
(150 KB real apartment page; first 4KB has ``g-recaptcha`` inside a
WCAG-accessibility-fix JS function for the contact-form widget).

Fix: split fingerprints into CHALLENGE-only (always trusted) and
WIDGET-dual-use (only trusted when ``body_size`` is below the
``_LIKELY_CHALLENGE_BODY_MAX`` threshold — real apartment pages are
50-300 KB; challenge interstitials are typically 5-15 KB).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Real captcha CHALLENGE pages are small — typically 3-15 KB of minimal
# layout + the challenge widget. Real content pages with embedded
# captcha widgets are 30 KB+. Threshold set conservatively at 30 KB so
# any genuine challenge interstitial stays below.
_LIKELY_CHALLENGE_BODY_MAX: int = 30_000

# Challenge-only fingerprints — appear only on actual challenge
# interstitials, never on widgets embedded in real pages. Always trusted
# regardless of body size.
_CHALLENGE_ONLY_FINGERPRINTS: dict[str, list[bytes]] = {
    "cloudflare": [
        b"challenge-platform",
        b"__cf_chl_",
        b"Just a moment...",
    ],
    "perimeterx": [
        b"_pxhd",
        b"PerimeterX",
    ],
}

# Widget OR challenge — these strings appear on both REAL pages with
# embedded captcha widgets AND on challenge interstitials. Only trusted
# as captcha when ``body_size`` is below ``_LIKELY_CHALLENGE_BODY_MAX``.
# Without the size guard, every contact-form / lease-inquiry / login
# page that uses captcha gets misclassified as BOT_BLOCKED.
_WIDGET_DUAL_USE_FINGERPRINTS: dict[str, list[bytes]] = {
    "recaptcha": [
        b"g-recaptcha",
        b"www.google.com/recaptcha",
    ],
    "hcaptcha": [
        b"hcaptcha.com",
        b"h-captcha",
    ],
}

# Back-compat alias: combined view of all fingerprints (used by anything
# external that imports ``_FINGERPRINTS``). The new code paths should
# iterate the two split dicts above with the size guard.
_FINGERPRINTS: dict[str, list[bytes]] = {
    **_CHALLENGE_ONLY_FINGERPRINTS,
    **_WIDGET_DUAL_USE_FINGERPRINTS,
}


def looks_like_captcha(
    body: bytes,
    body_size: int | None = None,
) -> tuple[bool, str | None]:
    """Detect whether a response body contains a CAPTCHA challenge.

    Args:
        body: Raw response body bytes (first ~4KB is sufficient).
        body_size: Total response-body size in bytes (NOT just the
            slice length). When ``None``, the function falls back to
            ``len(body)`` — which is correct iff the caller passed the
            full body, but underestimates when only a head slice was
            passed. Callers that have the full body should pass its
            length explicitly. When the resolved size exceeds
            ``_LIKELY_CHALLENGE_BODY_MAX``, the widget-dual-use
            fingerprints (``g-recaptcha`` / ``hcaptcha.com``, etc.) are
            skipped to avoid false-positives on real pages that embed
            captcha widgets in contact forms.

    Returns:
        Tuple of (is_captcha, provider_name_or_none).
        Provider is one of: cloudflare, recaptcha, hcaptcha, perimeterx.
    """
    if not body:
        return False, None
    try:
        head = body[:4096]
    except Exception:
        return False, None

    # CHALLENGE-only markers — always trusted.
    for provider, patterns in _CHALLENGE_ONLY_FINGERPRINTS.items():
        for pattern in patterns:
            if pattern in head:
                return True, provider

    # WIDGET-dual-use markers — only trusted on small bodies (likely
    # challenge interstitials, not real pages with embedded widgets).
    # Resolve the effective body size: caller-provided > body slice len.
    effective_size = body_size if body_size is not None else len(body)
    if effective_size <= _LIKELY_CHALLENGE_BODY_MAX:
        for provider, patterns in _WIDGET_DUAL_USE_FINGERPRINTS.items():
            for pattern in patterns:
                if pattern in head:
                    return True, provider

    return False, None
