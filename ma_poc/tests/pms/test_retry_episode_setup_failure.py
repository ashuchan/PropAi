"""The RETRY_EPISODE terminal event, exercised against the REAL scraper.

WHY THIS FILE EXISTS, AND WHY IT COVERS EVERY OUTCOME
-----------------------------------------------------
``test_path_b_retry_telemetry.py`` re-implements the Path-B/C retry loop as a
test helper and drives ~19 telemetry assertions through that MIRROR. A mirror
can only ever prove the mirror correct. Measured, not assumed: rewriting
production's four ``lost_*`` outcome assignments to ``"not_triggered"`` — so
that four of the thirteen declared outcomes were assigned from nowhere — left
the entire suite green. So did deleting the ``aborted_error`` /
``aborted_cancelled`` classification and suppressing the ``finally`` emit on
the exception path, which would make every timed-out property vanish from the
funnel: the exact class of silence the closed funnel exists to remove.

This file is the binding. Every member of ``RETRY_EPISODE_OUTCOMES`` is
produced by driving ``ma_poc.pms.scraper.scrape()`` itself, and each scenario
asserts the outcome it expects, so a mis-assignment in production fails here
by name. ``test_every_declared_outcome_is_reachable_from_production`` then
closes the set: an outcome nothing can produce is a lie about what the funnel
can report, and an outcome produced but undeclared trips the vocabulary
invariant in ``retry_funnel.py``.

It also pins two claims that are asserted nowhere else:

  * ``setup_error`` means the block's imports or ``int(PATH_B_MAX_RETRIES)``
    raised — retry is then DEAD FOR THE WHOLE RUN while the run looks exactly
    like "nothing ever triggered" (the MAPPING_SAVE_DROPPED shape: a writer
    that runs but never writes). ``retry_funnel.py`` PAGES on it. A crash in
    the per-property trigger predicate must therefore NOT land here — it is
    ``trigger_error``.
  * the ``finally`` block covers all thirteen exits, including the
    ``CancelledError`` that unwinds through no handler at all.

ENV HAZARD: use ``monkeypatch.setenv`` only, never ``os.environ[...] = ...``.
``tests/pms/test_retry_plan_level_trigger.py`` asserts against the AMBIENT
process environment, so a leaked var makes it fail depending on collection
order.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ma_poc.observability import events as _events_mod
from ma_poc.observability.events import Event, EventKind
from ma_poc.pms.adapters.base import AdapterResult
from ma_poc.pms.detector import DetectedPMS
from ma_poc.pms.resolver import ResolvedTarget
from ma_poc.pms.scraper import RETRY_EPISODE_OUTCOMES, scrape


class _DeadProbe:
    """``probe_get`` stand-in: a 404 with an empty body.

    Keeps ``_drive`` off the network. Carries only the two attributes the
    two call sites read (``status_code``, ``text``)."""

    status_code = 404
    text = ""


class _CapturedEvents:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, kind: EventKind, property_id: str, **data: Any) -> Event:
        ev = Event(kind=kind, property_id=property_id, data=data, run_id="")
        self.events.append(ev)
        return ev

    def of_kind(self, kind: EventKind) -> list[Event]:
        return [e for e in self.events if e.kind == kind]

    def episode(self) -> dict[str, Any]:
        """The single RETRY_EPISODE payload — asserts there is exactly one.

        One terminal event per EPISODE is the whole contract; every test here
        goes through this accessor so a double-emit or a missing emit fails
        even when the test was written to check something else.
        """
        eps = self.of_kind(EventKind.RETRY_EPISODE)
        assert len(eps) == 1, f"expected exactly 1 RETRY_EPISODE, got {len(eps)}"
        return dict(eps[0].data)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> _CapturedEvents:
    cap = _CapturedEvents()
    monkeypatch.setattr(_events_mod, "emit", cap)
    return cap


# ─────────────────────────────────────────────────────────────────────
# Fixtures: pages and unit shapes
# ─────────────────────────────────────────────────────────────────────

#: Two co-resident PMS signals besides the baseline. ``detect_pms_candidates``
#: returns ``['knock', 'rentcafe']`` for this page when 'g5' is excluded — the
#: same fixture the mirror in test_path_b_retry_telemetry.py uses, so the
#: parity tests at the bottom feed both implementations identical input.
_HTML_TWO_CANDIDATES = (
    "<html><body>"
    '<script src="https://themes.g5dxm.com/themes/g5-c-acme/main.js"></script>'
    '<script src="https://doorway.knck.io/latest/doorway.min.js"></script>'
    '<script>knockDoorway.init("a8e311e98aee0ee4545fea9e01b06ac6",'
    '"community","69e936e6567a11ef");</script>'
    '<a href="https://lpc.securecafe.com/onlineleasing/x/availableunits.aspx">x</a>'
    "</body></html>"
)
#: No co-resident PMS — the ~37% of the plan cohort with nowhere to retry to.
_HTML_NO_CANDIDATE = "<html><body>hello</body></html>"

#: Real per-apartment identity + rent + area: the trigger stays None.
_UNIT_LEVEL = [
    {"unit_number": "101", "beds": 1, "baths": 1, "sqft": 750, "asking_rent": 1500}
]
#: Floor-plan rows: dims + rent + area, but no per-apartment anchor.
_PLAN_LEVEL = [
    {"floor_plan": "A1", "beds": 1, "baths": 1, "sqft": 750, "asking_rent": 1500},
    {"floor_plan": "B2", "beds": 2, "baths": 2, "sqft": 1100, "asking_rent": 2100},
]
#: Name-only stubs: no numeric dimension at all → fails the quality gate.
_NAME_ONLY = [{"floor_plan": "A1"}, {"floor_plan": "B2"}]
#: A non-dict row among the units. ``property_passes_quality_gate`` tolerates
#: it (1 of 2 substantive == the 0.5 threshold) and ``property_has_rent_signal``
#: then raises AttributeError on ``"junk".get(...)``.
_MALFORMED = [
    {"unit_number": "1", "beds": 1, "baths": 1, "sqft": 700, "asking_rent": 1200},
    "junk",
]


def _make_page(content: str = _HTML_NO_CANDIDATE) -> AsyncMock:
    page = AsyncMock()
    page.url = "https://example.com/"
    page.content = AsyncMock(return_value=content)
    page.evaluate = AsyncMock(return_value=[])
    return page


def _make_detection(pms: str = "entrata") -> DetectedPMS:
    return DetectedPMS(
        pms=pms,  # type: ignore[arg-type]
        confidence=0.90,
        evidence=["test"],
        recommended_strategy="api_first",
    )


def _make_resolved(pms: str = "entrata") -> ResolvedTarget:
    url = "https://example.com/"
    return ResolvedTarget(
        original_url=url,
        resolved_url=url,
        hop_path=[url],
        final_detection=_make_detection(pms),
        method="no_hop",  # type: ignore[arg-type]
    )


def _stub_adapter(pms: str, outcome: AdapterResult | BaseException) -> AsyncMock:
    """An adapter whose ``extract`` returns *outcome*, or raises it."""
    ad = AsyncMock()
    ad.pms_name = pms
    if isinstance(outcome, BaseException):
        ad.extract = AsyncMock(side_effect=outcome)
    else:
        ad.extract = AsyncMock(return_value=outcome)
    return ad


async def _drive(
    *,
    baseline_units: list[Any],
    baseline_tier: str,
    baseline_pms: str = "g5",
    html: str = _HTML_NO_CANDIDATE,
    retry_table: dict[str, AdapterResult | BaseException] | None = None,
    candidates_side_effect: BaseException | None = None,
) -> dict[str, Any]:
    """Drive the real ``scrape()`` far enough to reach the Path-B/C block.

    Args:
        baseline_units: what the FIRST adapter returns.
        baseline_tier: its ``tier_used`` (an ``*_EMPTY`` suffix is an
            empty-exit label and triggers Path B).
        baseline_pms: the baseline adapter's ``pms_name``; the retry excludes
            it from the candidate pool.
        html: page content — this is what ``detect_pms_candidates`` reads.
        retry_table: pms name → the retry adapter's result, or an exception
            for it to raise. Patches the registry lookup the retry block does
            (``ma_poc.pms.adapters.registry.get_adapter``), which is a
            DIFFERENT binding from the baseline's ``scraper.get_adapter``.
        candidates_side_effect: make ``detect_pms_candidates`` raise, which is
            the only way to get an exception into the loop region that the
            per-attempt ``except Exception`` does not already swallow.

    Returns:
        The ``scrape()`` result dict.
    """
    baseline = _stub_adapter(
        baseline_pms,
        AdapterResult(
            units=baseline_units, tier_used=baseline_tier, errors=[], confidence=0.85
        ),
    )
    table = retry_table or {}

    def _registry_get_adapter(pms: str) -> AsyncMock:
        # The real registry never returns None — unknown PMSs resolve to
        # ``generic`` — so an unlisted candidate yields an adapter that
        # extracts nothing rather than a KeyError.
        return _stub_adapter(
            pms, table.get(pms, AdapterResult(units=[], tier_used="TIER_3_DOM"))
        )

    from ma_poc.pms import detector as _detector_mod

    real_candidates = _detector_mod.detect_pms_candidates
    cand_patch = patch.object(
        _detector_mod,
        "detect_pms_candidates",
        side_effect=(
            candidates_side_effect
            if candidates_side_effect is not None
            else real_candidates
        ),
    )

    with (
        patch("ma_poc.pms.scraper.detect_pms", return_value=_make_detection()),
        patch("ma_poc.pms.scraper.resolve_target", return_value=_make_resolved()),
        patch("ma_poc.pms.scraper.get_adapter", return_value=baseline),
        patch(
            "ma_poc.pms.adapters.registry.get_adapter",
            side_effect=_registry_get_adapter,
        ),
        patch(
            "ma_poc.pms.scraper.confirm_detection",
            side_effect=lambda det, _responses: det,
        ),
        # These tests drive the REAL scrape(), which reaches probe_get twice
        # outside the retry block: plan-page enrichment (scraper.py:649) and
        # the crawl-GET hop gate (:689). Both are sync curl_cffi calls, so
        # stubbing detect_pms/get_adapter/resolve_target does not stop them —
        # they escaped to the live internet until ma_poc/conftest.py's guard
        # started failing them. 404 + empty body is the shape example.com was
        # already returning: enrichment skips (non-200) and the gate retires
        # the hop, so behaviour is unchanged and now hermetic.
        patch(
            "ma_poc.pms.adapters._probe.probe_get",
            side_effect=lambda *_a, **_k: _DeadProbe(),
        ),
        cand_patch,
    ):
        return await scrape(
            "https://example.com/", page=_make_page(html), property_id="P-real-001"
        )


@pytest.fixture(autouse=True)
def _retry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the flags every test here depends on; individual tests override."""
    monkeypatch.setenv("PATH_B_RETRY_ENABLED", "1")
    monkeypatch.setenv("PATH_B_MAX_RETRIES", "2")


