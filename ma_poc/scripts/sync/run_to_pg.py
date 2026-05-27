"""sync_run_to_pg.py — copy one shard's filesystem output into Postgres.

Invoked by ``jugnu_shard_entry.py`` after the scrape runner finishes
(including partial runs where some properties failed — prior versions
gated sync on ``runner_exit == 0`` which meant 359 out of 499 failing
left the DB completely empty). The production scrape pipeline writes
exclusively to the local filesystem (``/tmp/data/v2/runs/{date}/…``)
and relies on this module to land every artifact in Cloud SQL. Without
this step the Postgres tables stay empty regardless of the
``DATA_PROVIDER`` env var — the runner itself has no knowledge of the
data-provider layer.

Write surface (keep in sync with ``scripts/CLAUDE.md`` and
``data_provider/sql/models.py``):

  current state — derived from runs/{date}/properties.json via
  ``_sync_current_state_from_snapshots``. Jugnu runner never writes
  property_index.json/unit_index.json, so the legacy ``_copy_state``
  that reads from those files produces zero rows. Reading the run's
  properties.json instead is how we get ``properties`` + ``units``
  populated.
    - properties        via SqlPropertyStateStore.upsert
    - units             via SqlUnitStateStore.upsert_units

  per-run
    - runs              (registry row) via SqlRunStore.write_properties
    - property_snapshots via SqlRunStore.write_properties (shard-safe:
                        deletes only this batch's canonical_ids, not
                        ``WHERE run_date=X``; see SqlRunStore)
    - run_reports       via ``_merge_run_report`` — each shard writes its
                        partial report under extra.shards[shard_id]; the
                        top-level totals are recomputed as the sum over
                        all shards. Last-writer-wins on the top-level
                        would give us a 1/10 report when 10 shards run.
    - run_issues        derived per shard from per-property validation
                        results (``_meta.issues``) + the runner's
                        issues.jsonl. Idempotent by
                        (run_date, canonical_id).
    - run_ledger        derived per shard from properties.json — one
                        entry per property with verdict/units/url.
                        Idempotent by (run_date, canonical_id).

  audit + learning
    - scrape_events     derived per shard from properties.json — one
                        event per property. event_id is a stable hash
                        (cid + run_date) so retries upsert in place.
    - scrape_profiles   via SqlProfileStore.put (already written to FS
                        by the profile_updater).
    - extraction_results derived per shard from each property's
                        ``_extract_result`` field in properties.json.

  run artifacts (tables added in alembic 0003_artifacts)
    - property_reports       from runs/{date}/property_reports/{cid}.md
    - llm_reports            merge-on-write: payload.shards[shard_id]
                             holds this shard's aggregate; top-level
                             payload is recomputed from all shards.
    - llm_property_details   from runs/{date}/llm_report/{cid}.json
    - llm_diagnostics        from runs/{date}/llm_diagnostics/{cid}_{kind}.json

  durable cross-run state (table added in alembic 0006_dlq_entries)
    - dlq_entries            from state/dlq.jsonl (not per-run — ``state/``
                             lives above ``runs/`` and holds the global
                             retry queue, frontier, conditional cache).
                             Without this mirror the DLQ is lost every
                             time Cloud Run tears down the container.

Idempotent: every insert uses ``on_conflict_do_update`` on the table's
natural key. Re-running the sync for the same ``(run_date, shard_id)``
replaces rows in place — safe to invoke on Cloud Run retry.

Multi-shard safe: every delete-before-insert operation is scoped to
either (a) this batch's canonical_ids (property_snapshots, run_issues,
run_ledger, scrape_events for this shard) or (b) this shard's
contribution slot in an extra JSON blob (run_reports, llm_reports).
Concurrent shards writing to the same run_date never clobber each other.

Never raises unexpectedly. SQL errors bubble up so the caller (shard_entry)
can surface them as shard failures — that's the desired behaviour: DB
is the destination of record and silent sync failures are exactly how
prod shipped at 0 rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

_MA_POC_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_MA_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_MA_POC_ROOT))

from data_provider import (  # noqa: E402
    DataProvider,
    FileSystemDataProvider,
    IssueEntry,
    LedgerEntry,
    PostgresDataProvider,
)
from data_provider.sql.provider import SqlDataProvider  # noqa: E402
from data_provider.sql.engine import dialect_insert  # noqa: E402
from data_provider.sql.resilience import (  # noqa: E402
    PERMANENT_ROW_ERRORS,
    TRANSIENT_DB_ERRORS,
    isolate_row_writes,
    with_db_retry,
)
from data_provider.sql.models import (  # noqa: E402
    DlqEntryRow,
    LlmDiagnosticRow,
    LlmPropertyDetailRow,
    LlmReportRow,
    PropertyReportRow,
    RunIssueRow,
    RunLedgerRow,
    RunReportRow,
    RunRow,
    ScrapeEventRow,
)
from models.extraction_result import (  # noqa: E402
    ExtractionResult,
    ExtractionStatus,
    ExtractionTier,
)
from models.scrape_event import ScrapeEvent, ScrapeOutcome  # noqa: E402

log = logging.getLogger("sync_run_to_pg")

# ``{canonical_id}_{kind}.json`` — ids can contain digits but not ``_``
# in practice; we split on the first underscore.
_DIAG_NAME = re.compile(r"^(?P<cid>[^_]+)_(?P<kind>.+)\.json$")


# ── Reusable copies from FS provider → SQL provider ──────────────────────────


def _copy_state(src: DataProvider, dst: DataProvider) -> int:
    """Copy properties + units. Returns number of canonical_ids touched."""
    ids = sorted(src.property_state.all_canonical_ids())
    if not ids:
        return 0
    for cid in ids:
        entry = src.property_state.get(cid)
        if entry is None:
            continue
        snap = entry.model_dump(exclude={"canonical_id"})
        # Mirror backfill_pg: use last_seen_at's date part, fall back to
        # first_seen_date. Gives the SQL row the same provenance as FS.
        seen_at_date = (entry.last_seen_at or "")[:10]
        run_date = seen_at_date or entry.first_seen_date or "sync"
        dst.property_state.upsert(cid, snap, run_date)

        units = src.unit_state.get_units(cid)
        if units:
            units_list = [u.model_dump() for u in units.values()]
            dst.unit_state.upsert_units(cid, units_list, run_date)
    return len(ids)


def _copy_profiles(src: DataProvider, dst: DataProvider) -> int:
    ids = src.profiles.list_ids()
    for cid in ids:
        prof = src.profiles.get(cid)
        if prof is not None:
            dst.profiles.put(prof)
    return len(ids)


def _copy_events(src: DataProvider, dst: DataProvider) -> int:
    count = 0
    for ev in src.scrape_events.read_all():
        dst.scrape_events.append(ev)
        count += 1
    return count


def _copy_run(
    src: DataProvider,
    dst: DataProvider,
    run_date: str,
    shard_id: str | None = None,
) -> dict[str, int]:
    """Copy one run_date's properties, issues, ledger via the store contracts.

    Runs inside the provider's shared-session transaction. The report is
    handled separately in Stage 2 (``_merge_run_report``) because its
    merge path uses ``engine.begin()`` with ``SELECT … FOR UPDATE`` and
    nesting that inside the session triggers "database is locked" on
    SQLite.

    Multi-shard safe:

    - ``write_properties`` deletes only this batch's canonical_ids
      (see SqlRunStore.write_properties), so shards don't clobber each
      other.
    - Issues / ledger are scoped to this batch's canonical_ids —
      existing rows for those cids are deleted via the shared session
      before re-append so a shard retry is idempotent.
    """
    out = {"properties": 0, "issues": 0, "ledger": 0}
    props = src.runs.read_properties(run_date)
    my_cids: set[str] = set()
    if props:
        dst.runs.write_properties(run_date, props)
        out["properties"] = len(props)
        my_cids = _collect_canonical_ids(props)

    # Delete-then-append pattern scoped to this batch's cids so shard
    # retries don't duplicate, but OTHER shards' rows stay put. Uses
    # the shared session (not engine.begin()) so it commits atomically
    # with the rest of Stage 1.
    if my_cids and isinstance(dst, SqlDataProvider):
        _delete_run_rows_in_session(dst, run_date, my_cids)
    for issue in src.runs.read_issues(run_date):
        dst.runs.append_issue(run_date, issue)
        out["issues"] += 1
    for entry in src.runs.read_ledger(run_date):
        dst.runs.append_ledger_entry(run_date, entry)
        out["ledger"] += 1
    return out


def _delete_run_rows_in_session(
    dst: SqlDataProvider,
    run_date: str,
    cids: set[str],
) -> None:
    """Delete run_issues + run_ledger rows for ``cids`` using the shared session.

    Must be called from INSIDE ``dst.transaction()`` — uses the
    session the provider has bound to its _SessionHolder so the delete
    commits atomically with the following append_issue / append_ledger
    calls, and does NOT open a second DB connection (SQLite would
    deadlock).
    """
    from sqlalchemy import delete

    session = dst._holder.active  # type: ignore[attr-defined]
    if session is None:  # defensive — caller must be inside a txn
        return
    session.execute(
        delete(RunIssueRow).where(
            RunIssueRow.run_date == run_date,
            RunIssueRow.canonical_id.in_(cids),
        )
    )
    session.execute(
        delete(RunLedgerRow).where(
            RunLedgerRow.run_date == run_date,
            RunLedgerRow.canonical_id.in_(cids),
        )
    )


def _collect_canonical_ids(properties: Iterable[dict[str, Any]]) -> set[str]:
    """Pull canonical_ids out of a properties-list payload (best-effort)."""
    out: set[str] = set()
    for p in properties:
        cid = _extract_canonical_id(p)
        if cid:
            out.add(cid)
    return out


def _extract_canonical_id(p: dict[str, Any]) -> str | None:
    """Mirrors SqlRunStore._extract_canonical_id. Kept local so the sync
    layer doesn't reach into a private SQL-store method."""
    meta = p.get("_meta") or {}
    for key in ("canonical_id", "Unique ID", "unique_id", "Property ID", "property_id"):
        v = meta.get(key) if isinstance(meta, dict) else None
        if v:
            return str(v)
        v = p.get(key)
        if v:
            return str(v)
    return None


