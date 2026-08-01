"""Extract layer: Entrata per-PMS happy path.

Uses corpus fixture `corpus/entrata/widget_response.json` to drive the Entrata
adapter and asserts that at least one UnitRecord-shaped dict with required
fields is produced.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from pms.adapters.base import AdapterContext
from pms.adapters.entrata import EntrataAdapter

# ── probe-seam stub (2026-07-26) ────────────────────────────────────────────
# ``test_entrata_empty_api_returns_no_units`` passes an empty response list, so
# the adapter walks its static Prospect-Portal fallbacks
# (``_entrata_static_fetch`` → the *sync*
# ``ma_poc.pms.adapters._probe.probe_get`` curl_cffi seam). That fetch was going
# to the live internet (www.testprop.com, a domain the fixture invented). The
# stub answers "nothing here", which is the no-data condition the test
# describes. Corpus-driven tests in this file return before reaching it.


class _NoDataProbeResponse:
    """curl_cffi-response stand-in carrying no usable body.

    Exposes the full attribute surface adapter call sites read off a probe
    response: ``status_code`` / ``text`` / ``content`` / ``headers`` / ``url``.
    """

    def __init__(self, url: str, status_code: int = 404, text: str = "") -> None:
        self.url = url
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _stub_probe_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the ``_probe`` fetch seam at an offline 404/empty-body stub."""

    def _fake_probe_get(url: str = "", **_kw: object) -> _NoDataProbeResponse:
        return _NoDataProbeResponse(url)

    def _fake_probe_post(
        url: str = "", data: object = None, **_kw: object
    ) -> _NoDataProbeResponse:
        return _NoDataProbeResponse(url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _fake_probe_get)
    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_post", _fake_probe_post)


@dataclass
class _StubDetected:
    pms: str = "entrata"
    confidence: float = 0.95
    evidence: list = None  # type: ignore[assignment]
    recommended_strategy: str = "api_first"

    def __post_init__(self) -> None:
        if self.evidence is None:
            self.evidence = ["host ends in entrata.com"]


def _make_ctx(api_responses: list[dict[str, Any]], property_id: str = "prop-en-001") -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://www.testprop.com/",
        detected=_StubDetected(),  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id=property_id,
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


def test_entrata_corpus_preserves_plans_without_fabricating_units(corpus: Any) -> None:
    """Entrata's flat floor-plan catalogue must not fabricate unit IDs."""
    api_responses = corpus.load_json("entrata/widget_response.json")
    ctx = _make_ctx(api_responses)
    result = asyncio.run(EntrataAdapter().extract(page=None, ctx=ctx))

    assert result.units == []
    assert len(result.plan_summaries) == 3
    assert "PLAN_LEVEL" in result.tier_used
    assert all(not plan.get("unit_number") for plan in result.plan_summaries)


def test_entrata_corpus_tier_is_api(corpus: Any) -> None:
    """Entrata extraction via widget API wins the API tier."""
    api_responses = corpus.load_json("entrata/widget_response.json")
    ctx = _make_ctx(api_responses)
    result = asyncio.run(EntrataAdapter().extract(page=None, ctx=ctx))

    assert "TIER_1" in result.tier_used or "API" in result.tier_used, (
        f"Expected a tier-1 API win but tier_used={result.tier_used!r}"
    )


def test_entrata_corpus_confidence_above_threshold(corpus: Any) -> None:
    """A preserved plan catalogue should still carry bounded confidence."""
    api_responses = corpus.load_json("entrata/widget_response.json")
    ctx = _make_ctx(api_responses)
    result = asyncio.run(EntrataAdapter().extract(page=None, ctx=ctx))

    assert result.units == []
    assert len(result.plan_summaries) == 3
    assert result.confidence >= 0.7, (
        f"Expected confidence ≥ 0.7 but got {result.confidence}"
    )


def test_entrata_empty_api_returns_no_units() -> None:
    """When no Entrata-shaped responses are present, result is empty."""
    ctx = _make_ctx([])
    result = asyncio.run(EntrataAdapter().extract(page=None, ctx=ctx))

    assert result.units == []


def test_entrata_flat_list_fixture_preserves_multiple_plans(corpus: Any) -> None:
    """The corpus has three floor plans; all remain plan summaries."""
    api_responses = corpus.load_json("entrata/widget_response.json")
    ctx = _make_ctx(api_responses)
    result = asyncio.run(EntrataAdapter().extract(page=None, ctx=ctx))

    assert result.units == []
    assert len(result.plan_summaries) == 3
