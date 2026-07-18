"""Tests for the #41 empty-exit → marketing-subpage plan-text recovery helper.

`_empty_exit_subpage_plan_text` is the loop that a confirmed-PMS empty-exit
(e.g. the AppFolio contamination demote → TIER_1_API_APPFOLIO_EMPTY) uses to
recover the property's OWN plan-level rents from its marketing /floor-plans
page. These lock the loop logic — path order, first-hit short-circuit, status
filtering, and never-raise behaviour — with probe_get / parse mocked (both are
covered by their own suites)."""
from __future__ import annotations

import pytest

from ma_poc.config.feature_flags import ENABLE_EMPTY_EXIT_PLAN_TEXT
from ma_poc.pms.scraper import _empty_exit_subpage_plan_text

_ROWS = [{"floor_plan_name": "A1", "rent_low": 1050, "rent_high": 1335}]


def _resp(status: int, text: str):
    class _R:
        status_code = status

    r = _R()
    r.text = text
    return r


@pytest.fixture(autouse=True)
def _stub_parse(monkeypatch):
    # bodytext + parser are tested elsewhere; here we only exercise the loop.
    monkeypatch.setattr(
        "ma_poc.pms.adapters.generic_plan_text._bodytext_from_fetch_result",
        lambda ctx: "plan body text",
    )


def test_flag_defaults_off() -> None:
    # The whole recovery is gated behind this flag (default off = no behaviour
    # change) — a flag-on run measures recovery before it ships enabled.
    assert ENABLE_EMPTY_EXIT_PLAN_TEXT is False


def test_returns_rows_from_first_hitting_subpage(monkeypatch) -> None:
    calls: list[str] = []

    def fake_probe(url, timeout=None, unlocker=None):
        calls.append(url)
        # /floorplans/ and /floorplans 404; /floor-plans/ (3rd) is the hit
        return _resp(200, "x") if url.endswith("/floor-plans/") else _resp(404, "")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe)
    monkeypatch.setattr(
        "ma_poc.pms.adapters.generic_plan_text.parse_generic_plan_text",
        lambda body, url: _ROWS,
    )
    out = _empty_exit_subpage_plan_text("https://example.com")
    assert out == _ROWS
    # short-circuited at the first hitting path — never probed /availability etc.
    assert calls[-1].endswith("/floor-plans/")
    assert not any(c.endswith("/availability/") for c in calls)


def test_probe_get_called_with_unlocker_false(monkeypatch) -> None:
    seen: dict = {}

    def fake_probe(url, timeout=None, unlocker=None):
        seen["unlocker"] = unlocker
        seen["timeout"] = timeout
        return _resp(200, "x")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe)
    monkeypatch.setattr(
        "ma_poc.pms.adapters.generic_plan_text.parse_generic_plan_text",
        lambda body, url: _ROWS,
    )
    _empty_exit_subpage_plan_text("https://example.com")
    assert seen["unlocker"] is False  # public plan pages need no Web-Unlocker
    assert seen["timeout"] == 12


def test_all_404_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda url, timeout=None, unlocker=None: _resp(404, ""),
    )
    monkeypatch.setattr(
        "ma_poc.pms.adapters.generic_plan_text.parse_generic_plan_text",
        lambda body, url: _ROWS,
    )
    assert _empty_exit_subpage_plan_text("https://example.com") == []


def test_200_but_no_plan_rows_returns_empty(monkeypatch) -> None:
    # every subpage 200s but the parser finds no plan-level rows → nothing.
    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda url, timeout=None, unlocker=None: _resp(200, "no plans here"),
    )
    monkeypatch.setattr(
        "ma_poc.pms.adapters.generic_plan_text.parse_generic_plan_text",
        lambda body, url: [],
    )
    assert _empty_exit_subpage_plan_text("https://example.com") == []


def test_probe_exception_never_raises(monkeypatch) -> None:
    def boom(url, timeout=None, unlocker=None):
        raise RuntimeError("network down")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", boom)
    monkeypatch.setattr(
        "ma_poc.pms.adapters.generic_plan_text.parse_generic_plan_text",
        lambda body, url: _ROWS,
    )
    assert _empty_exit_subpage_plan_text("https://example.com") == []


def test_empty_base_url_short_circuits(monkeypatch) -> None:
    called = {"n": 0}

    def fake_probe(url, timeout=None, unlocker=None):
        called["n"] += 1
        return _resp(200, "x")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe)
    assert _empty_exit_subpage_plan_text("") == []
    assert called["n"] == 0  # never probes when there's no origin
