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


# Tests that exercise ``sync_run_to_postgres()`` end-to-end must use a
# recent run_date — the orchestrator now invokes ``_apply_retention()``
# at the end of every sync, which deletes anything older than 3 days.
# A hard-coded fixed date would be silently swept after the calendar
# moves past it, leaving the tests asserting against empty tables.
from datetime import date as _date

RUN_DATE = _date.today().isoformat()


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
        # _copy_run handles properties + issues + ledger inside the
        # session txn. The report lives in Stage 2 (engine.begin
        # path) so it's exercised by the top-level sync_run_to_postgres
        # — or by calling the store directly, as here.
        assert run_out["properties"] == 2
        assert run_out["issues"] == 1
        assert run_out["ledger"] == 1
        # Drive the report write via the store contract (legacy
        # replace semantics — backfill_pg.py uses this path).
        report = src2.runs.read_report(RUN_DATE)
        assert report is not None
        target.runs.write_report(RUN_DATE, report)

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
                # Report + artifact tables run Stage-2 (engine.begin).
                report = src2.runs.read_report(RUN_DATE)
                if report is not None:
                    target.runs.write_report(RUN_DATE, report)
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
        from sqlalchemy.orm import Session
        from data_provider.sql.models import (
            LlmReportRow,
            PropertyReportRow,
            RunReportRow,
        )

        with Session(engine) as session:
            assert (
                len(list(session.execute(
                    select(RunReportRow).where(RunReportRow.run_date == RUN_DATE)
                ).scalars()))
                == 1
            )
            assert (
                len(list(session.execute(
                    select(LlmReportRow).where(LlmReportRow.run_date == RUN_DATE)
                ).scalars()))
                == 1
            )
            # property_reports is keyed by (run_date, cid) → 2 rows, not 4.
            rows = list(session.execute(
                select(PropertyReportRow).where(PropertyReportRow.run_date == RUN_DATE)
            ).scalars())
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


# ── Multi-shard + derive-from-snapshots ────────────────────────────────────


def _jugnu_v2_property(
    canonical_id: str,
    *,
    tier: str = "TIER_4_LLM_DOM",
    verdict: str = "SUCCESS",
    units: list[dict[str, Any]] | None = None,
    proj_name: str | None = None,
    city: str | None = None,
    website: str | None = None,
) -> dict[str, Any]:
    """Build a V2 property dict shaped like what jugnu_runner writes.

    Mirrors the structure of ``ma_poc/scripts/jugnu_runner.py::_format_v2``
    — the minimum keys the sync layer reads. If this drifts from the
    runner's actual output, the sync test is a false positive.
    """
    return {
        "apartment_id": None,
        "proj_name": proj_name or f"Property {canonical_id}",
        "address": None,
        "city": city,
        "state": None,
        "zip_code": None,
        "country": None,
        "phone": None,
        "email_address": None,
        "website": website or f"https://{canonical_id}.example.com",
        "pmc": None,
        "website_design": None,
        "concessions": None,
        "units": units or [],
        "_meta": {
            "canonical_id": canonical_id,
            "verdict": verdict,
            "scrape_tier_used": tier,
        },
        "_extract_result": {"tier_used": tier, "llm_cost_usd": 0.0},
    }


