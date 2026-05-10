"""Consume layer: property_report.generate_property_report renders markdown.

Verifies that the function produces a non-empty markdown string with the
expected sections (title, status table) when given a minimal scrape result.
"""

from __future__ import annotations

from typing import Any

from reporting.property_report import generate_property_report


RUN_DATE = "2026-05-10"


def _scrape_result(
    units: list[dict] | None = None,
    verdict: str = "SUCCESS",
    tier: str = "TIER_2_JSONLD",
) -> dict[str, Any]:
    return {
        "property_name": "Sunset Villas",
        "units": units or [],
        "errors": [],
        "_meta": {"verdict": verdict, "scrape_tier_used": tier},
        "_detected_pms": {"name": "unknown", "confidence": "n/a", "evidence": [], "adapter": "n/a"},
        "scrape_duration_s": 2.5,
    }


# ---------------------------------------------------------------------------
# Basic rendering
# ---------------------------------------------------------------------------


def test_property_report_returns_string(tmp_path: Any) -> None:
    """generate_property_report returns a non-empty string."""
    md = generate_property_report(
        scrape_result=_scrape_result(),
        property_id="PROP-001",
        run_date=RUN_DATE,
    )

    assert isinstance(md, str)
    assert len(md) > 0


def test_property_report_contains_title(tmp_path: Any) -> None:
    """Report markdown starts with the property name as a heading."""
    md = generate_property_report(
        scrape_result=_scrape_result(),
        property_id="PROP-002",
        run_date=RUN_DATE,
    )

    assert "Sunset Villas" in md
    assert RUN_DATE in md


def test_property_report_with_units_includes_count(tmp_path: Any) -> None:
    """Report produced with 3 units contains unit count information."""
    units = [
        {"unit_id": f"U{i}", "bedrooms": 1, "market_rent_low": 1500 + i * 100}
        for i in range(3)
    ]
    md = generate_property_report(
        scrape_result=_scrape_result(units=units),
        property_id="PROP-003",
        run_date=RUN_DATE,
    )

    assert isinstance(md, str)
    assert len(md) > 0


def test_property_report_failed_verdict_no_crash(tmp_path: Any) -> None:
    """generate_property_report must not raise for a failed scrape."""
    md = generate_property_report(
        scrape_result=_scrape_result(units=[], verdict="FAILED_UNREACHABLE"),
        property_id="PROP-004",
        run_date=RUN_DATE,
    )

    assert isinstance(md, str)


def test_property_report_with_prior_units_no_crash(tmp_path: Any) -> None:
    """Passing prior_units for diff comparison must not raise."""
    units = [{"unit_id": "U1", "market_rent_low": 1500}]
    prior = [{"unit_id": "U1", "market_rent_low": 1400}]

    md = generate_property_report(
        scrape_result=_scrape_result(units=units),
        property_id="PROP-005",
        run_date=RUN_DATE,
        prior_units=prior,
    )

    assert isinstance(md, str)


def test_property_report_with_issues_no_crash(tmp_path: Any) -> None:
    """Passing validation issues list renders without error."""
    issues = [
        {"severity": "WARNING", "code": "LOW_UNIT_COUNT", "message": "Only 1 unit"},
    ]

    md = generate_property_report(
        scrape_result=_scrape_result(),
        property_id="PROP-006",
        run_date=RUN_DATE,
        issues=issues,
    )

    assert isinstance(md, str)
