"""Consume layer: run_report.build() summarises events.jsonl correctly.

Verifies that bot-block and CAPTCHA tallies are computed from events.jsonl
and that tier distribution is derived correctly from properties list.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reporting.run_report import build


RUN_DATE = "2026-05-10"


def _write_events(run_dir: Path, events: list[dict[str, Any]]) -> None:
    events_path = run_dir / "events.jsonl"
    events_path.write_text(
        "\n".join(json.dumps(e) for e in events),
        encoding="utf-8",
    )


def _make_run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "runs" / RUN_DATE
    run_dir.mkdir(parents=True)
    return run_dir


# ---------------------------------------------------------------------------
# Bot-block tally
# ---------------------------------------------------------------------------


def test_bot_block_count_from_events_jsonl(tmp_path: Path) -> None:
    """Bot-blocked properties are counted from events.jsonl."""
    run_dir = _make_run_dir(tmp_path)
    _write_events(run_dir, [
        {"kind": "fetch.bot_blocked", "property_id": "P1", "url": "https://p1.com"},
        {"kind": "fetch.bot_blocked", "property_id": "P2", "url": "https://p2.com"},
        {"kind": "fetch.success", "property_id": "P3", "url": "https://p3.com"},
    ])
    props = [
        {"_meta": {"verdict": "FAILED_BOT_BLOCKED", "property_id": "P1"}},
        {"_meta": {"verdict": "FAILED_BOT_BLOCKED", "property_id": "P2"}},
        {"_meta": {"verdict": "SUCCESS", "property_id": "P3"}},
    ]

    report = build(props, run_dir, RUN_DATE)

    assert report["fetch_bot_blocked"]["count"] == 2
    assert "P1" in report["fetch_bot_blocked"]["property_ids"]


def test_captcha_count_from_events_jsonl(tmp_path: Path) -> None:
    """CAPTCHA-detected properties are tallied from events.jsonl."""
    run_dir = _make_run_dir(tmp_path)
    _write_events(run_dir, [
        {"kind": "fetch.captcha_detected", "property_id": "PC1", "url": "https://pc1.com"},
    ])
    props = [{"_meta": {"verdict": "FAILED_CAPTCHA", "property_id": "PC1"}}]

    report = build(props, run_dir, RUN_DATE)

    assert report["fetch_captcha_detected"]["count"] == 1


def test_no_events_file_produces_zero_bot_count(tmp_path: Path) -> None:
    """With no events.jsonl, bot/captcha counts are 0."""
    run_dir = _make_run_dir(tmp_path)
    # Deliberately do NOT write events.jsonl
    props = [{"_meta": {"verdict": "SUCCESS"}}]

    report = build(props, run_dir, RUN_DATE)

    assert report["fetch_bot_blocked"]["count"] == 0
    assert report["fetch_captcha_detected"]["count"] == 0


# ---------------------------------------------------------------------------
# Tier distribution
# ---------------------------------------------------------------------------


def test_tier_distribution_from_properties(tmp_path: Path) -> None:
    """Tier distribution is computed from the properties list, not from events."""
    run_dir = _make_run_dir(tmp_path)
    props = [
        {"_meta": {"verdict": "SUCCESS", "scrape_tier_used": "TIER_1_API"}},
        {"_meta": {"verdict": "SUCCESS", "scrape_tier_used": "TIER_1_API"}},
        {"_meta": {"verdict": "SUCCESS", "scrape_tier_used": "TIER_3_DOM"}},
    ]

    report = build(props, run_dir, RUN_DATE)

    dist = report["tier_distribution"]
    assert dist.get("TIER_1_API") == 2
    assert dist.get("TIER_3_DOM") == 1


def test_success_rate_computed_correctly(tmp_path: Path) -> None:
    """Success rate reflects the fraction of non-failed properties."""
    run_dir = _make_run_dir(tmp_path)
    props = [
        {"_meta": {"verdict": "SUCCESS", "scrape_tier_used": "TIER_2_JSONLD"}},
        {"_meta": {"verdict": "SUCCESS", "scrape_tier_used": "TIER_2_JSONLD"}},
        {"_meta": {"verdict": "FAILED_UNREACHABLE", "scrape_tier_used": "FAILED"}},
    ]

    report = build(props, run_dir, RUN_DATE)

    assert report["totals"]["properties"] == 3
    # 1 failed → success_rate ≈ 66.67%
    assert report["totals"]["success_rate_pct"] < 100
    assert report["totals"]["failed"] == 1


def test_run_date_in_report(tmp_path: Path) -> None:
    """run_date field in report matches the provided run_date."""
    run_dir = _make_run_dir(tmp_path)

    report = build([], run_dir, RUN_DATE)

    assert report["run_date"] == RUN_DATE
