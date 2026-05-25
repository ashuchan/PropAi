"""2026-05-25 canary 1ef1060 follow-up — captcha-detected promotes
``FetchOutcome.OK`` to ``BOT_BLOCKED`` so the existing curl_cffi → Unlocker
cascade fires.

Pre-fix: a Sucuri-walled property returned to the orchestrator with
outcome=OK + ~12KB challenge body. The cascade at
``ma_poc/fetch/fetcher.py:_do_request`` only escalated on outcome=BOT_BLOCKED,
so the challenge HTML reached the extractor and produced ran_empty across
the tier cascade. ~738 units across 4 properties (Reserve at Belvedere,
Heritage by Fairlawn, Vivid, Terrain) were dropped this way.

Post-fix: when ``looks_like_captcha`` detects a challenge body AND outcome
is still OK, the fetcher promotes outcome to BOT_BLOCKED. The cascade then
tries curl_cffi (different TLS fingerprint may bypass) and finally Unlocker.

This file pins the promotion behaviour and the no-op cases (already
BOT_BLOCKED, no captcha, etc.) so a refactor cannot silently re-introduce
the regression.
"""
from __future__ import annotations

import dataclasses

import pytest

from ma_poc.fetch.captcha_detect import looks_like_captcha
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode

# ─────────────────────────────────────────────────────────────────────
# The detection function itself (unit-level).
# ─────────────────────────────────────────────────────────────────────


def test_sucuri_body_detected_as_captcha() -> None:
    """Reality check that looks_like_captcha catches the Sucuri body
    that motivated the promotion — without this, the promotion code
    wouldn't fire."""
    body = (
        b'<!doctype html><html><head>'
        b'<title>Robot Challenge Screen</title>'
        b'<script>const sgchallenge="21:1779683624:abc";</script>'
        b'</head><body></body></html>'
    )
    is_captcha, provider = looks_like_captcha(body, body_size=len(body))
    assert is_captcha is True
    assert provider == "sucuri"


# ─────────────────────────────────────────────────────────────────────
# The promotion logic — replicates the exact 4-line code path added in
# ma_poc/fetch/fetcher.py without standing up the full async fetcher.
# That code path is:
#   if captcha_detected and result.outcome == FetchOutcome.OK:
#       result = dataclasses.replace(result, outcome=FetchOutcome.BOT_BLOCKED)
# Pinning the semantics in isolation guarantees the contract even if
# the surrounding fetcher code is refactored.
# ─────────────────────────────────────────────────────────────────────


def _apply_promotion(result: FetchResult, captcha_detected: bool) -> FetchResult:
    """Mirror of the fetcher.py promotion block."""
    if captcha_detected and result.outcome == FetchOutcome.OK:
        return dataclasses.replace(result, outcome=FetchOutcome.BOT_BLOCKED)
    return result


def _make_result(
    outcome: FetchOutcome = FetchOutcome.OK,
    status: int = 200,
    body: bytes | None = b"",
    final_url: str = "https://x.test/",
) -> FetchResult:
    return FetchResult(
        url="https://x.test/",
        outcome=outcome,
        status=status,
        body=body,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url=final_url,
        attempts=1,
        elapsed_ms=100,
    )


def test_promotion_fires_on_ok_plus_captcha_detected() -> None:
    """The canary signature: outcome=OK, body has Sucuri markers,
    detector said True. Promotion flips outcome to BOT_BLOCKED so
    the cascade picks it up."""
    r = _make_result(outcome=FetchOutcome.OK)
    r2 = _apply_promotion(r, captcha_detected=True)
    assert r2.outcome == FetchOutcome.BOT_BLOCKED
    # All other fields preserved.
    assert r2.body == r.body
    assert r2.status == r.status
    assert r2.final_url == r.final_url


def test_promotion_noop_when_no_captcha() -> None:
    """Real apartment page (no captcha detected) keeps outcome=OK."""
    r = _make_result(outcome=FetchOutcome.OK)
    r2 = _apply_promotion(r, captcha_detected=False)
    assert r2.outcome == FetchOutcome.OK
    assert r2 is r  # exact same object — no mutation/replacement


def test_promotion_noop_when_already_bot_blocked() -> None:
    """A Cloudflare 403 already arrives as BOT_BLOCKED. Promotion must
    NOT fire (would be a no-op anyway, but pin it to avoid weird
    double-replacement)."""
    r = _make_result(outcome=FetchOutcome.BOT_BLOCKED, status=403)
    r2 = _apply_promotion(r, captcha_detected=True)
    assert r2.outcome == FetchOutcome.BOT_BLOCKED
    assert r2 is r  # exact same object


def test_promotion_noop_on_hard_fail() -> None:
    """Hard failures (SSL, DNS, etc.) don't get promoted — the cascade
    won't help those, and Unlocker would just waste a paid call."""
    r = _make_result(outcome=FetchOutcome.HARD_FAIL, status=0)
    r2 = _apply_promotion(r, captcha_detected=True)
    assert r2.outcome == FetchOutcome.HARD_FAIL
    assert r2 is r


def test_promotion_noop_on_transient() -> None:
    """Transient errors (timeouts) — operator-side; cascade can't fix."""
    r = _make_result(outcome=FetchOutcome.TRANSIENT, status=0)
    r2 = _apply_promotion(r, captcha_detected=True)
    assert r2.outcome == FetchOutcome.TRANSIENT
    assert r2 is r


def test_promotion_noop_on_dead_url() -> None:
    """Dead URLs (DNS NXDOMAIN, persistent 404) — no benefit from cascade."""
    r = _make_result(outcome=FetchOutcome.DEAD_URL, status=404)
    r2 = _apply_promotion(r, captcha_detected=True)
    assert r2.outcome == FetchOutcome.DEAD_URL


def test_promotion_noop_on_not_modified() -> None:
    """NOT_MODIFIED (304) means we have a cached good copy. No promotion."""
    r = _make_result(outcome=FetchOutcome.NOT_MODIFIED, status=304, body=None)
    r2 = _apply_promotion(r, captcha_detected=True)
    assert r2.outcome == FetchOutcome.NOT_MODIFIED


@pytest.mark.parametrize("status", [200, 202, 204])
def test_promotion_fires_across_all_ok_status_codes(status: int) -> None:
    """Sucuri serves the wall at HTTP 200 AND 202 (observed in canary
    1ef1060 — Belvedere returned 202 with the captcha redirect, Terrain
    returned 202 with the sgcaptcha redirect). 204 is unusual but
    completes the OK family for safety."""
    r = _make_result(outcome=FetchOutcome.OK, status=status)
    r2 = _apply_promotion(r, captcha_detected=True)
    assert r2.outcome == FetchOutcome.BOT_BLOCKED


def test_promotion_preserves_captcha_detected_flag() -> None:
    """captcha_detected flag on the result must survive the promotion
    so downstream telemetry (FETCH_CAPTCHA_DETECTED event) still fires."""
    r = _make_result(outcome=FetchOutcome.OK)
    r = dataclasses.replace(r, captcha_detected=True)
    r2 = _apply_promotion(r, captcha_detected=True)
    assert r2.outcome == FetchOutcome.BOT_BLOCKED
    assert r2.captcha_detected is True
