"""Per-shared-host politeness throttle for outbound scraper fetches.

Keys on the **registrable domain** (eTLD+1) of the request host so that
every property funnelling into one shared PMS backend shares ONE budget:
all ``*.securecafe.com`` client subdomains, the single ``sightmap.com``
API host, ``doorway-api.knockrentals.com``, ``www.rentcafe.com``,
``nestiolistings.com``, the ``realpage.com`` family, etc.

This is deliberately different from :class:`fetch.rate_limiter.HostRateLimiter`,
which keys on the property's OWN front-end ``netloc`` — a host that is
unique per property, so its token bucket never accumulates and never
protects the shared backend that is actually hit hundreds of times.

Two enforcement dimensions per registrable host:

* **rate** — a min-interval gate paces request *initiation* (with jitter,
  so N concurrent workers do not fire in lockstep).
* **concurrency** — a semaphore caps *simultaneous in-flight* requests to
  one backend.

Both a **sync** (``threading``) and an **async** (``asyncio``) variant are
provided, because the shared-backend "drill" fetches split across:

* sync ``curl_cffi`` via ``probe_get`` / ``probe_post``
  (``pms/adapters/_probe.py``), which run in the async pool's worker
  threads — a threading throttle blocks the worker thread, never the loop;
* async ``httpx`` in a few adapters (Knock ``doorway-api``, RentCafe-direct
  ``www.rentcafe.com``).

Config (env), with **safe, generous defaults** — the throttle acts as a
ceiling on abusive bursts, not a pacing brake at normal load:

===========================  =========  =====================================
Env var                      Default    Meaning
===========================  =========  =====================================
``RATELIMIT_ENABLED``        ``"1"``    ``"0"`` fully disables (pure pass-through)
``RATELIMIT_DEFAULT_RPS``    ``8.0``    requests/sec per registrable host, per process
``RATELIMIT_HOST_CONCURRENCY`` ``8``    max simultaneous per host, per process
``RATELIMIT_PER_HOST_RPS``   (unset)    JSON overrides, e.g. ``{"sightmap.com": 2}``
``RATELIMIT_DIVIDE_BY_TASKS`` ``"0"``   opt-in: divide RPS by ``CLOUD_RUN_TASK_COUNT``
                                        so the *aggregate* rate across parallel
                                        Cloud Run tasks stays ≈ ``RATELIMIT_DEFAULT_RPS``
                                        (strict politeness; slower at high parallelism)
``RATELIMIT_JITTER_MS``      ``150``    uniform ``[0, N]`` ms added to each wait
===========================  =========  =====================================

Config is read from the environment on every call (mirroring the
``_wu_cap()`` pattern in ``_probe.py``), so tests and per-run tuning take
effect without a process restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import threading
import time
from collections.abc import AsyncIterator, Iterator
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# Minimal public-suffix set for the two-label TLDs that would otherwise
# collapse to a bare suffix (``co.uk`` -> ``uk``). The PMS backends this
# protects are all single-label ``.com`` hosts, so this only guards against
# the odd non-US front-end leaking into a drill URL.
_MULTI_LABEL_SUFFIXES: frozenset[str] = frozenset(
    {
        "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "ltd.uk", "plc.uk",
        "com.au", "net.au", "org.au", "co.nz", "co.za", "com.br", "com.mx",
        "co.jp", "co.in", "co.kr", "com.sg", "com.hk",
    }
)


def _normalize_host(url_or_host: str) -> str:
    """Full normalized hostname: lower-case, no scheme/path/creds/port, no
    trailing dot. Empty on unparseable input.

    Accepts a full URL (``https://a.b.securecafe.com/x``) or a bare host
    (``a.b.securecafe.com``, optionally with ``user:pass@`` creds or a
    ``:port``).
    """
    if not url_or_host:
        return ""
    raw = url_or_host.strip()
    host = urlsplit(raw).hostname if "://" in raw else raw.split("/", 1)[0]
    if not host:
        return ""
    host = host.split("@")[-1]          # strip any user:pass@
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host  # strip :port
    return host.strip("[]").rstrip(".").lower()


def throttle_key(url_or_host: str) -> str:
    """Bucket key for the politeness throttle — the FULL hostname.

    Deliberately NOT the registrable domain. Sharded PMS backends serve each
    tenant on its own subdomain (``client-a.securecafe.com``,
    ``foo.appfolio.com``, ``bar.myresman.com``) — distinct servers that must
    NOT share one budget, or ~28k securecafe tenants collapse into a single
    bucket and serialize behind it (a ~93-minute admission floor + mass
    timeout failures). The genuinely single-hostname backends
    (``sightmap.com``, ``doorway-api.knockrentals.com``, ``www.rentcafe.com``,
    ``nestiolistings.com``) are already one host, so full-host keying gives
    them exactly one bucket. ``RATELIMIT_PER_HOST_RPS`` overrides key on this
    same full host.
    """
    return _normalize_host(url_or_host)


def registrable_domain(url_or_host: str) -> str:
    """Return the registrable domain (eTLD+1) for *url_or_host*.

    Retained for the distributed-throttle reference module + its tests; the
    in-process throttle keys on :func:`throttle_key` (full host) instead — see
    that function for why registrable-domain collapse is wrong for sharded
    backends.

    Examples::

        registrable_domain("https://client.securecafe.com/x") -> "securecafe.com"
        registrable_domain("shop.example.co.uk")              -> "example.co.uk"
    """
    host = _normalize_host(url_or_host)
    if not host:
        return ""
    labels = host.split(".")
    # IPv4 literal -> use as-is (one server, its own bucket).
    if len(labels) == 4 and all(lbl.isdigit() for lbl in labels):
        return host
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_LABEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last_two


# --------------------------------------------------------------------------- #
# Config (read live from env on each call)                                     #
# --------------------------------------------------------------------------- #

def _enabled() -> bool:
    return os.getenv("RATELIMIT_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off", "",
    )


def _default_rps() -> float:
    try:
        return max(0.05, float(os.getenv("RATELIMIT_DEFAULT_RPS", "8")))
    except (TypeError, ValueError):
        return 8.0


def _host_concurrency() -> int:
    try:
        return max(1, int(os.getenv("RATELIMIT_HOST_CONCURRENCY", "8")))
    except (TypeError, ValueError):
        return 8


def _jitter_s() -> float:
    try:
        return max(0.0, float(os.getenv("RATELIMIT_JITTER_MS", "150")) / 1000.0)
    except (TypeError, ValueError):
        return 0.15


def _task_count() -> int:
    for key in ("CLOUD_RUN_TASK_COUNT", "JUGNU_TASK_COUNT", "CLOUD_RUN_JOB_TASK_COUNT"):
        val = os.getenv(key)
        if val:
            try:
                return max(1, int(val))
            except (TypeError, ValueError):
                continue
    return 1


def _divide_by_tasks() -> bool:
    # Default OFF: dividing the per-host RPS by CLOUD_RUN_TASK_COUNT gives a
    # truly polite *aggregate* rate, but at high parallelism it throttles each
    # task hard (8 rps / 40 tasks = 0.2 rps/host) and can slow a big run. Opt
    # in (RATELIMIT_DIVIDE_BY_TASKS=1) when strict aggregate politeness matters
    # more than wall-clock; the per-task caps below already bound each process.
    return os.getenv("RATELIMIT_DIVIDE_BY_TASKS", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _per_host_rps_override(host: str) -> float | None:
    raw = os.getenv("RATELIMIT_PER_HOST_RPS", "").strip()
    if not raw:
        return None
    try:
        mapping = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(mapping, dict):
        return None
    val = mapping.get(host)
    if val is None:
        return None
    try:
        return max(0.05, float(val))
    except (TypeError, ValueError):
        return None


def _effective_rps(host: str) -> float:
    """Per-process RPS budget for *host*, after per-host override and the
    optional cross-task division."""
    base = _per_host_rps_override(host)
    if base is None:
        base = _default_rps()
    if _divide_by_tasks():
        base = base / _task_count()
    return max(0.01, base)


# --------------------------------------------------------------------------- #
# Sync throttle (threading) — for probe_get / probe_post curl_cffi drills      #
# --------------------------------------------------------------------------- #

class _SyncHostThrottle:
    """Per-registrable-host rate + concurrency gate for worker-thread callers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_ok: dict[str, float] = {}
        self._sems: dict[str, threading.Semaphore] = {}

    def _sem(self, host: str) -> threading.Semaphore:
        with self._lock:
            sem = self._sems.get(host)
            if sem is None:
                sem = threading.Semaphore(_host_concurrency())
                self._sems[host] = sem
            return sem

    def _reserve(self, host: str) -> float:
        """Reserve the next initiation slot for *host*; return seconds to wait."""
        interval = 1.0 / _effective_rps(host)
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_ok.get(host, 0.0))
            self._next_ok[host] = start + interval
        return max(0.0, start - now)

    @contextlib.contextmanager
    def hold(self, host: str) -> Iterator[None]:
        sem = self._sem(host)
        sem.acquire()
        try:
            wait = self._reserve(host)
            if wait > 0.0:
                time.sleep(wait + random.uniform(0.0, _jitter_s()))
            yield
        finally:
            sem.release()


