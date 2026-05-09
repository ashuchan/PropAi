"""F7 — smoke threshold (H9): ≥30 of 50 produce ≥1 unit via direct path.

The smoke script is run manually (it hits live network from production
egress; see ma_poc/scripts/smoke/rentcafe_direct.py docstring). This
test asserts that the committed output meets the H9 threshold; until
the smoke is run, it is auto-skipped with a clear instruction so the
broader gate can pass on a fresh checkout.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

# parents[0]=integration, [1]=tests, [2]=ma_poc, [3]=PropAi
_REPO_ROOT = Path(__file__).resolve().parents[3]
SMOKE = _REPO_ROOT / "ma_poc" / "data" / "smoke" / "rentcafe_direct_smoke.json"


def test_f7_smoke_threshold_30_of_50() -> None:
    """H9 — at least 30/50 RentCafe-family blocked properties recover via direct path."""
    if not SMOKE.exists():
        pytest.skip(
            "Smoke output not present; run "
            "`python -m ma_poc.scripts.smoke.rentcafe_direct "
            "--blocked-input <bot_blocked_properties_latest.json>` first."
        )
    data = json.loads(SMOKE.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) == 50, (
        f"Expected exactly 50 entries; got "
        f"{len(data) if isinstance(data, list) else type(data).__name__}"
    )
    successes = sum(1 for r in data if r.get("units_extracted", 0) > 0)
    failure_modes = Counter(
        r["direct_tier"] for r in data if r.get("units_extracted", 0) == 0
    )
    assert successes >= 30, (
        f"H9 threshold not met: {successes}/50. Failure modes: {dict(failure_modes)}"
    )
