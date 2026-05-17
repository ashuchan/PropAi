"""Tests for the 2026-05-17 RC2 generic-fallback acceptance gate.

``scraper.py`` invokes the generic fallback adapter when the detected
PMS adapter returns 0 units. The precondition at lines ~876-882 of
``scrape()`` checks both ``adapter_result.units`` and
``adapter_result.plan_summaries`` — but the acceptance gate (line ~916)
only checked ``fallback_result.units``. Asymmetric.

If the generic fallback extracts plan-level rows from a marketing
shell (the post-detector-RC-4-widening case where a non-RentCafe site
got locked to the RentCafe adapter and the RentCafe adapter
correctly returned NO_RESPONSE), the fallback's plan_summaries were
silently discarded. The original PMS adapter's empty
``TIER_1_API_RENTCAFE_NO_RESPONSE`` result stayed terminal and the
property shipped as FAILED_NO_DATA.

This test pins the acceptance-gate symmetry: a fallback result with
``units=[] AND plan_summaries=[...]`` must replace the original.
"""

from __future__ import annotations

import inspect


def test_fallback_acceptance_gate_admits_plan_only_result() -> None:
    """The fallback acceptance gate must accept plan-only fallback
    results, mirroring the precondition that triggered the fallback in
    the first place.

    Source-level pin: the gate reads
    ``if fallback_result.units or getattr(fallback_result, "plan_summaries", None):``.
    Any future edit that drops the plan_summaries arm re-introduces
    the RC2 regression (~118 PIDs in the May 17 cloud run).
    """
    from ma_poc.pms import scraper as scraper_mod

    src = inspect.getsource(scraper_mod)
    # Locate the fallback block that ends with `adapter_result = fallback_result`.
    # Look back up to 500 chars and require both `fallback_result.units` and a
    # plan_summaries check (either inline getattr or via a `_fallback_has_plan_rows`-
    # style local). This stays robust against minor stylistic refactors of the
    # gate while still pinning that BOTH partitions are checked.
    import re
    gate_match = re.search(
        r"if fallback_result\.units or [\s\S]{0,200}?:\s*\n"
        r"\s*adapter_result = fallback_result",
        src,
    )
    assert gate_match is not None, (
        "Generic-fallback acceptance gate does not match the pattern "
        "`if fallback_result.units or <plan_summaries-check>: "
        "adapter_result = fallback_result`. The gate must be plan-aware."
    )
    gate_body = gate_match.group(0)
    assert "plan_summaries" in gate_body or "_fallback_has_plan_rows" in gate_body, (
        "Generic-fallback acceptance gate is not checking "
        "fallback_result.plan_summaries (or the _fallback_has_plan_rows "
        "local). Plan-only fallback recoveries will be silently "
        "discarded — re-introduces the RentCafe NO_RESPONSE regression "
        "for ~118 PIDs."
    )


def test_fallback_precondition_already_checks_plan_summaries() -> None:
    """Sanity: the precondition that DECIDES to run the fallback
    already considers plan_summaries (via ``_adapter_has_plan_rows``).
    The acceptance-gate fix only restores symmetry — it doesn't
    introduce new behaviour. If this assertion ever fails, RC2 may
    have been deployed without the upstream precondition in place,
    which would let the fallback run unnecessarily."""
    from ma_poc.pms import scraper as scraper_mod

    src = inspect.getsource(scraper_mod)
    assert "_adapter_has_plan_rows" in src, (
        "_adapter_has_plan_rows guard missing from the fallback "
        "precondition. The acceptance-gate fix (RC2) assumes the "
        "precondition is already plan-aware; without it the fallback "
        "would run on plan-only adapters and may degrade their output."
    )
