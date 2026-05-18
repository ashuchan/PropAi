"""Verdict short-circuit on vendor-lockout / parked-domain bodies.

Some property websites are administratively shut down (non-payment,
expired domain, account suspended, parking) but still respond HTTP 200
with a small landlord-vendor stub page. Pre-fix these flowed through
``LLM_GATE_NO_BODY`` -> ``FAILED_NO_DATA``, which is the wrong terminal
verdict — the URL is permanently dead at the application level. The
verdict layer now consumes the optional ``fetch_body`` / ``fetch_final_url``
kwargs, runs the body / URL through
``reporting.vendor_lockout.detect_vendor_lockout``, and short-circuits
to ``DEAD_URL`` when a known vendor pattern matches.

Tests here cover the ``compute()`` integration. Pure-function coverage
for the detector lives in ``test_vendor_lockout.py``.

Canonical incident: PID 33993 cityclubapartments.com redirected to
``https://nonpayment.spherexx.com/`` (962-byte "Site Taken Down
(Non-Payment)" stub) — 8 such properties on the 2026-05-18 cloud run;
cumulative across runs is much higher.
"""

from __future__ import annotations

from ma_poc.reporting.verdict import Verdict, compute


def test_compute_returns_dead_url_on_spherexx_lockout_body() -> None:
    body = b"<html><title>Site Taken Down (Non-Payment) - Spherexx</title></html>"
    result = compute(
        fetch_outcome="OK",
        extract_result={"records": []},
        fetch_body=body,
    )
    assert result.verdict == Verdict.DEAD_URL
    assert "spherexx" in result.reason


def test_compute_returns_dead_url_on_lockout_final_url_alone() -> None:
    """Final URL alone is enough — some vendor takedowns respond with
    near-empty bodies but the redirect target itself is the signal."""
    result = compute(
        fetch_outcome="OK",
        extract_result={"records": []},
        fetch_body=b"",
        fetch_final_url="https://nonpayment.spherexx.com/",
    )
    assert result.verdict == Verdict.DEAD_URL


def test_compute_lockout_takes_priority_over_no_data() -> None:
    """If both signals point to dead URL (vendor lockout) and no records,
    DEAD_URL wins — the page is permanently gone, no point logging it as
    FAILED_NO_DATA."""
    body = b"<html>This domain has expired</html>"
    result = compute(
        fetch_outcome="OK",
        extract_result=None,
        fetch_body=body,
    )
    assert result.verdict == Verdict.DEAD_URL


def test_compute_does_not_trigger_lockout_on_real_apartment_copy() -> None:
    """Sanity: real listing copy must not trigger the lockout classifier."""
    body = b"<html><h1>Welcome to Sunset Towers</h1><p>2-bedroom from $1,200</p></html>"
    result = compute(
        fetch_outcome="OK",
        extract_result={"records": []},
        fetch_body=body,
    )
    # No records + no lockout = the standard FAILED_NO_DATA path.
    assert result.verdict == Verdict.FAILED_NO_DATA


def test_compute_skips_lockout_check_when_kwargs_absent() -> None:
    """Callers that pass neither fetch_body nor fetch_final_url retain the
    pre-fix behaviour (no lockout check fires). Backwards compatibility
    for any caller still on the old signature."""
    result = compute(
        fetch_outcome="OK",
        extract_result={"records": []},
    )
    assert result.verdict == Verdict.FAILED_NO_DATA


def test_compute_skips_lockout_check_when_fetch_failed() -> None:
    """If the fetch itself failed (FAILED_UNREACHABLE), we never reach the
    lockout check — the upstream failure dominates. Otherwise a body
    that happened to mention 'suspended' on a BOT_BLOCKED fetch would
    incorrectly flip to DEAD_URL."""
    result = compute(
        fetch_outcome="BOT_BLOCKED",
        extract_result=None,
        fetch_body=b"Site Taken Down (Non-Payment)",
    )
    assert result.verdict == Verdict.FAILED_UNREACHABLE
