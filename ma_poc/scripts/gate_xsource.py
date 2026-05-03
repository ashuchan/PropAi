#!/usr/bin/env python3
"""Cross-source + self-learning gate runner.

Mirrors scripts/gate_refactor.py. Runs the per-phase test files and the
static scans defined in CLAUDE_XSOURCE_AND_LEARNING.

Usage:
    python -m ma_poc.scripts.gate_xsource phase <N>
    python -m ma_poc.scripts.gate_xsource all

A phase passes iff:
  1. its pytest files all pass
  2. its static scans return the expected hit counts (0 or >=N)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAPOC = REPO_ROOT / "ma_poc"

# Per-phase pytest targets (relative to ma_poc/)
PHASE_TESTS: dict[int, list[str]] = {
    1: ["tests/profile/test_phase1_writers.py"],
    2: ["tests/services/test_source_primitive.py"],
    3: ["tests/services/test_source_merger.py"],
    4: ["tests/services/test_source_planner.py"],
    5: ["tests/integration/test_phase5_cascade_merge.py", "tests/pms/"],
    6: ["tests/profile/test_mapping_eviction.py"],
    7: ["tests/profile/test_field_patches.py"],
    8: ["tests/profile/test_dom_hints_wiring.py"],
    9: ["tests/integration/test_phase9_cross_page.py", "tests/integration/test_loop_safeguards.py"],
    10: ["tests/profile/test_mapping_eviction.py::test_save_mapping_self_validation_demotes",
         "tests/profile/test_mapping_eviction.py::test_save_mapping_full_replay_keeps_quality_one",
         "tests/profile/test_mapping_eviction.py::test_cold_run_count_increments_for_cold_profile",
         "tests/profile/test_mapping_eviction.py::test_cold_run_count_resets_on_promotion",
         "tests/services/test_source_planner.py::test_compute_budget_warm_full",
         "tests/services/test_source_planner.py::test_compute_budget_cold_rotates"],
    11: ["tests/profile/test_source_observations.py"],
    12: ["tests/profile/test_cluster_bootstrap.py"],
    13: ["tests/observability/test_source_telemetry.py"],
}


def _scan_count(pattern: str, paths: list[Path]) -> int:
    """Count occurrences of `pattern` (regex) across files under paths."""
    rx = re.compile(pattern)
    n = 0
    for p in paths:
        if not p.exists():
            continue
        for f in p.rglob("*.py"):
            try:
                txt = f.read_text(encoding="utf-8")
            except OSError:
                continue
            n += sum(1 for _ in rx.finditer(txt))
    return n


def _run_static_scans(phase: int) -> tuple[bool, list[str]]:
    """Returns (passed, log_lines)."""
    log: list[str] = []

    if phase == 1:
        # >=4 stats writers in profile_updater.py
        text = (MAPOC / "services" / "profile_updater.py").read_text(encoding="utf-8")
        n = len(re.findall(r"profile\.stats\.total_(scrapes|successes|failures|llm_calls)\s*\+=", text))
        ok = n >= 4
        log.append(f"  stats_writers_present: count={n}, expected>=4 --> {'PASS' if ok else 'FAIL'}")
        if not ok:
            return False, log

    if phase == 2:
        # Closed enum: no string literals for SourceId values outside source.py / source_planner.py / source_merger.py / source_observer.py
        suspect = [
            r'"mapping_replay"',
            r'"field_patch"',
            r'"cluster_mapping_replay"',
            r'"llm_monolithic"',
            r'"dom_profile_hints"',
        ]
        rx = re.compile("|".join(suspect))
        allowed = {
            MAPOC / "models" / "source.py",
            MAPOC / "services" / "source_planner.py",
            MAPOC / "services" / "source_merger.py",
            MAPOC / "services" / "source_observer.py",
            MAPOC / "observability" / "source_telemetry.py",
        }
        hits = []
        for scan_dir in (MAPOC / "services", MAPOC / "pms", MAPOC / "observability"):
            for f in scan_dir.rglob("*.py"):
                if f in allowed:
                    continue
                try:
                    txt = f.read_text(encoding="utf-8")
                except OSError:
                    continue
                if rx.search(txt):
                    hits.append(str(f.relative_to(REPO_ROOT)))
        ok = not hits
        log.append(f"  source_id_enum_closed: hits={hits or 0} --> {'PASS' if ok else 'FAIL'}")
        if not ok:
            return False, log

    if phase == 3:
        # Merger purity — no logging, no profile mutations
        text = (MAPOC / "services" / "source_merger.py").read_text(encoding="utf-8")
        # Strip module docstring before scanning
        body_start = text.find('"""', text.find('"""') + 3)
        body = text[body_start + 3:] if body_start != -1 else text
        bad = []
        if "import logging" in body:
            bad.append("import logging")
        if re.search(r"\blog\.(warning|info|error|debug)\b", body):
            bad.append("log.* call")
        if re.search(r"\bprofile\.[a-z_]", body):
            bad.append("profile. mutation")
        ok = not bad
        log.append(f"  merger_purity: violations={bad or 'none'} --> {'PASS' if ok else 'FAIL'}")
        if not ok:
            return False, log

    return True, log


def run_phase(phase: int) -> int:
    print(f"=== Phase {phase} gate ===")

    tests = PHASE_TESTS.get(phase, [])
    if tests:
        cmd = [sys.executable, "-m", "pytest", "-v", "--tb=short", *tests]
        rc = subprocess.run(cmd, cwd=str(MAPOC)).returncode
        if rc != 0:
            print(f"FAIL: pytest returned {rc}")
            return rc
    elif PHASE_TESTS.get(phase) is None:
        print(f"  no test mapping for phase {phase}")
    else:
        print(f"  phase {phase} has no test target yet (cascade restructure pending)")

    ok, log = _run_static_scans(phase)
    for line in log:
        print(line)
    if not ok:
        return 2

    print(f"=== Phase {phase} gate: PASS ===")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("phase")
    p.add_argument("number", type=int)
    sub.add_parser("all")

    args = parser.parse_args()

    if args.cmd == "all":
        for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13):
            rc = run_phase(n)
            if rc != 0:
                return rc
        return 0
    return run_phase(args.number)


if __name__ == "__main__":
    sys.exit(main())
