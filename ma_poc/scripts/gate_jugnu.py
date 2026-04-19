"""
Jugnu J9 — Unified gate script for all phases.

Usage:
    python scripts/gate_jugnu.py phase 0   # Check J0 gate
    python scripts/gate_jugnu.py phase 1   # Check J1 gate
    python scripts/gate_jugnu.py all       # Check all phases
"""
from __future__ import annotations

import argparse
import importlib
import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("gate_jugnu")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def check_phase_0() -> list[str]:
    """J0 gate: baseline metrics exist and are populated."""
    failures: list[str] = []
    baseline_md = _PROJECT_ROOT / "docs" / "JUGNU_BASELINE.md"
    if not baseline_md.exists():
        failures.append("docs/JUGNU_BASELINE.md does not exist")
    else:
        content = baseline_md.read_text()
        if "## 1. Totals" not in content:
            failures.append("JUGNU_BASELINE.md missing '## 1. Totals' section")
        if "{auto-filled}" in content:
            failures.append("JUGNU_BASELINE.md still has placeholder values")

    baseline_json = list((_PROJECT_ROOT / "data" / "baseline").glob("*.json"))
    if not baseline_json:
        failures.append("No data/baseline/<date>.json found")

    return failures


def check_phase_1() -> list[str]:
    """J1 gate: fetch layer modules exist and import."""
    failures: list[str] = []
    fetch_dir = _PROJECT_ROOT / "fetch"
    required = [
        "contracts.py", "fetcher.py", "retry_policy.py", "proxy_pool.py",
        "rate_limiter.py", "stealth.py", "conditional.py",
        "response_classifier.py", "captcha_detect.py", "browser_pool.py",
    ]
    for f in required:
        if not (fetch_dir / f).exists():
            failures.append(f"fetch/{f} missing")

    try:
        sys.path.insert(0, str(_PROJECT_ROOT.parent))
        importlib.import_module("ma_poc.fetch.contracts")
        importlib.import_module("ma_poc.fetch.fetcher")
    except ImportError as e:
        failures.append(f"Import error: {e}")
    return failures


def check_phase_2() -> list[str]:
    """J2 gate: discovery layer modules exist."""
    failures: list[str] = []
    disc_dir = _PROJECT_ROOT / "discovery"
    required = [
        "contracts.py", "frontier.py", "sitemap.py",
        "change_detector.py", "dlq.py", "carry_forward.py", "scheduler.py",
    ]
    for f in required:
        if not (disc_dir / f).exists():
            failures.append(f"discovery/{f} missing")
    return failures


def check_phase_3() -> list[str]:
    """J3 gate: PMS layer with Jugnu deltas."""
    failures: list[str] = []

    # Delta 6: no LLM imports in non-generic adapters
    adapters_dir = _PROJECT_ROOT / "pms" / "adapters"
    if adapters_dir.exists():
        for adapter_file in adapters_dir.glob("*.py"):
            if adapter_file.name in ("__init__.py", "base.py", "registry.py",
                                      "generic.py", "_parsing.py", "_stub.py"):
                continue
            text = adapter_file.read_text(encoding="utf-8")
            if "openai" in text.lower() and "import" in text.lower():
                failures.append(f"{adapter_file.name} imports OpenAI (Delta 6 violation)")
            if "llm_extractor" in text:
                failures.append(f"{adapter_file.name} uses llm_extractor (Delta 6 violation)")

    # Check scrape_jugnu exists
    scraper = _PROJECT_ROOT / "pms" / "scraper.py"
    if scraper.exists():
        text = scraper.read_text()
        if "scrape_jugnu" not in text:
            failures.append("pms/scraper.py missing scrape_jugnu function")

    return failures


def check_phase_4() -> list[str]:
    """J4 gate: validation layer modules exist."""
    failures: list[str] = []
    val_dir = _PROJECT_ROOT / "validation"
    required = [
        "contracts.py", "schema_gate.py", "identity_fallback.py",
        "cross_run_sanity.py", "orchestrator.py",
    ]
    for f in required:
        if not (val_dir / f).exists():
            failures.append(f"validation/{f} missing")
    return failures


def check_phase_5() -> list[str]:
    """J5 gate: observability layer modules exist."""
    failures: list[str] = []
    obs_dir = _PROJECT_ROOT / "observability"
    required = [
        "events.py", "event_ledger.py", "cost_ledger.py",
        "replay_store.py", "slo_watcher.py", "dlq_controller.py",
    ]
    for f in required:
        if not (obs_dir / f).exists():
            failures.append(f"observability/{f} missing")

    # Check configure() exists in events.py
    events_text = (obs_dir / "events.py").read_text()
    if "def configure" not in events_text:
        failures.append("events.py missing configure() function")
    return failures


