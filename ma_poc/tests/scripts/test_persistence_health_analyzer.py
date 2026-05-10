"""Persistence-health analyzer + dashboard tests.

Three layers:

1. ``db_query._is_read_only`` — every diagnostic SQL must pass; INSERT/
   UPDATE/DELETE/DROP must fail loud at the runner before reaching the DB.
2. ``analyze_cloud_run.parse_shard`` — the new persistence-health events
   (mapping.save_dropped, profile.update_failed, profile.replay_hit, etc.)
   accumulate correctly per shard.
3. ``analyze_cloud_run.render_persistence_health_md`` — the markdown table
   renders, the SLO thresholds fire correctly, and ALERT block appears
   only when warranted.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

# Repo-relative path so tests work whether invoked from repo root or ma_poc/.
_DIAG_DIR = Path(__file__).resolve().parents[2] / "scripts" / "diagnostics"
if str(_DIAG_DIR) not in sys.path:
    sys.path.insert(0, str(_DIAG_DIR))


# ── db_query.py read-only guard ────────────────────────────────────────────


@pytest.fixture
def dq():
    """Import the diagnostics db_query module."""
    import db_query  # type: ignore[import-not-found]
    return db_query


class TestReadOnlyGuard:
    def test_select_passes(self, dq):
        ok, reason = dq._is_read_only("SELECT 1")
        assert ok, reason

    def test_with_cte_passes(self, dq):
        ok, _ = dq._is_read_only("WITH foo AS (SELECT 1) SELECT * FROM foo")
        assert ok

    def test_explain_passes(self, dq):
        ok, _ = dq._is_read_only("EXPLAIN SELECT * FROM scrape_profiles")
        assert ok

    def test_insert_blocked(self, dq):
        ok, reason = dq._is_read_only("INSERT INTO scrape_profiles VALUES (1)")
        assert not ok
        assert "INSERT" in reason.upper()

    def test_update_blocked(self, dq):
        ok, reason = dq._is_read_only("UPDATE scrape_profiles SET version=0")
        assert not ok

    def test_delete_blocked(self, dq):
        ok, reason = dq._is_read_only("DELETE FROM scrape_profiles WHERE 1=1")
        assert not ok

    def test_drop_blocked(self, dq):
        ok, reason = dq._is_read_only("DROP TABLE scrape_profiles")
        assert not ok

    def test_truncate_blocked(self, dq):
        ok, _ = dq._is_read_only("TRUNCATE TABLE scrape_profiles")
        assert not ok

    def test_cte_with_insert_blocked(self, dq):
        """The most-likely bypass attempt: hide an INSERT inside a WITH."""
        sql = "WITH foo AS (SELECT 1) INSERT INTO scrape_profiles VALUES (1)"
        ok, reason = dq._is_read_only(sql)
        assert not ok
        assert "INSERT" in reason.upper()

    def test_string_literal_with_drop_passes(self, dq):
        """Forbidden keywords inside string literals must NOT block the
        statement — that's a false positive that would block legitimate
        diagnostic queries (e.g. searching error_messages for the word
        'DROP')."""
        sql = "SELECT 1 WHERE error_message = 'DROP TABLE was attempted'"
        ok, reason = dq._is_read_only(sql)
        assert ok, reason

    def test_empty_blocked(self, dq):
        ok, reason = dq._is_read_only("")
        assert not ok
        assert "empty" in reason.lower()

    def test_comment_only_blocked(self, dq):
        ok, _ = dq._is_read_only("-- just a comment\n")
        assert not ok


# ── db_query.py SQL file parser ────────────────────────────────────────────


class TestParseSqlFile:
    def test_named_queries_split(self, dq, tmp_path: Path):
        sql_file = tmp_path / "x.sql"
        sql_file.write_text(
            "-- :: q1\nSELECT 1;\n\n-- :: q2\nSELECT 2;\n",
            encoding="utf-8",
        )
        queries = dq.parse_sql_file(sql_file)
        assert [q.name for q in queries] == ["q1", "q2"]

    def test_default_block_kept(self, dq, tmp_path: Path):
        sql_file = tmp_path / "x.sql"
        sql_file.write_text("SELECT 1;\n-- :: named\nSELECT 2;\n", encoding="utf-8")
        queries = dq.parse_sql_file(sql_file)
        assert [q.name for q in queries] == ["_default", "named"]

    def test_real_persistence_health_sql_parses(self, dq):
        """The actual production SQL file must parse cleanly. PR 3 ships
        4 named queries; this test fails if a future edit accidentally
        breaks the marker syntax or strips a query."""
        path = _DIAG_DIR / "profile_persistence_health.sql"
        queries = dq.parse_sql_file(path)
        names = [q.name for q in queries]
        assert "Q4_channel_row_counts" in names
        assert "Q5_recent_activity" in names
        assert "Q6_maturity_distribution" in names
        # Every query must be read-only.
        for q in queries:
            ok, reason = dq._is_read_only(q.sql)
            assert ok, f"{q.name}: {reason}"


# ── analyze_cloud_run persistence-health aggregation ───────────────────────


@pytest.fixture
def analyzer():
    import analyze_cloud_run  # type: ignore[import-not-found]
    return analyze_cloud_run


def _write_shard(tmp_path: Path, shard_name: str, events: list[dict[str, Any]]) -> Path:
    shard = tmp_path / shard_name
    shard.mkdir()
    (shard / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )
    (shard / "issues.jsonl").write_text("", encoding="utf-8")
    # Minimal report.json so aggregate_run doesn't trip
    (shard / "report.json").write_text(
        json.dumps({"totals": {"properties": 1, "succeeded": 0, "failed": 1}}),
        encoding="utf-8",
    )
    return shard


class TestPersistenceHealthAccumulation:
    def test_mapping_save_dropped_counted_by_reason(self, analyzer, tmp_path):
        shard = _write_shard(tmp_path, "shard_0", [
            {"kind": "mapping.save_dropped", "property_id": "p1", "reason": "empty_pattern"},
            {"kind": "mapping.save_dropped", "property_id": "p2", "reason": "empty_pattern"},
            {"kind": "mapping.save_dropped", "property_id": "p3", "reason": "disabled_by_flag"},
        ])
        outcomes, report, ph = analyzer.parse_shard(shard)
        assert ph["mapping_save_dropped"]["empty_pattern"] == 2
        assert ph["mapping_save_dropped"]["disabled_by_flag"] == 1

    def test_profile_update_failed_counted(self, analyzer, tmp_path):
        shard = _write_shard(tmp_path, "shard_0", [
            {"kind": "profile.update_failed", "property_id": "p1", "error": "x"},
            {"kind": "profile.update_failed", "property_id": "p2", "error": "y"},
        ])
        _, _, ph = analyzer.parse_shard(shard)
        assert ph["profile_update_failed"] == 2

    def test_startup_probe_events_have_no_property_id(self, analyzer, tmp_path):
        """STARTUP_PROBE events emit with property_id="" — the parser must
        still count them. Pre-PR-3 the property_id gate would have
        dropped them silently."""
        shard = _write_shard(tmp_path, "shard_0", [
            {"kind": "startup.probe_ok", "property_id": ""},
            {"kind": "startup.probe_failed", "property_id": "", "error": "x"},
        ])
        _, _, ph = analyzer.parse_shard(shard)
        assert ph["startup_probe_ok"] == 1
        assert ph["startup_probe_failed"] == 1

    def test_replay_and_field_patch_events(self, analyzer, tmp_path):
        shard = _write_shard(tmp_path, "shard_0", [
            {"kind": "profile.replay_hit", "property_id": "p1"},
            {"kind": "profile.replay_hit", "property_id": "p2"},
            {"kind": "profile.replay_miss_with_saved", "property_id": "p3"},
            {"kind": "field_patch.hit", "property_id": "p1", "field": "rent_low"},
            {"kind": "field_patch.drift", "property_id": "p2", "field": "unit_id"},
        ])
        _, _, ph = analyzer.parse_shard(shard)
        assert ph["profile_replay_hits"] == 2
        assert ph["profile_replay_miss_with_saved"] == 1
        assert ph["field_patch_hits"] == 1
        assert ph["field_patch_drift"] == 1


# ── render_persistence_health_md SLO logic ─────────────────────────────────


class TestRenderPersistenceHealth:
    def _make_stats(self, analyzer, **overrides) -> Any:
        s = analyzer.RunStats(run_date="2026-05-10", shards_seen=["shard_0"], shards_expected=1)
        for k, v in overrides.items():
            setattr(s, k, v)
        return s

    def test_zero_signal_renders_no_alert(self, analyzer):
        s = self._make_stats(analyzer)
        out = "\n".join(analyzer.render_persistence_health_md(s))
        assert "Persistence health" in out
        assert "PERSISTENCE-LOOP ALERT" not in out

    def test_low_replay_rate_alerts(self, analyzer):
        # 1 hit + 9 misses = 10% < 30% → ALERT
        s = self._make_stats(
            analyzer,
            profile_replay_hits=1,
            profile_replay_miss_with_saved=9,
        )
        out = "\n".join(analyzer.render_persistence_health_md(s))
        assert "PERSISTENCE-LOOP ALERT" in out
        assert "profile_replay_hit_rate 10.0%" in out

    def test_high_replay_rate_no_alert(self, analyzer):
        # 50 hits + 10 misses = 83% > 30% → OK
        s = self._make_stats(
            analyzer,
            profile_replay_hits=50,
            profile_replay_miss_with_saved=10,
        )
        out = "\n".join(analyzer.render_persistence_health_md(s))
        assert "PERSISTENCE-LOOP ALERT" not in out

    def test_high_save_drop_rate_alerts(self, analyzer):
        # 80 drops vs 10 hits → drop_rate = 80/(80+10) ≈ 89% > 50% → ALERT
        s = self._make_stats(
            analyzer,
            mapping_save_dropped=Counter({"empty_pattern": 80}),
            profile_replay_hits=10,
            profile_replay_miss_with_saved=0,
        )
        out = "\n".join(analyzer.render_persistence_health_md(s))
        assert "PERSISTENCE-LOOP ALERT" in out
        assert "mapping_save_drop_rate" in out

    def test_profile_update_failed_alerts_above_threshold(self, analyzer):
        s = self._make_stats(analyzer, profile_update_failed=200)
        out = "\n".join(analyzer.render_persistence_health_md(s))
        assert "PERSISTENCE-LOOP ALERT" in out
        assert "profile_update_failed 200" in out

    def test_startup_probe_failed_always_alerts(self, analyzer):
        """Threshold == 0 means any non-zero count is an ALERT — the probe
        is a deploy-time guard and any failure means PG poisoning prevented."""
        s = self._make_stats(analyzer, startup_probe_failed=1)
        out = "\n".join(analyzer.render_persistence_health_md(s))
        assert "PERSISTENCE-LOOP ALERT" in out
        assert "RUNNER FAILED DEPLOY GUARD" in out

    def test_drop_reasons_table_renders_when_drops_exist(self, analyzer):
        s = self._make_stats(
            analyzer,
            mapping_save_dropped=Counter({"empty_pattern": 7, "disabled_by_flag": 3}),
        )
        out = "\n".join(analyzer.render_persistence_health_md(s))
        assert "MAPPING_SAVE_DROPPED reasons" in out
        assert "empty_pattern" in out
        assert "disabled_by_flag" in out

    def test_summary_json_contains_persistence_health(self, analyzer, tmp_path):
        """Self-review pass-1 fix: machine-readable downstream consumers
        (alerting pipelines, monitoring) need persistence-health in JSON,
        not just markdown. Pin the JSON shape so a future refactor can't
        drop it silently."""
        s = self._make_stats(
            analyzer,
            mapping_save_dropped=Counter({"empty_pattern": 5}),
            profile_update_failed=2,
            profile_replay_hits=10,
            profile_replay_miss_with_saved=3,
            startup_probe_ok=20,
            field_patch_hits=7,
        )
        analyzer.write_outputs(s, {}, tmp_path)
        payload = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
        ph = payload["persistence_health"]
        assert ph["mapping_save_dropped"] == {"empty_pattern": 5}
        assert ph["profile_update_failed"] == 2
        assert ph["profile_replay_hits"] == 10
        assert ph["profile_replay_miss_with_saved"] == 3
        assert ph["startup_probe_ok"] == 20
        assert ph["field_patch_hits"] == 7
        # DB section is None when not attached (no --check-db).
        assert ph["db"] is None

    def test_db_section_renders_when_attached(self, analyzer):
        s = self._make_stats(
            analyzer,
            db_persistence_health={
                "Q4_channel_row_counts": [
                    {"total_profiles": 5054, "profiles_with_mappings": 3,
                     "profiles_with_patches": 0},
                ],
            },
        )
        out = "\n".join(analyzer.render_persistence_health_md(s))
        assert "DB row counts" in out
        assert "Q4_channel_row_counts" in out
        assert "5054" in out