def _seed_shard_fs(
    tmp_path: Path,
    shard_name: str,
    properties: list[dict[str, Any]],
) -> tuple[Path, Path]:
    """Write one shard's filesystem layout under ``tmp_path/shard_name/``.

    Returns (data_dir, config_dir). Each shard is a standalone /tmp so
    the test can drive concurrent sync_run_to_postgres invocations
    against the same SQLite DB — exactly like Cloud Run runs 10 tasks
    with isolated /tmp.
    """
    data_dir = tmp_path / shard_name / "data"
    config_dir = tmp_path / shard_name / "config"
    (data_dir / "runs" / RUN_DATE).mkdir(parents=True, exist_ok=True)
    (data_dir / "state").mkdir(parents=True, exist_ok=True)

    # Write properties.json directly (jugnu runner writes it incrementally;
    # a single final write is functionally equivalent for the sync layer).
    import json as _json

    (data_dir / "runs" / RUN_DATE / "properties.json").write_text(
        _json.dumps(properties), encoding="utf-8"
    )
    # Minimal report.json so _copy_run's report path fires.
    (data_dir / "runs" / RUN_DATE / "report.json").write_text(
        _json.dumps(
            {
                "run_date": RUN_DATE,
                "generated_at": datetime.now(UTC).isoformat(),
                "totals": {
                    "properties": len(properties),
                    "succeeded": sum(
                        1 for p in properties if (p.get("_meta") or {}).get("verdict") == "SUCCESS"
                    ),
                    "failed": sum(
                        1
                        for p in properties
                        if str((p.get("_meta") or {}).get("verdict") or "").startswith("FAIL")
                    ),
                    "carry_forward": 0,
                    "success_rate_pct": 0.0,
                },
                "tier_distribution": {
                    str((p.get("_meta") or {}).get("scrape_tier_used") or "UNKNOWN"): 1
                    for p in properties
                },
                "cost": {"openrouter": 0.001 * len(properties)},
                "slo_violations": [],
            }
        ),
        encoding="utf-8",
    )
    # Minimal llm_report.json so the shard-merge path fires.
    (data_dir / "runs" / RUN_DATE / "llm_report.json").write_text(
        _json.dumps(
            {
                "calls": len(properties),
                "total_cost_usd": 0.001 * len(properties),
                "total_tokens_in": 100 * len(properties),
                "total_tokens_out": 50 * len(properties),
                "by_property": {
                    (p.get("_meta") or {}).get("canonical_id", "?"): {"calls": 1}
                    for p in properties
                },
            }
        ),
        encoding="utf-8",
    )
    return data_dir, config_dir


def test_multi_shard_sync_does_not_clobber_other_shards_snapshots(tmp_path: Path) -> None:
    """Two shards syncing to the same run_date keep BOTH sets of property_snapshots.

    Regression for the "all shards wipe each other" bug: previously
    ``SqlRunStore.write_properties`` did ``DELETE WHERE run_date = X``
    before inserting, so a 10-shard run produced only the last shard's
    ~50 rows in ``property_snapshots``. With the fix, the delete is
    scoped to THIS batch's canonical_ids, leaving other shards alone.
    """
    shard_a_props = [
        _jugnu_v2_property(f"A-{i}", units=[{"unit_id": f"a{i}", "rent_low": 1000 + i}])
        for i in range(3)
    ]
    shard_b_props = [
        _jugnu_v2_property(f"B-{i}", units=[{"unit_id": f"b{i}", "rent_low": 2000 + i}])
        for i in range(3)
    ]
    data_a, config_a = _seed_shard_fs(tmp_path, "shard_a", shard_a_props)
    data_b, config_b = _seed_shard_fs(tmp_path, "shard_b", shard_b_props)

    url = _sqlite_url(tmp_path)
    sync_run_to_pg.sync_run_to_postgres(
        run_date=RUN_DATE, data_dir=data_a, config_dir=config_a,
        database_url=url, shard_id="0",
    )
    sync_run_to_pg.sync_run_to_postgres(
        run_date=RUN_DATE, data_dir=data_b, config_dir=config_b,
        database_url=url, shard_id="1",
    )

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from data_provider.sql.models import PropertyRow, PropertySnapshotRow, UnitRow
    from data_provider.sqlite import SqliteDataProvider as _Sqlite

    verify = _Sqlite(url=url)
    try:
        with Session(verify.engine) as session:
            # All 6 snapshots survive (3 from each shard, not 3 total).
            snap_cids = {
                row.canonical_id
                for row in session.execute(
                    select(PropertySnapshotRow).where(PropertySnapshotRow.run_date == RUN_DATE)
                ).scalars()
            }
            assert snap_cids == {f"A-{i}" for i in range(3)} | {f"B-{i}" for i in range(3)}

            # Current-state tables (the TS frontend reads these).
            prop_cids = {
                r.canonical_id
                for r in session.execute(select(PropertyRow)).scalars()
            }
            assert prop_cids == snap_cids

            # Units populated too — previously ``state: 0`` in the summary
            # because property_index.json was never written.
            unit_rows = list(session.execute(select(UnitRow)).scalars())
            assert len(unit_rows) == 6  # one unit per property in this fixture
    finally:
        verify.close()


