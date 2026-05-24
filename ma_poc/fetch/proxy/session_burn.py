"""Per-session burn tracker for BrightData residential proxy (2026-05-24).

Background
==========
BrightData residential sessions pin one exit IP for the session TTL
(~30 min). The session-id is derived deterministically from the
property_id (``s{sha256(property_id)[:10]}``), so every retry on the
same property — even across separate ``fetch()`` calls — hits the
*same* exit IP.

If that IP gets burned by an operator's WAF (consecutive 403s from a
geo-gated listing endpoint), the property stays unreachable until the
BrightData session TTL flips. With our retry budget that means the
property fails permanently for the canary run.

The fix
=======
Track per-canonical-id consecutive BOT_BLOCKED counts. When a property
crosses the threshold, bump a *salt* into the session-id derivation so
BrightData hands us a fresh exit IP on the next request.

The tracker is:
* **In-process** — a process-wide singleton. Cloud Run instances are
  single-tenant for a shard, so no cross-process coordination is
  needed. Restarting the process resets the tracker — that's fine
  because BrightData session TTLs also flip after restarts.
* **TTL-decayed** — failure counters older than the BrightData
  session TTL (~30 min) are forgotten. A property that 403'd 60 min
  ago should get a clean slot today.
* **Thread-safe** — guarded by a single ``threading.Lock`` so async
  workers in the same process can all hit the tracker safely.

The salt is monotonically increasing. Salt 0 means "no rotation
needed yet"; salt 1 means "first rotation"; salt 2 the second, etc.
``next_salt()`` returns the salt to use for the *next* request, given
the burn state.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# Default threshold: two consecutive BOT_BLOCKED on the same session
# triggers a rotation. One isolated 403 might be a transient fluke
# (e.g. operator rate-limit) that doesn't warrant burning a fresh IP.
DEFAULT_ROTATE_AFTER_FAILURES: int = 2

# BrightData residential session TTL is documented as ~30 min. We use
# the same value as the burn-counter decay window: a property that
# 403'd more than TTL minutes ago has likely already been issued a
# fresh exit IP by BrightData's pool rotation, so we reset our local
# counter to avoid pessimistically over-rotating.
DEFAULT_TTL_SECONDS: float = 30 * 60.0


@dataclass
class _BurnInfo:
    """Per-canonical-id burn state."""

    # Number of consecutive BOT_BLOCKED responses on the current salt
    consecutive_failures: int = 0
    # Wall-clock seconds at which the latest failure was recorded
    last_failure_at: float = 0.0
    # Current salt — bumped each time we cross the rotate threshold
    current_salt: int = 0


class SessionBurnTracker:
    """In-process burn tracker for BrightData residential sessions.

    Typical lifecycle for one property over a canary run:

    1. ``next_salt("prop42")`` → ``0`` (clean slot, no burns).
    2. Fetcher uses session-id derived with ``salt=0``, hits BOT_BLOCKED.
    3. ``mark_failure("prop42")`` → counter 1, salt still 0.
    4. Fetcher checks ``next_salt("prop42")`` → still ``0``
       (below threshold).
    5. Retry on same salt fails again with BOT_BLOCKED.
    6. ``mark_failure("prop42")`` → counter 2 (threshold), salt bumps
       to ``1``.
    7. ``next_salt("prop42")`` → ``1`` — caller uses salt=1, gets a
       fresh IP from BrightData's pool.
    8. Success → ``mark_success("prop42")`` resets counter to 0;
       salt stays at 1 (a property that succeeded on a rotated salt
       should keep using that salt for follow-ups).
    """

    def __init__(
        self,
        *,
        rotate_after_failures: int = DEFAULT_ROTATE_AFTER_FAILURES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: "callable | None" = None,
    ) -> None:
        if rotate_after_failures < 1:
            raise ValueError(
                "rotate_after_failures must be >= 1 — 0 would force-rotate "
                "every request, breaking sticky-session geo-accuracy."
            )
        self._rotate_after = rotate_after_failures
        self._ttl = ttl_seconds
        self._clock = clock or time.monotonic
        self._state: dict[str, _BurnInfo] = {}
        self._lock = threading.Lock()

    # ── core API ──────────────────────────────────────────────────────

    def next_salt(self, canonical_id: str) -> int:
        """Return the salt the next request for ``canonical_id`` should use.

        Always returns the *current* salt — never advances it. The salt
        only advances inside ``mark_failure`` when the consecutive
        failure count crosses ``rotate_after_failures``.

        Returns ``0`` for a clean property (no recent burns) and a
        positive integer for a property that has been rotated at least
        once.
        """
        with self._lock:
            info = self._state.get(canonical_id)
            if info is None:
                return 0
            # TTL decay: if the last failure is older than the session TTL,
            # BrightData has likely cycled the IP underneath us anyway —
            # reset our local view to a clean slate.
            if self._is_stale_locked(info):
                self._state.pop(canonical_id, None)
                return 0
            return info.current_salt

    def mark_failure(self, canonical_id: str) -> int:
        """Record a BOT_BLOCKED on ``canonical_id``'s current session.

        Returns the salt the *next* request should use. If the failure
        count crosses ``rotate_after_failures``, the salt is advanced
        before returning, so the immediate next request gets a fresh
        BrightData exit IP.
        """
        with self._lock:
            now = self._clock()
            info = self._state.get(canonical_id)
            if info is None or self._is_stale_locked(info):
                # Fresh slot — start a clean count at 0; we'll bump it
                # below alongside the threshold check so both fresh and
                # existing paths apply the same rotation logic.
                info = _BurnInfo(
                    consecutive_failures=0,
                    last_failure_at=now,
                    current_salt=0,
                )
                self._state[canonical_id] = info
            info.consecutive_failures += 1
            info.last_failure_at = now
            if info.consecutive_failures >= self._rotate_after:
                # Cross the rotate threshold — bump salt, reset counter
                info.current_salt += 1
                info.consecutive_failures = 0
            return info.current_salt

    def mark_success(self, canonical_id: str, *, working_salt: int = 0) -> None:
        """Record a successful (OK) fetch on ``canonical_id``.

        Resets the consecutive-failure counter to 0 but keeps the
        current salt — a successful rotated salt should keep being
        used until it too gets burned. Resetting the salt back to 0 on
        every success would defeat the purpose of rotating.

        Args:
            canonical_id: Property identifier.
            working_salt: If the OK was obtained via an in-fetch
                forced-rotation (i.e. the working IP came from a salt
                higher than the tracker had recorded), pass that salt
                here. The tracker advances ``current_salt`` to
                ``max(current_salt, working_salt)`` so subsequent
                fetches start on the IP that's known to work. Default
                ``0`` is a no-op for the common path where success
                came from the first attempt.
        """
        if working_salt < 0:
            raise ValueError(
                f"working_salt must be >= 0, got {working_salt}"
            )
        with self._lock:
            info = self._state.get(canonical_id)
            if info is None:
                if working_salt > 0:
                    # First-ever entry for this property, but success
                    # came from a rotated salt — record it so the
                    # tracker remembers which IP works.
                    self._state[canonical_id] = _BurnInfo(
                        consecutive_failures=0,
                        last_failure_at=self._clock(),
                        current_salt=working_salt,
                    )
                return
            info.consecutive_failures = 0
            if working_salt > info.current_salt:
                info.current_salt = working_salt
            # Intentionally keep ``last_failure_at`` so the TTL decay
            # still applies if the property goes idle.

    def reset(self, canonical_id: str | None = None) -> None:
        """Wipe burn state.

        ``reset()`` clears everything (used between test cases).
        ``reset("propX")`` clears one property's entry.
        """
        with self._lock:
            if canonical_id is None:
                self._state.clear()
            else:
                self._state.pop(canonical_id, None)

    # ── introspection (for tests + telemetry) ─────────────────────────

    def state_snapshot(self, canonical_id: str) -> tuple[int, int, float]:
        """Return ``(consecutive_failures, current_salt, age_seconds)``.

        ``age_seconds`` is ``-1`` when the canonical_id has no entry.
        """
        with self._lock:
            info = self._state.get(canonical_id)
            if info is None:
                return (0, 0, -1.0)
            age = max(0.0, self._clock() - info.last_failure_at)
            return (info.consecutive_failures, info.current_salt, age)

    def tracked_count(self) -> int:
        """Number of canonical_ids currently held in the tracker.

        Useful as a smoke-test metric — if it grows unbounded across
        a long run, something is wrong with TTL eviction.
        """
        with self._lock:
            return len(self._state)

    # ── internals ─────────────────────────────────────────────────────

    def _is_stale_locked(self, info: _BurnInfo) -> bool:
        """Caller must hold ``self._lock``."""
        if info.last_failure_at == 0.0:
            return False
        return (self._clock() - info.last_failure_at) > self._ttl


# ── process-wide singleton ───────────────────────────────────────────

_DEFAULT_TRACKER: SessionBurnTracker | None = None
_DEFAULT_TRACKER_LOCK = threading.Lock()


def get_default_tracker() -> SessionBurnTracker:
    """Return the process-wide default tracker.

    Constructed lazily on first call so import-time side effects stay
    cheap. Tests that want isolation can construct their own
    ``SessionBurnTracker`` and inject it directly.
    """
    global _DEFAULT_TRACKER
    if _DEFAULT_TRACKER is not None:
        return _DEFAULT_TRACKER
    with _DEFAULT_TRACKER_LOCK:
        if _DEFAULT_TRACKER is None:
            _DEFAULT_TRACKER = SessionBurnTracker()
    return _DEFAULT_TRACKER


def reset_default_tracker() -> None:
    """Wipe the default tracker. Tests must call this in setup/teardown
    when they manipulate proxy state in ways that could leak between
    cases."""
    global _DEFAULT_TRACKER
    if _DEFAULT_TRACKER is not None:
        _DEFAULT_TRACKER.reset()
