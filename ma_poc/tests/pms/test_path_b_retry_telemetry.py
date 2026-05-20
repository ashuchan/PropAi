"""Path B Piece 3a — empty-exit retry telemetry.

The hook lives in ``ma_poc.pms.scraper.scrape_jugnu`` right after the
first adapter dispatch. When the adapter returns an empty-exit label
AND produces no units, the hook emits a ``RETRY_WOULD_DISPATCH`` event
with the would-be next PMS candidate from ``detect_pms_candidates``.

These tests verify the *event-emission contract* in isolation by
exercising the same code path the scraper uses (``is_empty_exit`` +
``detect_pms_candidates`` + ``emit``) without spinning up a Playwright
session or the rest of ``scrape_jugnu``. The scrape-time integration
gets a smaller end-to-end pass via the existing test_scraper suite.
"""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.observability import events as _events_mod
from ma_poc.observability.events import Event, EventKind
from ma_poc.pms.detector import detect_pms_candidates
from ma_poc.pms.empty_exit import empty_exit_reason, is_empty_exit


class _CapturedEvents:
    """Collects every ``emit()`` call so tests can assert on the payload
    without spinning up the real EventLedger (which writes to disk)."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, kind: EventKind, property_id: str, **data: Any) -> Event:
        ev = Event(kind=kind, property_id=property_id, data=data, run_id="")
        self.events.append(ev)
        return ev

    def of_kind(self, kind: EventKind) -> list[Event]:
        return [e for e in self.events if e.kind == kind]


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> _CapturedEvents:
    """Swap ``ma_poc.observability.events.emit`` for a capturing stub.

    Reaches into the events module namespace so the bound name used by
    callers inside this test file (``from ma_poc.observability.events
    import emit``) sees the patched version. Tests in this file use
    the helper's own ``emit`` re-import below.
    """
    cap = _CapturedEvents()
    monkeypatch.setattr(_events_mod, "emit", cap)
    return cap


# ─────────────────────────────────────────────────────────────────────
# Section 1 — the predicate that triggers the telemetry.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tier_used,has_units,should_trigger",
    [
        # Empty-exit + no units → triggers
        ("TIER_1_API_G5_EMPTY", False, True),
        ("NOT_ENCORESKYLINE_TEMPLATE", False, True),
        ("TIER_1_API_SIGHTMAP_SHAPE_REJECTED", False, True),
        # Empty-exit + units present → does NOT trigger (adapter recovered)
        ("TIER_1_API_G5_EMPTY", True, False),
        # Success label + no units → does NOT trigger (adapter declared success)
        ("TIER_1_API_KNOCK", False, False),
        # LLM-tier outcomes never retried
        ("TIER_4_LLM_DOM_EMPTY", False, False),
        # No tier_used at all → no signal to retry from
        (None, False, False),
        ("", False, False),
    ],
)
def test_predicate_for_retry_would_dispatch_emission(
    tier_used: str | None, has_units: bool, should_trigger: bool
) -> None:
    """The exact predicate the scraper uses to decide whether to emit
    ``RETRY_WOULD_DISPATCH``."""
    fake_units = [{"unit_id": "1"}] if has_units else []
    actual = is_empty_exit(tier_used) and not fake_units
    assert actual is should_trigger, (
        f"predicate(tier={tier_used!r}, units={has_units}) = {actual!r}, "
        f"expected {should_trigger!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# Section 2 — full event-shape contract.
# ─────────────────────────────────────────────────────────────────────


def _emit_retry_telemetry(
    *,
    adapter_name: str,
    tier_used: str | None,
    units: list,
    url: str,
    page_html: str | None,
    property_id: str,
) -> None:
    """Mirror the exact hook in scraper.py. Kept in sync via
    ``test_scraper_hook_kept_in_sync_with_test_helper`` below.

    Calls ``_events_mod.emit`` via the module reference so the test
    fixture's monkeypatch is honored (a ``from … import emit`` import
    would have bound the original function name at import time)."""
    if not (is_empty_exit(tier_used) and not units):
        return
    candidates = detect_pms_candidates(
        url=url,
        csv_row=None,
        page_html=page_html,
        exclude={adapter_name},
        max_candidates=2,
    )
    if not candidates:
        return
    _events_mod.emit(
        EventKind.RETRY_WOULD_DISPATCH,
        property_id=property_id,
        previous_pms=adapter_name,
        previous_tier=tier_used or "",
        empty_exit_reason=empty_exit_reason(tier_used) or "",
        next_pms=candidates[0].pms,
        next_confidence=candidates[0].confidence,
        remaining_candidates=len(candidates),
    )


def test_emits_on_g5_empty_with_knock_marker(captured: _CapturedEvents) -> None:
    """The Flatiron / Alta / griffis pattern — G5 adapter returns
    TIER_1_API_G5_EMPTY, page has Knock markers, telemetry would route
    to knock."""
    html = (
        "<html><body>"
        '<script src="https://themes.g5dxm.com/themes/g5-c-acme/main.js"></script>'
        '<script>knockDoorway.init("a8e311e98aee0ee4545fea9e01b06ac6",'
        '"community","69e936e6567a11ef");</script>'
        "</body></html>"
    )
    _emit_retry_telemetry(
        adapter_name="g5",
        tier_used="TIER_1_API_G5_EMPTY",
        units=[],
        url="https://example.com/",
        page_html=html,
        property_id="P-test-001",
    )
    matched = captured.of_kind(EventKind.RETRY_WOULD_DISPATCH)
    assert len(matched) == 1, (
        f"expected exactly 1 RETRY_WOULD_DISPATCH event, got {len(matched)}"
    )
    payload = matched[0].data
    assert payload["previous_pms"] == "g5"
    assert payload["previous_tier"] == "TIER_1_API_G5_EMPTY"
    assert payload["empty_exit_reason"] == "_EMPTY"
    assert payload["next_pms"] == "knock", (
        f"expected next_pms=knock (Doorway widget present), got {payload['next_pms']!r}"
    )
    assert payload["next_confidence"] > 0
    assert payload["remaining_candidates"] >= 1


def test_emits_on_sightmap_shape_rejected_with_co_resident_pms(
    captured: _CapturedEvents,
) -> None:
    """SightMap returns SHAPE_REJECTED on a page with a Knock widget —
    telemetry says retry would try knock."""
    html = (
        "<html><body>"
        '<iframe src="https://sightmap.com/embed/abc123xyz"></iframe>'
        '<script src="https://doorway.knck.io/latest/doorway.min.js"></script>'
        '<script>knockDoorway.init("a8e311e98aee0ee4545fea9e01b06ac6",'
        '"community","69e936e6567a11ef");</script>'
        "</body></html>"
    )
    _emit_retry_telemetry(
        adapter_name="sightmap",
        tier_used="TIER_1_API_SIGHTMAP_SHAPE_REJECTED",
        units=[],
        url="https://example.com/",
        page_html=html,
        property_id="P-test-002",
    )
    matched = captured.of_kind(EventKind.RETRY_WOULD_DISPATCH)
    assert len(matched) == 1
    payload = matched[0].data
    assert payload["previous_pms"] == "sightmap"
    assert payload["empty_exit_reason"] == "_SHAPE_REJECTED"
    assert payload["next_pms"] == "knock"


def test_does_not_emit_when_units_present(captured: _CapturedEvents) -> None:
    """Adapter returned empty-exit label but ALSO returned units — the
    SightMap _AMENITIES_ONLY partial case where the join recovered
    *some* records. No retry telemetry."""
    html = (
        "<html><body>"
        '<iframe src="https://sightmap.com/embed/abc123xyz"></iframe>'
        "</body></html>"
    )
    _emit_retry_telemetry(
        adapter_name="sightmap",
        tier_used="TIER_1_API_SIGHTMAP_AMENITIES_ONLY",
        units=[{"unit_id": "1", "asking_rent": 1500}],  # one unit recovered
        url="https://example.com/",
        page_html=html,
        property_id="P-test-003",
    )
    matched = captured.of_kind(EventKind.RETRY_WOULD_DISPATCH)
    assert matched == [], (
        f"should NOT emit when units were extracted; got {matched!r}"
    )


def test_does_not_emit_on_success_label(captured: _CapturedEvents) -> None:
    """Bare success label (TIER_1_API_KNOCK) — never emits even if units
    list is empty (could be a no-units-available property, not an
    adapter failure)."""
    html = '<script>knockDoorway.init("a","community","b");</script>'
    _emit_retry_telemetry(
        adapter_name="knock",
        tier_used="TIER_1_API_KNOCK",
        units=[],
        url="https://example.com/",
        page_html=html,
        property_id="P-test-004",
    )
    matched = captured.of_kind(EventKind.RETRY_WOULD_DISPATCH)
    assert matched == []


def test_does_not_emit_when_no_next_candidate(captured: _CapturedEvents) -> None:
    """G5 returns empty but page has ONLY G5 markers — no co-resident
    PMS to retry with → telemetry stays silent (this is a property
    needing LLM rescue or a real adapter fix, not a retry candidate)."""
    html = (
        "<html><body>"
        '<script src="https://themes.g5dxm.com/themes/g5-c-acme/main.js"></script>'
        "</body></html>"
    )
    _emit_retry_telemetry(
        adapter_name="g5",
        tier_used="TIER_1_API_G5_EMPTY",
        units=[],
        url="https://example.com/",
        page_html=html,
        property_id="P-test-005",
    )
    matched = captured.of_kind(EventKind.RETRY_WOULD_DISPATCH)
    assert matched == [], (
        f"no co-resident PMS → no retry candidate → no event; got {matched!r}"
    )


def test_does_not_emit_on_llm_tier_failure(captured: _CapturedEvents) -> None:
    """LLM tier failures never trigger retry — LLM is the last-resort
    tier itself, no escalation target."""
    html = (
        "<html><body>"
        '<iframe src="https://sightmap.com/embed/abc123xyz"></iframe>'
        "</body></html>"
    )
    _emit_retry_telemetry(
        adapter_name="generic",
        tier_used="TIER_4_LLM_DOM_EMPTY",
        units=[],
        url="https://example.com/",
        page_html=html,
        property_id="P-test-006",
    )
    matched = captured.of_kind(EventKind.RETRY_WOULD_DISPATCH)
    assert matched == []


# ─────────────────────────────────────────────────────────────────────
# Section 3 — keep the test helper in sync with the scraper hook.
# This is a *cheap* check that catches drift: if the hook in
# scraper.py changes but the test helper doesn't, this fails and forces
# the test author to look.
# ─────────────────────────────────────────────────────────────────────


def test_scraper_hook_kept_in_sync_with_test_helper() -> None:
    """Reads the actual scraper.py source and asserts the hook still
    uses the four primitives this test helper uses. Doesn't compare
    line-by-line — just that the hook still calls each of:
      - is_empty_exit
      - detect_pms_candidates
      - emit(RETRY_WOULD_DISPATCH, ...)
      - empty_exit_reason
    """
    from pathlib import Path

    scraper_src = (
        Path(__file__).resolve().parents[2] / "pms" / "scraper.py"
    ).read_text(encoding="utf-8")
    for symbol in (
        "is_empty_exit",
        "detect_pms_candidates",
        "RETRY_WOULD_DISPATCH",
        "empty_exit_reason",
    ):
        assert symbol in scraper_src, (
            f"Path B telemetry hook in scraper.py no longer references "
            f"{symbol!r} — test helper and hook are now out of sync; "
            f"update one or the other."
        )