def test_multi_shard_sync_merges_report_totals(tmp_path: Path) -> None:
    """Two shards each reporting 3/3 success produce an aggregate 6/6.

    Previously ``write_report`` upserted by run_date, last-writer-wins,
    so ten shards reporting ~50/50 each left the run_reports row holding
    just one shard's totals. The fix routes each shard's report through
    ``_merge_run_report`` which stores per-shard contributions under
    ``extra.shards[shard_id]`` and recomputes aggregate totals.
    """
    shard_a_props = [_jugnu_v2_property(f"A-{i}") for i in range(3)]
    shard_b_props = [_jugnu_v2_property(f"B-{i}") for i in range(3)]
    data_a, config_a = _seed_shard_fs(tmp_path, "shard_a", shard_a_props)
    data_b, config_b = _seed_shard_fs(tmp_path, "shard_b", shard_b_props)

    url = _sqlite_url(tmp_path)
    sync_run_to_pg.sync_run_to_postgres(
        run_date=RUN_DATE, data_dir=data_a, config_dir=config_a,
        database_url=url, shard_id="0",
    )
    sync_run_to_pg.sync_run_to_postgres(
        run_date=RUN_DATE, data_dir=data_b, config_dir=config_b,
        database_url=url, shard_id="1",
    )

    from sqlalchemy.orm import Session

    from data_provider.sql.models import LlmReportRow, RunReportRow
    from data_provider.sqlite import SqliteDataProvider as _Sqlite

    verify = _Sqlite(url=url)
    try:
        with Session(verify.engine) as session:
            row = session.get(RunReportRow, RUN_DATE)
            assert row is not None
            totals = row.totals or {}
            assert totals.get("properties") == 6
            assert totals.get("succeeded") == 6
            assert totals.get("failed") == 0
            assert totals.get("success_rate_pct") == 100.0
            # Cost summed across shards.
            assert abs((row.cost or {}).get("openrouter", 0.0) - 0.006) < 1e-6
            # Per-shard contributions preserved for debugging.
            shards = (row.extra or {}).get("shards") or {}
            assert set(shards.keys()) == {"0", "1"}

            # LLM report merged.
            llm = session.get(LlmReportRow, RUN_DATE)
            assert llm is not None
            body = llm.payload or {}
            assert body.get("calls") == 6
            assert abs(body.get("total_cost_usd", 0.0) - 0.006) < 1e-6
            assert set((body.get("shards") or {}).keys()) == {"0", "1"}
    finally:
        verify.close()


def test_shard_retry_is_idempotent_on_report_merge(tmp_path: Path) -> None:
    """A shard retrying with the same shard_id must not double-count.

    Cloud Run retries failed shards up to ``max_retries``. Each retry
    should replace its prior contribution under ``shards[shard_id]``
    rather than append. The aggregate must stay consistent with the
    union of live shards.
    """
    props = [_jugnu_v2_property(f"R-{i}") for i in range(4)]
    data_dir, config_dir = _seed_shard_fs(tmp_path, "shard_r", props)
    url = _sqlite_url(tmp_path)

    # Fire the same shard's sync twice.
    sync_run_to_pg.sync_run_to_postgres(
        run_date=RUN_DATE, data_dir=data_dir, config_dir=config_dir,
        database_url=url, shard_id="0",
    )
    sync_run_to_pg.sync_run_to_postgres(
        run_date=RUN_DATE, data_dir=data_dir, config_dir=config_dir,
        database_url=url, shard_id="0",
    )

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from data_provider.sql.models import (
        PropertyRow,
        RunLedgerRow,
        RunReportRow,
        ScrapeEventRow,
    )
    from data_provider.sqlite import SqliteDataProvider as _Sqlite

    verify = _Sqlite(url=url)
    try:
        with Session(verify.engine) as session:
            # Totals are still 4/4, not 8/8.
            row = session.get(RunReportRow, RUN_DATE)
            assert row is not None
            assert row.totals.get("properties") == 4

            # Current state: 4 rows, not 4 (identical) on a retry.
            assert len(list(session.execute(select(PropertyRow)).scalars())) == 4

            # Ledger: 4 rows per cid, not 8.
            ledger_rows = list(session.execute(select(RunLedgerRow)).scalars())
            assert len(ledger_rows) == 4
            assert {r.canonical_id for r in ledger_rows} == {f"R-{i}" for i in range(4)}

            # scrape_events keyed on (property_id, run_date) via stable
            # event_id — 4 rows, not 8.
            ev_rows = list(session.execute(select(ScrapeEventRow)).scalars())
            assert len(ev_rows) == 4
    finally:
        verify.close()


