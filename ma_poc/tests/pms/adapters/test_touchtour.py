"""Change 4 — TouchTour / Ovation adapter tests."""
from __future__ import annotations

import pytest

import ma_poc.pms.adapters  # noqa: F401  # ensure adapters registry is populated
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.touchtour import TouchTourAdapter
from ma_poc.pms.detector import DetectedPMS, detect_pms


def _make_ctx(api_responses: list[dict]) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://lasvegasliving.mytouchtour.com/community/floorplans/summer_winds",
        detected=DetectedPMS(
            pms="touchtour", confidence=0.95, evidence=["test"],
            recommended_strategy="cascade",
        ),
        profile=None,
        expected_total_units=None,
        property_id="24928",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    pass


# ---------------------------------------------------------------------------
# Adapter behaviour (research-blocked stub)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_touchtour_returns_empty_on_no_data() -> None:
    adapter = TouchTourAdapter()
    ctx = _make_ctx([])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert result.units == []
    assert result.tier_used == "TIER_1_API_TOUCHTOUR_NO_RESPONSE"
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_touchtour_research_blocked_when_responses_present() -> None:
    adapter = TouchTourAdapter()
    ctx = _make_ctx([{
        "url": "https://lasvegasliving.mytouchtour.com/api/floorplans",
        "body": {"units": [{"n": 1}]},
    }])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_TOUCHTOUR_RESEARCH_BLOCKED"
    assert any("RESEARCH_BLOCKED" in e for e in result.errors)


def test_touchtour_static_fingerprints_contains_mytouchtour() -> None:
    assert "mytouchtour.com" in TouchTourAdapter().static_fingerprints()


def test_touchtour_does_not_implement_body_check() -> None:
    # DOM-only adapters opt out of confirm_detection demotion by omitting
    # the method — required for Change 2 to keep URL-based routing intact
    # when no XHR body carries inventory data.
    adapter = TouchTourAdapter()
    assert getattr(adapter, "matches_response_body", None) is None


# ---------------------------------------------------------------------------
# Detector routing
# ---------------------------------------------------------------------------


def test_touchtour_detector_routes_from_mytouchtour_url() -> None:
    r = detect_pms(
        "https://lasvegasliving.mytouchtour.com/community/floorplans/summer_winds"
    )
    assert r.pms == "touchtour"
    assert r.confidence >= 0.95


def test_touchtour_detector_routes_from_liveovation_url() -> None:
    r = detect_pms("https://www.liveovation.com/apartments/madera/")
    assert r.pms == "touchtour"
    assert r.confidence >= 0.85


def test_touchtour_detector_routes_from_mgmt_ovation() -> None:
    r = detect_pms(
        "https://some-vanity.example/",
        csv_row={"Management Company": "Ovation Property Management"},
    )
    assert r.pms == "touchtour"
    assert r.confidence >= 0.70


def test_touchtour_sightmap_no_longer_matches_vegas() -> None:
    # Historical misrouting: the 3 Vegas properties were stamped
    # TIER_1_API_SIGHTMAP. With mytouchtour.com in the host fingerprint,
    # the router must now pick touchtour, not sightmap.
    r = detect_pms(
        "https://lasvegasliving.mytouchtour.com/community/floorplans/positano"
    )
    assert r.pms != "sightmap"
    assert r.pms == "touchtour"


# ---------------------------------------------------------------------------
# Research-blocked real-capture test — surfaces the gate without failing CI.
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="research-blocked: need >=2 real TouchTour captures from "
           "Summer Winds 24928, Madera 26151, Positano 27595"
)
def test_touchtour_real_capture_parser_happy_path() -> None:
    raise AssertionError("placeholder for real-capture validation")
