"""Gate script for Phase 1. Run before starting Phase 2."""
import subprocess
import sys

result = subprocess.run(
    ["pytest", "tests/unit_matching/test_phase1_fallback.py", "-v", "--tb=short"],
    capture_output=False,
)
if result.returncode != 0:
    print("\n❌ PHASE 1 GATE FAILED — do not start Phase 2")
    sys.exit(1)
print("\n✅ PHASE 1 GATE PASSED — safe to start Phase 2")
