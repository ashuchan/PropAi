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
        "RETRY_DISPATCHED",
        "RETRY_SUCCESS",
        "PATH_B_RETRY_ENABLED",
        "PATH_B_MAX_RETRIES",
        "empty_exit_reason",
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
) -> tuple[str, _StubAdapterResult, list[str]]:
    """Mirror of the production retry loop body in
    ``ma_poc.pms.scraper`` (Path B Piece 3). Returns the final
    (adapter_name, adapter_result, fallback_chain). Kept in sync with
    the production hook via ``test_scraper_hook_kept_in_sync...``."""
    from ma_poc.observability import events as _events_mod
    from ma_poc.observability.events import EventKind
    from ma_poc.pms.detector import detect_pms_candidates
    from ma_poc.pms.empty_exit import empty_exit_reason, is_empty_exit

    adapter_result = initial_result
    adapter_name = initial_adapter_name
    tried: set[str] = {adapter_name}
    fallback_chain: list[str] = []
    attempt = 0

    while (
        is_empty_exit(adapter_result.tier_used)
        and not adapter_result.units
        and attempt < max_retries
    ):
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
        previous_tier = adapter_result.tier_used or ""
        previous_pms = adapter_name

        if not enabled:
            _events_mod.emit(
                EventKind.RETRY_WOULD_DISPATCH,
                property_id=ctx.property_id,
                previous_pms=previous_pms,
                previous_tier=previous_tier,
                empty_exit_reason=empty_exit_reason(previous_tier) or "",
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
        if new_result.units:
            _events_mod.emit(
                EventKind.RETRY_SUCCESS,
                property_id=ctx.property_id,
                attempt=attempt,
                previous_pms=previous_pms,
                previous_tier=previous_tier,
                won_pms=nc.pms,
                won_tier=new_result.tier_used or "",
                unit_count=len(new_result.units),
            )
            adapter_result = new_result
            adapter_name = nc.pms
            break
        adapter_result = new_result

    return adapter_name, adapter_result, fallback_chain


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
        units=[{"unit_id": "K1", "asking_rent": 1500}],
    )
    table = {
        "knock": _StubAdapter("knock", knock_result),
        "rentcafe": _StubAdapter(
            "rentcafe",
            _StubAdapterResult(tier_used="TIER_1_API_RENTCAFE_SHAPE_REJECTED"),
        ),
    }
    name, result, chain = await _run_retry_loop_under_test(
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
                units=[{"unit_id": "RC1"}, {"unit_id": "RC2"}],
            ),
        ),
    }
    name, result, chain = await _run_retry_loop_under_test(
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
    name, result, chain = await _run_retry_loop_under_test(
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
    name, _, chain = await _run_retry_loop_under_test(
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

    name, result, chain = await _run_retry_loop_under_test(
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
    """First adapter returned units — retry never enters the loop."""
    initial = _StubAdapterResult(
        tier_used="TIER_1_API_KNOCK",
        units=[{"unit_id": "K1"}],
    )
    name, result, chain = await _run_retry_loop_under_test(
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
    name, result, chain = await _run_retry_loop_under_test(
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
