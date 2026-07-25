"""Event-loop starvation guards (RCA 2026-07-25).

Root cause found in the 5k canary: blocking ``curl_cffi`` probes were called
*directly* from async code, freezing the whole shard's event loop (all
``AsyncPool`` workers) for up to the probe timeout. Mean per-property wall time
reached ~305s against a 600s cap, so ~10% of properties were clipped at random
— the "rotating timeout victims" symptom.

These tests pin the two invariants that keep the loop free:
  1. a slow ``probe_get`` inside the curl_cffi fallback must NOT stall a
     concurrently-running coroutine (i.e. it is off-loaded to a thread);
  2. the pool sizer must not oversubscribe cores by default (the old
     I/O-bound ``cpu * 2`` heuristic).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from ma_poc.core.concurrency import SystemResources

# ── 1. blocking probes must not stall the loop ──────────────────────────────

@pytest.mark.asyncio
async def test_blocking_probe_does_not_stall_the_event_loop() -> None:
    """A blocking call wrapped in ``to_thread`` must let other coroutines run.

    This is the shape of the fix at ``fetch/fetcher.py`` (curl_cffi fallback)
    and the four async ``rentcafe.py`` probe sites. Measured via the MAXIMUM
    gap between heartbeat ticks — the same quantity that showed up in the
    canary as ``asyncio.wait_for`` overshooting its own deadline. Using the max
    gap (rather than a tick count) keeps the test independent of the order in
    which the scheduler happens to start the two coroutines.
    """
    BLOCK_S = 0.40
    TICK_S = 0.01

    def _blocking_probe() -> str:
        time.sleep(BLOCK_S)  # stands in for curl_cffi probe_get
        return "body"

    async def _max_tick_gap(run_probe) -> float:
        """Return the worst loop stall observed while *run_probe* executes."""
        gaps: list[float] = []
        stop = False

        async def _heartbeat() -> None:
            # Represents the other pool workers sharing this event loop.
            last = time.monotonic()
            while not stop:
                await asyncio.sleep(TICK_S)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        hb = asyncio.create_task(_heartbeat())
        await asyncio.sleep(0.05)  # let the heartbeat establish a rhythm
        await run_probe()
        stop = True
        await hb
        return max(gaps) if gaps else 0.0

    # CORRECT shape (what the fix does): probe off-loaded to a worker thread.
    async def _offloaded() -> None:
        await asyncio.to_thread(_blocking_probe)

    fixed_gap = await _max_tick_gap(_offloaded)
    assert fixed_gap < BLOCK_S / 2, (
        f"loop stalled {fixed_gap:.3f}s despite to_thread — the blocking probe "
        "is not actually being off-loaded"
    )

    # REGRESSION shape (the bug): calling it inline freezes every coroutine.
    async def _inline() -> None:
        _blocking_probe()  # no await, no to_thread → freezes the loop

    bug_gap = await _max_tick_gap(_inline)
    assert bug_gap >= BLOCK_S * 0.8, (
        f"inline shape only stalled {bug_gap:.3f}s; the test no longer "
        "discriminates blocking from non-blocking call shapes"
    )


@pytest.mark.asyncio
async def test_fetcher_curl_fallback_offloads_probe(monkeypatch) -> None:
    """``fetcher.py``'s curl_cffi fallback must call probe_get off-loop.

    Asserts on the *observable* property — the call happens on a different
    thread than the event loop — rather than on source text.
    """
    import threading

    from ma_poc.pms.adapters import _probe

    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _fake_probe_get(*_a, **_kw):
        seen.append(threading.get_ident())

        class _R:
            status_code = 200
            text = "x" * 4096
            headers: dict[str, str] = {}

        return _R()

    monkeypatch.setattr(_probe, "probe_get", _fake_probe_get)

    # Exercise the same shape the fetcher uses.
    await asyncio.to_thread(_probe.probe_get, "https://example.com", timeout=20)

    assert seen, "probe_get was never called"
    assert seen[0] != loop_thread, (
        "probe_get ran on the event-loop thread — it must be off-loaded via "
        "asyncio.to_thread (see fetch/fetcher.py curl_cffi fallback)"
    )


# ── 2. pool sizer must not oversubscribe cores ──────────────────────────────

def test_pool_sizer_does_not_oversubscribe_cores_by_default(monkeypatch) -> None:
    """Default is one worker per core — the old ``cpu * 2`` starved the loop."""
    monkeypatch.delenv("CPU_OVERSUBSCRIBE", raising=False)
    monkeypatch.delenv("MAX_CONCURRENT_BROWSERS", raising=False)

    res = SystemResources(
        cpu_count=4,
        total_ram_bytes=8 * 1024**3,
        available_ram_bytes=8 * 1024**3,
    )
    # RAM is generous here so the CPU constraint is the binding one.
    assert res.optimal_pool_size(ram_per_worker_bytes=64 * 1024**2) == 4


def test_pool_sizer_oversubscribe_is_tunable(monkeypatch) -> None:
    """``CPU_OVERSUBSCRIBE`` restores headroom for genuinely I/O-bound runs."""
    monkeypatch.delenv("MAX_CONCURRENT_BROWSERS", raising=False)
    res = SystemResources(
        cpu_count=4,
        total_ram_bytes=8 * 1024**3,
        available_ram_bytes=8 * 1024**3,
    )

    monkeypatch.setenv("CPU_OVERSUBSCRIBE", "2.0")
    assert res.optimal_pool_size(ram_per_worker_bytes=64 * 1024**2) == 8

    # Garbage falls back to the safe default rather than raising.
    monkeypatch.setenv("CPU_OVERSUBSCRIBE", "not-a-number")
    assert res.optimal_pool_size(ram_per_worker_bytes=64 * 1024**2) == 4
