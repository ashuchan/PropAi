"""Regression tests — ``_meta.verdict`` round-trip through ``_v2_formatted``.

These tests reproduce the 2026-05-11 reporting regression caused by commit
3013362 ("Fixing the ever alluding llm feedback loop") hoisting
``_format_output(...)`` into ``_process_property`` BEFORE
``result.setdefault("_meta", {})`` runs. The result was that
``_v2_formatted["_meta"]`` became a fresh empty dict, distinct from the
``result["_meta"]`` that the verdict-writer later mutates — so
``properties.json`` ended up with ``_meta.verdict = None`` for every property,
and ``run_report.build`` counted every failure as a success.

The fix (see docs/2026_05_11_regressions_fix_design.md, section
"Bug A — _meta.verdict lost through _v2_formatted cache") changes
``_format_v1`` / ``_format_v2`` to use ``result.setdefault("_meta", {})``
so the returned dict's ``_meta`` is the SAME object as ``result["_meta"]``.
Mutations downstream of the formatter call are then observed by both.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ── 1. Direct test of the dict-object sharing assumption ─────────────────────


def test_v2_formatted_meta_shares_object_with_result_meta() -> None:
    """``_format_v2`` must yield a formatted dict whose ``_meta`` is the
    SAME OBJECT as ``result['_meta']``. If not, late mutations to
    ``result['_meta']`` (the verdict assignment at ``jugnu.py:763``) do not
    reach the cached formatted dict that gets written to ``properties.json``.

    This is the proximate cause of the verdict-persistence regression. In
    the production failure mode the
    hoisted ``_format_output`` call at ``jugnu.py:691`` runs when
    ``result['_meta']`` is not yet set, so ``_format_v2`` falls through to
    ``meta = result.get('_meta', {})`` which returns a brand-new empty dict
    that has no connection to whatever ``result.setdefault('_meta', {})``
    creates 70 lines later.
    """
    from ma_poc.scripts.runners.jugnu import _format_v2

    # Mirror the state of ``result`` at the moment the hoisted call runs:
    # extraction has populated ``units``, but ``_meta`` has not been
    # initialised by the verdict-writer block yet.
    result: dict = {"units": []}
    formatted = _format_v2(result, {})

    # ``_process_property`` later does this:
    meta = result.setdefault("_meta", {})
    meta["verdict"] = "FAILED_NO_DATA"
    meta["canonical_id"] = "TEST-001"

    # The cached formatted dict must observe both mutations.
    assert formatted["_meta"] is result["_meta"], (
        "_format_v2 returned a formatted dict whose ``_meta`` is a different "
        "object than ``result['_meta']``. In production this drops the "
        "verdict that ``_process_property`` writes after the hoisted "
        "``_format_output(...)`` call returns — see "
        "docs/run_2026_05_11_manual_analysis.md.\n"
        f"  result['_meta']:    id={id(result['_meta'])}  value={result['_meta']!r}\n"
        f"  formatted['_meta']: id={id(formatted['_meta'])}  value={formatted['_meta']!r}"
    )


# ── 2. End-to-end report round-trip (the production-shape failure) ───────────


def test_run_report_succeeded_count_matches_event_emit_verdicts(
    tmp_path: Path,
) -> None:
    """``report.json.totals.succeeded`` must equal the count of
    ``output.property_emitted`` events with ``verdict='SUCCESS'``.

    This is the headline metric the daily analyser keys off; on
    2026-05-11 every shard's report claimed 100% success while events.jsonl
    showed 30-70% per shard. The mismatch propagates from the verdict-not-
    persisted regression: ``_meta``
    on each property dict is the empty placeholder that ``_format_v2``
    captured before the verdict-writer ran, so ``run_report.build`` reads
    ``meta.get('verdict') or ''`` → empty string → never matches
    ``startswith('FAILED')`` → counts every property as a success.

    The fixture below is the minimal reproducer: 5 properties with empty
    ``_meta`` and a non-FAIL terminal tier, plus an events.jsonl with 3
    SUCCESS + 2 FAILED_NO_DATA emits. The true success count is 3 but the
    current report says 5.
    """
    from ma_poc.reporting.run_report import build

    run_dir = tmp_path / "run-2026-05-11"
    run_dir.mkdir()

    # ``_meta = {}`` exactly mirrors what shard_0/properties.json contained
    # on 2026-05-11 — see the per-shard reconciliation table in the manual
    # analysis doc. ``_extract_result.tier_used = TIER_1_API`` matches the
    # failed-extraction terminal label produced by the runner; it contains
    # no "FAIL" substring so the run_report's tier-string fallback does not
    # rescue the count.
    properties = [
        {
            "property_id": f"P{i}",
            "units": [],
            "_meta": {},
            "_extract_result": {"tier_used": "TIER_1_API"},
        }
        for i in range(5)
    ]

    events = [
        {"kind": "output.property_emitted", "property_id": "P0", "verdict": "SUCCESS"},
        {"kind": "output.property_emitted", "property_id": "P1", "verdict": "SUCCESS"},
        {"kind": "output.property_emitted", "property_id": "P2", "verdict": "SUCCESS"},
        {"kind": "output.property_emitted", "property_id": "P3", "verdict": "FAILED_NO_DATA"},
        {"kind": "output.property_emitted", "property_id": "P4", "verdict": "FAILED_NO_DATA"},
    ]
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )

    report = build(properties, run_dir, "2026-05-11", {}, [])

    succ_in_events = sum(
        1 for e in events
        if e["kind"] == "output.property_emitted" and e["verdict"] == "SUCCESS"
    )
    assert report["totals"]["succeeded"] == succ_in_events, (
        f"Report claims {report['totals']['succeeded']}/{report['totals']['properties']} succeeded "
        f"but events.jsonl shows {succ_in_events}. See "
        f"docs/run_2026_05_11_manual_analysis.md. The fix is to either "
        f"share ``_meta`` between ``result`` and ``_v2_formatted`` (so the "
        f"verdict written at ``jugnu.py:763`` reaches the on-disk JSON) or "
        f"have ``run_report.build`` cross-reference events.jsonl for the "
        f"verdict count rather than trusting ``_meta.verdict`` from "
        f"``properties.json``."
    )


# ── 3. Sibling formatter has the same sharing contract ──────────────────────


def test_v1_formatted_meta_shares_object_with_result_meta() -> None:
    """``_format_v1`` must obey the same sharing contract as ``_format_v2``.

    Today the v1 path is never invoked before ``_meta`` is initialised, so
    the bug doesn't manifest in production. But the source-level pattern is
    identical (``meta = result.get('_meta', {})`` followed by embedding ``meta``
    into the returned dict). If a future caller hoists ``_format_output`` for
    v1 the way commit 3013362 did for v2, the same class of bug reappears.
    This test pins the
    invariant for both formatters so that drift is impossible without the test
    going red.
    """
    from ma_poc.scripts.runners.jugnu import _format_v1

    result: dict = {"units": []}
    formatted = _format_v1(result, {})

    meta = result.setdefault("_meta", {})
    meta["verdict"] = "FAILED_NO_DATA"
    meta["canonical_id"] = "TEST-V1"

    assert formatted["_meta"] is result["_meta"], (
        "_format_v1 captured a different ``_meta`` dict object than the one "
        "that later lives at result['_meta']. Same root cause as the v2 "
            "verdict-not-persisted regression; "
        "must share the same dict so post-format mutations propagate.\n"
        f"  result['_meta']:    id={id(result['_meta'])}  value={result['_meta']!r}\n"
        f"  formatted['_meta']: id={id(formatted['_meta'])}  value={formatted['_meta']!r}"
    )


# ── 4. Retry caller's pre-init pattern survives the fix ──────────────────────


def test_retry_caller_pattern_preserves_meta_fields() -> None:
    """``jugnu_retry.py:731`` does ``result.setdefault('_meta', {})``, mutates
    it (``retry=True``, ``retry_source_run=date``), and calls ``_format_output``.

    After the Bug-A fix, ``_format_v2`` must observe the pre-existing
    ``_meta`` dict (not capture a fresh one) so the retry fields it carries
    survive into the formatted output that ``properties.json`` is built from.

    This test exists because the proposed fix changes formatter access from
    ``.get`` to ``setdefault`` — a regression here would mean the retry path
    silently loses its ``retry`` markers, which would only surface days later
    when the retry analyser shows an empty bucket.
    """
    from ma_poc.scripts.runners.jugnu import _format_output

    result: dict = {"units": []}
    meta = result.setdefault("_meta", {})
    meta["retry"] = True
    meta["retry_source_run"] = "2026-05-10"

    formatted = _format_output(result, {}, schema_version="v2")

    assert formatted["_meta"].get("retry") is True, (
        "retry marker set before _format_output was called did not survive "
        "into the formatted dict — _format_v2 is capturing a fresh _meta "
        "instead of sharing the existing one."
    )
    assert formatted["_meta"].get("retry_source_run") == "2026-05-10"
    assert formatted["_meta"] is result["_meta"]


# ── 5. Carry-forward path keeps its SUCCESS verdict ──────────────────────────


def test_carry_forward_caller_preserves_success_verdict() -> None:
    """``jugnu.py:629-632`` carry-forward path sets ``_meta.verdict='SUCCESS'``
    before returning the cf_record. That record bypasses ``_process_property``
    entirely (early return) and goes straight to ``_format_output`` in
    ``_process_one``.

    The Bug-A fix must not regress this: a result with a pre-existing
    ``_meta.verdict`` already populated must surface that verdict in the
    formatted dict. Symmetric to the retry-fields test above.
    """
    from ma_poc.scripts.runners.jugnu import _format_output

    result: dict = {
        "units": [{"unit_id": "101", "rent_low": 1450}],
        "_meta": {
            "canonical_id": "CF-001",
            "verdict": "SUCCESS",
            "verdict_reason": "carry_forward_applied",
            "carry_forward_used": True,
        },
    }
    formatted = _format_output(result, {}, schema_version="v2")

    assert formatted["_meta"].get("verdict") == "SUCCESS", (
        "carry-forward record's verdict did not survive into the formatted "
        "output. _format_v2 is replacing or shadowing the pre-existing _meta "
        "dict instead of sharing it."
    )
    assert formatted["_meta"].get("verdict_reason") == "carry_forward_applied"
    assert formatted["_meta"] is result["_meta"]


# ── 6-9. Defence in depth: run_report verdict resolver behaviour ────────────


def _build_report_with_events(
    tmp_path: Path,
    *,
    properties: list[dict],
    events: list[dict],
) -> dict:
    """Helper: stage a run_dir with events.jsonl and invoke run_report.build.

    Centralises the boilerplate of constructing a per-test run_dir so the
    four resolver-behaviour tests below stay focused on the assertion.
    """
    from ma_poc.reporting.run_report import build

    run_dir = tmp_path / "run-2026-05-11"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + ("\n" if events else ""),
        encoding="utf-8",
    )
    return build(properties, run_dir, "2026-05-11", {}, [])


def test_resolver_agreement_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When _meta.verdict and the emit event agree, the count reflects
    that value and no disagreement warning is logged. Documents the
    common path (post-Bug-A-fix, this is what production looks like)."""
    import logging
    caplog.set_level(logging.WARNING, logger="ma_poc.reporting.run_report")

    properties = [
        {
            "property_id": "P0",
            "units": [{"unit_id": "1"}],
            "_meta": {"canonical_id": "P0", "verdict": "SUCCESS"},
            "_extract_result": {"tier_used": "TIER_1_API"},
        },
        {
            "property_id": "P1",
            "units": [],
            "_meta": {"canonical_id": "P1", "verdict": "FAILED_NO_DATA"},
            "_extract_result": {"tier_used": "TIER_1_API"},
        },
    ]
    events = [
        {"kind": "output.property_emitted", "property_id": "P0", "verdict": "SUCCESS"},
        {"kind": "output.property_emitted", "property_id": "P1", "verdict": "FAILED_NO_DATA"},
    ]
    report = _build_report_with_events(tmp_path, properties=properties, events=events)
    assert report["totals"]["succeeded"] == 1
    assert report["totals"]["failed"] == 1
    assert not any(
        "verdict disagreement" in rec.message for rec in caplog.records
    ), "no disagreement warning expected when both sources agree"