def _single_writer_report(dst: DataProvider, run_date: str, report: Any) -> None:
    """Legacy single-writer report path (replace semantics).

    Used by CLI re-sync / ``backfill_pg.py`` where there is no shard
    concurrency. Each call fully replaces the run_reports row.
    """
    # Write through the store contract; opens a fresh session
    # implicitly via ``SqlRunStore.scope``.
    dst.runs.write_report(run_date, report)


# ── Per-shard merge: run_reports ─────────────────────────────────────────────


def _ensure_run_row(engine: Any, run_date: str) -> None:
    """Ensure a row exists in ``runs`` so the registry lists this run_date."""
    from sqlalchemy import select

    with engine.begin() as conn:
        exists = conn.execute(select(RunRow.run_date).where(RunRow.run_date == run_date)).scalar()
        if exists is None:
            conn.execute(RunRow.__table__.insert().values(run_date=run_date))


def _merge_run_report(engine: Any, run_date: str, shard_id: str, report: Any) -> None:
    """Merge this shard's report into ``run_reports`` atomically.

    Each shard records its partial report under ``extra.shards[shard_id]``.
    The top-level ``totals`` / ``tier_distribution`` / ``cost`` /
    ``slo_violations`` columns are recomputed as the aggregation across
    every shard's contribution. Shard retries overwrite their own entry
    in place — the aggregate stays consistent with the union of live
    shards.

    Uses ``SELECT … FOR UPDATE`` (Postgres) to serialise concurrent
    shards writing to the same run_date. SQLite serialises all writes
    via its file lock, so FOR UPDATE is a no-op there and this still
    works correctly in tests.
    """
    from sqlalchemy import select

    _ensure_run_row(engine, run_date)

    body = report.model_dump(mode="json") if hasattr(report, "model_dump") else dict(report)
    this_shard = {
        "generated_at": body.get("generated_at"),
        "totals": body.get("totals") or {},
        "tier_distribution": body.get("tier_distribution") or {},
        "cost": body.get("cost") or {},
        "slo_violations": body.get("slo_violations") or [],
        # Preserve any extra keys the runner added (non_extracted_fields,
        # pre_extraction_terminations, etc.) so the TS frontend can
        # render them per-shard.
        "extra": {
            k: v
            for k, v in body.items()
            if k
            not in {
                "run_date",
                "generated_at",
                "totals",
                "tier_distribution",
                "cost",
                "slo_violations",
            }
        },
    }

    # 1) Make sure a row exists so the SELECT FOR UPDATE has something
    #    to lock. ON CONFLICT DO NOTHING = no-op if another shard got
    #    there first.
    insert_stmt = dialect_insert(engine, RunReportRow).values(
        run_date=run_date,
        generated_at=body.get("generated_at") or datetime.now(UTC).isoformat(),
        totals={},
        tier_distribution={},
        cost={},
        slo_violations=[],
        extra={"shards": {}},
    )
    insert_stmt = insert_stmt.on_conflict_do_nothing(index_elements=[RunReportRow.run_date])

    with engine.begin() as conn:
        conn.execute(insert_stmt)
        # 2) Lock the row for concurrent-shard safety (Postgres). SQLite
        #    ignores FOR UPDATE but serialises via its DB-level write lock.
        #    We use a Core select of the columns we need (not
        #    ``select(RunReportRow)``) because ``conn.execute(...)`` on a
        #    raw Connection doesn't hydrate ORM objects — it returns
        #    ``Row`` tuples, so we pull columns explicitly.
        row_stmt = select(
            RunReportRow.generated_at,
            RunReportRow.extra,
        ).where(RunReportRow.run_date == run_date)
        if engine.dialect.name == "postgresql":
            row_stmt = row_stmt.with_for_update()
        existing = conn.execute(row_stmt).one()
        existing_generated_at = existing[0]
        existing_extra = existing[1]

        # 3) Record this shard's contribution and recompute aggregate.
        extra = dict(existing_extra or {})
        shards = dict(extra.get("shards") or {})
        shards[str(shard_id)] = this_shard
        extra["shards"] = shards

        merged_totals = _sum_totals([s["totals"] for s in shards.values()])
        merged_tier = _sum_counters([s["tier_distribution"] for s in shards.values()])
        merged_cost = _sum_cost([s["cost"] for s in shards.values()])
        merged_slo = _concat_unique([s["slo_violations"] for s in shards.values()])

        # Promote the most recent shard's extra (non-standard keys) up
        # to the row's ``extra`` so readers that don't understand
        # ``shards`` still get something meaningful.
        for shard_extra in [s.get("extra") or {} for s in shards.values()]:
            for k, v in shard_extra.items():
                extra.setdefault(k, v)

        conn.execute(
            RunReportRow.__table__.update()
            .where(RunReportRow.run_date == run_date)
            .values(
                generated_at=this_shard["generated_at"] or existing_generated_at,
                totals=merged_totals,
                tier_distribution=merged_tier,
                cost=merged_cost,
                slo_violations=merged_slo,
                extra=extra,
            )
        )


