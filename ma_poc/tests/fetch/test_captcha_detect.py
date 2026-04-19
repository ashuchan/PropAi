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