def test_derived_state_populates_previously_empty_tables(tmp_path: Path) -> None:
    """properties, units, run_ledger, scrape_events, extraction_results —
    all derived from properties.json during sync.

    These tables stayed empty because jugnu_runner never writes
    property_index.json / unit_index.json / issues.jsonl / ledger.jsonl /
    extraction_output/. Without this derive-from-snapshots step, the TS
    frontend's ``properties`` + ``units`` endpoints returned no data
    even when property_snapshots had rows.
    """
    units_a = [
        {"unit_id": "101", "rent_low": 1500, "rent_high": 1500, "beds": 1, "baths": 1.0, "area": 700},
        {"unit_id": "102", "rent_low": 1800, "rent_high": 1800, "beds": 2, "baths": 2.0, "area": 950},
    ]
    props = [
        _jugnu_v2_property("P-ok", tier="TIER_1_API_RENTCAFE", units=units_a),
        _jugnu_v2_property("P-fail", tier="generic:no_body_short_circuit", verdict="FAILED_UNREACHABLE"),
    ]
    data_dir, config_dir = _seed_shard_fs(tmp_path, "derive_shard", props)
    url = _sqlite_url(tmp_path)

    summary = sync_run_to_pg.sync_run_to_postgres(
        run_date=RUN_DATE, data_dir=data_dir, config_dir=config_dir,
        database_url=url, shard_id="0",
    )

    # Every derive-from-snapshots counter must have work to show.
    assert summary["state_from_snapshots"] == 2
    assert summary["ledger_from_snapshots"] == 2
    assert summary["scrape_events_from_snapshots"] == 2
    assert summary["extractions_from_snapshots"] == 2
    # P-fail emits a synthetic FAILED_* issue; P-ok has no validation data.
    assert summary["issues_from_snapshots"] >= 1

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from data_provider.sql.models import (
        ExtractionResultRow,
        PropertyRow,
        RunIssueRow,
        RunLedgerRow,
        ScrapeEventRow,
        UnitRow,
    )
    from data_provider.sqlite import SqliteDataProvider as _Sqlite

    verify = _Sqlite(url=url)
    try:
        with Session(verify.engine) as session:
            # properties: both.
            assert {
                r.canonical_id for r in session.execute(select(PropertyRow)).scalars()
            } == {"P-ok", "P-fail"}
            # units: two units for P-ok.
            unit_rows = list(session.execute(select(UnitRow)).scalars())
            assert {(r.canonical_id, r.unit_id) for r in unit_rows} == {
                ("P-ok", "101"),
                ("P-ok", "102"),
            }
            # Unit data flows through the V2 upsert (beds/baths/rent_low/area).
            unit_101 = next(r for r in unit_rows if r.unit_id == "101")
            assert unit_101.rent_low == 1500
            assert unit_101.beds == 1
            assert unit_101.area == 700

            # run_ledger: 2 rows.
            lrows = list(
                session.execute(select(RunLedgerRow).where(RunLedgerRow.run_date == RUN_DATE)).scalars()
            )
            assert {r.canonical_id for r in lrows} == {"P-ok", "P-fail"}
            ok_row = next(r for r in lrows if r.canonical_id == "P-ok")
            assert ok_row.status == "SUCCESS"
            assert ok_row.units_count == 2
            fail_row = next(r for r in lrows if r.canonical_id == "P-fail")
            assert fail_row.scrape_failed is True

            # scrape_events: 2 rows, stable event_ids so a re-sync upserts.
            events = list(session.execute(select(ScrapeEventRow)).scalars())
            assert {e.property_id for e in events} == {"P-ok", "P-fail"}
            # Tier string to int mapping: TIER_1_API_* → 1
            ok_event = next(e for e in events if e.property_id == "P-ok")
            assert ok_event.extraction_tier == 1
            assert ok_event.scrape_outcome == "SUCCESS"

            # extraction_results: 2 rows.
            xrows = list(session.execute(select(ExtractionResultRow)).scalars())
            assert {r.property_id for r in xrows} == {"P-ok", "P-fail"}
            ok_x = next(r for r in xrows if r.property_id == "P-ok")
            assert ok_x.status == "SUCCESS"
            assert ok_x.tier == 1

            # run_issues: at least the synthetic failure for P-fail.
            issue_rows = list(
                session.execute(
                    select(RunIssueRow).where(RunIssueRow.run_date == RUN_DATE)
                ).scalars()
            )
            assert any(i.canonical_id == "P-fail" and i.severity == "ERROR" for i in issue_rows)
    finally:
        verify.close()


