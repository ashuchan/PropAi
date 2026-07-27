"""Path B Pieces 3a + 3b — empty-exit retry telemetry and re-dispatch.

The hook lives in ``ma_poc.pms.scraper`` right after the first adapter
dispatch. Two modes:

  - Piece 3a (telemetry-only, ``PATH_B_RETRY_ENABLED=0``): emits a
    single ``RETRY_WOULD_DISPATCH`` event when the adapter returns an
    empty-exit label AND produces no units; does NOT actually retry.
  - Piece 3b (default): emits ``RETRY_DISPATCHED`` per attempt and
    ``RETRY_SUCCESS`` when an attempt recovers units; re-dispatches
    on the same page using the next PMS from ``detect_pms_candidates``.
    Bounded by ``PATH_B_MAX_RETRIES`` (default 2).

These tests verify the contract in isolation by exercising the same
primitives the scraper uses (``is_empty_exit`` + ``detect_pms_candidates``
+ ``emit`` + ``get_adapter``) plus a re-implementation of the retry
loop logic against mocked adapters. The scraper hook itself is checked
for drift via the source-grep contract test.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from dataclasses import field as _dc_field
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
    would have bound the original function name at import time).

    Trigger conditions (combined Path B + Path C):
      * empty_exit: adapter self-reported empty-exit AND no units
      * quality_gate: adapter returned units but they all failed
        ``property_passes_quality_gate``
    """
    from ma_poc.validation.schema_gate import property_passes_quality_gate

    if is_empty_exit(tier_used) and not units:
        trigger_reason = "empty_exit"
    elif units and not property_passes_quality_gate(units):
        trigger_reason = "quality_gate"
    else:
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
        trigger_reason=trigger_reason,
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


def test_does_not_emit_when_substantive_units_present(
    captured: _CapturedEvents,
) -> None:
    """Adapter returned empty-exit label but ALSO returned substantive
    units (rent + a physical dimension) — the SightMap _AMENITIES_ONLY
    partial case where the join recovered *some* records. No retry
    telemetry: partial recovery beats no recovery."""
    html = (
        "<html><body>"
        '<iframe src="https://sightmap.com/embed/abc123xyz"></iframe>'
        "</body></html>"
    )
    _emit_retry_telemetry(
        adapter_name="sightmap",
        tier_used="TIER_1_API_SIGHTMAP_AMENITIES_ONLY",
        # Substantive unit — has rent + beds, passes quality gate.
        units=[{"unit_id": "1", "asking_rent": 1500, "beds": 1}],
        url="https://example.com/",
        page_html=html,
        property_id="P-test-003",
    )
    matched = captured.of_kind(EventKind.RETRY_WOULD_DISPATCH)
    assert matched == [], (
        f"should NOT emit when substantive units were extracted; got {matched!r}"
    )


def test_emits_on_hollow_units_path_c(captured: _CapturedEvents) -> None:
    """Path C trigger: adapter returned units, but they all fail the
    quality gate (no physical dimension — floorplan-name-only rows or
    rent-only rows). Retry telemetry fires with
    ``trigger_reason='quality_gate'``."""
    html = (
        "<html><body>"
        '<script src="https://themes.g5dxm.com/themes/g5-c-acme/main.js"></script>'
        '<script>knockDoorway.init("a8e311e98aee0ee4545fea9e01b06ac6",'
        '"community","69e936e6567a11ef");</script>'
        "</body></html>"
    )
    _emit_retry_telemetry(
        adapter_name="g5",
        tier_used="TIER_1_API_G5",  # bare success label — adapter claimed success
        # Hollow units: rent + plan-name, no physical dimension.
        # The classic "JSONLD-ALL-fail" / inferred_id shape.
        units=[
            {"unit_id": "g5-1", "asking_rent": 1500},
            {"unit_id": "g5-2", "asking_rent": 1800},
        ],
        url="https://example.com/",
        page_html=html,
        property_id="P-test-pathc",
    )
    matched = captured.of_kind(EventKind.RETRY_WOULD_DISPATCH)
    assert len(matched) == 1, (
        f"Path C should emit 1 RETRY_WOULD_DISPATCH on hollow units; "
        f"got {len(matched)}"
    )
    payload = matched[0].data
    assert payload["trigger_reason"] == "quality_gate"
    assert payload["previous_pms"] == "g5"
    assert payload["next_pms"] == "knock"


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
# Section 3 — a REMINDER that the helper mirrors the scraper hook.
#
# NOT a drift contract. This is a substring grep over scraper.py source; it
# is blind to behaviour and it has a proven miss. When the 2026-07-25
# ``plan_level_only`` trigger was added to production and not to the mirror,
# the grep stayed green because the symbol still appeared elsewhere in the
# file — and the trigger the 2026-07-26 post-mortem was trying to measure
# went untested in the mirror. Two later mutations (rewriting production's
# ``lost_*`` assignments; deleting the abort classifier) were likewise pure
# production/mirror divergences that this grep did not notice.
#
# The load-bearing check is
# ``tests/pms/test_retry_episode_setup_failure.py::
# test_mirror_matches_production_payload`` — same input to both
# implementations, same RETRY_EPISODE payload out, divergence is a failure.
# Keep this one as a cheap "go look" nudge when a symbol disappears
# entirely; do not rely on it to catch drift.
# ─────────────────────────────────────────────────────────────────────


def test_scraper_hook_still_mentions_the_primitives_this_helper_uses() -> None:
    """Greps scraper.py for the symbols the mirror is built on.

    A REMINDER, not a contract: presence of a symbol anywhere in the file —
    including inside a comment or the outcome frozenset — satisfies it. It
    catches wholesale removal and nothing subtler. Behavioural parity is
    asserted in tests/pms/test_retry_episode_setup_failure.py.
    """
    from pathlib import Path

    scraper_src = (
        Path(__file__).resolve().parents[2] / "pms" / "scraper.py"
    ).read_text(encoding="utf-8")
    for symbol in (
        "is_empty_exit",
        "detect_pms_candidates",
        "RETRY_WOULD_DISPATCH",
        "RETRY_DISPATCHED",
        "RETRY_SUCCESS",
        "PATH_B_RETRY_ENABLED",
        "PATH_B_MAX_RETRIES",
        "empty_exit_reason",
        # Path C: quality-gate trigger uses property_passes_quality_gate.
        "property_passes_quality_gate",
        "trigger_reason",
        '"quality_gate"',
        '"empty_exit"',
        # Path C extension (2026-05-20): rent + area signal predicates,
        # the no_rent / no_area triggers, and the plan-level fallback.
        "property_has_rent_signal",
        "property_has_area_signal",
        '"no_rent"',
        '"no_area"',
        "_PLAN_LEVEL",
        "SUCCESS_PLAN_LEVEL",
        "_plan_level_reason",
        # 2026-07-25 plan-level trigger. It was NOT pinned here, so the test
        # helper silently lost it — this grep missed a real drift in exactly
        # the trigger the 2026-07-26 post-mortem was trying to measure.
        '"plan_level_only"',
        "rows_are_plan_level",
        "_retry_win_condition_for",
        # 2026-07-26 closed-funnel telemetry.
        "RETRY_EPISODE",
        "RETRY_EPISODE_OUTCOMES",
        "episode_id",
        "initial_trigger_reason",
        "baseline_restored",
        "candidates_offered",
        # 2026-07-26 — the trigger predicate's own crash is its OWN outcome,
        # split out of setup_error (which pages run-wide).
        '"trigger_error"',
        "_ep_in_trigger_eval",
    ):
        assert symbol in scraper_src, (
            f"Path B retry hook in scraper.py no longer references "
            f"{symbol!r} — test helper and hook are now out of sync; "
            f"update one or the other."
        )


def test_retry_episode_outcome_vocabulary_is_declared_once() -> None:
    """``RETRY_EPISODE_OUTCOMES`` is the single source of truth for the
    terminal-outcome vocabulary, and it may not silently grow or shrink.

    Aggregators partition on this set, so an unrecognised value must be a
    loud failure rather than a silent drop.

    This test used to ALSO assert that each member appears as a quoted
    literal in scraper.py, with a message claiming it caught outcomes that
    nothing assigns. It could not: the frozenset it iterates declares those
    very literals in that same file, so the grep was satisfied by its own
    source. Verified — under a mutation where four ``lost_*`` outcomes were
    assigned from nowhere, that half still passed. Reachability is now
    proven by driving the REAL ``scrape()``, in
    ``tests/pms/test_retry_episode_setup_failure.py::
    test_every_declared_outcome_is_reachable_from_production``.
    """
    from ma_poc.pms.scraper import RETRY_EPISODE_OUTCOMES

    expected = {
        "not_triggered",
        "no_budget",
        "no_candidate",
        "telemetry_only",
        "won",
        "lost_candidates_exhausted",
        "lost_adapter_error",
        "lost_dead_end",
        "lost_max_retries",
        "aborted_error",
        "aborted_cancelled",
        "trigger_error",
        "setup_error",
    }
    assert len(RETRY_EPISODE_OUTCOMES) == 13
    assert set(RETRY_EPISODE_OUTCOMES) == expected


# ─────────────────────────────────────────────────────────────────────
# Section 6 — Piece 3b retry-loop logic (mocked adapters, no scraper).
#
# Re-implements the production retry loop in a testable shape so we can
# exercise: max-retries cap, win-on-first-retry, win-on-second-retry,
# all-retries-fail, no-candidate, telemetry-only mode (3a). Mocks the
# adapter dispatch via a per-PMS preset map; the loop calls
# ``get_adapter(pms)`` via a dependency-injected callable so tests can
# substitute a stub.
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _StubAdapterResult:
    """Minimal stand-in for ``ma_poc.pms.adapters.base.AdapterResult``.

    The retry loop only reads ``tier_used`` and ``units`` so the stub
    only carries those two fields."""
    tier_used: str | None = None
    units: list = _dc_field(default_factory=list)


@dataclass
class _StubAdapter:
    """Stub adapter whose ``extract()`` returns a preset result."""
    pms_name: str
    preset: _StubAdapterResult

    async def extract(self, page, ctx):  # noqa: ARG002 — page/ctx unused
        return self.preset


