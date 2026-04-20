"""Change 5 — LLM escalation gate tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ma_poc.services.llm_gate import (
    LLM_GATE_DETERMINISTIC_TIERS_SUFFICED,
    LLM_GATE_FALLBACK_JUSTIFIED,
    LLM_GATE_NO_BODY,
    LLM_GATE_TIER1_SHAPE_MATCHED,
    page_has_meaningful_body,
    should_escalate_to_llm,
)


@dataclass
class _FakeAdapterResult:
    api_responses: list[dict[str, Any]] = field(default_factory=list)
    units: list[dict[str, Any]] = field(default_factory=list)


_MEANINGFUL_HTML = (
    "<html><body>" + ("<p>rent available apartment studio floor</p>" * 80) + "</body></html>"
)
assert len(_MEANINGFUL_HTML.encode("utf-8")) > 1024


def test_llm_gate_skips_on_no_body() -> None:
    d = should_escalate_to_llm(html=None, tier1_result=None)
    assert d.escalate is False
    assert LLM_GATE_NO_BODY in d.reason


def test_llm_gate_skips_on_tiny_body() -> None:
    d = should_escalate_to_llm(html="<html>hi</html>", tier1_result=None)
    assert d.escalate is False
    assert LLM_GATE_NO_BODY in d.reason


def test_llm_gate_skips_on_body_without_rent_tokens() -> None:
    padded = "<html>" + ("x" * 2000) + "</html>"
    d = should_escalate_to_llm(html=padded, tier1_result=None)
    assert d.escalate is False
    assert LLM_GATE_NO_BODY in d.reason


def test_llm_gate_escalates_on_shape_matched_but_empty() -> None:
    tier1 = _FakeAdapterResult(
        api_responses=[{"url": "https://x", "body": {"data": [1]}}]
    )
    d = should_escalate_to_llm(html=_MEANINGFUL_HTML, tier1_result=tier1)
    assert d.escalate is True
    assert LLM_GATE_TIER1_SHAPE_MATCHED in d.reason


def test_llm_gate_skips_when_tier3_got_units() -> None:
    d = should_escalate_to_llm(
        html=_MEANINGFUL_HTML,
        tier1_result=_FakeAdapterResult(),
        tier3_units=[{"unit_number": "101"}] * 5,
    )
    assert d.escalate is False
    assert LLM_GATE_DETERMINISTIC_TIERS_SUFFICED in d.reason


def test_llm_gate_skips_when_tier2_got_units() -> None:
    d = should_escalate_to_llm(
        html=_MEANINGFUL_HTML,
        tier1_result=None,
        tier2_units=[{"unit_number": "A"}],
    )
    assert d.escalate is False
    assert LLM_GATE_DETERMINISTIC_TIERS_SUFFICED in d.reason


def test_llm_gate_escalates_on_fallback() -> None:
    d = should_escalate_to_llm(
        html=_MEANINGFUL_HTML,
        tier1_result=_FakeAdapterResult(),
    )
    assert d.escalate is True
    assert LLM_GATE_FALLBACK_JUSTIFIED in d.reason


def test_llm_gate_handles_dict_duck_typed_tier1() -> None:
    # Orchestrator may pass a dict rather than an AdapterResult — gate must
    # not care about the type as long as api_responses is exposed.
    tier1 = {"api_responses": [{"url": "https://x", "body": {"k": 1}}]}
    d = should_escalate_to_llm(html=_MEANINGFUL_HTML, tier1_result=tier1)
    assert d.escalate is True
    assert LLM_GATE_TIER1_SHAPE_MATCHED in d.reason


def test_page_has_meaningful_body_thresholds() -> None:
    # None / empty → False
    assert page_has_meaningful_body(None) is False
    assert page_has_meaningful_body("") is False
    # Large body without rent tokens → False
    assert page_has_meaningful_body("a" * 4096) is False
    # Large body with rent token → True
    assert page_has_meaningful_body("x" * 2000 + " floor plan ") is True
    # Just over threshold → True
    body = ("apartment " * 120)
    assert len(body.encode("utf-8")) > 1024
    assert page_has_meaningful_body(body) is True