def test_crashed_property_record_is_still_persisted_everywhere(tmp_path: Path) -> None:
    """A property whose scrape threw (and was rescued by the crash-catcher
    in ``jugnu_runner._make_failed_record``) has NO ``_meta.verdict``.
    The sync must still land it in every downstream table — otherwise a
    hard crash at scrape time drops the property off the reporting
    surface even though it's present in properties.json.

    This mirrors what jugnu_runner._make_failed_record emits: only
    ``_meta.scrape_tier_used = "FAILED"`` and ``_meta.scrape_errors``.
    """
    crashed = {
        "apartment_id": None,
        "proj_name": None,
        "website": "https://broken.example.com",
        "units": [],
        "_meta": {
            "canonical_id": "P-crash",
            "scrape_tier_used": "FAILED",
            "scrape_errors": ["Playwright timeout after 180s"],
            "carry_forward_used": False,
            # Note: NO verdict key — the crash path never writes one.
        },
    }
    ok = _jugnu_v2_property("P-ok", tier="TIER_1_API_RENTCAFE")
    data_dir, config_dir = _seed_shard_fs(tmp_path, "crash_shard", [crashed, ok])
    url = _sqlite_url(tmp_path)

    sync_run_to_pg.sync_run_to_postgres(
        run_date=RUN_DATE, data_dir=data_dir, config_dir=config_dir,
        database_url=url, shard_id="0",
    )

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from data_provider.sql.models import (
        ExtractionResultRow,
        PropertyRow,
        PropertySnapshotRow,
        RunIssueRow,
        RunLedgerRow,
        ScrapeEventRow,
    )
    from data_provider.sqlite import SqliteDataProvider as _Sqlite

    verify = _Sqlite(url=url)
    try:
        with Session(verify.engine) as s:
            # Current-state row exists.
            assert s.get(PropertyRow, "P-crash") is not None

            # Snapshot row exists.
            snap = next(
                s.execute(
                    select(PropertySnapshotRow)
                    .where(PropertySnapshotRow.run_date == RUN_DATE)
                    .where(PropertySnapshotRow.canonical_id == "P-crash")
                ).scalars()
            )
            assert snap.payload["_meta"]["scrape_tier_used"] == "FAILED"

            # Ledger entry marked scrape_failed=True.
            lrow = next(
                s.execute(
                    select(RunLedgerRow).where(RunLedgerRow.canonical_id == "P-crash")
                ).scalars()
            )
            assert lrow.scrape_failed is True
            assert lrow.error_count == 1

            # Synthetic issue recorded (previously the sync only looked
            # at ``_meta.verdict`` and silently skipped crash-path
            # records that had no verdict).
            issues = list(
                s.execute(
                    select(RunIssueRow).where(RunIssueRow.canonical_id == "P-crash")
                ).scalars()
            )
            assert any(i.severity == "ERROR" for i in issues)

            # scrape_events.outcome == FAILED.
            ev = next(
                s.execute(
                    select(ScrapeEventRow).where(ScrapeEventRow.property_id == "P-crash")
                ).scalars()
            )
            assert ev.scrape_outcome == "FAILED"

            # extraction_results.status == FAILED.
            xr = next(
                s.execute(
                    select(ExtractionResultRow)
                    .where(ExtractionResultRow.run_date == RUN_DATE)
                    .where(ExtractionResultRow.property_id == "P-crash")
                ).scalars()
            )
            assert xr.status == "FAILED"
    finally:
        verify.close()


