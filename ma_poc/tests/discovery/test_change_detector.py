"""Tests for change_detector — render mode decisions."""
from __future__ import annotations

from datetime import datetime, timezone

from ma_poc.discovery.change_detector import ChangeDecision, decide
from ma_poc.fetch.contracts import RenderMode


def test_change_force_full_always_renders() -> None:
    d = decide("HOT", None, None, 0, force_full=True)
    assert d.render_mode == RenderMode.RENDER
    assert d.reason == "manual_force"


def test_change_stale_render_after_7_days() -> None:
    d = decide("HOT", None, None, 8)
    assert d.render_mode == RenderMode.RENDER
    assert d.reason == "stale_render_7d"


def test_change_hot_profile_fresh_is_head() -> None:
    d = decide("HOT", None, None, 0)
    assert d.render_mode == RenderMode.HEAD
    assert d.reason == "hot_profile_fresh"


def test_change_sitemap_unchanged_is_head() -> None:
    old_lastmod = datetime(2026, 4, 10, tzinfo=timezone.utc)
    frontier_entry = {"last_attempted": "2026-04-15T00:00:00+00:00"}
    d = decide("WARM", frontier_entry, old_lastmod, 2)
    assert d.render_mode == RenderMode.HEAD
    assert d.reason == "sitemap_unchanged"


def test_change_warm_profile_is_get() -> None:
    d = decide("WARM", None, None, 2)
    assert d.render_mode == RenderMode.GET
    assert d.reason == "warm_profile_static"


def test_change_cold_profile_always_renders() -> None:
    d = decide("COLD", None, None, None)
    assert d.render_mode == RenderMode.RENDER


def test_change_decision_is_pure() -> None:
    d1 = decide("HOT", None, None, 0)
    d2 = decide("HOT", None, None, 0)
    assert d1 == d2