# ─────────────────────────────────────────────────────────────────────
# Section 1 — the setup/config outcomes.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_malformed_max_retries_emits_setup_error(
    captured: _CapturedEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-integer PATH_B_MAX_RETRIES kills the retry for the whole run.

    Before this event existed the run was indistinguishable from a healthy
    one in which nothing happened to trigger. Now it pages.
    """
    monkeypatch.setenv("PATH_B_MAX_RETRIES", "two")

    result = await _drive(baseline_units=_UNIT_LEVEL, baseline_tier="TIER_1_API_ENTRATA")

    d = captured.episode()
    assert d["outcome"] == "setup_error"
    assert d["error_type"] == "ValueError"
    assert d["attempts"] == 0
    assert d["candidates_offered"] == -1
    assert d["trigger_reason"] == ""
    # Baseline facts are still reported: they are snapshotted OUTSIDE the
    # outer try precisely so this handler has something real to say.
    assert d["baseline_pms"] == "g5"
    assert d["baseline_tier"] == "TIER_1_API_ENTRATA"
    assert d["baseline_unit_count"] == 1
    assert d["baseline_error_count"] == 0
    # 0 <= attempts <= max_retries still holds (invariant A6).
    assert 0 <= d["attempts"] <= d["max_retries"]
    # The scrape itself is unharmed — Path B/C must never block a scrape.
    assert result["units"]


@pytest.mark.asyncio
async def test_setup_error_does_not_double_emit(
    captured: _CapturedEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``_retry_episode_emitted`` guard: the inner ``finally`` and the
    outer handler must never both fire for one episode."""
    monkeypatch.setenv("PATH_B_MAX_RETRIES", "not-a-number")
    await _drive(baseline_units=_UNIT_LEVEL, baseline_tier="TIER_X")
    d = captured.episode()  # asserts exactly one
    assert d["episode_id"]


