"""Path-B/C retry funnel — print it, and assert that it closes.

Nothing in this repo read ``extract.retry_dispatched`` /
``extract.retry_success`` / ``extract.retry_would_dispatch``: grep found them
only at their enum definition, their emit sites and one test file. That was
the SECOND, independent reason the 2026-07-26 plan-cohort post-mortem could
not answer "did the plan_level_only retry trigger ever fire?" — even a closed
funnel is inert unless something executes the arithmetic.

This is that something. It unions every shard ledger in a run, prints the
funnel, and exits non-zero when an invariant fails.

The headline number is ``dangling``::

    dangling = count(RETRY_DISPATCHED) - sum(episode.attempts)

On the real 2026-07-16 ledger it was 106 (115 dispatches, 9 successes, no
terminal event for the rest). It must now be 0.

Usage::

    python -m ma_poc.scripts.reports.retry_funnel data/v2/runs/2026-07-26
    python -m ma_poc.scripts.reports.retry_funnel <run_dir> --properties-json out.json
    python -m ma_poc.scripts.reports.retry_funnel <run_dir> --json

Episodes, not properties: ``scrape()`` recurses for link-hop sub-pages with
the SAME property_id (one pid in the 2026-07-16 ledger carried 13 dispatches),
so every join here is on ``episode_id``. Because a per-episode rate is not the
number anyone asks for — "for how many PROPERTIES did plan_level_only fire?" —
``compute_funnel`` also emits a per-property rollup, and ``render`` prints both
blocks with every rate explicitly labelled. On the 2026-07-16 ledger the mean
was 3.73 episodes per property, so the two blocks differ by up to ~3.7x.

Scope of "CLOSED": the arithmetic closes over the EPISODES in the ledger. A
``scrape()`` call that returns FAILED_UNREACHABLE, or whose baseline
``adapter.extract`` is cancelled by jugnu's 600s ``asyncio.wait_for``, never
reaches the retry block and emits no episode at all — so no invariant here can
see it. That gap (``detector_signals - episodes``) is printed as its own line.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ma_poc.pms.scraper import RETRY_EPISODE_OUTCOMES

EPISODE_KIND = "extract.retry_episode"
DISPATCHED_KIND = "extract.retry_dispatched"
SUCCESS_KIND = "extract.retry_success"
WOULD_DISPATCH_KIND = "extract.retry_would_dispatch"
DETECTOR_SIGNALS_KIND = "extract.detector_signals"

TRIGGER_REASONS = frozenset(
    {"empty_exit", "quality_gate", "no_rent", "no_area", "plan_level_only"}
)

#: Outcomes reached with at least one dispatch.
DISPATCHED_OUTCOMES = frozenset(
    {
        "won",
        "lost_candidates_exhausted",
        "lost_adapter_error",
        "lost_dead_end",
        "lost_max_retries",
    }
)
#: Outcomes where the loop was never entered.
UNDISPATCHED_OUTCOMES = frozenset(
    {"not_triggered", "no_budget", "no_candidate", "telemetry_only", "setup_error"}
)
ABORTED_OUTCOMES = frozenset({"aborted_error", "aborted_cancelled"})
#: The trigger predicate itself raised on this property's rows. Per-property,
#: NOT run-wide — the whole reason it is split out of ``setup_error``, which
#: pages. Lands on either side of the dispatch split: ``attempts == 0`` means
#: the initial evaluation crashed, ``attempts >= 1`` a roll-forward one.
TRIGGER_ERROR = "trigger_error"
#: Outcomes that tear the block down mid-flight. They can carry any number of
#: attempts, so the funnel arithmetic splits them by ``attempts`` rather than
#: assigning them wholesale to the dispatched or undispatched side.
TORN_DOWN_OUTCOMES = ABORTED_OUTCOMES | {TRIGGER_ERROR}
#: Outcomes that never reached the candidate lookup (candidates_offered == -1).
NEVER_LOOKED_OUTCOMES = frozenset({"not_triggered", "no_budget", "setup_error"})


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------


def iter_event_files(root: Path) -> list[Path]:
    """Return every ``events.jsonl`` under *root*.

    Each Cloud Run shard writes its OWN ledger and uploads it separately;
    nothing merges them, so a single-file read silently analyses one twentieth
    of a run.

    Args:
        root: a run directory, a shard directory, or a path to an events.jsonl.

    Returns:
        Sorted list of ledger paths (possibly empty).
    """
    if root.is_file():
        return [root]
    found = sorted(root.glob("**/events.jsonl"))
    return found


def load_events(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Parse newline-delimited JSON events, tolerating a truncated tail.

    A SIGKILL/OOM can cut the final line mid-object because ``EventLedger``
    buffers 16 events. That shows up downstream as ``dangling > 0`` on the
    affected shard and must not be confused with a telemetry bug.

    Args:
        paths: ledger files to read.

    Returns:
        All successfully parsed event dicts, in file order.

    Raises:
        Nothing — unreadable files and unparseable lines are skipped.
    """
    out: list[dict[str, Any]] = []
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ---------------------------------------------------------------------------
# Funnel
# ---------------------------------------------------------------------------


