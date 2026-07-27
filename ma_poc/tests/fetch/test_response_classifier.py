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
    from patchright._impl._errors import TimeoutError as PlaywrightTimeoutError

    exc = PlaywrightTimeoutError("Page.goto: Timeout 20000ms exceeded.")
    outcome, sig = classify(None, {}, None, exception=exc)
    assert outcome == FetchOutcome.TRANSIENT
    assert sig == "timeout"


def test_classify_404_is_dead_url() -> None:
    """Stage 3 (2026-05-12): 404 is terminal — the resource is gone.
    Distinct from HARD_FAIL (which 4xx-other-than-dead-URL covers).
    Reporting excludes DEAD_URL from the success-rate denominator.
    (Pre-Stage-3: this asserted FetchOutcome.HARD_FAIL.)"""
    outcome, sig = classify(404, {}, b"")
    assert outcome == FetchOutcome.DEAD_URL
    assert sig == "HTTP_404"


def test_classify_410_is_dead_url() -> None:
    """410 Gone — the resource was permanently removed."""
    outcome, sig = classify(410, {}, b"")
    assert outcome == FetchOutcome.DEAD_URL
    assert sig == "HTTP_410"


def test_classify_451_is_dead_url() -> None:
    """451 Unavailable For Legal Reasons — terminal, never retry."""
    outcome, sig = classify(451, {}, b"")
    assert outcome == FetchOutcome.DEAD_URL
    assert sig == "HTTP_451"


def test_classify_other_4xx_stays_hard_fail() -> None:
    """4xx codes other than dead-URL codes still route to HARD_FAIL.
    400 (bad request), 405 (method not allowed), 406 (not acceptable),
    etc. — these are application-layer issues, not "resource gone"."""
    for status in (400, 405, 406, 408, 409, 411, 415, 422):
        outcome, sig = classify(status, {}, b"")
        assert outcome == FetchOutcome.HARD_FAIL, f"status={status} should be HARD_FAIL"
        assert sig == f"HTTP_{status}"


def test_classify_nxdomain_exception_is_dead_url() -> None:
    """``ERR_NAME_NOT_RESOLVED`` / NXDOMAIN — the domain doesn't exist.
    Distinct from a transient DNS hiccup which stays HARD_FAIL."""
    from socket import gaierror

    exc = gaierror("[Errno -2] Name or service not known")
    outcome, sig = classify(None, {}, None, exception=exc)
    # "name or service not known" matches the NXDOMAIN heuristic
    assert outcome == FetchOutcome.DEAD_URL
    assert sig == "ERR_DNS_NXDOMAIN"


def test_classify_nxdomain_chrome_signature_is_dead_url() -> None:
    """Chrome / Chromium-emitted ``ERR_NAME_NOT_RESOLVED`` from Playwright."""
    exc = OSError("net::ERR_NAME_NOT_RESOLVED at https://gone-domain.example/")
    outcome, sig = classify(None, {}, None, exception=exc)
    assert outcome == FetchOutcome.DEAD_URL
    assert sig == "ERR_DNS_NXDOMAIN"


# ── Soft-404 detection (#28, 2026-07-16) ──────────────────────────────────────
# HTTP 200 whose <title>/<h1> declares the page a not-found placeholder is a
# dead URL (terminal, excluded from success-rate denominator, re-discovery
# queue) — not a real property with zero inventory.


def test_classify_soft_404_title_is_dead_url() -> None:
    """200 with a 'Page Not Found' <title> — the notfound.apts247.info pattern."""
    body = b"<html><head><title>Page Not Found</title></head><body>Sorry.</body></html>"
    outcome, sig = classify(200, {}, body)
    assert outcome == FetchOutcome.DEAD_URL
    assert sig == "SOFT_404"


def test_classify_soft_404_h1_is_dead_url() -> None:
    """200 whose <h1> headline names it a 404, even with a benign <title>."""
    body = (
        b"<html><head><title>Acme Apartments</title></head>"
        b"<body><h1>404 - Page Not Found</h1></body></html>"
    )
    outcome, sig = classify(200, {}, body)
    assert outcome == FetchOutcome.DEAD_URL
    assert sig == "SOFT_404"


def test_classify_soft_404_exact_title() -> None:
    """A terse ``<title>404</title>`` error page."""
    outcome, sig = classify(200, {}, b"<html><head><title>404</title></head></html>")
    assert outcome == FetchOutcome.DEAD_URL
    assert sig == "SOFT_404"


def test_classify_real_property_page_not_soft_404() -> None:
    """A real property page whose title contains no not-found headline stays OK,
    even when '404' appears incidentally in a body asset path (the false-positive
    that a raw body-substring scan would trip on — reserveatriverplace/266lofts)."""
    body = (
        b"<html><head><title>Apartments in Memphis, TN | 266 LOFTS</title></head>"
        b"<body><script src='/assets/app.404abc.js'></script>"
        b"<h1>A Refreshing Community</h1></body></html>"
    )
    outcome, sig = classify(200, {}, body)
    assert outcome == FetchOutcome.OK
    assert sig is None