def _sum_totals(dicts: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Sum numeric fields of RunSummary across shards; recompute success rate."""
    out = {
        "properties": 0,
        "succeeded": 0,
        "failed": 0,
        "carry_forward": 0,
        "success_rate_pct": 0.0,
    }
    for d in dicts:
        if not isinstance(d, dict):
            continue
        for k in ("properties", "succeeded", "failed", "carry_forward"):
            v = d.get(k) or 0
            try:
                out[k] += int(v)
            except (TypeError, ValueError):
                continue
    if out["properties"] > 0:
        out["success_rate_pct"] = round(
            100.0 * out["succeeded"] / out["properties"], 2
        )
    return out


def _sum_counters(dicts: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Sum per-key counts across shards (e.g. tier_distribution)."""
    out: dict[str, int] = {}
    for d in dicts:
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            try:
                out[k] = out.get(k, 0) + int(v or 0)
            except (TypeError, ValueError):
                continue
    return out


def _sum_cost(dicts: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Sum floating-point per-category costs across shards."""
    out: dict[str, float] = {}
    for d in dicts:
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            try:
                out[k] = out.get(k, 0.0) + float(v or 0.0)
            except (TypeError, ValueError):
                continue
    return out


def _concat_unique(lists: Iterable[list[Any]]) -> list[Any]:
    """Concatenate lists dropping dict duplicates (best-effort JSON key)."""
    out: list[Any] = []
    seen: set[str] = set()
    for lst in lists:
        if not isinstance(lst, list):
            continue
        for item in lst:
            try:
                key = json.dumps(item, sort_keys=True, default=str)
            except (TypeError, ValueError):
                out.append(item)
                continue
            if key not in seen:
                seen.add(key)
                out.append(item)
    return out


# ── Derive-from-snapshots: current state + run_ledger + scrape_events + extraction_results ──


_RUN_LEDGER_STATUS_FALLBACK = "UNKNOWN"


def _tier_str_to_int(tier: Any) -> int | None:
    """Map a tier string like ``TIER_4_LLM_DOM`` / ``generic:llm_api_targeted``
    back to a 1..5 ExtractionTier. Used to populate the scrape_events +
    extraction_results ``tier`` int column.
    """
    if tier is None:
        return None
    s = str(tier).upper()
    # Exact "TIER_N_…"
    m = re.match(r"^TIER_(\d)_", s)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    # Generic sub-tier keys (jugnu GenericAdapter)
    if "API" in s:
        return ExtractionTier.API_INTERCEPTION.value
    if "JSONLD" in s or "JSON_LD" in s:
        return ExtractionTier.JSON_LD.value
    if "DOM" in s or "TEMPLATE" in s or "TPL" in s:
        return ExtractionTier.PLAYWRIGHT_TPL.value
    if "LLM" in s:
        return ExtractionTier.LLM_GPT4O_MINI.value
    if "VISION" in s:
        return ExtractionTier.VISION_FALLBACK.value
    return None


def _meta_to_outcome(meta: dict[str, Any]) -> ScrapeOutcome:
    """Map ``_meta`` to the audit-log ``ScrapeOutcome`` enum.

    Reads verdict when present; falls back to ``scrape_tier_used == "FAILED"``
    so the crash-catcher path in ``jugnu_runner._make_failed_record``
    (which never sets verdict) still surfaces as FAILED in
    ``scrape_events.scrape_outcome`` instead of silently defaulting to
    something neutral.
    """
    verdict = meta.get("verdict")
    if verdict is not None:
        s = str(verdict).upper()
        if s == "SUCCESS_NO_AVAILABILITY":
            # 2026-05-27: surface operator-transparent zero-inventory
            # state distinctly in the audit log so the failure_rate
            # signal isn't inflated by waitlist / fully-leased properties.
            return ScrapeOutcome.SUCCESS_NO_INVENTORY
        if s.startswith("SUCCESS"):
            return ScrapeOutcome.SUCCESS
        if s.startswith("CARRY") or s == "SKIPPED":
            return ScrapeOutcome.SKIPPED
        if s == "PARTIAL":
            return ScrapeOutcome.PARTIAL
        return ScrapeOutcome.FAILED
    # No verdict — classify off tier_used (crash path).
    if _is_failure_meta(meta):
        return ScrapeOutcome.FAILED
    return ScrapeOutcome.FAILED  # Conservative default; no verdict == no scrape evidence.


def _is_failure_meta(meta: dict[str, Any]) -> bool:
    """True when ``_meta`` represents a failure, irrespective of which
    signal the runner used to mark it.

    Two signals reach the sync:

    - ``_meta.verdict == "FAILED_*"`` — the normal Jugnu path via
      ``reporting.verdict.compute``.
    - ``_meta.scrape_tier_used == "FAILED"`` — the crash-catcher path in
      ``jugnu_runner._make_failed_record`` (property raised an exception
      before verdict could be computed). That path does NOT set verdict,
      so checking verdict alone silently drops these from the issues +
      event synthesis.
    """
    verdict = str(meta.get("verdict") or "").upper()
    if verdict.startswith("FAIL"):
        return True
    tier_used = str(meta.get("scrape_tier_used") or "").upper()
    return tier_used == "FAILED"


def _stable_event_id(property_id: str, run_date: str) -> str:
    """Deterministic UUID4-format string for (property_id, run_date).

    Stable across retries so the scrape_events upsert by event_id stays
    idempotent. Uses sha256 (never the built-in ``hash()`` — non-
    deterministic across Python processes — see CLAUDE.md bug-hunt #15).
    """
    digest = hashlib.sha256(f"{property_id}|{run_date}".encode()).hexdigest()
    # Shape as UUID4 so downstream consumers that assume UUID parsing work.
    # We only use the first 32 hex chars; version nibble + variant bits
    # are stamped per RFC 4122 for UUIDv4 semantics.
    hex32 = digest[:32]
    return str(uuid.UUID(hex=hex32, version=4))


def _sync_current_state_from_snapshots(
    dst: DataProvider,
    run_date: str,
    properties: list[dict[str, Any]],
) -> int:
    """Populate ``properties`` + ``units`` tables from properties.json.

    Jugnu runner does not write ``property_index.json`` / ``unit_index.json``
    — those are legacy StateStore files. Without this step the current-
    state tables (which the TS frontend reads directly) stay empty even
    when property_snapshots has data. Returns the number of canonical_ids
    upserted.

    Per-property writes are isolated via SAVEPOINTs (see
    ``data_provider.sql.resilience.isolate_row_writes``) so a single bad
    row (e.g. an over-length string from a misbehaving extractor) can no
    longer roll back the entire shard. Failures are recorded in
    ``run_issues`` with code ``SYNC_PROPERTY_FAILED`` so they're visible
    in the daily report instead of silently dropped.

    The 2026-04-29 → 05-01 incident: a 455-char marketing blurb in
    ``floor_plan_name`` (declared ``String(256)``) raised ``DataError``
    inside the shared shard transaction; everything for shard 0 — 499
    properties × 3 days — rolled back. ``isolate_row_writes`` makes this
    a per-row data-quality warning instead of a shard-wide outage.
    """
    sql_holder = getattr(dst, "_holder", None)
    session = sql_holder.active if sql_holder else None
    if session is None:
        # Defensive: callers always wrap us in dst.transaction() so this
        # branch is the FS provider / a misuse. Fall back to non-isolated
        # writes for that case (FS provider has no rollback semantics).
        return _sync_current_state_no_isolation(dst, run_date, properties)

    eligible: list[tuple[str, dict[str, Any]]] = []
    for p in properties:
        cid = _extract_canonical_id(p)
        if cid:
            eligible.append((cid, p))

    def _apply(item: tuple[str, dict[str, Any]]) -> None:
        cid, p = item
        snap = {
            "apartment_id": p.get("apartment_id"),
            "proj_name": p.get("proj_name") or p.get("Property Name"),
            "address": p.get("address") or p.get("Property Address"),
            "city": p.get("city") or p.get("City"),
            "state": p.get("state") or p.get("State"),
            "zip_code": p.get("zip_code") or p.get("ZIP Code"),
            "country": p.get("country"),
            "phone": p.get("phone") or p.get("Phone"),
            "email_address": p.get("email_address"),
            "website": p.get("website") or p.get("Website"),
            "pmc": p.get("pmc") or p.get("Management Company"),
            "website_design": p.get("website_design"),
            "concessions": p.get("concessions"),
        }
        meta = p.get("_meta") or {}
        verdict = meta.get("verdict")
        units = p.get("units") or []
        snap["last_scrape_status"] = verdict or meta.get("scrape_tier_used")
        snap["last_units_count"] = len(units)
        dst.property_state.upsert(cid, snap, run_date)
        if units:
            dst.unit_state.upsert_units(cid, units, run_date)

    success, failures = isolate_row_writes(
        session,
        eligible,
        _apply,
        label_for=lambda item: f"cid={item[0]}",
    )

    # Surface skipped rows as run_issues so they show up in the report.
    # Done outside the per-row savepoint because we want these issue
    # rows themselves to commit even if some of them fail validation.
    for label, exc in failures:
        try:
            cid = label.split("=", 1)[1] if "=" in label else None
            dst.runs.append_issue(
                run_date,
                IssueEntry(
                    severity="ERROR",
                    code="SYNC_PROPERTY_FAILED",
                    message=f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}",
                    canonical_id=cid,
                    details={"phase": "sync_current_state"},
                ),
            )
        except Exception:  # noqa: BLE001 — issue logging must never fail the sync
            log.exception("Failed to log sync failure issue for %s", label)

    return success


def _sync_current_state_no_isolation(
    dst: DataProvider,
    run_date: str,
    properties: list[dict[str, Any]],
) -> int:
    """Fallback for the FS provider (no SAVEPOINT support).

    Behaves like the original loop. Kept separate so the SQL path's
    isolation wiring stays clean and so unit tests can target either
    branch directly.
    """
    count = 0
    for p in properties:
        cid = _extract_canonical_id(p)
        if not cid:
            continue
        snap = {
            "apartment_id": p.get("apartment_id"),
            "proj_name": p.get("proj_name") or p.get("Property Name"),
            "address": p.get("address") or p.get("Property Address"),
            "city": p.get("city") or p.get("City"),
            "state": p.get("state") or p.get("State"),
            "zip_code": p.get("zip_code") or p.get("ZIP Code"),
            "country": p.get("country"),
            "phone": p.get("phone") or p.get("Phone"),
            "email_address": p.get("email_address"),
            "website": p.get("website") or p.get("Website"),
            "pmc": p.get("pmc") or p.get("Management Company"),
            "website_design": p.get("website_design"),
            "concessions": p.get("concessions"),
        }
        meta = p.get("_meta") or {}
        verdict = meta.get("verdict")
        units = p.get("units") or []
        snap["last_scrape_status"] = verdict or meta.get("scrape_tier_used")
        snap["last_units_count"] = len(units)
        dst.property_state.upsert(cid, snap, run_date)
        if units:
            dst.unit_state.upsert_units(cid, units, run_date)
        count += 1
    return count


def _sync_run_ledger_from_snapshots(
    dst: DataProvider,
    run_date: str,
    properties: list[dict[str, Any]],
) -> int:
    """Append one LedgerEntry per property in this shard's properties.json.

    Jugnu runner doesn't write ledger.jsonl; the TS frontend reads
    run_ledger (``PgRunStore.readLedger``). Without this the per-property
    ledger view is empty. Idempotent: ``_copy_run`` deletes existing
    entries for this batch's cids before appending.
    """
    count = 0
    for idx, p in enumerate(properties):
        cid = _extract_canonical_id(p)
        meta = p.get("_meta") or {}
        verdict = str(meta.get("verdict") or "").upper()
        units_count = len(p.get("units") or [])
        url = (
            p.get("website")
            or p.get("Website")
            or meta.get("url")
            or meta.get("scrape_url")
        )
        errors = meta.get("scrape_errors") or meta.get("errors") or []
        failed = _is_failure_meta(meta)
        status = verdict or ("FAILED" if failed else _RUN_LEDGER_STATUS_FALLBACK)
        entry = LedgerEntry(
            canonical_id=cid,
            row_index=idx,
            status=status,
            units_count=units_count,
            carry_forward_used=bool(meta.get("carry_forward_used")),
            scrape_failed=failed,
            error_count=len(errors) if isinstance(errors, list) else None,
            url=str(url) if url else None,
            timestamp=meta.get("scrape_timestamp"),
        )
        dst.runs.append_ledger_entry(run_date, entry)
        count += 1
    return count


def _sync_run_issues_from_snapshots(
    dst: DataProvider,
    run_date: str,
    properties: list[dict[str, Any]],
) -> int:
    """Append IssueEntry rows for per-property validation failures.

    Reads two shapes: explicit ``_meta.issues`` list (one dict per issue)
    and the L4 validation ``_validated.rejected`` / ``_validated.flagged``
    arrays. Both are emitted by jugnu_runner but never persisted to
    issues.jsonl before this sync. Without this the ``run_issues`` table
    stays empty and the TS frontend can't show validation errors.
    """
    count = 0
    for p in properties:
        cid = _extract_canonical_id(p)
        meta = p.get("_meta") or {}
        # 1) Explicit issues list from the runner / adapter
        for iss in meta.get("issues") or []:
            if not isinstance(iss, dict):
                continue
            entry = IssueEntry(
                severity=str(iss.get("severity", "INFO")).upper(),
                code=str(iss.get("code", "RUNNER_ISSUE")),
                message=str(iss.get("message", "")),
                canonical_id=cid,
                details=iss.get("details"),
                timestamp=iss.get("timestamp"),
            )
            dst.runs.append_issue(run_date, entry)
            count += 1

        # 2) L4 validation rejections / flags
        validated = p.get("_validated") or {}
        for rej in validated.get("rejected") or []:
            msg = rej.get("reason") if isinstance(rej, dict) else str(rej)
            entry = IssueEntry(
                severity="ERROR",
                code="VALIDATION_REJECTED",
                message=str(msg)[:500] if msg else "rejected by validator",
                canonical_id=cid,
            )
            dst.runs.append_issue(run_date, entry)
            count += 1
        for fl in validated.get("flagged") or []:
            msg = fl.get("flag") if isinstance(fl, dict) else str(fl)
            entry = IssueEntry(
                severity="WARNING",
                code="VALIDATION_FLAGGED",
                message=str(msg)[:500] if msg else "flagged by validator",
                canonical_id=cid,
            )
            dst.runs.append_issue(run_date, entry)
            count += 1

        # 3) Synthetic FAILURE issue when either verdict is FAILED_* OR
        #    scrape_tier_used == "FAILED" (the exception-catcher path in
        #    jugnu_runner._make_failed_record doesn't set verdict). The
        #    issues view needs a row per failure even when the adapter
        #    didn't emit one explicitly.
        if _is_failure_meta(meta):
            errors = meta.get("scrape_errors") or meta.get("errors") or []
            verdict = str(meta.get("verdict") or "").upper()
            code = verdict or "FAILED_CRASH"
            msg = errors[0] if errors else code
            entry = IssueEntry(
                severity="ERROR",
                code=code,
                message=str(msg)[:500],
                canonical_id=cid,
            )
            dst.runs.append_issue(run_date, entry)
            count += 1
    return count


def _sync_scrape_events_from_snapshots(
    dst: DataProvider,
    run_date: str,
    properties: list[dict[str, Any]],
) -> int:
    """Upsert one ScrapeEvent per property from properties.json.

    Jugnu writes a per-run observability ledger (``events.jsonl``, 28
    event kinds) but never the legacy ``ScrapeEvent`` schema. The
    ``scrape_events`` audit table was therefore empty. This derives one
    audit event per (canonical_id, run_date) so downstream analytics +
    validate_outputs.py can compute tier distributions, confidence
    histograms, etc. against Postgres instead of raw GCS JSONL.

    event_id is a stable hash of (property_id, run_date) so a shard
    retry upserts in place.
    """
    count = 0
    for p in properties:
        cid = _extract_canonical_id(p)
        if not cid:
            continue
        meta = p.get("_meta") or {}
        extract = p.get("_extract_result") or {}
        tier_str = extract.get("tier_used") if isinstance(extract, dict) else None
        event = ScrapeEvent(
            event_id=_stable_event_id(cid, run_date),
            property_id=cid,
            scrape_timestamp=datetime.now(UTC),
            extraction_tier=_tier_str_to_int(tier_str or meta.get("scrape_tier_used")),
            scrape_outcome=_meta_to_outcome(meta),
            failure_reason=(
                ";".join(str(e) for e in (meta.get("scrape_errors") or meta.get("errors") or []))[:2000]
                or None
            ),
            page_load_ms=meta.get("page_load_ms"),
            proxy_used=bool(meta.get("proxy_used")),
            proxy_provider=meta.get("proxy_provider"),
            confidence_score=meta.get("confidence") or meta.get("confidence_score"),
        )
        dst.scrape_events.append(event)
        count += 1
    return count


def _sync_extraction_results_from_snapshots(
    dst: DataProvider,
    run_date: str,
    properties: list[dict[str, Any]],
) -> int:
    """Write one ExtractionResult per property from its ``_extract_result``.

    Upsert keyed on (run_date, property_id). Without this the
    ``extraction_results`` table stays empty and backfill / audit
    tooling has no per-property tier + cost record to query.
    """
    count = 0
    for p in properties:
        cid = _extract_canonical_id(p)
        if not cid:
            continue
        extract = p.get("_extract_result") or {}
        if not isinstance(extract, dict):
            # Dataclass or similar — best-effort dict coerce
            extract = {
                "tier_used": getattr(extract, "tier_used", None),
                "llm_cost_usd": getattr(extract, "llm_cost_usd", 0.0),
            }
        meta = p.get("_meta") or {}
        verdict = str(meta.get("verdict") or "").upper()
        if verdict.startswith("SUCCESS"):
            status = ExtractionStatus.SUCCESS
        elif verdict == "SKIPPED" or verdict.startswith("CARRY"):
            status = ExtractionStatus.SKIPPED
        elif verdict.startswith("FAIL") or _is_failure_meta(meta):
            status = ExtractionStatus.FAILED
        else:
            # No verdict, no FAILED tier signal — a Jugnu path that
            # completed without classifying. Conservative: treat
            # absence-of-signal as FAILED so the row is still logged.
            status = ExtractionStatus.FAILED

        tier_int = _tier_str_to_int(extract.get("tier_used") or meta.get("scrape_tier_used"))
        tier_enum = ExtractionTier(tier_int) if tier_int in {1, 2, 3, 4, 5} else None
        try:
            confidence = float(meta.get("confidence") or meta.get("confidence_score") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(confidence, 1.0))
        result = ExtractionResult(
            property_id=cid,
            tier=tier_enum,
            status=status,
            confidence_score=confidence,
            raw_fields={
                "tier_used": extract.get("tier_used"),
                "llm_cost_usd": extract.get("llm_cost_usd"),
                "units_count": len(p.get("units") or []),
            },
            error_message=(
                "; ".join(str(e) for e in (meta.get("scrape_errors") or meta.get("errors") or []))[:2000]
                or None
            ),
        )
        dst.extraction_results.write(run_date, result)
        count += 1
    return count


def _copy_extractions(
    src: DataProvider,
    dst: DataProvider,
    run_date: str,
    canonical_ids: Iterable[str],
) -> int:
    count = 0
    for cid in canonical_ids:
        res = src.extraction_results.read(run_date, cid)
        if res is None:
            continue
        dst.extraction_results.write(run_date, res)
        count += 1
    return count


# ── Artifact tables (no store interface yet — use engine directly) ───────────


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("skipping %s: %s", path.name, exc)
        return None


def _upsert_property_reports(engine: Any, run_date: str, dir_: Path) -> int:
    if not dir_.exists():
        return 0
    files = [p for p in dir_.glob("*.md") if p.is_file()]
    if not files:
        return 0
    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as conn:
        for path in files:
            stmt = dialect_insert(engine, PropertyReportRow).values(
                run_date=run_date,
                canonical_id=path.stem,
                markdown=path.read_text(encoding="utf-8"),
                written_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    PropertyReportRow.run_date,
                    PropertyReportRow.canonical_id,
                ],
                set_={
                    "markdown": stmt.excluded.markdown,
                    "written_at": stmt.excluded.written_at,
                },
            )
            conn.execute(stmt)
    return len(files)


def _upsert_llm_report(
    engine: Any,
    run_date: str,
    path: Path,
    shard_id: str | None = None,
) -> int:
    """Merge this shard's llm_report.json into the ``llm_reports`` row.

    When ``shard_id`` is provided (Cloud Run multi-shard path), each
    shard's payload is stored under ``payload.shards[shard_id]`` and the
    top-level payload keys are recomputed as an aggregate so the TS
    frontend still sees a single merged report:

    - ``calls`` / ``total_tokens_in`` / ``total_tokens_out`` — summed
    - ``total_cost_usd`` — summed
    - ``by_property`` — shallow-merged (per-property records don't
      overlap across shards because CSV slices are disjoint by cid)

    Without this merge, 10 shards writing to the same run_date would
    keep only the last shard's report (1/10 of the data).

    Without ``shard_id`` (legacy single-writer path, e.g. backfill_pg),
    falls back to the previous on_conflict_do_update replace semantics.
    """
    if not path.exists():
        return 0
    payload = _load_json(path)
    if payload is None:
        return 0
    if not isinstance(payload, dict):
        # Defensive: the writer always produces an object, but if a run
        # ever emits a top-level list we still upsert a wrapper so the
        # JSON column constraint doesn't trip.
        payload = {"raw": payload}
    now = datetime.now(UTC).replace(tzinfo=None)

    if shard_id is None:
        # Legacy single-writer path — preserves prior behaviour for
        # backfill_pg.py and manual CLI re-syncs.
        with engine.begin() as conn:
            stmt = dialect_insert(engine, LlmReportRow).values(
                run_date=run_date,
                payload=payload,
                written_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[LlmReportRow.run_date],
                set_={
                    "payload": stmt.excluded.payload,
                    "written_at": stmt.excluded.written_at,
                },
            )
            conn.execute(stmt)
        return 1

    # Shard-aware merge (Cloud Run multi-shard path).
    from sqlalchemy import select

    insert_stmt = dialect_insert(engine, LlmReportRow).values(
        run_date=run_date,
        payload={"shards": {}},
        written_at=now,
    )
    insert_stmt = insert_stmt.on_conflict_do_nothing(
        index_elements=[LlmReportRow.run_date]
    )

    with engine.begin() as conn:
        conn.execute(insert_stmt)
        row_stmt = select(LlmReportRow.payload).where(LlmReportRow.run_date == run_date)
        if engine.dialect.name == "postgresql":
            row_stmt = row_stmt.with_for_update()
        existing_payload = conn.execute(row_stmt).scalar_one()

        body = dict(existing_payload or {})
        shards = dict(body.get("shards") or {})
        shards[str(shard_id)] = payload
        body["shards"] = shards

        # Recompute aggregates across shards. Keys here mirror the
        # shape produced by ma_poc.llm.interaction_logger.write_run_summary.
        body["calls"] = sum(
            _coerce_int(s.get("calls")) for s in shards.values()
        )
        body["total_cost_usd"] = sum(
            _coerce_float(s.get("total_cost_usd")) for s in shards.values()
        )
        body["total_tokens_in"] = sum(
            _coerce_int(s.get("total_tokens_in")) for s in shards.values()
        )
        body["total_tokens_out"] = sum(
            _coerce_int(s.get("total_tokens_out")) for s in shards.values()
        )
        merged_props: dict[str, Any] = {}
        for s_payload in shards.values():
            by_prop = s_payload.get("by_property") if isinstance(s_payload, dict) else None
            if isinstance(by_prop, dict):
                merged_props.update(by_prop)
        if merged_props:
            body["by_property"] = merged_props

        conn.execute(
            LlmReportRow.__table__.update()
            .where(LlmReportRow.run_date == run_date)
            .values(payload=body, written_at=now)
        )
    return 1


def _coerce_int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _coerce_float(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _upsert_llm_property_details(engine: Any, run_date: str, dir_: Path) -> int:
    if not dir_.exists():
        return 0
    files = [p for p in dir_.glob("*.json") if p.is_file()]
    if not files:
        return 0
    now = datetime.now(UTC).replace(tzinfo=None)
    count = 0
    with engine.begin() as conn:
        for path in files:
            payload = _load_json(path)
            if payload is None:
                continue
            stmt = dialect_insert(engine, LlmPropertyDetailRow).values(
                run_date=run_date,
                property_id=path.stem,
                payload=payload,
                written_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    LlmPropertyDetailRow.run_date,
                    LlmPropertyDetailRow.property_id,
                ],
                set_={
                    "payload": stmt.excluded.payload,
                    "written_at": stmt.excluded.written_at,
                },
            )
            conn.execute(stmt)
            count += 1
    return count


# ── DLQ (cross-run, not per-run) ─────────────────────────────────────────────


def _load_dlq_live_entries(path: Path) -> dict[str, dict[str, Any]]:
    """Read ``state/dlq.jsonl`` and return the post-compaction live state.

    The JSONL file is an append-only log with ``unparked`` tombstones —
    re-parking a property adds a new line, unparking writes a tombstone.
    We walk every line in order and keep only the latest non-unparked
    entry per property_id, matching ``Dlq._load()`` exactly.
    """
    if not path.exists():
        return {}
    live: dict[str, dict[str, Any]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        pid = data.get("property_id")
        if not pid:
            continue
        if data.get("unparked"):
            live.pop(pid, None)
        else:
            live[pid] = data
    return live


def _sync_dlq(engine: Any, path: Path) -> dict[str, int]:
    """Mirror ``state/dlq.jsonl`` into the ``dlq_entries`` table.

    Live entries are UPSERTed; any row in the DB whose property_id is
    not in the live set is DELETEd (the property was unparked locally
    since the last sync). Returns counts so the caller can log them.
    """
    live = _load_dlq_live_entries(path)
    from sqlalchemy import delete, select

    now = datetime.now(UTC).replace(tzinfo=None)
    upserted = 0
    deleted = 0
    with engine.begin() as conn:
        # Snapshot current DB property_ids so we can delete the ones that
        # disappeared from the local log (i.e. were unparked).
        db_ids = {row[0] for row in conn.execute(select(DlqEntryRow.property_id)).all()}

        for pid, data in live.items():
            stmt = dialect_insert(engine, DlqEntryRow).values(
                property_id=pid,
                parked_at=data.get("parked_at", ""),
                reason=data.get("reason", ""),
                last_error_signature=data.get("last_error_signature", ""),
                retry_at=data.get("retry_at", ""),
                last_synced_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[DlqEntryRow.property_id],
                set_={
                    "parked_at": stmt.excluded.parked_at,
                    "reason": stmt.excluded.reason,
                    "last_error_signature": stmt.excluded.last_error_signature,
                    "retry_at": stmt.excluded.retry_at,
                    "last_synced_at": stmt.excluded.last_synced_at,
                },
            )
            conn.execute(stmt)
            upserted += 1

        to_delete = db_ids - set(live.keys())
        if to_delete:
            conn.execute(
                delete(DlqEntryRow).where(DlqEntryRow.property_id.in_(to_delete))
            )
            deleted = len(to_delete)

    return {"upserted": upserted, "deleted": deleted}


def load_dlq_from_db_to_file(*, engine: Any, path: Path) -> int:
    """Materialise the DB DLQ into a local JSONL file.

    Called by ``jugnu_retry_entry`` before invoking the retry runner so
    that ``Dlq(state_dir / "dlq.jsonl")._load()`` sees the cross-run
    state instead of the empty /tmp it would otherwise get in a fresh
    Cloud Run container. Writes a compacted file (one line per live
    entry, no tombstones) so ``Dlq._load()`` sees it as pre-compacted.

    If the DB has zero entries we still create an empty file so the
    caller doesn't need to special-case the "no DLQ yet" condition.
    """
    from sqlalchemy import select

    path.parent.mkdir(parents=True, exist_ok=True)
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                DlqEntryRow.property_id,
                DlqEntryRow.parked_at,
                DlqEntryRow.reason,
                DlqEntryRow.last_error_signature,
                DlqEntryRow.retry_at,
            )
        ).all()

    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            entry = {
                "property_id": row[0],
                "parked_at": row[1],
                "reason": row[2],
                "last_error_signature": row[3],
                "retry_at": row[4],
                "unparked": False,
            }
            f.write(json.dumps(entry) + "\n")
    return len(rows)


def _apply_retention(engine: Any) -> dict[str, int]:
    """Enforce the 3-day retention window on all non-upsert tables.

    Per project policy, every per-run table is capped at a 3-day rolling
    window. Tables on this list:
      - run_date-keyed (ISO date strings — sort lexicographically, so
        ``WHERE run_date < :cutoff`` is dialect-portable):
        ``llm_reports``, ``llm_diagnostics``, ``llm_property_details``,
        ``property_snapshots``, ``run_issues``, ``run_ledger``.
      - timestamp-keyed: ``scrape_events`` (``scrape_timestamp``,
        ``DateTime``) and ``dlq_entries`` (``parked_at``, ISO string in
        ``String(64)``).

    ``properties``, ``units``, and ``scrape_profiles`` are upsert-only
    and intentionally absent from this helper — they store current
    state, not history.

    Multi-shard safe: every shard observes the same wall-clock cutoff,
    so concurrent calls converge on the same end state. Idempotent: a
    second call with no new aged-out rows deletes nothing.

    Returns ``{table_name: rows_deleted}`` so the run summary can log
    what got swept.
    """
    from sqlalchemy import text

    cutoff_date_iso = (date.today() - timedelta(days=3)).isoformat()
    cutoff_dt = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=3)
    cutoff_iso_ts = cutoff_dt.isoformat()

    deleted: dict[str, int] = {}
    with engine.begin() as conn:
        for table in (
            "llm_reports",
            "llm_diagnostics",
            "llm_property_details",
            "property_snapshots",
            "run_issues",
            "run_ledger",
        ):
            r = conn.execute(
                text(f"DELETE FROM {table} WHERE run_date < :cutoff"),
                {"cutoff": cutoff_date_iso},
            )
            deleted[table] = r.rowcount or 0

        # scrape_events.scrape_timestamp is a DateTime — pass a naive
        # UTC datetime so SQLAlchemy adapts it identically for Postgres
        # and SQLite.
        r = conn.execute(
            text("DELETE FROM scrape_events WHERE scrape_timestamp < :cutoff"),
            {"cutoff": cutoff_dt},
        )
        deleted["scrape_events"] = r.rowcount or 0

        # dlq_entries.parked_at is String(64) holding an ISO-8601 UTC
        # timestamp; ISO-8601 strings sort lexicographically, so the
        # text comparison is exact.
        r = conn.execute(
            text("DELETE FROM dlq_entries WHERE parked_at < :cutoff"),
            {"cutoff": cutoff_iso_ts},
        )
        deleted["dlq_entries"] = r.rowcount or 0

    return deleted


def _upsert_llm_diagnostics(engine: Any, run_date: str, dir_: Path) -> int:
    if not dir_.exists():
        return 0
    files = [p for p in dir_.glob("*.json") if p.is_file()]
    if not files:
        return 0
    now = datetime.now(UTC).replace(tzinfo=None)
    count = 0
    with engine.begin() as conn:
        for path in files:
            m = _DIAG_NAME.match(path.name)
            if m is None:
                log.warning("llm_diagnostics: skipping unrecognised name %s", path.name)
                continue
            payload = _load_json(path)
            if payload is None:
                continue
            stmt = dialect_insert(engine, LlmDiagnosticRow).values(
                run_date=run_date,
                property_id=m.group("cid"),
                kind=m.group("kind"),
                payload=payload,
                written_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    LlmDiagnosticRow.run_date,
                    LlmDiagnosticRow.property_id,
                    LlmDiagnosticRow.kind,
                ],
                set_={
                    "payload": stmt.excluded.payload,
                    "written_at": stmt.excluded.written_at,
                },
            )
            conn.execute(stmt)
            count += 1
    return count


# ── Orchestration ────────────────────────────────────────────────────────────


def sync_run_to_postgres(
    *,
    run_date: str,
    data_dir: Path,
    config_dir: Path,
    database_url: str | None = None,
    shard_id: str | None = None,
) -> dict[str, Any]:
    """Copy one shard's FS output into Postgres. Returns a counts summary.

    Args:
        run_date: YYYY-MM-DD string matching a ``runs/{run_date}/`` folder
            under ``data_dir``.
        data_dir: The base dir the runner wrote to (e.g. ``/tmp/data/v2``).
            Must already contain ``runs/{run_date}/`` + ``state/``.
        config_dir: Directory whose ``profiles/`` subdir holds the
            ``{cid}.json`` files the runner updated during the scrape.
        database_url: Passed to ``PostgresDataProvider``. When omitted,
            the provider resolves ``DATABASE_URL`` internally (with the
            Cloud SQL Connector path picked up via ``CLOUD_SQL_INSTANCE``
            in ``make_engine``).
        shard_id: When provided, writes that are naturally aggregated
            across shards (run_reports, llm_reports) use per-shard
            merge semantics instead of replace semantics. Pass the
            Cloud Run task index or any per-shard unique key.

    Raises on any SQL error — the caller (shard_entry) decides whether
    to fail the shard. Fail-the-shard is the right default: a silent
    sync failure is exactly how we got here.
    """
    data_dir = Path(data_dir)
    config_dir = Path(config_dir)
    run_dir = data_dir / "runs" / run_date
    state_dir = data_dir / "state"
    if not run_dir.exists():
        raise FileNotFoundError(
            f"run dir not found: {run_dir}. Did the runner exit successfully "
            f"with --data-dir={data_dir} and --run-date={run_date}?"
        )

    log.info(
        "sync start: run_date=%s data_dir=%s config_dir=%s provider=postgres shard_id=%s",
        run_date,
        data_dir,
        config_dir,
        shard_id,
    )

    src = FileSystemDataProvider(base_dir=data_dir, config_dir=config_dir)
    # Skip runtime create_all() for real Postgres — alembic owns the
    # schema and the Cloud Run worker SA doesn't have CREATE on schema
    # public (least privilege). For sqlite we still want create_all()
    # because the tests and local-dev fallbacks start from a fresh DB
    # with no alembic run.
    resolved_url = database_url or os.environ.get("DATABASE_URL") or ""
    is_postgres = resolved_url.startswith("postgresql")
    dst = PostgresDataProvider(url=database_url, create_schema=not is_postgres)
    summary: dict[str, Any] = {}
    try:
        # Stage 0 — read the per-run properties once so every downstream
        # derive-from-snapshots helper sees the same list.
        try:
            run_properties = src.runs.read_properties(run_date)
        except Exception:
            log.exception("sync: failed to read properties.json — continuing with empty list")
            run_properties = []

        # Stage 1 — everything that has a store interface goes inside a
        # single Postgres transaction wrapped in retry-on-transient logic.
        # If any per-row write fails with a *permanent* error (DataError,
        # IntegrityError) the row is isolated via SAVEPOINT and the rest
        # of the stage continues — see ``_sync_current_state_from_snapshots``.
        # Transient errors (OperationalError, etc.) trigger a bounded
        # exponential-backoff retry of the whole stage.
        fs_canonical_ids = sorted(src.property_state.all_canonical_ids())

        @with_db_retry(attempts=3, initial_wait_s=1.0, max_wait_s=8.0)
        def _stage1() -> None:
            with dst.transaction():
                summary["profiles"] = _copy_profiles(src, dst)
                summary["events_legacy"] = _copy_events(src, dst)
                # _copy_state reads from property_index.json (legacy pipeline).
                # For Jugnu that file is empty, so this is a no-op — the
                # real work happens in _sync_current_state_from_snapshots
                # below. We keep _copy_state for the backfill_pg.py path
                # where property_index.json IS populated.
                summary["state_legacy"] = _copy_state(src, dst)
                summary["state_from_snapshots"] = _sync_current_state_from_snapshots(
                    dst, run_date, run_properties
                )
                summary["run"] = _copy_run(src, dst, run_date, shard_id=shard_id)
                # Legacy path reads extraction_output/{cid}/{date}.json from
                # disk; Jugnu never writes those. The derive-from-snapshots
                # helper populates extraction_results directly from each
                # property's ``_extract_result`` field instead.
                summary["extractions_legacy"] = _copy_extractions(
                    src, dst, run_date, fs_canonical_ids
                )
                summary["extractions_from_snapshots"] = _sync_extraction_results_from_snapshots(
                    dst, run_date, run_properties
                )
                summary["scrape_events_from_snapshots"] = _sync_scrape_events_from_snapshots(
                    dst, run_date, run_properties
                )
                # run_ledger + run_issues are already partially populated by
                # _copy_run if the runner wrote issues.jsonl / ledger.jsonl.
                # Jugnu doesn't write those files, so we supplement here
                # from the property snapshots. Idempotent: _copy_run deletes
                # this batch's cids from the two tables before appending, so
                # these helpers append fresh entries that won't duplicate.
                summary["ledger_from_snapshots"] = _sync_run_ledger_from_snapshots(
                    dst, run_date, run_properties
                )
                summary["issues_from_snapshots"] = _sync_run_issues_from_snapshots(
                    dst, run_date, run_properties
                )

        _stage1()

        # Stage 2 — raw-engine work. Each helper manages its own txn via
        # ``engine.begin()`` (the SQL provider exposes the engine). This
        # runs OUTSIDE the Stage 1 transaction because
        # ``_merge_run_report`` / ``_upsert_llm_report`` use
        # ``SELECT … FOR UPDATE`` for cross-shard safety, and SQLite
        # deadlocks when ``engine.begin()`` is nested under an active
        # session.
        engine = dst.engine

        # Each Stage 2 helper is independent — wrap each call in
        # ``_run_stage2`` so a transient retry covers connection blips,
        # and a permanent error in one helper (e.g. a malformed
        # llm_diagnostics file) doesn't prevent the rest from running.
        # The per-helper failure is recorded in ``summary[..._error]``
        # so the caller can surface it.
        def _run_stage2(name: str, fn: Callable[[], Any]) -> Any:
            wrapped = with_db_retry(attempts=3, initial_wait_s=0.5)(fn)
            try:
                return wrapped()
            except PERMANENT_ROW_ERRORS as exc:
                log.warning(
                    "stage2 %s permanent error — skipping. %s: %s",
                    name,
                    type(exc).__name__,
                    str(exc).splitlines()[0] if str(exc) else "",
                )
                summary[f"{name}_error"] = f"{type(exc).__name__}: {exc}"
                return None
            except Exception as exc:  # noqa: BLE001
                # Retries already exhausted for transient errors, or a
                # genuine code bug. Don't kill the whole sync — record
                # and continue. The caller (shard_entry) treats sync
                # failure on a single helper as a soft warning.
                log.exception("stage2 %s failed", name)
                summary[f"{name}_error"] = f"{type(exc).__name__}: {exc}"
                return None

        # Report merge must happen here (not in _copy_run) for the same
        # reason: it opens its own engine-level txn and can't live
        # inside the shared-session Stage 1 block.
        report = src.runs.read_report(run_date)
        if report is not None:
            if shard_id is not None:
                _run_stage2("report", lambda: _merge_run_report(engine, run_date, shard_id, report))
            else:
                # Legacy single-writer path (e.g. CLI backfill without
                # --shard-id). Open a throwaway provider to call the
                # store's replace-semantics write_report.
                _run_stage2("report", lambda: _single_writer_report(dst, run_date, report))
            summary["report"] = 1
        else:
            summary["report"] = 0
        summary["property_reports"] = _run_stage2(
            "property_reports",
            lambda: _upsert_property_reports(engine, run_date, run_dir / "property_reports"),
        ) or 0
        summary["llm_reports"] = _run_stage2(
            "llm_reports",
            lambda: _upsert_llm_report(engine, run_date, run_dir / "llm_report.json", shard_id=shard_id),
        ) or 0
        summary["llm_property_details"] = _run_stage2(
            "llm_property_details",
            lambda: _upsert_llm_property_details(engine, run_date, run_dir / "llm_report"),
        ) or 0
        summary["llm_diagnostics"] = _run_stage2(
            "llm_diagnostics",
            lambda: _upsert_llm_diagnostics(engine, run_date, run_dir / "llm_diagnostics"),
        ) or 0
        # DLQ is cross-run global state — lives under state/, not runs/.
        # Without this mirror, Cloud Run's ephemeral /tmp loses every
        # parked property the moment the container exits.
        summary["dlq"] = _run_stage2(
            "dlq",
            lambda: _sync_dlq(engine, state_dir / "dlq.jsonl"),
        ) or {"upserted": 0, "deleted": 0}
        # Retention sweep — last step. Never let a janitorial failure
        # block a successful run from reporting completion; _run_stage2
        # already records the error in summary["retention_error"] if it
        # fails so the daily report still surfaces it.
        summary["retention"] = _run_stage2(
            "retention",
            lambda: _apply_retention(engine),
        ) or {}
    finally:
        try:
            src.close()
        finally:
            dst.close()

    log.info("sync complete: %s", summary)
    return summary


# ── CLI (for manual re-sync / debugging) ─────────────────────────────────────


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(
        description="Copy one run's FS output into Postgres.",
    )
    ap.add_argument("--run-date", required=True)
    ap.add_argument(
        "--data-dir",
        required=True,
        help="Base dir containing runs/{date}/ + state/ (e.g. /tmp/data/v2)",
    )
    ap.add_argument(
        "--config-dir",
        default=str(_MA_POC_ROOT / "config"),
        help="Dir containing profiles/ (default: ma_poc/config)",
    )
    ap.add_argument("--url", default=None, help="DATABASE_URL override")
    ap.add_argument(
        "--shard-id",
        default=None,
        help=(
            "Per-shard key for multi-shard merge. Cloud Run sets this to "
            "the task index; CLI invocations can leave it unset to use "
            "the single-writer replace semantics (legacy behaviour)."
        ),
    )
    args = ap.parse_args()

    try:
        sync_run_to_postgres(
            run_date=args.run_date,
            data_dir=Path(args.data_dir),
            config_dir=Path(args.config_dir),
            database_url=args.url,
            shard_id=args.shard_id,
        )
    except Exception:
        log.exception("sync failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
