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
    ):
        assert symbol in scraper_src, (
            f"Path B retry hook in scraper.py no longer references "
            f"{symbol!r} — test helper and hook are now out of sync; "
            f"update one or the other."
        )


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
    """
    from ma_poc.observability import events as _events_mod
    from ma_poc.observability.events import EventKind
    from ma_poc.pms.detector import detect_pms_candidates
    from ma_poc.pms.empty_exit import empty_exit_reason, is_empty_exit
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
        return None

    def _win(res: _StubAdapterResult) -> bool:
        return bool(
            res.units
            and property_passes_quality_gate(res.units)
            and property_has_rent_signal(res.units)
        )

    result_dict: dict[str, Any] = {}
    adapter_result = initial_result
    adapter_name = initial_adapter_name
    baseline_result = initial_result if initial_result.units else None
    baseline_adapter_name = initial_adapter_name
    tried: set[str] = {adapter_name}
    fallback_chain: list[str] = []
    attempt = 0
    retry_won = False
    trigger_reason = _trigger(adapter_result)
    initial_trigger_reason = trigger_reason
    current_result = adapter_result

    while trigger_reason is not None and attempt < max_retries:
        candidates = detect_pms_candidates(
            url=ctx.base_url,
            csv_row=None,
            page_html=page_html,
            exclude=tried,
            max_candidates=max_retries,
        )
        if not candidates:
            break
        nc = candidates[0]
        previous_tier = current_result.tier_used or ""
        previous_pms = (
            adapter_name if attempt == 0 else baseline_adapter_name
        )

        if not enabled:
            _events_mod.emit(
                EventKind.RETRY_WOULD_DISPATCH,
                property_id=ctx.property_id,
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
            attempt=attempt,
            previous_pms=previous_pms,
            previous_tier=previous_tier,
            empty_exit_reason=empty_exit_reason(previous_tier) or "",
            trigger_reason=trigger_reason,
            next_pms=nc.pms,
            next_confidence=nc.confidence,
        )

        tried.add(nc.pms)
        new_adapter = adapter_table.get(nc.pms)
        if new_adapter is None:
            fallback_chain.append(f"retry_failed:{nc.pms}:NoAdapter")
            break
        new_result = await new_adapter.extract(None, ctx)
        fallback_chain.append(f"retry:{nc.pms}")
        if _win(new_result):
            _events_mod.emit(
                EventKind.RETRY_SUCCESS,
                property_id=ctx.property_id,
                attempt=attempt,
                previous_pms=previous_pms,
                previous_tier=previous_tier,
                trigger_reason=trigger_reason,
                won_pms=nc.pms,
                won_tier=new_result.tier_used or "",
                unit_count=len(new_result.units),
            )
            adapter_result = new_result
            adapter_name = nc.pms
            retry_won = True
            break
        current_result = new_result
        trigger_reason = _trigger(current_result)

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
async def test_retry_no_candidate_emits_nothing(captured: _CapturedEvents) -> None:
    """Page has only G5 markers — no co-resident PMS, no candidates.
    detect_pms_candidates(exclude={'g5'}) returns []. Retry loop exits
    without emitting any event."""
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
    assert captured.events == []


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
    assert captured.events == []


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
    assert captured.events == [], (
        f"clean win on first dispatch must not trigger any retry events; "
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
    assert captured.events == []
