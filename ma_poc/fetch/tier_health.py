"""Run-level fetch-tier circuit breaker.

Why this module exists (RCA 2026-07-25 / analysis 2026-07-27)
------------------------------------------------------------
``ProxyPool`` (proxy_pool.py) tracks per-proxy health and quarantines a proxy
once ``mark_failure`` has driven it below 0.25. That machinery is real, and
since ec292e9 the ``Fetcher`` inline loop actually drives it.

But the **tier escalator never touches ``ProxyPool`` at all**. Its providers
(``DcProxyProvider``, ``ResidentialProvider``) build their proxy URL from
``BrightDataProvider.get_config()``, an entirely separate source from
``PROXY_POOL_URLS``. There is no ``pick()``, no ``mark_failure()``, and
``_make_provider()`` constructs a fresh provider on every escalation hop, so
even provider-local state would be discarded. The escalator therefore had *no*
health tracking whatsoever — quarantine did not fail to engage on that path, it
did not exist there.

That is how one dead proxy produced 267 consecutive ``TRANSIENT`` fetches with
zero successes across just two shards of the 2026-07-22-hb250 run, silently
degrading all 4,982 properties for eight days before a post-hoc analysis caught
it. This module is the missing feedback loop: a tier that returns *nothing but
transport failures* for N consecutive attempts is dead, and a dead tier should
get loud and get out of the ladder rather than burn the whole run.

Design notes
------------
* **Only transport failures count.** ``BOT_BLOCKED`` means the tier delivered a
  response and the *site* refused it — that is the proxy working. Counting it
  would disable healthy tiers on a heavily-walled cohort. Same reasoning as
  ``_probe_proxy_note_failure``'s "a 403 from the target is the proxy working".
  ``DEAD_URL`` / ``HARD_FAIL`` are likewise site-side.
* **DIRECT is never guarded.** It has no proxy, and it is the floor the
  escalator falls back to. Disabling it could strand a run with an empty ladder.
* **Latched for the run, not a cooldown.** The ask was "stop using that tier for
  the remainder of the run". Process lifetime == run lifetime for the Cloud Run
  shard jobs, so module state is run state. ``reset()`` is the test hook.
* **Ships in SHADOW MODE.** ``ENABLE_TIER_HEALTH_GUARD`` defaults to false, so
  out of the box this module counts, logs and *alarms* but never changes
  routing. That is deliberate on two grounds. First, the alarm is the part that
  was actually missing on 2026-07-25 — the run degraded silently for eight days;
  detection alone would have caught it on day one. Second, the incident's own
  root cause was a flag (``ENABLE_DC_PROXY_TIER``) that defaulted ON and so was
  opt-OUT: every job that didn't explicitly disable it inherited the bad path.
  Shipping *this* guard default-on, and relying on four Cloud Run jobs to set
  ``=false``, would rebuild that trap with the polarity flipped.
  Set ``ENABLE_TIER_HEALTH_GUARD=true`` to let it skip dead tiers, once the
  shadow-mode alarms have been observed to fire only when they should.
"""

from __future__ import annotations

import logging
import os
import threading

from ma_poc.fetch.contracts import FetchOutcome
from ma_poc.observability.events import EventKind, emit

log = logging.getLogger(__name__)

#: Outcomes that indicate the *tier itself* failed to deliver a response.
#: Everything else (BOT_BLOCKED, DEAD_URL, HARD_FAIL, EMPTY_BODY, RATE_LIMITED)
#: means the tier reached the origin and the origin answered — not a tier fault.
TRANSPORT_FAILURES: frozenset[FetchOutcome] = frozenset(
    {FetchOutcome.TRANSIENT, FetchOutcome.PROXY_ERROR}
)

#: Never disabled — no proxy involved, and it is the escalator's fallback floor.
UNGUARDED_TIERS: frozenset[str] = frozenset({"DIRECT"})

_DEFAULT_DEAD_AFTER = 20

_lock = threading.Lock()
_consecutive_failures: dict[str, int] = {}
_successes: dict[str, int] = {}
_attempts: dict[str, int] = {}
_disabled: set[str] = set()


