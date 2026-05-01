"""Final gate — all 5 phases must pass before shipping."""
import subprocess
import sys

suites = [
    "tests/unit_matching/test_phase1_fallback.py",
    "tests/unit_matching/test_phase2_state.py",
    "tests/unit_matching/test_phase3_runner.py",
    "tests/unit_matching/test_phase4_scrape.py",
    "tests/unit_matching/test_phase5_e2e.py",
]

result = subprocess.run(
    ["pytest", *suites, "-v", "--tb=short"],
    capture_output=False,
)
if result.returncode != 0:
    print("\n❌ FINAL GATE FAILED — do not ship")
    sys.exit(1)
print("\n✅ FINAL GATE PASSED — unit matching fixes ready to ship")
