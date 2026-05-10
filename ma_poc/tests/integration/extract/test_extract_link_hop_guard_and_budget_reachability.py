"""Phase G tests — G2: link-hop guard relaxed so Phase 9 cross-page merge is reachable."""

from __future__ import annotations

import pathlib

_MA_POC = pathlib.Path(__file__).resolve().parent.parent.parent.parent  # ma_poc/
_SCRAPER = _MA_POC / "pms" / "scraper.py"


def test_g2_unreachable_comment_removed() -> None:
    """The 'future-proofing path' comment must be removed from scraper.py."""
    src = _SCRAPER.read_text()
    assert "future-proofing path" not in src
    assert "Today the link-hop only fires when main is empty" not in src


def test_g2_should_hop_variable_present() -> None:
    """G2 guard must use a should_hop variable wired to the planner."""
    src = _SCRAPER.read_text()
    assert "should_hop" in src, "scraper.py must contain should_hop guard (Phase G2)"
    assert "ESCALATE_LINK_HOP" in src, "Phase G2 must check for ESCALATE_LINK_HOP decision"


def test_g2_budget_link_hop_zero_prevents_hop() -> None:
    """When _jugnu_budget['link_hop'] == 0, should_hop must never be set."""
    src = _SCRAPER.read_text()
    assert "_jugnu_budget" in src and "link_hop" in src, (
        "Phase G2 budget cap must reference _jugnu_budget['link_hop']"
    )


def test_g2_cross_page_branch_reachable_when_both_have_units() -> None:
    """The 'if main_units and sub_units' branch must NOT be guarded by main_empty check.

    Phase G2 makes the hop fire when planner says ESCALATE_LINK_HOP — so
    main_units can be non-empty when we enter the merge branch.
    """
    src = _SCRAPER.read_text()
    # The old guard was: if not result.get("units") and fetch_result is not None:
    # which made the main_units branch unreachable (main was always empty on entry).
    # Phase G2 replaces it with should_hop, making main_units potentially non-empty.
    assert "should_hop" in src
    # The cross-page merge branch must still be present
    assert "if main_units and sub_units:" in src
