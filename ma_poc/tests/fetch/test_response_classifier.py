"""Tests for response_classifier — pure function mapping responses to outcomes."""

from __future__ import annotations

import ssl
from socket import gaierror

from ma_poc.fetch.contracts import FetchOutcome
from ma_poc.fetch.response_classifier import classify


def test_classify_ok_200() -> None:
    outcome, sig = classify(200, {}, b"<html>")
    assert outcome == FetchOutcome.OK
    assert sig is None


def test_classify_not_modified() -> None:
    outcome, sig = classify(304, {}, None)
    assert outcome == FetchOutcome.NOT_MODIFIED
    assert sig is None


def test_classify_proxy_407() -> None:
    outcome, sig = classify(407, {}, b"")
    assert outcome == FetchOutcome.PROXY_ERROR
    assert sig == "HTTP_407"


def test_classify_rate_limited() -> None:
    outcome, _sig = classify(429, {"retry-after": "10"}, b"")
    assert outcome == FetchOutcome.RATE_LIMITED


def test_classify_captcha_cloudflare() -> None:
    body = b"<html>Just a moment...challenge-platform</html>"
    outcome, sig = classify(403, {}, body)
    assert outcome == FetchOutcome.BOT_BLOCKED
    assert sig == "CF_CHALLENGE"


def test_classify_5xx_transient() -> None:
    outcome, _sig = classify(503, {}, b"")
    assert outcome == FetchOutcome.TRANSIENT


def test_classify_ssl_error() -> None:
    exc = ssl.SSLError("SSL handshake failed")
    outcome, sig = classify(None, {}, None, exception=exc)
    assert outcome == FetchOutcome.HARD_FAIL
    assert sig == "ERR_SSL_PROTOCOL_ERROR"


def test_classify_dns_error() -> None:
    exc = gaierror("getaddrinfo failed")
    outcome, sig = classify(None, {}, None, exception=exc)
    assert outcome == FetchOutcome.HARD_FAIL
    assert sig == "ERR_DNS"


def test_classify_timeout_exception() -> None:
    exc = TimeoutError()
    outcome, sig = classify(None, {}, None, exception=exc)
    assert outcome == FetchOutcome.TRANSIENT
    assert sig == "timeout"


def test_classify_playwright_timeout_exception() -> None:
    # Playwright's TimeoutError is its own hierarchy and does not inherit from
    # asyncio.TimeoutError or builtins.TimeoutError — before the classifier was
    # taught about it, render-mode navigation timeouts fell through to a
    # generic "TimeoutError" signature, which broke retry back-off that keyed
    # on the literal string "timeout".
    from playwright._impl._errors import TimeoutError as PlaywrightTimeoutError

    exc = PlaywrightTimeoutError("Page.goto: Timeout 20000ms exceeded.")
    outcome, sig = classify(None, {}, None, exception=exc)
    assert outcome == FetchOutcome.TRANSIENT
    assert sig == "timeout"


def test_classify_404_hard_fail() -> None:
    outcome, sig = classify(404, {}, b"")
    assert outcome == FetchOutcome.HARD_FAIL
    assert sig == "HTTP_404"
