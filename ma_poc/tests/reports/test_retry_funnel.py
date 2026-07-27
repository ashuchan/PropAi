"""The retry-funnel aggregator must actually catch an open funnel.

An invariant nothing evaluates is a comment. These tests feed synthetic
ledgers to ``ma_poc.scripts.reports.retry_funnel`` and assert that a closed
funnel passes and each specific breakage is REPORTED — including the exact
shape seen on the real 2026-07-16 ledger, where 115 dispatches produced 9
successes and 106 dangling attempts with no terminal event at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ma_poc.scripts.reports.retry_funnel import (
    check_invariants,
    compute_funnel,
    iter_event_files,
    load_events,
    main,
    render,
)


def _episode(**over: Any) -> dict[str, Any]:
    """A valid ``not_triggered`` episode; override fields per scenario."""
    base: dict[str, Any] = {
        "kind": "extract.retry_episode",
        "property_id": "P1",
        "episode_id": "ep0000000000000a",
        "scrape_url": "https://example.com/",
        "outcome": "not_triggered",
        "trigger_reason": "",
        "final_trigger_reason": "",
        "attempts": 0,
        "candidates_offered": -1,
        "baseline_pms": "knock",
        "baseline_tier": "TIER_1_API_KNOCK",
        "baseline_unit_count": 3,
        "baseline_error_count": 0,
        "baseline_plan_level": False,
        "tried_pms": [],
        "tried_adapters": [],
        "won_pms": "",
        "won_tier": "",
        "won_unit_count": -1,
        "baseline_restored": False,
        "error_type": "",
        "retry_enabled": True,
        "max_retries": 2,
    }
    base.update(over)
    return base


def _detector_signal(pid: str) -> dict[str, Any]:
    """One ``scrape()`` call reached the detector. Emitted UPSTREAM of both
    the FAILED_UNREACHABLE return and the baseline ``adapter.extract``, so
    ``detector_signals - episodes`` is the count of scrape() calls that never
    reached the retry block."""
    return {"kind": "extract.detector_signals", "property_id": pid}


def _dispatch(episode_id: str, attempt: int, pid: str = "P1") -> dict[str, Any]:
    return {
        "kind": "extract.retry_dispatched",
        "property_id": pid,
        "episode_id": episode_id,
        "attempt": attempt,
        "trigger_reason": "empty_exit",
        "initial_trigger_reason": "empty_exit",
        "next_pms": "knock",
    }


def _success(episode_id: str, pid: str = "P1") -> dict[str, Any]:
    return {
        "kind": "extract.retry_success",
        "property_id": pid,
        "episode_id": episode_id,
        "attempt": 1,
        "won_pms": "knock",
        "unit_count": 4,
    }


def _write(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )


_WON = _episode(
    episode_id="ep0000000000000b",
    property_id="P2",
    outcome="won",
    trigger_reason="empty_exit",
    final_trigger_reason="empty_exit",
    attempts=1,
    candidates_offered=2,
    baseline_unit_count=0,
    tried_pms=["knock"],
    tried_adapters=["knock"],
    won_pms="knock",
    won_tier="TIER_1_API_KNOCK",
    won_unit_count=4,
)
_NO_CANDIDATE = _episode(
    episode_id="ep0000000000000c",
    property_id="P3",
    outcome="no_candidate",
    trigger_reason="plan_level_only",
    final_trigger_reason="plan_level_only",
    attempts=0,
    candidates_offered=0,
    baseline_plan_level=True,
)


def test_closed_funnel_passes(tmp_path: Path) -> None:
    """One won + one no_candidate + one not_triggered, all reconciled."""
    events = [
        _episode(),
        _WON,
        _NO_CANDIDATE,
        _dispatch("ep0000000000000b", 1, "P2"),
        _success("ep0000000000000b", "P2"),
    ]
    _write(tmp_path / "shard_0" / "events.jsonl", events)
    funnel = compute_funnel(load_events(iter_event_files(tmp_path)))

    assert funnel["episodes"] == 3
    assert funnel["triggered"] == 2
    assert funnel["dispatched"] == 1
    assert funnel["won"] == 1
    assert funnel["dangling"] == 0
    assert check_invariants(funnel) == []
    assert main([str(tmp_path)]) == 0


def test_dangling_dispatch_fails_b1(tmp_path: Path) -> None:
    """THE 2026-07-16 SHAPE: a dispatch with no terminal event.

    106 of these were invisible on the real ledger. This is the single
    number that proves the funnel closed, so it must be loud.
    """
    events = [
        _episode(),
        _dispatch("ep-orphan-0000000", 1),
    ]
    _write(tmp_path / "shard_0" / "events.jsonl", events)
    funnel = compute_funnel(load_events(iter_event_files(tmp_path)))

    assert funnel["dangling"] == 1
    violations = check_invariants(funnel)
    assert any(x.startswith("B1/D6") for x in violations), violations
    assert any("unknown episode_id" in x for x in violations), violations
    assert main([str(tmp_path)]) == 1


def test_unknown_outcome_fails_vocabulary(tmp_path: Path) -> None:
    """An unrecognised outcome still lands in the episode count and trips
    the vocabulary check — the opposite failure mode from an unrecognised
    EventKind, which every literal-string consumer drops silently."""
    events = [_episode(outcome="banana", trigger_reason="empty_exit")]
    _write(tmp_path / "shard_0" / "events.jsonl", events)
    funnel = compute_funnel(load_events(iter_event_files(tmp_path)))

    assert funnel["episodes"] == 1  # counted, not dropped
    violations = check_invariants(funnel)
    assert any("banana" in x for x in violations), violations
    assert main([str(tmp_path)]) == 1


def test_shards_are_unioned(tmp_path: Path) -> None:
    """Each Cloud Run shard writes its OWN ledger and nothing merges them —
    reading one file silently analyses a twentieth of the run."""
    _write(tmp_path / "shard_0" / "events.jsonl", [_episode()])
    _write(
        tmp_path / "shard_1" / "events.jsonl",
        [_WON, _dispatch("ep0000000000000b", 1, "P2"), _success("ep0000000000000b", "P2")],
    )
    paths = iter_event_files(tmp_path)
    assert len(paths) == 2
    funnel = compute_funnel(load_events(paths))
    assert funnel["episodes"] == 2
    assert funnel["dispatched"] == 1
    assert check_invariants(funnel) == []


def test_open_funnel_a5_is_reported(tmp_path: Path) -> None:
    """An episode that dispatched but claims an undispatched outcome is
    exactly "the funnel is open" — A5 must name it."""
    bad = _episode(
        episode_id="ep0000000000000d",
        outcome="no_candidate",
        trigger_reason="empty_exit",
        attempts=1,
        candidates_offered=0,
        tried_pms=["knock"],
        tried_adapters=["knock"],
    )
    _write(
        tmp_path / "shard_0" / "events.jsonl",
        [bad, _dispatch("ep0000000000000d", 1)],
    )
    funnel = compute_funnel(load_events(iter_event_files(tmp_path)))
    violations = check_invariants(funnel)
    assert any("THE FUNNEL IS OPEN" in x for x in violations), violations


def test_setup_error_pages(tmp_path: Path) -> None:
    """D1 — a single setup_error means retry is dead for the whole run."""
    _write(
        tmp_path / "shard_0" / "events.jsonl",
        [_episode(outcome="setup_error", error_type="ValueError", max_retries=0)],
    )
    funnel = compute_funnel(load_events(iter_event_files(tmp_path)))
    violations = check_invariants(funnel)
    assert any(x.startswith("D1") for x in violations), violations


def test_truncated_final_line_is_tolerated(tmp_path: Path) -> None:
    """A SIGKILL mid-flush leaves a partial JSON line. That must not crash
    the aggregator — it surfaces as dangling on the affected shard."""
    p = tmp_path / "shard_0" / "events.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text(
        json.dumps(_episode()) + "\n" + '{"kind": "extract.retry_epi',
        encoding="utf-8",
    )
    funnel = compute_funnel(load_events(iter_event_files(tmp_path)))
    assert funnel["episodes"] == 1
    assert check_invariants(funnel) == []


def test_no_episodes_exits_nonzero(tmp_path: Path) -> None:
    """Zero episodes is now a DIAGNOSTIC state, not an ambiguous one: it can
    only mean the hook did not run."""
    _write(tmp_path / "shard_0" / "events.jsonl", [{"kind": "fetch.started"}])
    assert main([str(tmp_path)]) == 1


def test_per_property_rollup_undilutes_the_link_hop_recursion(
    tmp_path: Path,
) -> None:
    """THE POST-MORTEM QUESTION: for how many PROPERTIES did the trigger fire?

    ``scrape()`` recurses for link-hop sub-pages under the SAME property_id, so
    one property can contribute many episodes — 3.73 on average and up to 31
    on the real 2026-07-16 ledger, with 43% of properties above one. An
    episode-denominated trigger rate is diluted by exactly that factor, and a
    single property can stuff the denominator with ``not_triggered`` rows.

    Here P1 emits three episodes, one of which triggers; P2 emits one that
    does not. Per EPISODE that is 1/4 = 25%. Per PROPERTY it is 1/2 = 50%.
    """
    events = [
        _episode(property_id="P1", episode_id="ep000000000000p1"),
        _episode(property_id="P1", episode_id="ep000000000000p2"),
        _episode(
            property_id="P1",
            episode_id="ep000000000000p3",
            outcome="no_candidate",
            trigger_reason="plan_level_only",
            final_trigger_reason="plan_level_only",
            candidates_offered=0,
            baseline_plan_level=True,
        ),
        _episode(property_id="P2", episode_id="ep000000000000p4"),
    ]
    _write(tmp_path / "shard_0" / "events.jsonl", events)
    funnel = compute_funnel(load_events(iter_event_files(tmp_path)))

    assert funnel["episodes"] == 4
    assert funnel["properties"] == 2
    assert funnel["episodes_per_property"] == 2.0
    assert funnel["properties_with_trigger"] == 1
    assert funnel["per_trigger_properties"] == {"plan_level_only": 1}
    # The two denominators genuinely disagree — that is the finding.
    assert funnel["trigger_rate_per_episode"] == 0.25
    assert funnel["trigger_rate_per_property"] == 0.5
    assert check_invariants(funnel) == []

    out = render(funnel)
    assert "(per episode)" in out and "(per property)" in out
    assert "PER PROPERTY" in out


def test_unreached_scrapes_are_printed_not_inferred(tmp_path: Path) -> None:
    """"CLOSED" is a claim about EPISODES, not about every property.

    A scrape() call cancelled while awaiting the BASELINE ``adapter.extract``
    — where nearly all wall-clock under jugnu's 600s ``asyncio.wait_for``
    sits — escapes through ``except Exception`` and never reaches the retry
    block, so it emits NO episode and no invariant here can see it. Neither
    can D3, whose ``aborted_cancelled`` gate only covers cancellation INSIDE
    the loop. The gap is therefore printed as its own line rather than left
    to be inferred from a silence.
    """
    events: list[dict[str, Any]] = [_episode()]
    events += [_detector_signal("P1"), _detector_signal("P7"), _detector_signal("P8")]
    _write(tmp_path / "shard_0" / "events.jsonl", events)
    funnel = compute_funnel(load_events(iter_event_files(tmp_path)))

    assert funnel["episodes"] == 1
    assert funnel["detector_signal_events"] == 3
    assert funnel["unreached_scrapes"] == 2
    # Still "closed" — which is exactly why the gap must be stated out loud.
    assert check_invariants(funnel) == []
    assert "never reached the block" in render(funnel)


def test_trigger_error_is_counted_separately_and_does_not_page_run_wide(
    tmp_path: Path,
) -> None:
    """D2b, not D1. A crash in the per-property trigger predicate must not
    read as "retry is dead run-wide"; those demand opposite responses.

    It is also excluded from ``triggered``: the FIRST evaluation crashed, so
    no trigger was ever resolved and A3 (blank trigger_reason) still holds.
    """
    _write(
        tmp_path / "shard_0" / "events.jsonl",
        [
            _episode(
                episode_id="ep00000000000te1",
                outcome="trigger_error",
                error_type="AttributeError",
            )
        ],
    )
    funnel = compute_funnel(load_events(iter_event_files(tmp_path)))

    assert funnel["episodes"] == 1
    assert funnel["triggered"] == 0
    assert funnel["trigger_error_undispatched"] == 1
    violations = check_invariants(funnel)
    assert any(x.startswith("D2b") for x in violations), violations
    assert not any(x.startswith("D1") for x in violations), violations
    assert not any(x.startswith("A3") for x in violations), violations


def test_trigger_error_after_a_dispatch_stays_on_the_dispatched_side(
    tmp_path: Path,
) -> None:
    """A roll-forward predicate crash DID trigger and DID dispatch, so it must
    keep A5 (``dispatched == won + lost_* + torn_down``) closed — which is why
    torn-down outcomes are split by ``attempts`` rather than assigned
    wholesale to one side of the funnel."""
    ep = _episode(
        episode_id="ep00000000000te2",
        outcome="trigger_error",
        error_type="AttributeError",
        trigger_reason="empty_exit",
        final_trigger_reason="empty_exit",
        attempts=1,
        candidates_offered=2,
        tried_pms=["knock"],
        tried_adapters=["knock"],
    )
    _write(
        tmp_path / "shard_0" / "events.jsonl",
        [ep, _dispatch("ep00000000000te2", 1)],
    )
    funnel = compute_funnel(load_events(iter_event_files(tmp_path)))

    assert funnel["triggered"] == 1
    assert funnel["dispatched"] == 1
    violations = check_invariants(funnel)
    assert not any("THE FUNNEL IS OPEN" in x for x in violations), violations
    assert not any(x.startswith(("A3", "A4")) for x in violations), violations


def test_c3_baseline_restored_checked_against_properties(tmp_path: Path) -> None:
    """C3 — a restored baseline must carry the SUCCESS_PLAN_LEVEL verdict."""
    ep = _episode(
        episode_id="ep0000000000000e",
        property_id="P9",
        outcome="lost_max_retries",
        trigger_reason="no_rent",
        final_trigger_reason="no_rent",
        attempts=2,
        candidates_offered=2,
        tried_pms=["knock", "rentcafe"],
        tried_adapters=["knock", "rentcafe"],
        baseline_restored=True,
    )
    _write(
        tmp_path / "shard_0" / "events.jsonl",
        [ep, _dispatch("ep0000000000000e", 1, "P9"), _dispatch("ep0000000000000e", 2, "P9")],
    )
    props = tmp_path / "properties.json"
    props.write_text(
        json.dumps([{"property_id": "P9", "_verdict_quality": "SUCCESS"}]),
        encoding="utf-8",
    )
    assert main([str(tmp_path), "--properties-json", str(props)]) == 1

    props.write_text(
        json.dumps(
            [{"property_id": "P9", "_verdict_quality": "SUCCESS_PLAN_LEVEL"}]
        ),
        encoding="utf-8",
    )
    assert main([str(tmp_path), "--properties-json", str(props)]) == 0
