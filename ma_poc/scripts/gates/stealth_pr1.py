#!/usr/bin/env python3
"""Stealth Hardening PR-1 gate runner — S0 + S1 + S2 + S3 + S4 + S5."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MAPOC = REPO_ROOT / "ma_poc"

PHASE_TESTS: dict[str, list[str]] = {
    "S0": ["tests/fetch/test_verdict_committed.py"],
    "S1": ["tests/fetch/test_stealth_shim.py"],
    "S2": ["tests/fetch/test_http_client_factory.py"],
    "S3": ["tests/fetch/test_browser_headers.py"],
    "S4": ["tests/fetch/test_cookie_jar.py"],
}

PHASE_FILES: dict[str, list[Path]] = {
    "S0": [REPO_ROOT / "docs" / "ANTIBOT_TLS_VERDICT.md"],
    "S1": [REPO_ROOT / "docs" / "STEALTH_SHIM_AUDIT.md"],
    "S2": [MAPOC / "fetch" / "http_client.py"],
    "S3": [MAPOC / "fetch" / "headers.py"],
    "S4": [MAPOC / "fetch" / "cookie_jar.py"],
    "S5": [REPO_ROOT / "docs" / "PR1_REVIEW_CHECKLIST.md"],
}


def _check_files_exist(phase: str) -> tuple[bool, list[str]]:
    log: list[str] = []
    ok = True
    for f in PHASE_FILES.get(phase, []):
        if not f.exists():
            log.append(f"  {phase}: MISSING {f.relative_to(REPO_ROOT)}")
            ok = False
        else:
            log.append(f"  {phase}: OK {f.relative_to(REPO_ROOT)}")
    return ok, log


def _check_s5_checklist_complete() -> tuple[bool, list[str]]:
    """S5 — every mandatory checkbox must be ticked or N/A."""
    path = REPO_ROOT / "docs" / "PR1_REVIEW_CHECKLIST.md"
    if not path.exists():
        return False, [f"  S5 missing checklist {path}"]
    text = path.read_text(encoding="utf-8")
    unticked = [
        line for line in text.splitlines()
        if line.lstrip().startswith("- [ ]")
    ]
    if unticked:
        return False, [f"  S5: {len(unticked)} unticked items remain:"] + [
            f"    {u}" for u in unticked[:10]
        ]
    return True, ["  S5: all checklist items ticked or N/A — PASS"]


def _run_pytest(phase: str) -> tuple[bool, list[str]]:
    targets = PHASE_TESTS.get(phase, [])
    if not targets:
        return True, [f"  {phase}: no pytest targets"]
    cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q"] + [
        str(MAPOC / t) for t in targets
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    log = [result.stdout[-2000:] if result.stdout else ""]
    if result.returncode != 0:
        log.append(result.stderr[-1000:] if result.stderr else "")
        return False, log
    return True, log


def run_phase(phase: str) -> bool:
    print(f"\n{'='*60}\nPhase {phase}\n{'='*60}")
    ok_files, file_log = _check_files_exist(phase)
    for line in file_log:
        print(line)
    if not ok_files:
        print(f"Phase {phase}: FAIL (file presence)")
        return False

    if phase == "S5":
        ok_chk, chk_log = _check_s5_checklist_complete()
        for line in chk_log:
            print(line)
        if not ok_chk:
            print(f"Phase {phase}: FAIL (checklist)")
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
    parser = argparse.ArgumentParser(description="Stealth Hardening PR-1 gate")
    parser.add_argument("command", choices=["phase", "all"])
    parser.add_argument("phase", nargs="?", choices=["S0", "S1", "S2", "S3", "S4", "S5"])
    args = parser.parse_args()

    phases = [args.phase] if args.command == "phase" else ["S0", "S1", "S2", "S3", "S4", "S5"]
    failed = [p for p in phases if not run_phase(p)]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