_SYNC: _SyncHostThrottle | None = None
_SYNC_LOCK = threading.Lock()


def get_sync_throttle() -> _SyncHostThrottle:
    global _SYNC
    with _SYNC_LOCK:
        if _SYNC is None:
            _SYNC = _SyncHostThrottle()
        return _SYNC


@contextlib.contextmanager
def throttle(url_or_host: str) -> Iterator[None]:
    """Block (in the calling thread) until it is polite to hit the registrable
    host of *url_or_host*. No-op when disabled or the host is unparseable."""
    if not _enabled():
        yield
        return
    host = throttle_key(url_or_host)
    if not host:
        yield
        return
    with get_sync_throttle().hold(host):
        yield


# --------------------------------------------------------------------------- #
# Async throttle (asyncio) — for httpx-based adapter drills                    #
# --------------------------------------------------------------------------- #

class _AsyncHostThrottle:
    """asyncio mirror of :class:`_SyncHostThrottle`."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._next_ok: dict[str, float] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}

    def _sem(self, host: str) -> asyncio.Semaphore:
        sem = self._sems.get(host)
        if sem is None:
            sem = asyncio.Semaphore(_host_concurrency())
            self._sems[host] = sem
        return sem

    async def _reserve(self, host: str) -> float:
        interval = 1.0 / _effective_rps(host)
        async with self._lock:
            now = time.monotonic()
            start = max(now, self._next_ok.get(host, 0.0))
            self._next_ok[host] = start + interval
        return max(0.0, start - now)

    @contextlib.asynccontextmanager
    async def hold(self, host: str) -> AsyncIterator[None]:
        sem = self._sem(host)
        await sem.acquire()
        try:
            wait = await self._reserve(host)
            if wait > 0.0:
                await asyncio.sleep(wait + random.uniform(0.0, _jitter_s()))
            yield
        finally:
            sem.release()


_ASYNC: _AsyncHostThrottle | None = None


def get_async_throttle() -> _AsyncHostThrottle:
    global _ASYNC
    if _ASYNC is None:
        _ASYNC = _AsyncHostThrottle()
    return _ASYNC


@contextlib.asynccontextmanager
async def async_throttle(url_or_host: str) -> AsyncIterator[None]:
    """Async equivalent of :func:`throttle` for httpx-based drills."""
    if not _enabled():
        yield
        return
    host = throttle_key(url_or_host)
    if not host:
        yield
        return
    async with get_async_throttle().hold(host):
        yield


def _reset_for_tests() -> None:
    """Drop cached singletons so a test can rebuild state with fresh env."""
    global _SYNC, _ASYNC
    with _SYNC_LOCK:
        _SYNC = None
    _ASYNC = None