def compute_funnel(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce a raw event stream to the retry funnel.

    Args:
        events: parsed events from one or more shard ledgers.

    Returns:
        A dict with counts, per-dimension splits and the reconciliation
        figures (``dangling``, ``dispatched_events``, ...).
    """
    episodes = [e for e in events if e.get("kind") == EPISODE_KIND]
    dispatched_evs = [e for e in events if e.get("kind") == DISPATCHED_KIND]
    success_evs = [e for e in events if e.get("kind") == SUCCESS_KIND]
    would_evs = [e for e in events if e.get("kind") == WOULD_DISPATCH_KIND]
    detector_evs = [e for e in events if e.get("kind") == DETECTOR_SIGNALS_KIND]

    by_outcome: Counter = Counter(str(e.get("outcome")) for e in episodes)
    by_trigger: Counter = Counter(str(e.get("trigger_reason") or "") for e in episodes)
    by_baseline_pms: Counter = Counter(
        str(e.get("baseline_pms") or "") for e in episodes
    )

    dispatched_eps = [e for e in episodes if int(e.get("attempts") or 0) >= 1]
    total = len(episodes)
    not_triggered = by_outcome.get("not_triggered", 0)
    setup_error = by_outcome.get("setup_error", 0)
    # A trigger_error with attempts == 0 crashed evaluating the FIRST trigger,
    # so it never resolved one and cannot be counted as triggered. One with
    # attempts >= 1 crashed on a roll-forward evaluation, so it did trigger and
    # did dispatch; it stays in both numerators.
    trigger_error_undispatched = sum(
        1
        for e in episodes
        if e.get("outcome") == TRIGGER_ERROR and int(e.get("attempts") or 0) == 0
    )
    triggered = total - not_triggered - setup_error - trigger_error_undispatched
    won = by_outcome.get("won", 0)

    # Win rate per trigger — the question the mis-attributed RETRY_SUCCESS
    # payload could not answer before ``initial_trigger_reason`` was carried.
    per_trigger: dict[str, dict[str, int]] = defaultdict(
        lambda: {"episodes": 0, "dispatched": 0, "won": 0}
    )
    for e in episodes:
        t = str(e.get("trigger_reason") or "(none)")
        per_trigger[t]["episodes"] += 1
        if int(e.get("attempts") or 0) >= 1:
            per_trigger[t]["dispatched"] += 1
        if e.get("outcome") == "won":
            per_trigger[t]["won"] += 1

    attempts_sum = sum(int(e.get("attempts") or 0) for e in episodes)

    # --- PER-PROPERTY ROLLUP -------------------------------------------------
    # Every number above is denominated in EPISODES, and an episode is one
    # ``scrape()`` call, not one property: ``scrape()`` recurses for link-hop
    # sub-pages carrying the SAME property_id. On the real 2026-07-16 ledger
    # that is 664 scrape() calls over 178 properties — mean 3.73, max 31, and
    # 43% of properties with more than one. So an episode-denominated trigger
    # rate is diluted up to ~3.7x, and a single property can contribute 31
    # ``not_triggered`` rows to the denominator.
    #
    # The 2026-07-26 post-mortem question was "for how many PROPERTIES did
    # plan_level_only fire?" — that is this block, not the one above.
    eps_by_pid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in episodes:
        eps_by_pid[str(e.get("property_id") or "")].append(e)
    prop_trigger: Counter = Counter()
    props_with_trigger = 0
    props_with_dispatch = 0
    props_won = 0
    for pid_eps in eps_by_pid.values():
        triggers = {
            str(e.get("trigger_reason") or "") for e in pid_eps
        } - {""}
        if triggers:
            props_with_trigger += 1
        for t in triggers:
            prop_trigger[t] += 1
        if any(int(e.get("attempts") or 0) >= 1 for e in pid_eps):
            props_with_dispatch += 1
        if any(e.get("outcome") == "won" for e in pid_eps):
            props_won += 1
    n_props = len(eps_by_pid)

    # scrape() calls that emitted detector signals but never reached the retry
    # block at all. NOT a subset of any outcome — these properties produce ZERO
    # episodes, so no invariant here can see them. See ``render``.
    unreached = max(0, len(detector_evs) - total)

    return {
        "episodes": total,
        "properties": n_props,
        "properties_with_trigger": props_with_trigger,
        "properties_with_dispatch": props_with_dispatch,
        "properties_won": props_won,
        "per_trigger_properties": dict(prop_trigger),
        "episodes_per_property": (total / n_props) if n_props else 0.0,
        "unreached_scrapes": unreached,
        "triggered": triggered,
        "trigger_error_undispatched": trigger_error_undispatched,
        "dispatched": len(dispatched_eps),
        "won": won,
        "by_outcome": dict(by_outcome),
        "by_trigger": dict(by_trigger),
        "by_baseline_pms": dict(by_baseline_pms.most_common(15)),
        "per_trigger": {k: dict(v) for k, v in per_trigger.items()},
        "attempts_sum": attempts_sum,
        "dispatched_events": len(dispatched_evs),
        "success_events": len(success_evs),
        "would_dispatch_events": len(would_evs),
        "detector_signal_events": len(detector_evs),
        "dangling": len(dispatched_evs) - attempts_sum,
        # PER EPISODE. Named ``*_rate_per_episode`` because the unqualified
        # names read as per-property and are diluted by the link-hop recursion
        # (3.73x on the 2026-07-16 ledger). The per-property twins are below.
        "trigger_rate_per_episode": (1 - not_triggered / total) if total else 0.0,
        "dispatch_rate_per_episode": (
            (len(dispatched_eps) / triggered) if triggered else 0.0
        ),
        "win_rate_per_episode": (
            (won / len(dispatched_eps)) if dispatched_eps else 0.0
        ),
        "trigger_rate_per_property": (
            (props_with_trigger / n_props) if n_props else 0.0
        ),
        "dispatch_rate_per_property": (
            (props_with_dispatch / props_with_trigger) if props_with_trigger else 0.0
        ),
        "win_rate_per_property": (
            (props_won / props_with_dispatch) if props_with_dispatch else 0.0
        ),
        # Retained for the invariant pass.
        "_episodes": episodes,
        "_dispatched_evs": dispatched_evs,
        "_success_evs": success_evs,
        "_would_evs": would_evs,
    }


def check_invariants(funnel: dict[str, Any]) -> list[str]:
    """Assert sets A, B and D from the closed-funnel design.

    Args:
        funnel: the output of :func:`compute_funnel`.

    Returns:
        A list of human-readable violations. Empty means the funnel closed.
    """
    v: list[str] = []
    eps: list[dict[str, Any]] = funnel["_episodes"]
    dispatched_evs: list[dict[str, Any]] = funnel["_dispatched_evs"]

    # A1 — one episode, one id.
    ids = [e.get("episode_id") for e in eps]
    if len(ids) != len(set(ids)):
        v.append(f"A1: duplicate episode_id ({len(ids)} events, {len(set(ids))} ids)")
    if any(not i for i in ids):
        v.append("A1: episode(s) with an empty episode_id — cannot be joined")

    # A2 / D4 — closed vocabulary.
    for outcome, n in funnel["by_outcome"].items():
        if outcome not in RETRY_EPISODE_OUTCOMES:
            v.append(f"A2/D4: unknown outcome {outcome!r} x{n}")
    for trig, n in funnel["by_trigger"].items():
        if trig and trig not in TRIGGER_REASONS:
            v.append(f"D4: unknown trigger_reason {trig!r} x{n}")

    by = funnel["by_outcome"]
    # Torn-down episodes (both aborts + trigger_error) can land on either side
    # of the dispatch split, so split them by ``attempts`` rather than
    # assigning them wholesale.
    torn_no_dispatch = sum(
        1
        for e in eps
        if e.get("outcome") in TORN_DOWN_OUTCOMES and int(e.get("attempts") or 0) == 0
    )
    torn_dispatch = sum(
        1
        for e in eps
        if e.get("outcome") in TORN_DOWN_OUTCOMES and int(e.get("attempts") or 0) >= 1
    )
    trigger_error_no_dispatch = sum(
        1
        for e in eps
        if e.get("outcome") == TRIGGER_ERROR and int(e.get("attempts") or 0) == 0
    )

    # A3 — trigger_reason "" exactly covers the outcomes that never resolved
    # one: not_triggered, setup_error, and a trigger_error whose FIRST
    # evaluation crashed (attempts == 0). A roll-forward trigger_error did
    # resolve an initial trigger, so it carries a non-blank reason.
    blank_trigger = funnel["by_trigger"].get("", 0)
    expected_blank = (
        by.get("not_triggered", 0)
        + by.get("setup_error", 0)
        + trigger_error_no_dispatch
    )
    if blank_trigger != expected_blank:
        v.append(
            f"A3: blank trigger_reason={blank_trigger} but "
            f"not_triggered+setup_error+trigger_error(attempts=0)={expected_blank}"
        )

    # A4 — every triggered episode is accounted for.
    a4 = (
        by.get("no_budget", 0)
        + by.get("no_candidate", 0)
        + by.get("telemetry_only", 0)
        + funnel["dispatched"]
        + torn_no_dispatch
        - trigger_error_no_dispatch  # excluded from ``triggered`` by definition
    )
    if a4 != funnel["triggered"]:
        v.append(f"A4: triggered={funnel['triggered']} but accounted={a4}")

    # A5 — THE HEADLINE.
    a5 = sum(by.get(o, 0) for o in DISPATCHED_OUTCOMES) + torn_dispatch
    if a5 != funnel["dispatched"]:
        v.append(
            f"A5: dispatched={funnel['dispatched']} but "
            f"won+lost+aborted={a5} — THE FUNNEL IS OPEN"
        )

    # A6–A13 — per-episode shape.
    for e in eps:
        eid = e.get("episode_id")
        outcome = str(e.get("outcome"))
        attempts = int(e.get("attempts") or 0)
        max_retries = int(e.get("max_retries") or 0)
        offered = int(e.get("candidates_offered", -1))
        tried_pms = e.get("tried_pms") or []
        tried_adapters = e.get("tried_adapters") or []

        if not 0 <= attempts <= max_retries:
            v.append(f"A6[{eid}]: attempts={attempts} max_retries={max_retries}")
        if len(tried_pms) != attempts:
            v.append(f"A7[{eid}]: len(tried_pms)={len(tried_pms)} attempts={attempts}")
        if outcome not in TORN_DOWN_OUTCOMES and len(tried_adapters) != attempts:
            v.append(
                f"A7[{eid}]: len(tried_adapters)={len(tried_adapters)} "
                f"attempts={attempts}"
            )
        if (outcome == "no_candidate") != (attempts == 0 and offered == 0):
            v.append(f"A8[{eid}]: outcome={outcome} attempts={attempts} offered={offered}")
        # A9 — the -1 sentinel. Aborts are exempt in the ⟸ direction: an
        # exception raised BY the candidate lookup truthfully never completed
        # it, exactly as in A7.
        if offered == -1 and outcome not in NEVER_LOOKED_OUTCOMES | TORN_DOWN_OUTCOMES:
            v.append(f"A9[{eid}]: candidates_offered=-1 with outcome={outcome}")
        if outcome in NEVER_LOOKED_OUTCOMES and offered != -1:
            v.append(f"A9[{eid}]: outcome={outcome} but candidates_offered={offered}")
        if outcome == "lost_max_retries" and attempts != max_retries:
            v.append(f"A10[{eid}]: lost_max_retries with attempts={attempts}")
        if outcome == "lost_candidates_exhausted" and not 1 <= attempts < max_retries:
            v.append(f"A10[{eid}]: lost_candidates_exhausted with attempts={attempts}")
        if (outcome == "won") != bool(e.get("won_pms")):
            v.append(f"A11[{eid}]: outcome={outcome} won_pms={e.get('won_pms')!r}")
        if outcome == "won":
            if int(e.get("won_unit_count", -1)) < 1 or not e.get("won_tier"):
                v.append(f"A11[{eid}]: won but empty won_tier/won_unit_count")
            if tried_adapters and e.get("won_pms") != tried_adapters[-1]:
                v.append(f"A11[{eid}]: won_pms != tried_adapters[-1]")
        elif e.get("won_tier") or int(e.get("won_unit_count", -1)) != -1:
            v.append(f"A12[{eid}]: non-win carries won_* fields")
        if e.get("baseline_restored"):
            if outcome == "won":
                v.append(f"A13[{eid}]: baseline_restored on a win")
            if str(e.get("trigger_reason")) not in {
                "quality_gate",
                "no_rent",
                "no_area",
            }:
                v.append(
                    f"A13[{eid}]: baseline_restored with "
                    f"trigger_reason={e.get('trigger_reason')!r}"
                )
            if int(e.get("baseline_unit_count") or 0) <= 0:
                v.append(f"A13[{eid}]: baseline_restored with no baseline units")

    # B1 — zero dangling dispatches, and each dispatch joins its episode.
    if funnel["dangling"] != 0:
        v.append(
            f"B1/D6: dangling={funnel['dangling']} "
            f"(RETRY_DISPATCHED={funnel['dispatched_events']}, "
            f"sum(attempts)={funnel['attempts_sum']}) — a dispatch has no "
            f"terminal event, OR a shard ledger was truncated by a hard kill"
        )
    per_ep: Counter = Counter(str(d.get("episode_id") or "") for d in dispatched_evs)
    known = {str(e.get("episode_id")) for e in eps}
    for eid, n in per_ep.items():
        if eid not in known:
            v.append(f"B1: {n} dispatch(es) reference unknown episode_id {eid!r}")
    for e in eps:
        eid = str(e.get("episode_id"))
        if per_ep.get(eid, 0) != int(e.get("attempts") or 0):
            v.append(
                f"B1[{eid}]: attempts={e.get('attempts')} but "
                f"{per_ep.get(eid, 0)} dispatch events"
            )

    # B2 / B3.
    if by.get("won", 0) != funnel["success_events"]:
        v.append(
            f"B2: won={by.get('won', 0)} but "
            f"RETRY_SUCCESS={funnel['success_events']}"
        )
    if by.get("telemetry_only", 0) != funnel["would_dispatch_events"]:
        v.append(
            f"B3: telemetry_only={by.get('telemetry_only', 0)} but "
            f"RETRY_WOULD_DISPATCH={funnel['would_dispatch_events']}"
        )

    # B6 — episodes are a subset of scrape() calls (detector signals fire
    # upstream of the FAILED_UNREACHABLE return, so this is an inequality).
    if funnel["detector_signal_events"] and funnel["episodes"] > funnel[
        "detector_signal_events"
    ]:
        v.append(
            f"B6: episodes={funnel['episodes']} > "
            f"detector_signals={funnel['detector_signal_events']}"
        )

    # D1 / D2 / D3 — run-health gates.
    if by.get("setup_error", 0):
        v.append(
            f"D1: setup_error={by['setup_error']} — PAGE. The retry block's "
            f"imports or int(PATH_B_MAX_RETRIES) raised; retry is dead run-wide."
        )
    if by.get("aborted_error", 0):
        v.append(f"D2: aborted_error={by['aborted_error']} — a bug inside the loop")
    if by.get(TRIGGER_ERROR, 0):
        v.append(
            f"D2b: trigger_error={by[TRIGGER_ERROR]} — the trigger predicate "
            f"raised on that many episodes, i.e. a baseline or retry adapter "
            f"emitted rows that are not dicts. Per-property, NOT run-wide: do "
            f"not confuse this with D1"
        )
    if funnel["episodes"]:
        # SCOPE: cancellation INSIDE the retry block only. A property cancelled
        # while awaiting the BASELINE ``adapter.extract`` never reaches the
        # block, produces ZERO episodes, and is therefore invisible to this
        # gate — see the ``unreached`` line in ``render``. Since nearly all
        # wall-clock under jugnu's 600s ``asyncio.wait_for`` sits in that
        # baseline await, this measures a tail, and a 0% reading is NOT
        # evidence that no property timed out.
        cancel_pct = 100.0 * by.get("aborted_cancelled", 0) / funnel["episodes"]
        if cancel_pct >= 1.0:
            v.append(
                f"D3: aborted_cancelled={cancel_pct:.1f}% of episodes (>=1%) — "
                f"cancellation is landing INSIDE the retry loop, i.e. retry "
                f"cost is pushing properties past the 600s wait_for cap"
            )

    # D5 — a large run must show both sides of the trigger.
    if funnel["episodes"] > 100 and (
        by.get("not_triggered", 0) == 0 or funnel["triggered"] == 0
    ):
        v.append(
            "D5: a >100-episode run with no not_triggered or no triggered "
            "episodes — the hook is probably not running where you think"
        )

    return v


def check_properties(
    funnel: dict[str, Any], properties_json: Path
) -> list[str]:
    """Cross-check set C against a run's ``properties.json``.

    Args:
        funnel: output of :func:`compute_funnel`.
        properties_json: path to the run's property records.

    Returns:
        Violations of C1 (plan-level output implies a plan-level trigger or a
        restore) and C3 (restore implies the SUCCESS_PLAN_LEVEL verdict).
    """
    v: list[str] = []
    try:
        raw = json.loads(properties_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"C: could not read {properties_json}: {exc}"]

    records = raw if isinstance(raw, list) else raw.get("properties") or []
    verdict_by_pid = {
        str(r.get("property_id") or ""): str(r.get("_verdict_quality") or "")
        for r in records
        if isinstance(r, dict)
    }

    eps: list[dict[str, Any]] = funnel["_episodes"]
    by_pid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in eps:
        by_pid[str(e.get("property_id") or "")].append(e)

    for pid, verdict in verdict_by_pid.items():
        if verdict != "SUCCESS_PLAN_LEVEL":
            continue
        mine = by_pid.get(pid) or []
        # C1 is DIRECTIONAL: the #41 empty-exit subpage fallback sets the same
        # verdict with _plan_level_reason="empty_exit_subpage_recovery", so a
        # plan-level property need not have a plan-level trigger.
        if mine and not any(
            e.get("trigger_reason") == "plan_level_only" or e.get("baseline_restored")
            for e in mine
        ):
            v.append(
                f"C1: {pid} shipped SUCCESS_PLAN_LEVEL but no episode carries "
                f"plan_level_only or baseline_restored"
            )

    # C3 — restore implies the verdict (one direction only).
    for e in eps:
        if not e.get("baseline_restored"):
            continue
        pid = str(e.get("property_id") or "")
        if pid in verdict_by_pid and verdict_by_pid[pid] != "SUCCESS_PLAN_LEVEL":
            v.append(
                f"C3: {pid} had baseline_restored but verdict is "
                f"{verdict_by_pid[pid]!r}"
            )
    return v


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(funnel: dict[str, Any]) -> str:
    """Render the funnel as plain text.

    Args:
        funnel: output of :func:`compute_funnel`.

    Returns:
        A multi-line report.
    """
    by = funnel["by_outcome"]
    lines: list[str] = []
    add = lines.append
    add("=" * 66)
    add("PATH-B/C RETRY FUNNEL")
    add("=" * 66)
    add(f"  episodes (reached the decision point) : {funnel['episodes']:>8}")
    add(f"    not_triggered                       : {by.get('not_triggered', 0):>8}")
    add(f"  triggered                             : {funnel['triggered']:>8}")
    add(f"    no_budget                           : {by.get('no_budget', 0):>8}")
    add(f"    no_candidate  (the ~37% bucket)     : {by.get('no_candidate', 0):>8}")
    add(f"    telemetry_only                      : {by.get('telemetry_only', 0):>8}")
    add(f"  dispatched                            : {funnel['dispatched']:>8}")
    add(f"    won                                 : {by.get('won', 0):>8}")
    for o in (
        "lost_max_retries",
        "lost_candidates_exhausted",
        "lost_adapter_error",
        "lost_dead_end",
    ):
        add(f"    {o:<36}: {by.get(o, 0):>8}")
    add(f"    aborted_cancelled  (in-loop only)   : {by.get('aborted_cancelled', 0):>8}")
    add(f"    aborted_error                       : {by.get('aborted_error', 0):>8}")
    add(f"  trigger_error (per-property)          : {by.get(TRIGGER_ERROR, 0):>8}")
    add(f"  setup_error (PAGES — run-wide)        : {by.get('setup_error', 0):>8}")
    add("-" * 66)
    add(f"  trigger rate  : {funnel['trigger_rate_per_episode'] * 100:6.1f}%  (per episode)")
    add(
        f"  dispatch rate : {funnel['dispatch_rate_per_episode'] * 100:6.1f}%  "
        f"(per episode, of triggered)"
    )
    add(
        f"  win rate      : {funnel['win_rate_per_episode'] * 100:6.1f}%  "
        f"(per episode, of dispatched)"
    )
    add("-" * 66)
    # THE POST-MORTEM QUESTION was "for how many PROPERTIES did the trigger
    # fire?" — every number above answers a different one, because scrape()
    # recurses for link-hop sub-pages under the SAME property_id.
    add("PER PROPERTY  (episodes are scrape() calls, not properties)")
    add(f"  properties                            : {funnel['properties']:>8}")
    add(
        f"  episodes per property (mean)          : "
        f"{funnel['episodes_per_property']:>8.2f}"
    )
    add(f"  properties with any trigger           : {funnel['properties_with_trigger']:>8}")
    add(f"  properties with >=1 dispatch          : {funnel['properties_with_dispatch']:>8}")
    add(f"  properties won                        : {funnel['properties_won']:>8}")
    add(f"  trigger rate  : {funnel['trigger_rate_per_property'] * 100:6.1f}%  (per property)")
    add(
        f"  dispatch rate : {funnel['dispatch_rate_per_property'] * 100:6.1f}%  "
        f"(per property, of triggered)"
    )
    add(
        f"  win rate      : {funnel['win_rate_per_property'] * 100:6.1f}%  "
        f"(per property, of dispatched)"
    )
    if funnel["per_trigger_properties"]:
        add(f"  {'trigger':<20}{'properties':>12}")
        for trig, n in sorted(
            funnel["per_trigger_properties"].items(), key=lambda kv: -kv[1]
        ):
            add(f"  {trig:<20}{n:>12}")
    add("-" * 66)
    add("RECONCILIATION")
    add(f"  sum(episode.attempts)   : {funnel['attempts_sum']:>8}")
    add(f"  RETRY_DISPATCHED events : {funnel['dispatched_events']:>8}")
    add(f"  RETRY_SUCCESS events    : {funnel['success_events']:>8}")
    add(f"  detector_signals events : {funnel['detector_signal_events']:>8}  (>= episodes)")
    # THE FUNNEL IS CLOSED OVER EPISODES, AND EPISODES ARE NOT ALL SCRAPES.
    # A scrape() call that returns FAILED_UNREACHABLE, or whose baseline
    # ``adapter.extract`` is cancelled by jugnu's 600s wait_for, never reaches
    # the retry block and emits NO episode. Those calls cannot violate any
    # invariant here — "all invariants held" says nothing about them — so the
    # gap is printed rather than left to be inferred. It is correlated with
    # the hardest cohort (slow / bot-blocked sites), which is exactly why it
    # must not be silent.
    add(
        f"  never reached the block : {funnel['unreached_scrapes']:>8}  "
        f"(detector_signals - episodes; unreachable-return, baseline "
        f"cancellation, or an early exit — NO episode exists for these)"
    )
    add(f"  DANGLING                : {funnel['dangling']:>8}  (must be 0)")
    if funnel["per_trigger"]:
        add("-" * 66)
        add(f"  {'trigger':<20}{'episodes':>10}{'dispatched':>12}{'won':>8}")
        for trig, d in sorted(
            funnel["per_trigger"].items(), key=lambda kv: -kv[1]["episodes"]
        ):
            add(
                f"  {trig:<20}{d['episodes']:>10}{d['dispatched']:>12}{d['won']:>8}"
            )
    if funnel["by_baseline_pms"]:
        add("-" * 66)
        add("  episodes by baseline pms (top 15)")
        for pms, n in funnel["by_baseline_pms"].items():
            add(f"    {pms or '(none)':<30}{n:>8}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint.

    Args:
        argv: argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        0 when every invariant held, 1 otherwise (or when no episodes were
        found, which is itself the diagnostic).
    """
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", type=Path, help="run dir, shard dir, or events.jsonl")
    ap.add_argument("--properties-json", type=Path, default=None)
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    paths = iter_event_files(args.run_dir)
    if not paths:
        print(f"no events.jsonl under {args.run_dir}", file=sys.stderr)
        return 1
    events = load_events(paths)
    funnel = compute_funnel(events)

    violations = check_invariants(funnel)
    if args.properties_json:
        violations += check_properties(funnel, args.properties_json)

    public = {k: val for k, val in funnel.items() if not k.startswith("_")}
    if args.json:
        print(json.dumps({**public, "violations": violations}, indent=2, default=str))
    else:
        print(f"ledgers: {len(paths)}   events: {len(events)}")
        print(render(funnel))
        if violations:
            print("-" * 66)
            print(f"INVARIANT VIOLATIONS ({len(violations)}):")
            for x in violations:
                print(f"  ! {x}")
        else:
            print("-" * 66)
            # Deliberately scoped. "CLOSED" is a statement about the EPISODES
            # in this ledger, not about every property in the run: scrapes
            # that never reached the retry block emit nothing and so cannot
            # fail an invariant. Say so on the same line, or the next
            # post-mortem reads this as "nothing is missing".
            print(
                f"all invariants held — the funnel is CLOSED over "
                f"{funnel['episodes']} episodes / {funnel['properties']} "
                f"properties ({funnel['unreached_scrapes']} scrape() calls "
                f"never reached the retry block and are outside it)"
            )

    if funnel["episodes"] == 0:
        print(
            "no RETRY_EPISODE events found — the hook did not run "
            "(this is now a distinguishable state, which is the point)",
            file=sys.stderr,
        )
        return 1
    return 1 if violations else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
