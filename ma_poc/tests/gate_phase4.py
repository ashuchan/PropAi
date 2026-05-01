"""Gate script for Phase 4. Runs Phases 1 + 2 + 3 + 4 tests."""
import subprocess
import sys

suites = [
    "tests/unit_matching/test_phase1_fallback.py",
    "tests/unit_matching/test_phase2_state.py",
    "tests/unit_matching/test_phase3_runner.py",
    "tests/unit_matching/test_phase4_scrape.py",
]

result = subprocess.run(
    ["pytest", *suites, "-v", "--tb=short"],
    capture_output=False,
)
if result.returncode != 0:
    print("\n❌ PHASE 4 GATE FAILED — do not start Phase 5")
    sys.exit(1)
print("\n✅ PHASE 4 GATE PASSED — safe to start Phase 5")