@dataclass
class _Ctx:
    """Minimal stand-in for ``AdapterContext`` covering the retry path."""
    base_url: str
    property_id: str
    detected: object = None


async def _run_retry_loop_under_test(
    *,
    initial_adapter_name: str,
    initial_result: _StubAdapterResult,
    page_html: str | None,
    ctx: _Ctx,
    adapter_table: dict[str, _StubAdapter],
    enabled: bool = True,
    max_retries: int = 2,
) -> tuple[str, _StubAdapterResult, list[str], dict[str, Any]]:
    """Mirror of the production retry loop body in
    ``ma_poc.pms.scraper`` (Path B/C). Returns
    (adapter_name, adapter_result, fallback_chain, result_dict).
    Kept in sync with the production hook via
    ``test_scraper_hook_kept_in_sync_with_test_helper``.

    The 4th return slot ``result_dict`` mirrors the scraper's ``result``
    dict — exposes ``_verdict_quality`` / ``_plan_level_reason`` keys
    so tests can assert the SUCCESS_PLAN_LEVEL fallback fires correctly.

    2026-07-26 — mirrors the CLOSED-FUNNEL telemetry too: exactly one
    ``RETRY_EPISODE`` per call, emitted from a ``finally`` so it covers
    the loop-condition falsifications and the cancellation path as well
    as the breaks. ``episode_id`` + ``initial_trigger_reason`` were added
    to the three pre-existing emits at the same time.
    """
    from ma_poc.observability import events as _events_mod
    from ma_poc.observability.events import EventKind
    from ma_poc.pms.detector import detect_pms_candidates
    from ma_poc.pms.empty_exit import empty_exit_reason, is_empty_exit

    # Imported, never redefined: tests/pms/test_universal_recovery_plan_level_
    # gate.py pins that exactly ONE definition of this predicate exists, and a
    # second copy of the plan-level rule is precisely the drift this repo keeps
    # paying for.
    from ma_poc.pms.scraper import rows_are_plan_level
    from ma_poc.validation.schema_gate import (
        property_has_area_signal,
        property_has_rent_signal,
        property_passes_quality_gate,
    )

    def _trigger(res: _StubAdapterResult) -> str | None:
        if is_empty_exit(res.tier_used) and not res.units:
            return "empty_exit"
        if res.units:
            if not property_passes_quality_gate(res.units):
                return "quality_gate"
            if not property_has_rent_signal(res.units):
                return "no_rent"
            if not property_has_area_signal(res.units):
                return "no_area"
            # 2026-07-25 trigger — was MISSING from this mirror, and the
            # drift-contract grep did not pin it, so the very trigger the
            # 2026-07-26 post-mortem asked about was untested here.
            if rows_are_plan_level(res.units):
                return "plan_level_only"
        return None

    def _win(res: _StubAdapterResult) -> bool:
        return bool(
            res.units
            and property_passes_quality_gate(res.units)
            and property_has_rent_signal(res.units)
        )

    def _win_for(res: _StubAdapterResult, trigger: str | None) -> bool:
        """Mirror of ``_retry_win_condition_for``: swapping one plan-level
        result for another is not a win."""
        if not _win(res):
            return False
        if trigger == "plan_level_only":
            return not rows_are_plan_level(res.units)
        return True

    result_dict: dict[str, Any] = {}
    adapter_result = initial_result
    adapter_name = initial_adapter_name
    baseline_result = initial_result if initial_result.units else None
    baseline_adapter_name = initial_adapter_name
    tried: set[str] = {adapter_name}
    fallback_chain: list[str] = []
    attempt = 0
    retry_won = False
    # Mirrors scraper.py: the trigger evaluation lives INSIDE the try, because
    # the predicates it calls do ``unit.get(...)`` and raise on a non-dict row.
    # Outside the try it produced no terminal event at all here, and in
    # production it was misreported as ``setup_error`` — the outcome that
    # means "retry is dead RUN-WIDE" and pages.
    trigger_reason: str | None = None
    initial_trigger_reason: str | None = None
    in_trigger_eval = False
    current_result = adapter_result

    # --- episode state (mirrors the ``_ep_*`` locals in scraper.py) ------
    episode_id = uuid.uuid4().hex[:16]
    ep_baseline_pms = initial_adapter_name
    ep_baseline_tier = initial_result.tier_used or ""
    ep_baseline_unit_count = len(initial_result.units or [])
    ep_baseline_error_count = 0  # _StubAdapterResult carries no errors list
    ep_baseline_plan_level = rows_are_plan_level(initial_result.units)
    ep_outcome = ""
    ep_error_type = ""
    ep_final_trigger_reason = ""
    ep_candidates_offered = -1
    ep_tried_pms: list[str] = []
    ep_tried_adapters: list[str] = []
    ep_won_pms = ""
    ep_won_tier = ""
    ep_won_unit_count = -1
    ep_baseline_restored = False
    prev_adapter_for_event = baseline_adapter_name

    try:
        in_trigger_eval = True
        trigger_reason = _trigger(adapter_result)
        initial_trigger_reason = trigger_reason
        ep_final_trigger_reason = trigger_reason or ""
        in_trigger_eval = False
        while trigger_reason is not None and attempt < max_retries:
            candidates = detect_pms_candidates(
                url=ctx.base_url,
                csv_row=None,
                page_html=page_html,
                exclude=tried,
                max_candidates=max_retries,
            )
            if ep_candidates_offered < 0:
                ep_candidates_offered = len(candidates)
            if not candidates:
                ep_outcome = (
                    "no_candidate" if attempt == 0 else "lost_candidates_exhausted"
                )
                break
            nc = candidates[0]
            previous_tier = current_result.tier_used or ""
            previous_pms = prev_adapter_for_event

            if not enabled:
                ep_outcome = "telemetry_only"
                _events_mod.emit(
                    EventKind.RETRY_WOULD_DISPATCH,
                    property_id=ctx.property_id,
                    episode_id=episode_id,
                    previous_pms=previous_pms,
                    previous_tier=previous_tier,
                    empty_exit_reason=empty_exit_reason(previous_tier) or "",
                    trigger_reason=trigger_reason,
                    next_pms=nc.pms,
                    next_confidence=nc.confidence,
                    remaining_candidates=len(candidates),
                )
                break

            attempt += 1
            _events_mod.emit(
                EventKind.RETRY_DISPATCHED,
                property_id=ctx.property_id,
                episode_id=episode_id,
                attempt=attempt,
                previous_pms=previous_pms,
                previous_tier=previous_tier,
                empty_exit_reason=empty_exit_reason(previous_tier) or "",
                trigger_reason=trigger_reason,
                initial_trigger_reason=initial_trigger_reason or "",
                next_pms=nc.pms,
                next_confidence=nc.confidence,
            )

            tried.add(nc.pms)
            ep_tried_pms.append(nc.pms)
            new_adapter = adapter_table.get(nc.pms)
            # Mirrors production's ``getattr(_new_adapter, "pms_name",
            # _next_cand.pms)`` — the registry can hand back a different
            # adapter than the candidate name suggests.
            ep_tried_adapters.append(getattr(new_adapter, "pms_name", nc.pms))
            if new_adapter is None:
                # Helper-only exit: production calls ``get_adapter``, which
                # falls back to ``generic`` rather than returning None, and
                # reaches this outcome via a KeyError instead.
                ep_outcome = "lost_adapter_error"
                ep_error_type = "NoAdapter"
                fallback_chain.append(f"retry_failed:{nc.pms}:NoAdapter")
                break
            try:
                new_result = await new_adapter.extract(None, ctx)
            except Exception as exc:
                ep_outcome = "lost_adapter_error"
                ep_error_type = type(exc).__name__
                fallback_chain.append(f"retry_failed:{nc.pms}:{type(exc).__name__}")
                break
            fallback_chain.append(f"retry:{nc.pms}")
            if _win_for(new_result, initial_trigger_reason):
                ep_outcome = "won"
                ep_won_pms = nc.pms
                ep_won_tier = new_result.tier_used or ""
                ep_won_unit_count = len(new_result.units)
                _events_mod.emit(
                    EventKind.RETRY_SUCCESS,
                    property_id=ctx.property_id,
                    episode_id=episode_id,
                    attempt=attempt,
                    previous_pms=previous_pms,
                    previous_tier=previous_tier,
                    trigger_reason=trigger_reason,
                    initial_trigger_reason=initial_trigger_reason or "",
                    won_pms=nc.pms,
                    won_tier=new_result.tier_used or "",
                    unit_count=len(new_result.units),
                )
                adapter_result = new_result
                adapter_name = nc.pms
                retry_won = True
                break
            current_result = new_result
            prev_adapter_for_event = nc.pms
            in_trigger_eval = True
            trigger_reason = _trigger(current_result)
            in_trigger_eval = False

        ep_final_trigger_reason = trigger_reason or ""
        if not ep_outcome:
            if initial_trigger_reason is None:
                ep_outcome = "not_triggered"
            elif max_retries <= 0:
                ep_outcome = "no_budget"
            elif attempt >= max_retries:
                ep_outcome = "lost_max_retries"
            else:
                ep_outcome = "lost_dead_end"

        # Plan-level fallback: all retries failed AND baseline had units AND
        # the initial trigger was a quality concern (not empty-exit).
        if (
            not retry_won
            and baseline_result is not None
            and baseline_result.units
            and initial_trigger_reason in {"quality_gate", "no_rent", "no_area"}
        ):
            adapter_result = baseline_result
            baseline_tier = baseline_result.tier_used or ""
            if baseline_tier and "_PLAN_LEVEL" not in baseline_tier:
                adapter_result.tier_used = f"{baseline_tier}_PLAN_LEVEL"
            result_dict["_verdict_quality"] = "SUCCESS_PLAN_LEVEL"
            result_dict["_plan_level_reason"] = initial_trigger_reason
            ep_baseline_restored = True
    except BaseException as exc:  # classify, do not catch
        if not isinstance(exc, Exception):
            ep_outcome = "aborted_cancelled"
        elif in_trigger_eval:
            # A predicate crash on THIS property's rows — not a bug in the
            # loop machinery (aborted_error) and not a run-wide setup failure
            # (setup_error). Mirrors scraper.py's three-way classifier.
            ep_outcome = "trigger_error"
        else:
            ep_outcome = "aborted_error"
        ep_error_type = type(exc).__name__
        ep_final_trigger_reason = trigger_reason or ""
        raise
    finally:
        _events_mod.emit(
            EventKind.RETRY_EPISODE,
            property_id=ctx.property_id,
            episode_id=episode_id,
            scrape_url=ctx.base_url,
            outcome=ep_outcome,
            trigger_reason=initial_trigger_reason or "",
            final_trigger_reason=ep_final_trigger_reason,
            attempts=attempt,
            candidates_offered=ep_candidates_offered,
            baseline_pms=ep_baseline_pms,
            baseline_tier=ep_baseline_tier,
            baseline_unit_count=ep_baseline_unit_count,
            baseline_error_count=ep_baseline_error_count,
            baseline_plan_level=ep_baseline_plan_level,
            tried_pms=list(ep_tried_pms),
            tried_adapters=list(ep_tried_adapters),
            won_pms=ep_won_pms,
            won_tier=ep_won_tier,
            won_unit_count=ep_won_unit_count,
            baseline_restored=ep_baseline_restored,
            error_type=ep_error_type,
            retry_enabled=enabled,
            max_retries=max_retries,
        )

    return adapter_name, adapter_result, fallback_chain, result_dict


