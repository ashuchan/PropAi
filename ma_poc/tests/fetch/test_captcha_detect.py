"""Tests for captcha_detect — CAPTCHA fingerprint detection."""

from __future__ import annotations

from ma_poc.fetch.captcha_detect import looks_like_captcha


def test_captcha_cloudflare() -> None:
    body = b"<html><script>challenge-platform</script></html>"
    is_captcha, provider = looks_like_captcha(body)
    assert is_captcha is True
    assert provider == "cloudflare"


def test_captcha_recaptcha() -> None:
    body = b'<div class="g-recaptcha" data-sitekey="abc"></div>'
    is_captcha, provider = looks_like_captcha(body)
    assert is_captcha is True
    assert provider == "recaptcha"


def test_captcha_hcaptcha() -> None:
    body = b'<script src="https://hcaptcha.com/1/api.js"></script>'
    is_captcha, provider = looks_like_captcha(body)
    assert is_captcha is True
    assert provider == "hcaptcha"


def test_captcha_clean_html_returns_false() -> None:
    body = b"<html><body><h1>Apartments for Rent</h1></body></html>"
    is_captcha, provider = looks_like_captcha(body)
    assert is_captcha is False
    assert provider is None


def test_captcha_on_binary_garbage_returns_false_safely() -> None:
    body = bytes(range(256)) * 10
    is_captcha, provider = looks_like_captcha(body)
    assert is_captcha is False


# ── F1.2 (2026-05-08 plan) — captcha_detected propagation onto FetchResult ──


def test_f1_2_fetchresult_captcha_detected_field_default_is_false() -> None:
    """F1.2: FetchResult must default ``captcha_detected`` to False so all
    pre-F1.2 constructors (tier_escalator, providers/*, the early-return
    paths in fetcher) keep producing valid results without explicit kwargs."""
    from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode

    fr = FetchResult(
        url="https://x.test/",
        outcome=FetchOutcome.OK,
        status=200,
        body=None,
        headers={},
        render_mode=RenderMode.GET,
        final_url="https://x.test/",
        attempts=1,
        elapsed_ms=10,
    )
    assert fr.captcha_detected is False


def test_f1_2_fetchresult_replace_sets_captcha_detected() -> None:
    """F1.2: ``dataclasses.replace`` on a frozen FetchResult correctly
    propagates the captcha flag — the mechanism the fetcher uses at
    fetcher.py:264 to attach the detector result without breaking the
    frozen contract."""
    import dataclasses

    from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode

    fr = FetchResult(
        url="https://x.test/",
        outcome=FetchOutcome.OK,
        status=200,
        body=b"<html>cf challenge</html>",
        headers={},
        render_mode=RenderMode.RENDER,
        final_url="https://x.test/",
        attempts=1,
        elapsed_ms=42,
    )
    assert fr.captcha_detected is False
    fr2 = dataclasses.replace(fr, captcha_detected=True)
    assert fr2.captcha_detected is True
    # Original is immutable — replace returns a new instance.
    assert fr.captcha_detected is False
    # All other fields preserved.
    assert fr2.url == fr.url
    assert fr2.body == fr.body
    assert fr2.attempts == fr.attempts


# ─────────────────────────────────────────────────────────────────────
# 2026-05-20 cluster #4 shea-style false-positive fix.
#
# The reCAPTCHA / hCaptcha fingerprints are dual-use — they appear both
# on captcha CHALLENGE pages AND on legitimate pages that embed the
# captcha as a widget in contact forms. Without a body-size guard, a
# real apartment page with an embedded reCAPTCHA contact form gets
# misclassified as BOT_BLOCKED → no_body_short_circuit → property
# dropped from the canary.
#
# Live-verified false positive: sheaapartments.com/citylights returns
# 200 OK with 150 KB of real apartment content; the first 4KB contains
# ``g-recaptcha`` inside a WCAG-accessibility-fix JS function for the
# contact-form widget. Pre-fix: classifier returned (True, "recaptcha").
# Post-fix: returns (False, None) because body_size (150_194) exceeds
# the 30_000-byte challenge-interstitial threshold.
# ─────────────────────────────────────────────────────────────────────


