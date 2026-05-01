"""Gate script for Phase 3. Runs Phases 1 + 2 + 3 tests."""
import subprocess
import sys

suites = [
    "tests/unit_matching/test_phase1_fallback.py",
    "tests/unit_matching/test_phase2_state.py",
    "tests/unit_matching/test_phase3_runner.py",
]

result = subprocess.run(
    ["pytest", *suites, "-v", "--tb=short"],
    capture_output=False,
)
if result.returncode != 0:
    print("\n❌ PHASE 3 GATE FAILED — do not start Phase 4")
    sys.exit(1)
print("\n✅ PHASE 3 GATE PASSED — safe to start Phase 4")