# A page where G5 wins detection but Knock is also present — the
# Flatiron pattern. detect_pms_candidates returns ['knock', 'rentcafe']
# (G5 is gated out by the cluster-3 detector fix).
_HTML_KNOCK_THEN_RENTCAFE = (
    "<html><body>"
    '<script src="https://themes.g5dxm.com/themes/g5-c-acme/main.js"></script>'
    '<script src="https://doorway.knck.io/latest/doorway.min.js"></script>'
    '<script>knockDoorway.init("a8e311e98aee0ee4545fea9e01b06ac6",'
    '"community","69e936e6567a11ef");</script>'
    '<a href="https://lpc.securecafe.com/onlineleasing/x/availableunits.aspx">x</a>'
    "</body></html>"
)


@pytest.mark.asyncio
async def test_retry_succeeds_on_first_attempt(captured: _CapturedEvents) -> None:
    """G5 returns _EMPTY, retry picks Knock, Knock recovers units —
    emits RETRY_DISPATCHED + RETRY_SUCCESS, adapter_name becomes knock."""
    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY", units=[])
    knock_result = _StubAdapterResult(
        tier_used="TIER_1_API_KNOCK",
        # Real units need a physical dimension to pass property_passes_quality_gate
        # (post-Path-C win condition).
        units=[{"unit_id": "K1", "asking_rent": 1500, "beds": 1}],
    )
    table = {
        "knock": _StubAdapter("knock", knock_result),
        "rentcafe": _StubAdapter(
            "rentcafe",
            _StubAdapterResult(tier_used="TIER_1_API_RENTCAFE_SHAPE_REJECTED"),
        ),
    }
    name, result, chain, _result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-001"),
        adapter_table=table,
    )

    assert name == "knock"
    assert result.units == knock_result.units
    assert chain == ["retry:knock"]
    dispatched = captured.of_kind(EventKind.RETRY_DISPATCHED)
    success = captured.of_kind(EventKind.RETRY_SUCCESS)
    assert len(dispatched) == 1 and dispatched[0].data["attempt"] == 1
    assert dispatched[0].data["next_pms"] == "knock"
    assert len(success) == 1
    assert success[0].data["won_pms"] == "knock"
    assert success[0].data["unit_count"] == 1


@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt(captured: _CapturedEvents) -> None:
    """Knock also returns empty, retry escalates to rentcafe — emits
    2x RETRY_DISPATCHED and 1x RETRY_SUCCESS."""
    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY")
    table = {
        "knock": _StubAdapter(
            "knock", _StubAdapterResult(tier_used="TIER_1_API_KNOCK_EMPTY")
        ),
        "rentcafe": _StubAdapter(
            "rentcafe",
            _StubAdapterResult(
                tier_used="TIER_1_API_RENTCAFE",
                # Real units with physical dimension (post-Path-C win condition).
                units=[
                    {"unit_id": "RC1", "asking_rent": 1500, "beds": 1},
                    {"unit_id": "RC2", "asking_rent": 2200, "beds": 2},
                ],
            ),
        ),
    }
    name, result, chain, _result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-002"),
        adapter_table=table,
    )

    assert name == "rentcafe"
    assert len(result.units) == 2
    assert chain == ["retry:knock", "retry:rentcafe"]
    assert len(captured.of_kind(EventKind.RETRY_DISPATCHED)) == 2
    assert len(captured.of_kind(EventKind.RETRY_SUCCESS)) == 1
    assert captured.of_kind(EventKind.RETRY_SUCCESS)[0].data["won_pms"] == "rentcafe"


@pytest.mark.asyncio
async def test_retry_exhausts_all_candidates_without_recovery(
    captured: _CapturedEvents,
) -> None:
    """All retry attempts return empty — no RETRY_SUCCESS event,
    adapter_name stays at the LAST tried PMS."""
    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY")
    table = {
        "knock": _StubAdapter(
            "knock", _StubAdapterResult(tier_used="TIER_1_API_KNOCK_EMPTY")
        ),
        "rentcafe": _StubAdapter(
            "rentcafe",
            _StubAdapterResult(tier_used="TIER_1_API_RENTCAFE_SHAPE_REJECTED"),
        ),
    }
    name, result, chain, _result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-003"),
        adapter_table=table,
    )

    assert not result.units
    assert "retry:knock" in chain and "retry:rentcafe" in chain
    assert len(captured.of_kind(EventKind.RETRY_DISPATCHED)) == 2
    assert captured.of_kind(EventKind.RETRY_SUCCESS) == []


@pytest.mark.asyncio
async def test_retry_caps_at_max_retries(captured: _CapturedEvents) -> None:
    """``max_retries=1`` means at most ONE retry attempt, even with
    multiple candidates available."""
    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY")
    table = {
        "knock": _StubAdapter(
            "knock", _StubAdapterResult(tier_used="TIER_1_API_KNOCK_EMPTY")
        ),
        "rentcafe": _StubAdapter(
            "rentcafe",
            _StubAdapterResult(tier_used="TIER_1_API_RENTCAFE_EMPTY"),
        ),
    }
    await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-004"),
        adapter_table=table,
        max_retries=1,
    )
    # Exactly 1 dispatch attempt because max_retries=1
    assert len(captured.of_kind(EventKind.RETRY_DISPATCHED)) == 1


@pytest.mark.asyncio
async def test_retry_no_candidate_emits_terminal_episode(
    captured: _CapturedEvents,
) -> None:
    """Page has only G5 markers — no co-resident PMS, no candidates.
    detect_pms_candidates(exclude={'g5'}) returns [].

    THE ~37% BLIND SPOT. This test used to assert the silence — it was
    the bug written down as a guaranteed property of the system, and it
    is why the 1,127-property plan-cohort canary could not distinguish
    "the trigger never fired" from "it fired every time and dead-ended
    right here". The episode must now be COUNTED, with the runtime path
    still byte-identical (all three original silence assertions kept).
    """
    html_g5_only = (
        "<html><body>"
        '<script src="https://themes.g5dxm.com/themes/g5-c-acme/main.js"></script>'
        "</body></html>"
    )
    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY")
    name, _, chain, _result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=html_g5_only,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-005"),
        adapter_table={},
    )
    assert name == "g5"  # no change
    assert chain == []

    eps = captured.of_kind(EventKind.RETRY_EPISODE)
    assert len(eps) == 1
    d = eps[0].data
    assert d["outcome"] == "no_candidate"
    assert d["attempts"] == 0
    assert d["candidates_offered"] == 0
    assert d["trigger_reason"] == "empty_exit"
    assert d["baseline_pms"] == "g5"
    assert d["tried_pms"] == []
    assert d["won_pms"] == "" and d["won_unit_count"] == -1
    assert d["baseline_restored"] is False

    # Runtime path unchanged — the original silence assertions still hold.
    assert captured.of_kind(EventKind.RETRY_DISPATCHED) == []
    assert captured.of_kind(EventKind.RETRY_WOULD_DISPATCH) == []
    assert captured.of_kind(EventKind.RETRY_SUCCESS) == []


@pytest.mark.asyncio
async def test_retry_disabled_flag_falls_back_to_telemetry_only(
    captured: _CapturedEvents,
) -> None:
    """When ``enabled=False`` (env ``PATH_B_RETRY_ENABLED=0``), behavior
    matches Piece 3a: emit RETRY_WOULD_DISPATCH and stop. No actual
    retry happens even with candidates available."""
    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY")
    knock_result = _StubAdapterResult(
        tier_used="TIER_1_API_KNOCK",
        units=[{"unit_id": "K1"}],
    )
    table = {"knock": _StubAdapter("knock", knock_result)}

    name, result, chain, _result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-006"),
        adapter_table=table,
        enabled=False,
    )
    assert name == "g5"  # no actual dispatch happened
    assert not result.units
    assert chain == []
    assert captured.of_kind(EventKind.RETRY_WOULD_DISPATCH)
    assert captured.of_kind(EventKind.RETRY_DISPATCHED) == []
    assert captured.of_kind(EventKind.RETRY_SUCCESS) == []


@pytest.mark.asyncio
async def test_retry_does_not_fire_when_initial_succeeds(
    captured: _CapturedEvents,
) -> None:
    """First adapter returned substantive units — retry never enters the
    loop. Unit needs rent + a physical dimension + area to satisfy the
    full Path C predicate chain (quality_gate + no_rent + no_area)."""
    initial = _StubAdapterResult(
        tier_used="TIER_1_API_KNOCK",
        units=[{"unit_id": "K1", "asking_rent": 1500, "beds": 1, "sqft": 750}],
    )
    name, result, chain, _result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="knock",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-007"),
        adapter_table={},
    )
    assert name == "knock"
    assert result.units
    assert chain == []
    # The retry never engaged — but the episode is still counted, because a
    # self-contained denominator is what makes "zero events" mean "the hook
    # did not run" instead of "we cannot tell".
    eps = captured.of_kind(EventKind.RETRY_EPISODE)
    assert len(eps) == 1 and eps[0].data["outcome"] == "not_triggered"
    assert eps[0].data["candidates_offered"] == -1  # never looked
    assert [e for e in captured.events if e.kind is not EventKind.RETRY_EPISODE] == []


