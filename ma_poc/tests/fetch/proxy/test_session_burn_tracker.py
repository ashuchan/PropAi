"""Unit tests for SessionBurnTracker (2026-05-24).

No network. Exercises the burn-count threshold, TTL decay, salt
monotonicity, success/failure interplay, and thread safety.
"""

from __future__ import annotations

import threading

import pytest

from ma_poc.fetch.proxy.session_burn import (
    DEFAULT_ROTATE_AFTER_FAILURES,
    DEFAULT_TTL_SECONDS,
    SessionBurnTracker,
    get_default_tracker,
    reset_default_tracker,
)


# ── construction guards ───────────────────────────────────────────────


def test_rotate_after_failures_must_be_positive() -> None:
    """Threshold 0 would force-rotate every request, breaking sticky
    sessions. Reject at construction."""
    with pytest.raises(ValueError, match="rotate_after_failures must be >= 1"):
        SessionBurnTracker(rotate_after_failures=0)


def test_rotate_after_failures_negative_rejected() -> None:
    with pytest.raises(ValueError):
        SessionBurnTracker(rotate_after_failures=-3)


def test_defaults_are_sensible() -> None:
    assert DEFAULT_ROTATE_AFTER_FAILURES >= 1
    # TTL should be at least the documented BrightData session TTL
    # (≥ 30 minutes) so we don't decay state too aggressively
    assert DEFAULT_TTL_SECONDS >= 30 * 60


# ── clean-slot behaviour ──────────────────────────────────────────────


def test_next_salt_is_zero_for_unknown_property() -> None:
    t = SessionBurnTracker()
    assert t.next_salt("never-seen") == 0


def test_state_snapshot_reports_minus_one_age_for_unknown() -> None:
    t = SessionBurnTracker()
    failures, salt, age = t.state_snapshot("nobody")
    assert failures == 0 and salt == 0 and age == -1.0


# ── single failure below threshold ────────────────────────────────────


def test_one_failure_below_threshold_does_not_rotate() -> None:
    """With the default threshold of 2, one failure leaves salt at 0
    — an isolated 403 could be a transient operator rate-limit, not
    a burned IP."""
    t = SessionBurnTracker(rotate_after_failures=2)
    salt = t.mark_failure("propA")
    assert salt == 0
    assert t.next_salt("propA") == 0


def test_consecutive_failures_at_threshold_rotates() -> None:
    """The (threshold)th failure must advance the salt so the next
    request uses a fresh BrightData exit IP."""
    t = SessionBurnTracker(rotate_after_failures=2)
    t.mark_failure("propA")
    salt_after_second = t.mark_failure("propA")
    assert salt_after_second == 1, "second failure must trigger rotate"
    assert t.next_salt("propA") == 1


def test_salt_increments_monotonically_across_burns() -> None:
    """Each time the counter crosses threshold again, salt advances by 1."""
    t = SessionBurnTracker(rotate_after_failures=2)
    # First burn → salt 1
    t.mark_failure("propA"); t.mark_failure("propA")
    assert t.next_salt("propA") == 1
    # Second burn on the rotated session → salt 2
    t.mark_failure("propA"); t.mark_failure("propA")
    assert t.next_salt("propA") == 2
    # Third → salt 3
    t.mark_failure("propA"); t.mark_failure("propA")
    assert t.next_salt("propA") == 3


def test_threshold_one_rotates_on_every_failure() -> None:
    """``rotate_after_failures=1`` is an aggressive setting: every
    BOT_BLOCKED triggers a salt bump. Used when an operator is known
    to ban-on-first-touch."""
    t = SessionBurnTracker(rotate_after_failures=1)
    assert t.mark_failure("propA") == 1
    assert t.mark_failure("propA") == 2
    assert t.mark_failure("propA") == 3


# ── success resets the counter ────────────────────────────────────────


def test_success_resets_consecutive_failure_count() -> None:
    """A success in between failures resets the count — two failures
    after a success should NOT rotate if separated by an OK."""
    t = SessionBurnTracker(rotate_after_failures=2)
    t.mark_failure("propA")
    t.mark_success("propA")
    salt_after_one_more = t.mark_failure("propA")
    # Counter reset → only 1 consecutive failure → no rotation
    assert salt_after_one_more == 0


