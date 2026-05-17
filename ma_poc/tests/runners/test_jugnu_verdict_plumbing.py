"""Tests for the 2026-05-17 RC3 verdict plumbing fix.

``reporting.verdict.compute()`` accepts a ``plan_summaries`` kwarg that,
when non-empty, returns ``SUCCESS_PLAN_LEVEL`` instead of
``FAILED_NO_DATA``. But ``scripts.runners.jugnu._process_property`` was
calling ``compute()`` without that kwarg — so every property whose D16
partition routed all rows to ``plan_summaries`` was misclassified as
FAILED_NO_DATA, despite shipping correct ``floor_plans[]`` data in the
v2 output.

73 PIDs in the May 17 cloud run had non-empty ``floor_plans`` AND
``verdict=FAILED_NO_DATA``. The fix is a one-line kwarg addition at
the verdict call site.
"""

from __future__ import annotations

import inspect
import re


def test_compute_verdict_call_passes_plan_summaries_kwarg() -> None:
    """The ``compute_verdict(...)`` call inside ``_process_property``
    must forward ``plan_summaries`` from the result dict.

    Pre-RC3 the call site read::

        verdict = compute_verdict(
            fetch_outcome=outcome_val,
            extract_result=extract_result,
            carry_forward_applied=...,
        )

    Post-RC3 it includes ``plan_summaries=result.get("plan_summaries")``.
    The verdict layer can't see the partition without this kwarg.
    """
    from ma_poc.scripts.runners import jugnu

    src = inspect.getsource(jugnu)
    # Look for a compute_verdict call followed (within 600 chars) by
    # plan_summaries. The 600-char window is generous enough to span
    # the full multi-line kwarg block.
    call_block = re.search(
        r"compute_verdict\([\s\S]{0,600}?plan_summaries\s*=",
        src,
    )
    assert call_block is not None, (
        "compute_verdict() call site in jugnu.py is not passing "
        "plan_summaries. Properties whose D16 partition routed all "
        "rows to plan_summaries will continue to be misclassified as "
        "FAILED_NO_DATA despite shipping data. See RC3 in the "
        "2026-05-17 regression analysis."
    )


def test_compute_verdict_kwarg_sources_from_result_dict() -> None:
    """The ``plan_summaries`` kwarg must read from the local ``result``
    dict — not from ``extract_result`` (which uses different field
    names) and not from a hardcoded list.

    Pre-D16 plan_summaries was emitted onto ``result["plan_summaries"]``
    by ``scraper.py:943``; that's the canonical source. Reading from
    anywhere else risks divergence.
    """
    from ma_poc.scripts.runners import jugnu

    src = inspect.getsource(jugnu)
    call_block = re.search(
        r"compute_verdict\([\s\S]{0,600}?"
        r"plan_summaries\s*=\s*result\.get\(['\"]plan_summaries['\"]\)",
        src,
    )
    assert call_block is not None, (
        "plan_summaries kwarg is not sourced from result.get('plan_summaries'). "
        "The runner-level result dict is the canonical carrier; "
        "reading from anywhere else risks divergence with the v2 "
        "floor_plans output."
    )
