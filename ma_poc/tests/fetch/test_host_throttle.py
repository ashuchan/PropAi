"""Tests for the per-shared-host politeness throttle (fetch/host_throttle.py)."""

from __future__ import annotations

import threading
import time

import pytest

from ma_poc.fetch import host_throttle as ht


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from ambient RATELIMIT_* / task-count env + reset singletons."""
    for key in (
        "RATELIMIT_ENABLED",
        "RATELIMIT_DEFAULT_RPS",
        "RATELIMIT_HOST_CONCURRENCY",
        "RATELIMIT_PER_HOST_RPS",
        "RATELIMIT_DIVIDE_BY_TASKS",
        "RATELIMIT_JITTER_MS",
        "CLOUD_RUN_TASK_COUNT",
        "JUGNU_TASK_COUNT",
        "CLOUD_RUN_JOB_TASK_COUNT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RATELIMIT_JITTER_MS", "0")  # deterministic timing
    ht._reset_for_tests()


@pytest.mark.parametrize(
    ("inp", "expected"),
    [
        ("https://client.securecafe.com/onlineleasing/x/availableunits.aspx", "securecafe.com"),
        ("doorway-api.knockrentals.com", "knockrentals.com"),
        ("https://sightmap.com/app/api/v1/tok/sightmaps/9", "sightmap.com"),
        ("www.rentcafe.com", "rentcafe.com"),
        ("sub.a.b.prospectportal.com", "prospectportal.com"),
        ("https://user:pass@leasing.realpage.com:443/x", "realpage.com"),
        ("nestiolistings.com", "nestiolistings.com"),
        ("shop.example.co.uk", "example.co.uk"),
        ("192.168.0.1", "192.168.0.1"),
        ("localhost", "localhost"),
        ("", ""),
    ],
)
def test_registrable_domain(inp: str, expected: str) -> None:
    assert ht.registrable_domain(inp) == expected


@pytest.mark.parametrize(
    ("inp", "expected"),
    [
        ("https://client-a.securecafe.com/onlineleasing/x", "client-a.securecafe.com"),
        ("https://sightmap.com/app/api", "sightmap.com"),
        ("doorway-api.knockrentals.com", "doorway-api.knockrentals.com"),
        ("www.rentcafe.com", "www.rentcafe.com"),
        ("HTTPS://Foo.AppFolio.com:443/listings", "foo.appfolio.com"),
        ("bar.myresman.com", "bar.myresman.com"),
        ("", ""),
    ],
)
def test_throttle_key_is_full_host(inp: str, expected: str) -> None:
    # The throttle keys on the FULL host, NOT the registrable domain — so
    # per-tenant backends don't collapse into one bucket.
    assert ht.throttle_key(inp) == expected


def test_sharded_subdomains_get_independent_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fix for the review's blocker: two securecafe client subdomains must
    # NOT pace each other (registrable-domain keying would have collapsed the
    # ~28k securecafe tenants into one 5/s bucket).
    monkeypatch.setenv("RATELIMIT_DEFAULT_RPS", "20")
    ht._reset_for_tests()
    t0 = time.monotonic()
    with ht.throttle("https://client-a.securecafe.com/x"):
        pass
    with ht.throttle("https://client-b.securecafe.com/x"):
        pass
    assert time.monotonic() - t0 < 0.05  # distinct hosts -> no cross-pacing


def test_disabled_is_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATELIMIT_ENABLED", "0")
    monkeypatch.setenv("RATELIMIT_DEFAULT_RPS", "2")  # would pace hard if enabled
    ht._reset_for_tests()
    t0 = time.monotonic()
    for _ in range(6):
        with ht.throttle("https://sightmap.com/x"):
            pass
    assert time.monotonic() - t0 < 0.2  # no pacing at all


def test_sync_paces_same_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATELIMIT_DEFAULT_RPS", "20")  # 50 ms interval
    ht._reset_for_tests()
    t0 = time.monotonic()
    for _ in range(4):
        with ht.throttle("https://client.securecafe.com/a"):
            pass
    # First fires immediately, then 3 × 50 ms of pacing.
    assert time.monotonic() - t0 >= 0.13


def test_sync_hosts_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATELIMIT_DEFAULT_RPS", "20")
    ht._reset_for_tests()
    t0 = time.monotonic()
    for i in range(4):
        with ht.throttle(f"https://host.backend{i}.com/x"):
            pass
    # Four distinct registrable domains -> no cross-host pacing.
    assert time.monotonic() - t0 < 0.1


def test_per_host_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATELIMIT_DEFAULT_RPS", "1000")  # everything else fast
    monkeypatch.setenv("RATELIMIT_PER_HOST_RPS", '{"sightmap.com": 10}')
    ht._reset_for_tests()
    t0 = time.monotonic()
    for _ in range(3):
        with ht.throttle("https://sightmap.com/x"):
            pass
    assert time.monotonic() - t0 >= 0.18  # 10 rps -> 2 × 100 ms

    t1 = time.monotonic()
    for _ in range(3):
        with ht.throttle("https://rentcafe.com/x"):  # uses 1000 rps default
            pass
    assert time.monotonic() - t1 < 0.05


def test_divide_by_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATELIMIT_DEFAULT_RPS", "40")
    monkeypatch.setenv("RATELIMIT_DIVIDE_BY_TASKS", "1")  # opt-in (default off)
    monkeypatch.setenv("CLOUD_RUN_TASK_COUNT", "10")  # effective 4 rps -> 250 ms
    ht._reset_for_tests()
    t0 = time.monotonic()
    for _ in range(2):
        with ht.throttle("https://sightmap.com/x"):
            pass
    assert time.monotonic() - t0 >= 0.22


def test_sync_concurrency_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATELIMIT_DEFAULT_RPS", "1000")  # rate is not the constraint
    monkeypatch.setenv("RATELIMIT_HOST_CONCURRENCY", "2")
    ht._reset_for_tests()

    active = 0
    peak = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal active, peak
        with ht.throttle("https://sightmap.com/x"):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak <= 2  # per-host semaphore holds concurrency at the cap


@pytest.mark.asyncio
async def test_async_paces_same_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATELIMIT_DEFAULT_RPS", "20")
    ht._reset_for_tests()
    t0 = time.monotonic()
    for _ in range(4):
        async with ht.async_throttle("https://doorway-api.knockrentals.com/x"):
            pass
    assert time.monotonic() - t0 >= 0.13