def test_success_keeps_current_salt() -> None:
    """A property that succeeds *after* its salt was bumped must keep
    the bumped salt — the rotated IP is the one that works, we want
    to keep using it for follow-up requests."""
    t = SessionBurnTracker(rotate_after_failures=2)
    t.mark_failure("propA"); t.mark_failure("propA")  # → salt 1
    assert t.next_salt("propA") == 1
    t.mark_success("propA")
    assert t.next_salt("propA") == 1, "success must not reset salt"


def test_success_with_working_salt_advances_current_salt() -> None:
    """In-fetch forced-rotation: the provider tried salt=0, failed,
    and got OK on salt=1 BEFORE the tracker's threshold would have
    advanced (only 1 mark_failure was called). Passing working_salt=1
    to mark_success pins the working IP so the NEXT fetch starts on
    salt=1 instead of regressing to salt=0."""
    t = SessionBurnTracker(rotate_after_failures=2)
    t.mark_failure("propA")  # count=1, tracker salt=0 (below threshold)
    t.mark_success("propA", working_salt=1)
    assert t.next_salt("propA") == 1, (
        "working_salt should pin the salt that actually worked"
    )


def test_success_with_working_salt_zero_is_unchanged() -> None:
    """working_salt=0 (the default) must NOT decrease the tracker salt
    when it was already higher — protects against accidental salt
    erasure on the common path."""
    t = SessionBurnTracker(rotate_after_failures=2)
    t.mark_failure("propA"); t.mark_failure("propA")  # → salt 1
    t.mark_success("propA", working_salt=0)
    assert t.next_salt("propA") == 1


def test_success_with_working_salt_lower_than_current_is_noop() -> None:
    """If the caller passes a working_salt lower than what the tracker
    already has (e.g. a stale callback), the tracker's higher salt
    must win — never regress."""
    t = SessionBurnTracker(rotate_after_failures=1)  # aggressive
    t.mark_failure("propA")  # → salt 1
    t.mark_failure("propA")  # → salt 2
    t.mark_success("propA", working_salt=1)
    assert t.next_salt("propA") == 2, "tracker must not regress salt"


def test_success_with_working_salt_on_unknown_property_creates_entry() -> None:
    """First-ever interaction for a property is a success on a rotated
    salt (e.g. a manual override). The tracker must create the entry
    and remember the working salt."""
    t = SessionBurnTracker()
    t.mark_success("propNew", working_salt=3)
    assert t.next_salt("propNew") == 3


def test_success_with_working_salt_unknown_zero_is_noop() -> None:
    """Don't allocate entries for unknown properties when working_salt=0
    — that's the common path and would balloon memory."""
    t = SessionBurnTracker()
    t.mark_success("propNew", working_salt=0)
    assert t.tracked_count() == 0


def test_success_with_negative_working_salt_rejected() -> None:
    t = SessionBurnTracker()
    with pytest.raises(ValueError, match="working_salt must be >= 0"):
        t.mark_success("propA", working_salt=-2)


def test_success_on_unknown_property_is_noop() -> None:
    """Calling mark_success on a property we've never seen must not
    crash and must not allocate state for it."""
    t = SessionBurnTracker()
    t.mark_success("never-seen")
    assert t.tracked_count() == 0


# ── TTL decay ─────────────────────────────────────────────────────────


def test_burn_state_decays_after_ttl() -> None:
    """A failure older than the TTL is forgotten — next_salt returns
    0, mark_failure starts a clean count."""
    fake_time = [1000.0]
    t = SessionBurnTracker(
        rotate_after_failures=2,
        ttl_seconds=300.0,
        clock=lambda: fake_time[0],
    )
    t.mark_failure("propA"); t.mark_failure("propA")  # → salt 1
    assert t.next_salt("propA") == 1

    # Advance past TTL
    fake_time[0] += 400.0
    # Decayed → fresh slot
    assert t.next_salt("propA") == 0
    # Verify the entry was actually removed
    failures, salt, age = t.state_snapshot("propA")
    assert failures == 0 and salt == 0 and age == -1.0


