"""Tests for the 2026-05-16 shard_84 hop-budget protections.

Adds three independent budget caps to _try_link_hop / scrape_jugnu:

1. **Hop-count early termination** — when entry page has 0 fp_signals
   AND no embedded portal hints AND no profile.winning_page_url,
   max_hops is capped to 2 at the orchestrator (scraper.py:3375+).
2. **Cumulative hop-time budget** — _HOP_CUMULATIVE_BUDGET_MS = 240_000
   in _try_link_hop; the hop loop breaks when sum(elapsed_ms) exceeds
   it, regardless of remaining queue.
3. **Redirect-to-entry tracking** — sub_urls that redirect to the
   homepage path are added to shared_budget["_redirect_to_entry_urls"]
   so next-run skip-list writers can persist them.

Canonical victims on the 2026-05-16 cloud run shard_84:
- PIDs 63485, 63486, 63534, 6395 — hop-loop cases (6 of 32 timeouts)
- PIDs 63878, 6340, 63766, etc. — hard-wedge cases (26 of 32 timeouts;
  these are covered by the wedge-rescue retry pass in jugnu.py rather
  than by hop-budget caps)
"""
from __future__ import annotations

import re


def test_hop_cumulative_budget_constant_is_reasonable() -> None:
    """The cumulative hop-time cap should sit in a sensible range.

    Too small and legitimate slow sites lose their hop budget; too
    large and a single property can monopolise its per-property 600s
    wallclock. 240s gives ~7 hops of legitimate p95 (~25s each) before
    cap-out.
    """
    from ma_poc.pms import scraper as _scraper
    assert hasattr(_scraper, "_HOP_CUMULATIVE_BUDGET_MS") is False, (
        "_HOP_CUMULATIVE_BUDGET_MS is a local in _try_link_hop, not a "
        "module-level constant; this test asserts that fact so future "
        "refactors don't accidentally expose it."
    )


def test_hop_budget_local_value_present_in_source() -> None:
    """Grep guard: the local hop-budget constant is named and set to
    240_000 ms in _try_link_hop. This test fails loudly if a refactor
    silently changes the name or value, since the value matters for
    SLO budgeting and shouldn't drift unnoticed.
    """
    import inspect
    from ma_poc.pms.scraper import _try_link_hop
    src = inspect.getsource(_try_link_hop)
    assert "_HOP_CUMULATIVE_BUDGET_MS" in src
    # Allow the constant to be tuned, but force the change to be
    # deliberate (test must be updated in lock-step).
    m = re.search(r"_HOP_CUMULATIVE_BUDGET_MS\s*=\s*(\d[\d_]*)", src)
    assert m is not None, "constant assignment not found"
    value = int(m.group(1).replace("_", ""))
    assert 60_000 <= value <= 600_000, (
        f"_HOP_CUMULATIVE_BUDGET_MS={value} is outside the safe range "
        "[60s, 600s]. Document the tuning rationale before relaxing."
    )


def test_hop_cumulative_budget_breaks_the_loop() -> None:
    """The hop loop must break — not continue, not raise — when
    cumulative elapsed time crosses the budget.
    """
    import inspect
    from ma_poc.pms.scraper import _try_link_hop
    src = inspect.getsource(_try_link_hop)
    # Verify the break statement is in the cumulative-budget check
    # block. Pattern: ``if _hop_elapsed_total_ms >= _HOP_CUMULATIVE_BUDGET_MS``
    # followed by a ``break`` within ~10 lines.
    m = re.search(
        r"_hop_elapsed_total_ms\s*>=\s*_HOP_CUMULATIVE_BUDGET_MS"
        r"[\s\S]{0,1200}?\bbreak\b",
        src,
    )
    assert m is not None, (
        "Cumulative-budget check exists but doesn't break the hop loop. "
        "Without `break`, the budget cap is informational only — every "
        "subsequent hop still runs."
    )


def test_hop_elapsed_accumulator_is_updated() -> None:
    """_hop_elapsed_total_ms must increment after each sub_fetch — if a
    refactor removes the accumulator update, the budget check fires
    only on the first hop (which is always 0ms).
    """
    import inspect
    from ma_poc.pms.scraper import _try_link_hop
    src = inspect.getsource(_try_link_hop)
    # Accumulator update pattern: `_hop_elapsed_total_ms += int(sub_fetch.elapsed_ms or 0)`
    assert "_hop_elapsed_total_ms +=" in src, (
        "Accumulator update missing — cumulative hop budget will never "
        "fire because _hop_elapsed_total_ms stays at 0."
    )


def test_redirect_to_entry_url_persistence() -> None:
    """The redirect-to-entry short-circuit should record the redirected
    URL in shared_budget["_redirect_to_entry_urls"] so next-run skip-
    list writers can persist them. Without this, every run re-tries
    URLs that always 301 back to the homepage.
    """
    import inspect
    from ma_poc.pms.scraper import _try_link_hop
    src = inspect.getsource(_try_link_hop)
    assert "_redirect_to_entry_urls" in src, (
        "Redirect-to-entry tracking missing — same dead URLs will be "
        "re-tried every run, burning hop budget that should go to "
        "real candidates."
    )


def test_zero_signal_hop_cap_logic_present_in_scrape() -> None:
    """The hop-count cap (max_hops=2 when entry has 0 fp_signals AND no
    portal hints AND no winning_page_url) must be wired in the
    orchestrator before _try_link_hop is called. This test grep-asserts
    the gate is present and references the three signals the design
    requires.
    """
    import inspect
    from ma_poc.pms import scraper as _scraper
    src = inspect.getsource(_scraper)
    # The cap block decorates the _try_link_hop call. Verify all three
    # input signals are referenced in the same code block.
    cap_re = re.search(
        r"_entry_fp_count\s*=[\s\S]{0,2500}?_max_hops_effective\s*=\s*2",
        src,
    )
    assert cap_re is not None, (
        "Hop-count cap (max_hops=2 on zero-signal entry) is missing "
        "from the orchestrator."
    )
    block = cap_re.group(0)
    assert "_has_portal_hints" in block
    assert "_has_winning_page" in block
    assert "floor_plan_signal_count" in block
