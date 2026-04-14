"""Tests for drift detector — claude-scrapper-arch.md Step 6.5."""
from __future__ import annotations

from models.scrape_profile import ProfileMaturity, ScrapeProfile
from services.drift_detector import apply_drift_demotion, detect_drift


def _make_profile(maturity: ProfileMaturity, last_count: int = 10) -> ScrapeProfile:
    p = ScrapeProfile(canonical_id="drift-test")
    p.confidence.maturity = maturity
    p.confidence.last_unit_count = last_count
    return p


def test_no_drift_on_cold_profile() -> None:
    p = _make_profile(ProfileMaturity.COLD)
    detected, reasons = detect_drift(p, 0, {"units": []})
    assert detected is False
    assert reasons == []


def test_unit_count_drop_30pct_detected() -> None:
    p = _make_profile(ProfileMaturity.HOT, last_count=20)
    detected, reasons = detect_drift(p, 10, {"units": []})
    assert detected is True
    assert any("unit_count_drop" in r for r in reasons)


def test_all_rents_null_detected() -> None:
    p = _make_profile(ProfileMaturity.WARM, last_count=5)
    units = [{"unit_id": str(i)} for i in range(5)]  # No rent data
    detected, reasons = detect_drift(p, 5, {"units": units})
    assert detected is True
    assert any("all_rents_null" in r for r in reasons)


def test_timeout_pattern_detected_after_3_failures() -> None:
    p = _make_profile(ProfileMaturity.WARM)
    p.confidence.consecutive_failures = 2
    detected, reasons = detect_drift(p, 0, {"units": [], "_timeout": True})
    assert detected is True
    assert any("timeout_pattern" in r for r in reasons)


def test_severe_drift_demotes_to_cold() -> None:
    p = _make_profile(ProfileMaturity.HOT)
    p.confidence.consecutive_successes = 5
    p = apply_drift_demotion(p, ["all_rents_null: 10/10 units have no rent data"])
    assert p.confidence.maturity == ProfileMaturity.COLD
    assert p.confidence.consecutive_successes == 0


def test_mild_drift_demotes_hot_to_warm() -> None:
    p = _make_profile(ProfileMaturity.HOT)
    p.confidence.consecutive_successes = 5
    p = apply_drift_demotion(p, ["unit_count_drop: expected ~20, got 12"])
    assert p.confidence.maturity == ProfileMaturity.WARM
    assert p.confidence.consecutive_successes == 0


def test_no_drift_no_demotion() -> None:
    p = _make_profile(ProfileMaturity.HOT)
    detected, reasons = detect_drift(p, 10, {"units": [{"rent_range": "$1200"}] * 10})
    assert detected is False
    # No demotion needed
    assert p.confidence.maturity == ProfileMaturity.HOT
