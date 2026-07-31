"""probe_get(retries=N) — compliance-safe plain re-GET on a transient soft-block.

2026-07-31. Under COMPLIANCE_MODE the Web Unlocker / FlareSolverr / CAPTCHA-
solving are a legal no-fly zone (all off), so the code-only recovery routes
(Lead-3 ``/conventional/``, the direct-GET family, #80 ``/availableunits``) have
NO fallback — a single transient 403/429/503 kills them. The soft-block is
single-shot intermittent (live-verified 2026-07-31: the same hosts flip 403↔200
seconds apart, a plain re-GET recovered 4/4). ``retries>0`` adds a bounded plain
re-GET (an ordinary request, NOT a solver/unblocker → inside the compliance
envelope). ``retries=0`` (default) must be byte-for-byte the prior behaviour.

Transport (``curl_cffi.requests.get``) is mocked, so this is a probe-seam test.
"""

from __future__ import annotations

import pytest

import ma_poc.pms.adapters._probe as _probe

pytestmark = pytest.mark.probe_seam


class _Resp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # probe_get does ``import time as _time`` locally → same module object.
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)


def test_retry_recovers_transient_soft_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """403 → 403 → 200: retries=3 keeps re-GETting until the block clears."""
    calls = {"n": 0}

    def fake_get(url: str, **kw: object) -> _Resp:
        calls["n"] += 1
        return _Resp(403) if calls["n"] < 3 else _Resp(200, "real page content")

    monkeypatch.setattr("curl_cffi.requests.get", fake_get)
    r = _probe.probe_get("https://x.test/", unlocker=False, retries=3)
    assert r.status_code == 200
    assert calls["n"] == 3  # initial + 2 retries, stopped as soon as it cleared


def test_default_is_single_shot_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """retries=0 (default): a 403 is returned as-is with exactly ONE GET —
    byte-for-byte the prior behaviour, so no existing caller changes."""
    calls = {"n": 0}

    def fake_get(url: str, **kw: object) -> _Resp:
        calls["n"] += 1
        return _Resp(403)

    monkeypatch.setattr("curl_cffi.requests.get", fake_get)
    r = _probe.probe_get("https://x.test/", unlocker=False)
    assert r.status_code == 403
    assert calls["n"] == 1  # no retry


def test_retry_is_bounded_and_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """A persistently-blocked host is retried at most ``retries`` times, then the
    last (still-blocked) response is returned — never an infinite loop."""
    calls = {"n": 0}

    def fake_get(url: str, **kw: object) -> _Resp:
        calls["n"] += 1
        return _Resp(429)

    monkeypatch.setattr("curl_cffi.requests.get", fake_get)
    r = _probe.probe_get("https://x.test/", unlocker=False, retries=2)
    assert r.status_code == 429
    assert calls["n"] == 3  # initial + 2 bounded retries


def test_success_first_try_never_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean 200 is returned immediately — retries only fire on a block."""
    calls = {"n": 0}

    def fake_get(url: str, **kw: object) -> _Resp:
        calls["n"] += 1
        return _Resp(200, "ok")

    monkeypatch.setattr("curl_cffi.requests.get", fake_get)
    r = _probe.probe_get("https://x.test/", unlocker=False, retries=3)
    assert r.status_code == 200
    assert calls["n"] == 1
