"""End-to-end gate for CLAUDE_PROMPTS_MERGE_RESILIENCE.

Runs the per-phase pytest selectors in order. Exits non-zero on any
phase failure and prints the failing invariant labels so the operator
can jump straight to the broken contract.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class Phase:
    label: str
    invariants: tuple[str, ...]
    selectors: tuple[str, ...]


PHASES: tuple[Phase, ...] = (
    Phase(
        label="Phase 0 — CSV mapping verified",
        invariants=("H6",),
        selectors=("ma_poc/tests/integration/test_csv_property_id_mapping_verified.py",),
    ),
    Phase(
        label="Phase 1 — FloorplanCatalog",
        invariants=("H2", "H6", "H8", "H11"),
        selectors=("ma_poc/tests/services/test_floorplan_catalog.py",),
    ),
    Phase(
        label="Phase 2 — Prompt templates",
        invariants=("H1", "H11"),
        selectors=("ma_poc/tests/services/test_prompt_templates.py",),
    ),
    Phase(
        label="Phase 3 — Post-extraction CSV snap",
        invariants=("H2", "H4"),
        selectors=("ma_poc/tests/integration/test_floorplan_snap.py",),
    ),
    Phase(
        label="Phase 4 — Anchor-first merge cascade",
        invariants=("H2", "H8", "H9", "H10"),
        selectors=(
            "ma_poc/tests/integration/test_merge_anchor_first.py",
            "ma_poc/tests/integration/test_pr25_merge_into_result_units.py",
        ),
    ),
    Phase(
        label="Phase 5 — Validator (H5)",
        invariants=("H5",),
        selectors=(
            "ma_poc/tests/validation/test_schema_gate_unit_id_alone.py",
            "ma_poc/tests/validation/test_schema_gate.py",
        ),
    ),
    Phase(
        label="Phase 6/7 — Observation reports",
        invariants=("H7",),
        selectors=(
            "ma_poc/tests/reporting/test_amenities_report.py",
            "ma_poc/tests/reporting/test_concessions_report.py",
        ),
    ),
    Phase(
        label="Phase 8 — Deploy-time validator",
        invariants=("H6",),
        selectors=("ma_poc/tests/scripts/test_validate_deployment.py",),
    ),
)


def _run_phase(phase: Phase, *, verbose: bool) -> int:
    print(f"\n=== {phase.label} (invariants: {', '.join(phase.invariants)}) ===")
    cmd = [sys.executable, "-m", "pytest", "--tb=short"]
    if verbose:
        cmd.append("-v")
    cmd.extend(phase.selectors)
    return subprocess.call(cmd, cwd=REPO_ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--phase",
        type=int,
        default=None,
        help="Run a single phase by index (0..N-1). Default runs all.",
    )
    args = parser.parse_args(argv)

    failures: list[Phase] = []
    if args.phase is not None:
        if not 0 <= args.phase < len(PHASES):
            print(f"phase index {args.phase} out of range")
            return 2
        if _run_phase(PHASES[args.phase], verbose=args.verbose) != 0:
            failures.append(PHASES[args.phase])
    else:
        for phase in PHASES:
            if _run_phase(phase, verbose=args.verbose) != 0:
                failures.append(phase)

    if not failures:
        print("\nALL PHASES PASSED")
        return 0
    print("\nFAILED PHASES:")
    for phase in failures:
        print(f"  - {phase.label} (invariants: {', '.join(phase.invariants)})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
