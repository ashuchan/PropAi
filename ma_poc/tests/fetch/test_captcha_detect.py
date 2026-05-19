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
