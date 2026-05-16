"""Tests for the WEDGE_RESCUE_RETRY EventKinds.

Added 2026-05-16 as the Step-3 follow-up. Previously the wedge-rescue
retry pass piggybacked its start signal onto PROPERTY_EMITTED with a
custom verdict string. The 2026-05-16 patch splits this into two
dedicated event kinds so cross-run analyzers can compute:

- rescue_attempt_rate  = STARTED count / property count
- rescue_recovery_rate = STARTED count where resolution == UPGRADED_TO_SUCCESS

without conflating retry telemetry with verdict telemetry.
"""
from __future__ import annotations

from ma_poc.observability.events import EventKind


def test_wedge_rescue_retry_started_exists() -> None:
    """The STARTED event kind must be in the enum."""
    assert hasattr(EventKind, "WEDGE_RESCUE_RETRY_STARTED")
    assert EventKind.WEDGE_RESCUE_RETRY_STARTED.value == \
        "extract.wedge_rescue_retry_started"


def test_wedge_rescue_retry_resolved_exists() -> None:
    """The RESOLVED event kind must be in the enum."""
    assert hasattr(EventKind, "WEDGE_RESCUE_RETRY_RESOLVED")
    assert EventKind.WEDGE_RESCUE_RETRY_RESOLVED.value == \
        "extract.wedge_rescue_retry_resolved"


def test_wedge_rescue_kinds_are_unique_strings() -> None:
    """STARTED and RESOLVED must have distinct values — analyzers depend
    on this to count attempt vs. resolution separately."""
    assert (
        EventKind.WEDGE_RESCUE_RETRY_STARTED.value
        != EventKind.WEDGE_RESCUE_RETRY_RESOLVED.value
    )


def test_wedge_rescue_kinds_namespace_is_extract() -> None:
    """Both events live under the ``extract.*`` namespace — that's where
    the runner emits them from. Analyzers filter by this prefix when
    computing the rescue rate."""
    assert EventKind.WEDGE_RESCUE_RETRY_STARTED.value.startswith("extract.")
    assert EventKind.WEDGE_RESCUE_RETRY_RESOLVED.value.startswith("extract.")


def test_runner_emits_started_for_each_retry_task() -> None:
    """Grep guard: jugnu.py must reference WEDGE_RESCUE_RETRY_STARTED in
    the retry-task construction block. Without this, no STARTED events
    fire and the analyzer can't compute attempt rate."""
    import inspect
    from ma_poc.scripts.runners import jugnu

    src = inspect.getsource(jugnu)
    assert "WEDGE_RESCUE_RETRY_STARTED" in src


def test_runner_emits_resolved_with_resolution_payload() -> None:
    """The RESOLVED event must include a ``resolution`` payload field
    that's one of: UPGRADED_TO_SUCCESS / RETRY_ALSO_FAILED / CRASHED.
    Without the resolution field, recovery_rate can't be computed."""
    import inspect
    from ma_poc.scripts.runners import jugnu

    src = inspect.getsource(jugnu)
    assert "WEDGE_RESCUE_RETRY_RESOLVED" in src
    # All three resolution literals must appear so every retry-result
    # branch emits with a meaningful classification.
    assert '"UPGRADED_TO_SUCCESS"' in src or "'UPGRADED_TO_SUCCESS'" in src
    assert '"RETRY_ALSO_FAILED"' in src or "'RETRY_ALSO_FAILED'" in src
    assert '"CRASHED"' in src or "'CRASHED'" in src


def test_runner_no_longer_piggybacks_on_property_emitted() -> None:
    """The old workaround used PROPERTY_EMITTED with verdict=
    'WEDGE_RESCUE_RETRY_STARTED' as a string — that's now removed in
    favour of the dedicated EventKinds. Guard against accidental
    re-introduction (analyzers were starting to count rescue-attempts
    twice when both code paths fired)."""
    import inspect
    from ma_poc.scripts.runners import jugnu

    src = inspect.getsource(jugnu)
    # The old piggyback shape was a verdict-string assignment within a
    # PROPERTY_EMITTED emit call. Ensure that exact shape is gone.
    # (We allow the literal string elsewhere — e.g. test names — but
    # not paired with PROPERTY_EMITTED in an emit call.)
    import re
    # Match an `_emit(_EK.PROPERTY_EMITTED ... verdict="WEDGE_RESCUE_RETRY_STARTED"`
    # construct, with arbitrary whitespace/newlines in between.
    piggyback_re = re.search(
        r"_emit\(\s*_EK\.PROPERTY_EMITTED[\s\S]{0,400}?"
        r"verdict\s*=\s*['\"]WEDGE_RESCUE_RETRY_STARTED['\"]",
        src,
    )
    assert piggyback_re is None, (
        "Found legacy piggyback shape: PROPERTY_EMITTED with "
        "verdict='WEDGE_RESCUE_RETRY_STARTED'. Use the dedicated "
        "WEDGE_RESCUE_RETRY_STARTED EventKind instead."
    )
