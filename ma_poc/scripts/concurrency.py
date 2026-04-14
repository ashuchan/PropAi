"""
Multi-threaded / async-concurrent utility for property scraping.
================================================================

Detects system resources (CPU cores, available RAM) and calculates
an optimal worker pool size for Playwright-based scraping where each
worker consumes ~250 MB of RAM for a headless Chromium instance.

Two execution strategies are provided:

  1. **AsyncPool** — asyncio.Semaphore + gather.  Best for I/O-bound
     Playwright scraping within a single event loop.

  2. **ThreadedPool** — concurrent.futures.ThreadPoolExecutor where
     each thread spins up its own asyncio event loop.  Useful when
     individual scrapes block the loop (e.g. synchronous post-
     processing) or when the caller is not already async.

Usage::

    from concurrency import SystemResources, AsyncPool, ThreadedPool

    # Auto-detect optimal pool size
    res = SystemResources.detect()
    pool_size = res.optimal_pool_size()

    # Async strategy (inside an existing event loop)
    pool = AsyncPool(pool_size)
    results = await pool.map(scrape_one, list_of_args)

    # Threaded strategy (from synchronous code)
    pool = ThreadedPool(pool_size)
    results = pool.map(scrape_one_sync, list_of_args)
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import platform
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, TypeVar

log = logging.getLogger("concurrency")

T = TypeVar("T")

# ── System resource detection ────────────────────────────────────────────────

# Estimated RAM consumed by one headless Chromium instance (bytes).
_BROWSER_RAM_MB = 250
_BROWSER_RAM_BYTES = _BROWSER_RAM_MB * 1024 * 1024

# Fraction of *available* RAM we're willing to allocate to browsers.
# Keep a 30 % margin so the OS, Python, and other processes stay healthy.
_RAM_BUDGET_FRACTION = 0.70

# Hard floor / ceiling regardless of what detection says.
_MIN_WORKERS = 1
_MAX_WORKERS_HARD_CAP = 32


@dataclass
class SystemResources:
    """Snapshot of the machine's CPU and memory capacity."""

    cpu_count: int
    total_ram_bytes: int
    available_ram_bytes: int

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def detect(cls) -> SystemResources:
        """Detect CPU count and RAM.  Works on Windows, Linux, and macOS."""
        cpus = os.cpu_count() or 4

        total, available = cls._detect_ram()
        return cls(
            cpu_count=cpus,
            total_ram_bytes=total,
            available_ram_bytes=available,
        )

    # ── Optimal pool size ────────────────────────────────────────────────────

    def optimal_pool_size(
        self,
        *,
        ram_per_worker_bytes: int = _BROWSER_RAM_BYTES,
        ram_budget_fraction: float = _RAM_BUDGET_FRACTION,
        env_override_key: str = "MAX_CONCURRENT_BROWSERS",
    ) -> int:
        """
        Return the number of concurrent workers this machine can sustain.

        The calculation takes the *minimum* of three constraints:

          1. **RAM-based** — ``available_ram * budget_fraction / ram_per_worker``.
             Uses *available* (not total) RAM so we respect what the OS is
             actually offering right now.

          2. **CPU-based** — ``cpu_count * 2``.  Playwright scraping is I/O-
             bound so ~2× CPU count is a reasonable upper bound before context-
             switch overhead dominates.

          3. **Environment cap** — ``MAX_CONCURRENT_BROWSERS`` env var (if set).
             Lets operators hard-cap concurrency independent of hardware.

        The result is clamped to [1, 32].
        """
        # Constraint 1 — RAM
        usable_ram = int(self.available_ram_bytes * ram_budget_fraction)
        ram_limit = max(1, usable_ram // ram_per_worker_bytes)

        # Constraint 2 — CPU (I/O-bound heuristic: 2× cores)
        cpu_limit = max(1, self.cpu_count * 2)

        # Constraint 3 — environment override
        env_val = os.environ.get(env_override_key)
        env_limit = _MAX_WORKERS_HARD_CAP
        if env_val:
            try:
                env_limit = int(env_val)
            except ValueError:
                log.warning(
                    f"{env_override_key}={env_val!r} is not an integer; ignoring"
                )

        pool_size = min(ram_limit, cpu_limit, env_limit)
        pool_size = max(_MIN_WORKERS, min(pool_size, _MAX_WORKERS_HARD_CAP))

        log.info(
            f"Pool size calculation: "
            f"ram_limit={ram_limit} (avail={self.available_ram_bytes / (1024**3):.1f}GB, "
            f"budget={ram_budget_fraction:.0%}, per_worker={ram_per_worker_bytes // (1024**2)}MB), "
            f"cpu_limit={cpu_limit} (cores={self.cpu_count}), "
            f"env_limit={env_limit} → pool_size={pool_size}"
        )
        return pool_size

    # ── RAM detection (cross-platform) ───────────────────────────────────────

    @staticmethod
    def _detect_ram() -> tuple[int, int]:
        """Return (total_ram_bytes, available_ram_bytes)."""
        system = platform.system()

        if system == "Windows":
            return SystemResources._detect_ram_windows()
        elif system == "Linux":
            return SystemResources._detect_ram_linux()
        elif system == "Darwin":
            return SystemResources._detect_ram_darwin()

        # Fallback: assume 8 GB total / 4 GB available.
        log.warning(f"Unknown platform {system!r}; using 8GB/4GB RAM fallback")
        return (8 * 1024**3, 4 * 1024**3)

    @staticmethod
    def _detect_ram_windows() -> tuple[int, int]:
        """Use Win32 GlobalMemoryStatusEx via ctypes."""

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        ms = MEMORYSTATUSEX()
        ms.dwLength = ctypes.sizeof(ms)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))  # type: ignore[attr-defined]
        return (ms.ullTotalPhys, ms.ullAvailPhys)

    @staticmethod
    def _detect_ram_linux() -> tuple[int, int]:
        """Parse /proc/meminfo."""
        info: dict[str, int] = {}
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            key = parts[0].rstrip(":")
                            val = int(parts[1]) * 1024  # kB → bytes
                            info[key] = val
                        except ValueError:
                            continue  # skip malformed lines, don't bail
        except OSError:
            return (8 * 1024**3, 4 * 1024**3)

        total = info.get("MemTotal", 8 * 1024**3)
        available = info.get("MemAvailable", info.get("MemFree", total // 2))
        return (total, available)

    @staticmethod
    def _detect_ram_darwin() -> tuple[int, int]:
        """Use sysctl on macOS."""
        import subprocess

        total = 8 * 1024**3
        available = 4 * 1024**3
        try:
            out = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], text=True
            ).strip()
            total = int(out)
        except Exception:
            pass
        try:
            # vm_stat gives pages; page size is typically 16384 on Apple Silicon.
            out = subprocess.check_output(["vm_stat"], text=True)
            page_size = 16384
            free_pages = 0
            for line in out.splitlines():
                if "page size of" in line:
                    page_size = int(line.split()[-2])
                if "Pages free" in line:
                    free_pages += int(line.split()[-1].rstrip("."))
                if "Pages inactive" in line:
                    free_pages += int(line.split()[-1].rstrip("."))
            available = free_pages * page_size
        except Exception:
            available = total // 2
        return (total, available)

    def summary(self) -> str:
        """Human-readable one-liner."""
        return (
            f"CPUs={self.cpu_count}, "
            f"RAM total={self.total_ram_bytes / (1024**3):.1f}GB, "
            f"RAM available={self.available_ram_bytes / (1024**3):.1f}GB"
        )