def test_recaptcha_widget_on_large_real_page_is_not_captcha() -> None:
    """The shea-style false positive — g-recaptcha widget embedded
    in a 150 KB real apartment page must NOT be flagged as captcha
    when the body size is passed."""
    # Body matches the shea pattern: short captcha widget reference
    # embedded in a much larger page.
    body = (
        b'<html><head><title>Apartments | Real Property</title></head>'
        b'<body><h1>Available units</h1>'
        b'<script>const captchaResponseId = "g-recaptcha-response-100000";</script>'
        b'<div class="content">' + b"x" * 200_000 + b"</div></body></html>"
    )
    is_captcha, provider = looks_like_captcha(body, body_size=len(body))
    assert is_captcha is False, (
        f"large real page with embedded g-recaptcha widget must NOT be "
        f"flagged as captcha; got is_captcha={is_captcha!r} provider={provider!r}"
    )


def test_recaptcha_widget_on_small_body_still_flagged() -> None:
    """A genuine reCAPTCHA challenge interstitial is small (<30 KB).
    The same g-recaptcha marker on a small body must still be flagged
    so real challenges don't slip past."""
    # Small body — mimics a typical challenge interstitial.
    body = (
        b'<html><head><title>Verify</title></head><body>'
        b'<div class="g-recaptcha" data-sitekey="abc"></div>'
        b'</body></html>'
    )
    is_captcha, provider = looks_like_captcha(body, body_size=len(body))
    assert is_captcha is True
    assert provider == "recaptcha"


def test_hcaptcha_widget_on_large_real_page_is_not_captcha() -> None:
    """Same false-positive shape for hCaptcha widget on a real page."""
    body = (
        b'<html><body>'
        b'<script src="https://hcaptcha.com/1/api.js"></script>'
        + b"x" * 100_000
        + b"</body></html>"
    )
    is_captcha, provider = looks_like_captcha(body, body_size=len(body))
    assert is_captcha is False


def test_cloudflare_challenge_always_flagged_regardless_of_size() -> None:
    """CHALLENGE-only fingerprints (cloudflare/perimeterx) are ALWAYS
    trusted — they don't appear on real content pages. Body size guard
    does not apply."""
    # Pad to a large size to verify the size guard doesn't skip
    # challenge-only fingerprints.
    body = (
        b'<html><script>challenge-platform</script>' + b"x" * 200_000 + b"</html>"
    )
    is_captcha, provider = looks_like_captcha(body, body_size=len(body))
    assert is_captcha is True
    assert provider == "cloudflare"


def test_perimeterx_always_flagged_regardless_of_size() -> None:
    """Same for PerimeterX — challenge-only marker, always trusted."""
    body = b'<html><script>_pxhd = "x";</script>' + b"x" * 100_000 + b"</html>"
    is_captcha, provider = looks_like_captcha(body, body_size=len(body))
    assert is_captcha is True
    assert provider == "perimeterx"


def test_body_size_none_preserves_backcompat_behavior() -> None:
    """When body_size is not passed (legacy callers), the function
    falls back to ``len(body)`` so the existing widget-detection tests
    keep their meaning. A small recaptcha-widget body still flags."""
    body = b'<div class="g-recaptcha" data-sitekey="abc"></div>'  # 50 bytes
    is_captcha, provider = looks_like_captcha(body)  # body_size omitted
    assert is_captcha is True
    assert provider == "recaptcha"


# ─────────────────────────────────────────────────────────────────────
# 2026-05-25 — Sucuri / SiteGuard sgcaptcha fingerprint (canary 1ef1060)
#
# AppFolio PROBE cohort (4 props × ~190 units avg = ~738 units) silently
# served Sucuri interstitials that our existing detectors missed because
# the wall returns HTTP 200/202 (not 403) and the body uses none of
# the cloudflare/recaptcha/hcaptcha/perimeterx markers. Adding the
# Sucuri provider so the wall promotes to BOT_BLOCKED and the existing
# Unlocker cascade can recover the unit data.
#
# Real fixture bytes captured from terrainaustin.com/.well-known/sgcaptcha/
# probe on 2026-05-25 (12 KB challenge body).
# ─────────────────────────────────────────────────────────────────────


