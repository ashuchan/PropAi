"""Tests for the distributed (cross-task) per-host token bucket.

Uses a temp-file sqlite DB (so state persists across sessions) + an
injected clock, so the token-bucket math is asserted deterministically
without real sleeps. The true multi-process guarantee needs Postgres
``SELECT … FOR UPDATE`` (a no-op on sqlite); here we prove the algorithm.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ma_poc.data_provider.sql.engine import make_engine
from ma_poc.fetch import distributed_throttle as dt


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "RATELIMIT_DISTRIBUTED_ENABLED",
        "RATELIMIT_DISTRIBUTED_DEFAULT_RPS",
        "RATELIMIT_DISTRIBUTED_CAPACITY",
        "RATELIMIT_DISTRIBUTED_MAX_WAIT_S",
        "RATELIMIT_DISTRIBUTED_PER_HOST_RPS",
    ):
        monkeypatch.delenv(key, raising=False)
    dt._reset_for_tests()


def _limiter(tmp_path: Path, now: list[float]) -> dt.DistributedHostRateLimiter:
    engine = make_engine(f"sqlite:///{tmp_path}/rl.db")
    lim = dt.DistributedHostRateLimiter(engine=engine, clock=lambda: now[0])
    lim.ensure_schema()
    return lim


def test_first_acquire_is_immediate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATELIMIT_DISTRIBUTED_DEFAULT_RPS", "10")
    monkeypatch.setenv("RATELIMIT_DISTRIBUTED_CAPACITY", "1")
    lim = _limiter(tmp_path, [1000.0])
    assert lim.acquire("sightmap.com") == 0.0


def test_sequential_pacing_queues_at_one_over_rps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RATELIMIT_DISTRIBUTED_DEFAULT_RPS", "10")  # 100 ms spacing
    monkeypatch.setenv("RATELIMIT_DISTRIBUTED_CAPACITY", "1")
    lim = _limiter(tmp_path, [1000.0])
    waits = [lim.acquire("sightmap.com") for _ in range(4)]
    # Concurrent-at-same-instant callers queue at exactly 1/rps spacing —
    # this is the cross-task aggregate cap (all tasks share the one row).
    assert waits[0] == 0.0
    assert waits[1] == pytest.approx(0.1, abs=1e-6)
    assert waits[2] == pytest.approx(0.2, abs=1e-6)
    assert waits[3] == pytest.approx(0.3, abs=1e-6)


def test_burst_capacity_allows_n_immediate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RATELIMIT_DISTRIBUTED_DEFAULT_RPS", "10")
    monkeypatch.setenv("RATELIMIT_DISTRIBUTED_CAPACITY", "3")
    lim = _limiter(tmp_path, [1000.0])
    waits = [lim.acquire("sightmap.com") for _ in range(4)]
    assert waits[0] == 0.0 and waits[1] == 0.0 and waits[2] == 0.0
    assert waits[3] == pytest.approx(0.1, abs=1e-6)


def test_refill_after_elapsed_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATELIMIT_DISTRIBUTED_DEFAULT_RPS", "10")
    monkeypatch.setenv("RATELIMIT_DISTRIBUTED_CAPACITY", "1")
    now = [1000.0]
    lim = _limiter(tmp_path, now)
    assert lim.acquire("h.com") == 0.0  # tokens 1 -> 0
    now[0] += 0.1  # 0.1 s * 10 rps = 1 token refilled
    assert lim.acquire("h.com") == 0.0  # immediate again


def test_hosts_have_independent_buckets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RATELIMIT_DISTRIBUTED_DEFAULT_RPS", "10")
    monkeypatch.setenv("RATELIMIT_DISTRIBUTED_CAPACITY", "1")
    lim = _limiter(tmp_path, [1000.0])
    assert lim.acquire("a.com") == 0.0
    assert lim.acquire("b.com") == 0.0  # separate row, still immediate


def test_per_host_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATELIMIT_DISTRIBUTED_DEFAULT_RPS", "1000")  # others fast
    monkeypatch.setenv("RATELIMIT_DISTRIBUTED_CAPACITY", "1")
    monkeypatch.setenv("RATELIMIT_DISTRIBUTED_PER_HOST_RPS", '{"sightmap.com": 10}')
    lim = _limiter(tmp_path, [1000.0])
    lim.acquire("sightmap.com")  # 1 -> 0
    assert lim.acquire("sightmap.com") == pytest.approx(0.1, abs=1e-6)  # 10 rps, not 1000


def test_max_wait_caps_single_caller_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RATELIMIT_DISTRIBUTED_DEFAULT_RPS", "0.1")  # 10 s / token
    monkeypatch.setenv("RATELIMIT_DISTRIBUTED_CAPACITY", "1")
    monkeypatch.setenv("RATELIMIT_DISTRIBUTED_MAX_WAIT_S", "2")
    lim = _limiter(tmp_path, [1000.0])
    lim.acquire("h.com")  # 1 -> 0
    assert lim.acquire("h.com") == 2.0  # 10 s deficit capped to 2 s


def test_throttle_disabled_is_noop_no_db(tmp_path: Path) -> None:
    # ENABLED unset -> pure pass-through, never constructs an engine / hits DB.
    with dt.throttle("https://sightmap.com/x"):
        pass


def test_ensure_schema_idempotent(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path}/x.db")
    lim = dt.DistributedHostRateLimiter(engine=engine, clock=lambda: 1000.0)
    lim.ensure_schema()
    lim.ensure_schema()  # second call must be a no-op, not an error
    assert lim.acquire("h.com") == 0.0
