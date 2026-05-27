"""Web Unlocker per-process call cap (2026-05-24).

Pins ``WEB_UNLOCKER_MAX_CALLS_PER_JOB`` — the env-var hard ceiling
shipped after jugnu-unlocker-test-3886351-fl9gv burned ~3,180 calls
($4.65-$9.30 at BD's $1.50-3/1000 tier rate) past the user's $2-3
budget ceiling before manual cancellation. Per-property gates inside
``_probe_prospectportal`` weren't enough; the orchestrator runs many
props per shard so the per-shard total can balloon an order of
magnitude.
"""
from __future__ import annotations

import pytest

from ma_poc.pms.adapters import _probe


@pytest.fixture(autouse=True)
def _reset_counter():
    """Each test starts with a clean call counter + warn flag."""
    _probe.reset_web_unlocker_call_count()
    yield
    _probe.reset_web_unlocker_call_count()


def _stub_brightdata(monkeypatch, captured: list[bytes]) -> None:
    """Make web_unlocker_get's BD POST a no-op that captures the body."""
    monkeypatch.setattr(_probe, "web_unlocker_key", lambda: "test-key")

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b"<html>ok</html>"

    def fake_urlopen(req, timeout=120):
        captured.append(req.data)
        return _FakeResp()

    monkeypatch.setattr(_probe.urllib.request, "urlopen", fake_urlopen)


def test_no_cap_set_allows_unlimited_calls(monkeypatch) -> None:
    """Default behaviour (env var unset) = no cap. Backward-compatible
    for legacy canary configs that don't set the cap."""
    monkeypatch.delenv("WEB_UNLOCKER_MAX_CALLS_PER_JOB", raising=False)
    captured: list[bytes] = []
    _stub_brightdata(monkeypatch, captured)

    for i in range(10):
        resp = _probe.web_unlocker_get(f"https://x.com/?n={i}", timeout=5)
        assert resp.status_code == 200

    assert _probe.web_unlocker_call_count() == 10
    assert len(captured) == 10, "all 10 calls should have hit BD"


def test_empty_env_var_treated_as_no_cap(monkeypatch) -> None:
    """Empty string env var = no cap (same as unset)."""
    monkeypatch.setenv("WEB_UNLOCKER_MAX_CALLS_PER_JOB", "")
    captured: list[bytes] = []
    _stub_brightdata(monkeypatch, captured)

    for i in range(5):
        _probe.web_unlocker_get(f"https://x.com/?n={i}", timeout=5)
    assert len(captured) == 5


def test_non_numeric_env_var_treated_as_no_cap(monkeypatch) -> None:
    """``WEB_UNLOCKER_MAX_CALLS_PER_JOB=lots`` is misconfigured but
    must not crash the call chain — treat as no cap, log nothing."""
    monkeypatch.setenv("WEB_UNLOCKER_MAX_CALLS_PER_JOB", "lots")
    captured: list[bytes] = []
    _stub_brightdata(monkeypatch, captured)

    _probe.web_unlocker_get("https://x.com/?n=1", timeout=5)
    assert len(captured) == 1


def test_zero_cap_treated_as_no_cap(monkeypatch) -> None:
    """``=0`` matches the explicit "no cap" sentinel — easier to set
    than unset in YAML/secret-manager configs."""
    monkeypatch.setenv("WEB_UNLOCKER_MAX_CALLS_PER_JOB", "0")
    captured: list[bytes] = []
    _stub_brightdata(monkeypatch, captured)

    for _ in range(3):
        _probe.web_unlocker_get("https://x.com/", timeout=5)
    assert len(captured) == 3


def test_cap_stops_after_n_calls(monkeypatch) -> None:
    """The recommended production setting (500) won't fire in a unit
    test — use 3 to exercise the boundary cheaply."""
    monkeypatch.setenv("WEB_UNLOCKER_MAX_CALLS_PER_JOB", "3")
    captured: list[bytes] = []
    _stub_brightdata(monkeypatch, captured)

    # First 3 calls hit BD
    for i in range(3):
        resp = _probe.web_unlocker_get(f"https://x.com/?n={i}", timeout=5)
        assert resp.status_code == 200, f"call {i} unexpectedly capped"

    assert len(captured) == 3
    assert _probe.web_unlocker_call_count() == 3

    # 4th and beyond return the empty shape — no BD hit
    for i in range(3, 8):
        resp = _probe.web_unlocker_get(f"https://x.com/?n={i}", timeout=5)
        assert resp.status_code == 0, f"call {i} should be cap-blocked"
        assert resp.text == ""

    # BD was NOT called for any of the cap-blocked attempts
    assert len(captured) == 3, "extra BD calls leaked through after cap"
    # Counter doesn't increment past the cap
    assert _probe.web_unlocker_call_count() == 3


