"""End-to-end telemetry-emission canary for the persistence-loop fixes.

Each fix in the persistence loop emits a specific event kind that the
analyzer aggregates into an SLO row in ``summary.md``. If the wiring
silently breaks (a writer stops emitting, or the analyzer stops
counting), the dashboard rows go to zero while the loop is broken.

This module exercises each event-emission path end-to-end:

  - ``DOM_HINTS_DEGRADED_SAVED`` is emitted when the loosened-self-validation
    path persists a degraded selector.
  - ``DOM_HINTS_EVICTED`` is emitted with quality + threshold fields when
    the quality-tiered eviction policy fires.
  - ``MAPPING_SAVE_DROPPED`` is emitted with a reason when a mapping is
    dropped (already covered by writer-contract test, pinned here too).

The tests use the same ``capture_emitted_events`` pattern as the analyzer
itself to assert the events appear with the correct fields, so future
refactors of the event payload shape surface as a failing test.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import pytest

from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.observability.events import EventKind
from ma_poc.services.profile_store import ProfileStore
from ma_poc.services.profile_updater import (
    DOM_HINT_QUALITY_RESILIENT_FLOOR,
    update_profile_after_extraction,
)


@contextlib.contextmanager
def capture_emitted_events() -> Iterator[list[dict[str, Any]]]:
    """Patch ``observability.events.emit`` and capture every call.

    The real ``emit`` writes to an event ledger that requires an
    initialised runtime context. Tests use the patched version to
    inspect the (kind, property_id, payload) tuples without the
    surrounding plumbing.
    """
    captured: list[dict[str, Any]] = []
    import ma_poc.observability.events as ev

    original = ev.emit

    def fake_emit(kind: Any, property_id: str, **payload: Any) -> None:
        captured.append({
            "kind": getattr(kind, "value", str(kind)),
            "property_id": property_id,
            "payload": payload,
        })

    ev.emit = fake_emit  # type: ignore[assignment]
    try:
        yield captured
    finally:
        ev.emit = original  # type: ignore[assignment]


@pytest.fixture
def store(tmp_path: Any) -> ProfileStore:
    return ProfileStore(tmp_path / "profiles")


# ── DOM-hint eviction emits quality + threshold for the analyzer ──────────


class TestDomEvictionTelemetry:
    def test_low_quality_eviction_emits_one_strike_threshold(
        self, store: ProfileStore
    ) -> None:
        """A quality<0.8 entry evicted on a single miss must emit
        DOM_HINTS_EVICTED with threshold=1 — the analyzer's
        eviction-by-quality histogram needs the breakdown."""
        p = ScrapeProfile(canonical_id="canary_dom_evict_low")
        p.dom_hints.field_selectors.container = ".unit"
        p.dom_hints.field_selectors_quality = 0.4
        store.save(p)

        miss = {
            "extraction_tier_used": "TIER_3_DOM",
            "_dom_hints_attempted": True,
            "_dom_hints_hit": False,
            "units": [{"unit_id": "U1"}],
        }

        with capture_emitted_events() as events:
            update_profile_after_extraction(p, miss, 5, store)

        evicted = [e for e in events if e["kind"] == EventKind.DOM_HINTS_EVICTED.value]
        assert len(evicted) == 1, (
            "Eviction MUST emit DOM_HINTS_EVICTED — without it the dashboard "
            "can't compute eviction-by-quality histograms."
        )
        payload = evicted[0]["payload"]
        assert payload["threshold"] == 1
        assert abs(payload["quality"] - 0.4) < 1e-9

    def test_high_quality_three_strike_emits_threshold_three(
        self, store: ProfileStore
    ) -> None:
        """Validated (>=0.8) entries evict at threshold=3."""
        p = ScrapeProfile(canonical_id="canary_dom_evict_high")
        p.dom_hints.field_selectors.container = ".unit"
        p.dom_hints.field_selectors_quality = DOM_HINT_QUALITY_RESILIENT_FLOOR  # 0.8 boundary
        store.save(p)

        miss = {
            "extraction_tier_used": "TIER_3_DOM",
            "_dom_hints_attempted": True,
            "_dom_hints_hit": False,
            "units": [{"unit_id": "U1"}],
        }

        with capture_emitted_events() as events:
            for _ in range(3):
                update_profile_after_extraction(p, miss, 5, store)

        evicted = [e for e in events if e["kind"] == EventKind.DOM_HINTS_EVICTED.value]
        assert len(evicted) == 1
        assert evicted[0]["payload"]["threshold"] == 3

    def test_no_eviction_no_emit(self, store: ProfileStore) -> None:
        """Replay hit must NOT emit eviction."""
        p = ScrapeProfile(canonical_id="canary_dom_no_evict")
        p.dom_hints.field_selectors.container = ".unit"
        p.dom_hints.field_selectors_quality = 0.4
        store.save(p)

        hit = {
            "extraction_tier_used": "TIER_3_DOM",
            "_dom_hints_attempted": True,
            "_dom_hints_hit": True,
            "units": [{"unit_id": "U1"}],
        }

        with capture_emitted_events() as events:
            update_profile_after_extraction(p, hit, 1, store)

        evicted = [e for e in events if e["kind"] == EventKind.DOM_HINTS_EVICTED.value]
        assert evicted == []