@pytest.mark.asyncio
async def test_retry_does_not_fire_on_success_label_with_no_units(
    captured: _CapturedEvents,
) -> None:
    """Bare success label (TIER_1_API_KNOCK) with empty units — could be
    a genuine no-availability property, not adapter failure. Retry must
    NOT fire."""
    initial = _StubAdapterResult(tier_used="TIER_1_API_KNOCK", units=[])
    name, result, chain, _result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="knock",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-008"),
        adapter_table={},
    )
    assert name == "knock"
    assert chain == []
    eps = captured.of_kind(EventKind.RETRY_EPISODE)
    assert len(eps) == 1 and eps[0].data["outcome"] == "not_triggered"
    assert [e for e in captured.events if e.kind is not EventKind.RETRY_EPISODE] == []


@pytest.mark.asyncio
async def test_retry_handles_adapter_exception_gracefully(
    captured: _CapturedEvents,
) -> None:
    """When the retry adapter's extract() raises, record on the fallback
    chain and stop — no RETRY_SUCCESS, but RETRY_DISPATCHED was already
    emitted before the call."""

    class _RaisingAdapter:
        pms_name = "knock"

        async def extract(self, page, ctx):  # noqa: ARG002
            raise RuntimeError("simulated knock crash")

    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY")
    table: dict[str, Any] = {"knock": _RaisingAdapter()}

    # The reimplemented loop catches the exception around the
    # extract() call and records it on the fallback chain.
    async def _loop_with_exception_handling():
        from ma_poc.observability import events as _events_mod
        from ma_poc.observability.events import EventKind
        from ma_poc.pms.detector import detect_pms_candidates
        from ma_poc.pms.empty_exit import empty_exit_reason, is_empty_exit

        adapter_result = initial
        adapter_name = "g5"
        tried = {adapter_name}
        fallback_chain: list[str] = []
        attempt = 0
        ctx = _Ctx(base_url="https://example.com/", property_id="P-009")
        while (
            is_empty_exit(adapter_result.tier_used)
            and not adapter_result.units
            and attempt < 2
        ):
            cands = detect_pms_candidates(
                url=ctx.base_url,
                csv_row=None,
                page_html=_HTML_KNOCK_THEN_RENTCAFE,
                exclude=tried,
                max_candidates=2,
            )
            if not cands:
                break
            nc = cands[0]
            attempt += 1
            _events_mod.emit(
                EventKind.RETRY_DISPATCHED,
                property_id=ctx.property_id,
                attempt=attempt,
                previous_pms=adapter_name,
                previous_tier=adapter_result.tier_used or "",
                empty_exit_reason=empty_exit_reason(adapter_result.tier_used)
                or "",
                next_pms=nc.pms,
                next_confidence=nc.confidence,
            )
            tried.add(nc.pms)
            try:
                _new_result = await table[nc.pms].extract(None, ctx)
            except Exception as e:
                fallback_chain.append(
                    f"retry_failed:{nc.pms}:{type(e).__name__}"
                )
                break
        return fallback_chain

    chain = await _loop_with_exception_handling()
    assert "retry_failed:knock:RuntimeError" in chain
    # RETRY_DISPATCHED was emitted before the crash
    assert len(captured.of_kind(EventKind.RETRY_DISPATCHED)) == 1
    # No success was emitted
    assert captured.of_kind(EventKind.RETRY_SUCCESS) == []


# ─────────────────────────────────────────────────────────────────────
# Section 7 — Path C: quality-gate retry trigger.
#
# Path B only triggers on adapter-self-reported empty exits. Path C
# extends that to "adapter returned units but they're all hollow"
# (no physical dimension — silent under-recovery). Uses the same
# retry mechanism with ``trigger_reason="quality_gate"``.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_path_c_retry_fires_on_hollow_units(
    captured: _CapturedEvents,
) -> None:
    """Adapter returns SUCCESS label + units, but units fail the
    quality gate (rent-only, no physical dimension). Path C re-dispatches
    with the next PMS; retry adapter returns substantive units; retry
    wins with trigger_reason='quality_gate'."""
    # Initial: G5 produced 2 hollow rows (rent only, no beds/baths/area).
    initial = _StubAdapterResult(
        tier_used="TIER_1_API_G5",  # success label, NOT empty-exit
        units=[
            {"unit_id": "g5-1", "asking_rent": 1500},
            {"unit_id": "g5-2", "asking_rent": 1800},
        ],
    )
    # Retry adapter (Knock) returns real units with beds.
    knock_result = _StubAdapterResult(
        tier_used="TIER_1_API_KNOCK",
        units=[{"unit_id": "K1", "asking_rent": 1500, "beds": 1}],
    )
    table = {"knock": _StubAdapter("knock", knock_result)}

    name, result, chain, _result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-pc-001"),
        adapter_table=table,
    )

    assert name == "knock", (
        f"Path C should promote the Knock retry winner; got {name!r}"
    )
    assert result.units == knock_result.units
    assert chain == ["retry:knock"]

    dispatched = captured.of_kind(EventKind.RETRY_DISPATCHED)
    success = captured.of_kind(EventKind.RETRY_SUCCESS)
    assert len(dispatched) == 1
    assert dispatched[0].data["trigger_reason"] == "quality_gate"
    assert dispatched[0].data["previous_pms"] == "g5"
    assert dispatched[0].data["previous_tier"] == "TIER_1_API_G5"
    assert len(success) == 1
    assert success[0].data["trigger_reason"] == "quality_gate"
    assert success[0].data["won_pms"] == "knock"


@pytest.mark.asyncio
async def test_path_c_retry_does_not_promote_more_hollow_units(
    captured: _CapturedEvents,
) -> None:
    """Win condition is units AND quality-gate pass. A retry that
    produces *more hollow* units is treated as a failed attempt; the
    loop continues. Path C must not silently promote slightly-better
    hollow output as if it were a real recovery."""
    initial = _StubAdapterResult(
        tier_used="TIER_1_API_G5",
        units=[{"unit_id": "g5-1", "asking_rent": 1500}],  # hollow
    )
    # Knock also returns hollow units. RentCafe returns real units.
    table = {
        "knock": _StubAdapter(
            "knock",
            _StubAdapterResult(
                tier_used="TIER_1_API_KNOCK",
                units=[{"unit_id": "K1", "asking_rent": 1200}],  # hollow
            ),
        ),
        "rentcafe": _StubAdapter(
            "rentcafe",
            _StubAdapterResult(
                tier_used="TIER_1_API_RENTCAFE",
                units=[{"unit_id": "RC1", "asking_rent": 1500, "beds": 1}],
            ),
        ),
    }
    name, result, chain, _result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-pc-002"),
        adapter_table=table,
    )

    assert name == "rentcafe", (
        f"Path C should keep retrying until the quality gate passes; "
        f"got {name!r}"
    )
    assert chain == ["retry:knock", "retry:rentcafe"]
    # Both dispatches carry the quality_gate trigger because each
    # successive attempt also failed the gate until rentcafe.
    dispatched = captured.of_kind(EventKind.RETRY_DISPATCHED)
    assert len(dispatched) == 2
    for ev in dispatched:
        assert ev.data["trigger_reason"] == "quality_gate"
    # Only the final attempt emitted RETRY_SUCCESS.
    success = captured.of_kind(EventKind.RETRY_SUCCESS)
    assert len(success) == 1
    assert success[0].data["won_pms"] == "rentcafe"


@pytest.mark.asyncio
async def test_path_c_no_retry_when_initial_passes_quality_gate(
    captured: _CapturedEvents,
) -> None:
    """When the initial adapter's units pass the quality gate, no retry
    fires even if there ARE co-resident PMS candidates. Path C must not
    keep escalating after a clean win."""
    initial = _StubAdapterResult(
        tier_used="TIER_1_API_G5",
        units=[
            # Full unit data: rent + beds + sqft → passes all Path C predicates.
            {"unit_id": "g5-1", "asking_rent": 1500, "beds": 1, "sqft": 750},
            {"unit_id": "g5-2", "asking_rent": 1800, "beds": 2, "sqft": 1100},
        ],
    )
    # Co-resident PMS available, but no retry should fire.
    table = {
        "knock": _StubAdapter(
            "knock",
            _StubAdapterResult(
                tier_used="TIER_1_API_KNOCK",
                units=[{"unit_id": "K1", "asking_rent": 1500, "beds": 1, "sqft": 750}],
            ),
        ),
    }
    name, result, chain, _result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-pc-003"),
        adapter_table=table,
    )

    assert name == "g5"  # original adapter, no promotion
    assert result.units == initial.units
    assert chain == []
    eps = captured.of_kind(EventKind.RETRY_EPISODE)
    assert len(eps) == 1 and eps[0].data["outcome"] == "not_triggered"
    assert [
        e for e in captured.events if e.kind is not EventKind.RETRY_EPISODE
    ] == [], (
        f"clean win on first dispatch must not trigger any retry activity; "
        f"got {captured.events!r}"
    )


@pytest.mark.asyncio
async def test_path_c_telemetry_only_mode_emits_quality_gate_reason(
    captured: _CapturedEvents,
) -> None:
    """With ``PATH_B_RETRY_ENABLED=0`` (enabled=False), Path C still
    emits RETRY_WOULD_DISPATCH with trigger_reason='quality_gate' on
    hollow-units input — telemetry without re-dispatch."""
    initial = _StubAdapterResult(
        tier_used="TIER_1_API_G5",
        units=[{"unit_id": "g5-1", "asking_rent": 1500}],  # hollow
    )
    knock_result = _StubAdapterResult(
        tier_used="TIER_1_API_KNOCK",
        units=[{"unit_id": "K1", "asking_rent": 1500, "beds": 1}],
    )
    table = {"knock": _StubAdapter("knock", knock_result)}

    name, result, chain, _result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-pc-004"),
        adapter_table=table,
        enabled=False,
    )
    assert name == "g5"
    assert chain == []
    would = captured.of_kind(EventKind.RETRY_WOULD_DISPATCH)
    assert len(would) == 1
    assert would[0].data["trigger_reason"] == "quality_gate"
    assert captured.of_kind(EventKind.RETRY_DISPATCHED) == []