def test_resolver_events_used_when_meta_empty(tmp_path: Path) -> None:
    """When ``_meta.verdict`` is missing (the production-observed shape on
    2026-05-11), the report must still produce the correct totals by
    consulting events.jsonl. This is the headline defence-in-depth assertion."""
    properties = [
        {
            "property_id": f"P{i}",
            "units": [],
            "_meta": {"canonical_id": f"P{i}"},  # NO verdict
            "_extract_result": {"tier_used": "TIER_1_API"},
        }
        for i in range(5)
    ]
    events = [
        {"kind": "output.property_emitted", "property_id": "P0", "verdict": "SUCCESS"},
        {"kind": "output.property_emitted", "property_id": "P1", "verdict": "SUCCESS"},
        {"kind": "output.property_emitted", "property_id": "P2", "verdict": "SUCCESS"},
        {"kind": "output.property_emitted", "property_id": "P3", "verdict": "FAILED_NO_DATA"},
        {"kind": "output.property_emitted", "property_id": "P4", "verdict": "FAILED_NO_DATA"},
    ]
    report = _build_report_with_events(tmp_path, properties=properties, events=events)
    assert report["totals"]["succeeded"] == 3, (
        f"events should have rescued the count; got {report['totals']['succeeded']}"
    )
    assert report["totals"]["failed"] == 2