def check_phase_6() -> list[str]:
    """J6 gate: profile v2 schema."""
    failures: list[str] = []
    profile_py = _PROJECT_ROOT / "models" / "scrape_profile.py"
    if profile_py.exists():
        text = profile_py.read_text()
        if "schema_version" not in text:
            failures.append("scrape_profile.py missing schema_version field")
        if "ProfileStats" not in text:
            failures.append("scrape_profile.py missing ProfileStats class")
        if "consecutive_unreachable" not in text:
            failures.append("scrape_profile.py missing consecutive_unreachable field")
    else:
        failures.append("models/scrape_profile.py not found")
    return failures


def check_phase_7() -> list[str]:
    """J7 gate: report v2 modules exist."""
    failures: list[str] = []
    rpt_dir = _PROJECT_ROOT / "reporting"
    if not (rpt_dir / "verdict.py").exists():
        failures.append("reporting/verdict.py missing")
    if not (rpt_dir / "run_report.py").exists():
        failures.append("reporting/run_report.py missing")
    return failures


def check_phase_8() -> list[str]:
    """J8 gate: integration runner exists."""
    failures: list[str] = []
    runner = _PROJECT_ROOT / "scripts" / "jugnu_runner.py"
    if not runner.exists():
        failures.append("scripts/jugnu_runner.py missing")
    return failures


def check_phase_9() -> list[str]:
    """J9 gate: this script exists + bug hunt checklist."""
    failures: list[str] = []
    if not (_PROJECT_ROOT / "docs" / "BUG_HUNT_CHECKLIST.md").exists():
        failures.append("docs/BUG_HUNT_CHECKLIST.md missing")
    return failures


_PHASE_CHECKS = {
    0: check_phase_0,
    1: check_phase_1,
    2: check_phase_2,
    3: check_phase_3,
    4: check_phase_4,
    5: check_phase_5,
    6: check_phase_6,
    7: check_phase_7,
    8: check_phase_8,
    9: check_phase_9,
}


def run_tests(phase: int | None = None) -> tuple[int, int]:
    """Run pytest for a specific phase or all.

    Args:
        phase: Phase number, or None for all.

    Returns:
        (passed, failed) counts.
    """
    test_dirs = {
        0: "tests/baseline",
        1: "tests/fetch",
        2: "tests/discovery",
        3: "tests/pms",
        4: "tests/validation",
        5: "tests/observability",
        6: "tests/profile",
        7: "tests/reporting",
    }

    if phase is not None:
        test_dir = test_dirs.get(phase)
        if test_dir:
            cmd = [sys.executable, "-m", "pytest", test_dir, "-v", "--tb=short"]
        else:
            return 0, 0
    else:
        cmd = [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short",
               "--ignore=data", "--ignore=config"]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(_PROJECT_ROOT))
    # Parse pytest output
    output = result.stdout + result.stderr
    passed = output.count(" passed")
    failed_count = output.count(" failed")
    return passed, failed_count


def main() -> int:
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Jugnu gate validator")
    parser.add_argument("command", choices=["phase", "all", "tests"])
    parser.add_argument("phase_num", type=int, nargs="?", default=None)
    args = parser.parse_args()

    if args.command == "phase" and args.phase_num is not None:
        check_fn = _PHASE_CHECKS.get(args.phase_num)
        if check_fn is None:
            log.error("Unknown phase: %d", args.phase_num)
            return 1
        failures = check_fn()
        if failures:
            log.error("Phase %d FAILED:", args.phase_num)
            for f in failures:
                log.error("  - %s", f)
            return 1
        log.info("Phase %d PASSED", args.phase_num)
        return 0

    elif args.command == "all":
        total_failures: list[str] = []
        for phase_num in sorted(_PHASE_CHECKS.keys()):
            check_fn = _PHASE_CHECKS[phase_num]
            failures = check_fn()
            if failures:
                log.error("Phase %d FAILED:", phase_num)
                for f in failures:
                    log.error("  - %s", f)
                total_failures.extend(failures)
            else:
                log.info("Phase %d PASSED", phase_num)

        if total_failures:
            log.error("\n%d total failures across all phases", len(total_failures))
            return 1
        log.info("\nAll phases PASSED")
        return 0

    elif args.command == "tests":
        passed, failed = run_tests(args.phase_num)
        log.info("Tests: %d passed, %d failed", passed, failed)
        return 1 if failed > 0 else 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