# ─────────────────────────────────────────────────────────────────────
# Section 8 — Path C extensions (no_rent / no_area triggers + plan-level
# fallback).
#
# Covers the JSON-LD inflated-SUCCESS bucket (project_jsonld_recovery_
# 2026-05-20 memo): adapters emit beds+baths+sqft rows with no rent
# and label SUCCESS. The dimension gate passes; the rent-signal gate
# fails; Path C retries; if the retry returns real unit-level data
# with rent we promote, otherwise we keep the baseline plan-level
# rows flagged as SUCCESS_PLAN_LEVEL.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_path_c_no_rent_trigger_fires_on_jsonld_shape(
    captured: _CapturedEvents,
) -> None:
    """The 1,031-prop inflated-SUCCESS JSON-LD pattern: all units have
    beds+baths+sqft, no row has rent. Path C must trigger with
    ``trigger_reason='no_rent'`` and retry."""
    # Initial: JSON-LD-shape rows that pass quality_gate (dims present)
    # but fail rent-signal.
    initial = _StubAdapterResult(
        tier_used="TIER_2_JSONLD",
        units=[
            {"unit_id": "inferred_1", "beds": 1, "baths": 1, "sqft": 750},
            {"unit_id": "inferred_2", "beds": 2, "baths": 2, "sqft": 1100},
        ],
    )
    knock_result = _StubAdapterResult(
        tier_used="TIER_1_API_KNOCK",
        units=[{"unit_id": "K1", "asking_rent": 1500, "beds": 1, "sqft": 750}],
    )
    table = {"knock": _StubAdapter("knock", knock_result)}

    name, result, chain, result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-pc-no-rent"),
        adapter_table=table,
    )

    assert name == "knock"
    assert result.units == knock_result.units
    dispatched = captured.of_kind(EventKind.RETRY_DISPATCHED)
    assert len(dispatched) == 1
    assert dispatched[0].data["trigger_reason"] == "no_rent"
    assert dispatched[0].data["previous_tier"] == "TIER_2_JSONLD"
    success = captured.of_kind(EventKind.RETRY_SUCCESS)
    assert len(success) == 1 and success[0].data["trigger_reason"] == "no_rent"
    # On WIN, no plan-level fallback applied.
    assert result_dict.get("_verdict_quality") != "SUCCESS_PLAN_LEVEL"


@pytest.mark.asyncio
async def test_path_c_no_area_trigger_fires_on_rent_only_units(
    captured: _CapturedEvents,
) -> None:
    """Rare-but-real shape: units have rent + beds but no sqft (some
    SightMap responses). Path C triggers with ``no_area``."""
    initial = _StubAdapterResult(
        tier_used="TIER_1_API_SIGHTMAP",
        units=[
            {"unit_id": "S1", "beds": 1, "asking_rent": 1500},
            {"unit_id": "S2", "beds": 2, "asking_rent": 2200},
        ],
    )
    knock_result = _StubAdapterResult(
        tier_used="TIER_1_API_KNOCK",
        units=[{"unit_id": "K1", "asking_rent": 1500, "beds": 1, "sqft": 750}],
    )
    table = {"knock": _StubAdapter("knock", knock_result)}

    name, result, chain, _rd = await _run_retry_loop_under_test(
        initial_adapter_name="sightmap",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-pc-no-area"),
        adapter_table=table,
    )

    assert name == "knock"
    dispatched = captured.of_kind(EventKind.RETRY_DISPATCHED)
    assert dispatched[0].data["trigger_reason"] == "no_area"


@pytest.mark.asyncio
async def test_path_c_plan_level_fallback_when_all_retries_fail(
    captured: _CapturedEvents,
) -> None:
    """Per the project_jsonld_recovery memo: 'getting floor plan level
    data is okay but just should be flagged'. When the baseline had
    plan-level rows (no rent) and all retries fail, restore the
    baseline AND mark the property dict with
    ``_verdict_quality=SUCCESS_PLAN_LEVEL``."""
    # Baseline: JSON-LD plan-level rows (dims, no rent).
    baseline_units = [
        {"unit_id": "inferred_1", "beds": 1, "baths": 1, "sqft": 750},
        {"unit_id": "inferred_2", "beds": 2, "baths": 2, "sqft": 1100},
    ]
    initial = _StubAdapterResult(
        tier_used="TIER_2_JSONLD",
        units=baseline_units,
    )
    # Both retry candidates fail (return empty).
    table = {
        "knock": _StubAdapter(
            "knock", _StubAdapterResult(tier_used="TIER_1_API_KNOCK_EMPTY")
        ),
        "rentcafe": _StubAdapter(
            "rentcafe",
            _StubAdapterResult(tier_used="TIER_1_API_RENTCAFE_SHAPE_REJECTED"),
        ),
    }
    name, result, chain, result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-pc-fallback"),
        adapter_table=table,
    )

    # Baseline plan-level rows preserved.
    assert result.units == baseline_units, (
        "all retries failed → baseline plan-level data must be preserved, "
        f"not the empty last-attempt result; got {result.units!r}"
    )
    # Tier stamped with _PLAN_LEVEL suffix.
    assert result.tier_used == "TIER_2_JSONLD_PLAN_LEVEL", (
        f"baseline tier must be flagged with _PLAN_LEVEL; got {result.tier_used!r}"
    )
    # Verdict quality marker on the result dict.
    assert result_dict.get("_verdict_quality") == "SUCCESS_PLAN_LEVEL"
    assert result_dict.get("_plan_level_reason") == "no_rent"
    # Retries did fire (and emit telemetry).
    assert len(captured.of_kind(EventKind.RETRY_DISPATCHED)) >= 1
    assert captured.of_kind(EventKind.RETRY_SUCCESS) == []


@pytest.mark.asyncio
async def test_path_c_retry_promotes_over_plan_level_baseline(
    captured: _CapturedEvents,
) -> None:
    """Win case: baseline has plan-level rows (no rent); retry returns
    real unit-level rows with rent → promote retry, do NOT apply
    plan-level fallback."""
    baseline_units = [
        {"unit_id": "inferred_1", "beds": 1, "baths": 1, "sqft": 750},
    ]
    initial = _StubAdapterResult(
        tier_used="TIER_2_JSONLD",
        units=baseline_units,
    )
    knock_units = [
        {"unit_id": "K1", "asking_rent": 1500, "beds": 1, "sqft": 750},
        {"unit_id": "K2", "asking_rent": 1800, "beds": 2, "sqft": 1100},
    ]
    table = {
        "knock": _StubAdapter(
            "knock",
            _StubAdapterResult(tier_used="TIER_1_API_KNOCK", units=knock_units),
        ),
    }
    name, result, chain, result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-pc-promote"),
        adapter_table=table,
    )

    # Retry winner promoted.
    assert name == "knock"
    assert result.units == knock_units
    # _PLAN_LEVEL fallback NOT applied (the retry succeeded).
    assert "_PLAN_LEVEL" not in (result.tier_used or "")
    assert result_dict.get("_verdict_quality") != "SUCCESS_PLAN_LEVEL"


@pytest.mark.asyncio
async def test_path_c_no_fallback_when_initial_was_empty_exit(
    captured: _CapturedEvents,
) -> None:
    """Plan-level fallback only applies when the BASELINE had units.
    For empty-exit triggers (no baseline units), no fallback restoration
    happens — the final result stays empty."""
    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY", units=[])
    # All retries also fail.
    table = {
        "knock": _StubAdapter(
            "knock", _StubAdapterResult(tier_used="TIER_1_API_KNOCK_EMPTY")
        ),
        "rentcafe": _StubAdapter(
            "rentcafe",
            _StubAdapterResult(tier_used="TIER_1_API_RENTCAFE_EMPTY"),
        ),
    }
    name, result, chain, result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-pc-no-baseline"),
        adapter_table=table,
    )

    # No baseline-units → no plan-level fallback marker.
    assert "_PLAN_LEVEL" not in (result.tier_used or "")
    assert result_dict.get("_verdict_quality") != "SUCCESS_PLAN_LEVEL"


@pytest.mark.asyncio
async def test_path_c_partial_rent_signal_does_not_trigger(
    captured: _CapturedEvents,
) -> None:
    """Threshold = 0.5. If at least half the units have rent, no
    Path-C-no-rent trigger. The data is good enough."""
    initial = _StubAdapterResult(
        tier_used="TIER_1_API_RENTCAFE",
        units=[
            {"unit_id": "1", "asking_rent": 1500, "beds": 1, "sqft": 750},
            {"unit_id": "2", "asking_rent": None, "beds": 2, "sqft": 1100},
            # 2/3 have rent → above 0.5 → passes rent-signal
            {"unit_id": "3", "asking_rent": 2200, "beds": 3, "sqft": 1400},
        ],
    )
    name, result, chain, _rd = await _run_retry_loop_under_test(
        initial_adapter_name="rentcafe",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-pc-partial"),
        adapter_table={},
    )
    assert name == "rentcafe"
    assert chain == []
    eps = captured.of_kind(EventKind.RETRY_EPISODE)
    assert len(eps) == 1 and eps[0].data["outcome"] == "not_triggered"
    assert [e for e in captured.events if e.kind is not EventKind.RETRY_EPISODE] == []


