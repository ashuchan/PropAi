#!/usr/bin/env python3
"""PR #25 gate runner — bug-fix audit for B1 + B2 + 8 missing tests + 2 stale tests.

Verifies:
  B1:    quality_score wired into replay confidence + field-rich gate
  B2:    FieldPatch hit/miss counters wired + eviction fires after 3 misses
  T1-T6: _merge_into_result_units rank-ladder tests
  T7-T8: E2E cross-source merge tests
  S1-S2: Stale tests updated to new budget keys

Usage:
    python -m ma_poc.scripts.gate_pr25 all
    python -m ma_poc.scripts.gate_pr25 phase B1
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAPOC = REPO_ROOT / "ma_poc"

FIX_TESTS: dict[str, list[str]] = {
    "B1":   ["tests/integration/test_pr25_quality_score_wiring.py"],
    "B2":   ["tests/profile/test_pr25_field_patch_eviction.py"],
    "T1-T6": ["tests/integration/test_pr25_merge_into_result_units.py"],
    "T7-T8": ["tests/integration/test_pr25_e2e_cross_source.py"],
    "S1-S2": ["tests/services/test_source_planner.py"],
    "Z": [
        "tests/integration/test_pr25_quality_score_wiring.py",
        "tests/profile/test_pr25_field_patch_eviction.py",
        "tests/integration/test_pr25_merge_into_result_units.py",
        "tests/integration/test_pr25_e2e_cross_source.py",
        "tests/services/test_source_planner.py",
    ],
}


def _run_static_scans(fix: str) -> tuple[bool, list[str]]:
    log: list[str] = []
    ok = True

    def _check_symbol(symbol: str, filepath: Path) -> bool:
        if not filepath.exists():
            log.append(f"  MISSING: {filepath}")
            return False
        found = symbol in filepath.read_text(encoding="utf-8")
        log.append(f"  {symbol!r} in {filepath.name}: {'PASS' if found else 'FAIL'}")
        return found

    def _check_absent(symbol: str, filepath: Path) -> bool:
        if not filepath.exists():
            log.append(f"  MISSING: {filepath}")
            return False
        found = symbol in filepath.read_text(encoding="utf-8")
        log.append(f"  absent {symbol!r} in {filepath.name}: {'FAIL' if found else 'PASS'}")
        return not found

    if fix in ("B1", "Z"):
        # B1: quality_score is read by generic.py
        generic = MAPOC / "pms" / "adapters" / "generic.py"
        ok &= _check_symbol("quality_score", generic)
        ok &= _check_symbol("_aggregate_quality(", generic)
        ok &= _check_symbol("agg_q >= 0.7", generic)

    if fix in ("B2", "Z"):
        # B2: _mark_patch_hit and _mark_patch_miss exist and are called
        generic = MAPOC / "pms" / "adapters" / "generic.py"
        ok &= _check_symbol("_mark_patch_hit(", generic)
        ok &= _check_symbol("_mark_patch_miss(", generic)
        ok &= _check_symbol("consecutive_replay_failures", generic)

    if fix in ("S1-S2", "Z"):
        # Stale tests use new keys (no llm_targeted key)
        planner_tests = MAPOC / "tests" / "services" / "test_source_planner.py"
        ok &= _check_absent('"llm_targeted"', planner_tests)

    return ok, log


def _run_pytest(fix: str) -> tuple[bool, list[str]]:
    targets = FIX_TESTS.get(fix, [])
    if not targets:
        return True, [f"  no pytest targets for fix {fix}"]
    log: list[str] = []
    cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q"] + [str(MAPOC / t) for t in targets]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    log.append(result.stdout[-2000:] if result.stdout else "")
    if result.returncode != 0:
        log.append(result.stderr[-1000:] if result.stderr else "")
        return False, log
    return True, log


def run_fix(fix: str) -> bool:
    print(f"\n{'='*60}\nFix {fix}\n{'='*60}")
    ok_scans, scan_log = _run_static_scans(fix)
    for line in scan_log:
        print(line)
    if not ok_scans:
        print(f"Fix {fix}: FAIL (static scans)")
        return False
    ok_tests, test_log = _run_pytest(fix)
    for line in test_log:
        print(line)
    if not ok_tests:
        print(f"Fix {fix}: FAIL (pytest)")
        return False
    print(f"Fix {fix}: PASS")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="PR #25 gate runner")
    parser.add_argument("command", choices=["phase", "all"])
    parser.add_argument("phase", nargs="?", default=None)
    args = parser.parse_args()

    all_fixes = ["B1", "B2", "T1-T6", "T7-T8", "S1-S2", "Z"]
    if args.command == "all":
        targets = all_fixes
    else:
        if args.phase not in all_fixes:
            print(f"Unknown fix '{args.phase}'. Valid: {all_fixes}", file=sys.stderr)
            sys.exit(1)
        targets = [args.phase]

    results = {f: run_fix(f) for f in targets}
    print(f"\n{'='*60}\nSummary:")
    for f, ok in results.items():
        print(f"  Fix {f}: {'PASS' if ok else 'FAIL'}")
    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
