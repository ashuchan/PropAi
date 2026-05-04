"""S0 test — verdict file is no longer the placeholder."""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
VERDICT_PATH = REPO_ROOT / "docs" / "ANTIBOT_TLS_VERDICT.md"

_VALID_VERDICTS = {
    "TLS_FINGERPRINT",
    "IP_REPUTATION",
    "MIXED",
    "NOT_REPRODUCIBLE",
    "INCONCLUSIVE",
}


def test_s0_verdict_committed_with_real_outcome() -> None:
    assert VERDICT_PATH.exists(), f"verdict file missing at {VERDICT_PATH}"
    text = VERDICT_PATH.read_text(encoding="utf-8")

    assert "PENDING_LIVE_RUN" not in text, (
        "Verdict file still contains PENDING_LIVE_RUN placeholder. "
        "Run `python -m ma_poc.scripts.diagnostics.tls_vs_ip_diagnostic` "
        "from a production-equivalent egress and commit the result."
    )

    match = re.search(r"^verdict:\s*(\w+)\s*$", text, re.MULTILINE)
    assert match is not None, "no `verdict: <VALUE>` line found in verdict file"
    assert match.group(1) in _VALID_VERDICTS, (
        f"verdict {match.group(1)!r} is not one of {_VALID_VERDICTS}"
    )


def test_s0_verdict_has_per_url_table() -> None:
    """The per-URL results table is the evidence the diagnostic actually ran."""
    text = VERDICT_PATH.read_text(encoding="utf-8")
    data_rows = [
        line for line in text.splitlines()
        if line.startswith("| ")
        and not line.startswith("|---")
        and not line.strip().startswith("| shard |")  # skip the column-header row
    ]
    assert len(data_rows) >= 6, (
        f"verdict file has only {len(data_rows)} data rows; expected ≥ 6 "
        "(diagnostic runs against 6 known-blocked URLs)"
    )
