"""
PR-7 — TDD tests for daily_runner.py decomposition.

These tests verify:
1. Each new sub-module is importable from its target package.
2. The key functions are importable from their new canonical locations.
3. Backward compat: all public names are still importable from scripts.daily_runner.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. New sub-packages are importable
# ---------------------------------------------------------------------------


def test_state_package_importable():
    import scripts.state  # noqa: F401


def test_state_ledger_importable():
    import scripts.state.ledger  # noqa: F401


def test_state_property_record_importable():
    import scripts.state.property_record  # noqa: F401


def test_state_csv_reader_importable():
    import scripts.state.csv_reader  # noqa: F401


def test_orchestration_package_importable():
    import scripts.orchestration  # noqa: F401


def test_orchestration_scrape_worker_importable():
    import scripts.orchestration.scrape_worker  # noqa: F401


def test_reporting_package_importable():
    import scripts.reporting  # noqa: F401


def test_reporting_markdown_importable():
    import scripts.reporting.markdown  # noqa: F401


def test_reporting_issues_importable():
    import scripts.reporting.issues  # noqa: F401


# ---------------------------------------------------------------------------
# 2. Functions are importable from their new canonical locations
# ---------------------------------------------------------------------------


def test_read_properties_csv_in_csv_reader():
    from scripts.state.csv_reader import read_properties_csv  # noqa: F401

    assert callable(read_properties_csv)


def test_build_property_record_in_property_record():
    from scripts.state.property_record import build_property_record  # noqa: F401

    assert callable(build_property_record)


def test_append_ledger_in_ledger():
    from scripts.state.ledger import _append_ledger  # noqa: F401

    assert callable(_append_ledger)


def test_load_ledger_in_ledger():
    from scripts.state.ledger import load_ledger  # noqa: F401

    assert callable(load_ledger)


def test_write_markdown_report_in_markdown():
    from scripts.reporting.markdown import _write_markdown_report  # noqa: F401

    assert callable(_write_markdown_report)


def test_write_issues_jsonl_in_issues():
    from scripts.reporting.issues import _write_issues_jsonl  # noqa: F401

    assert callable(_write_issues_jsonl)


def test_scrape_one_in_scrape_worker():
    from scripts.orchestration.scrape_worker import _scrape_one  # noqa: F401

    assert callable(_scrape_one)


def test_scrape_in_thread_in_scrape_worker():
    from scripts.orchestration.scrape_worker import _scrape_in_thread  # noqa: F401

    assert callable(_scrape_in_thread)


# ---------------------------------------------------------------------------
# 3. Backward compat: all names still importable from scripts.daily_runner
# ---------------------------------------------------------------------------


def test_backward_compat_read_properties_csv():
    from scripts.daily_runner import read_properties_csv  # noqa: F401

    assert callable(read_properties_csv)


def test_backward_compat_build_property_record():
    from scripts.daily_runner import build_property_record  # noqa: F401

    assert callable(build_property_record)


def test_backward_compat_append_ledger():
    from scripts.daily_runner import _append_ledger  # noqa: F401

    assert callable(_append_ledger)


def test_backward_compat_load_ledger():
    from scripts.daily_runner import load_ledger  # noqa: F401

    assert callable(load_ledger)


def test_backward_compat_write_markdown_report():
    from scripts.daily_runner import _write_markdown_report  # noqa: F401

    assert callable(_write_markdown_report)


def test_backward_compat_write_issues_jsonl():
    from scripts.daily_runner import _write_issues_jsonl  # noqa: F401

    assert callable(_write_issues_jsonl)


def test_backward_compat_run_daily():
    from scripts.daily_runner import run_daily  # noqa: F401

    assert callable(run_daily)


def test_backward_compat_main():
    from scripts.daily_runner import main  # noqa: F401

    assert callable(main)