@pytest.mark.asyncio
async def test_zero_budget_emits_no_budget_not_lost_max_retries(
    captured: _CapturedEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PATH_B_MAX_RETRIES=0`` is a misconfiguration, not an exhausted retry.

    Both exits falsify the same loop condition, so the resolution order
    (no_budget tested BEFORE lost_max_retries) is the only thing keeping
    ``0 >= 0`` from reporting a run that never retried as one that tried
    twice and lost.
    """
    monkeypatch.setenv("PATH_B_MAX_RETRIES", "0")

    await _drive(
        baseline_units=[],
        baseline_tier="TIER_1_API_G5_EMPTY",
        html=_HTML_TWO_CANDIDATES,
    )

    d = captured.episode()
    assert d["outcome"] == "no_budget"
    assert d["trigger_reason"] == "empty_exit"
    assert d["attempts"] == 0
    assert d["max_retries"] == 0
    # Never reached the candidate lookup — the -1 sentinel, not 0.
    assert d["candidates_offered"] == -1


# ─────────────────────────────────────────────────────────────────────
# Section 2 — the undispatched outcomes.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_production_hook_emits_not_triggered_denominator(
    captured: _CapturedEvents,
) -> None:
    """The REAL hook emits an episode for a healthy property.

    This is the test that would have failed on 2026-07-25: it pins that
    ``count(RETRY_EPISODE) == 0`` can only ever mean "the hook did not run".
    """
    await _drive(baseline_units=_UNIT_LEVEL, baseline_tier="TIER_1_API_ENTRATA")

    d = captured.episode()
    assert d["outcome"] == "not_triggered"
    assert d["attempts"] == 0
    assert d["candidates_offered"] == -1
    assert d["baseline_plan_level"] is False
    assert d["retry_enabled"] is True
    assert d["max_retries"] == 2
    assert d["error_type"] == ""
    assert captured.of_kind(EventKind.RETRY_EPISODE)[0].property_id == "P-real-001"


@pytest.mark.asyncio
async def test_production_hook_counts_plan_level_trigger(
    captured: _CapturedEvents,
) -> None:
    """Plan-level rows in, ``plan_level_only`` out — measured at the real
    hook, on a page with no co-resident PMS so the episode terminates in the
    ~37% ``no_candidate`` bucket that used to be completely silent."""
    await _drive(baseline_units=_PLAN_LEVEL, baseline_tier="TIER_1_API_ENTRATA")

    d = captured.episode()
    assert d["trigger_reason"] == "plan_level_only"
    assert d["baseline_plan_level"] is True
    assert d["baseline_unit_count"] == 2
    # No co-resident PMS on this page → nowhere to retry to. Previously this
    # emitted NOTHING, which is the whole reason the canary was unanswerable.
    assert d["outcome"] == "no_candidate"
    assert d["attempts"] == 0
    assert d["candidates_offered"] == 0
    assert captured.of_kind(EventKind.RETRY_DISPATCHED) == []


@pytest.mark.asyncio
async def test_telemetry_only_mode_emits_would_dispatch_and_stops(
    captured: _CapturedEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PATH_B_RETRY_ENABLED=0``: one would-dispatch, zero dispatches, and a
    terminal episode that says so."""
    monkeypatch.setenv("PATH_B_RETRY_ENABLED", "0")

    await _drive(
        baseline_units=[],
        baseline_tier="TIER_1_API_G5_EMPTY",
        html=_HTML_TWO_CANDIDATES,
    )

    d = captured.episode()
    assert d["outcome"] == "telemetry_only"
    assert d["attempts"] == 0
    assert d["candidates_offered"] == 2
    assert d["retry_enabled"] is False
    assert len(captured.of_kind(EventKind.RETRY_WOULD_DISPATCH)) == 1
    assert captured.of_kind(EventKind.RETRY_DISPATCHED) == []


# ─────────────────────────────────────────────────────────────────────
# Section 3 — the dispatched outcomes. ``dispatched = won + lost_*`` is the
# headline identity, and this is the only place it is bound to production.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_won_promotes_the_retry_result(captured: _CapturedEvents) -> None:
    """First candidate recovers real units → promoted, RETRY_SUCCESS emitted."""
    await _drive(
        baseline_units=[],
        baseline_tier="TIER_1_API_G5_EMPTY",
        html=_HTML_TWO_CANDIDATES,
        retry_table={
            "knock": AdapterResult(
                units=_UNIT_LEVEL, tier_used="TIER_1_API_KNOCK", errors=[]
            )
        },
    )

    d = captured.episode()
    assert d["outcome"] == "won"
    assert d["attempts"] == 1
    assert d["tried_pms"] == ["knock"]
    assert d["tried_adapters"] == ["knock"]
    assert d["won_pms"] == "knock"
    assert d["won_tier"] == "TIER_1_API_KNOCK"
    assert d["won_unit_count"] == 1
    assert d["baseline_restored"] is False
    assert len(captured.of_kind(EventKind.RETRY_SUCCESS)) == 1
    assert len(captured.of_kind(EventKind.RETRY_DISPATCHED)) == 1


@pytest.mark.asyncio
async def test_lost_max_retries_when_cap_is_hit_still_triggering(
    captured: _CapturedEvents,
) -> None:
    """Both candidates come back name-only, so the trigger never clears and
    the attempt cap is what stops the loop."""
    dud = AdapterResult(units=_NAME_ONLY, tier_used="TIER_3_DOM", errors=[])
    await _drive(
        baseline_units=[],
        baseline_tier="TIER_1_API_G5_EMPTY",
        html=_HTML_TWO_CANDIDATES,
        retry_table={"knock": dud, "rentcafe": dud},
    )

    d = captured.episode()
    assert d["outcome"] == "lost_max_retries"
    assert d["attempts"] == 2 == d["max_retries"]
    assert d["tried_pms"] == ["knock", "rentcafe"]
    assert d["final_trigger_reason"] == "quality_gate"
    assert d["won_pms"] == ""
    assert len(captured.of_kind(EventKind.RETRY_DISPATCHED)) == 2
    assert captured.of_kind(EventKind.RETRY_SUCCESS) == []


@pytest.mark.asyncio
async def test_lost_candidates_exhausted_when_pool_empties_under_the_cap(
    captured: _CapturedEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Budget of 3, only 2 candidates on the page: the pool runs dry first.

    Distinguishing this from ``lost_max_retries`` is what tells you whether
    to raise the cap or to improve detection — the two demand opposite fixes.
    """
    monkeypatch.setenv("PATH_B_MAX_RETRIES", "3")
    dud = AdapterResult(units=_NAME_ONLY, tier_used="TIER_3_DOM", errors=[])
    await _drive(
        baseline_units=[],
        baseline_tier="TIER_1_API_G5_EMPTY",
        html=_HTML_TWO_CANDIDATES,
        retry_table={"knock": dud, "rentcafe": dud},
    )

    d = captured.episode()
    assert d["outcome"] == "lost_candidates_exhausted"
    assert d["attempts"] == 2
    assert d["max_retries"] == 3
    assert 1 <= d["attempts"] < d["max_retries"]  # invariant A10


@pytest.mark.asyncio
async def test_lost_adapter_error_when_the_retry_adapter_raises(
    captured: _CapturedEvents,
) -> None:
    """A retry adapter blowing up is a LOSS, not an abort — the loop's own
    ``except Exception`` handles it and the scrape continues."""
    result = await _drive(
        baseline_units=[],
        baseline_tier="TIER_1_API_G5_EMPTY",
        html=_HTML_TWO_CANDIDATES,
        retry_table={"knock": RuntimeError("adapter exploded")},
    )

    d = captured.episode()
    assert d["outcome"] == "lost_adapter_error"
    assert d["error_type"] == "RuntimeError"
    assert d["attempts"] == 1
    assert d["tried_pms"] == ["knock"]
    assert result is not None


@pytest.mark.asyncio
async def test_lost_dead_end_when_the_trigger_stops_firing_without_a_win(
    captured: _CapturedEvents,
) -> None:
    """The retry returns nothing under a non-empty-exit tier: the trigger
    clears (no units to fault) but the win condition needs units, so the loop
    falls out of its condition having neither won nor exhausted anything.

    This exit is a loop-CONDITION falsification with no statement to hang an
    assignment on, which is why the terminal event is emitted from a
    ``finally`` rather than after the loop.
    """
    await _drive(
        baseline_units=[],
        baseline_tier="TIER_1_API_G5_EMPTY",
        html=_HTML_TWO_CANDIDATES,
        retry_table={
            "knock": AdapterResult(units=[], tier_used="TIER_3_DOM", errors=[])
        },
    )

    d = captured.episode()
    assert d["outcome"] == "lost_dead_end"
    assert d["attempts"] == 1
    assert d["attempts"] < d["max_retries"]
    assert d["final_trigger_reason"] == ""
    assert d["trigger_reason"] == "empty_exit"


# ─────────────────────────────────────────────────────────────────────
# Section 4 — the torn-down outcomes. THE "``finally`` COVERS EVERY EXIT"
# CLAIM LIVES OR DIES HERE.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aborted_error_is_emitted_and_the_scrape_survives(
    captured: _CapturedEvents,
) -> None:
    """An Exception in the loop region that is NOT the adapter's own extract.

    The block classifies and re-raises; the outer handler swallows it so the
    scrape survives, exactly as before this telemetry existed.
    """
    result = await _drive(
        baseline_units=[],
        baseline_tier="TIER_1_API_G5_EMPTY",
        html=_HTML_TWO_CANDIDATES,
        candidates_side_effect=RuntimeError("detector exploded"),
    )

    d = captured.episode()
    assert d["outcome"] == "aborted_error"
    assert d["error_type"] == "RuntimeError"
    assert d["attempts"] == 0
    # The candidate lookup itself raised, so it truthfully never completed.
    assert d["candidates_offered"] == -1
    assert result is not None


@pytest.mark.asyncio
async def test_aborted_cancelled_is_split_from_error_and_propagates(
    captured: _CapturedEvents,
) -> None:
    """A CancelledError mid-retry — the EXPECTED shape under jugnu's 600s
    ``asyncio.wait_for`` — must emit a terminal episode and still kill the
    coroutine.

    Two things are pinned. (1) The ``finally`` fires on a path that unwinds
    through NO handler: the per-attempt ``except Exception`` does not catch a
    BaseException and neither does the outer ``except Exception``, so an emit
    placed after the loop would miss this entirely and the property would
    vanish from the funnel. (2) It is classified apart from ``aborted_error``,
    or the "is the loop buggy?" gate would be permanently red from ordinary
    timeouts and therefore useless.
    """
    with pytest.raises(asyncio.CancelledError):
        await _drive(
            baseline_units=[],
            baseline_tier="TIER_1_API_G5_EMPTY",
            html=_HTML_TWO_CANDIDATES,
            retry_table={"knock": asyncio.CancelledError()},
        )

    d = captured.episode()
    assert d["outcome"] == "aborted_cancelled"
    assert d["error_type"] == "CancelledError"
    # Cancelled AFTER dispatching, so it belongs on the dispatched side of
    # the funnel split — which is why torn-down episodes are split by
    # ``attempts`` rather than assigned wholesale to one side.
    assert d["attempts"] == 1
    assert d["tried_pms"] == ["knock"]
    assert len(captured.of_kind(EventKind.RETRY_DISPATCHED)) == 1


@pytest.mark.asyncio
async def test_trigger_predicate_crash_is_trigger_error_not_setup_error(
    captured: _CapturedEvents,
) -> None:
    """One malformed unit row must not raise a run-wide outage alarm.

    ``_retry_trigger_reason`` walks every extracted row through
    ``property_has_rent_signal`` / ``property_has_area_signal`` /
    ``rows_are_plan_level``, all of which do ``unit.get(...)``. A single
    non-dict row raises AttributeError. While that call sat between the outer
    and the inner ``try`` its only handler was the outer one, which reports
    ``setup_error`` — documented as "retry is DEAD RUN-WIDE, page
    immediately" and PAGED on by ``retry_funnel.py``. One bad property would
    have declared a run-wide outage, and the ``setup_error`` payload
    hard-codes ``trigger_reason=""``, so the mislabelling was invisible on
    top of it.
    """
    result = await _drive(baseline_units=_MALFORMED, baseline_tier="TIER_1_API_ENTRATA")

    d = captured.episode()
    assert d["outcome"] == "trigger_error", "a per-property crash must not page run-wide"
    assert d["error_type"] == "AttributeError"
    assert d["attempts"] == 0
    assert d["candidates_offered"] == -1
    # Baseline facts survive: this property DID reach the decision point.
    assert d["baseline_pms"] == "g5"
    assert d["baseline_unit_count"] == 2
    # Runtime behaviour is unchanged: the exception is still swallowed by the
    # outer handler and the scrape still returns.
    assert result is not None


# ─────────────────────────────────────────────────────────────────────
# Section 5 — close the vocabulary against production.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_declared_outcome_is_reachable_from_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every member of ``RETRY_EPISODE_OUTCOMES`` is produced by the REAL
    ``scrape()``, and nothing outside the set is.

    This replaces a grep that could not fail: the previous version asserted
    that each outcome literal appears in scraper.py while iterating the
    frozenset that declares those very literals in that same file. Under a
    mutation where four ``lost_*`` outcomes were assigned from nowhere, it
    still passed.

    Self-contained (every scenario runs inside this one test) because the
    suite has order-dependent tests, and a set accumulated across test
    functions would silently weaken under ``-k`` or a shuffle.
    """
    dud = AdapterResult(units=_NAME_ONLY, tier_used="TIER_3_DOM", errors=[])
    empty = AdapterResult(units=[], tier_used="TIER_3_DOM", errors=[])
    good = AdapterResult(units=_UNIT_LEVEL, tier_used="TIER_1_API_KNOCK", errors=[])
    empty_exit = {"baseline_units": [], "baseline_tier": "TIER_1_API_G5_EMPTY",
                  "html": _HTML_TWO_CANDIDATES}

    # (expected outcome, env overrides, _drive kwargs, does scrape() raise?)
    scenarios: list[tuple[str, dict[str, str], dict[str, Any], bool]] = [
        ("not_triggered", {},
         {"baseline_units": _UNIT_LEVEL, "baseline_tier": "T"}, False),
        ("no_budget", {"PATH_B_MAX_RETRIES": "0"}, empty_exit, False),
        ("no_candidate", {},
         {"baseline_units": _PLAN_LEVEL, "baseline_tier": "T"}, False),
        ("telemetry_only", {"PATH_B_RETRY_ENABLED": "0"}, empty_exit, False),
        ("won", {}, {**empty_exit, "retry_table": {"knock": good}}, False),
        ("lost_max_retries", {},
         {**empty_exit, "retry_table": {"knock": dud, "rentcafe": dud}}, False),
        ("lost_candidates_exhausted", {"PATH_B_MAX_RETRIES": "3"},
         {**empty_exit, "retry_table": {"knock": dud, "rentcafe": dud}}, False),
        ("lost_adapter_error", {},
         {**empty_exit, "retry_table": {"knock": RuntimeError("boom")}}, False),
        ("lost_dead_end", {}, {**empty_exit, "retry_table": {"knock": empty}}, False),
        ("aborted_error", {},
         {**empty_exit, "candidates_side_effect": RuntimeError("boom")}, False),
        ("aborted_cancelled", {},
         {**empty_exit, "retry_table": {"knock": asyncio.CancelledError()}}, True),
        ("trigger_error", {},
         {"baseline_units": _MALFORMED, "baseline_tier": "T"}, False),
        ("setup_error", {"PATH_B_MAX_RETRIES": "two"},
         {"baseline_units": _UNIT_LEVEL, "baseline_tier": "T"}, False),
    ]

    seen: dict[str, str] = {}
    for expected, env, kwargs, expects_raise in scenarios:
        cap = _CapturedEvents()
        monkeypatch.setattr(_events_mod, "emit", cap)
        monkeypatch.setenv("PATH_B_RETRY_ENABLED", env.get("PATH_B_RETRY_ENABLED", "1"))
        monkeypatch.setenv("PATH_B_MAX_RETRIES", env.get("PATH_B_MAX_RETRIES", "2"))
        if expects_raise:
            with pytest.raises(asyncio.CancelledError):
                await _drive(**kwargs)
        else:
            await _drive(**kwargs)
        seen[expected] = str(cap.episode()["outcome"])

    wrong = {k: v for k, v in seen.items() if k != v}
    assert not wrong, (
        f"production assigned the wrong terminal outcome for these scenarios "
        f"(expected -> got): {wrong}"
    )
    assert set(seen.values()) == set(RETRY_EPISODE_OUTCOMES), (
        f"declared but never produced by production: "
        f"{sorted(set(RETRY_EPISODE_OUTCOMES) - set(seen.values()))}; "
        f"produced but not declared: "
        f"{sorted(set(seen.values()) - set(RETRY_EPISODE_OUTCOMES))}"
    )


# ─────────────────────────────────────────────────────────────────────
# Section 6 — BEHAVIOURAL PARITY between production and the test mirror.
#
# ``test_scraper_hook_kept_in_sync_with_test_helper`` is a substring grep. It
# cannot see a divergence in behaviour, and it demonstrably missed one: the
# 2026-07-25 ``plan_level_only`` trigger was added to production and never to
# the mirror, and the grep stayed green because the symbol still appeared in
# a comment. Both surviving mutations in the 2026-07-26 review were likewise
# pure production/mirror divergences that the grep did not notice.
#
# These tests are the real binding: identical input to both implementations,
# identical RETRY_EPISODE payload out. Divergence is a failure, not a comment.
# ─────────────────────────────────────────────────────────────────────

#: Fields whose meaning is identical in both implementations. Excluded:
#: ``episode_id`` (random) and ``scrape_url`` (envelope-ish).
_PARITY_FIELDS = (
    "outcome",
    "trigger_reason",
    "final_trigger_reason",
    "attempts",
    "candidates_offered",
    "baseline_pms",
    "baseline_tier",
    "baseline_unit_count",
    "baseline_error_count",
    "baseline_plan_level",
    "tried_pms",
    "tried_adapters",
    "won_pms",
    "won_tier",
    "won_unit_count",
    "baseline_restored",
    "error_type",
    "retry_enabled",
    "max_retries",
)

_PARITY_SCENARIOS = [
    ("not_triggered", _UNIT_LEVEL, "TIER_1_API_ENTRATA", _HTML_NO_CANDIDATE, {}, True),
    ("no_candidate", _PLAN_LEVEL, "TIER_1_API_ENTRATA", _HTML_NO_CANDIDATE, {}, True),
    (
        "won",
        [],
        "TIER_1_API_G5_EMPTY",
        _HTML_TWO_CANDIDATES,
        {"knock": ("TIER_1_API_KNOCK", _UNIT_LEVEL)},
        True,
    ),
    (
        "lost_max_retries",
        [],
        "TIER_1_API_G5_EMPTY",
        _HTML_TWO_CANDIDATES,
        {"knock": ("TIER_3_DOM", _NAME_ONLY), "rentcafe": ("TIER_3_DOM", _NAME_ONLY)},
        True,
    ),
    (
        "lost_dead_end",
        [],
        "TIER_1_API_G5_EMPTY",
        _HTML_TWO_CANDIDATES,
        {"knock": ("TIER_3_DOM", [])},
        True,
    ),
    ("telemetry_only", [], "TIER_1_API_G5_EMPTY", _HTML_TWO_CANDIDATES, {}, False),
    # The plan-level restore path: baseline has rows that fail the quality
    # gate, every retry dead-ends, so the baseline is put back and stamped.
    (
        "plan_level_restore",
        _NAME_ONLY,
        "TIER_2_JSONLD",
        _HTML_TWO_CANDIDATES,
        {"knock": ("TIER_3_DOM", []), "rentcafe": ("TIER_3_DOM", [])},
        True,
    ),
    # The predicate crash. Both sides must classify it as trigger_error and
    # still emit exactly one episode from the ``finally``.
    ("trigger_error", _MALFORMED, "TIER_1_API_ENTRATA", _HTML_NO_CANDIDATE, {}, True),
]


@pytest.mark.parametrize(
    ("label", "units", "tier", "html", "table", "enabled"), _PARITY_SCENARIOS
)
@pytest.mark.asyncio
async def test_mirror_matches_production_payload(
    label: str,
    units: list[Any],
    tier: str,
    html: str,
    table: dict[str, tuple[str, list[Any]]],
    enabled: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror and ``scrape()`` emit the same episode for the same input."""
    from tests.pms.test_path_b_retry_telemetry import (
        _Ctx,
        _run_retry_loop_under_test,
        _StubAdapter,
        _StubAdapterResult,
    )

    monkeypatch.setenv("PATH_B_RETRY_ENABLED", "1" if enabled else "0")
    monkeypatch.setenv("PATH_B_MAX_RETRIES", "2")

    prod_cap = _CapturedEvents()
    monkeypatch.setattr(_events_mod, "emit", prod_cap)
    await _drive(
        baseline_units=list(units),
        baseline_tier=tier,
        html=html,
        retry_table={
            pms: AdapterResult(units=list(u), tier_used=t, errors=[])
            for pms, (t, u) in table.items()
        },
    )
    produced = prod_cap.episode()

    mirror_cap = _CapturedEvents()
    monkeypatch.setattr(_events_mod, "emit", mirror_cap)
    try:
        await _run_retry_loop_under_test(
            initial_adapter_name="g5",
            initial_result=_StubAdapterResult(tier_used=tier, units=list(units)),
            page_html=html,
            ctx=_Ctx(base_url="https://example.com/", property_id="P-real-001"),
            adapter_table={
                pms: _StubAdapter(pms, _StubAdapterResult(tier_used=t, units=list(u)))
                for pms, (t, u) in table.items()
            },
            enabled=enabled,
            max_retries=2,
        )
    except BaseException:  # noqa: BLE001 — see below
        # The mirror models only the INNER block; production wraps it in an
        # outer ``except Exception`` that swallows so a scrape never dies of
        # a retry bug. Standing in for that handler here keeps the comparison
        # about the PAYLOAD rather than about the propagation shape. Nothing
        # is hidden: ``.episode()`` below asserts exactly one event, which
        # fails if the mirror died before reaching its ``finally``.
        pass
    mirrored = mirror_cap.episode()

    diffs = {
        f: (produced.get(f), mirrored.get(f))
        for f in _PARITY_FIELDS
        if produced.get(f) != mirrored.get(f)
    }
    assert not diffs, (
        f"[{label}] production and the test mirror have DRIFTED "
        f"(production, mirror): {diffs}"
    )