def dead_after() -> int:
    """Consecutive transport failures that mark a tier dead for the run.

    Read from ``FETCH_TIER_DEAD_AFTER`` at call time (not import time) so tests
    and per-job config can override it. Fault-tolerant: junk falls back to the
    default rather than raising mid-run.

    Returns:
        Threshold, at least 1.
    """
    try:
        return max(1, int(os.environ.get("FETCH_TIER_DEAD_AFTER", _DEFAULT_DEAD_AFTER)))
    except (TypeError, ValueError):
        return _DEFAULT_DEAD_AFTER


def guard_enabled() -> bool:
    """Whether the breaker may actually *disable* tiers. Opt-in.

    Default OFF — the module ships in shadow mode: it counts, it logs, it emits
    ``fetch.tier_disabled``, but the escalator keeps using the tier. Turning the
    skipping on is ``ENABLE_TIER_HEALTH_GUARD=true``.

    Returns:
        True only when explicitly enabled.
    """
    return os.environ.get("ENABLE_TIER_HEALTH_GUARD", "false").strip().lower() == "true"


def is_tier_disabled(tier_name: str) -> bool:
    """Whether *tier_name* has been latched off for the remainder of the run.

    Args:
        tier_name: ``FetchTier`` member name, e.g. ``"RESIDENTIAL"``.

    Returns:
        True if the breaker has tripped for this tier and the guard is enabled.
    """
    if not guard_enabled():
        return False
    with _lock:
        return tier_name in _disabled


def note_result(
    tier_name: str,
    outcome: FetchOutcome,
    *,
    property_id: str = "",
    error_signature: str | None = None,
) -> None:
    """Record one tier attempt and trip the breaker when a tier looks dead.

    A success — or any non-transport outcome, which proves the tier reached the
    origin — resets the consecutive-failure counter.

    Args:
        tier_name: ``FetchTier`` member name.
        outcome: The outcome the tier's provider returned.
        property_id: Property this attempt was for, for the trip event payload.
        error_signature: Last error signature, for the trip event payload.
    """
    if tier_name in UNGUARDED_TIERS:
        return

    with _lock:
        _attempts[tier_name] = _attempts.get(tier_name, 0) + 1

        if outcome not in TRANSPORT_FAILURES:
            # Reached the origin. Even a block or a 404 proves transport works.
            if outcome in (FetchOutcome.OK, FetchOutcome.NOT_MODIFIED):
                _successes[tier_name] = _successes.get(tier_name, 0) + 1
            _consecutive_failures[tier_name] = 0
            return

        fails = _consecutive_failures.get(tier_name, 0) + 1
        _consecutive_failures[tier_name] = fails

        if fails < dead_after() or tier_name in _disabled:
            return

        # Trip. Latch off and emit once — the run-level alarm that was missing.
        _disabled.add(tier_name)
        attempts = _attempts.get(tier_name, fails)
        successes = _successes.get(tier_name, 0)

    log.error(
        "FETCH TIER DEAD: %s returned %d consecutive transport failures "
        "(%d attempts, %d successes this run, last=%s). Disabling it for the "
        "remainder of the run. Check the proxy/backend behind this tier.",
        tier_name,
        fails,
        attempts,
        successes,
        error_signature,
    )
    emit(
        EventKind.FETCH_TIER_DISABLED,
        property_id,
        tier=tier_name,
        consecutive_failures=fails,
        attempts=attempts,
        successes=successes,
        threshold=dead_after(),
        error_signature=error_signature,
    )


def snapshot() -> dict[str, dict[str, object]]:
    """Per-tier health counters, for run reports and diagnostics.

    Returns:
        Mapping of tier name to its attempt/success/failure/disabled state.
    """
    with _lock:
        names = set(_attempts) | set(_successes) | set(_consecutive_failures) | _disabled
        return {
            name: {
                "attempts": _attempts.get(name, 0),
                "successes": _successes.get(name, 0),
                "consecutive_failures": _consecutive_failures.get(name, 0),
                "disabled": name in _disabled,
            }
            for name in sorted(names)
        }


def reset() -> None:
    """Clear all breaker state. Test hook; also safe at run start."""
    with _lock:
        _consecutive_failures.clear()
        _successes.clear()
        _attempts.clear()
        _disabled.clear()