def test_sucuri_sgcaptcha_title_marker() -> None:
    """Sucuri's <title>Robot Challenge Screen</title> is the most
    distinctive marker — verbatim from the captured 2026-05-25 body."""
    body = (
        b'<!doctype html><html lang="en"><head>'
        b'<title>Robot Challenge Screen</title>'
        b'<script>const sgchallenge="21:1779683624:abc";</script>'
        b'</head><body></body></html>'
    )
    is_captcha, provider = looks_like_captcha(body, body_size=len(body))
    assert is_captcha is True
    assert provider == "sucuri"


def test_sucuri_sgchallenge_js_token() -> None:
    """The sgchallenge JS token alone is sufficient — the title may be
    missing on some Sucuri variants."""
    body = b'<html><body><script>const sgchallenge="abc";</script></body></html>'
    is_captcha, provider = looks_like_captcha(body, body_size=len(body))
    assert is_captcha is True
    assert provider == "sucuri"


def test_sucuri_well_known_sgcaptcha_path_in_body() -> None:
    """Pages that embed the /.well-known/sgcaptcha/ path as a meta-refresh
    target (the no-script fallback Sucuri injects) — also Sucuri."""
    body = (
        b'<html><head>'
        b'<meta http-equiv="refresh" content="0;/.well-known/sgcaptcha/?r=%2F">'
        b'</head><body></body></html>'
    )
    is_captcha, provider = looks_like_captcha(body, body_size=len(body))
    assert is_captcha is True
    assert provider == "sucuri"


def test_sucuri_well_known_captcha_path_in_body() -> None:
    """Belvedere observed final_url at /.well-known/captcha/ (no 'sg' prefix).
    The body for that variant carries the bare /.well-known/captcha/ path."""
    body = (
        b'<html><head>'
        b'<meta http-equiv="refresh" content="0;/.well-known/captcha/?y=ipc:1">'
        b'</head></html>'
    )
    is_captcha, provider = looks_like_captcha(body, body_size=len(body))
    assert is_captcha is True
    assert provider == "sucuri"


def test_sucuri_marker_on_large_body_still_flagged() -> None:
    """Sucuri lives in the challenge-only bucket — always flagged
    regardless of body size (no widget-form false-positive worry,
    since Robot Challenge Screen never appears on real apartment pages)."""
    body = (
        b'<html><head><title>Robot Challenge Screen</title></head>'
        + b"x" * 200_000
        + b'</html>'
    )
    is_captcha, provider = looks_like_captcha(body, body_size=len(body))
    assert is_captcha is True
    assert provider == "sucuri"


def test_clean_html_with_word_challenge_is_not_sucuri() -> None:
    """The word 'challenge' appears on real apartment-leasing pages
    ('challenge yourself to', 'no leasing challenges') but we don't
    use it as a Sucuri fingerprint. Verify a clean page with that
    word isn't false-flagged."""
    body = (
        b'<html><body><h1>Live the Challenge</h1>'
        b'<p>Modern apartments, on-site fitness challenge.</p>'
        b'</body></html>'
    )
    is_captcha, provider = looks_like_captcha(body, body_size=len(body))
    assert is_captcha is False


def test_real_shea_first_4kb_with_full_size_is_not_captcha() -> None:
    """Synthesized from the actual 2026-05-20 live-probe bytes —
    sheaapartments.com/citylights first 4KB has g-recaptcha inside
    a wcagFix() JS function; full body is 150 KB."""
    head = (
        b'<!DOCTYPE html>\n<html class="no-js" lang="en"><head>'
        b'<link rel="preload" href="/assets/fonts/38D01F_0_0.woff" as="font">'
        b'<script>function wcagFix(){'
        b'const captchaResponseId="g-recaptcha-response-100000";'
        b'}</script></head><body>...real content...'
    )
    # Pad to match the real shea body size (~150 KB).
    full_body = head + (b"x" * (150_000 - len(head)))
    is_captcha, provider = looks_like_captcha(full_body, body_size=len(full_body))
    assert is_captcha is False, (
        f"shea-style real page (150KB body, g-recaptcha in head) must NOT "
        f"flag as captcha; got is_captcha={is_captcha!r} provider={provider!r}"
    )
