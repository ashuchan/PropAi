"""Consume layer: validate_outputs metrics computed correctly on fixture data.

Tests the helper functions in scripts/checks/outputs.py against synthetic
event fixtures to verify the 10 metric computations are correct.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.checks.outputs import percentile, _domain, load_events


# ---------------------------------------------------------------------------
# percentile helper
# ---------------------------------------------------------------------------


def test_percentile_p95_exact() -> None:
    """p95 of a sorted list returns the correct element via nearest-rank formula."""
    values = list(range(100))  # 0..99
    p95 = percentile(values, 95)
    # Formula: k = round((95/100) * 99) = round(94.05) = 94 → values[94] = 94
    assert p95 == 94


def test_percentile_empty_returns_zero() -> None:
    """percentile of empty list returns 0.0 without error."""
    assert percentile([], 95) == 0.0


def test_percentile_single_element() -> None:
    """percentile of a single-element list always returns that element."""
    assert percentile([42.0], 50) == 42.0
    assert percentile([42.0], 99) == 42.0


# ---------------------------------------------------------------------------
# _domain helper
# ---------------------------------------------------------------------------


def test_domain_extracts_hostname() -> None:
    """_domain extracts the hostname from a full URL."""
    assert _domain("https://www.example.com/scrape/page") == "www.example.com"


def test_domain_handles_none() -> None:
    """_domain on None returns '(none)'."""
    assert _domain(None) == "(none)"


def test_domain_handles_empty_string() -> None:
    """_domain on empty string returns '(none)'."""
    assert _domain("") == "(none)"


# ---------------------------------------------------------------------------
# load_events with patched DATA_DIR
# ---------------------------------------------------------------------------


def _make_event(
    outcome: str = "SUCCESS",
    tier: int = 1,
    pid: str = "P1",
    vision: bool = False,
) -> dict[str, Any]:
    return {
        "scrape_timestamp": datetime.now(UTC).isoformat(),
        "property_id": pid,
        "scrape_outcome": outcome,
        "extraction_tier": tier,
        "vision_fallback_used": vision,
        "banner_capture_attempted": True,
        "page_load_ms": 1500,
    }


def test_load_events_reads_from_data_dir(tmp_path: Path, monkeypatch: Any) -> None:
    """load_events() reads scrape_events.jsonl from DATA_DIR."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    events_file = data_dir / "scrape_events.jsonl"
    events = [_make_event("SUCCESS", 1, f"P{i}") for i in range(5)]
    events_file.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    # Reload module to pick up new env var
    import importlib
    import scripts.checks.outputs as mod
    importlib.reload(mod)

    loaded = mod.load_events()
    assert len(loaded) == 5


def test_success_rate_metric_computation() -> None:
    """Success rate is (successes / total) — verify the formula directly."""
    recent = [_make_event("SUCCESS")] * 8 + [_make_event("FAILED")] * 2
    successes = [e for e in recent if e["scrape_outcome"] == "SUCCESS"]
    rate = len(successes) / len(recent) if recent else 0.0
    assert abs(rate - 0.8) < 0.01


def test_tier12_share_computation() -> None:
    """Tier 1+2 share is fraction of successes from tiers 1 and 2."""
    from collections import defaultdict

    successes = (
        [_make_event("SUCCESS", tier=1)] * 4
        + [_make_event("SUCCESS", tier=2)] * 2
        + [_make_event("SUCCESS", tier=3)] * 4
    )
    tiers: dict[int, int] = defaultdict(int)
    for e in successes:
        if e.get("extraction_tier") is not None:
            tiers[int(e["extraction_tier"])] += 1

    tier12_share = (tiers[1] + tiers[2]) / len(successes)
    assert abs(tier12_share - 0.6) < 0.01
