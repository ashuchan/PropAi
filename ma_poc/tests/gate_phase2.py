"""Gate script for Phase 2. Runs Phase 1 + Phase 2 tests."""
import subprocess
import sys

suites = [
    "tests/unit_matching/test_phase1_fallback.py",
    "tests/unit_matching/test_phase2_state.py",
]

result = subprocess.run(
    ["pytest", *suites, "-v", "--tb=short"],
    capture_output=False,
)
if result.returncode != 0:
    print("\n❌ PHASE 2 GATE FAILED — do not start Phase 3")
    sys.exit(1)
print("\n✅ PHASE 2 GATE PASSED — safe to start Phase 3")
