"""Tests for F4 — available_date DOM extraction."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

from ma_poc.pms.adapters._html_extract import extract_available_date_from_card


def _future_iso(days: int = 30) -> str:
    from datetime import timedelta
    return (date.today() + timedelta(days=days)).isoformat()


def _past_iso(days: int = 5) -> str:
    from datetime import timedelta
    return (date.today() - timedelta(days=days)).isoformat()


def test_available_date_extracts_from_data_available_date_attr() -> None:
    future = _future_iso(30)
    html = f'<div data-available-date="{future}">Unit 101</div>'
    result = extract_available_date_from_card(html)
    assert result == future


def test_available_date_extracts_from_time_element() -> None:
    future = _future_iso(15)
    html = f'<time datetime="{future}">Available {future}</time>'
    result = extract_available_date_from_card(html)
    assert result == future


def test_available_date_extracts_from_class_available() -> None:
    future = _future_iso(20)
    html = f'<span class="available-date">{future}</span>'
    result = extract_available_date_from_card(html)
    assert result == future


def test_available_date_regex_handles_jan_15_2026() -> None:
    html = '<div class="unit-card">Available Jan 15, 2026</div>'
    with patch("ma_poc.pms.adapters._html_extract._today_date", return_value=date(2026, 1, 1)):
        result = extract_available_date_from_card(html)
    assert result == "2026-01-15"


def test_available_date_regex_handles_1_15_2026() -> None:
    html = '<div class="unit-card">Move in: 1/15/2026</div>'
    with patch("ma_poc.pms.adapters._html_extract._today_date", return_value=date(2026, 1, 1)):
        result = extract_available_date_from_card(html)
    assert result == "2026-01-15"


def test_available_date_regex_case_insensitive() -> None:
    html = '<div>AVAILABLE: Jan 15, 2026</div>'
    with patch("ma_poc.pms.adapters._html_extract._today_date", return_value=date(2026, 1, 1)):
        result = extract_available_date_from_card(html)
    assert result == "2026-01-15"


def test_available_date_skips_past_dates_unless_available_now() -> None:
    past = _past_iso(5)
    html = f'<div class="available-date">{past}</div>'
    result = extract_available_date_from_card(html)
    assert result is None


def test_available_date_normalizes_to_iso_yyyy_mm_dd() -> None:
    future = _future_iso(30)
    html = f'<time datetime="{future}">some text</time>'
    result = extract_available_date_from_card(html)
    assert result is not None
    parts = result.split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 4  # YYYY


def test_run_report_annotates_non_extracted_fields() -> None:
    from pathlib import Path
    from ma_poc.reporting import run_report

    with patch.object(Path, "write_text"):
        report = run_report.build(
            properties=[],
            run_dir=Path("/tmp/test_run"),
            run_date="2026-04-20",
        )

    assert "non_extracted_fields" in report
    assert "lease_term" in report["non_extracted_fields"]
    assert "move_in_date" in report["non_extracted_fields"]
