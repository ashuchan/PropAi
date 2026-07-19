"""Tests for the durable hot-URL maintenance job (2026-07-19).

Pins the migration detector: a cached surface that still serves a roster is
kept; one that 404s or returns an empty shell is invalidated (cleared from the
profile so the pipeline re-discovers) and flagged to the triage queue.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from models.scrape_profile import ApiEndpoint, ScrapeProfile
from services.profile_store import ProfileStore

_MOD = Path(__file__).resolve().parents[2] / "scripts" / "resurface_profiles.py"
_spec = importlib.util.spec_from_file_location("_resurface", _MOD)
rs = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(rs)


class _Resp:
    def __init__(self, status: int, text: str) -> None:
        self.status_code = status
        self.text = text


# Roster body carries a unit signal; shell body does not.
_ROSTER = '{"units":[{"unitNumber":"101","rent":1200}]}'
_SHELL = "<html><body><div id=root></div></body></html>"


def _patch_probe(monkeypatch, mapping: dict[str, _Resp]) -> None:
    def fake(url, timeout=None, unlocker=None):  # noqa: ANN001
        return mapping.get(url, _Resp(404, "Not Found"))

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake)


def test_surface_alive_on_roster(monkeypatch) -> None:
    _patch_probe(monkeypatch, {"u": _Resp(200, _ROSTER)})
    assert rs.surface_is_alive("u") is True


def test_surface_dead_on_404(monkeypatch) -> None:
    _patch_probe(monkeypatch, {"u": _Resp(404, "gone")})
    assert rs.surface_is_alive("u") is False


def test_surface_dead_on_empty_shell(monkeypatch) -> None:
    # 200 but no unit signal = migrated to an empty client shell → DEAD
    _patch_probe(monkeypatch, {"u": _Resp(200, _SHELL)})
    assert rs.surface_is_alive("u") is False


def test_surface_kept_on_transient_and_walled(monkeypatch) -> None:
    """Conservative: probe error (0), 403 walled, and 5xx transient must KEEP the
    seed — invalidating on a blip would wipe good seeds."""
    for status in (0, 403, 500, 503):
        _patch_probe(monkeypatch, {"u": _Resp(status, "just a moment")})
        assert rs.surface_is_alive("u") is True, f"status {status} should keep"


def test_surface_kept_on_probe_exception(monkeypatch) -> None:
    def boom(url, timeout=None, unlocker=None):  # noqa: ANN001
        raise RuntimeError("network down")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", boom)
    assert rs.surface_is_alive("u") is True


def _seed_profile(store: ProfileStore, cid: str, wpu: str | None, endpoint: str | None) -> None:
    p = ScrapeProfile(canonical_id=cid)
    if wpu:
        p.navigation.winning_page_url = wpu
        p.navigation.availability_links = [wpu]
    if endpoint:
        p.api_hints.known_endpoints.append(ApiEndpoint(url_pattern=endpoint, provider="x"))
    store.save(p)


def test_resurface_keeps_alive_invalidates_migrated(tmp_path, monkeypatch) -> None:
    pdir = tmp_path / "profiles"
    store = ProfileStore(pdir)
    _seed_profile(store, "ALIVE1", "https://ok.com/roster", None)
    _seed_profile(store, "DEAD1", "https://moved.com/oldpage", None)
    _seed_profile(store, "APIDEAD", None, "https://api.moved.com/units")

    _patch_probe(
        monkeypatch,
        {
            "https://ok.com/roster": _Resp(200, _ROSTER),
            "https://moved.com/oldpage": _Resp(404, "gone"),
            "https://api.moved.com/units": _Resp(200, _SHELL),
        },
    )
    stale_log = tmp_path / "stale.jsonl"
    stats = rs.resurface(pdir, stale_log)

    assert stats["checked"] == 3
    assert stats["alive"] == 1
    assert stats["invalidated_winning_url"] == 1
    assert stats["invalidated_endpoints"] == 1
    assert stats["stale_flagged"] == 2

    # the migrated winning_page_url was cleared so the pipeline re-discovers
    dead = store.load("DEAD1")
    assert dead is not None and dead.navigation.winning_page_url is None
    assert dead.updated_by == "RESURFACE_MAINT_2026-07-19"
    # the alive one is untouched
    alive = store.load("ALIVE1")
    assert alive is not None and alive.navigation.winning_page_url == "https://ok.com/roster"
    # the dead endpoint was dropped
    apidead = store.load("APIDEAD")
    assert apidead is not None and apidead.api_hints.known_endpoints == []

    # stale triage queue written
    lines = [json.loads(x) for x in stale_log.read_text().splitlines() if x.strip()]
    assert {r["canonical_id"] for r in lines} == {"DEAD1", "APIDEAD"}


def test_resurface_dry_run_writes_nothing(tmp_path, monkeypatch) -> None:
    pdir = tmp_path / "profiles"
    store = ProfileStore(pdir)
    _seed_profile(store, "DEAD1", "https://moved.com/oldpage", None)
    _patch_probe(monkeypatch, {"https://moved.com/oldpage": _Resp(404, "gone")})
    stale_log = tmp_path / "stale.jsonl"

    stats = rs.resurface(pdir, stale_log, dry_run=True)
    assert stats["invalidated_winning_url"] == 1
    # dry-run: profile NOT modified, no stale log written
    dead = store.load("DEAD1")
    assert dead is not None and dead.navigation.winning_page_url == "https://moved.com/oldpage"
    assert not stale_log.exists()


def test_stride_partitions_deterministically(tmp_path, monkeypatch) -> None:
    """With stride=N, each profile is checked in exactly one of the N offsets,
    and the partition is deterministic (sha256, not builtin hash)."""
    pdir = tmp_path / "profiles"
    store = ProfileStore(pdir)
    cids = [f"P{i:03d}" for i in range(30)]
    for c in cids:
        _seed_profile(store, c, f"https://ok.com/{c}", None)
    _patch_probe(monkeypatch, {f"https://ok.com/{c}": _Resp(200, _ROSTER) for c in cids})

    stride = 5
    checked_total = 0
    for offset in range(stride):
        stats = rs.resurface(pdir, tmp_path / f"s{offset}.jsonl", stride=stride, offset=offset)
        checked_total += stats["checked"]
    # every profile checked exactly once across the full stride cycle
    assert checked_total == len(cids)
    # a single offset checks only its share (deterministic + repeatable)
    s0a = rs.resurface(pdir, tmp_path / "a.jsonl", stride=stride, offset=0)
    s0b = rs.resurface(pdir, tmp_path / "b.jsonl", stride=stride, offset=0)
    assert s0a["checked"] == s0b["checked"]
    assert 0 < s0a["checked"] < len(cids)


def test_resurface_skips_profiles_without_surface(tmp_path, monkeypatch) -> None:
    pdir = tmp_path / "profiles"
    store = ProfileStore(pdir)
    _seed_profile(store, "NOSURFACE", None, None)
    _patch_probe(monkeypatch, {})
    stats = rs.resurface(pdir, tmp_path / "stale.jsonl")
    assert stats["no_surface"] == 1
    assert stats["checked"] == 0
