"""Regression test — scraper and rescue must use the SAME adapter allow-list.

The scraper's rescue gate (``ma_poc/pms/scraper.py``) and the rescue
module's gate (``ma_poc/services/llm_api_rescue.py``) must agree on the
set of source-adapters that go through the rescue path. When they
disagree, ``rescue_from_api_responses`` rejects calls with ``unsupported
adapter: <name>`` — silent in scraper-side tests because they mock the
rescue function. This footprint was 427 properties/day on 2026-05-11
(408 OneSite + 19 AMLI).

The fix (see docs/2026_05_11_regressions_fix_design.md, section
"Bug D — Rescue allow-list drift between scraper and rescue") collapses
the two gates to a single ``SUPPORTED_ADAPTERS`` constant owned by the
rescue module (P2 in the design's principles). The scraper imports it.
Drift becomes structurally impossible.

This test enforces the **post-fix** invariant: that scraper.py imports
and uses ``SUPPORTED_ADAPTERS`` rather than carrying its own literal.
Before the fix landed, the test asserted equality of two duplicated
literals; the change to import-from-owner makes the previous AST walk
obsolete (no inline literal to find). The structural assertion has been
strengthened accordingly: any future re-introduction of a duplicated
literal will still fail this test because the literal would not equal
the imported constant.
"""

from __future__ import annotations

import ast
import inspect


def _scraper_source_uses_supported_adapters() -> bool:
    """Return True if scraper.py references ``SUPPORTED_ADAPTERS`` as a
    Name (the imported constant) anywhere in its source. Detects both
    the gate site (``adapter_name in SUPPORTED_ADAPTERS``) and any other
    reference. Uses AST rather than substring matching so a string
    literal ``"SUPPORTED_ADAPTERS"`` in a log message doesn't false-pass.
    """
    from ma_poc.pms import scraper

    tree = ast.parse(inspect.getsource(scraper))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "SUPPORTED_ADAPTERS":
            return True
    return False


def _scraper_has_inline_allow_list_literal() -> set[str] | None:
    """Return the inline ``adapter_name in {...}`` set literal if present,
    else None. Used to catch re-introduction of the bug — any literal
    here that doesn't exactly equal ``SUPPORTED_ADAPTERS`` is a drift.
    """
    from ma_poc.pms import scraper

    tree = ast.parse(inspect.getsource(scraper))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not (
            isinstance(node.left, ast.Name)
            and node.left.id == "adapter_name"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Set)
        ):
            continue
        values = {
            elt.value
            for elt in node.comparators[0].elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        }
        if values:
            return values
    return None


def test_scraper_imports_supported_adapters_from_rescue() -> None:
    """The scraper must use the rescue module's ``SUPPORTED_ADAPTERS``
    constant — not a duplicated literal — to gate ``adapter_name``
    before invoking the rescue. This is the structural enforcement of
    P2 (cross-file invariants live in one module and are imported)."""
    assert _scraper_source_uses_supported_adapters(), (
        "scraper.py does not reference ``SUPPORTED_ADAPTERS``. The "
        "rescue allow-list must be imported from "
        "``ma_poc.services.llm_api_rescue`` and used in the "
        "``needs_rescue`` gate, not duplicated as an inline literal. "
        "See docs/2026_05_11_regressions_fix_design.md."
    )


def test_scraper_carries_no_competing_inline_literal() -> None:
    """Belt-and-braces guard against the original drift recurring: if
    a future commit reintroduces an inline ``adapter_name in {...}``
    set literal that disagrees with the imported ``SUPPORTED_ADAPTERS``,
    flag it."""
    from ma_poc.services.llm_api_rescue import SUPPORTED_ADAPTERS

    inline = _scraper_has_inline_allow_list_literal()
    if inline is None:
        # No inline literal — clean. The scraper relies entirely on the
        # imported constant. This is the desired post-fix state.
        return

    rescue_set = set(SUPPORTED_ADAPTERS)
    assert inline == rescue_set, (
        "scraper.py reintroduced an inline allow-list literal that "
        "disagrees with ``SUPPORTED_ADAPTERS``.\n\n"
        f"  scraper.py inline literal:                {sorted(inline)}\n"
        f"  llm_api_rescue.py:SUPPORTED_ADAPTERS:     {sorted(rescue_set)}\n\n"
        f"  Passed by scraper but rejected by rescue: {sorted(inline - rescue_set)}\n"
        f"  Accepted by rescue but blocked by scraper: {sorted(rescue_set - inline)}\n\n"
        "Remove the inline literal; use the imported ``SUPPORTED_ADAPTERS`` "
        "constant. See docs/2026_05_11_regressions_fix_design.md."
    )


def test_supported_adapters_includes_widened_set() -> None:
    """Lock in the post-fix membership: rescue must accept all five
    adapter names the May-8 F1.3 implementation plan widened the
    scraper-side gate to. Catches a regression where someone narrows
    ``SUPPORTED_ADAPTERS`` back to the legacy pre-widening set without
    realising the scraper expects the widened set."""
    from ma_poc.services.llm_api_rescue import SUPPORTED_ADAPTERS

    required = {"generic", "entrata", "appfolio", "onesite", "amli"}
    missing = required - set(SUPPORTED_ADAPTERS)
    assert not missing, (
        f"SUPPORTED_ADAPTERS is missing the following adapter names that "
        f"the scraper-side rescue gate expects to be accepted: "
        f"{sorted(missing)}. The widening was part of the May-8 F1.3 "
        f"plan + the rescue-allow-list fix — any narrowing should be intentional and "
        f"paired with a scraper-side change."
    )
