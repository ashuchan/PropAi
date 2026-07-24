"""Agentic router Phase 1 — deterministic signals + policy.

Pins compute_signals (never-raise, correct signal extraction) and route (the
closed-menu deterministic policy: dead-url, known-endpoint→direct-GET, CF+PMS→HB,
OK+PMS+no-units→render, real-empty→verified-empty, else→INVOKE_AGENT). No LLM, no
wiring — this is the shadow-ready foundation.
"""

from __future__ import annotations

from ma_poc.services.route_policy import (
    ESCALATE_HB_SHELL,
    ESCALATE_RENDER,
    INVOKE_AGENT,
    MARK_DEAD_URL,
    MARK_VERIFIED_EMPTY,
    STOP,
    TRY_DIRECT_GET,
    RouteSignals,
    compute_signals,
    route,
)


class _FR:
    def __init__(self, *, outcome="OK", status=200, body=b"", network_log=None,
                 captcha_detected=False, content_type="text/html"):
        self.outcome = outcome
        self.status = status
        self.body = body
        self.network_log = network_log or []
        self.captcha_detected = captcha_detected
        self.content_type = content_type


class _Prof:
    class _C:
        maturity = "WARM"
        preferred_tier = 1
        consecutive_failures = 0

    class _A:
        known_endpoints = [{"url_pattern": "https://doorway-api.knockrentals.com/v1/property/2021296/units"}]

    confidence = _C()
    api_hints = _A()


# ── compute_signals ──────────────────────────────────────────────────────────


def test_compute_signals_never_raises_on_garbage() -> None:
    s = compute_signals(None, None, None)
    assert isinstance(s, RouteSignals) and s.outcome == "UNKNOWN"


def test_compute_signals_extracts_pms_and_rent() -> None:
    body = b"<html>doorway-api.knockrentals.com ... $1,450 rent unit_number 101</html>"
    s = compute_signals(_FR(body=body), None, _Prof())
    assert "knock" in s.pms_fingerprints
    assert s.has_rent_signal and s.has_unit_signal
    assert s.known_endpoint_match is True  # profile endpoint host appears in body
    assert s.maturity == "WARM"


def test_compute_signals_counts_json_xhr_and_cf_shell() -> None:
    nlog = [{"url": "https://x/api/units", "content_type": "application/json"},
            {"url": "https://x/img", "content_type": "image/png"}]
    s = compute_signals(_FR(body=b"Just a moment... checking your browser", network_log=nlog))
    assert s.xhr_captured == 1
    assert s.cf_shell is True


# ── route policy ─────────────────────────────────────────────────────────────


def test_route_stop_when_units_present() -> None:
    assert route(RouteSignals(units_extracted=12)).action == STOP


def test_route_dead_url() -> None:
    assert route(RouteSignals(outcome="DEAD_URL", status=404)).action == MARK_DEAD_URL


def test_route_known_endpoint_to_direct_get() -> None:
    d = route(RouteSignals(known_endpoint_match=True, pms_fingerprints=["knock"]))
    assert d.action == TRY_DIRECT_GET and d.target_field_group == "knock"


def test_route_cf_blocked_with_pms_to_hb_shell() -> None:
    d = route(RouteSignals(cf_shell=True, pms_fingerprints=["sightmap"]))
    assert d.action == ESCALATE_HB_SHELL and d.target_field_group == "sightmap"


def test_route_ok_pms_no_units_to_render() -> None:
    d = route(RouteSignals(outcome="OK", pms_fingerprints=["entrata"], has_unit_signal=False))
    assert d.action == ESCALATE_RENDER


def test_route_real_empty_page_to_verified_empty() -> None:
    d = route(RouteSignals(outcome="OK", body_bytes=20_000, has_rent_signal=False,
                           has_unit_signal=False, pms_fingerprints=[], cf_shell=False))
    assert d.action == MARK_VERIFIED_EMPTY


def test_route_ambiguous_to_invoke_agent() -> None:
    # OK body WITH rent/unit signal but NO known PMS route → the hard-tail case.
    d = route(RouteSignals(outcome="OK", body_bytes=20_000, has_rent_signal=True,
                           has_unit_signal=True, pms_fingerprints=[]))
    assert d.action == INVOKE_AGENT
