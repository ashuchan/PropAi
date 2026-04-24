"""End-to-end test for ``scripts.sync_run_to_pg.sync_run_to_postgres``.

Seeds an FS provider with the full surface the scrape runner writes
(state, profiles, events, runs, artifacts) and syncs it into a SQLite
provider. SQLite exercises the same dialect-aware upsert code path as
Postgres via ``data_provider.sql.engine.dialect_insert``.

Regression scope: every row this test counts must land in Postgres in
prod. If you add a new artifact to the scrape pipeline, extend this
test first — silent-drop in prod is exactly how we shipped "smoke
passes, DB is empty" the first time.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from data_provider import (
    FileSystemDataProvider,
    IssueEntry,
    LedgerEntry,
    RunReport,
    RunSummary,
    SqliteDataProvider,
)
from models.extraction_result import (
    ExtractionResult,
    ExtractionStatus,
    ExtractionTier,
)
from models.scrape_event import ScrapeEvent, ScrapeOutcome
from models.scrape_profile import ScrapeProfile

from scripts import sync_run_to_pg


RUN_DATE = "2026-04-23"


def _seed_fs_provider(tmp_path: Path) -> tuple[FileSystemDataProvider, Path, Path]:
    """Build an FS provider with every store populated + artifact files on disk."""
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    src = FileSystemDataProvider(base_dir=data_dir, config_dir=config_dir)

    # Current state
    src.property_state.upsert(
        "P-1",
        {"proj_name": "San Artes", "city": "Phoenix", "website": "https://example.com"},
        RUN_DATE,
    )
    src.property_state.upsert(
        "P-2",
        {"proj_name": "Lofts 99", "city": "Austin"},
        RUN_DATE,
    )
    src.unit_state.upsert_units(
        "P-1",
        [
            {"unit_id": "101", "rent_low": 1500, "rent_high": 1500, "beds": 1, "baths": 1.0},
            {"unit_id": "102", "rent_low": 1800, "rent_high": 1800, "beds": 2, "baths": 2.0},
        ],
        RUN_DATE,
    )

    # Scrape events
    src.scrape_events.append(
        ScrapeEvent(
            event_id=str(uuid.uuid4()),
            property_id="P-1",
            scrape_timestamp=datetime.now(UTC),
            scrape_outcome=ScrapeOutcome.SUCCESS,
            extraction_tier=1,
            page_load_ms=4200,
            confidence_score=0.92,
        )
    )

    # Profiles
    src.profiles.put(ScrapeProfile(canonical_id="P-1", version=1, updated_by="BOOTSTRAP"))

    # Run artifacts written by the runner
    src.runs.write_properties(
        RUN_DATE,
        [
            {"Property Name": "San Artes", "City": "Phoenix", "units": []},
            {"Property Name": "Lofts 99", "City": "Austin", "units": []},
        ],
    )
    src.runs.write_report(
        RUN_DATE,
        RunReport(
            run_date=RUN_DATE,
            generated_at=datetime.now(UTC).isoformat(),
            totals=RunSummary(properties=2, succeeded=2, failed=0, success_rate_pct=100.0),
            tier_distribution={"TIER_1_API": 2},
            cost={"openrouter": 0.019},
            slo_violations=[],
        ),
    )
    src.runs.append_issue(
        RUN_DATE,
        IssueEntry(severity="INFO", code="PROPERTY_NEW", message="first scrape", canonical_id="P-1"),
    )
    src.runs.append_ledger_entry(
        RUN_DATE,
        LedgerEntry(canonical_id="P-1", status="SUCCESS", units_count=2, url="https://example.com"),
    )

    # Per-property extraction result
    src.extraction_results.write(
        RUN_DATE,
        ExtractionResult(
            property_id="P-1",
            tier=ExtractionTier.API_INTERCEPTION,
            status=ExtractionStatus.SUCCESS,
            confidence_score=0.92,
            raw_fields={"unit_count": 2},
        ),
    )

    # Raw artifact files the runner writes outside the DataProvider contract.
    # sync_run_to_pg reads these with Path.glob, so they must live on disk.
    run_dir = data_dir / "runs" / RUN_DATE

    property_reports_dir = run_dir / "property_reports"
    property_reports_dir.mkdir(parents=True, exist_ok=True)
    (property_reports_dir / "P-1.md").write_text("# P-1\nreport body", encoding="utf-8")
    (property_reports_dir / "P-2.md").write_text("# P-2\nreport body", encoding="utf-8")

    (run_dir / "llm_report.json").write_text(
        '{"calls": 3, "total_cost_usd": 0.019}', encoding="utf-8"
    )

    llm_details_dir = run_dir / "llm_report"
    llm_details_dir.mkdir(parents=True, exist_ok=True)
    (llm_details_dir / "P-1.json").write_text('{"calls": 2}', encoding="utf-8")
    (llm_details_dir / "P-2.json").write_text('{"calls": 1}', encoding="utf-8")

    llm_diag_dir = run_dir / "llm_diagnostics"
    llm_diag_dir.mkdir(parents=True, exist_ok=True)
    (llm_diag_dir / "P-1_field_recovery.json").write_text('{"recovered": 3}', encoding="utf-8")
    (llm_diag_dir / "P-1_tier_trace.json").write_text('{"tier": 1}', encoding="utf-8")

    return src, data_dir, config_dir


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'sync_target.db'}"


def test_sync_round_trip_populates_every_artifact(tmp_path: Path) -> None:
    """One pass through sync_run_to_postgres must land every artifact.

    If this test passes but prod stays empty, the bug is in the runtime
    wiring (env vars, IAM, container) — not the sync logic.
    """
    src, data_dir, config_dir = _seed_fs_provider(tmp_path)
    src.close()

    # Use monkeypatching at import time? No — sync_run_to_postgres builds
    # its own PostgresDataProvider; we need to substitute. Simpler: call
    # the helpers directly with matching inputs, which is what the
    # orchestrator does internally.
    target = SqliteDataProvider(url=_sqlite_url(tmp_path))
    src2 = FileSystemDataProvider(base_dir=data_dir, config_dir=config_dir)
    try:
        canonical_ids = sorted(src2.property_state.all_canonical_ids())
        with target.transaction():
            assert sync_run_to_pg._copy_state(src2, target) == 2
            assert sync_run_to_pg._copy_profiles(src2, target) == 1
            assert sync_run_to_pg._copy_events(src2, target) == 1
            run_out = sync_run_to_pg._copy_run(src2, target, RUN_DATE)
            assert sync_run_to_pg._copy_extractions(src2, target, RUN_DATE, canonical_ids) == 1
        assert run_out["properties"] == 2
        assert run_out["report"] == 1
        assert run_out["issues"] == 1
        assert run_out["ledger"] == 1

        engine = target.engine
        run_dir = data_dir / "runs" / RUN_DATE
        assert sync_run_to_pg._upsert_property_reports(engine, RUN_DATE, run_dir / "property_reports") == 2
        assert sync_run_to_pg._upsert_llm_report(engine, RUN_DATE, run_dir / "llm_report.json") == 1
        assert sync_run_to_pg._upsert_llm_property_details(engine, RUN_DATE, run_dir / "llm_report") == 2
        assert sync_run_to_pg._upsert_llm_diagnostics(engine, RUN_DATE, run_dir / "llm_diagnostics") == 2
    finally:
        src2.close()
        target.close()


def test_sync_is_idempotent(tmp_path: Path) -> None:
    """Re-running the sync must not duplicate rows — every write is an upsert.

    Cloud Run retries are a fact; if a second invocation inserts ghost
    rows the DB drifts away from FS and we get duplicate-key spam or
    stale data depending on how on_conflict behaves per table.
    """
    src, data_dir, config_dir = _seed_fs_provider(tmp_path)
    src.close()

    target = SqliteDataProvider(url=_sqlite_url(tmp_path))
    try:
        for _ in range(2):
            src2 = FileSystemDataProvider(base_dir=data_dir, config_dir=config_dir)
            try:
                canonical_ids = sorted(src2.property_state.all_canonical_ids())
                with target.transaction():
                    sync_run_to_pg._copy_state(src2, target)
                    sync_run_to_pg._copy_profiles(src2, target)
                    sync_run_to_pg._copy_run(src2, target, RUN_DATE)
                    sync_run_to_pg._copy_extractions(src2, target, RUN_DATE, canonical_ids)
                engine = target.engine
                run_dir = data_dir / "runs" / RUN_DATE
                sync_run_to_pg._upsert_property_reports(engine, RUN_DATE, run_dir / "property_reports")
                sync_run_to_pg._upsert_llm_report(engine, RUN_DATE, run_dir / "llm_report.json")
                sync_run_to_pg._upsert_llm_property_details(engine, RUN_DATE, run_dir / "llm_report")
                sync_run_to_pg._upsert_llm_diagnostics(engine, RUN_DATE, run_dir / "llm_diagnostics")
            finally:
                src2.close()

        # After two passes property_state still holds two distinct rows
        # (upsert semantics), not four.
        assert len(target.property_state.all_canonical_ids()) == 2

        # run_reports is keyed by run_date → single row.
        engine = target.engine
        from sqlalchemy import select
        from data_provider.sql.models import (
            LlmReportRow,
            PropertyReportRow,
            RunReportRow,
        )

        with engine.connect() as conn:
            assert conn.execute(select(RunReportRow).where(RunReportRow.run_date == RUN_DATE)).all().__len__() == 1
            assert conn.execute(select(LlmReportRow).where(LlmReportRow.run_date == RUN_DATE)).all().__len__() == 1
            # property_reports is keyed by (run_date, cid) → 2 rows, not 4.
            rows = conn.execute(
                select(PropertyReportRow).where(PropertyReportRow.run_date == RUN_DATE)
            ).all()
            assert len(rows) == 2
    finally:
        target.close()


def test_sync_raises_when_run_dir_missing(tmp_path: Path) -> None:
    """sync_run_to_postgres must fail fast on a missing run dir.

    Swallowing this error would let shard_entry mark the shard green
    while nothing moved — the exact failure mode we're fixing.
    """
    with pytest.raises(FileNotFoundError, match="run dir not found"):
        sync_run_to_pg.sync_run_to_postgres(
            run_date="2099-12-31",
            data_dir=tmp_path / "does-not-exist",
            config_dir=tmp_path / "config",
            database_url=_sqlite_url(tmp_path),
        )


def test_sync_llm_diagnostics_skips_unparseable_filenames(tmp_path: Path) -> None:
    """Files not matching ``{cid}_{kind}.json`` are logged + skipped, not counted.

    Real runs have occasionally produced names like ``unknown.json`` or
    ``.gitkeep``; we don't want those to crash the whole sync.
    """
    data_dir = tmp_path / "data"
    run_dir = data_dir / "runs" / RUN_DATE
    diag_dir = run_dir / "llm_diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)
    (diag_dir / "bad.json").write_text("{}", encoding="utf-8")       # no underscore
    (diag_dir / "P-1_valid.json").write_text("{}", encoding="utf-8")  # good

    target = SqliteDataProvider(url=_sqlite_url(tmp_path))
    try:
        n = sync_run_to_pg._upsert_llm_diagnostics(target.engine, RUN_DATE, diag_dir)
    finally:
        target.close()
    assert n == 1


def test_sync_handles_empty_artifact_dirs(tmp_path: Path) -> None:
    """A run with no LLM diagnostics / no property reports should still succeed.

    Many canary runs produce zero llm_diagnostics because every property
    hit a deterministic tier. The sync must not treat that as failure.
    """
    data_dir = tmp_path / "data"
    run_dir = data_dir / "runs" / RUN_DATE
    run_dir.mkdir(parents=True, exist_ok=True)

    target = SqliteDataProvider(url=_sqlite_url(tmp_path))
    try:
        engine = target.engine
        assert sync_run_to_pg._upsert_property_reports(engine, RUN_DATE, run_dir / "property_reports") == 0
        assert sync_run_to_pg._upsert_llm_report(engine, RUN_DATE, run_dir / "llm_report.json") == 0
        assert sync_run_to_pg._upsert_llm_property_details(engine, RUN_DATE, run_dir / "llm_report") == 0
        assert sync_run_to_pg._upsert_llm_diagnostics(engine, RUN_DATE, run_dir / "llm_diagnostics") == 0
    finally:
        target.close()


# ── DLQ sync ────────────────────────────────────────────────────────────────


def _dlq_count(engine) -> int:
    from sqlalchemy import select
    from data_provider.sql.models import DlqEntryRow

    with engine.connect() as conn:
        return conn.execute(select(DlqEntryRow)).all().__len__()


def _dlq_row(engine, pid: str):
    from sqlalchemy import select
    from data_provider.sql.models import DlqEntryRow

    with engine.connect() as conn:
        return conn.execute(select(DlqEntryRow).where(DlqEntryRow.property_id == pid)).first()


def test_dlq_load_live_entries_honors_tombstones(tmp_path: Path) -> None:
    """_load_dlq_live_entries matches Dlq._load's compaction semantics.

    A property parked → unparked → parked sequence produces one live
    entry (the final park). An orphan unpark at end produces no entry.
    """
    path = tmp_path / "dlq.jsonl"
    path.write_text(
        '\n'.join([
            '{"property_id":"P-1","parked_at":"t0","reason":"timeout","last_error_signature":"sig","retry_at":"t1","unparked":false}',
            '{"property_id":"P-2","parked_at":"t0","reason":"blocked","last_error_signature":"sig","retry_at":"t1","unparked":false}',
            '{"property_id":"P-1","parked_at":"t0","reason":"","last_error_signature":"","retry_at":"","unparked":true}',
            '{"property_id":"P-1","parked_at":"t2","reason":"timeout","last_error_signature":"sig2","retry_at":"t3","unparked":false}',
            '{"property_id":"P-3","parked_at":"","reason":"","last_error_signature":"","retry_at":"","unparked":true}',
        ]) + "\n",
        encoding="utf-8",
    )
    live = sync_run_to_pg._load_dlq_live_entries(path)
    assert set(live.keys()) == {"P-1", "P-2"}
    # Re-park should win over the earlier version, carrying the newer signature.
    assert live["P-1"]["last_error_signature"] == "sig2"


def test_dlq_sync_upserts_parked_and_deletes_unparked(tmp_path: Path) -> None:
    """DB mirrors the post-compaction live set.

    Regression: without the DELETE step, unparking a property in the
    JSONL log would leave a ghost row in dlq_entries — the retry job
    would keep processing a property that's no longer parked locally.
    """
    target = SqliteDataProvider(url=_sqlite_url(tmp_path))
    dlq_path = tmp_path / "dlq.jsonl"
    try:
        # Round 1: two properties parked.
        dlq_path.write_text(
            '\n'.join([
                '{"property_id":"P-1","parked_at":"t0","reason":"timeout","last_error_signature":"sig","retry_at":"t1","unparked":false}',
                '{"property_id":"P-2","parked_at":"t0","reason":"blocked","last_error_signature":"sig","retry_at":"t1","unparked":false}',
            ]) + "\n",
            encoding="utf-8",
        )
        summary = sync_run_to_pg._sync_dlq(target.engine, dlq_path)
        assert summary == {"upserted": 2, "deleted": 0}
        assert _dlq_count(target.engine) == 2

        # Round 2: P-1 unparked. DB must drop it.
        dlq_path.write_text(
            '\n'.join([
                '{"property_id":"P-1","parked_at":"t0","reason":"timeout","last_error_signature":"sig","retry_at":"t1","unparked":false}',
                '{"property_id":"P-2","parked_at":"t0","reason":"blocked","last_error_signature":"sig","retry_at":"t1","unparked":false}',
                '{"property_id":"P-1","parked_at":"","reason":"","last_error_signature":"","retry_at":"","unparked":true}',
            ]) + "\n",
            encoding="utf-8",
        )
        summary = sync_run_to_pg._sync_dlq(target.engine, dlq_path)
        assert summary == {"upserted": 1, "deleted": 1}
        assert _dlq_count(target.engine) == 1
        assert _dlq_row(target.engine, "P-1") is None
        assert _dlq_row(target.engine, "P-2") is not None

        # Round 3: re-parking P-1 with new reason overwrites in-place.
        dlq_path.write_text(
            '\n'.join([
                '{"property_id":"P-1","parked_at":"t5","reason":"blocked","last_error_signature":"newsig","retry_at":"t6","unparked":false}',
                '{"property_id":"P-2","parked_at":"t0","reason":"blocked","last_error_signature":"sig","retry_at":"t1","unparked":false}',
            ]) + "\n",
            encoding="utf-8",
        )
        summary = sync_run_to_pg._sync_dlq(target.engine, dlq_path)
        assert summary == {"upserted": 2, "deleted": 0}
        row = _dlq_row(target.engine, "P-1")
        assert row is not None
        assert row.reason == "blocked"
        assert row.last_error_signature == "newsig"
    finally:
        target.close()


def test_dlq_sync_missing_file_is_noop(tmp_path: Path) -> None:
    """A fresh data dir with no DLQ file yet must not crash the sync.

    Happens on the very first run of a brand-new environment.
    """
    target = SqliteDataProvider(url=_sqlite_url(tmp_path))
    try:
        summary = sync_run_to_pg._sync_dlq(target.engine, tmp_path / "does-not-exist.jsonl")
        assert summary == {"upserted": 0, "deleted": 0}
    finally:
        target.close()


def test_load_dlq_from_db_to_file_writes_compacted_jsonl(tmp_path: Path) -> None:
    """retry_entry's pre-hydration step reproduces the JSONL format Dlq expects.

    Regression: if the produced file isn't valid JSONL, ``Dlq._load()``
    silently skips every line (``try/except (JSONDecodeError, TypeError)``)
    and the retry runner sees an empty DLQ — identical user-visible
    symptom to the bug we're fixing.
    """
    import json

    target = SqliteDataProvider(url=_sqlite_url(tmp_path))
    dlq_path_src = tmp_path / "src.jsonl"
    dlq_path_dst = tmp_path / "dst.jsonl"
    try:
        dlq_path_src.write_text(
            '\n'.join([
                '{"property_id":"P-1","parked_at":"t0","reason":"timeout","last_error_signature":"sig","retry_at":"t1","unparked":false}',
                '{"property_id":"P-2","parked_at":"t0","reason":"blocked","last_error_signature":"sig","retry_at":"t1","unparked":false}',
            ]) + "\n",
            encoding="utf-8",
        )
        sync_run_to_pg._sync_dlq(target.engine, dlq_path_src)

        n = sync_run_to_pg.load_dlq_from_db_to_file(engine=target.engine, path=dlq_path_dst)
        assert n == 2

        # Round-trip: feed the written file back through _load_dlq_live_entries
        # and confirm Dlq._load would see both properties.
        round_trip = sync_run_to_pg._load_dlq_live_entries(dlq_path_dst)
        assert set(round_trip.keys()) == {"P-1", "P-2"}

        # Every line must be a valid JSON object with the Dlq field names.
        for raw in dlq_path_dst.read_text(encoding="utf-8").splitlines():
            entry = json.loads(raw)
            assert set(entry.keys()) == {
                "property_id", "parked_at", "reason",
                "last_error_signature", "retry_at", "unparked",
            }
            assert entry["unparked"] is False
    finally:
        target.close()


def test_load_dlq_from_db_to_file_creates_empty_file_when_db_empty(tmp_path: Path) -> None:
    """Fresh environments with no parked properties still get a valid empty file."""
    target = SqliteDataProvider(url=_sqlite_url(tmp_path))
    dlq_path = tmp_path / "fresh" / "dlq.jsonl"
    try:
        n = sync_run_to_pg.load_dlq_from_db_to_file(engine=target.engine, path=dlq_path)
    finally:
        target.close()
    assert n == 0
    assert dlq_path.exists()
    assert dlq_path.read_text(encoding="utf-8") == ""


def test_dlq_round_trip_with_real_dlq_class(tmp_path: Path) -> None:
    """End-to-end: Dlq instance → sync → load_from_db → new Dlq instance sees same state.

    This is the contract retry_entry relies on: we take the current
    Dlq's live entries, push them to DB, then the retry job fetches
    them back into a brand-new Dlq and sees the same entries. Any
    schema drift between sync and Dlq._load breaks this.
    """
    from ma_poc.discovery.dlq import Dlq

    target = SqliteDataProvider(url=_sqlite_url(tmp_path))
    try:
        # 1. Scrape-side: populate a Dlq.
        src_path = tmp_path / "scrape_state" / "dlq.jsonl"
        dlq = Dlq(src_path)
        dlq.park("P-A", reason="timeout", err_sig="ETIMEDOUT")
        dlq.park("P-B", reason="blocked", err_sig="HTTP 403")

        # 2. Scrape-side sync to DB.
        sync_run_to_pg._sync_dlq(target.engine, src_path)
        assert _dlq_count(target.engine) == 2

        # 3. Retry-side hydrate: DB → fresh /tmp file.
        dst_path = tmp_path / "retry_state" / "dlq.jsonl"
        sync_run_to_pg.load_dlq_from_db_to_file(engine=target.engine, path=dst_path)

        # 4. Retry-side: fresh Dlq instance reads the hydrated file.
        retry_dlq = Dlq(dst_path)
        assert retry_dlq.is_parked("P-A")
        assert retry_dlq.is_parked("P-B")
        assert not retry_dlq.is_parked("P-NONEXISTENT")

        # 5. Retry-side unparks P-A and re-syncs. DB must reflect it.
        retry_dlq.unpark("P-A")
        sync_run_to_pg._sync_dlq(target.engine, dst_path)
        assert _dlq_count(target.engine) == 1
        assert _dlq_row(target.engine, "P-A") is None
        assert _dlq_row(target.engine, "P-B") is not None
    finally:
        target.close()


def test_sync_run_to_postgres_includes_dlq(tmp_path: Path) -> None:
    """The top-level sync_run_to_postgres must call _sync_dlq too.

    Regression: if anyone refactors sync_run_to_postgres and drops the
    DLQ call, the scrape-side DLQ stops persisting and we silently
    regress to the pre-fix state. Check it's wired end-to-end.
    """
    src, data_dir, config_dir = _seed_fs_provider(tmp_path)
    src.close()

    # Seed a DLQ entry in the state dir the runner would use.
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "dlq.jsonl").write_text(
        '{"property_id":"P-PARKED","parked_at":"t0","reason":"timeout","last_error_signature":"sig","retry_at":"t1","unparked":false}\n',
        encoding="utf-8",
    )

    url = _sqlite_url(tmp_path)
    summary = sync_run_to_pg.sync_run_to_postgres(
        run_date=RUN_DATE,
        data_dir=data_dir,
        config_dir=config_dir,
        database_url=url,
    )
    assert summary["dlq"] == {"upserted": 1, "deleted": 0}

    from data_provider.sqlite import SqliteDataProvider as _Sqlite
    verify = _Sqlite(url=url)
    try:
        assert _dlq_count(verify.engine) == 1
        assert _dlq_row(verify.engine, "P-PARKED") is not None
    finally:
        verify.close()
