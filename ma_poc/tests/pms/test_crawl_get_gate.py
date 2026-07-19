"""Tests for the #timeout crawl cheap-GET-gate (`_crawl_get_gate_should_skip`).

The link-hop crawl RENDERs every guessed subpath, so a 404 guess-path burns
~155-191s (render + Web-Unlocker fallback) — the dominant driver of the 600s
timeouts. The gate skips the render ONLY for a genuine empty 404, and must
preserve soft-404s (≥10KB body carrying the unit roster), 200s, and walled
pages. probe_get is mocked (it has its own suite)."""
from __future__ import annotations

from ma_poc.pms.scraper import _crawl_get_gate_should_skip


def _resp(status: int, text: str):
    class _R:
        pass

    r = _R()
    r.status_code = status
    r.text = text
    return r


def _patch(monkeypatch, resp_or_exc):
    def fake(url, timeout=None, unlocker=None):
        if isinstance(resp_or_exc, Exception):
            raise resp_or_exc
        return resp_or_exc

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake)


def test_genuine_empty_404_is_skipped(monkeypatch) -> None:
    _patch(monkeypatch, _resp(404, "Not Found"))
    assert _crawl_get_gate_should_skip("https://x.com/floorplans") is True


def test_410_gone_is_skipped(monkeypatch) -> None:
    _patch(monkeypatch, _resp(410, "gone"))
    assert _crawl_get_gate_should_skip("https://x.com/availability") is True


def test_soft_404_with_substantive_body_is_preserved(monkeypatch) -> None:
    # 404 but ≥10KB body (the unit roster lives here) → must NOT be skipped
    _patch(monkeypatch, _resp(404, "x" * 12_000))
    assert _crawl_get_gate_should_skip("https://x.com/availability") is False


def test_200_is_not_skipped(monkeypatch) -> None:
    _patch(monkeypatch, _resp(200, "floor plans here"))
    assert _crawl_get_gate_should_skip("https://x.com/floorplans") is False


def test_walled_403_is_not_skipped(monkeypatch) -> None:
    # a CF-walled 403 must still RENDER (browser CF-solve), not be gated out
    _patch(monkeypatch, _resp(403, "just a moment"))
    assert _crawl_get_gate_should_skip("https://x.com/floorplans") is False


def test_probe_exception_fails_open(monkeypatch) -> None:
    _patch(monkeypatch, RuntimeError("network down"))
    assert _crawl_get_gate_should_skip("https://x.com/floorplans") is False