def test_classify_lease_up_coming_soon_not_soft_404() -> None:
    """'Coming soon' / 'under construction' are legitimate LEASE_UP previews,
    NOT dead URLs — must stay OK so lease-up properties aren't dropped."""
    body = (
        b"<html><head><title>Now Leasing - Coming Soon | The Grove</title></head>"
        b"<body><h1>Under Construction</h1></body></html>"
    )
    outcome, sig = classify(200, {}, body)
    assert outcome == FetchOutcome.OK
    assert sig is None


def test_patchright_timeout_import_resolves() -> None:
    """Catches patchright internal reorganisation that would silently degrade
    classify() to generic 'TimeoutError' signatures, breaking retry back-off."""
    from ma_poc.fetch.response_classifier import _PlaywrightTimeoutError
    assert _PlaywrightTimeoutError is not None, (
        "patchright._impl._errors.TimeoutError is not importable. "
        "The classifier will fall back to generic 'TimeoutError' signatures, "
        "breaking retry-after behaviour that keys on 'timeout' in the signature."
    )


# ── Exception signatures (RCA 2026-07-27) ───────────────────────────────────
# All 267 fetches that a dead datacentre proxy failed in the 2026-07-22-hb250
# run recorded the bare signature "Error" — because Playwright's base exception
# class is literally named ``Error``, so ``type(exc).__name__`` was maximally
# uninformative while the message carrying net::ERR_TUNNEL_CONNECTION_FAILED
# was discarded. Eight days of opaque telemetry followed.


class Error(Exception):
    """Stands in for ``playwright._impl._errors.Error``.

    Deliberately a local class rather than a live import: other tests in the
    suite replace ``playwright`` in ``sys.modules``, so importing it here is
    order-dependent (it fails with "unknown location" in a full-suite run while
    passing in isolation). What is under test is the *shape* — an exception
    class whose ``__name__`` is the useless string ``"Error"`` — not Playwright
    itself. ``test_playwright_base_error_is_still_named_error`` pins the premise
    separately.
    """


def _pw_error(msg: str) -> Exception:
    return Error(msg)


def test_playwright_base_error_is_still_named_error() -> None:
    """Premise check: if Playwright ever renames its base class, this whole
    section's motivation changes. Skipped when the module is stubbed by another
    test, so suite ordering can't turn this into a false alarm."""
    import pytest

    try:
        from playwright.async_api import Error as PlaywrightError
    except ImportError:  # pragma: no cover - depends on suite ordering
        pytest.skip("playwright module is stubbed/unavailable in this process")
    assert PlaywrightError.__name__ == "Error"


def test_playwright_error_keeps_the_net_error_code() -> None:
    """The regression: 'Error' alone must never be the whole signature."""
    exc = _pw_error("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://x.com/u")
    outcome, sig = classify(None, {}, None, exception=exc)

    assert outcome == FetchOutcome.TRANSIENT
    assert sig != "Error", "regressed to the uninformative bare type name"
    assert sig == "Error:net::ERR_TUNNEL_CONNECTION_FAILED"


def test_signature_preserves_type_and_cause_for_plain_exceptions() -> None:
    outcome, sig = classify(
        None, {}, None, exception=Exception("CONNECT tunnel failed, response 502")
    )
    assert outcome == FetchOutcome.TRANSIENT
    assert sig == "Exception:connect_tunnel_failed_response_502"


def test_signature_cardinality_is_bounded() -> None:
    """error_signature is group-by'd in analyze_cloud_run and stored in a
    String(512) column — raw messages with URLs/IPs would explode the aggregate.

    Two failures against different hosts must collapse to ONE signature.
    """
    a = classify(None, {}, None, exception=_pw_error(
        "net::ERR_PROXY_CONNECTION_FAILED at https://alpha.example.com:8443/units"))[1]
    b = classify(None, {}, None, exception=_pw_error(
        "net::ERR_PROXY_CONNECTION_FAILED at https://beta.other.org:9001/floorplans"))[1]
    assert a == b == "Error:net::ERR_PROXY_CONNECTION_FAILED"

    # Unrecognised messages are slugified, scrubbed of hosts/IPs, and truncated.
    long_sig = classify(None, {}, None, exception=Exception("boom " * 200))[1]
    assert long_sig is not None and len(long_sig) < 120

    hosty = classify(None, {}, None, exception=Exception(
        "refused by 10.1.2.3:8080 for host cdn.example.com"))[1]
    assert hosty is not None
    assert "10.1.2.3" not in hosty and "example" not in hosty


def test_signature_falls_back_to_type_name_when_message_is_empty() -> None:
    assert classify(None, {}, None, exception=Exception(""))[1] == "Exception"


def test_connection_error_keeps_its_cause() -> None:
    outcome, sig = classify(
        None, {}, None, exception=ConnectionError("Connection reset by peer")
    )
    assert outcome == FetchOutcome.TRANSIENT
    assert sig == "ConnectionError:connection_reset_by_peer"


def test_timeout_signature_is_unchanged() -> None:
    """Retry back-off keys on 'timeout' in the signature — must not drift."""
    assert classify(None, {}, None, exception=TimeoutError("slow"))[1] == "timeout"