# ─────────────────────────────────────────────────────────────────────
# Section 9 — CLOSED-FUNNEL episode telemetry (2026-07-26).
#
# One terminal RETRY_EPISODE per EPISODE — one execution of the block, i.e.
# one ``scrape()`` call, NOT one property: scrape() recurses for link-hop
# sub-pages under the same property_id (mean 3.73 on the real 2026-07-16
# ledger). Per-property numbers come from the rollup in
# ``scripts/reports/retry_funnel.py``.
#
# Each episode carries a trigger reason and an outcome from a closed 13-value
# vocabulary, so that:
#
#   dispatched == won + lost_* + torn_down(attempts >= 1)
#
# closes — and so that a trigger which never dispatched (the ~37% bucket
# with no second candidate) is COUNTED rather than silent. Before this,
# zero events was equally consistent with "never fired" and "fired
# constantly and always dead-ended".
#
# Every test here asserts the arithmetic via ``_assert_episode_invariants``
# in addition to its own outcome-specific claims.
# ─────────────────────────────────────────────────────────────────────


_DISPATCHED_OUTCOMES = {
    "won",
    "lost_candidates_exhausted",
    "lost_adapter_error",
    "lost_dead_end",
    "lost_max_retries",
}
_ABORTED_OUTCOMES = {"aborted_error", "aborted_cancelled"}
#: Torn down mid-flight; can land on either side of the dispatch split, so
#: the arithmetic splits them by ``attempts``. ``trigger_error`` joins the
#: aborts here because the predicate crash leaves the same partial state.
_TORN_DOWN_OUTCOMES = _ABORTED_OUTCOMES | {"trigger_error"}
_NEVER_LOOKED_OUTCOMES = {"not_triggered", "no_budget", "setup_error"}


def _assert_episode_invariants(captured: _CapturedEvents) -> list[Event]:
    """Assert the design's set-A and set-B invariants over one captured
    event stream, and return the episode events.

    This is the whole point of the exercise: every funnel number must be
    derivable AND cross-checkable, so the tests assert the arithmetic
    rather than just the presence of a field.
    """
    from ma_poc.pms.scraper import RETRY_EPISODE_OUTCOMES

    eps = captured.of_kind(EventKind.RETRY_EPISODE)
    dispatched_evs = captured.of_kind(EventKind.RETRY_DISPATCHED)
    success_evs = captured.of_kind(EventKind.RETRY_SUCCESS)
    would_evs = captured.of_kind(EventKind.RETRY_WOULD_DISPATCH)

    # A1 — one episode, one id.
    ids = [e.data["episode_id"] for e in eps]
    assert len(ids) == len(set(ids)), f"duplicate episode_id in {ids!r}"

    # A2 / D4 — closed vocabulary.
    for e in eps:
        assert e.data["outcome"] in RETRY_EPISODE_OUTCOMES, (
            f"outcome {e.data['outcome']!r} is outside the declared "
            f"vocabulary — an aggregator would silently mis-partition it"
        )
        assert e.data["trigger_reason"] in {
            "",
            "empty_exit",
            "quality_gate",
            "no_rent",
            "no_area",
            "plan_level_only",
        }

    dispatched = [e for e in eps if e.data["attempts"] >= 1]

    # A5 — THE HEADLINE. dispatched == won + lost_* + aborted(attempts>=1).
    closed = [
        e
        for e in dispatched
        if e.data["outcome"] in _DISPATCHED_OUTCOMES | _TORN_DOWN_OUTCOMES
    ]
    assert len(dispatched) == len(closed), (
        "the funnel is OPEN: an episode dispatched at least once but its "
        "outcome is not one of won / lost_* / aborted_*"
    )

    for e in eps:
        d = e.data
        outcome = d["outcome"]

        # A6 — attempts bounded by the configured budget.
        assert 0 <= d["attempts"] <= d["max_retries"]

        # A7 — the tried lists agree with the attempt count.
        assert len(d["tried_pms"]) == d["attempts"]
        if outcome not in _TORN_DOWN_OUTCOMES:
            assert len(d["tried_adapters"]) == d["attempts"]

        # A8 — no_candidate is exactly "looked on the first pass, found none".
        assert (outcome == "no_candidate") == (
            d["attempts"] == 0 and d["candidates_offered"] == 0
        )

        # A9 — the -1 sentinel means "never looked".
        #
        # DEVIATION from the written design, which states this as a strict
        # biconditional over {not_triggered, no_budget, setup_error}. An abort
        # raised BY detect_pms_candidates itself also leaves the sentinel at
        # -1 — truthfully, since the look never completed — so aborted_* is
        # exempted here exactly as it is in A7.
        if d["candidates_offered"] == -1 and outcome not in _TORN_DOWN_OUTCOMES:
            assert outcome in _NEVER_LOOKED_OUTCOMES
        if outcome in _NEVER_LOOKED_OUTCOMES:
            assert d["candidates_offered"] == -1

        # A10 — the two exhaustion outcomes are distinguishable by attempts.
        if outcome == "lost_max_retries":
            assert d["attempts"] == d["max_retries"]
        if outcome == "lost_candidates_exhausted":
            assert 1 <= d["attempts"] < d["max_retries"]

        # A11 / A12 — win fields are populated iff the episode won.
        assert (outcome == "won") == bool(d["won_pms"])
        if outcome == "won":
            assert d["won_unit_count"] >= 1
            assert d["won_tier"] != ""
            assert d["won_pms"] == d["tried_adapters"][-1]
        else:
            assert d["won_tier"] == ""
            assert d["won_unit_count"] == -1

        # A13 — the plan-level fallback only fires on a loss with baseline rows.
        if d["baseline_restored"]:
            assert outcome != "won"
            assert d["trigger_reason"] in {"quality_gate", "no_rent", "no_area"}
            assert d["baseline_unit_count"] > 0

    # B1 — every dispatch pairs with its episode, and the totals agree.
    assert sum(e.data["attempts"] for e in eps) == len(dispatched_evs), (
        "dangling dispatches: sum(episode.attempts) != count(RETRY_DISPATCHED)"
    )
    for e in eps:
        mine = [
            x
            for x in dispatched_evs
            if x.data["episode_id"] == e.data["episode_id"]
        ]
        assert len(mine) == e.data["attempts"]

    # B2 / B3 — wins and telemetry-only breaks reconcile with the old events.
    assert len([e for e in eps if e.data["outcome"] == "won"]) == len(success_evs)
    assert len(
        [e for e in eps if e.data["outcome"] == "telemetry_only"]
    ) == len(would_evs)

    return eps


def _one_episode(captured: _CapturedEvents) -> dict[str, Any]:
    """Assert the invariants, assert there is exactly one episode, return
    its payload."""
    eps = _assert_episode_invariants(captured)
    assert len(eps) == 1, f"expected exactly one episode, got {len(eps)}"
    return eps[0].data


_HTML_G5_ONLY = (
    "<html><body>"
    '<script src="https://themes.g5dxm.com/themes/g5-c-acme/main.js"></script>'
    "</body></html>"
)

# Passes quality_gate + rent-signal + area-signal, but no row carries a
# per-apartment anchor → rows_are_plan_level() is True → plan_level_only.
_PLAN_LEVEL_UNITS = [
    {"floor_plan": "A1", "beds": 1, "baths": 1, "sqft": 750, "asking_rent": 1500},
    {"floor_plan": "B2", "beds": 2, "baths": 2, "sqft": 1100, "asking_rent": 2100},
]
_UNIT_LEVEL_UNITS = [
    {"unit_number": "101", "beds": 1, "baths": 1, "sqft": 750, "asking_rent": 1500},
]


@pytest.mark.asyncio
async def test_episode_not_triggered_healthy_property(
    captured: _CapturedEvents,
) -> None:
    """outcome=not_triggered — the DENOMINATOR. Emitted even though the
    retry never engaged, which is what makes ``count(RETRY_EPISODE) == 0``
    mean "the hook did not run" instead of "we cannot tell"."""
    initial = _StubAdapterResult(
        tier_used="TIER_1_API_KNOCK", units=_UNIT_LEVEL_UNITS
    )
    await _run_retry_loop_under_test(
        initial_adapter_name="knock",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/a", property_id="P-ep-nt"),
        adapter_table={},
    )
    d = _one_episode(captured)
    assert d["outcome"] == "not_triggered"
    assert d["trigger_reason"] == ""
    assert d["final_trigger_reason"] == ""
    assert d["attempts"] == 0
    assert d["candidates_offered"] == -1
    assert d["baseline_pms"] == "knock"
    assert d["baseline_tier"] == "TIER_1_API_KNOCK"
    assert d["baseline_unit_count"] == 1
    assert d["baseline_plan_level"] is False
    assert d["scrape_url"] == "https://example.com/a"


@pytest.mark.asyncio
async def test_episode_not_triggered_zero_units_success_label(
    captured: _CapturedEvents,
) -> None:
    """outcome=not_triggered on a SUCCESS label with zero units and zero
    errors — no data, and no retry either.

    This is the next blind spot of the same shape as the plan-level one,
    and these fields are what make it a one-line query instead of an
    invisible population.
    """
    initial = _StubAdapterResult(tier_used="TIER_1_API_KNOCK", units=[])
    await _run_retry_loop_under_test(
        initial_adapter_name="knock",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-ep-zero"),
        adapter_table={},
    )
    d = _one_episode(captured)
    assert d["outcome"] == "not_triggered"
    assert d["baseline_unit_count"] == 0
    assert d["baseline_error_count"] == 0
    assert d["baseline_plan_level"] is False


@pytest.mark.asyncio
async def test_episode_no_budget_when_max_retries_zero(
    captured: _CapturedEvents,
) -> None:
    """outcome=no_budget — the trigger DID fire, but PATH_B_MAX_RETRIES<=0
    meant the loop was never entered. Split from not_triggered because one
    is a healthy property and the other is a misconfigured run."""
    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY", units=[])
    await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-ep-budget"),
        adapter_table={},
        max_retries=0,
    )
    d = _one_episode(captured)
    assert d["outcome"] == "no_budget"
    assert d["trigger_reason"] == "empty_exit"  # it DID trigger
    assert d["attempts"] == 0
    assert d["candidates_offered"] == -1
    assert d["max_retries"] == 0


