"""
scripts/reporting/issues.py
============================
Validation issues JSONL writer for the daily runner pipeline.

Extracted from scripts/daily_runner.py (lines 461-464).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make sibling script modules importable regardless of invocation cwd.
_HERE = Path(__file__).resolve().parent.parent  # scripts/
_PROJECT_ROOT = _HERE.parent  # ma_poc/
for _p in (_HERE, _PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import validation as V  # noqa: E402


def _write_issues_jsonl(path: Path, issues: list[V.ValidationIssue]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for iss in issues:
            f.write(json.dumps(iss.to_dict(), ensure_ascii=False, default=str) + "\n")
