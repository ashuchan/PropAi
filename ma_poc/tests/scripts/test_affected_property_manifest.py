"""Determinism and coverage checks for the zero-cost focused-canary input."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_ROOT = REPO_ROOT / "investigations/2026-08-01-consolidated-canary/affected-property-manifest-v1"
BUILDER = REPO_ROOT / "investigations/2026-08-01-consolidated-canary/build_affected_property_manifest.py"


def test_manifest_is_complete_and_byte_deterministic() -> None:
    subprocess.run(
        [sys.executable, str(BUILDER), "--check"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_manifest_represents_every_finding_and_has_job_safe_input() -> None:
    summary = json.loads((MANIFEST_ROOT / "manifest_summary.json").read_text())
    coverage = json.loads((MANIFEST_ROOT / "finding_coverage.json").read_text())
    launch_contract = json.loads((MANIFEST_ROOT / "future_launch_contract.json").read_text())
    with (MANIFEST_ROOT / "launch_properties.csv").open(newline="") as handle:
        launch_rows = list(csv.DictReader(handle))

    assert summary["zero_cost_local_only"] is True
    assert summary["build_or_deploy_performed"] is False
    assert summary["job_started"] is False
    assert summary["finding_ids"] == list(range(1, 50))
    assert coverage["finding_ids"] == list(range(1, 50))
    assert launch_contract["launch_authorized"] is False
    assert launch_contract["build_or_deploy_performed"] is False
    assert launch_contract["job_started"] is False
    assert launch_contract["property_count"] == len(launch_rows)
    assert launch_contract["environment"] == {
        "COMPLIANCE_MODE": "1",
        "ENABLE_UNLOCKER_TIER": "false",
        "FETCH_BACKEND": "hyperbrowser",
        "HYPERBROWSER_MAX_CALLS_PER_PROPERTY": "3",
        "HYPERBROWSER_RESERVED_PRIORITY_CALLS": "1",
    }
    assert len(coverage["findings"]) == 49
    assert len(launch_rows) == summary["unique_launch_property_count"]
    assert tuple(launch_rows[0]) == (
        "apartmentid",
        "name",
        "address",
        "city",
        "state",
        "zip",
        "website",
    )
    assert len({row["apartmentid"] for row in launch_rows}) == len(launch_rows)
