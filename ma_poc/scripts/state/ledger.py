"""
scripts/state/ledger.py
=======================
Run ledger helpers for crash-safe resume support.

Extracted from scripts/daily_runner.py (lines 430-458).
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Make sibling script modules importable regardless of invocation cwd.
_HERE = Path(__file__).resolve().parent.parent  # scripts/
_PROJECT_ROOT = _HERE.parent  # ma_poc/
for _p in (_HERE, _PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

log = logging.getLogger("state.ledger")


def _append_ledger(path: Path, entry: dict) -> None:
    """Append one checkpoint entry to the run ledger (crash-safe resume support)."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def load_ledger(path: Path) -> dict[str, dict]:
    """
    Read a ledger.jsonl and return the *last* entry per canonical_id.

    Later entries overwrite earlier ones so retries update the record.
    Returns {canonical_id: {status, row_index, timestamp, ...}}.
    """
    entries: dict[str, dict] = {}
    if not path.exists():
        return entries
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                cid = rec.get("canonical_id")
                if cid:
                    entries[cid] = rec
            except json.JSONDecodeError:
                continue
    return entries
