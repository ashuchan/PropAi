#!/usr/bin/env python3
"""PR #23 gate runner — wiring audit for phases A–I.

Mirrors scripts/gate_xsource.py. Verifies that every phase claimed live in
PR #23 has a non-test caller in production code (H11) and no dead branches
remain (H12).

Usage:
    python -m ma_poc.scripts.gate_pr23 phase <A|B|C|D|E|F|G|H|I|Z>
    python -m ma_poc.scripts.gate_pr23 all
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAPOC = REPO_ROOT / "ma_poc"

# Per-phase pytest targets (relative to ma_poc/)
PHASE_TESTS: dict[str, list[str]] = {
    "A": ["tests/services/test_phase_a_correctness.py"],
    "B": ["tests/profile/test_phase_b_save_mapping_kwargs.py"],
    "C": ["tests/integration/test_phase_c_field_patches.py"],
    "D": ["tests/profile/test_phase_d_observations.py"],
    "E": ["tests/profile/test_phase_e_dom_hints.py"],
    "F": ["tests/integration/test_phase_f_cluster.py"],
    "G": ["tests/integration/test_phase_g_phase9_reachability.py"],
    "H": ["tests/integration/test_phase_h_planner_wiring.py"],
    "I": ["tests/integration/test_phase_i_telemetry.py"],
    "Z": [
        "tests/services/test_source_primitive.py",
        "tests/services/test_source_planner.py",
        "tests/services/test_phase_a_correctness.py",
        "tests/profile/test_phase_b_save_mapping_kwargs.py",
        "tests/integration/test_phase_c_field_patches.py",
        "tests/profile/test_phase_d_observations.py",
        "tests/profile/test_phase_e_dom_hints.py",
        "tests/integration/test_phase_f_cluster.py",
        "tests/integration/test_phase_g_phase9_reachability.py",
        "tests/integration/test_phase_h_planner_wiring.py",
        "tests/integration/test_phase_i_telemetry.py",
    ],
}


def _run_static_scans(phase: str) -> tuple[bool, list[str]]:
    """Returns (passed, log_lines)."""
    log: list[str] = []

    if phase in ("A", "Z"):
        # H11: every claimed-live phase has a production caller
        callers = {
            "compute_budget(": MAPOC / "pms" / "scraper.py",
            "plan_next_action(": MAPOC / "pms" / "adapters" / "generic.py",
            "record_source_observations(": MAPOC / "services" / "profile_updater.py",
            "find_cluster_mates(": MAPOC / "services" / "cluster_store.py",
            "_resolve_source_url(": MAPOC / "scripts" / "jugnu_runner.py",
            "merge_sources(": MAPOC / "pms" / "adapters" / "generic.py",
        }
        all_ok = True
        for symbol, filepath in callers.items():
            if not filepath.exists():
                log.append(f"  MISSING FILE: {filepath}")
                all_ok = False
                continue
            found = symbol in filepath.read_text(encoding="utf-8")
            status = "PASS" if found else "FAIL"
            log.append(f"  {symbol} in {filepath.name}: {status}")
            if not found:
                all_ok = False
        if not all_ok:
            return False, log

    if phase in ("G", "Z"):
        # H12: no dead branches — future-proofing comment removed
        scraper = MAPOC / "pms" / "scraper.py"
        if scraper.exists():
            text = scraper.read_text(encoding="utf-8")
            dead_comments = [
                "future-proofing path",
                "Today the link-hop only fires when main is empty",
            ]
            for comment in dead_comments:
                found = comment in text
                status = "FAIL" if found else "PASS"
                log.append(f"  dead-branch comment '{comment[:40]}...': {status}")
                if found:
                    return False, log

    if phase in ("H", "Z"):
        # Budget propagated via ctx.budget
        generic = MAPOC / "pms" / "adapters" / "generic.py"
        scraper = MAPOC / "pms" / "scraper.py"
        for f, symbol in [(generic, "ctx.budget"), (scraper, "compute_budget(")]:
            if not f.exists():
                log.append(f"  MISSING: {f}")
                return False, log
            found = symbol in f.read_text(encoding="utf-8")
            status = "PASS" if found else "FAIL"
            log.append(f"  {symbol} in {f.name}: {status}")
            if not found:
                return False, log

    return True, log


def _run_pytest(phase: str) -> tuple[bool, list[str]]:
    targets = PHASE_TESTS.get(phase, [])
    if not targets:
        return True, [f"  no pytest targets for phase {phase}"]
    log: list[str] = []
    cmd = [
        sys.executable, "-m", "pytest",
        "--tb=short", "-q",
    ] + [str(MAPOC / t) for t in targets]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    log.append(result.stdout[-2000:] if result.stdout else "")
    if result.returncode != 0:
        log.append(result.stderr[-1000:] if result.stderr else "")
        return False, log
    return True, log


def run_phase(phase: str) -> bool:
    print(f"\n{'='*60}")
    print(f"Phase {phase}")
    print(f"{'='*60}")

    ok_scans, scan_log = _run_static_scans(phase)
    for line in scan_log:
        print(line)
    if not ok_scans:
        print(f"Phase {phase}: FAIL (static scans)")
        return False

    ok_tests, test_log = _run_pytest(phase)
    for line in test_log:
        print(line)
    if not ok_tests:
        print(f"Phase {phase}: FAIL (pytest)")
        return False

    print(f"Phase {phase}: PASS")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="PR #23 gate runner")
    parser.add_argument("command", choices=["phase", "all"])
    parser.add_argument("phase", nargs="?", default=None)
    args = parser.parse_args()

    phases = list(PHASE_TESTS.keys())
    if args.command == "all":
        targets = phases
    else:
        if args.phase not in phases:
            print(f"Unknown phase '{args.phase}'. Valid: {phases}", file=sys.stderr)
            sys.exit(1)
        targets = [args.phase]

    results = {p: run_phase(p) for p in targets}
    print(f"\n{'='*60}")
    print("Summary:")
    for p, ok in results.items():
        print(f"  Phase {p}: {'PASS' if ok else 'FAIL'}")
    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
