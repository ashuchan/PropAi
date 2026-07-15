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
from enum import StrEnum

log = logging.getLogger(__name__)


class ChallengeKind(StrEnum):
    """How an anti-bot interstitial should be handled by a real-browser render.

    This is the legal boundary for the clean "2a" residential-render tier
    (:mod:`ma_poc.fetch.providers.residential_render`): a real browser is
    allowed to *pass* a challenge only by rendering the page and *waiting*
    for the site's own JavaScript to clear it — the same thing every human
    visitor's browser does. It is NEVER allowed to *solve* a challenge
    (click a widget, answer an image grid, run a CAPTCHA solver).
    """

    #: No challenge — the body is the real content.
    NONE = "NONE"
    #: A JavaScript interstitial (Cloudflare "Just a moment…", managed
    #: challenge) that a real browser clears on its own by executing the
    #: page's JS and waiting. Passing it = "being a browser", not defeating
    #: a control. The render tier waits and re-checks.
    PASSABLE_JS = "PASSABLE_JS"
    #: An INTERACTIVE captcha (hCaptcha, reCAPTCHA image grid, PerimeterX
    #: press-and-hold, Sucuri "Robot Challenge Screen") that requires a
    #: human action to clear. The render tier must ABORT here — never click,
    #: never solve. Reaching one means the site is gating with a real
    #: access control and we go out of scope.
    INTERACTIVE = "INTERACTIVE"


# Providers whose interstitial a real browser clears by executing JS +
# waiting (no human action). Cloudflare's managed / JS challenge is the
# canonical case. If it does NOT clear within the wait budget, the render
# tier treats it as still-blocked and aborts — it never interacts.
_PASSABLE_JS_PROVIDERS: frozenset[str] = frozenset({"cloudflare"})

# Providers whose interstitial requires a human action. The render tier
# aborts on sight — solving/clicking these is exactly the line we won't
# cross.
_INTERACTIVE_PROVIDERS: frozenset[str] = frozenset(
    {"recaptcha", "hcaptcha", "perimeterx", "sucuri"}
)

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
    # Sucuri / SiteGuard CAPTCHA (sgcaptcha). Verified 2026-05-25 against
    # canary 1ef1060 — affects ~738 units across the AppFolio cohort
    # (Reserve at Belvedere, Heritage by Fairlawn, Vivid, Terrain,
    # Reserve at Stone Port). Sucuri serves a ~12KB interstitial at
    # /.well-known/sgcaptcha/?r=... or /.well-known/captcha/?y=... that
    # carries a <title>Robot Challenge Screen</title> + a sgchallenge
    # JS token. These markers do not appear on any real apartment page.
    # Without this fingerprint the body inspector misses the wall and
    # the page reaches the extractor as a successful 200/202 OK,
    # producing 0 units and a misleading TIER_1_API_* ran_empty event.
    "sucuri": [
        b"Robot Challenge Screen",
        b"sgchallenge",
        b"/.well-known/sgcaptcha/",
        b"/.well-known/captcha/",
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


def classify_challenge(
    body: bytes,
    body_size: int | None = None,
) -> tuple[ChallengeKind, str | None]:
    """Classify an anti-bot interstitial for the clean render tier.

    Wraps :func:`looks_like_captcha` and maps the detected provider to a
    :class:`ChallengeKind`, which decides the render tier's behaviour:

      * ``NONE``        → the body is real content; return it.
      * ``PASSABLE_JS`` → a Cloudflare JS/managed challenge; a real browser
        clears it by executing the page JS and waiting. The tier waits and
        re-checks; it never interacts.
      * ``INTERACTIVE`` → an hCaptcha / reCAPTCHA / PerimeterX / Sucuri
        challenge that needs a human action; the tier ABORTS (never solves).

    Returns ``(kind, provider_or_none)``. This is a pure function — the
    same body-size guard as :func:`looks_like_captcha` applies so real
    pages that merely embed a captcha widget are ``NONE``.
    """
    is_captcha, provider = looks_like_captcha(body, body_size)
    if not is_captcha:
        return ChallengeKind.NONE, None
    if provider in _PASSABLE_JS_PROVIDERS:
        return ChallengeKind.PASSABLE_JS, provider
    # Any recognised non-JS provider (or an unexpected/unknown one) is
    # treated as interactive — we fail safe toward NOT interacting.
    return ChallengeKind.INTERACTIVE, provider