def test_cap_blocked_calls_return_same_shape_as_missing_key(monkeypatch) -> None:
    """Cap-blocked responses must be indistinguishable from
    no-key-configured responses, so callers (probe_get +
    _probe_prospectportal) handle them via the existing
    non-200-treated-as-empty branch — no new branch needed."""
    monkeypatch.setenv("WEB_UNLOCKER_MAX_CALLS_PER_JOB", "1")
    captured: list[bytes] = []
    _stub_brightdata(monkeypatch, captured)

    # Use up the single slot
    _probe.web_unlocker_get("https://x.com/a", timeout=5)
    # The cap-blocked call
    blocked = _probe.web_unlocker_get("https://x.com/b", timeout=5)

    # Compare to missing-key shape
    monkeypatch.setattr(_probe, "web_unlocker_key", lambda: "")
    _probe.reset_web_unlocker_call_count()
    no_key = _probe.web_unlocker_get("https://x.com/c", timeout=5)

    assert blocked.status_code == no_key.status_code == 0
    assert blocked.text == no_key.text == ""
    assert type(blocked) is type(no_key)


def test_cap_warning_logged_once_per_process(monkeypatch, caplog) -> None:
    """Operators need ONE warning when the cap first blocks a call —
    not one per blocked attempt (log spam). The warn flag resets on
    ``reset_web_unlocker_call_count``."""
    import logging
    monkeypatch.setenv("WEB_UNLOCKER_MAX_CALLS_PER_JOB", "2")
    captured: list[bytes] = []
    _stub_brightdata(monkeypatch, captured)

    with caplog.at_level(logging.WARNING, logger="ma_poc.pms.adapters._probe"):
        # Fill the cap
        _probe.web_unlocker_get("https://x.com/a", timeout=5)
        _probe.web_unlocker_get("https://x.com/b", timeout=5)
        # First over-cap call should log
        _probe.web_unlocker_get("https://x.com/c", timeout=5)
        # 4 more over-cap calls — should NOT log again
        for i in range(4):
            _probe.web_unlocker_get(f"https://x.com/d{i}", timeout=5)

    cap_warnings = [
        r for r in caplog.records
        if "web_unlocker.cap_reached" in (r.getMessage() or "")
    ]
    assert len(cap_warnings) == 1, (
        f"expected exactly 1 cap_reached warning, got {len(cap_warnings)}: "
        f"{[r.getMessage() for r in cap_warnings]}"
    )


def test_cap_is_per_process_not_per_url(monkeypatch) -> None:
    """Cap counts TOTAL calls across all URLs — not per-URL. A shard
    that hits one host hard mustn't escape the cap by varying URLs."""
    monkeypatch.setenv("WEB_UNLOCKER_MAX_CALLS_PER_JOB", "5")
    captured: list[bytes] = []
    _stub_brightdata(monkeypatch, captured)

    hosts = [
        "https://a.com/", "https://b.com/", "https://c.com/",
        "https://d.com/", "https://e.com/", "https://f.com/",
        "https://g.com/", "https://h.com/",
    ]
    for h in hosts:
        _probe.web_unlocker_get(h, timeout=5)

    # Only 5 BD calls — 3 cap-blocked
    assert len(captured) == 5
    assert _probe.web_unlocker_call_count() == 5


def test_reset_counter_restores_full_budget(monkeypatch) -> None:
    """``reset_web_unlocker_call_count`` is the per-job reset hook —
    tests use it; production scraper init can use it too if it ever
    wants to re-arm mid-run (though current design is per-process so
    a reset isn't needed in normal operation)."""
    monkeypatch.setenv("WEB_UNLOCKER_MAX_CALLS_PER_JOB", "2")
    captured: list[bytes] = []
    _stub_brightdata(monkeypatch, captured)

    _probe.web_unlocker_get("https://x.com/", timeout=5)
    _probe.web_unlocker_get("https://y.com/", timeout=5)
    # Capped
    blocked = _probe.web_unlocker_get("https://z.com/", timeout=5)
    assert blocked.status_code == 0
    assert len(captured) == 2

    # Reset re-arms the budget
    _probe.reset_web_unlocker_call_count()
    assert _probe.web_unlocker_call_count() == 0
    again = _probe.web_unlocker_get("https://z.com/", timeout=5)
    assert again.status_code == 200
    assert len(captured) == 3