def test_failure_just_inside_ttl_keeps_state() -> None:
    fake_time = [1000.0]
    t = SessionBurnTracker(
        rotate_after_failures=2,
        ttl_seconds=300.0,
        clock=lambda: fake_time[0],
    )
    t.mark_failure("propA"); t.mark_failure("propA")  # → salt 1
    fake_time[0] += 299.0  # just under TTL
    assert t.next_salt("propA") == 1, "state must persist within TTL"


def test_mark_failure_on_stale_entry_starts_fresh_count() -> None:
    """If we recover a stale slot via mark_failure, it must start at
    count=1 / salt=0, not inherit the burned state."""
    fake_time = [1000.0]
    t = SessionBurnTracker(
        rotate_after_failures=2,
        ttl_seconds=60.0,
        clock=lambda: fake_time[0],
    )
    t.mark_failure("propA"); t.mark_failure("propA")  # → salt 1
    fake_time[0] += 100.0  # past TTL
    salt = t.mark_failure("propA")
    assert salt == 0, "recovered slot must start at salt 0"
    failures, current_salt, _ = t.state_snapshot("propA")
    assert failures == 1 and current_salt == 0


# ── reset ──────────────────────────────────────────────────────────────


def test_reset_all_clears_every_entry() -> None:
    t = SessionBurnTracker(rotate_after_failures=1)
    t.mark_failure("propA")
    t.mark_failure("propB")
    assert t.tracked_count() == 2
    t.reset()
    assert t.tracked_count() == 0
    assert t.next_salt("propA") == 0
    assert t.next_salt("propB") == 0


def test_reset_single_clears_only_named_entry() -> None:
    t = SessionBurnTracker(rotate_after_failures=1)
    t.mark_failure("propA")
    t.mark_failure("propB")
    t.reset("propA")
    assert t.tracked_count() == 1
    assert t.next_salt("propA") == 0
    assert t.next_salt("propB") == 1, "untouched entry must persist"


# ── per-property isolation ────────────────────────────────────────────


def test_properties_have_independent_state() -> None:
    """Two properties' burn counters and salts must never mix."""
    t = SessionBurnTracker(rotate_after_failures=2)
    # propA gets burned and rotated
    t.mark_failure("propA"); t.mark_failure("propA")
    # propB is untouched
    assert t.next_salt("propA") == 1
    assert t.next_salt("propB") == 0


# ── thread safety ─────────────────────────────────────────────────────


def test_concurrent_failures_do_not_corrupt_count() -> None:
    """50 threads each pumping 20 failures should produce exactly 1000
    counted failures total — no lost updates, no race-induced over-
    counting."""
    t = SessionBurnTracker(rotate_after_failures=10_000)  # never rotate

    def hammer() -> None:
        for _ in range(20):
            t.mark_failure("hot")

    threads = [threading.Thread(target=hammer) for _ in range(50)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    failures, salt, _ = t.state_snapshot("hot")
    assert failures == 1000, f"expected 1000 failures, got {failures}"
    assert salt == 0, "threshold not crossed → salt stays at 0"


# ── default singleton ─────────────────────────────────────────────────


def test_default_tracker_is_a_singleton() -> None:
    a = get_default_tracker()
    b = get_default_tracker()
    assert a is b


def test_reset_default_tracker_clears_singleton_state() -> None:
    tracker = get_default_tracker()
    tracker.mark_failure("global-prop")
    assert tracker.tracked_count() >= 1
    reset_default_tracker()
    assert tracker.tracked_count() == 0


# ── snapshot age sanity ───────────────────────────────────────────────


def test_state_snapshot_age_advances_with_clock() -> None:
    fake_time = [0.0]
    t = SessionBurnTracker(clock=lambda: fake_time[0])
    fake_time[0] = 100.0
    t.mark_failure("propA")
    fake_time[0] = 150.0
    failures, salt, age = t.state_snapshot("propA")
    assert failures == 1 and salt == 0
    assert age == pytest.approx(50.0, abs=0.5)
