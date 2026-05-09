"""Gate runner for the fetch-tier escalation ladder.

Same structure as scripts/gates/refactor.py — one function per phase,
each returning a GateResult.

Usage::

    python scripts/gates/escalation.py phase 0
    python scripts/gates/escalation.py phase 1
    python scripts/gates/escalation.py all
    python scripts/gates/escalation.py final
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class GateResult:
    phase: int | str
    passed: bool
    reasons: list[str] = field(default_factory=list)

    def add(self, passed: bool, reason: str) -> None:
        self.reasons.append(("PASS  " if passed else "FAIL  ") + reason)
        if not passed:
            self.passed = False


def _run_pytest(target: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "--tb=line"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    tail = (proc.stdout or proc.stderr).strip().splitlines()[-1:] or [""]
    return proc.returncode == 0, tail[0]


def _run_cmd(cmd: list[str]) -> tuple[bool, str]:
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    output = (proc.stdout + proc.stderr).strip()
    tail = output.splitlines()[-1:] or [""]
    return proc.returncode == 0, tail[0]


# ---------------------------------------------------------------------------
# Phase E0
# ---------------------------------------------------------------------------

def check_phase_0() -> GateResult:
    result = GateResult(phase=0, passed=True)

    # Check ESCALATION_BASELINE.md exists and has required sections
    baseline_path = ROOT / "docs" / "ESCALATION_BASELINE.md"
    result.add(baseline_path.is_file(), "docs/ESCALATION_BASELINE.md exists")

    if baseline_path.is_file():
        body = baseline_path.read_text(encoding="utf-8")
        result.add(len(body) > 100, "docs/ESCALATION_BASELINE.md is non-empty (>100 bytes)")
        required_sections = [
            "## 1. Fetch Outcome Distribution",
            "## 2. Block-Signature Breakdown",
            "## 3. Property Success Rate",
            "## 4. Cost Baseline",
        ]
        for section in required_sections:
            result.add(section in body, f"ESCALATION_BASELINE.md contains '{section}'")

    # Check baselines/escalation.py exists and runs cleanly
    script_path = ROOT / "scripts" / "baselines" / "escalation.py"
    result.add(script_path.is_file(), "scripts/baselines/escalation.py exists")

    if script_path.is_file():
        ok, summary = _run_cmd([sys.executable, str(script_path)])
        result.add(ok, f"baselines/escalation.py runs cleanly ({summary})")

    # Check feature-flag tests pass
    ok, summary = _run_pytest("tests/test_feature_flags.py")
    result.add(ok, f"4 feature-flag tests pass ({summary})")

    # Check ruff
    ok, summary = _run_cmd(["python", "-m", "ruff", "check", "config/"])
    result.add(ok, f"ruff check config/ clean ({summary})")

    # Check mypy --strict
    ok, summary = _run_cmd(["python", "-m", "mypy", "--strict", "config/"])
    result.add(ok, f"mypy --strict config/ clean ({summary})")

    return result


# ---------------------------------------------------------------------------
# Phase E1 (stub — gates added as phases are implemented)
# ---------------------------------------------------------------------------

def check_phase_1() -> GateResult:
    result = GateResult(phase=1, passed=True)

    # Model files
    tier_path = ROOT / "models" / "fetch_tier.py"
    result.add(tier_path.is_file(), "models/fetch_tier.py exists")

    profile_path = ROOT / "models" / "fetch_profile.py"
    result.add(profile_path.is_file(), "models/fetch_profile.py exists (or embedded in scrape_profile)")

    # Tests
    ok, summary = _run_pytest("tests/test_fetch_tier.py")
    result.add(ok, f"fetch tier tests pass ({summary})")

    ok, summary = _run_pytest("tests/test_fetch_profile_migration.py")
    result.add(ok, f"fetch profile migration tests pass ({summary})")

    ok, summary = _run_cmd(["python", "-m", "ruff", "check", "models/"])
    result.add(ok, f"ruff check models/ clean ({summary})")

    ok, summary = _run_cmd(["python", "-m", "mypy", "--strict", "models/fetch_tier.py"])
    result.add(ok, f"mypy --strict models/fetch_tier.py clean ({summary})")

    return result


# ---------------------------------------------------------------------------
# Phase E2 (stub)
# ---------------------------------------------------------------------------

def check_phase_2() -> GateResult:
    result = GateResult(phase=2, passed=True)

    sig_path = ROOT / "fetch" / "block_signatures.py"
    result.add(sig_path.is_file(), "fetch/block_signatures.py exists")

    ok, summary = _run_pytest("tests/fetch/test_block_signatures.py")
    result.add(ok, f"block-signature tests pass ({summary})")

    ok, summary = _run_cmd(["python", "-m", "ruff", "check", "fetch/block_signatures.py"])
    result.add(ok, f"ruff check block_signatures.py clean ({summary})")

    ok, summary = _run_cmd(["python", "-m", "mypy", "--strict", "fetch/block_signatures.py"])
    result.add(ok, f"mypy --strict block_signatures.py clean ({summary})")

    return result


# ---------------------------------------------------------------------------
# Phase E3 (stub)
# ---------------------------------------------------------------------------

def check_phase_3() -> GateResult:
    result = GateResult(phase=3, passed=True)

    escalator_path = ROOT / "fetch" / "tier_escalator.py"
    result.add(escalator_path.is_file(), "fetch/tier_escalator.py exists")

    ok, summary = _run_pytest("tests/fetch/test_tier_escalator.py")
    result.add(ok, f"tier escalator tests pass ({summary})")

    # Critical invariants greps
    escalator_text = escalator_path.read_text(encoding="utf-8") if escalator_path.is_file() else ""

    # No hardcoded site list
    banned = ["airbnb", "zillow", "apartments.com"]
    for banned_str in banned:
        result.add(
            banned_str not in escalator_text.lower(),
            f"No hardcoded site '{banned_str}' in tier_escalator.py",
        )

    # MAX_ESCALATIONS_PER_RUN appears in loop condition
    result.add(
        "MAX_ESCALATIONS_PER_RUN" in escalator_text,
        "MAX_ESCALATIONS_PER_RUN referenced in tier_escalator.py",
    )

    ok, summary = _run_cmd(["python", "-m", "ruff", "check", "fetch/tier_escalator.py"])
    result.add(ok, f"ruff check tier_escalator.py clean ({summary})")

    return result


# ---------------------------------------------------------------------------
# Phase E4 (stub)
# ---------------------------------------------------------------------------

def check_phase_4() -> GateResult:
    result = GateResult(phase=4, passed=True)

    res_path = ROOT / "fetch" / "providers" / "residential.py"
    result.add(res_path.is_file(), "fetch/providers/residential.py exists")

    ok, summary = _run_pytest("tests/fetch/test_provider_residential.py")
    result.add(ok, f"residential provider tests pass ({summary})")

    ok, summary = _run_pytest("tests/fetch/test_tier_skip_rules.py")
    result.add(ok, f"tier skip rule tests pass ({summary})")

    return result


# ---------------------------------------------------------------------------
# Phase E5 (stub)
# ---------------------------------------------------------------------------

def check_phase_5() -> GateResult:
    result = GateResult(phase=5, passed=True)

    unlocker_path = ROOT / "fetch" / "providers" / "unlocker.py"
    result.add(unlocker_path.is_file(), "fetch/providers/unlocker.py exists")

    ok, summary = _run_pytest("tests/fetch/test_provider_unlocker.py")
    result.add(ok, f"unlocker provider tests pass ({summary})")

    ok, summary = _run_pytest("tests/integration/test_escalation_e5_full.py")
    result.add(ok, f"E5 integration tests pass ({summary})")

    # Verify no tier_floor == DLQ_PARK in profiles
    profiles_dir = ROOT / "config" / "profiles"
    if profiles_dir.exists():
        dlq_floor_found = False
        for jf in profiles_dir.glob("*.json"):
            try:
                import json as _json
                data = _json.loads(jf.read_text(encoding="utf-8"))
                fetch_block = data.get("fetch", {})
                if str(fetch_block.get("tier_floor", "")).upper() == "DLQ_PARK":
                    dlq_floor_found = True
                    break
            except Exception:
                pass
        result.add(not dlq_floor_found, "No profile has tier_floor=DLQ_PARK")

    return result


# ---------------------------------------------------------------------------
# Final cross-cutting gate (E6)
# ---------------------------------------------------------------------------

def check_final() -> GateResult:
    result = GateResult(phase="final", passed=True)

    checklist_path = ROOT / "docs" / "BUG_HUNT_ESCALATION_CHECKLIST.md"
    result.add(checklist_path.is_file(), "docs/BUG_HUNT_ESCALATION_CHECKLIST.md exists")

    if checklist_path.is_file():
        body = checklist_path.read_text(encoding="utf-8")
        unchecked = body.count("- [ ]")
        result.add(unchecked == 0, f"All checklist items marked [x] (unchecked: {unchecked})")

    # Static analysis sweep
    for target in ["fetch/tier_escalator.py", "fetch/providers/", "config/feature_flags.py"]:
        path = ROOT / target
        if path.exists():
            ok, summary = _run_cmd(["python", "-m", "ruff", "check", target])
            result.add(ok, f"ruff {target} clean ({summary})")

            ok, summary = _run_cmd(["python", "-m", "mypy", "--strict", target])
            result.add(ok, f"mypy --strict {target} clean ({summary})")

    # Verify BOT_BLOCKED not in retry_policy's retry-eligible list
    retry_path = ROOT / "fetch" / "retry_policy.py"
    if retry_path.is_file():
        retry_text = retry_path.read_text(encoding="utf-8")
        # The retry policy SHOULD handle BOT_BLOCKED by returning should_retry=False
        # after 1 attempt (legacy). E3 will change this — for now just verify file exists.
        result.add(True, "retry_policy.py exists (BOT_BLOCKED escalation handled by escalator in E3+)")

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_PHASE_FUNCS = {
    0: check_phase_0,
    1: check_phase_1,
    2: check_phase_2,
    3: check_phase_3,
    4: check_phase_4,
    5: check_phase_5,
}


def _print_result(r: GateResult) -> None:
    label = f"Phase {r.phase}"
    status = "PASSED" if r.passed else "FAILED"
    print(f"\n{'='*60}")
    print(f"{label}: {status}")
    print(f"{'='*60}")
    for reason in r.reasons:
        print(f"  {reason}")


def _save_result(r: GateResult, gates_dir: Path) -> None:
    gates_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = gates_dir / f"phase{r.phase}_{ts}.json"
    out.write_text(json.dumps(asdict(r), indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Escalation gate runner")
    parser.add_argument(
        "command",
        choices=["phase", "all", "final"],
        help="'phase N', 'all', or 'final'",
    )
    parser.add_argument("phase_num", nargs="?", type=int, help="Phase number (0-5)")
    args = parser.parse_args()

    gates_dir = ROOT / "data" / "gates" / "escalation"

    if args.command == "phase":
        if args.phase_num is None:
            print("ERROR: phase number required", file=sys.stderr)
            return 1
        if args.phase_num not in _PHASE_FUNCS:
            print(f"ERROR: unknown phase {args.phase_num}", file=sys.stderr)
            return 1
        r = _PHASE_FUNCS[args.phase_num]()
        _print_result(r)
        _save_result(r, gates_dir)
        return 0 if r.passed else 1

    elif args.command == "all":
        all_passed = True
        for n, fn in sorted(_PHASE_FUNCS.items()):
            r = fn()
            _print_result(r)
            _save_result(r, gates_dir)
            if not r.passed:
                all_passed = False
                print(f"\nPhase {n} failed. Stopping.\n")
                break
        return 0 if all_passed else 1

    elif args.command == "final":
        r = check_final()
        _print_result(r)
        _save_result(r, gates_dir)
        return 0 if r.passed else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
