"""EventSpy — captures observability.events.emit() calls.

The spy is injected via the ``event_spy`` pytest fixture in conftest.py,
which monkeypatches ``ma_poc.observability.events.emit`` for the duration
of each test.

Usage::

    # In test:
    # ... run some pipeline code ...
    spy_calls = event_spy.calls
    tier_won = event_spy.all_of_kind(EventKind.TIER_WON)
    assert len(tier_won) == 1
    assert tier_won[0]["data"]["tier"] == "TIER_1_API"

    event_spy.assert_emitted(EventKind.PMS_DETECTED, property_id="prop-001")
"""

from __future__ import annotations

from typing import Any

from ma_poc.observability.events import EventKind


class EventSpy:
    """Captures (kind, property_id, kwargs) tuples emitted via emit().

    Limitation: only captures calls that reach ``ma_poc.observability.events.emit``
    via a lazy import (``from ma_poc.observability.events import emit`` inside a
    function body). Module-level eager imports cache the original function object
    and will bypass the spy. All current production callers use lazy imports, so
    this is safe today — keep this constraint in mind when adding new callers.
    """

    def __init__(self) -> None:
        self._calls: list[tuple[EventKind, str, dict[str, Any]]] = []

    def record(self, kind: EventKind, property_id: str, **kwargs: Any) -> None:
        """Called in place of the real emit(). Never raises."""
        self._calls.append((kind, property_id, dict(kwargs)))

    def reset(self) -> None:
        self._calls.clear()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def calls(self) -> list[tuple[EventKind, str, dict[str, Any]]]:
        """All recorded (kind, property_id, kwargs) in emission order."""
        return list(self._calls)

    def kinds(self) -> list[EventKind]:
        return [kind for kind, _, _ in self._calls]

    def all_of_kind(self, kind: EventKind) -> list[dict[str, Any]]:
        """Return kwargs dicts for every emit of *kind*."""
        return [kw for k, _pid, kw in self._calls if k == kind]

    def first_of_kind(self, kind: EventKind) -> dict[str, Any] | None:
        """Return the kwargs of the first matching emit, or None."""
        for k, _pid, kw in self._calls:
            if k == kind:
                return kw
        return None

    def count_of_kind(self, kind: EventKind) -> int:
        return sum(1 for k, _, _ in self._calls if k == kind)

    # ------------------------------------------------------------------
    # Assertion helpers
    # ------------------------------------------------------------------

    def assert_emitted(
        self,
        kind: EventKind,
        *,
        property_id: str | None = None,
        **expected_kwargs: Any,
    ) -> None:
        """Assert that *kind* was emitted, optionally matching property_id
        and any subset of kwargs.

        Raises AssertionError with a detailed message on mismatch.
        """
        candidates = [
            (pid, kw)
            for k, pid, kw in self._calls
            if k == kind
        ]
        assert candidates, (
            f"EventKind.{kind.name} was never emitted. "
            f"Kinds seen: {[k.name for k, _, _ in self._calls]}"
        )
        for pid, kw in candidates:
            if property_id is not None and pid != property_id:
                continue
            if all(kw.get(key) == val for key, val in expected_kwargs.items()):
                return  # found a match
        raise AssertionError(
            f"EventKind.{kind.name} was emitted {len(candidates)} time(s) "
            f"but none matched property_id={property_id!r} "
            f"and kwargs {expected_kwargs!r}. "
            f"Actual calls: {candidates}"
        )

    def assert_not_emitted(self, kind: EventKind) -> None:
        matches = [k for k, _, _ in self._calls if k == kind]
        assert not matches, (
            f"EventKind.{kind.name} was emitted {len(matches)} time(s) "
            f"but was expected NOT to be emitted."
        )
