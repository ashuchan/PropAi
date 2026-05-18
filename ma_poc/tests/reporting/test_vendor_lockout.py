"""Tests for ``reporting.vendor_lockout`` — Bug #6 fix (2026-05-18).

Verifies the pure detection function. The end-to-end wire-up into
``reporting.verdict.compute`` is covered by ``test_verdict_compute.py``.
"""

from __future__ import annotations

from ma_poc.reporting.vendor_lockout import detect_vendor_lockout


# ── Spherexx (canonical case — PID 33993 cityclubapartments.com) ─────────────


def test_spherexx_nonpayment_body_phrase() -> None:
    body = b"""<!DOCTYPE html><html><head>
    <title>Site Taken Down (Non-Payment) - Web hosting provided by Spherexx.com</title>
    </head><body>The website you are looking for has been removed.</body></html>"""
    is_lockout, label = detect_vendor_lockout(body)
    assert is_lockout
    assert label == "vendor_lockout:spherexx_nonpayment"


def test_spherexx_nonpayment_final_url() -> None:
    """Final URL alone is enough — even when body is zero bytes."""
    is_lockout, label = detect_vendor_lockout(
        b"", final_url="https://nonpayment.spherexx.com/",
    )
    assert is_lockout
    assert "spherexx" in (label or "")


# ── Other vendor patterns ────────────────────────────────────────────────────


def test_bluehost_account_suspended_url() -> None:
    is_lockout, label = detect_vendor_lockout(
        b"<html></html>",
        final_url="https://account-suspended.bluehost.com/",
    )
    assert is_lockout
    assert "bluehost" in (label or "")


def test_godaddy_parked_body_phrase() -> None:
    body = b"<html>This domain is parked at parking.godaddy.com</html>"
    is_lockout, label = detect_vendor_lockout(body)
    assert is_lockout


def test_domain_expired_phrase() -> None:
    body = b"<html><body>This domain has expired. Renew now.</body></html>"
    is_lockout, label = detect_vendor_lockout(body)
    assert is_lockout


# ── Non-matches (no false positives on real marketing copy) ──────────────────


def test_real_apartment_copy_is_not_lockout() -> None:
    body = b"""<html>
    <h1>Welcome to Sunset Towers Apartments</h1>
    <p>Our 2-bedroom and 3-bedroom units offer stunning views.</p>
    <p>Studio and 1-bedroom floor plans available from $1,200/month.</p>
    <p>Concessions: first month free on select units.</p>
    </html>"""
    is_lockout, label = detect_vendor_lockout(body)
    assert not is_lockout
    assert label is None


def test_word_suspended_alone_does_not_match() -> None:
    """``suspended`` alone is too generic — real copy uses it. The pattern
    list requires multi-word phrases or domain names."""
    body = b"<html>Services suspended for major holidays - see calendar.</html>"
    is_lockout, _ = detect_vendor_lockout(body)
    assert not is_lockout


def test_empty_body_no_url_returns_false() -> None:
    assert detect_vendor_lockout(None) == (False, None)
    assert detect_vendor_lockout(b"") == (False, None)
    assert detect_vendor_lockout("") == (False, None)


def test_str_body_accepted() -> None:
    body = "<html>Site taken down (non-payment)</html>"
    is_lockout, _ = detect_vendor_lockout(body)
    assert is_lockout


# ── Robustness ────────────────────────────────────────────────────────────────


def test_case_insensitive_match() -> None:
    body = b"<html>SITE TAKEN DOWN (NON-PAYMENT)</html>"
    is_lockout, _ = detect_vendor_lockout(body)
    assert is_lockout


def test_invalid_utf8_does_not_crash() -> None:
    """Decode-error-safe — caller passes raw bytes, we never re-raise."""
    body = b"\xff\xfe\x00\x00 invalid utf-8 \x80\x81"
    is_lockout, _ = detect_vendor_lockout(body)
    # Doesn't crash; returns False because no pattern matches in the garbage.
    assert is_lockout is False
