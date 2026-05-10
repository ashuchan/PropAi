"""Consume layer: TypeScript data-provider parity (skipped by default).

These tests validate that the TypeScript-side data provider produces
output compatible with the Python consume layer expectations.  They are
marked ``ts`` and skipped unless the TypeScript side is built and
``--run-ts`` is passed to pytest.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.ts


@pytest.fixture(autouse=True)
def _skip_unless_ts(request: pytest.FixtureRequest) -> None:
    """Skip all tests in this module unless --run-ts was supplied."""
    if not request.config.getoption("--run-ts", default=False):
        pytest.skip("TypeScript provider not built; pass --run-ts to enable")


def test_ts_provider_amenities_schema_matches_python() -> None:
    """TS amenities_report.json schema matches Python output schema."""
    # Placeholder: real test would read a TS-generated amenities_report.json
    # and assert the same top-level keys as build_amenities_report() produces.
    pass


def test_ts_provider_concessions_schema_matches_python() -> None:
    """TS concessions_report.json schema matches Python output schema."""
    pass


def test_ts_provider_run_report_keys_match_python() -> None:
    """TS run_report.json top-level keys match reporting.run_report.build() output."""
    pass
