"""Link-hop winning-tier survives the mid-hop timeout salvage (2026-07-12).

Root cause of the 231-prop SUCCESS-but-tier_used=NONE cohort: a property
recovers real Tier-1 unit data via link-hop, then times out (600s) mid-hop.
On asyncio.wait_for cancellation only the caller-owned _partial_state dict
survives — and it previously carried only units, never the winning tier. The
timeout salvage then rebuilt _extract_result as None, so the emitted record
shipped tier_used=None and a genuine Tier-1 recovery was not counted as gold.

Fix (two coordinated sites, generic plumbing, no per-adapter work):
  1. _try_link_hop checkpoints the winning tier into the cancellation-
     surviving dict alongside units (scraper.py).
  2. The jugnu timeout salvage reads _partial_state["tier_used"] and stamps
     _extract_result so reporting counts the true tier (jugnu.py).

These paths are hard to drive hermetically (deep async cancellation), so —
following test_scraper_transient_salvage.py — we (a) source-pin the two
production hooks and (b) decision-mirror the salvage-stamp logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_SCRAPER = (_ROOT / "pms" / "scraper.py").read_text()
_JUGNU = (_ROOT / "scripts" / "runners" / "jugnu.py").read_text()


# ── (a) source-pin the production hooks ─────────────────────────────────────

def test_scraper_checkpoints_tier_into_ext_ref() -> None:
    """_try_link_hop must write the winning tier into the surviving dict at
    BOTH accumulation checkpoints (not just units)."""
    # both checkpoints use the same assignment expression
    assert _SCRAPER.count('_ext_ref["tier_used"] = (') == 2
    assert '(\n                            _first_successful_result or sub_result\n' in _SCRAPER \
        or "_first_successful_result or sub_result" in _SCRAPER


def test_jugnu_salvage_stamps_extract_result_from_partial_state() -> None:
    """The timeout salvage must read _partial_state['tier_used'] and stamp
    _extract_result so the record ships a real tier."""
    assert '_salvage_tier = _partial_state.get("tier_used")' in _JUGNU
    assert 'failed["_extract_result"] = {' in _JUGNU
    assert '"tier_used": _salvage_tier' in _JUGNU
    # guarded on both a tier AND real salvaged units
    assert "if _salvage_tier and _partial_units:" in _JUGNU


# ── (b) decision-mirror the salvage-stamp logic ─────────────────────────────

def _salvage_stamp(partial_state: dict[str, Any], partial_units: list[Any]) -> dict | None:
    """Mirror of the jugnu salvage stamp: return the _extract_result the
    record would carry given the surviving partial state."""
    tier = partial_state.get("tier_used")
    if tier and partial_units:
        return {"tier_used": tier, "llm_cost_usd": 0.0}
    return None


def test_stamp_uses_checkpointed_tier1_tier() -> None:
    er = _salvage_stamp(
        {"units": [{"u": 1}], "tier_used": "TIER_1_API_RENTCAFE_SECURECAFE"},
        [{"u": 1}],
    )
    assert er is not None
    assert er["tier_used"] == "TIER_1_API_RENTCAFE_SECURECAFE"


def test_stamp_none_when_no_tier_checkpointed() -> None:
    # a hop that never recovered a tier → salvage stays untiered (FAILED-shaped)
    assert _salvage_stamp({"units": [{"u": 1}]}, [{"u": 1}]) is None


def test_stamp_none_when_no_units_salvaged() -> None:
    # a tier with zero salvaged units → nothing to stamp
    assert _salvage_stamp({"tier_used": "TIER_1_API_X"}, []) is None


def test_stamp_preserves_tier3_and_tier4_labels() -> None:
    """A DOM/LLM winning hop keeps its true (non-Tier-1) label — it must NOT
    be inflated to Tier-1, so it correctly stays out of the gold count."""
    for tier in ("TIER_3_DOM", "TIER_4_LLM_DOM"):
        er = _salvage_stamp({"tier_used": tier}, [{"u": 1}])
        assert er is not None and er["tier_used"] == tier
        assert not er["tier_used"].startswith("TIER_1")