@pytest.mark.asyncio
async def test_episode_telemetry_only_mode(captured: _CapturedEvents) -> None:
    """outcome=telemetry_only — PATH_B_RETRY_ENABLED=0. Reconciles 1:1
    with RETRY_WOULD_DISPATCH (invariant B3)."""
    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY", units=[])
    table = {
        "knock": _StubAdapter(
            "knock",
            _StubAdapterResult(
                tier_used="TIER_1_API_KNOCK", units=_UNIT_LEVEL_UNITS
            ),
        )
    }
    await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-ep-telem"),
        adapter_table=table,
        enabled=False,
    )
    d = _one_episode(captured)
    assert d["outcome"] == "telemetry_only"
    assert d["attempts"] == 0
    assert d["candidates_offered"] == 2
    assert d["retry_enabled"] is False
    # B5 — enabled=False must never produce a dispatch.
    assert captured.of_kind(EventKind.RETRY_DISPATCHED) == []


@pytest.mark.asyncio
async def test_episode_won_on_first_attempt(captured: _CapturedEvents) -> None:
    """outcome=won on attempt 1 — reconciles 1:1 with RETRY_SUCCESS."""
    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY", units=[])
    table = {
        "knock": _StubAdapter(
            "knock",
            _StubAdapterResult(
                tier_used="TIER_1_API_KNOCK", units=_UNIT_LEVEL_UNITS
            ),
        )
    }
    await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-ep-won1"),
        adapter_table=table,
    )
    d = _one_episode(captured)
    assert d["outcome"] == "won"
    assert d["attempts"] == 1
    assert d["tried_pms"] == ["knock"]
    assert d["tried_adapters"] == ["knock"]
    assert d["won_pms"] == "knock"
    assert d["won_tier"] == "TIER_1_API_KNOCK"
    assert d["won_unit_count"] == 1
    assert d["baseline_restored"] is False


@pytest.mark.asyncio
async def test_episode_won_on_second_attempt(captured: _CapturedEvents) -> None:
    """outcome=won on attempt 2 — ``tried_pms`` records the full ordered
    path, so a losing first candidate is still attributable."""
    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY", units=[])
    table = {
        "knock": _StubAdapter(
            "knock", _StubAdapterResult(tier_used="TIER_1_API_KNOCK_EMPTY")
        ),
        "rentcafe": _StubAdapter(
            "rentcafe",
            _StubAdapterResult(
                tier_used="TIER_1_API_RENTCAFE", units=_UNIT_LEVEL_UNITS
            ),
        ),
    }
    await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-ep-won2"),
        adapter_table=table,
    )
    d = _one_episode(captured)
    assert d["outcome"] == "won"
    assert d["attempts"] == 2
    assert d["tried_pms"] == ["knock", "rentcafe"]
    assert d["won_pms"] == "rentcafe"
    assert len(captured.of_kind(EventKind.RETRY_DISPATCHED)) == 2


@pytest.mark.asyncio
async def test_episode_lost_max_retries(captured: _CapturedEvents) -> None:
    """outcome=lost_max_retries — hit the attempt cap while still
    triggering. Previously indistinguishable from every other loss."""
    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY", units=[])
    table = {
        "knock": _StubAdapter(
            "knock", _StubAdapterResult(tier_used="TIER_1_API_KNOCK_EMPTY")
        ),
        "rentcafe": _StubAdapter(
            "rentcafe",
            _StubAdapterResult(tier_used="TIER_1_API_RENTCAFE_SHAPE_REJECTED"),
        ),
    }
    await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-ep-maxr"),
        adapter_table=table,
        max_retries=2,
    )
    d = _one_episode(captured)
    assert d["outcome"] == "lost_max_retries"
    assert d["attempts"] == 2 == d["max_retries"]
    assert d["final_trigger_reason"] == "empty_exit"
    assert captured.of_kind(EventKind.RETRY_SUCCESS) == []


@pytest.mark.asyncio
async def test_episode_lost_candidates_exhausted(
    captured: _CapturedEvents,
) -> None:
    """outcome=lost_candidates_exhausted — the SAME ``break`` as
    no_candidate, but on iteration >= 2. Splitting them is what separates
    "we had nowhere to go" from "we tried everywhere and lost"."""
    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY", units=[])
    table = {
        "knock": _StubAdapter(
            "knock", _StubAdapterResult(tier_used="TIER_1_API_KNOCK_EMPTY")
        ),
        "rentcafe": _StubAdapter(
            "rentcafe",
            _StubAdapterResult(tier_used="TIER_1_API_RENTCAFE_EMPTY"),
        ),
    }
    # Budget of 3 with only 2 co-resident candidates → the pool empties
    # before the cap is reached.
    await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-ep-exh"),
        adapter_table=table,
        max_retries=3,
    )
    d = _one_episode(captured)
    assert d["outcome"] == "lost_candidates_exhausted"
    assert d["attempts"] == 2
    assert d["max_retries"] == 3
    assert d["tried_pms"] == ["knock", "rentcafe"]
    # candidates_offered is the FIRST-iteration pool, not the last.
    assert d["candidates_offered"] == 2


@pytest.mark.asyncio
async def test_episode_lost_adapter_error(captured: _CapturedEvents) -> None:
    """outcome=lost_adapter_error — the retry adapter's extract() raised.
    RETRY_DISPATCHED was already emitted, so without a terminal event this
    dispatch dangles forever."""

    class _RaisingAdapter:
        pms_name = "knock"

        async def extract(self, page, ctx):  # noqa: ARG002
            raise RuntimeError("simulated knock crash")

    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY", units=[])
    _name, _res, chain, _rd = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-ep-adaperr"),
        adapter_table={"knock": _RaisingAdapter()},
    )
    assert chain == ["retry_failed:knock:RuntimeError"]
    d = _one_episode(captured)
    assert d["outcome"] == "lost_adapter_error"
    assert d["error_type"] == "RuntimeError"
    assert d["attempts"] == 1
    assert d["tried_pms"] == ["knock"]


@pytest.mark.asyncio
async def test_episode_lost_dead_end(captured: _CapturedEvents) -> None:
    """outcome=lost_dead_end — the retry neither won nor re-triggered, so
    the loop CONDITION went false with budget still remaining.

    A loop-condition falsification has no statement to hang an emit on,
    which is exactly why the terminal event lives in a ``finally``.
    """
    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY", units=[])
    table = {
        # Success label + zero units → _trigger() returns None (no retrigger)
        # and _win() is False (no units). Neither win nor loop continuation.
        "knock": _StubAdapter(
            "knock", _StubAdapterResult(tier_used="TIER_1_API_KNOCK", units=[])
        ),
    }
    await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-ep-dead"),
        adapter_table=table,
        max_retries=2,
    )
    d = _one_episode(captured)
    assert d["outcome"] == "lost_dead_end"
    assert d["attempts"] == 1  # budget remained: 1 < 2
    assert d["max_retries"] == 2
    assert d["trigger_reason"] == "empty_exit"
    assert d["final_trigger_reason"] == ""  # stopped triggering


@pytest.mark.asyncio
async def test_episode_aborted_error_propagates(
    captured: _CapturedEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    """outcome=aborted_error — an Exception escaped the loop region. The
    episode is still emitted (``finally``) and the exception still
    propagates unchanged."""
    import ma_poc.pms.detector as _detector_mod

    def _boom(**kwargs: Any) -> list:
        raise ValueError("detector exploded")

    monkeypatch.setattr(_detector_mod, "detect_pms_candidates", _boom)

    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY", units=[])
    with pytest.raises(ValueError, match="detector exploded"):
        await _run_retry_loop_under_test(
            initial_adapter_name="g5",
            initial_result=initial,
            page_html=_HTML_KNOCK_THEN_RENTCAFE,
            ctx=_Ctx(base_url="https://example.com/", property_id="P-ep-abort"),
            adapter_table={},
        )
    d = _one_episode(captured)
    assert d["outcome"] == "aborted_error"
    assert d["error_type"] == "ValueError"
    assert d["attempts"] == 0  # aborted before any dispatch


@pytest.mark.asyncio
async def test_episode_aborted_cancelled_is_split_from_error(
    captured: _CapturedEvents,
) -> None:
    """outcome=aborted_cancelled — CancelledError is a BaseException in
    3.12 and is the EXPECTED shape under jugnu's 600s wait_for. It gets
    its own outcome so the "is the loop buggy?" gate (aborted_error == 0)
    is not permanently red on any loaded run.

    Also pins that cancellation still propagates: classifying must not
    become catching, or asyncio cancellation breaks.
    """
    import asyncio

    class _CancelledAdapter:
        pms_name = "knock"

        async def extract(self, page, ctx):  # noqa: ARG002
            raise asyncio.CancelledError()

    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY", units=[])
    with pytest.raises(asyncio.CancelledError):
        await _run_retry_loop_under_test(
            initial_adapter_name="g5",
            initial_result=initial,
            page_html=_HTML_KNOCK_THEN_RENTCAFE,
            ctx=_Ctx(base_url="https://example.com/", property_id="P-ep-cancel"),
            adapter_table={"knock": _CancelledAdapter()},
        )
    d = _one_episode(captured)
    assert d["outcome"] == "aborted_cancelled"
    assert d["error_type"] == "CancelledError"
    # Dispatched before the teardown — the abort term in invariant A5 that
    # carries attempts >= 1.
    assert d["attempts"] == 1
    assert len(captured.of_kind(EventKind.RETRY_DISPATCHED)) == 1


@pytest.mark.asyncio
async def test_episode_id_joins_dispatch_to_terminal(
    captured: _CapturedEvents,
) -> None:
    """``episode_id`` is the join key. property_id CANNOT serve: scrape()
    recurses for link-hop sub-pages with the SAME property_id, and a real
    ledger showed one pid carrying 13 dispatches."""
    initial = _StubAdapterResult(tier_used="TIER_1_API_G5_EMPTY", units=[])
    table = {
        "knock": _StubAdapter(
            "knock", _StubAdapterResult(tier_used="TIER_1_API_KNOCK_EMPTY")
        ),
        "rentcafe": _StubAdapter(
            "rentcafe",
            _StubAdapterResult(
                tier_used="TIER_1_API_RENTCAFE", units=_UNIT_LEVEL_UNITS
            ),
        ),
    }
    await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-ep-join"),
        adapter_table=table,
    )
    d = _one_episode(captured)
    eid = d["episode_id"]
    assert eid and isinstance(eid, str)
    for ev in captured.of_kind(EventKind.RETRY_DISPATCHED):
        assert ev.data["episode_id"] == eid
        # Fixes the mis-attribution: the win condition keys off the INITIAL
        # trigger, but these events reported the rolled-forward one.
        assert ev.data["initial_trigger_reason"] == "empty_exit"
    for ev in captured.of_kind(EventKind.RETRY_SUCCESS):
        assert ev.data["episode_id"] == eid
        assert ev.data["initial_trigger_reason"] == "empty_exit"