# ── Async pool (semaphore + gather) ─────────────────────────────────────────


class AsyncPool:
    """
    Run async callables concurrently, bounded by a semaphore.

    Best for I/O-bound Playwright scraping within a running event loop.

    Usage::

        pool = AsyncPool(max_workers=6)
        results = await pool.map(scrape_one, [(url1, proxy), (url2, proxy), ...])

    Each element of *args_list* is unpacked as ``*args`` into *fn*.
    Exceptions are caught per-task and returned as the result (never crash
    the batch).
    """

    def __init__(self, max_workers: int) -> None:
        self.max_workers = max(1, max_workers)
        self._semaphore = asyncio.Semaphore(self.max_workers)
        self._completed = 0
        self._total = 0

    async def _run_one(
        self,
        index: int,
        fn: Callable[..., Awaitable[T]],
        args: tuple,
    ) -> tuple[int, T | Exception]:
        async with self._semaphore:
            try:
                result = await fn(*args)
                return (index, result)
            except Exception as exc:
                log.warning(f"AsyncPool task {index} raised {type(exc).__name__}: {exc}")
                return (index, exc)
            finally:
                self._completed += 1
                total = self._total
                if total > 0 and self._completed % max(1, total // 10) == 0:
                    log.info(
                        f"AsyncPool progress: {self._completed}/{total} "
                        f"({100 * self._completed / total:.0f}%)"
                    )

    async def map(
        self,
        fn: Callable[..., Awaitable[T]],
        args_list: list[tuple],
    ) -> list[T | Exception]:
        """
        Run *fn* for each argument tuple in *args_list*, returning results
        in the same order.  Exceptions are returned inline (not raised).
        """
        self._total = len(args_list)
        self._completed = 0

        tasks = [
            asyncio.create_task(self._run_one(i, fn, args))
            for i, args in enumerate(args_list)
        ]

        raw = await asyncio.gather(*tasks)
        # Re-order by original index.
        ordered: list[Any] = [None] * len(args_list)
        for idx, result in raw:
            ordered[idx] = result
        return ordered


# ── Threaded pool (ThreadPoolExecutor) ───────────────────────────────────────


class ThreadedPool:
    """
    Run callables across OS threads using ThreadPoolExecutor.

    Each thread can optionally spin up its own asyncio event loop (for
    running async functions from synchronous entry points like ``main()``).

    Usage::

        pool = ThreadedPool(max_workers=6)

        # Synchronous callables
        results = pool.map(process_property, [(row1,), (row2,), ...])

        # Async callables (each gets its own event loop in its thread)
        results = pool.map_async(scrape_one, [(url1, proxy, 180), ...])
    """

    def __init__(self, max_workers: int) -> None:
        self.max_workers = max(1, max_workers)

    def map(
        self,
        fn: Callable[..., T],
        args_list: list[tuple],
        *,
        timeout_per_task: float | None = None,
    ) -> list[T | Exception]:
        """
        Submit synchronous *fn* for each args tuple.  Returns results in
        order; exceptions are returned inline.
        """
        results: list[Any] = [None] * len(args_list)
        total = len(args_list)
        completed = 0

        with ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="scrape-worker",
        ) as executor:
            future_to_idx = {
                executor.submit(fn, *args): i
                for i, args in enumerate(args_list)
            }

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result(timeout=timeout_per_task)
                except Exception as exc:
                    log.warning(
                        f"ThreadedPool task {idx} raised "
                        f"{type(exc).__name__}: {exc}"
                    )
                    results[idx] = exc
                finally:
                    completed += 1
                    if total > 0 and completed % max(1, total // 10) == 0:
                        log.info(
                            f"ThreadedPool progress: {completed}/{total} "
                            f"({100 * completed / total:.0f}%)"
                        )

        return results

    def map_async(
        self,
        fn: Callable[..., Awaitable[T]],
        args_list: list[tuple],
    ) -> list[T | Exception]:
        """
        Run async *fn* in threads, each with its own event loop.

        Useful when calling from synchronous code that needs to run many
        independent async operations in parallel.
        """

        def _thread_runner(async_fn: Callable, args: tuple) -> Any:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(async_fn(*args))
            finally:
                # Drain pending tasks and async generators before closing so
                # httpx/Playwright background ``aclose()`` coroutines don't
                # spam "Event loop is closed" errors after we return.
                try:
                    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True),
                        )
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                finally:
                    loop.close()

        return self.map(
            _thread_runner,
            [(fn, a) for a in args_list],
        )


# ── Convenience: one-call concurrent scrape ──────────────────────────────────


async def run_concurrent_scrapes(
    scrape_fn: Callable[..., Awaitable[dict]],
    work_items: list[tuple],
    *,
    max_workers: int | None = None,
) -> list[dict | Exception]:
    """
    High-level helper: detect resources, size the pool, run all scrapes.

    Parameters
    ----------
    scrape_fn:
        Async callable — signature must match the unpacked tuples in
        *work_items*.
    work_items:
        Each element is a tuple of args for one call to *scrape_fn*.
    max_workers:
        Override auto-detection.  ``None`` → auto-detect from system
        resources.

    Returns
    -------
    List in the same order as *work_items*.  Exceptions are returned
    inline (never raised).
    """
    if max_workers is None:
        res = SystemResources.detect()
        log.info(f"System: {res.summary()}")
        max_workers = res.optimal_pool_size()

    pool = AsyncPool(max_workers)
    log.info(
        f"Starting concurrent scrapes: {len(work_items)} items, "
        f"{max_workers} workers"
    )
    return await pool.map(scrape_fn, work_items)
