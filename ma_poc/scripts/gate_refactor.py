"""
Refactor gate runner.

Implements ``scripts/gate_refactor.py phase <N>`` described in
``claude_refactor.md``. Phases 0–2 are wired up in this revision; later phases
add their own checks as they are implemented.

Usage::

    python scripts/gate_refactor.py phase 0
    python scripts/gate_refactor.py phase 1
    python scripts/gate_refactor.py phase 2
    python scripts/gate_refactor.py all
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import typing as t
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class GateResult:
    phase: int
    passed: bool
    reasons: list[str] = field(default_factory=list)

    def add(self, passed: bool, reason: str) -> None:
        self.reasons.append(("PASS  " if passed else "FAIL  ") + reason)
        if not passed:
            self.passed = False


def _run_pytest(target: str) -> tuple[bool, str]:
    """Run pytest against ``target``, returning (ok, last-line-summary)."""
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
    tail = (proc.stdout or proc.stderr).strip().splitlines()[-1:] or [""]
    return proc.returncode == 0, tail[0]


def check_phase_0() -> GateResult:
    result = GateResult(phase=0, passed=True)

    doc_path = ROOT / "docs" / "REFACTOR_BASELINE.md"
    result.add(doc_path.is_file(), f"{doc_path.relative_to(ROOT)} exists")
    if doc_path.is_file():
        body = doc_path.read_text(encoding="utf-8")
        # Non-empty "Current-pipeline metrics" section: either inline content or
        # at least one appended "Baseline captured" block.
        has_appended = "## Baseline captured" in body
        result.add(
            has_appended,
            "'Current-pipeline metrics' section has at least one baseline capture",
        )

    script_path = ROOT / "scripts" / "refactor_baseline.py"
    result.add(script_path.is_file(), "scripts/refactor_baseline.py exists")

    ok, summary = _run_pytest("tests/refactor/test_baseline.py")
    result.add(ok, f"tests/refactor/test_baseline.py — {summary}")

    # Script runs cleanly against real data.
    ok, summary = _run_cmd([sys.executable, str(script_path)])
    result.add(ok, f"refactor_baseline.py runs against real data — {summary}")
    return result


def check_phase_1() -> GateResult:
    result = GateResult(phase=1, passed=True)

    detector = ROOT / "pms" / "detector.py"
    result.add(detector.is_file(), "pms/detector.py exists")

    ok, summary = _run_pytest("tests/pms/test_detector.py")
    result.add(ok, f"tests/pms/test_detector.py — {summary}")

    ok, summary = _run_cmd([sys.executable, "-m", "ruff", "check", str(detector)])
    result.add(ok, f"ruff check pms/detector.py — {summary}")

    ok, summary = _run_cmd([sys.executable, "-m", "mypy", "--strict", str(detector)])
    result.add(ok, f"mypy --strict pms/detector.py — {summary}")

    # Every PMS literal has at least one test asserting it is returned.
    result.add(*_every_literal_has_test())
    return result


def _every_literal_has_test() -> tuple[bool, str]:
    """Crude static check: every DetectedPMS literal appears in a test assertion."""
    from ma_poc.pms.detector import DetectedPMS

    literals = t.get_args(t.get_type_hints(DetectedPMS)["pms"])
    test_body = (ROOT / "tests" / "pms" / "test_detector.py").read_text(encoding="utf-8")
    missing = [lit for lit in literals if f'== "{lit}"' not in test_body and f"=='{lit}'" not in test_body]
    if missing:
        return False, f"missing assertion for literals: {missing}"
    return True, f"all {len(literals)} PMS literals have an assertion in tests"


def check_phase_2() -> GateResult:
    result = GateResult(phase=2, passed=True)

    base = ROOT / "pms" / "adapters" / "base.py"
    registry = ROOT / "pms" / "adapters" / "registry.py"
    init_file = ROOT / "pms" / "adapters" / "__init__.py"
    result.add(base.is_file(), "pms/adapters/base.py exists")
    result.add(registry.is_file(), "pms/adapters/registry.py exists")
    result.add(init_file.is_file(), "pms/adapters/__init__.py exists")

    ok, summary = _run_pytest("tests/pms/test_registry.py")
    result.add(ok, f"tests/pms/test_registry.py — {summary}")

    ok, summary = _run_cmd([sys.executable, "-m", "mypy", "--strict", str(ROOT / "pms" / "adapters")])
    result.add(ok, f"mypy --strict pms/adapters/ — {summary}")

    # Every literal (except unknown, custom) has an adapter module file.
    from ma_poc.pms.detector import DetectedPMS

    literals = t.get_args(t.get_type_hints(DetectedPMS)["pms"])
    adapters_dir = ROOT / "pms" / "adapters"
    missing: list[str] = []
    for lit in literals:
        if lit in ("unknown", "custom"):
            continue
        if not (adapters_dir / f"{lit}.py").is_file():
            missing.append(lit)
    result.add(not missing, f"adapter modules present for every literal (missing: {missing})")

    # Generic adapter exists (fallback for unknown/custom).
    result.add((adapters_dir / "generic.py").is_file(), "pms/adapters/generic.py exists")
    return result


def check_phase_3() -> GateResult:
    result = GateResult(phase=3, passed=True)

    # All adapter tests pass.
    ok, summary = _run_pytest("tests/pms/adapters/")
    result.add(ok, f"tests/pms/adapters/ — {summary}")

    # mypy strict on all adapter code.
    ok, summary = _run_cmd([sys.executable, "-m", "mypy", "--strict", str(ROOT / "pms" / "adapters")])
    result.add(ok, f"mypy --strict pms/adapters/ — {summary}")

    # ruff clean.
    ok, summary = _run_cmd([sys.executable, "-m", "ruff", "check", str(ROOT / "pms" / "adapters")])
    result.add(ok, f"ruff check pms/adapters/ — {summary}")

    # No adapter imports another adapter (except base).
    adapters_dir = ROOT / "pms" / "adapters"
    cross_imports: list[str] = []
    for py in adapters_dir.glob("*.py"):
        if py.name in ("__init__.py", "base.py", "registry.py", "_stub.py", "_parsing.py"):
            continue
        content = py.read_text(encoding="utf-8")
        for line in content.splitlines():
            if "from ma_poc.pms.adapters" in line and "from ma_poc.pms.adapters.base" not in line \
                    and "from ma_poc.pms.adapters._parsing" not in line \
                    and "from ma_poc.pms.adapters._stub" not in line:
                # realpage_oll importing onesite is acceptable (shared parser)
                if py.name == "realpage_oll.py" and "onesite" in line:
                    continue
                cross_imports.append(f"{py.name}: {line.strip()}")
    result.add(not cross_imports, f"no cross-adapter imports (found: {cross_imports})")

    # No PMS-specific host string in generic.py.
    generic_path = adapters_dir / "generic.py"
    if generic_path.is_file():
        generic_content = generic_path.read_text(encoding="utf-8").lower()
        banned = ["sightmap", "rentcafe", "appfolio", "entrata", "avaloncommunities", "onlineleasing"]
        found = [b for b in banned if b in generic_content]
        result.add(not found, f"no PMS strings in generic.py (found: {found})")

    # Research log comment at top of each adapter.
    from ma_poc.pms.detector import DetectedPMS
    literals = t.get_args(t.get_type_hints(DetectedPMS)["pms"])
    missing_log: list[str] = []
    for lit in literals:
        if lit in ("unknown", "custom"):
            continue
        adapter_path = adapters_dir / f"{lit}.py"
        if adapter_path.is_file():
            content = adapter_path.read_text(encoding="utf-8")
            if "Research log" not in content:
                missing_log.append(lit)
    result.add(not missing_log, f"all adapters have research log (missing: {missing_log})")

    # At least 2 fixture files per PMS with real data.
    fixtures_dir = ROOT / "tests" / "pms" / "adapters" / "fixtures"
    low_fixtures: list[str] = []
    for lit in literals:
        if lit in ("unknown", "custom"):
            continue
        pms_fixtures = fixtures_dir / lit
        if pms_fixtures.is_dir():
            count = len(list(pms_fixtures.glob("*.json")))
            if count < 2:
                low_fixtures.append(f"{lit}={count}")
        else:
            low_fixtures.append(f"{lit}=0")
    result.add(not low_fixtures, f">=2 fixture files per PMS (low: {low_fixtures})")

    return result


def check_phase_4() -> GateResult:
    result = GateResult(phase=4, passed=True)

    resolver = ROOT / "pms" / "resolver.py"
    result.add(resolver.is_file(), "pms/resolver.py exists")

    ok, summary = _run_pytest("tests/pms/test_resolver.py")
    result.add(ok, f"tests/pms/test_resolver.py — {summary}")

    ok, summary = _run_cmd([sys.executable, "-m", "mypy", "--strict", str(resolver)])
    result.add(ok, f"mypy --strict resolver.py — {summary}")

    ok, summary = _run_cmd([sys.executable, "-m", "ruff", "check", str(resolver)])
    result.add(ok, f"ruff check resolver.py — {summary}")

    return result


def check_phase_5() -> GateResult:
    result = GateResult(phase=5, passed=True)

    scraper = ROOT / "pms" / "scraper.py"
    result.add(scraper.is_file(), "pms/scraper.py exists")

    ok, summary = _run_pytest("tests/pms/test_scraper.py")
    result.add(ok, f"tests/pms/test_scraper.py — {summary}")

    ok, summary = _run_cmd([sys.executable, "-m", "ruff", "check", str(scraper)])
    result.add(ok, f"ruff check scraper.py — {summary}")

    return result


def check_phase_6() -> GateResult:
    result = GateResult(phase=6, passed=True)

    ok, summary = _run_pytest("tests/profile/test_migration.py")
    result.add(ok, f"tests/profile/test_migration.py — {summary}")

    migrate = ROOT / "scripts" / "migrate_profiles_v1_to_v2.py"
    result.add(migrate.is_file(), "scripts/migrate_profiles_v1_to_v2.py exists")

    return result


def check_phase_7() -> GateResult:
    result = GateResult(phase=7, passed=True)

    report_mod = ROOT / "reporting" / "property_report.py"
    result.add(report_mod.is_file(), "reporting/property_report.py exists")

    ok, summary = _run_pytest("tests/reporting/test_property_report.py")
    result.add(ok, f"tests/reporting/test_property_report.py — {summary}")

    return result


def check_phase_8() -> GateResult:
    result = GateResult(phase=8, passed=True)

    ok, summary = _run_pytest("tests/integration/test_daily_runner_refactor.py")
    result.add(ok, f"tests/integration/test_daily_runner_refactor.py — {summary}")

    return result


PHASE_CHECKS: dict[int, t.Callable[[], GateResult]] = {
    0: check_phase_0,
    1: check_phase_1,
    2: check_phase_2,
    3: check_phase_3,
    4: check_phase_4,
    5: check_phase_5,
    6: check_phase_6,
    7: check_phase_7,
    8: check_phase_8,
}


def run_phase(phase: int) -> GateResult:
    fn = PHASE_CHECKS.get(phase)
    if fn is None:
        return GateResult(phase=phase, passed=False, reasons=[f"FAIL  phase {phase} not implemented yet"])
    return fn()


def run_all() -> list[GateResult]:
    results: list[GateResult] = []
    for phase in sorted(PHASE_CHECKS):
        r = run_phase(phase)
        results.append(r)
        if not r.passed:
            break
    return results


def _print_result(result: GateResult) -> None:
    header = f"Phase {result.phase}: {'PASS' if result.passed else 'FAIL'}"
    print(header)
    print("-" * len(header))
    for line in result.reasons:
        print(f"  {line}")
    print()


def _write_json(results: list[GateResult]) -> Path:
    out_dir = ROOT / "data" / "gates"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"{ts}.json"
    out_path.write_text(
        json.dumps([asdict(r) for r in results], indent=2),
        encoding="utf-8",
    )
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_phase = sub.add_parser("phase")
    p_phase.add_argument("n", type=int)
    sub.add_parser("all")
    sub.add_parser("final")
    args = parser.parse_args(argv)

    if args.cmd == "phase":
        results = [run_phase(args.n)]
    elif args.cmd == "final":
        # Phase 9 cross-cutting check is a future task; for now, treat final as "run all phases".
        results = run_all()
    else:
        results = run_all()

    for r in results:
        _print_result(r)
    json_path = _write_json(results)
    print(f"Wrote {json_path.relative_to(ROOT)}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
