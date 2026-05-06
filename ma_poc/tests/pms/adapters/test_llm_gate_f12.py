"""F12: LLM gate condition — only skip LLM when the detected adapter
produced units. Mirrors the predicate at generic.py:1268.

The full GenericAdapter cascade requires Playwright + extensive context;
these tests assert the standalone predicate plus check that the source
file actually contains the F12 condition (so a refactor that drops it
would fail loudly).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterContext


@dataclass
class _DetectedStub:
    pms: str
    confidence: float = 0.9


def _gate_decision(detected_pms: str, adapter_unit_count: int) -> bool:
    """Mirror of the live predicate at ma_poc/pms/adapters/generic.py:1268.

    skip_llm = ctx.detected.pms != "unknown" AND adapter_unit_count > 0
    """
    return detected_pms != "unknown" and adapter_unit_count > 0


# ---- H19 parametric: gate behavior across the truth table ---------------


@pytest.mark.parametrize("pms,units,expect_skip", [
    # Known PMS + units extracted → skip LLM (don't burn budget)
    ("appfolio", 5, True),
    ("entrata", 12, True),
    ("rentcafe", 1, True),
    # Known PMS + zero units → DO NOT skip (the F12 fix)
    ("appfolio", 0, False),
    ("entrata", 0, False),
    ("rentcafe", 0, False),
    # Unknown PMS → never skip regardless of unit count
    ("unknown", 0, False),
    ("unknown", 5, False),
])
def test_h19_llm_gate_predicate(pms: str, units: int, expect_skip: bool) -> None:
    assert _gate_decision(pms, units) is expect_skip


# ---- AdapterContext carries the new field -------------------------------


def test_adapter_context_default_adapter_unit_count_is_zero() -> None:
    """New ctx field defaults to 0 so the gate stays open by default —
    prevents accidental gate-shut on contexts that haven't been threaded."""
    from ma_poc.pms.detector import detect_pms

    ctx = AdapterContext(
        base_url="https://example.com",
        detected=detect_pms("https://example.com"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    assert ctx.adapter_unit_count == 0


# ---- Static check: F12 predicate is in the source -----------------------


def test_f12_condition_present_in_generic_adapter_source() -> None:
    """If a future refactor drops the adapter_unit_count check, this test
    catches it before it ships. Logic lives in tier_orchestrator.py after PR-4."""
    # Cascade logic moved from generic.py → tier_orchestrator.py in PR-4
    src = Path(
        "ma_poc/pms/adapters/tier_orchestrator.py"
    ).read_text(encoding="utf-8")
    assert "adapter_unit_count" in src, "F12 gate variable missing from tier_orchestrator.py"
    # Both halves of the AND must be present
    assert 'ctx.detected.pms != "unknown"' in src or "ctx.detected.pms != 'unknown'" in src
    assert "adapter_unit_count > 0" in src


def test_f12_scraper_threads_adapter_unit_count_into_fallback_ctx() -> None:
    """The scraper must populate the new field on the fallback ctx so the
    generic adapter sees the upstream's unit count."""
    src = Path("ma_poc/pms/scraper.py").read_text(encoding="utf-8")
    assert "fallback_ctx.adapter_unit_count" in src, (
        "F12: scraper does not thread adapter_unit_count into fallback ctx"
    )
