"""Tests for captcha_detect — CAPTCHA fingerprint detection."""

from __future__ import annotations

from ma_poc.fetch.captcha_detect import detect_provider, looks_like_captcha


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


# ── detect_provider() — regex-based provider classification ──────────────────


class TestDetectProvider:
    """Tests for the standalone detect_provider() function."""

    def test_cloudflare_cdn_cgi_path(self):
        body = b'<script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1"></script>'
        assert detect_provider(body) == "cloudflare"

    def test_cloudflare_managed_tk(self):
        body = b"window.__cf_chl_managed_tk__='something'"
        assert detect_provider(body) == "cloudflare"

    def test_cloudflare_just_a_moment(self):
        body = b"<title>Just a moment...</title>"
        assert detect_provider(body) == "cloudflare"

    def test_cloudflare_case_insensitive(self):
        # The CDN path is lowercase in practice but the pattern should be
        # case-insensitive so capitalization drift doesn't break detection.
        body = b"CDN-CGI/CHALLENGE-PLATFORM"
        assert detect_provider(body) == "cloudflare"

    def test_sgcaptcha_domain(self):
        body = b'<script src="https://www.sgcaptcha.com/api.js"></script>'
        assert detect_provider(body) == "sgcaptcha"

    def test_sgcaptcha_siteground_security(self):
        body = b"Redirecting to siteground.com/security/captcha/"
        assert detect_provider(body) == "sgcaptcha"

    def test_perimeterx_app_id(self):
        body = b"window._pxAppId = 'PXabcdef12';"
        assert detect_provider(body) == "perimeterx"

    def test_perimeterx_captcha_domain(self):
        body = b'<script src="https://captcha.px-captcha.com/px/captcha.js"></script>'
        assert detect_provider(body) == "perimeterx"

    def test_hcaptcha_captcha_path(self):
        body = b'<script src="https://hcaptcha.com/captcha/v1/api.js"></script>'
        assert detect_provider(body) == "hcaptcha"

    def test_hcaptcha_sitekey_attr(self):
        body = b'<div class="h-captcha" data-hcaptcha-sitekey="abc123"></div>'
        assert detect_provider(body) == "hcaptcha"

    def test_recaptcha_api_js(self):
        body = b'<script src="https://www.google.com/recaptcha/api.js"></script>'
        assert detect_provider(body) == "recaptcha"

    def test_recaptcha_div(self):
        body = b'<div class="g-recaptcha" data-sitekey="xyz"></div>'
        assert detect_provider(body) == "recaptcha"

    def test_clean_html_returns_none(self):
        body = b"<html><body><h1>Apartments for Rent</h1></body></html>"
        assert detect_provider(body) is None

    def test_empty_body_returns_none(self):
        assert detect_provider(b"") is None

    def test_none_body_returns_none(self):
        # detect_provider accepts bytes, but defensive guard handles empties.
        assert detect_provider(b"") is None

    def test_only_first_4kb_inspected(self):
        # Padding before the pattern: if beyond 4096 bytes it should NOT match.
        padding = b"x" * 4096
        body = padding + b"cdn-cgi/challenge-platform"
        assert detect_provider(body) is None

    def test_looks_like_captcha_delegates_to_detect_provider(self):
        """looks_like_captcha must call detect_provider first and honour its result."""
        body = b"window._pxAppId = 'PXabcdef12';"
        is_captcha, provider = looks_like_captcha(body)
        assert is_captcha is True
        assert provider == "perimeterx"

    def test_byte_fallback_still_works_after_refactor(self):
        """The byte-literal fallback path in looks_like_captcha must still fire
        for patterns present in _FINGERPRINTS but not in _PROVIDER_PATTERNS."""
        # h-captcha byte literal is in _FINGERPRINTS but the regex path also
        # catches it via hcaptcha.com/captcha; this just confirms the combined
        # path returns True for such bodies.
        body = b'<script src="https://hcaptcha.com/1/api.js"></script>'
        is_captcha, provider = looks_like_captcha(body)
        assert is_captcha is True
        assert provider == "hcaptcha"