@pytest.mark.asyncio
async def test_two_episodes_same_property_id_stay_separable(
    captured: _CapturedEvents,
) -> None:
    """The link-hop case that breaks any property_id-keyed reconciliation:
    two episodes, same pid, different episode_ids, and B1 still closes."""
    for tier in ("TIER_1_API_G5_EMPTY", "TIER_1_API_G5_EMPTY"):
        await _run_retry_loop_under_test(
            initial_adapter_name="g5",
            initial_result=_StubAdapterResult(tier_used=tier, units=[]),
            page_html=_HTML_KNOCK_THEN_RENTCAFE,
            ctx=_Ctx(base_url="https://example.com/x", property_id="P-ep-dup"),
            adapter_table={
                "knock": _StubAdapter(
                    "knock",
                    _StubAdapterResult(tier_used="TIER_1_API_KNOCK_EMPTY"),
                ),
                "rentcafe": _StubAdapter(
                    "rentcafe",
                    _StubAdapterResult(
                        tier_used="TIER_1_API_RENTCAFE_EMPTY"
                    ),
                ),
            },
        )
    eps = _assert_episode_invariants(captured)
    assert len(eps) == 2
    assert eps[0].data["episode_id"] != eps[1].data["episode_id"]
    assert eps[0].property_id == eps[1].property_id == "P-ep-dup"
    # 2 episodes x 2 attempts, all paired.
    assert len(captured.of_kind(EventKind.RETRY_DISPATCHED)) == 4


@pytest.mark.asyncio
async def test_plan_level_only_trigger_emits_episode(
    captured: _CapturedEvents,
) -> None:
    """THE TRIGGER THE POST-MORTEM COULD NOT MEASURE.

    Plan-level baseline rows clear the dimension, rent and area gates, so
    the first four checks all pass and only ``rows_are_plan_level`` fires.
    A losing plan_level_only episode gets NO SUCCESS_PLAN_LEVEL stamp,
    because "plan_level_only" is deliberately absent from the fallback
    eligibility set — a genuine defect, left alone here because fixing it
    changes what ships in properties.json. This event makes that gap
    COUNTABLE in the meantime.
    """
    initial = _StubAdapterResult(
        tier_used="TIER_1_API_G5", units=_PLAN_LEVEL_UNITS
    )
    # Retry also comes back plan-level → not a win (swapping plan-level for
    # plan-level is not a recovery).
    table = {
        "knock": _StubAdapter(
            "knock",
            _StubAdapterResult(
                tier_used="TIER_1_API_KNOCK", units=_PLAN_LEVEL_UNITS
            ),
        ),
        "rentcafe": _StubAdapter(
            "rentcafe",
            _StubAdapterResult(
                tier_used="TIER_1_API_RENTCAFE", units=_PLAN_LEVEL_UNITS
            ),
        ),
    }
    _n, _r, _c, result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-ep-plan"),
        adapter_table=table,
    )
    d = _one_episode(captured)
    assert d["trigger_reason"] == "plan_level_only"
    assert d["baseline_plan_level"] is True
    assert d["outcome"] != "won"
    assert d["attempts"] >= 1
    # The countable gap: lost plan_level_only episodes are NOT restored.
    assert d["baseline_restored"] is False
    assert result_dict.get("_verdict_quality") != "SUCCESS_PLAN_LEVEL"


@pytest.mark.asyncio
async def test_plan_level_only_trigger_wins_on_unit_level_retry(
    captured: _CapturedEvents,
) -> None:
    """The recovery the trigger exists for: a genuinely unit-level retry
    result IS promoted over a plan-level baseline."""
    initial = _StubAdapterResult(
        tier_used="TIER_1_API_G5", units=_PLAN_LEVEL_UNITS
    )
    table = {
        "knock": _StubAdapter(
            "knock",
            _StubAdapterResult(
                tier_used="TIER_1_API_KNOCK", units=_UNIT_LEVEL_UNITS
            ),
        ),
    }
    name, _r, _c, _rd = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-ep-planwin"),
        adapter_table=table,
    )
    assert name == "knock"
    d = _one_episode(captured)
    assert d["trigger_reason"] == "plan_level_only"
    assert d["outcome"] == "won"
    assert d["won_pms"] == "knock"


@pytest.mark.asyncio
async def test_baseline_restored_matches_verdict(
    captured: _CapturedEvents,
) -> None:
    """``baseline_restored`` is true exactly when the SUCCESS_PLAN_LEVEL
    fallback fired, so C3 can be checked against properties.json."""
    baseline_units = [
        {"unit_id": "inferred_1", "beds": 1, "baths": 1, "sqft": 750},
        {"unit_id": "inferred_2", "beds": 2, "baths": 2, "sqft": 1100},
    ]
    initial = _StubAdapterResult(tier_used="TIER_2_JSONLD", units=baseline_units)
    table = {
        "knock": _StubAdapter(
            "knock", _StubAdapterResult(tier_used="TIER_1_API_KNOCK_EMPTY")
        ),
        "rentcafe": _StubAdapter(
            "rentcafe",
            _StubAdapterResult(tier_used="TIER_1_API_RENTCAFE_SHAPE_REJECTED"),
        ),
    }
    _n, result, _c, result_dict = await _run_retry_loop_under_test(
        initial_adapter_name="g5",
        initial_result=initial,
        page_html=_HTML_KNOCK_THEN_RENTCAFE,
        ctx=_Ctx(base_url="https://example.com/", property_id="P-ep-restore"),
        adapter_table=table,
    )
    d = _one_episode(captured)
    assert d["baseline_restored"] is True
    assert result_dict["_verdict_quality"] == "SUCCESS_PLAN_LEVEL"
    assert d["trigger_reason"] == "no_rent"
    assert d["outcome"] == "lost_max_retries"
    # baseline_tier is snapshotted BEFORE the fallback stamps _PLAN_LEVEL in
    # place — otherwise the event would report a tier the adapter never
    # returned.
    assert d["baseline_tier"] == "TIER_2_JSONLD"
    assert result.tier_used == "TIER_2_JSONLD_PLAN_LEVEL"


@pytest.mark.asyncio
async def test_episode_arithmetic_closes_across_scenarios(
    captured: _CapturedEvents,
) -> None:
    """A5 + B1 + B2 over a MIXED stream: win, loss, no-candidate and
    not-triggered episodes in one ledger.

    ``dispatched == won + lost_* + aborted_*`` is the number that proves
    the funnel closed. On the real 2026-07-16 ledger the equivalent
    ``dangling`` figure was 106; here it must be 0.
    """
    scenarios = [
        # (initial tier, initial units, adapter table, html)
        (
            "TIER_1_API_G5_EMPTY",
            [],
            {
                "knock": _StubAdapter(
                    "knock",
                    _StubAdapterResult(
                        tier_used="TIER_1_API_KNOCK", units=_UNIT_LEVEL_UNITS
                    ),
                )
            },
            _HTML_KNOCK_THEN_RENTCAFE,
        ),  # won
        (
            "TIER_1_API_G5_EMPTY",
            [],
            {
                "knock": _StubAdapter(
                    "knock",
                    _StubAdapterResult(tier_used="TIER_1_API_KNOCK_EMPTY"),
                ),
                "rentcafe": _StubAdapter(
                    "rentcafe",
                    _StubAdapterResult(tier_used="TIER_1_API_RENTCAFE_EMPTY"),
                ),
            },
            _HTML_KNOCK_THEN_RENTCAFE,
        ),  # lost_max_retries
        ("TIER_1_API_G5_EMPTY", [], {}, _HTML_G5_ONLY),  # no_candidate
        (
            "TIER_1_API_KNOCK",
            _UNIT_LEVEL_UNITS,
            {},
            _HTML_KNOCK_THEN_RENTCAFE,
        ),  # not_triggered
    ]
    for i, (tier, units, table, html) in enumerate(scenarios):
        await _run_retry_loop_under_test(
            initial_adapter_name="g5" if not units else "knock",
            initial_result=_StubAdapterResult(tier_used=tier, units=list(units)),
            page_html=html,
            ctx=_Ctx(
                base_url="https://example.com/", property_id=f"P-ep-mix-{i}"
            ),
            adapter_table=table,
        )

    eps = _assert_episode_invariants(captured)
    assert len(eps) == 4
    outcomes = sorted(e.data["outcome"] for e in eps)
    assert outcomes == [
        "lost_max_retries",
        "no_candidate",
        "not_triggered",
        "won",
    ]

    # The funnel, restated explicitly.
    total = len(eps)
    not_triggered = sum(1 for e in eps if e.data["outcome"] == "not_triggered")
    setup_error = sum(1 for e in eps if e.data["outcome"] == "setup_error")
    triggered = total - not_triggered - setup_error
    dispatched = sum(1 for e in eps if e.data["attempts"] >= 1)
    won = sum(1 for e in eps if e.data["outcome"] == "won")

    assert triggered == 3
    assert dispatched == 2
    assert won == 1
    # A4 — every triggered episode is accounted for.
    assert triggered == (
        sum(
            1
            for e in eps
            if e.data["outcome"]
            in {"no_budget", "no_candidate", "telemetry_only"}
        )
        + dispatched
    )
    # B1 — zero dangling dispatches.
    dangling = len(captured.of_kind(EventKind.RETRY_DISPATCHED)) - sum(
        e.data["attempts"] for e in eps
    )
    assert dangling == 0
