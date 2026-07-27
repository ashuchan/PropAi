"""Extract layer: RentCafe per-PMS happy path.

Uses corpus fixture `corpus/rentcafe/api_units.json` to drive the RentCafe
adapter and asserts that at least one UnitRecord-shaped dict with required
fields (beds, rent) is produced.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from pms.adapters.base import AdapterContext
from pms.adapters.rentcafe import RentCafeAdapter
from pms.detector import DetectedPMS


# ── probe-seam stub (2026-07-26) ────────────────────────────────────────────
# ``test_rentcafe_empty_api_returns_no_units`` passes an empty response list, so
# the adapter runs its recovery fallbacks — including the securecafe homepage
# re-fetch (rentcafe.py:1420) through the *sync*
# ``ma_poc.pms.adapters._probe.probe_get`` curl_cffi seam. That fetch was going
# to the live internet (testprop.rentcafe.com, a domain the fixture invented).
# The stub answers "nothing here", which is the no-data condition the test
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
    pms: str = "rentcafe"
    confidence: float = 0.95
    evidence: list = None  # type: ignore[assignment]
    recommended_strategy: str = "api_first"

    def __post_init__(self) -> None:
        if self.evidence is None:
            self.evidence = ["host ends in rentcafe.com"]


def _make_ctx(api_responses: list[dict[str, Any]], property_id: str = "prop-rc-001") -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://testprop.rentcafe.com/apartments/",
        detected=_StubDetected(),  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id=property_id,
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


def test_rentcafe_corpus_extracts_units(corpus: Any) -> None:
    """RentCafe corpus fixture → at least 1 unit with beds and rent fields."""
    api_responses = corpus.load_json("rentcafe/api_units.json")
    ctx = _make_ctx(api_responses)
    result = asyncio.run(RentCafeAdapter().extract(page=None, ctx=ctx))

    assert len(result.units) >= 1, (
        f"Expected at least 1 unit from RentCafe corpus but got 0. "
        f"tier_used={result.tier_used!r}, errors={result.errors}"
    )
    for unit in result.units:
        assert isinstance(unit, dict)
        has_beds = unit.get("bedrooms") is not None or unit.get("beds") is not None
        has_rent = (
            unit.get("market_rent_low") is not None
            or unit.get("rent_low") is not None
            or unit.get("rent_range") is not None
            or unit.get("asking_rent") is not None
        )
        assert has_beds or has_rent, (
            f"RentCafe unit missing beds AND rent fields: {unit}"
        )


def test_rentcafe_corpus_tier_is_api(corpus: Any) -> None:
    """RentCafe extraction via API responses wins the API tier."""
    api_responses = corpus.load_json("rentcafe/api_units.json")
    ctx = _make_ctx(api_responses)
    result = asyncio.run(RentCafeAdapter().extract(page=None, ctx=ctx))

    assert "TIER_1" in result.tier_used or "API" in result.tier_used, (
        f"Expected a tier-1 API win but tier_used={result.tier_used!r}"
    )


def test_rentcafe_corpus_confidence_above_threshold(corpus: Any) -> None:
    """Corpus extraction should have confidence ≥ 0.7."""
    api_responses = corpus.load_json("rentcafe/api_units.json")
    ctx = _make_ctx(api_responses)
    result = asyncio.run(RentCafeAdapter().extract(page=None, ctx=ctx))

    assert len(result.units) >= 1
    assert result.confidence >= 0.7, (
        f"Expected confidence ≥ 0.7 but got {result.confidence}"
    )


def test_rentcafe_empty_api_returns_no_units() -> None:
    """When no RentCafe-shaped responses are present, result is empty."""
    ctx = _make_ctx([])
    result = asyncio.run(RentCafeAdapter().extract(page=None, ctx=ctx))

    assert result.units == []
    assert result.confidence == 0.0


def test_rentcafe_corpus_all_units_have_floor_plan_name(corpus: Any) -> None:
    """RentCafe corpus units should have a floor_plan_name set."""
    api_responses = corpus.load_json("rentcafe/api_units.json")
    ctx = _make_ctx(api_responses)
    result = asyncio.run(RentCafeAdapter().extract(page=None, ctx=ctx))

    assert len(result.units) >= 1
    for unit in result.units:
        has_name = (
            unit.get("floor_plan_name") is not None
            or unit.get("floorplanName") is not None
            or unit.get("name") is not None
        )
        assert has_name, f"RentCafe unit missing floor plan name: {unit}"
