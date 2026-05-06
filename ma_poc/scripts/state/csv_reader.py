"""
scripts/state/csv_reader.py
===========================
UTF-8-BOM tolerant CSV reading for the daily runner pipeline.

Extracted from scripts/daily_runner.py (line 148).
"""
from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path

# Make sibling script modules importable regardless of invocation cwd.
_HERE = Path(__file__).resolve().parent.parent  # scripts/
_PROJECT_ROOT = _HERE.parent  # ma_poc/
for _p in (_HERE, _PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

log = logging.getLogger("daily_runner")


def read_properties_csv(path: Path) -> list[dict]:
    """UTF-8-BOM tolerant CSV read. Returns list of dict rows."""
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    log.info(f"Loaded {len(rows)} rows from {path}")
    return rows