def test_stable_event_id_is_deterministic(tmp_path: Path) -> None:
    """Same (property_id, run_date) pair always yields the same event_id.

    CLAUDE.md bug-hunt #15: must use hashlib.sha256, never built-in
    ``hash()`` — the latter is non-deterministic across Python
    processes, which would break upsert semantics on scrape_events.
    """
    a1 = sync_run_to_pg._stable_event_id("P-1", "2026-04-24")
    a2 = sync_run_to_pg._stable_event_id("P-1", "2026-04-24")
    b = sync_run_to_pg._stable_event_id("P-1", "2026-04-25")
    c = sync_run_to_pg._stable_event_id("P-2", "2026-04-24")
    assert a1 == a2
    assert a1 != b
    assert a1 != c
    # Is valid UUID shape so downstream consumers that parse UUIDs work.
    import uuid as _uuid

    _uuid.UUID(a1)


# ─────────────────────────────────────────────────────────────────────────────
# Resilience regression: 2026-04-29 → 05-01 shard-0 incident.
#
# The original bug: a 455-char marketing blurb in
# ``units.floor_plan_name`` (declared ``String(256)``) raised
# ``DataError`` mid-shared-transaction. The whole shard's Stage 1 write
# rolled back, dropping 499 properties × 3 days from Cloud SQL despite
# the runner having scraped them successfully.
#
# Two-layer fix:
#   1. ``_clip_to_column_limits`` trims oversize values at the boundary
#      (covers most cases pre-emptively).
#   2. ``isolate_row_writes`` puts each property's writes in a SAVEPOINT
#      so anything that does slip past the clip is contained to its own
#      row — the rest of the batch keeps committing.
#
# These tests fail loudly if either layer regresses.
# ─────────────────────────────────────────────────────────────────────────────


def _make_property_payload(cid: str, *, floor_plan_name: str = "1BR/1BA") -> dict[str, Any]:
    return {
        "apartment_id": int(cid.lstrip("P-")) if cid.lstrip("P-").isdigit() else None,
        "proj_name": f"Property {cid}",
        "city": "Phoenix",
        "state": "AZ",
        "website": f"https://prop-{cid}.example.com",
        "_meta": {"canonical_id": cid, "verdict": "SUCCESS"},
        "units": [
            {
                "unit_id": f"{cid}-101",
                "floor_plan_name": floor_plan_name,
                "rent_low": 1500,
                "rent_high": 1700,
                "beds": 1,
                "baths": 1.0,
            }
        ],
    }


def test_oversize_floor_plan_does_not_drop_other_properties(tmp_path: Path) -> None:
    """Reproduce the exact production failure mode end-to-end.

    Build 5 properties where one has an over-256-char floor_plan_name.
    Pre-fix: the DataError would roll back the shared transaction and
    zero properties would land. Post-fix (clip + isolate_row_writes):
    all 5 properties commit, with the bad one's floor_plan clipped.
    """
    target = SqliteDataProvider(url=_sqlite_url(tmp_path))
    try:
        bad = "Perfect luxury living space " * 20  # 560 chars > 256
        properties = [
            _make_property_payload(f"PROP-{i}", floor_plan_name=("1BR/1BA" if i != 2 else bad))
            for i in range(5)
        ]
        with target.transaction():
            count = sync_run_to_pg._sync_current_state_from_snapshots(
                target, "2026-05-02", properties
            )
        # All five properties land — none is silently dropped.
        assert count == 5, "the shard-0 incident regressed: a bad row killed the batch"
        assert target.property_state.all_canonical_ids() == {
            "PROP-0", "PROP-1", "PROP-2", "PROP-3", "PROP-4"
        }
        # The bad row is stored with a clipped floor_plan_name — the
        # rest of its data (rent, beds, etc.) is intact.
        bad_units = target.unit_state.get_units("PROP-2")
        assert "PROP-2-101" in bad_units
        assert len(bad_units["PROP-2-101"].floor_plan_name or "") <= 256
        assert bad_units["PROP-2-101"].rent_low == 1500
    finally:
        target.close()