def test_resolver_meta_used_when_events_missing(tmp_path: Path) -> None:
    """When a property has no emit event (the 4-no-emit observed-in-prod
    case — runner crash before PROPERTY_EMITTED), the report must fall
    back to ``_meta.verdict``. Today's behaviour for that case must not
    regress under the resolver."""
    properties = [
        {
            "property_id": "P0",
            "units": [{"unit_id": "1"}],
            "_meta": {"canonical_id": "P0", "verdict": "SUCCESS"},
            "_extract_result": {"tier_used": "TIER_1_API"},
        },
        {
            "property_id": "P1",
            "units": [],
            "_meta": {"canonical_id": "P1", "verdict": "FAILED_NO_DATA"},
            "_extract_result": {"tier_used": "TIER_1_API"},
        },
    ]
    # Empty events ledger — the runner crashed before emitting.
    report = _build_report_with_events(tmp_path, properties=properties, events=[])
    assert report["totals"]["succeeded"] == 1
    assert report["totals"]["failed"] == 1


def test_resolver_event_wins_on_disagreement_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When the two sources disagree (a runner bug — they're written from
    the same value), the event ledger wins and a WARNING is emitted so an
    operator notices. Silently picking either side would re-introduce the
    silent-mis-count failure mode this whole fix is designed to prevent."""
    import logging
    caplog.set_level(logging.WARNING, logger="ma_poc.reporting.run_report")

    properties = [
        {
            "property_id": "P0",
            "units": [{"unit_id": "1"}],
            # Meta says SUCCESS but emit says FAILED — runner bug.
            "_meta": {"canonical_id": "P0", "verdict": "SUCCESS"},
            "_extract_result": {"tier_used": "TIER_1_API"},
        }
    ]
    events = [
        {"kind": "output.property_emitted", "property_id": "P0", "verdict": "FAILED_NO_DATA"},
    ]
    report = _build_report_with_events(tmp_path, properties=properties, events=events)

    # Event wins.
    assert report["totals"]["failed"] == 1
    assert report["totals"]["succeeded"] == 0

    # And a warning surfaced for the operator.
    warnings = [
        rec for rec in caplog.records
        if "verdict disagreement" in rec.message and "P0" in rec.message
    ]
    assert warnings, (
        f"expected a 'verdict disagreement' warning for P0, got: "
        f"{[r.message for r in caplog.records]}"
    )
