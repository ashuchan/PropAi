"""PR24 Fix 1 — budget key rename regression tests."""

from __future__ import annotations

import sys
import types

for _name in ("playwright", "playwright.async_api"):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.BrowserContext = object  # type: ignore[attr-defined]
        _mod.Page = object  # type: ignore[attr-defined]
        _mod.async_playwright = object  # type: ignore[attr-defined]
        sys.modules[_name] = _mod

from ma_poc.services.source_planner import compute_budget


class _HotProfile:
    class confidence:
        cold_run_count = 0
        from ma_poc.models.scrape_profile import ProfileMaturity
        maturity = ProfileMaturity.HOT


def test_budget_keys_warm() -> None:
    """compute_budget returns the new key names, not the old llm_targeted."""
    b = compute_budget(_HotProfile(), is_cold=False)
    assert "llm_api_calls" in b, "llm_api_calls key missing from warm budget"
    assert "llm_dom_calls" in b, "llm_dom_calls key missing from warm budget"
    assert "llm_targeted" not in b, "old llm_targeted key must not be present"
    assert b["llm_api_calls"] == 3
    assert b["llm_dom_calls"] == 1
    assert b["llm_monolithic"] == 1
    assert b["link_hop"] == 3


def test_budget_keys_cold_rotation() -> None:
    """COLD budget rotations also use new key names."""
    class P:
        class confidence:
            cold_run_count = 0
    for cnt in (0, 1, 2):
        P.confidence.cold_run_count = cnt
        b = compute_budget(P(), is_cold=True)
        assert "llm_targeted" not in b, f"old key found at cold_run_count={cnt}"
        assert "llm_api_calls" in b
        assert "llm_dom_calls" in b


def test_budget_values_warm() -> None:
    """HOT/WARM budget: 3 api calls, 1 dom call, 1 monolithic, 3 link-hops."""
    b = compute_budget(_HotProfile(), is_cold=False)
    assert b["llm_api_calls"] >= 1
    assert b["llm_dom_calls"] >= 1
    assert b["llm_monolithic"] >= 1
    assert b["link_hop"] >= 1


def test_adapter_context_default_budget_keys() -> None:
    """AdapterContext default budget uses new key names."""
    from ma_poc.pms.adapters.base import AdapterContext
    from ma_poc.pms.detector import DetectedPMS
    ctx = AdapterContext(
        base_url="https://example.com",
        detected=DetectedPMS(pms="unknown", confidence=0.0),
        profile=None,
        expected_total_units=None,
        property_id="test",
    )
    assert "llm_api_calls" in ctx.budget
    assert "llm_dom_calls" in ctx.budget
    assert "llm_targeted" not in ctx.budget