def test_per_property_failure_is_logged_as_run_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a property's writes do fail (e.g. an integrity violation
    that the clip can't help with), the failure must show up in
    ``run_issues`` so it's visible in the daily report — not silently
    dropped from the DB."""
    target = SqliteDataProvider(url=_sqlite_url(tmp_path))
    try:
        # Force a permanent error from upsert_units for one specific cid
        # by monkeypatching the store. This simulates "a row that the
        # clip helper couldn't save" — IntegrityError, FK violation, etc.
        original = target.unit_state.upsert_units

        def maybe_fail(cid: str, units: list[dict[str, Any]], rd: str) -> Any:
            if cid == "PROP-BAD":
                from sqlalchemy.exc import IntegrityError as _IE
                raise _IE("INSERT", {}, Exception("simulated FK violation"))
            return original(cid, units, rd)

        monkeypatch.setattr(target.unit_state, "upsert_units", maybe_fail)

        properties = [
            _make_property_payload("PROP-OK1"),
            _make_property_payload("PROP-BAD"),
            _make_property_payload("PROP-OK2"),
        ]
        with target.transaction():
            count = sync_run_to_pg._sync_current_state_from_snapshots(
                target, "2026-05-02", properties
            )
        assert count == 2, "good rows must commit even when one row fails"
        assert "PROP-OK1" in target.property_state.all_canonical_ids()
        assert "PROP-OK2" in target.property_state.all_canonical_ids()
        assert "PROP-BAD" not in target.property_state.all_canonical_ids()

        # Failure must be visible in run_issues — NOT silently swallowed.
        issues = list(target.runs.read_issues("2026-05-02"))
        sync_failures = [i for i in issues if i.code == "SYNC_PROPERTY_FAILED"]
        assert len(sync_failures) == 1
        assert sync_failures[0].canonical_id == "PROP-BAD"
        assert sync_failures[0].severity == "ERROR"
        assert "IntegrityError" in sync_failures[0].message
    finally:
        target.close()


def test_fallback_path_used_when_no_active_session(tmp_path: Path) -> None:
    """``_sync_current_state_from_snapshots`` must work against the FS
    provider too (used by the legacy backfill_pg.py path), where there
    is no SQL session to attach a SAVEPOINT to. The non-isolation
    fallback handles that case."""
    fs_target = FileSystemDataProvider(base_dir=tmp_path / "data", config_dir=tmp_path / "config")
    try:
        properties = [_make_property_payload("PROP-A"), _make_property_payload("PROP-B")]
        count = sync_run_to_pg._sync_current_state_from_snapshots(
            fs_target, "2026-05-02", properties
        )
        assert count == 2
        assert fs_target.property_state.exists("PROP-A")
        assert fs_target.property_state.exists("PROP-B")
    finally:
        fs_target.close()


# ─────────────────────────────────────────────────────────────────────────────
# Retention sweep — _apply_retention enforces a 3-day rolling window on
# every per-run table. Upsert-only tables (properties / units /
# scrape_profiles) must remain untouched.
# ─────────────────────────────────────────────────────────────────────────────


def test_apply_retention_drops_aged_rows_and_keeps_recent(tmp_path: Path) -> None:
    from datetime import UTC, date, datetime, timedelta

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from data_provider.sql.models import (
        DlqEntryRow,
        LlmDiagnosticRow,
        LlmPropertyDetailRow,
        LlmReportRow,
        PropertyRow,
        PropertySnapshotRow,
        RunIssueRow,
        RunLedgerRow,
        ScrapeEventRow,
        ScrapeProfileRow,
        UnitRow,
    )

    target = SqliteDataProvider(url=_sqlite_url(tmp_path))
    try:
        today = date.today().isoformat()
        old_date = (date.today() - timedelta(days=5)).isoformat()
        now = datetime.now(UTC).replace(tzinfo=None)
        old_ts = now - timedelta(days=5)
        old_iso = old_ts.isoformat()
        now_iso = now.isoformat()

        with Session(target.engine) as s:
            # run_date-keyed rows: today + aged
            for rd in (today, old_date):
                s.add(LlmReportRow(run_date=rd, payload={"calls": 1}, written_at=now))
                s.add(LlmPropertyDetailRow(run_date=rd, property_id="P1", payload={}, written_at=now))
                s.add(LlmDiagnosticRow(run_date=rd, property_id="P1", kind="trace", payload={}, written_at=now))
                s.add(PropertySnapshotRow(run_date=rd, canonical_id="P1", ordinal=0, payload={}))
                s.add(RunIssueRow(run_date=rd, seq=0, severity="INFO", code="OK", message="m", canonical_id="P1"))
                s.add(RunLedgerRow(run_date=rd, seq=0, canonical_id="P1", status="SUCCESS"))

            # scrape_events keyed by scrape_timestamp
            s.add(ScrapeEventRow(
                event_id="ev-new", property_id="P1", scrape_timestamp=now,
                scrape_outcome="SUCCESS",
            ))
            s.add(ScrapeEventRow(
                event_id="ev-old", property_id="P1", scrape_timestamp=old_ts,
                scrape_outcome="SUCCESS",
            ))

            # dlq_entries keyed by parked_at (ISO string)
            s.add(DlqEntryRow(
                property_id="P-NEW", parked_at=now_iso, reason="r",
                last_error_signature="", retry_at="", last_synced_at=now,
            ))
            s.add(DlqEntryRow(
                property_id="P-OLD", parked_at=old_iso, reason="r",
                last_error_signature="", retry_at="", last_synced_at=now,
            ))

            # Upsert-only tables — must survive untouched even though
            # the rows have no run_date / parked_at concept of recency.
            s.add(PropertyRow(canonical_id="P1", proj_name="Recent"))
            s.add(PropertyRow(canonical_id="P-OLD-PROP", proj_name="Long lived"))
            s.add(UnitRow(canonical_id="P1", unit_id="U1", rent_low=1000))
            s.add(ScrapeProfileRow(
                canonical_id="P1", version=1, schema_version="2",
                created_at=now, updated_at=now, updated_by="TEST", payload={},
            ))
            s.commit()

        deleted = sync_run_to_pg._apply_retention(target.engine)

        # Aged rows in the 3-day-window tables are gone.
        assert deleted["llm_reports"] == 1
        assert deleted["llm_property_details"] == 1
        assert deleted["llm_diagnostics"] == 1
        assert deleted["property_snapshots"] == 1
        assert deleted["run_issues"] == 1
        assert deleted["run_ledger"] == 1
        assert deleted["scrape_events"] == 1
        assert deleted["dlq_entries"] == 1

        with Session(target.engine) as s:
            # run_date tables: only today's row survives.
            for cls in (LlmReportRow, LlmPropertyDetailRow, LlmDiagnosticRow,
                        PropertySnapshotRow, RunIssueRow, RunLedgerRow):
                rows = list(s.execute(select(cls)).scalars())
                assert len(rows) == 1, f"{cls.__name__}: expected 1 row, got {len(rows)}"
                assert rows[0].run_date == today

            ev_ids = {r.event_id for r in s.execute(select(ScrapeEventRow)).scalars()}
            assert ev_ids == {"ev-new"}

            dlq_ids = {r.property_id for r in s.execute(select(DlqEntryRow)).scalars()}
            assert dlq_ids == {"P-NEW"}

            # Upsert-only tables: every row still present.
            prop_ids = {r.canonical_id for r in s.execute(select(PropertyRow)).scalars()}
            assert prop_ids == {"P1", "P-OLD-PROP"}
            unit_ids = {(r.canonical_id, r.unit_id) for r in s.execute(select(UnitRow)).scalars()}
            assert unit_ids == {("P1", "U1")}
            prof_ids = {r.canonical_id for r in s.execute(select(ScrapeProfileRow)).scalars()}
            assert prof_ids == {"P1"}
    finally:
        target.close()


def test_apply_retention_is_idempotent(tmp_path: Path) -> None:
    """Re-running on already-clean state must be a no-op (zero deletes)."""
    target = SqliteDataProvider(url=_sqlite_url(tmp_path))
    try:
        first = sync_run_to_pg._apply_retention(target.engine)
        # Fresh DB — nothing to delete.
        assert all(v == 0 for v in first.values()), first

        # Even with a row present, if it's recent the second call
        # deletes nothing.
        from datetime import UTC, date, datetime

        from sqlalchemy.orm import Session

        from data_provider.sql.models import LlmReportRow

        with Session(target.engine) as s:
            s.add(LlmReportRow(
                run_date=date.today().isoformat(),
                payload={},
                written_at=datetime.now(UTC).replace(tzinfo=None),
            ))
            s.commit()

        second = sync_run_to_pg._apply_retention(target.engine)
        assert second["llm_reports"] == 0

        third = sync_run_to_pg._apply_retention(target.engine)
        assert third == second
    finally:
        target.close()
