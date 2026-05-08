#!/usr/bin/env python3
"""PR #24 gate runner — 13-fix bug-fix audit.

Verifies all 13 fixes introduced after PR #23:
  Fix 1:  Budget keys renamed (llm_targeted → llm_api_calls)
  Fix 2:  _merge_into_result_units in generic.py
  Fix 3:  multi_source flag in save_llm_field_mapping
  Fix 4:  _stash_provenanced_units in generic.py
  Fix 5:  _client_account_id_from_url in detector.py
  Fix 6:  extract_units_from_dom returns (units, hit_mode) tuple
  Fix 7:  _apply_field_patches sub-tier 0b
  Fix 8:  shared_budget in scrape() and _try_link_hop()
  Fix 9:  has_field_group() public export in source_planner
  Fix 10: short-needle matching in _resolve_source_url
  Fix 11: beds/bedrooms in _ZERO_IS_VALID_FIELDS
  Fix 12: no bare imports in library files
  Fix 13: planner gates after HTML tiers in generic.py

Usage:
    python -m ma_poc.scripts.gate_pr24 all
    python -m ma_poc.scripts.gate_pr24 phase <1-13|Z>
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MAPOC = REPO_ROOT / "ma_poc"

FIX_TESTS: dict[str, list[str]] = {
    "1":  ["tests/integration/test_pr24_budget.py"],
    "2":  ["tests/integration/test_pr24_h13_units_preservation.py"],
    "3":  ["tests/profile/test_pr24_per_endpoint_validation.py"],
    "4":  ["tests/integration/test_pr24_h13_units_preservation.py"],
    "5":  ["tests/services/test_pr24_client_account_id.py"],
    "6":  ["tests/profile/test_pr24_dom_hints_hit_accuracy.py"],
    "7":  ["tests/profile/test_pr24_field_patch_replay.py"],
    "8":  ["tests/integration/test_pr24_h13_units_preservation.py"],
    "9":  ["tests/services/test_pr24_field_group_consistency.py"],
    "10": [],  # static scan only
    "11": ["tests/services/test_pr24_field_group_consistency.py"],
    "12": ["tests/profile/test_pr24_observations_common_path.py"],
    "13": ["tests/integration/test_pr24_planner_gates_html_tiers.py"],
    "Z": [
        "tests/integration/test_pr24_budget.py",
        "tests/integration/test_pr24_h13_units_preservation.py",
        "tests/profile/test_pr24_per_endpoint_validation.py",
        "tests/profile/test_pr24_dom_hints_hit_accuracy.py",
        "tests/profile/test_pr24_field_patch_replay.py",
        "tests/services/test_pr24_client_account_id.py",
        "tests/services/test_pr24_field_group_consistency.py",
        "tests/profile/test_pr24_observations_common_path.py",
        "tests/integration/test_pr24_planner_gates_html_tiers.py",
    ],
}


def _run_static_scans(fix: str) -> tuple[bool, list[str]]:
    """Returns (passed, log_lines)."""
    log: list[str] = []

    def _check_symbol(symbol: str, filepath: Path) -> bool:
        if not filepath.exists():
            log.append(f"  MISSING FILE: {filepath}")
            return False
        found = symbol in filepath.read_text(encoding="utf-8")
        status = "PASS" if found else "FAIL"
        log.append(f"  {symbol!r} in {filepath.name}: {status}")
        return found

    def _check_absent(symbol: str, filepath: Path) -> bool:
        if not filepath.exists():
            log.append(f"  MISSING FILE: {filepath}")
            return False
        found = symbol in filepath.read_text(encoding="utf-8")
        status = "FAIL" if found else "PASS"
        log.append(f"  absent {symbol!r} in {filepath.name}: {status}")
        return not found

    ok = True

    if fix in ("1", "Z"):
        # Fix 1: budget key rename
        planner = MAPOC / "services" / "source_planner.py"
        scraper = MAPOC / "pms" / "scraper.py"
        base = MAPOC / "pms" / "adapters" / "base.py"
        ok &= _check_symbol("llm_api_calls", planner)
        ok &= _check_absent("llm_targeted", planner)
        ok &= _check_symbol("llm_api_calls", scraper)
        ok &= _check_symbol("llm_api_calls", base)

    if fix in ("2", "Z"):
        # Fix 2: _merge_into_result_units
        generic = MAPOC / "pms" / "adapters" / "generic.py"
        ok &= _check_symbol("_merge_into_result_units(", generic)

    if fix in ("3", "Z"):
        # Fix 3: multi_source parameter
        updater = MAPOC / "services" / "profile_updater.py"
        ok &= _check_symbol("multi_source", updater)

    if fix in ("4", "Z"):
        # Fix 4: _stash_provenanced_units
        generic = MAPOC / "pms" / "adapters" / "generic.py"
        ok &= _check_symbol("_stash_provenanced_units(", generic)

    if fix in ("5", "Z"):
        # Fix 5: _client_account_id_from_url
        detector = MAPOC / "pms" / "detector.py"
        ok &= _check_symbol("_client_account_id_from_url(", detector)
        ok &= _check_symbol("pms_client_account_id", detector)

    if fix in ("6", "Z"):
        # Fix 6: tuple return from extract_units_from_dom
        html_extract = MAPOC / "pms" / "adapters" / "_html_extract.py"
        ok &= _check_symbol("hit_mode", html_extract)
        ok &= _check_symbol('tuple[list[dict', html_extract)

    if fix in ("7", "Z"):
        # Fix 7: _apply_field_patches
        generic = MAPOC / "pms" / "adapters" / "generic.py"
        ok &= _check_symbol("_apply_field_patches(", generic)

    if fix in ("8", "Z"):
        # Fix 8: shared_budget in scrape() and _try_link_hop()
        scraper = MAPOC / "pms" / "scraper.py"
        src = scraper.read_text(encoding="utf-8") if scraper.exists() else ""
        count = src.count("shared_budget")
        status = "PASS" if count >= 3 else "FAIL"
        log.append(f"  shared_budget occurrences in scraper.py (need ≥3): {count}: {status}")
        ok &= count >= 3

    if fix in ("9", "Z"):
        # Fix 9: has_field_group public export
        planner = MAPOC / "services" / "source_planner.py"
        ok &= _check_symbol("def has_field_group(", planner)

    if fix in ("10", "Z"):
        # Fix 10: short-needle matching in _resolve_source_url
        runner = MAPOC / "scripts" / "jugnu_runner.py"
        ok &= _check_symbol("short_needle", runner)
        ok &= _check_symbol("_resolve_source_url(", runner)

    if fix in ("11", "Z"):
        # Fix 11: beds/bedrooms in _ZERO_IS_VALID_FIELDS
        source = MAPOC / "models" / "source.py"
        src = source.read_text(encoding="utf-8") if source.exists() else ""
        has_beds = '"beds"' in src and "_ZERO_IS_VALID_FIELDS" in src
        has_bedrooms = '"bedrooms"' in src and "_ZERO_IS_VALID_FIELDS" in src
        status = "PASS" if (has_beds and has_bedrooms) else "FAIL"
        log.append(f"  beds/bedrooms in _ZERO_IS_VALID_FIELDS: {status}")
        ok &= has_beds and has_bedrooms

    if fix in ("12", "Z"):
        # Fix 12: no bare imports in library files
        bare_re = re.compile(r"^\s*from\s+(models|services)\.", re.MULTILINE)
        files = [
            MAPOC / "services" / "source_planner.py",
            MAPOC / "services" / "profile_updater.py",
            MAPOC / "services" / "source_merger.py",
            MAPOC / "services" / "cluster_store.py",
            MAPOC / "services" / "source_observer.py",
        ]
        for f in files:
            if not f.exists():
                log.append(f"  MISSING: {f.name}")
                ok = False
                continue
            bare = bare_re.findall(f.read_text(encoding="utf-8"))
            status = "FAIL" if bare else "PASS"
            log.append(f"  no bare imports in {f.name}: {status}")
            if bare:
                ok = False

    if fix in ("13", "Z"):
        # Fix 13: planner gates after HTML tiers
        generic = MAPOC / "pms" / "adapters" / "generic.py"
        src = generic.read_text(encoding="utf-8") if generic.exists() else ""
        count = src.count("_assess_and_decide(")
        status = "PASS" if count >= 4 else "FAIL"
        log.append(f"  _assess_and_decide calls in generic.py (need ≥4): {count}: {status}")
        ok &= count >= 4

    return ok, log


def _run_pytest(fix: str) -> tuple[bool, list[str]]:
    targets = FIX_TESTS.get(fix, [])
    if not targets:
        return True, [f"  no pytest targets for fix {fix}"]
    log: list[str] = []
    pytest_exe = shutil.which("pytest") or sys.executable
    if pytest_exe == sys.executable:
        cmd = [pytest_exe, "-m", "pytest", "--tb=short", "-q"]
    else:
        cmd = [pytest_exe, "--tb=short", "-q"]
    cmd += [str(MAPOC / t) for t in targets]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    log.append(result.stdout[-2000:] if result.stdout else "")
    if result.returncode != 0:
        log.append(result.stderr[-1000:] if result.stderr else "")
        return False, log
    return True, log


def run_fix(fix: str) -> bool:
    print(f"\n{'='*60}")
    print(f"Fix {fix}")
    print(f"{'='*60}")

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
    parser = argparse.ArgumentParser(description="PR #24 gate runner")
    parser.add_argument("command", choices=["phase", "all"])
    parser.add_argument("phase", nargs="?", default=None)
    args = parser.parse_args()

    all_fixes = [str(i) for i in range(1, 14)] + ["Z"]
    if args.command == "all":
        targets = all_fixes
    else:
        if args.phase not in all_fixes:
            print(f"Unknown fix '{args.phase}'. Valid: {all_fixes}", file=sys.stderr)
            sys.exit(1)
        targets = [args.phase]

    results = {f: run_fix(f) for f in targets}
    print(f"\n{'='*60}")
    print("Summary:")
    for f, ok in results.items():
        print(f"  Fix {f}: {'PASS' if ok else 'FAIL'}")
    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
