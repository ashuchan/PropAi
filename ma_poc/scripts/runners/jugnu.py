"""
Jugnu J8 — Integrated daily runner using Jugnu L1-L5 layers.

This wraps the existing daily_runner.py flow with Jugnu's:
  - L2 Scheduler for task generation
  - L1 Fetcher for HTTP/Playwright requests
  - L3 Scraper (via scrape_jugnu) with short-circuit on non-OK fetch
  - L4 Validation with schema gate + identity fallback
  - L5 Observability (event ledger, cost ledger, SLO checks)
  - L2 Carry-forward safety net on failures

Supports both v1 and v2 output schemas via --schema-version flag.

Usage:
  python scripts/runners/jugnu.py --csv config/properties.csv --limit 20
  python scripts/runners/jugnu.py --csv config/properties.csv --schema-version v2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re as _re
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

# Ensure ma_poc is importable regardless of working directory.
# _repo_root lets ``from ma_poc.pms...`` resolve; _MA_POC_ROOT lets
# ``from services.profile_store...`` and ``from models....`` resolve
# (those packages live directly under ma_poc/, not ma_poc/ma_poc/).
_repo_root = Path(__file__).resolve().parent.parent.parent.parent
_MA_POC_ROOT = Path(__file__).resolve().parent.parent.parent  # ma_poc/
for _p in (_repo_root, _MA_POC_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# Hoisted from inside _format_v2_unit; the merge-rescue path runs once per
# unit (~50K calls per run) and import caching makes the repeated lookup
# free, but module-level keeps the hot loop clean.
from ma_poc.core.identity import assign_fallback_unit_id

# Date parser + producer-token tables — single source of truth in
# :mod:`ma_poc.extraction.dates`. Imported under the legacy underscore
# names so the availability-status inference (~50K calls/run) reads
# them as module-level locals without a re-lookup hop.
from ma_poc.extraction.dates import (
    DATE_NOW_TOKENS as _DATE_NOW_TOKENS,
    DATE_PREFIX_RE as _DATE_PREFIX_RE,
    format_loose_date as _format_loose_date_impl,
)

log = logging.getLogger("jugnu_runner")


def _resolve_per_property_timeout() -> float:
    """Per-property wall-clock cap for the full L1→L4 pipeline.

    Sized off prod data: p95 property completes in ~30s; p99 in ~120s.
    600s (10 min) leaves enormous headroom for slow sites while still
    cutting the pathological tail (the multi-hour hangs that wedged
    shards 8/12/17 on three consecutive days). Above this cap, a single
    bad property would otherwise consume the entire 4h Cloud Run task
    budget and freeze the AsyncPool. Override via
    PER_PROPERTY_TIMEOUT_SECONDS for back-compat / debugging.
    """
    raw = os.getenv("PER_PROPERTY_TIMEOUT_SECONDS")
    if not raw:
        return 600.0
    try:
        v = float(raw)
        return v if v > 0 else 600.0
    except (TypeError, ValueError):
        return 600.0


PER_PROPERTY_TIMEOUT_SECONDS = _resolve_per_property_timeout()


def _resolve_schema_version(args: Any = None) -> str:
    """Resolve schema version from CLI args > env > default.

    Args:
        args: argparse namespace with optional ``schema_version`` attribute.

    Returns:
        ``"v1"`` or ``"v2"``.
    """
    if args and getattr(args, "schema_version", None):
        return args.schema_version
    return os.getenv("SCHEMA_VERSION", "v1").strip().lower()


def _resolve_data_dirs(
    data_dir: Path,
    schema_version: str,
    run_date: str,
) -> tuple[Path, Path, Path, Path]:
    """Resolve schema-namespaced data directories.

    V1 uses data/runs/{date}/ and data/state/ (legacy flat layout).
    V2 uses data/v2/runs/{date}/ and data/v2/state/.

    Args:
        data_dir: Base data directory.
        schema_version: "v1" or "v2".
        run_date: Date string for this run.

    Returns:
        (run_dir, state_dir, cache_dir, schema_root)
    """
    if schema_version == "v2":
        schema_root = data_dir / "v2"
    else:
        schema_root = data_dir

    run_dir = schema_root / "runs" / run_date
    state_dir = schema_root / "state"
    cache_dir = schema_root / "cache"

    run_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    return run_dir, state_dir, cache_dir, schema_root


async def run_jugnu(
    csv_path: Path | None = None,
    data_dir: Path = _MA_POC_ROOT / "data",
    limit: int | None = None,
    run_date: str | None = None,
    schema_version: str = "v1",
    start_index: int = 0,
    shard_index: int | None = None,
    shard_count: int | None = None,
    force_scrape: bool = False,
) -> dict[str, Any]:
    """Run the Jugnu integrated pipeline.

    Args:
        csv_path: Optional CSV override. When set, the runner reads the
            input catalog from this file regardless of DATA_PROVIDER —
            useful for back-compat / dev. When None (the default), the
            runner reads from `provider.property_catalog`, whose backing
            store depends on DATA_PROVIDER (CSV for filesystem, the
            `properties` table for postgres/sqlite).
        data_dir: Base data directory.
        limit: Max properties to process.
        run_date: Override run date (YYYY-MM-DD).
        schema_version: "v1" or "v2" output format.
        start_index: Zero-based row index in the catalog to start scraping
            from. Rows before this index are skipped. ``limit`` still caps
            the number of rows processed *after* skipping.
        shard_index: Zero-based index of this shard. Requires ``shard_count``.
            Catalog rows are sliced into ``shard_count`` contiguous chunks
            (ordered by canonical_id for SQL, original CSV order for CSV)
            and this shard processes the chunk at ``shard_index``.
        shard_count: Total number of shards. When set with ``shard_index``,
            replaces the legacy "download CSV → slice → exec --csv" pattern
            in jugnu_shard_entry.py.
        force_scrape: When True, bypass change-detection for every property
            and always issue a full RENDER task. Intended for canary replays
            and per-property forensics; do not set in production shards.

    Returns:
        Run summary dict.
    """
    from ma_poc.discovery.change_detector import decide as decide_change
    from ma_poc.discovery.contracts import CrawlTask
    from ma_poc.discovery.dlq import Dlq
    from ma_poc.discovery.frontier import Frontier
    from ma_poc.discovery.scheduler import Scheduler
    from ma_poc.discovery.sitemap import SitemapConsumer
    from ma_poc.fetch import fetch as jugnu_fetch
    from ma_poc.fetch.conditional import ConditionalCache
    from ma_poc.observability import events
    from ma_poc.observability.cost_ledger import CostLedger
    from ma_poc.observability.slo_watcher import check as slo_check
    from ma_poc.reporting.run_report import build as build_run_report

    # Setup
    today = run_date or date.today().isoformat()
    run_dir, state_dir, cache_dir, schema_root = _resolve_data_dirs(
        data_dir,
        schema_version,
        today,
    )
    run_id = f"{today}_{uuid.uuid4().hex[:8]}"

    log.info("Schema version: %s", schema_version)
    log.info("Run directory: %s", run_dir)

    # Configure observability
    events.configure(run_dir, run_id)
    cost_ledger = CostLedger(run_dir / "cost_ledger.db")

    # Resolve the catalog source — explicit --csv override beats the
    # provider-default. Without --csv we read whichever backing store the
    # configured DataProvider exposes (CSV for filesystem, DB for sql).
    from data_provider.dtos import CatalogFilters
    from data_provider.factory import get_data_provider
    from data_provider.filesystem import CsvPropertyCatalogSource

    if csv_path is not None:
        catalog_source = CsvPropertyCatalogSource(csv_path)
        catalog_label = f"csv:{csv_path}"
    else:
        catalog_source = get_data_provider().property_catalog
        catalog_label = type(catalog_source).__name__

    # Bug #5 Layer 3 (2026-05-18): pass run_date as the shuffle seed.
    # The catalog source shuffles deterministically by this key BEFORE
    # partitioning into shards, breaking up domain clusters (essex's
    # 27 contiguous rows in the CSV previously landed across only 12
    # shards). Same-day re-runs land the same property in the same
    # shard (debug reproducibility); day-over-day the distribution
    # rotates so no shard is always the "Essex shard".
    _shuffle_seed = today  # already YYYY-MM-DD string used downstream
    catalog_filters = CatalogFilters(
        start_index=start_index or None,
        shard_index=shard_index,
        shard_count=shard_count,
        shuffle_seed=_shuffle_seed,
    )
    catalog_rows = catalog_source.list_active(limit=limit, filters=catalog_filters)
    # Downstream consumers (scheduler.build_tasks, csv_lookup, _format_v1/v2)
    # treat each row as a flat dict with property_id/url + Title-Case CSV
    # aliases. `as_csv_row()` mints exactly that shape from the DTO so
    # nothing below this line cares whether the source was CSV or DB.
    rows = [p.as_csv_row() for p in catalog_rows]
    log.info(
        "Loaded %d properties from %s (start_index=%d, limit=%s, shard=%s/%s)",
        len(rows),
        catalog_label,
        start_index,
        limit,
        shard_index,
        shard_count,
    )
    # Empty-catalog defensive warning. Most likely cause: DATA_PROVIDER
    # points at a SQL backend that hasn't been seeded yet — the operator
    # forgot to run `python -m scripts.ingest_properties_csv`. Without
    # this log line the run silently "succeeds" with zero properties
    # processed, which looks like green CI but is a missed daily scrape.
    # A user passing --csv explicitly knows what they're doing and might
    # legitimately point at an empty file (smoke tests etc), so don't
    # warn in that case.
    if not rows and csv_path is None:
        log.warning(
            "Catalog source %s returned 0 properties. If DATA_PROVIDER points "
            "at a SQL backend, did you run `python -m scripts.ingest_properties_csv` "
            "to seed the `properties` table? Pass --csv to bypass the catalog "
            "and read from a file directly.",
            catalog_label,
        )

    # Setup L2 components
    frontier = Frontier(state_dir / "frontier.sqlite")
    dlq = Dlq(state_dir / "dlq.jsonl")
    cond_cache = ConditionalCache(cache_dir / "conditional.sqlite")
    sitemap = SitemapConsumer(fetcher=jugnu_fetch, cond_cache=cond_cache)

    # Load the FS state-store once for the whole run.  All _process_property
    # coroutines share this instance and call upsert_units() synchronously
    # (no await between accesses) so in-memory mutations are linearised under
    # AsyncPool's single-thread event loop.  save() happens once after the pool
    # completes so we pay only two file-IO round-trips regardless of run size.
    from ma_poc.core.state_store import StateStore as _RunStateStore
    run_state_store = _RunStateStore(state_dir)
    try:
        run_state_store.load()
    except Exception as _sse:
        log.warning("state_store.load() failed — carry-forward and keyless gate disabled: %s", _sse)
        run_state_store = None  # type: ignore[assignment]

    # Profile store. In production the DATA_PROVIDER env points at Postgres
    # so we get a SqlProfileStore that survives Cloud Run task teardown —
    # critical for the self-learning loop (saved llm_field_mappings &
    # dom_hints replay on the next run instead of re-paying LLM tax). Local
    # dev with DATA_PROVIDER unset still gets the FS store, same as before.
    profile_store = _build_profile_store(_MA_POC_ROOT / "config" / "profiles")

    # PR 1 (2026-05-10): Persistence sentinel probe. Verifies that every
    # writeable channel of the self-learning loop round-trips through the
    # store before the runner processes any property. On failure: emit
    # STARTUP_PROBE_FAILED and re-raise so the runner exits non-zero (the
    # shard's PG sync is gated on runner exit code in shard_entry.py and
    # so won't poison the DB with the output of a half-broken run). Toggle
    # via ENABLE_PERSISTENCE_PROBE=false (default true).
    from services.profile_persistence_probe import run_sentinel_probe
    run_sentinel_probe(
        profile_store.backing if hasattr(profile_store, "backing") else profile_store
    )

    scheduler = Scheduler(
        frontier=frontier,
        dlq=dlq,
        sitemap=sitemap,
        profile_store=profile_store,
        change_detector_fn=decide_change,
    )

    # Build tasks
    tasks: list[CrawlTask] = []
    async for task in scheduler.build_tasks(rows, force_scrape=force_scrape):
        tasks.append(task)
    log.info("Scheduled %d tasks", len(tasks))

    # Build CSV lookup for output formatting
    csv_lookup = {row["property_id"]: row for row in rows}

    # Determine concurrency pool size
    from ma_poc.core.concurrency import AsyncPool, SystemResources

    res = SystemResources.detect()
    pool_size = res.optimal_pool_size()

    # Shard_84 fix (2026-05-16): cap AsyncPool to MAX_CONCURRENT_BROWSERS.
    # Pre-fix the AsyncPool sized to CPU/RAM (8 on Cloud Run) while the
    # browser-context pool was sized to MAX_CONCURRENT_BROWSERS (5).
    # When 5 Chromium renderers wedged in IPC, the 3 extra AsyncPool
    # workers blocked indefinitely on ``browser_pool._semaphore.acquire()``
    # without progress. The per-property 600s wallclock was the only thing
    # eventually unsticking them. Sizing AsyncPool to the browser cap means
    # we never schedule more properties than the browser pool can run, so
    # no worker silently waits on a dead Chromium child.
    _browser_cap = int(os.getenv("MAX_CONCURRENT_BROWSERS", "5"))
    if _browser_cap > 0 and _browser_cap < pool_size:
        log.info(
            "Capping AsyncPool from %d to MAX_CONCURRENT_BROWSERS=%d "
            "(shard_84 wedge fix)",
            pool_size, _browser_cap,
        )
        pool_size = _browser_cap

    log.info("System resources: %s → pool_size=%d", res.summary(), pool_size)
    pool = AsyncPool(pool_size)

    # Accumulator for LLM interactions across the whole run. Safe to share
    # across `_process_one` coroutines because AsyncPool runs them on the
    # same event loop thread — list.append / extend are atomic under asyncio.
    all_llm_interactions: list[dict[str, Any]] = []

    async def _process_one(task: Any) -> dict[str, Any]:
        log.info("Processing %s (%s)", task.property_id, task.url)
        # Created BEFORE wait_for so it outlives the coroutine. The scraper
        # writes accumulated hop-units into this dict via shared_budget so the
        # timeout handler can salvage partial results without any I/O inside
        # the cancelled coroutine (which would deadlock on the event loop).
        _partial_state: dict[str, Any] = {}
        # Lookup csv_row BEFORE the try so the timeout/crash except branches
        # below can pass it to _make_failed_record — without this the failed
        # record emits NULL name/address and overwrites the properties row
        # for any property that has never had a successful scrape.
        csv_row = csv_lookup.get(task.property_id, {})
        try:
            # Per-property wall-clock guard. Without this, a single property
            # that gets caught in the LLM-retry/link-hop tail can monopolise
            # the AsyncPool until Cloud Run's 4h task timeout kills the whole
            # shard (observed three days running on shards 8 / 12 / 17).
            result = await asyncio.wait_for(
                _process_property(
                    task,
                    cost_ledger,
                    profile_store,
                    frontier,
                    dlq,
                    data_dir,
                    csv_row=csv_row,
                    run_dir=run_dir,
                    state_store=run_state_store,
                    schema_version=schema_version,
                    partial_state=_partial_state,
                ),
                timeout=PER_PROPERTY_TIMEOUT_SECONDS,
            )
            # RC-3 (2026-05-16): propagate entry-page captcha/bot-block flags
            # from _fetch_diagnostic into _meta so the wedge-rescue filter
            # (line ~530) can skip retries on properties that were rejected
            # at the WAF — a HTTP-only retry returns the same captcha stub
            # (~200 bytes) which then trips LLM_GATE_NO_BODY and DOWNGRADES
            # the correct FAILED_UNREACHABLE verdict to FAILED_NO_DATA.
            # PIDs 298969 thewattapts, 300327 flatson10th, 3188 thepointeatlapts,
            # 55317 abodes — all SiteGround SGCAPTCHA, all flipped UNREACHABLE
            # to NO_DATA via the wedge-rescue stub.
            _fd_for_captcha = result.get("_fetch_diagnostic") or {}
            if _fd_for_captcha.get("captcha_detected") or _fd_for_captcha.get("bot_blocked"):
                _result_meta = result.setdefault("_meta", {})
                _result_meta["entry_captcha_detected"] = bool(_fd_for_captcha.get("captcha_detected"))
                _result_meta["entry_bot_blocked"] = bool(_fd_for_captcha.get("bot_blocked"))
                _result_meta["entry_captcha_provider"] = _fd_for_captcha.get("captcha_provider")
            # PR 2 (2026-05-10): null-field recovery now runs INSIDE
            # _process_property (before the profile-update step) so
            # recovered FieldPatch entries reach the persistence layer.
            # Reuse the formatted dict produced there if present; otherwise
            # build it now for the report-writing path below.
            formatted = result.get("_v2_formatted") or _format_output(result, csv_row, schema_version)
            # Per-property report — same format as daily_runner emits, but
            # sourced from jugnu's raw scrape_result + formatted v1/v2 record
            # so v2 metadata (apartment_id/pmc/website_design/concessions)
            # and v2 unit fields (beds/baths/rent_low/rent_high) render.
            _write_property_report(
                result,
                formatted,
                run_dir,
                task.property_id,
                today,
            )

            # LLM cost accounting — write per-property llm_report/{id}.json
            # and accumulate onto the shared list for the run-wide summary
            # below. The raw scrape_result carries _llm_interactions emitted
            # by the GenericAdapter sub-tiers (api/dom/monolithic) and the
            # F1 adapter_debugger hook. Never let a report-write failure
            # crash the scrape.
            interactions = result.get("_llm_interactions") or []
            if interactions:
                try:
                    from ma_poc.llm.interaction_logger import write_property_report as _write_llm_report

                    _write_llm_report(task.property_id, interactions, run_dir)
                except Exception as exc:
                    log.warning("LLM per-property report failed for %s: %s", task.property_id, exc)
                all_llm_interactions.extend(interactions)

            return formatted
        except (TimeoutError, asyncio.TimeoutError) as exc:
            log.error(
                "Property %s timed out after %.0fs — attempting partial recovery",
                task.property_id, PER_PROPERTY_TIMEOUT_SECONDS,
            )
            # _partial_state was written by _try_link_hop via shared_budget
            # while the coroutine was running. Because it was created in THIS
            # scope (not inside the cancelled coroutine), it survives cancellation.
            _partial_units: list[Any] = _partial_state.get("units") or []
            _partial_profile = _partial_state.get("profile")
            if _partial_units:
                log.info(
                    "Property %s: recovered %d partial units from hop accumulation",
                    task.property_id, len(_partial_units),
                )
                # Persist partial units to state-store so carry-forward works
                # on the next run and the data isn't completely lost.
                try:
                    if run_state_store is not None and run_dir is not None:
                        _run_date_str = run_dir.name
                        run_state_store.upsert_units(
                            task.property_id, _partial_units, _run_date_str
                        )
                except Exception as _su_exc:
                    log.warning("partial state_store.upsert_units failed: %s", _su_exc)
                # Persist the scrape profile so LLM-learned CSS selectors and
                # field mappings from this run survive for next-day replay.
                try:
                    if _partial_profile is not None and hasattr(profile_store, "save"):
                        profile_store.save(_partial_profile)
                except Exception as _ps_exc:
                    log.warning("partial profile_store.save failed: %s", _ps_exc)
            failed = _make_failed_record(
                task.property_id,
                task.url,
                f"per_property_timeout:{int(PER_PROPERTY_TIMEOUT_SECONDS)}s ({type(exc).__name__})"
                + (f" — {len(_partial_units)} partial units persisted" if _partial_units else ""),
                schema_version,
                csv_row=csv_row,
            )
            # Surface partial units in the failed record so the run report can
            # show partial data rather than a zero-unit timeout row.
            if _partial_units:
                failed["units"] = _partial_units
                failed.setdefault("_meta", {})["partial_recovery"] = True
            # 2026-05-16: stamp verdict + emit PROPERTY_EMITTED so the
            # analyzer counts these in the right bucket. Before this fix,
            # 108 partial-recovery properties (~20–149 units each) on
            # 2026-05-16 were invisible to failures.csv AND successes.csv
            # because the only emit-site sat in the SUCCESS branch at the
            # bottom of this function — never reached on TimeoutError.
            #
            # 2026-05-17: distinguish from validation-majority-rejected
            # ``PARTIAL`` by using ``SUCCESS_PARTIAL`` here. Both verdicts
            # carry real units, but ``SUCCESS_PARTIAL`` is data-clean
            # (timeout cut the cascade short; the units that buffered
            # passed Stage-1 validity) while ``PARTIAL`` means the gate
            # actively rejected the majority of rows. The success
            # classifier (reporting/verdict.py:_SUCCESS_VERDICTS) admits
            # only ``SUCCESS_PARTIAL``, keeping the validation-rejected
            # case out of the headline success rate.
            _v = "SUCCESS_PARTIAL" if _partial_units else "FAILED_UNREACHABLE"
            _meta = failed.setdefault("_meta", {})
            _meta["verdict"] = _v
            _meta["verdict_reason"] = (
                f"per_property_timeout_partial_recovery ({len(_partial_units)} units)"
                if _partial_units
                else "per_property_timeout_no_recovery"
            )
            try:
                from ma_poc.observability.events import EventKind, emit
                emit(
                    EventKind.PROPERTY_EMITTED,
                    task.property_id,
                    verdict=_v,
                    units=len(_partial_units),
                )
            except Exception as _emit_exc:
                log.warning("partial-recovery PROPERTY_EMITTED emit failed: %s", _emit_exc)
            return failed
        except Exception as exc:
            log.error("Property %s crashed: %s", task.property_id, exc)
            failed = _make_failed_record(
                task.property_id,
                task.url,
                str(exc),
                schema_version,
                csv_row=csv_row,
            )
            # Bug #1 fix (2026-05-16): same verdict-stamp + emit for the crash
            # path. Without this, any uncaught exception inside
            # _process_property silently drops the property from reporting.
            failed.setdefault("_meta", {})["verdict"] = "FAILED_UNREACHABLE"
            failed["_meta"]["verdict_reason"] = f"runtime_exception:{type(exc).__name__}"
            try:
                from ma_poc.observability.events import EventKind, emit
                emit(
                    EventKind.PROPERTY_EMITTED,
                    task.property_id,
                    verdict="FAILED_UNREACHABLE",
                    units=0,
                )
            except Exception:
                pass
            return failed

    results = await pool.map(_process_one, [(t,) for t in tasks])

    # ── Wedge-rescue retry pass (2026-05-16 shard_84 fix) ──────────────
    # Identify properties that wedged (Playwright IPC death → CANCELLED
    # via the host-level page.goto timeout in fetch/fetcher.py, OR the
    # per-property 600s wallclock). For these, RENDER mode failed in a
    # way that's not data-dependent — the renderer process itself died.
    # Retry with RenderMode.GET, which bypasses Playwright entirely
    # (just a tier-aware HTTPS client). This recovers ~70% of static-
    # HTML / JSON-LD / embedded-JSON / DOM-cascade properties without
    # spending another full 600s budget on a wedge-prone path.
    #
    # Shard_84 (2026-05-16) had 32 of 50 PIDs in this state. Without
    # this retry pass, those 32 PIDs would re-wedge on the SAME
    # Chromium-IPC pathology tomorrow — the property data isn't broken;
    # only the runtime environment was.
    #
    # Retries DO NOT replace partial-recovery records that already have
    # units > 0 — those persisted partial data is real, and we only
    # want to upgrade truly empty results.
    _retry_candidate_pids: list[str] = []
    _pid_to_index: dict[str, int] = {}
    for _idx, _r in enumerate(results):
        if isinstance(_r, Exception):
            continue
        _meta_r = _r.get("_meta") or {} if isinstance(_r, dict) else {}
        _pid_r = str(_meta_r.get("canonical_id") or "")
        if not _pid_r:
            continue
        _pid_to_index[_pid_r] = _idx
        _has_units = bool(_r.get("units"))
        _decision = wedge_rescue_decision(_meta_r, has_units=_has_units)
        if _decision == "RETRY":
            _retry_candidate_pids.append(_pid_r)
        elif _decision == "SKIP_ENTRY_CAPTCHA":
            # Visibility: count skipped retries via a dedicated event so
            # we can see the SGCAPTCHA-blocked population separately
            # from rescue-attempt counts.
            try:
                from ma_poc.observability.events import EventKind as _EK
                from ma_poc.observability.events import emit as _emit
                _emit(
                    _EK.WEDGE_RESCUE_RETRY_RESOLVED,
                    _pid_r,
                    resolution="SKIPPED_ENTRY_CAPTCHA",
                    verdict=(_meta_r.get("verdict") or "UNKNOWN"),
                )
            except Exception:
                pass

    if _retry_candidate_pids:
        log.info(
            "Wedge-rescue: retrying %d cancelled/wedged PIDs with "
            "RenderMode.GET (HTTP-only, bypasses Playwright IPC)",
            len(_retry_candidate_pids),
        )
        from ma_poc.discovery.contracts import CrawlTask as _CrawlTask
        from ma_poc.discovery.contracts import TaskReason as _TaskReason
        from ma_poc.fetch.contracts import RenderMode as _RenderMode

        # Build retry tasks. Use a shorter budget (90s) since GET is
        # fundamentally faster than RENDER and we'd rather fail-fast on
        # a second timeout than burn the rest of the shard budget.
        _retry_tasks: list[_CrawlTask] = []
        _existing_tasks_by_pid = {t.property_id: t for t in tasks}
        for _pid in _retry_candidate_pids:
            _orig = _existing_tasks_by_pid.get(_pid)
            if _orig is None:
                continue
            _retry_tasks.append(_CrawlTask(
                url=_orig.url,
                property_id=_orig.property_id,
                priority=0,
                budget_ms=90_000,
                reason=_TaskReason.RETRY,
                render_mode=_RenderMode.GET,
                expected_pms=_orig.expected_pms,
            ))

        if _retry_tasks:
            # Step-3 (2026-05-16): dedicated EventKinds for wedge-rescue
            # telemetry. The previous implementation piggybacked the start
            # signal onto PROPERTY_EMITTED with a custom verdict string —
            # that conflated retry-attempt counts with verdict counts and
            # made rescue_attempt_rate / rescue_recovery_rate invisible to
            # cross-run analyzers.
            try:
                from ma_poc.observability.events import EventKind as _EK
                from ma_poc.observability.events import emit as _emit
                for _rt in _retry_tasks:
                    _emit(
                        _EK.WEDGE_RESCUE_RETRY_STARTED,
                        _rt.property_id,
                        url=_rt.url,
                        render_mode=_rt.render_mode.value,
                        budget_ms=_rt.budget_ms,
                    )
            except Exception:
                pass

            # Smaller pool — most wedge-rescue retries are quick.
            _retry_pool_size = min(len(_retry_tasks), pool_size)
            _retry_pool = AsyncPool(_retry_pool_size)
            log.info(
                "Wedge-rescue: pool_size=%d for %d retries",
                _retry_pool_size, len(_retry_tasks),
            )

            # RC5 (2026-05-17 regression fix) — enforce ``task.budget_ms``
            # on each retry. ``_process_one`` wraps its body in
            # ``asyncio.wait_for(..., timeout=PER_PROPERTY_TIMEOUT_SECONDS)``
            # (line 365 above, 600s), which ignores the 90s budget set
            # at the dispatch site. On 2026-05-17 shard_10 had 25 retry
            # candidates: with a pool size of ~10 and each task allowed
            # to hit the outer 600s timeout, the rescue phase serialised
            # to **30:01 minutes** (all 50 started events at
            # 08:31:44.873, all 50 resolved at 09:01:45.938). That
            # blew shard_10's wallclock from 29 min (May 16) to 92 min
            # and produced the +22 CANCELLED in the shard.
            #
            # The wrapper enforces the per-task budget and synthesises a
            # failed record on its own timeout so the zip-with-results
            # loop downstream sees a properly-shaped value and emits the
            # RESOLVED/RETRY_ALSO_FAILED event for telemetry.
            async def _process_one_with_budget(_task: Any) -> dict[str, Any]:
                _budget_sec = max(1.0, float(getattr(_task, "budget_ms", 90_000)) / 1000.0)
                try:
                    return await asyncio.wait_for(
                        _process_one(_task),
                        timeout=_budget_sec,
                    )
                except TimeoutError:
                    log.warning(
                        "wedge-rescue retry exceeded budget for %s: %.0fs",
                        _task.property_id, _budget_sec,
                    )
                    return _make_failed_record(
                        _task.property_id,
                        _task.url,
                        f"wedge_rescue_budget_exhausted:{int(_task.budget_ms)}ms",
                        schema_version,
                        csv_row=csv_lookup.get(_task.property_id, {}),
                    )

            _retry_results = await _retry_pool.map(
                _process_one_with_budget, [(t,) for t in _retry_tasks]
            )

            _upgrade_count = 0
            for _rt, _rr in zip(_retry_tasks, _retry_results):
                if isinstance(_rr, Exception):
                    log.warning(
                        "wedge-rescue retry crashed for %s: %s",
                        _rt.property_id, _rr,
                    )
                    # Step-3: emit RESOLVED with CRASHED resolution so the
                    # rescue-rate calculation sees every started attempt.
                    try:
                        from ma_poc.observability.events import EventKind as _EK
                        from ma_poc.observability.events import emit as _emit
                        _emit(
                            _EK.WEDGE_RESCUE_RETRY_RESOLVED,
                            _rt.property_id,
                            resolution="CRASHED",
                            error=str(_rr)[:200],
                        )
                    except Exception:
                        pass
                    continue
                _rr_meta = (_rr.get("_meta") or {}) if isinstance(_rr, dict) else {}
                _rr_v = (_rr_meta.get("verdict") or "").upper()
                _rr_has_units = bool(_rr.get("units"))

                # Shard_10 fix (2026-05-17): retroactively stamp the
                # original record with entry_bot_blocked when the rescue's
                # HTTP-only GET surfaced a WAF response. See
                # ``stamp_inferred_entry_block`` docstring for the full
                # rationale.
                _rr_fd = _rr.get("_fetch_diagnostic") or {}
                _orig_idx = _pid_to_index.get(_rt.property_id)
                if _orig_idx is not None:
                    _orig = results[_orig_idx]
                    if isinstance(_orig, dict):
                        stamp_inferred_entry_block(
                            _orig.setdefault("_meta", {}),
                            _rr_fd,
                        )

                # Upgrade ONLY if retry produced units OR a non-failure verdict.
                # An HTTP_ONLY retry that also fails doesn't help; keep the
                # original partial-recovery record (it might carry-forward
                # state-store data we don't want to overwrite).
                _upgraded = False
                if _rr_has_units or _rr_v in ("SUCCESS", "SUCCESS_PLAN_LEVEL", "SUCCESS_PARTIAL"):
                    _idx = _pid_to_index.get(_rt.property_id)
                    if _idx is not None:
                        # Stamp the retry record so the analyzer knows
                        # this was a rescue. Preserves cost-accountability
                        # to the wedge-rescue path.
                        _rr_meta["wedge_rescue_applied"] = True
                        _rr["_meta"] = _rr_meta
                        results[_idx] = _rr
                        _upgrade_count += 1
                        _upgraded = True
                # Step-3: emit RESOLVED so cross-run telemetry can compute
                # rescue_recovery_rate without re-walking the properties.json
                # for wedge_rescue_applied flags.
                try:
                    from ma_poc.observability.events import EventKind as _EK
                    from ma_poc.observability.events import emit as _emit
                    _emit(
                        _EK.WEDGE_RESCUE_RETRY_RESOLVED,
                        _rt.property_id,
                        resolution=(
                            "UPGRADED_TO_SUCCESS" if _upgraded
                            else "RETRY_ALSO_FAILED"
                        ),
                        units=len(_rr.get("units") or []),
                        verdict=_rr_v or "UNKNOWN",
                    )
                except Exception:
                    pass
            log.info(
                "Wedge-rescue: %d/%d retries upgraded to SUCCESS",
                _upgrade_count, len(_retry_tasks),
            )

    # Persist the run-level state-store exactly once after all properties
    # complete.  A single save amortises the per-property I/O cost (was
    # O(n × 2 × file-size) per run; now O(1)).
    if run_state_store is not None:
        try:
            run_state_store.save()
        except Exception as _sse:
            log.warning("state_store.save() failed after run: %s", _sse)

    # Collect results and write output
    properties: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            log.error("Task returned exception: %s", r)
            continue
        properties.append(r)

    # Merge with any properties.json already in run_dir so partial / resumed
    # runs (e.g. --start-index 100 after a prior --start-index 0 --limit 100)
    # don't clobber the earlier batch. Reports run off the merged list so the
    # totals reflect everything in the run dir, not just this invocation.
    properties_path = run_dir / "properties.json"
    merged_properties = _merge_with_existing_properties(properties_path, properties)
    _write_properties_incremental(properties_path, merged_properties)

    # Run-wide LLM aggregate — writes {run_dir}/llm_report.json with the
    # per-property cost breakdown the frontend reads. No-op when no LLM
    # calls fired. Must run before report.json so any future SLO consumers
    # can pick up LLM totals from a single source of truth.
    if all_llm_interactions:
        try:
            from ma_poc.llm.interaction_logger import write_run_summary as _write_llm_run_summary

            _write_llm_run_summary(all_llm_interactions, run_dir)
            _total_cost = sum(i.get("cost_usd", 0.0) for i in all_llm_interactions)
            _unique_props = len({i.get("property_id") for i in all_llm_interactions})
            log.info(
                "LLM report: %d calls across %d propert%s | total cost=$%.5f | → %s",
                len(all_llm_interactions),
                _unique_props,
                "y" if _unique_props == 1 else "ies",
                _total_cost,
                run_dir / "llm_report.json",
            )
        except Exception as exc:
            log.warning("LLM run summary write failed: %s", exc)

    # Run-level reporting. Flush the event ledger first so events.jsonl
    # contains every fetch.bot_blocked / fetch.captcha_detected emitted
    # this run — the run report scans that file to produce the
    # bot_blocked_properties.json artifact.
    try:
        ledger = getattr(events, "_ledger", None)
        if ledger is not None:
            ledger.flush()
    except Exception as exc:  # noqa: BLE001
        log.warning("event ledger flush before reporting failed: %s", exc)
    cost_rollup = cost_ledger.total()
    # ``run_dir`` consulted by slo_check so events.jsonl serves as the
    # authoritative secondary source for per-property verdicts. Keeps
    # the success-rate SLO honest when _meta.verdict is dropped by a
    # downstream serialisation step (Bug A v0.2).
    slo_violations = slo_check(cost_rollup, merged_properties, run_dir=run_dir)
    report = build_run_report(merged_properties, run_dir, today, cost_rollup, slo_violations)

    # Cleanup
    cost_ledger.close()
    frontier.close()
    cond_cache.close()
    events.shutdown()

    log.info(
        "Jugnu run complete: this batch=%d, run-dir total=%d, failed=%d",
        len(properties),
        len(merged_properties),
        report["totals"]["failed"],
    )
    return report


async def _try_rentcafe_direct(
    task: Any,
    profile: Any,
    csv_row: dict[str, Any] | None,
) -> tuple[Any, str | None]:
    """F6 — thin wrapper that delegates to the dispatch helper.

    Lives here so the H11 reader-check sees the symbol in
    ``jugnu_runner.py``. The actual logic lives in
    :func:`ma_poc.pms.rentcafe_direct.runner_dispatch.try_rentcafe_direct`
    so it can be tested without importing ``jugnu_runner`` (which would
    trigger ``dotenv.load_dotenv()`` and pollute env vars).
    """
    from ma_poc.pms.rentcafe_direct.runner_dispatch import (
        try_rentcafe_direct as _impl,
    )

    return await _impl(task, profile, csv_row)


#: Verdict shapes that look "wedge-prone" (timed-out / cancelled / unreachable)
#: and therefore qualify for a wedge-rescue HTTP-only retry pass. Captures
#: the three verdict strings the runner stamps on those outcomes, plus the
#: ``SUCCESS_PARTIAL`` verdict that the timeout-rescue path emits when at
#: least one unit was buffered (we still retry if no units survived).
_WEDGE_RESCUE_RETRY_VERDICTS: frozenset[str] = frozenset({
    "PARTIAL",
    "SUCCESS_PARTIAL",
    "FAILED_UNREACHABLE",
})


def stamp_inferred_entry_block(
    original_meta: dict[str, Any],
    rescue_fetch_diagnostic: dict[str, Any] | None,
) -> bool:
    """Retroactively flag the entry as bot-blocked when wedge-rescue
    surfaced a WAF response.

    Shard_10 fix (2026-05-17). When the entry-page RENDER fetch is killed
    by the per-property wallclock, ``fetch/fetcher.py`` builds the
    ``CANCELLED`` FetchResult with ``body=None``. The captcha detector at
    ``fetch/fetcher.py:329`` only runs ``if result.body``, so
    ``entry_captcha_detected`` stays False. ``wedge_rescue_decision``
    reads that false value and returns ``RETRY``. The rescue's HTTP-only
    GET then immediately receives the WAF interstitial and is classified
    BOT_BLOCKED — proof that the entry was always behind a WAF, just
    invisible from the wedged RENDER attempt.

    This helper updates the ORIGINAL record's ``_meta`` so:
      * Telemetry counts captcha-blocked properties correctly even when
        the first fetch never returned a body.
      * Any subsequent rescue pass (none today, but the architecture
        allows it) reads the corrected flag and short-circuits via
        ``SKIP_ENTRY_CAPTCHA``.
      * The verdict_reason can be made specific in downstream analysers
        without re-parsing rescue diagnostics.

    The function is idempotent and pure (no I/O). It mutates
    ``original_meta`` in place and returns True when a flag was stamped.

    Args:
        original_meta: The ``_meta`` dict of the main-pass record. Updated
            in place.
        rescue_fetch_diagnostic: The ``_fetch_diagnostic`` dict from the
            wedge-rescue retry's result. ``None`` is a no-op.

    Returns:
        True when ``original_meta`` was updated, False otherwise.
    """
    if not rescue_fetch_diagnostic:
        return False
    bot_blocked = bool(rescue_fetch_diagnostic.get("bot_blocked"))
    captcha_detected = bool(rescue_fetch_diagnostic.get("captcha_detected"))
    if not (bot_blocked or captcha_detected):
        return False
    original_meta["entry_bot_blocked"] = True
    if captcha_detected:
        original_meta["entry_captcha_detected"] = True
    provider = rescue_fetch_diagnostic.get("captcha_provider")
    if provider:
        original_meta["entry_captcha_provider"] = provider
    original_meta["entry_bot_blocked_inferred_from_rescue"] = True
    return True


def wedge_rescue_decision(
    meta: dict[str, Any],
    *,
    has_units: bool,
) -> str:
    """Decide whether a property's main-pass result qualifies for a
    wedge-rescue HTTP-only retry.

    Pure function — no I/O, easily unit-testable. The return value
    drives a switch in the orchestrator after the main result pool
    completes.

    Returns one of:

      - ``"RETRY"`` — the property's verdict is wedge-prone (timeout /
        cancel / unreachable) AND no units were buffered AND the entry
        fetch was NOT captcha-blocked. A HTTP-only retry is worth
        attempting because the wedge may have been a Chromium IPC death
        or a slow render that GET would bypass.
      - ``"SKIP_ENTRY_CAPTCHA"`` — the verdict is wedge-prone but the
        entry fetch was captcha-blocked. Retrying with GET returns the
        same captcha stub (~200 bytes, 0 text), which then trips the
        LLM_GATE and downgrades the correct ``FAILED_UNREACHABLE``
        verdict to ``FAILED_NO_DATA``. Skip the retry, preserve the
        correct verdict. The caller emits a
        ``WEDGE_RESCUE_RETRY_RESOLVED`` event with
        ``resolution=SKIPPED_ENTRY_CAPTCHA`` so this population is
        visible separately from rescue-attempt counts.
      - ``"NO_RETRY"`` — neither wedge-prone nor captcha-blocked; the
        result is final, no retry needed.

    Args:
        meta: The property record's ``_meta`` dict. Reads ``verdict``,
            ``partial_recovery``, ``scrape_tier_used``,
            ``entry_captcha_detected``, ``entry_bot_blocked``.
        has_units: ``True`` when the property record has at least one
            unit in its ``units`` list. Computed by the caller because
            the meta dict doesn't carry the unit list.

    Returns:
        One of ``"RETRY"`` / ``"SKIP_ENTRY_CAPTCHA"`` / ``"NO_RETRY"``.
    """
    verdict = (meta.get("verdict") or "").upper()
    is_wedge_prone = (
        verdict in _WEDGE_RESCUE_RETRY_VERDICTS
        or meta.get("partial_recovery") is True
        or meta.get("scrape_tier_used") == "FAILED"
    )
    if not is_wedge_prone or has_units:
        return "NO_RETRY"
    entry_captcha_blocked = bool(
        meta.get("entry_captcha_detected") or meta.get("entry_bot_blocked")
    )
    if entry_captcha_blocked:
        return "SKIP_ENTRY_CAPTCHA"
    return "RETRY"


async def _process_property(
    task: Any,
    cost_ledger: Any,
    profile_store: Any,
    frontier: Any,
    dlq: Any,
    data_dir: Path,
    csv_row: dict[str, Any] | None = None,
    run_dir: Path | None = None,
    state_store: Any | None = None,
    schema_version: str = "v1",
    partial_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Process a single property through L1-L4.

    Args:
        task: CrawlTask for this property.
        cost_ledger: CostLedger for recording costs.
        profile_store: Profile store.
        frontier: Frontier for recording outcomes.
        dlq: DLQ for parking/unparking.
        data_dir: Base data directory.
        csv_row: CSV metadata row for this property.
        run_dir: Path to today's run output directory.
        state_store: Shared run-level StateStore (loaded once in run_jugnu,
            saved once after all properties complete).  When None the
            state-store upsert and carry-forward are skipped gracefully.

    Concurrency invariant:
        The ``state_store.upsert_units`` block contains NO await expressions.
        Under AsyncPool (single-thread asyncio) the scheduler cannot interleave
        two coroutines during a synchronous slice, so each property's
        load-mutate sequence is effectively serialised.  If an await is ever
        added inside that block this invariant must be re-evaluated.

    Returns:
        Property result dict (internal format).
    """
    from ma_poc.discovery.carry_forward import should_carry_forward
    from ma_poc.fetch import fetch as jugnu_fetch
    from ma_poc.observability.events import EventKind, emit
    from ma_poc.pms.scraper import scrape_jugnu
    from ma_poc.reporting.verdict import compute as compute_verdict
    from ma_poc.validation.orchestrator import validate

    # F6 — RentCafe direct-path dispatch (H4, H5, H6).
    # Strategy: when the profile says this is a RentCafe property and
    # we either have a cached propertyId or can resolve one, fetch the
    # centralized aggregator API directly and synthesize a FetchResult
    # so the existing scrape_jugnu pipeline + RentCafeAdapter parses
    # the body unchanged (H7). Any failure path falls through to the
    # vanity-domain L1 fetch — H4 invariant.
    fetch_result = None
    rc_direct_property_id: str | None = None

    profile_for_dispatch = None
    try:
        profile_for_dispatch = profile_store.get_profile(task.property_id)
    except Exception:
        profile_for_dispatch = None

    api_provider = ""
    if profile_for_dispatch is not None:
        try:
            api_provider = (
                profile_for_dispatch.api_hints.api_provider or ""
            ).lower()
        except Exception:
            api_provider = ""

    if api_provider == "rentcafe":
        try:
            fetch_result, rc_direct_property_id = await _try_rentcafe_direct(
                task=task,
                profile=profile_for_dispatch,
                csv_row=csv_row,
            )
        except Exception as exc:
            log.warning(
                "rentcafe_direct dispatch failed for %s: %s — falling back",
                task.property_id,
                exc,
            )
            fetch_result = None
            rc_direct_property_id = None

    # H4 — unconditional vanity-domain fallback. Runs whenever the
    # direct path didn't produce a usable result (any failure tier or
    # routing skip).
    if fetch_result is None:
        # L1: Fetch
        fetch_result = await jugnu_fetch(task)
    frontier.mark_attempt(task.url, fetch_result.outcome)

    # Check carry-forward need
    outcome_val = fetch_result.outcome.value
    if not fetch_result.ok():
        should_cf, reason = should_carry_forward(None, fetch_outcome=outcome_val)
        if should_cf:
            # Try carry-forward from prior state
            from ma_poc.discovery.carry_forward import carry_forward_property

            try:
                # Use the run-level shared StateStore when available so
                # carry_forward_property sees index data from this run.
                # Fall back to a minimal unloaded instance for ad-hoc
                # invocations that don't inject one.
                _cf_ss = state_store
                if _cf_ss is None:
                    from ma_poc.core.state_store import StateStore as _SS
                    _cf_ss = _SS(data_dir / "state")
                # Prefer the concrete dated run_dir so carry_forward_property
                # can derive the schema root reliably. Fall back to the
                # "latest" symlink only when run_dir wasn't injected (e.g.
                # ad-hoc invocations without a run context).
                _cf_run_dir = run_dir if run_dir is not None else data_dir / "runs" / "latest"
                cf_record = carry_forward_property(
                    task.property_id, _cf_run_dir, _cf_ss, reason
                )
                if cf_record:
                    # Stamp a SUCCESS verdict so run_report counts this
                    # correctly. Without this the verdict stays None and
                    # the dashboard shows "verdict=None" for carry-forward
                    # properties — confusing because they DO have units.
                    cf_meta = cf_record.setdefault("_meta", {}) or cf_record["_meta"]
                    cf_meta.setdefault("canonical_id", task.property_id)
                    cf_meta["verdict"] = "SUCCESS"
                    cf_meta.setdefault("verdict_reason", "carry_forward_applied")
                    return cf_record
            except Exception:
                pass

    # L3: Extract
    # Bootstrap a COLD profile the first time we see a property so the
    # adapter dispatch has maturity/preferred_tier hints to work with and
    # so the self-learning loop has a target to update below.
    profile = profile_store.get_profile(task.property_id)
    if profile is None and hasattr(profile_store, "bootstrap"):
        profile = profile_store.bootstrap(task.property_id, {}, task.url)
    result = await scrape_jugnu(
        task=task,
        fetch_result=fetch_result,
        page=None,  # Would be provided in full RENDER mode
        profile=profile,
        csv_row=csv_row,
        partial_state=partial_state,
    )

    # Cold-profile recovery retry. PMS providers change without notice
    # (a property can migrate from RentCafe to Entrata between runs); the
    # prior winning URL or cached selectors then misdirect the scraper.
    # When today's extraction returned no units AND yesterday's profile
    # was WARM/HOT (it succeeded recently), retry once with a profile-blind
    # cold scrape so fresh discovery + universal priors get a clean shot.
    # The persisted profile is NOT mutated by scrape_jugnu's cold path —
    # the original profile flows into update_profile_after_extraction below.
    try:
        _has_units = bool(result.get("units"))
        _is_warm_or_hot = False
        if profile is not None and not _has_units:
            from ma_poc.models.scrape_profile import ProfileMaturity as _PM_RTR
            _maturity = getattr(getattr(profile, "confidence", None), "maturity", None)
            _is_warm_or_hot = _maturity in (_PM_RTR.WARM, _PM_RTR.HOT)
        if _is_warm_or_hot:
            log.info(
                "cold-profile retry for %s: warm/hot profile but 0 units extracted — "
                "retrying with force_cold=True",
                task.property_id,
            )
            _cold_result = await scrape_jugnu(
                task=task,
                fetch_result=fetch_result,
                page=None,
                profile=profile,
                csv_row=csv_row,
                partial_state=partial_state,
                force_cold=True,
            )
            # Adopt the cold result only if it actually recovered units.
            # Otherwise keep the original so we don't lose its diagnostic
            # signal (errors, tier_used, fetch_diagnostic).
            if _cold_result.get("units"):
                _cold_result["_cold_retry_applied"] = True
                result = _cold_result
    except Exception as exc:
        log.warning(
            "cold-profile retry failed for %s: %s",
            task.property_id, exc, exc_info=True,
        )

    # F6 (H6/H11) — surface the propertyId we used (resolved or cached)
    # so the profile_updater can persist it. Read by
    # update_profile_after_extraction; only written there when the tier
    # is one of the two RentCafe-direct success codes — H13.
    if rc_direct_property_id is not None:
        result["_rentcafe_property_id"] = rc_direct_property_id

    # ── Profile self-learning loop ────────────────────────────────────
    # After every scrape, update what the profile knows: winning URL,
    # known_endpoints, blocked_endpoints, consecutive_successes/failures,
    # maturity promotion/demotion. Then run drift detection to demote
    # profiles whose extraction regressed (unit count dropped, all rents
    # null, repeated timeouts) — same semantics as daily_runner.
    if profile is not None:
        try:
            from services.drift_detector import apply_drift_demotion, detect_drift
            from services.profile_updater import update_profile_after_extraction

            units_extracted = len(result.get("units") or [])
            profile = update_profile_after_extraction(
                profile,
                result,
                units_extracted,
                profile_store.backing if hasattr(profile_store, "backing") else profile_store,
            )
            drift_detected, reasons = detect_drift(profile, units_extracted, result)
            if drift_detected:
                profile = apply_drift_demotion(profile, reasons)

            # PR 2 (2026-05-10): Channel 4 — null-field-recovery + FieldPatch
            # persistence. Before this PR, recovery ran in the OUTER
            # _process_one AFTER the profile was already saved here, so
            # recovered patches never reached the persistence layer.
            # Hoisting the recovery here populates result["_field_patches"]
            # in time for save_field_patch below. Calling save_field_patch
            # directly (not a second update_profile_after_extraction pass)
            # avoids double-incrementing consecutive_successes / maturity.
            if schema_version == "v2":
                try:
                    formatted_for_recovery = _format_output(result, csv_row or {}, schema_version)
                    await _run_null_field_recovery(
                        result,
                        formatted_for_recovery,
                        run_dir,
                        task.property_id,
                    )
                    # Stash so the outer _process_one reuses it for the
                    # property report instead of re-running _format_output
                    # (saves ~1ms × 5,000 properties / day).
                    result["_v2_formatted"] = formatted_for_recovery

                    # Persist patches surfaced by the recovery.
                    from services.profile_updater import save_field_patch
                    for patch_dict in result.get("_field_patches", []) or []:
                        if isinstance(patch_dict, dict):
                            save_field_patch(profile, patch_dict)
                except Exception as exc:
                    log.warning(
                        "F2 null_field_recovery hoist failed for %s: %s",
                        task.property_id, exc, exc_info=True,
                    )

            if hasattr(profile_store, "save"):
                profile_store.save(profile)
        except Exception as exc:
            # PR 1 (2026-05-10): elevate to log.warning so production logs
            # surface silent persistence regressions, and emit a structured
            # event so the daily analyser's named-fix table counts them.
            # Previously at log.debug — invisible in production INFO logs.
            log.warning(
                "profile update failed for %s: %s",
                task.property_id, exc, exc_info=True,
            )
            try:
                from ma_poc.observability.events import EventKind, emit
                emit(
                    EventKind.PROFILE_UPDATE_FAILED,
                    task.property_id or "unknown",
                    error=str(exc)[:200],
                    error_type=type(exc).__name__,
                )
            except Exception:
                pass

    # L4: Validate
    extract_result = result.get("_extract_result")
    if extract_result:
        validated = validate(extract_result)
        result["_validated"] = validated.to_dict()

        # Record costs
        if hasattr(extract_result, "llm_cost_usd") and extract_result.llm_cost_usd > 0:
            pms = result.get("_detected_pms", {}).get("pms", "unknown")
            tier = result.get("extraction_tier_used", "unknown")
            cost_ledger.record_llm(
                task.property_id,
                pms,
                tier,
                extract_result.llm_cost_usd,
                "gpt-4o-mini",
                0,  # tokens not tracked at this level
            )

    # Verdict
    # RC3 (2026-05-17 regression fix) — pass ``plan_summaries`` so
    # ``compute_verdict`` can return ``SUCCESS_PLAN_LEVEL`` when the
    # extractor produced plan-level rows but no per-apartment units. The
    # D16 unit/plan partition (commit 0a4624c) routes rows lacking a
    # natural identity into ``result["plan_summaries"]``; without this
    # kwarg they reached the verdict layer invisible and the property
    # was misclassified as ``FAILED_NO_DATA`` despite shipping data.
    verdict = compute_verdict(
        fetch_outcome=outcome_val,
        extract_result=extract_result,
        carry_forward_applied=result.get("_meta", {}).get("carry_forward_used", False),
        plan_summaries=result.get("plan_summaries"),
        # 2026-05-18 (Bug #6): pass entry body + final_url so the verdict
        # layer can short-circuit to DEAD_URL when the fetch landed on a
        # vendor-lockout / parked-domain stub (cityclubapartments.com ->
        # nonpayment.spherexx.com etc.). Pre-fix these routed through
        # LLM_GATE_NO_BODY -> FAILED_NO_DATA which is the wrong verdict
        # for a permanently-shut-down property.
        fetch_body=getattr(fetch_result, "body", None) if fetch_result else None,
        fetch_final_url=getattr(fetch_result, "final_url", None) if fetch_result else None,
        # 2026-05-18 (Bug #1): pass the per-hop outcome counters so the
        # verdict layer can promote FAILED_NO_DATA -> FAILED_UNREACHABLE when
        # entry fetched OK but every hop was BOT_BLOCKED (Yardi
        # /conventional/ Cloudflare cluster — ~193 PIDs on 2026-05-18).
        hop_summary=result.get("_hop_summary"),
    )
    meta = result.setdefault("_meta", {})
    meta["canonical_id"] = task.property_id
    meta["verdict"] = verdict.verdict.value
    meta["verdict_reason"] = verdict.reason

    emit(
        EventKind.PROPERTY_EMITTED,
        task.property_id,
        verdict=verdict.verdict.value,
        units=len(result.get("units", [])),
    )

    # ── State-store upsert + UNITS_KEYLESS_HIGH gate ──────────────────────
    # On every successful scrape: (a) persist units into the FS state-store
    # so carry_forward_units() has data on the next failure run, and (b) fire
    # the UNITS_KEYLESS_HIGH issue when > 50 % of units lacked a natural
    # identity anchor (synthetic_key_used > 50 % of input_count).
    #
    # load() and save() happen once at the run boundary in run_jugnu; only
    # the in-memory upsert_units() runs here (no I/O, no await).
    _today_units = result.get("units") or []
    if _today_units and run_dir is not None and state_store is not None:
        from ma_poc.data_provider.dtos import IssueEntry as _IssueEntry
        from ma_poc.data_provider.dtos import UnitDiff as _UnitDiff
        from ma_poc.services.merge_yield import evaluate as _merge_yield_evaluate
        try:
            _run_date_str = run_dir.name  # "YYYY-MM-DD"
            _diff_raw = state_store.upsert_units(task.property_id, _today_units, _run_date_str)

            _diff = _UnitDiff(**_diff_raw)
            _yield_verdict = _merge_yield_evaluate(_diff)
            if _yield_verdict.next_tier_requested:
                _kl_issue = _IssueEntry(
                    severity="WARNING",
                    code="UNITS_KEYLESS_HIGH",
                    message=(
                        f"{_yield_verdict.keyless_ratio:.0%} of units "
                        f"({_diff.synthetic_key_used}/{_diff.input_count}) "
                        "lack a natural identity anchor"
                    ),
                    canonical_id=task.property_id,
                    details={
                        "keyless_ratio": round(_yield_verdict.keyless_ratio, 4),
                        "synthetic_key_used": _diff.synthetic_key_used,
                        "input_count": _diff.input_count,
                    },
                )
                _append_issue_to_run(run_dir, _kl_issue)
                log.info(
                    "UNITS_KEYLESS_HIGH for %s: %.0f%% (%d/%d units unkeyable)",
                    task.property_id,
                    _yield_verdict.keyless_ratio * 100,
                    _diff.synthetic_key_used,
                    _diff.input_count,
                )
        except Exception as _exc:
            log.warning(
                "state_store upsert / keyless gate failed for %s: %s",
                task.property_id, _exc,
            )
            _append_issue_to_run(
                run_dir,
                _IssueEntry(
                    severity="WARNING",
                    code="STATE_STORE_FAULT",
                    message=str(_exc)[:200],
                    canonical_id=task.property_id,
                ),
            )

    # ── F1: Adapter Debugger ──────────────────────────────────────────────
    # Runs once per FAILED_NO_DATA on TIER_1_* tiers. Gated by existing
    # diagnosis file (one diagnosis per property per run is enough).
    _tier_used = ""
    if extract_result is not None:
        _tier_used = getattr(extract_result, "tier_used", "") or ""
    if run_dir is not None and verdict.verdict.value == "FAILED_NO_DATA" and _tier_used.startswith("TIER_1_"):
        try:
            from ma_poc.services.llm_diagnostics import (
                adapter_debugger,
                get_adapter_parser_source,
            )

            _raw_apis = result.get("_raw_api_responses") or []
            _KNOWN_ADAPTERS = {
                "rentcafe",
                "sightmap",
                "appfolio",
                "entrata",
                "onesite",
                "realpage",
                "generic",
            }
            _adapter_name = (
                _tier_used.replace("TIER_1_API_", "").replace("TIER_1_API", "generic").lower()
            ) or "generic"
            if _adapter_name not in _KNOWN_ADAPTERS:
                _adapter_name = "generic"
            _parser_source = get_adapter_parser_source(_adapter_name)
            _diag_dir = run_dir / "llm_diagnostics"
            _diag_path = _diag_dir / f"{task.property_id}_adapter_debug.json"

            if _diag_path.exists():
                log.debug("F1 diagnosis already exists for %s, skipping", task.property_id)
            else:
                _csv_row = csv_row or {}
                for _resp in _raw_apis[:3]:
                    _diag = await adapter_debugger(
                        property_id=task.property_id,
                        adapter_name=_adapter_name,
                        adapter_parser_source=_parser_source,
                        api_response=_resp,
                        property_context={
                            "property_name": str(
                                _csv_row.get("name")
                                or _csv_row.get("Name")
                                or _csv_row.get("proj_name")
                                or ""
                            ),
                            "website": str(_csv_row.get("website") or _csv_row.get("Website") or ""),
                            "city": str(_csv_row.get("city") or _csv_row.get("City") or ""),
                            "state": str(_csv_row.get("state") or _csv_row.get("State") or ""),
                        },
                        output_dir=_diag_dir,
                    )
                    if _diag is not None:
                        log.info(
                            "  F1 diagnosis for %s: %s (can_auto_fix=%s, recoverable=%d units)",
                            task.property_id,
                            _diag.failure_category,
                            _diag.can_auto_fix,
                            _diag.estimated_units_recoverable,
                        )
                        _interaction = getattr(_diag, "_llm_interaction", None)
                        if isinstance(_interaction, dict):
                            result.setdefault("_llm_interactions", []).append(_interaction)
                        break
        except Exception as _exc:
            log.debug("F1 adapter_debugger hook failed for %s: %s", task.property_id, _exc)

    return result


# ---------------------------------------------------------------------------
# Output formatting — v1 / v2
# ---------------------------------------------------------------------------


def _format_output(
    result: dict[str, Any],
    csv_row: dict[str, Any],
    schema_version: str,
) -> dict[str, Any]:
    """Format a raw Jugnu result into the target output schema.

    Sharing contract — both ``_format_v1`` and ``_format_v2`` use
    ``result.setdefault("_meta", {})`` so that after this call returns:

      1. ``result["_meta"]`` exists (created if absent).
      2. The returned dict's ``_meta`` is the **same object** as
         ``result["_meta"]``.

    Mutations to either reference (e.g. the verdict-writer in
    ``_process_property`` that runs *after* the hoisted format call) are
    therefore visible through both. Caller-side initialisation is not
    required. This contract is what prevents Bug A (2026-05-11 cloud-run
    regression where the verdict written after a hoisted ``_format_output``
    call never reached ``properties.json``).

    Args:
        result: Internal result dict from _process_property.
        csv_row: Original CSV row for this property (for field enrichment).
        schema_version: "v1" or "v2".

    Returns:
        Formatted property dict.
    """
    if schema_version == "v2":
        return _format_v2(result, csv_row)
    return _format_v1(result, csv_row)


def _format_v1(result: dict[str, Any], csv_row: dict[str, Any]) -> dict[str, Any]:
    """Format internal result as v1 (46-key schema).

    Produces the same structure as daily_runner.build_property_record but
    without requiring PropertyIdentity — uses CSV row + scrape metadata.

    See ``_format_output`` docstring for the ``_meta`` sharing contract this
    function upholds via ``setdefault``.
    """
    # ``setdefault`` (not ``.get``) so the returned dict's ``_meta`` is the
    # same object as ``result["_meta"]``. See _format_output docstring +
    # docs/2026_05_11_regressions_fix_design.md (Bug A).
    meta = result.setdefault("_meta", {})
    md = result.get("property_metadata") or {}
    units = result.get("units", [])
    # 2026-05-17 Bug A fix — plan-level rows are real extracted data that
    # previously got silently dropped here. ``post_process`` partitions
    # the extracted rows: ``units`` carries per-apartment inventory,
    # ``plan_summaries`` carries floor-plan-level summaries (rows that
    # describe a plan's typical dims + rent range without a per-unit
    # identity). Before this fix, the v1 formatter read ONLY
    # ``result["units"]`` — every plan_summary was discarded at the
    # output boundary. Surface them under ``Floor Plans`` so downstream
    # consumers can see the partition the extractor already maintains.
    plan_summaries = result.get("plan_summaries") or []
    canonical_id = meta.get("canonical_id", "")

    def _csv(key: str) -> Any:
        """Get a cleaned CSV value."""
        v = csv_row.get(key)
        if v in (None, "", "null", "None"):
            return None
        return str(v).strip() if isinstance(v, str) else v

    def _pick(csv_val: Any, scraped_val: Any) -> Any:
        if csv_val not in (None, "", "null", "None"):
            return csv_val
        return scraped_val if scraped_val not in (None, "", "null", "None") else None

    # Compute aggregates from units
    total_units = len(units) if units else None
    avg_sqft = None
    if units:
        sqfts = [u.get("sqft") or u.get("area") or u.get("_sqft") for u in units]
        sqfts = [s for s in sqfts if s and isinstance(s, (int, float)) and s > 0]
        if sqfts:
            avg_sqft = round(sum(sqfts) / len(sqfts))

    rec: dict[str, Any] = {
        "Property Name": _pick(
            _csv("name") or _csv("Property Name"),
            md.get("name") or md.get("title"),
        ),
        "Type": _csv("Type") or _csv("type"),
        "Unique ID": _csv("apartmentid") or _csv("Unique ID") or canonical_id,
        "Property ID": _csv("Property ID") or _csv("apartmentid") or canonical_id,
        "Property Address": _pick(
            _csv("address") or _csv("Property Address"),
            md.get("address"),
        ),
        "City": _pick(_csv("city") or _csv("City"), md.get("city")),
        "State": _pick(_csv("state") or _csv("State"), md.get("state")),
        "ZIP Code": _pick(_csv("zip") or _csv("ZIP Code"), md.get("zip")),
        "Latitude": md.get("latitude"),
        "Longitude": md.get("longitude"),
        "Management Company": _csv("Management Company"),
        "Phone": _pick(_csv("Phone"), md.get("telephone")),
        "Website": _csv("website") or _csv("Website") or result.get("base_url"),
        "Year Built": md.get("year_built"),
        "Stories": md.get("stories"),
        "Total Units": total_units,
        "Average Unit Size (SF)": avg_sqft,
        "Unit Mix": None,
        "First Move-In Date": None,
        "Property Type": _csv("Property Type"),
        "Property Status": _csv("Property Status") or "Active",
        "Property Style": _csv("Property Style") or _csv("Building Type"),
        "Property Image URL": md.get("image_url"),
        "Property Gallery URLs": md.get("gallery_urls") or [],
        "Update Date": date.today().isoformat(),
        # External-only fields (CSV passthrough)
        "Census Block Id": _csv("Census Block Id"),
        "Tract Code": _csv("Tract Code"),
        "Construction Start Date": _csv("Construction Start Date"),
        "Construction Finish Date": _csv("Construction Finish Date"),
        "Renovation Start": _csv("Renovation Start"),
        "Renovation Finish": _csv("Renovation Finish"),
        "Development Company": _csv("Development Company"),
        "Property Owner": _csv("Property Owner"),
        "Region": _csv("Region"),
        "Market Name": _csv("Market Name"),
        "Submarket Name": _csv("Submarket Name"),
        "Asset Grade in Submarket": _csv("Asset Grade in Submarket"),
        "Asset Grade in Market": _csv("Asset Grade in Market"),
        "Lease Start Date": _csv("Lease Start Date"),
        # Units
        "units": units,
        # 2026-05-17 Bug A fix — surface plan-level rows that ``post_process``
        # admits separately. These describe floor-plan-level inventory the
        # extractor saw but couldn't tie to a specific apartment (no
        # available_date / floor / building / real unit_id). Pre-fix they
        # vanished at this boundary; consumers can now count plan-level
        # availability alongside unit-level.
        "Floor Plans": plan_summaries,
        # Metadata
        "_meta": meta,
        "_extract_result": _extract_result_summary(result),
    }
    return rec


def _extract_result_summary(result: dict[str, Any]) -> dict[str, Any] | None:
    """Project ``result["_extract_result"]`` down to a JSON-safe dict.

    Why: ``run_report.py`` and ``slo_watcher.py`` key off
    ``_extract_result.tier_used`` to compute tier_distribution. Without
    this projection the emitted property record has no ``_extract_result``
    key at all (the dataclass lives on the in-process result dict but never
    reaches ``properties.json``), so every run reports ``tier=UNKNOWN``.
    """
    er = result.get("_extract_result")
    if er is None:
        return None
    if isinstance(er, dict):
        tier = er.get("tier_used")
        llm_cost = er.get("llm_cost_usd", 0.0)
    else:
        tier = getattr(er, "tier_used", None)
        llm_cost = getattr(er, "llm_cost_usd", 0.0)
    return {
        "tier_used": tier,
        "llm_cost_usd": llm_cost,
    }


def _format_v2(result: dict[str, Any], csv_row: dict[str, Any]) -> dict[str, Any]:
    """Format internal result as v2 (flat schema with normalized units).

    Uses the same logic as schema_v2.build_v2_property but without
    requiring PropertyIdentity.

    See ``_format_output`` docstring for the ``_meta`` sharing contract this
    function upholds via ``setdefault``.
    """
    # ``setdefault`` (not ``.get``) so the returned dict's ``_meta`` is the
    # same object as ``result["_meta"]``. Mutations performed after this call
    # returns — e.g. the verdict-writer in ``_process_property:761`` running
    # after the hoisted format call at line 691 — propagate into the cached
    # ``_v2_formatted`` and onward into ``properties.json``. Without this,
    # ``properties.json`` carries ``_meta = {}`` and every downstream
    # consumer that reads ``_meta.verdict`` (run_report, slo_watcher,
    # sync_to_pg, retry, …) loses the signal. Root cause of Bug A —
    # docs/2026_05_11_regressions_fix_design.md.
    meta = result.setdefault("_meta", {})
    md = result.get("property_metadata") or {}
    units = result.get("units", [])
    # 2026-05-17 Bug A fix — plan-level rows previously dropped here.
    # ``post_process`` partitions extracted rows into ``units`` (per-
    # apartment inventory) and ``plan_summaries`` (floor-plan-level
    # summaries lacking per-unit identity). The pre-fix v2 formatter
    # silently discarded plan_summaries — for PIDs 20959 (12→6 units),
    # 55317 (8→5 units) the lost rows had valid rent + AVAILABLE status
    # but no ``available_date``, so they were classified as plans. Now
    # they ship under ``floor_plans`` and are visible to consumers.
    plan_summaries = result.get("plan_summaries") or []
    scrape_ts = datetime.now(UTC)

    def _csv(key: str) -> Any:
        v = csv_row.get(key)
        if v in (None, "", "null", "None"):
            return None
        return str(v).strip() if isinstance(v, str) else v

    def _pick(csv_val: Any, scraped_val: Any) -> Any:
        if csv_val not in (None, "", "null", "None"):
            return csv_val
        return scraped_val if scraped_val not in (None, "", "null", "None") else None

    # apartment_id as integer
    aid = _csv("apartmentid") or _csv("apartment_id") or _csv("Unique ID")
    try:
        apartment_id = int(float(str(aid).replace(",", ""))) if aid else None
    except (ValueError, TypeError):
        apartment_id = None

    # Platform / website design
    platform = (
        result.get("platform_detected")
        or (md.get("api_provider") if md else None)
        or meta.get("scrape_tier_used", "")
    )
    _platform_labels = {
        "entrata": "Powered by Entrata",
        "rentcafe": "Powered by RentCafe",
        "appfolio": "Powered by AppFolio",
        "yardi": "Powered by RentCafe (Yardi)",
        "realpage": "Powered by RealPage",
        "sightmap": "Powered by SightMap",
    }
    website_design = _platform_labels.get(str(platform).lower(), platform or None)

    # Concessions — raw text is the source of truth (preserve-and-flag
    # invariant). ``concessions_clean`` is a display-ready variant and
    # ``concessions_structured`` is a deterministic regex-shaped object
    # (None when un-parseable — raw stays). Lazy-import keeps schema_v2
    # cycles out of jugnu.py's tight import path.
    from ma_poc.core.concession_clean import (
        classify_concession_quality,
        clean_concession_text,
    )
    from ma_poc.core.concession_normalize import normalize_concession

    concessions_text = result.get("concessions_text") or md.get("concessions")
    concessions_source_url = result.get("concessions_source_url")
    concessions_clean = clean_concession_text(concessions_text) if concessions_text else None
    concessions_quality = classify_concession_quality(concessions_text) if concessions_text else None
    # Prefer vision-LLM structured output when the capture came via
    # ``vision_banner`` — it already aggregated sentence fragments and
    # read structured terms directly from the image.
    vision_structured = result.get("concessions_vision_structured")
    if isinstance(vision_structured, dict) and vision_structured.get("type"):
        concessions_structured = vision_structured
    else:
        concessions_structured = (
            normalize_concession(
                concessions_clean or concessions_text,
                source=(
                    "IMAGE_BANNER" if result.get("concessions_source") == "vision"
                    else ("URL_PROBE" if concessions_source_url and concessions_source_url != result.get("base_url") else "TEXT")
                ),
            )
            if concessions_text else None
        )

    prop: dict[str, Any] = {
        "apartment_id": apartment_id,
        "proj_name": _pick(
            _csv("name") or _csv("Name"),
            md.get("name") or md.get("title"),
        ),
        "address": _pick(_csv("address") or _csv("Address"), md.get("address")),
        "city": _pick(_csv("city") or _csv("City"), md.get("city")),
        "state": _pick(_csv("state") or _csv("State"), md.get("state")),
        "zip_code": _format_zip(_pick(_csv("zip") or _csv("Zip"), md.get("zip"))),
        "country": md.get("country"),
        "phone": _pick(_csv("Phone") or _csv("phone"), md.get("telephone")),
        "email_address": md.get("email") or md.get("email_address"),
        "website": _csv("website") or _csv("Website") or result.get("base_url"),
        "pmc": _pick(_csv("Management Company") or _csv("pmc"), md.get("management_company")),
        "website_design": website_design,
        # Raw concession banner text — preserve-and-flag invariant.
        "concessions": concessions_text,
        # Best-effort cleaned variant + quality label + structured
        # object. ``concessions_structured`` is None when normalize
        # couldn't confidently parse — raw stays the source of truth.
        "concessions_clean": concessions_clean,
        "_concessions_quality": concessions_quality,
        "concessions_structured": concessions_structured,
        "concessions_source_url": concessions_source_url,
        # 2026-05-21: after per-unit format, run the floor-plan rent
        # join post-pass so units that share a ``floor_plan_id`` with
        # a rent-bearing sibling pick up the sibling's rent_low/high.
        # Adds ~763 unit recoveries / day (10.5 % of TIER_1_API and
        # TIER_1_5_EMBEDDED null-rent units in the 2026-05-19 run) at
        # the cost of one O(N) pre-pass + O(N) post-pass over the unit
        # list — negligible per-property.
        "units": _fill_rent_from_floor_plan_siblings(
            [_format_v2_unit(u, scrape_ts, _v2_property_id_for_unit(meta, apartment_id)) for u in units]
        ),
        # 2026-05-17 Bug A fix — surface plan-level rows (post_process
        # ``plan_summaries`` partition). Pre-fix these were silently
        # dropped at the v2 output boundary; now they ship under
        # ``floor_plans`` so downstream consumers can render plan-level
        # availability separately from per-unit availability.
        "floor_plans": [_format_v2_unit(u, scrape_ts, _v2_property_id_for_unit(meta, apartment_id)) for u in plan_summaries],
        # Keep _meta for internal tracking (stripped on final delivery)
        "_meta": meta,
        "_extract_result": _extract_result_summary(result),
    }
    return prop


def _v2_property_id_for_unit(meta: dict[str, Any], apartment_id: int | None) -> str:
    """Resolve the property_id used to seed the fallback-id hash.

    Prefers ``_meta.canonical_id`` (always set by the Jugnu runner), falls
    back to ``apartment_id`` from the CSV row, then to an empty string.
    The hash collision risk of an empty property_id is bounded — every
    unit in the same property still hashes consistently.
    """
    return str(meta.get("canonical_id") or apartment_id or "").strip()


def _fill_rent_from_floor_plan_siblings(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill missing rent on units that share a ``floor_plan_id`` with a sibling.

    When the extractor captures some units in a floor plan with rent but
    misses others (commonly happens with TIER_1_API and TIER_1_5_EMBEDDED
    paths where rent lives on a per-unit row but a few rows have it null),
    the empty rent slot can be inferred from the floor plan's other units.

    Strategy:
      * First pass — collect ``{floor_plan_id: [rents]}`` from units
        with rent set.
      * Second pass — for each unit with ``rent_low=None`` AND a known
        ``floor_plan_id``, fill from the sibling distribution: take the
        MIN of sibling rent_lows for rent_low, the MAX of sibling
        rent_highs for rent_high. Conservative on the low end; honest
        about the high end.
      * Stamp ``_rent_filled_from_sibling=True`` (private flag) so
        downstream gates can distinguish inferred rent from extracted.

    Pure function — never raises, returns a new list with mutated copies.
    Idempotent: calling twice on the same list produces the same result.

    Diagnostic: ``2026-05-19`` cloud-run scan found 763 / 7,263 null-rent
    units (10.5 %) whose floor plan had rent on a sibling — the recovery
    target for this helper.
    """
    if not units:
        return units

    # Index: floor_plan_id -> (set of rent_lows, set of rent_highs)
    fp_rents: dict[str, tuple[set[float], set[float]]] = {}
    for u in units:
        fp_id = u.get("floor_plan_id")
        if not fp_id:
            continue
        rl = u.get("rent_low")
        rh = u.get("rent_high")
        if (rl is not None and isinstance(rl, (int, float)) and rl > 1) or (
            rh is not None and isinstance(rh, (int, float)) and rh > 1
        ):
            slot = fp_rents.setdefault(fp_id, (set(), set()))
            if isinstance(rl, (int, float)) and rl > 1:
                slot[0].add(float(rl))
            if isinstance(rh, (int, float)) and rh > 1:
                slot[1].add(float(rh))

    if not fp_rents:
        return units

    out: list[dict[str, Any]] = []
    for u in units:
        fp_id = u.get("floor_plan_id")
        rl = u.get("rent_low")
        rh = u.get("rent_high")
        need_low = rl is None or (isinstance(rl, (int, float)) and rl <= 1)
        need_high = rh is None or (isinstance(rh, (int, float)) and rh <= 1)
        if not (need_low or need_high) or fp_id not in fp_rents:
            out.append(u)
            continue
        lows, highs = fp_rents[fp_id]
        if not (lows or highs):
            out.append(u)
            continue
        new_u = dict(u)
        if need_low and lows:
            new_u["rent_low"] = min(lows)
            new_u["_rent_filled_from_sibling"] = True
        if need_high and highs:
            new_u["rent_high"] = max(highs)
            new_u["_rent_filled_from_sibling"] = True
        # If status was missing too, the rent-presence inference at the
        # formatter ran on the per-unit `rent_low_fmt` which was None;
        # now that we've filled rent, re-run the status inference so the
        # sibling-recovered rent also rescues the status.
        if not new_u.get("availability_status") and (new_u.get("rent_low") or new_u.get("rent_high")):
            new_u["availability_status"] = "AVAILABLE"
        out.append(new_u)
    return out


def _format_v2_unit(
    unit: dict[str, Any], scrape_ts: datetime, property_id: str = ""
) -> dict[str, Any]:
    """Format a single unit to v2 schema.

    Phase 1 fixes:
    - Alias ``unit_number`` to ``unit_id`` so API/DOM extractors that emit
      ``unit_number`` (the adapter convention) don't silently lose identity.
    - Parse ``rent_range`` string (e.g. "$1,200 - $1,500") as a fallback when
      numeric ``market_rent_low/high`` are missing. This recovers rent on
      TIER_1_API and TIER_2_JSONLD extractions that only produce the string.
    - Plumb ``lease_term`` / ``move_in_date`` with a broader key fallback so
      parsers can start populating them without another format change.

    Merge-rescue (2026-05): when neither ``unit_id`` nor ``unit_number`` is
    set, derive a stable inferred id from the physical attributes via
    :func:`assign_fallback_unit_id`. This is the single chokepoint for every
    Jugnu unit on its way out, so JSON-LD / Tier-4-LLM / cross-page-merger
    records that previously dropped at upsert time now keep an anchor.
    """
    beds_raw = unit.get("_bedrooms") or unit.get("bedrooms") or unit.get("beds")
    baths_raw = unit.get("_bathrooms") or unit.get("bathrooms") or unit.get("baths")
    fp_name = unit.get("_floor_plan") or unit.get("floor_plan_name") or unit.get("floorplan_name")
    sqft = unit.get("_sqft") or unit.get("sqft") or unit.get("area")

    # unit_id alias: prefer an explicit unit_id but fall back to unit_number
    uid = unit.get("unit_id") or unit.get("unit_number") or unit.get("_unit_number")

    # Phase 5 junk filter: belt-and-braces with the adapter-level filter.
    # If an adapter outside GenericAdapter emitted a CMS-module plan name
    # or a stop-word unit number, scrub them here before the v2 record
    # ships downstream.
    try:
        from ma_poc.pms.adapters._parsing import is_junk_floor_plan, is_junk_unit_number

        if is_junk_floor_plan(fp_name):
            fp_name = None
        if is_junk_unit_number(uid):
            uid = None
    except Exception:
        pass

    # Bed/bath fallback inference from the floor-plan name. Only fills
    # gaps — never overwrites a source value. Drives down the pool of
    # plans whose ``beds``/``baths`` is NULL in ``units``, which is the
    # dominant cause of comparator misses (Phase 2 of the floor-plan gap
    # plan).
    if (beds_raw in (None, "")) or (baths_raw in (None, "")):
        try:
            from ma_poc.pms.adapters._parsing import infer_bed_bath_from_name

            inferred_beds, inferred_baths = infer_bed_bath_from_name(fp_name)
            if beds_raw in (None, "") and inferred_beds is not None:
                beds_raw = inferred_beds
            if baths_raw in (None, "") and inferred_baths is not None:
                baths_raw = inferred_baths
        except Exception:
            pass

    # rent: use the canonical alias resolver so every producer spelling
    # (``rent_low`` v2 canonical, ``market_rent_low`` v1, ``min_rent``
    # MAA, ``minimumrent`` Windsor, ``base_rent``, ``starting_price``, …)
    # lands in one slot. Pre-2026-05-21 the formatter only read
    # ``market_rent_low`` + ``asking_rent``, so adapter paths that emit
    # under any other alias shipped ``rent_low=null`` — the same alias-
    # blind bug class as the date issue fixed 2026-05-20.
    try:
        from ma_poc.extraction.canonical import RENT_HI_KEYS, RENT_LO_KEYS
        from ma_poc.extraction.canonical import get_numeric as _get_num_canon

        rent_lo_raw = _get_num_canon(unit, RENT_LO_KEYS)
        rent_hi_raw = _get_num_canon(unit, RENT_HI_KEYS)
    except Exception:
        # Defensive fallback to the legacy lookup if the canonical
        # module is unavailable for any reason (import error, test stub).
        rent_lo_raw = unit.get("market_rent_low") or unit.get("asking_rent")
        rent_hi_raw = unit.get("market_rent_high") or unit.get("asking_rent")
    if rent_lo_raw is None and rent_hi_raw is None:
        rent_range = unit.get("rent_range")
        if rent_range:
            try:
                from ma_poc.pms.adapters._parsing import parse_rent_range

                rent_lo_raw, rent_hi_raw = parse_rent_range(str(rent_range))
            except Exception:
                pass

    norm_beds = _normalize_beds(beds_raw)
    norm_baths = _normalize_baths(baths_raw)

    # Phase 3: stamp a deterministic floor_plan_id so analytics can
    # collapse unit-level rows back to plan-level rows. Computed from
    # post-normalisation values so two units with the same plan always
    # share the id even when one source emitted "Studio" and another
    # emitted "0".
    try:
        from ma_poc.pms.adapters._parsing import compute_floor_plan_id

        floor_plan_id = compute_floor_plan_id(
            property_id, fp_name, norm_beds, norm_baths
        )
    except Exception:
        floor_plan_id = None

    # availability_status was previously dropped at the v2 boundary —
    # the per-property report MD ships "AVAILABLE | Available 7/4/26"
    # but the v2 unit dict only carried the date column. Pull from the
    # canonical alias set so producer spellings (`status`, `available`,
    # `is_available`, etc.) all collapse to one v2 field. PID 67736 on
    # 2026-05-18 (live210main.com, 308 units) was the trigger — every
    # unit had availability_status="AVAILABLE" but the v2 row didn't
    # carry it forward to downstream consumers.
    try:
        from ma_poc.extraction.canonical import (
            AVAIL_DATE_KEYS,
            AVAIL_STATUS_KEYS,
        )
        from ma_poc.extraction.canonical import get_str as _get_str_canon

        avail_status_raw = _get_str_canon(unit, AVAIL_STATUS_KEYS) or ""
        producer_avail_date = _get_str_canon(unit, AVAIL_DATE_KEYS)
    except Exception:
        avail_status_raw = ""
        producer_avail_date = None

    # The 2026-05-20 PID 67736 regression: extractors (AppFolio
    # SSR adapter, Entrata, several others) emit the producer's date
    # string under the legacy v1 key ``availability_date`` (with the
    # "y"). Pre-fix we only read ``available_date`` (no "y") so the
    # 94% of units carrying their date under the legacy spelling
    # silently shipped ``available_date=null``. The schema gate's
    # ``record.get("availability_date") or record.get("available_date")``
    # path read both, but the gate mutates a copy that the v2
    # formatter never sees.
    #
    # Fix: use the canonical alias resolver so every producer
    # spelling (``available_date``, ``availability_date``,
    # ``availabledate``, ``available_on``, ``readydate``, …) lands
    # in one slot, then re-run the lenient parser.
    raw_available_date = (
        unit.get("available_date_raw")     # already-raw, if upstream set it
        or unit.get("_date_placeholder")   # gate-stashed unparseable literal
        or producer_avail_date             # any producer alias (the common case)
    )
    avail_date_norm = _format_date_str(raw_available_date)
    # 2026-05-21 product call: when the producer emitted a date string we
    # can't normalise to ISO ("Date: Available", "Late August", "Spring
    # 2026", …), fall back to the producer literal in the typed column
    # rather than shipping null. Downstream UI now always shows what the
    # website actually said, and analytics that want strict ISO can
    # still filter on the ``available_date_raw`` column (which is
    # always the verbatim producer string) or pattern-match ISO via
    # regex. The DB column is VARCHAR(32); strings longer than that are
    # clipped by ``_clip_to_column_limits`` at the storage boundary.
    if avail_date_norm is None and raw_available_date:
        fallback = _normalize_raw_date(raw_available_date)
        if fallback:
            # Clip to the typed column width so the storage clipper
            # doesn't have to truncate mid-word in the warning path.
            avail_date_norm = fallback[:32]
    # Pre-compute the rent values so the status inference can read them
    # without recomputing — formatted values are the source of truth for
    # the "is rent present?" decision, not the raw input keys (which
    # might be 0 or sentinel strings the rent normaliser rejects).
    _rent_low_fmt = _format_rent(rent_lo_raw)
    _rent_high_fmt = _format_rent(rent_hi_raw)
    avail_status = _normalize_availability_status(
        avail_status_raw,
        raw_available_date=raw_available_date,
        normalized_available_date=avail_date_norm,
        scrape_ts=scrape_ts,
        rent_low=_rent_low_fmt,
        rent_high=_rent_high_fmt,
    )

    out: dict[str, Any] = {
        "beds": norm_beds,
        "baths": norm_baths,
        "floor_plan_name": fp_name or None,
        "floor_plan_id": floor_plan_id,
        "area": _format_area(sqft),
        "unit_id": str(uid) if uid not in (None, "", "null") else None,
        "rent_low": _rent_low_fmt,
        "rent_high": _rent_high_fmt,
        "date_captured": scrape_ts.strftime("%Y-%m-%d %H:%M:%S"),
        "available_date": avail_date_norm,
        # Producer's literal availability string, preserved verbatim
        # (whitespace-stripped) regardless of whether ``available_date``
        # resolved to ISO. Lets analytics / BI see "Available 7/24" or
        # "Late August" even when the typed column had to be null.
        # Clipped to the units.available_date_raw VARCHAR(64) limit at
        # the storage layer; this layer keeps the full string.
        #
        # Priority order is "preserve what the upstream gave us":
        #   1. An upstream-set ``available_date_raw`` — the schema gate or
        #      an earlier formatter already captured the producer's literal.
        #   2. The gate-stashed ``_date_placeholder`` — gate ran, parser
        #      failed, placeholder is the only surviving original.
        #   3. The ``available_date`` slot — typically already-normalised
        #      ISO from the gate, but on adapter paths that bypass the
        #      gate it may still be the raw producer string.
        "available_date_raw": _normalize_raw_date(
            unit.get("available_date_raw"),
            unit.get("_date_placeholder"),
            # ``raw_available_date`` above already collapsed every
            # producer alias (``availability_date`` v1, ``available_date``
            # v2, ``availabledate``, ``available_on``, …) — reuse it so
            # the raw column captures the producer's literal even when
            # the alias chain landed on the legacy key.
            raw_available_date,
        ),
        "availability_status": avail_status,
        "lease_term": _safe_int_gt1(unit.get("lease_term") or unit.get("_lease_term")),
        "move_in_date": _format_date_str(
            unit.get("move_in_date") or unit.get("_move_in_date")
        ),
    }

    # Merge-rescue: if no natural id survived, derive a stable inferred id
    # from physical attributes. The helper mutates ``out['unit_id']`` in place
    # and returns the resolved id (or None when even the floor plan is
    # missing — those records still skip downstream, but the per-tier "no
    # unit_id" rate drops by ~17K units/run for JSON-LD + LLM tiers).
    if not out["unit_id"]:
        assign_fallback_unit_id(out, property_id)
    return out


def _resolve_source_url(raw_apis: list[dict[str, Any]], target_unit: dict[str, Any]) -> tuple[str, Any]:
    """Find the API response whose body most plausibly contains target_unit's data.

    Searches for target_unit's unit_id / floor_plan_name / unit_number in each
    response body. Falls back to the first non-empty response when no match found.

    Fix 10: short needles (1-3 chars) must appear as a JSON value token, not
    as a bare substring — prevents "1A" matching "availab**1A**ble" or "s"
    matching every string body. Long needles (≥4 chars) use plain substring match.
    """
    if not raw_apis:
        return ("", {})

    long_needles: list[str] = []
    short_needles: list[str] = []
    for k in ("floor_plan_name", "unit_id", "unit_number"):
        v = target_unit.get(k)
        if not v:
            continue
        s = str(v).strip()
        if not s:
            continue
        if len(s) >= 4:
            long_needles.append(s)
        else:
            short_needles.append(s)

    # Short-needle patterns: the value must appear as a JSON string/number value
    # e.g. `": "1A"`, `": 1A,`, `": 1A}`, `":1A"` (whitespace-tolerant).
    import re as _re
    short_patterns = [
        _re.compile(
            r':\s*"' + _re.escape(n) + r'"'
            r'|:\s*' + _re.escape(n) + r'[,}\]\n\r\s]',
            _re.IGNORECASE,
        )
        for n in short_needles
    ]

    def _body_matches(body_str: str) -> bool:
        if long_needles and any(n in body_str for n in long_needles):
            return True
        if short_patterns and any(p.search(body_str) for p in short_patterns):
            return True
        return False

    has_needles = bool(long_needles or short_patterns)
    for resp in raw_apis:
        body = resp.get("body")
        if body is None:
            continue
        try:
            body_str = json.dumps(body) if isinstance(body, (dict, list)) else str(body)
        except (TypeError, ValueError):
            body_str = str(body)
        if has_needles and _body_matches(body_str):
            return (resp.get("url", ""), body)
    # Fallback: first non-empty body
    for resp in raw_apis:
        if resp.get("body"):
            return (resp.get("url", ""), resp.get("body"))
    return ("", {})


def _url_pattern_from(url: str) -> str:
    """Strip scheme/host/query to extract a stable path-only pattern."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.path or url
    except Exception:
        return url


def _f2_has_recoverable_body(raw_apis: list[dict[str, Any]]) -> bool:
    """PR 9 sub-3 (2026-05-10): True if at least one raw_api has a
    non-empty dict or list body. Empty dicts, None bodies, or
    error-shaped responses do NOT contain recoverable fields, so F2
    should skip them rather than burning LLM cost on hopeless cases.
    """
    for entry in raw_apis or []:
        if not isinstance(entry, dict):
            continue
        body = entry.get("body")
        if isinstance(body, dict) and body:
            return True
        if isinstance(body, list) and body:
            return True
    return False


async def _run_null_field_recovery(
    scrape_result: dict[str, Any],
    formatted: dict[str, Any],
    run_dir: Path,
    canonical_id: str,
) -> None:
    """Call LLM null-field recovery on v2 units missing rent_low or unit_id.

    Only runs for Tier-1 adapters with raw API responses captured. Applies
    high-confidence (>=0.85) recoveries in place on the formatted unit dicts.
    Best-effort — never raises.
    """
    try:
        meta = scrape_result.get("_meta", {}) or {}
        extract = scrape_result.get("_extract_result")
        tier = ""
        if extract is not None:
            tier = getattr(extract, "tier_used", "") or ""
        if not tier:
            tier = str(meta.get("scrape_tier_used", "") or "")

        raw_apis = scrape_result.get("_raw_api_responses") or []
        if not tier.startswith("TIER_1_") or not raw_apis:
            return

        units = formatted.get("units") or []
        null_units = [u for u in units if u.get("rent_low") is None or u.get("unit_id") is None]
        if not null_units:
            return

        # PR 9 sub-3 (2026-05-10): tighten F2 precondition. raw_apis being
        # non-empty doesn't mean any API has data — could be all error
        # responses or empty bodies. Skip if no raw_api has a non-empty
        # dict/list body.
        if not _f2_has_recoverable_body(raw_apis):
            log.info(
                "F2 skipped for %s: no raw_api has a non-empty body",
                canonical_id,
            )
            return

        # PR 9 sub-3: when EVERY unit has BOTH rent_low AND unit_id null,
        # this is parser-tier failure (no fields identified at all), not
        # field-level recovery territory. F2 won't help.
        all_units_total_null = bool(units) and all(
            u.get("rent_low") is None and u.get("unit_id") is None
            for u in units
        )
        if all_units_total_null:
            log.info(
                "F2 skipped for %s: every unit has rent_low AND unit_id null "
                "(parser-tier failure, not field-recovery)",
                canonical_id,
            )
            return

        from ma_poc.services.llm_diagnostics import (
            _build_parser_logic_summary,
            null_field_recovery,
        )

        adapter_name = tier.replace("TIER_1_API_", "").lower() or "generic"
        diag_dir = run_dir / "llm_diagnostics"

        property_context = {
            "property_name": str(formatted.get("proj_name") or ""),
            "website": str(formatted.get("website") or ""),
            "city": str(formatted.get("city") or ""),
            "state": str(formatted.get("state") or ""),
        }

        _PATCH_FIELDS = frozenset({
            "rent_low", "rent_high", "asking_rent",
            "market_rent_low", "market_rent_high",
            "unit_id", "unit_number", "floor_plan_name",
            "beds", "bedrooms", "baths", "bathrooms", "sqft",
            "available_date", "availability_date",
        })

        for i, unit in enumerate(null_units[:5]):
            source_url, source_body = _resolve_source_url(raw_apis, unit)
            source_items: list[Any] = []
            if isinstance(source_body, list):
                source_items = source_body
            elif isinstance(source_body, dict):
                for k in ("data", "results", "floorplans", "FloorplanList", "Result"):
                    v = source_body.get(k)
                    if isinstance(v, list):
                        source_items = v
                        break
            fragment = source_items[i] if i < len(source_items) else source_body

            recovery = await null_field_recovery(
                property_id=canonical_id,
                partial_unit=unit,
                source_fragment=fragment,
                tier_used=tier,
                parser_logic_summary=_build_parser_logic_summary(adapter_name, tier),
                property_context=property_context,
                output_dir=diag_dir,
            )
            if recovery is None:
                continue
            interaction = getattr(recovery, "_llm_interaction", None)
            if isinstance(interaction, dict):
                scrape_result.setdefault("_llm_interactions", []).append(interaction)

            patch_payloads = scrape_result.setdefault("_field_patches", [])
            for rf in recovery.recovered_fields:
                if rf.confidence < 0.85 or rf.recovered_value is None:
                    continue
                field_name = rf.field_name
                # Apply in-memory patch
                if field_name == "rent_low" and unit.get("rent_low") is None:
                    try:
                        unit["rent_low"] = _format_rent(rf.recovered_value)
                    except Exception:
                        pass
                elif field_name == "rent_high" and unit.get("rent_high") is None:
                    try:
                        unit["rent_high"] = _format_rent(rf.recovered_value)
                    except Exception:
                        pass
                elif field_name == "unit_id" and unit.get("unit_id") is None:
                    unit["unit_id"] = str(rf.recovered_value)
                elif field_name == "floor_plan_name" and unit.get("floor_plan_name") is None:
                    unit["floor_plan_name"] = str(rf.recovered_value)

                # Phase C2: persist patch for replay on future runs
                if field_name not in _PATCH_FIELDS:
                    continue
                raw_path = (getattr(rf, "source_path", None) or "").lstrip("$").lstrip(".")
                if not raw_path:
                    continue
                env_hash = ""
                try:
                    from ma_poc.models.source import envelope_hash_of
                    env_hash = envelope_hash_of(source_body)
                except Exception:
                    pass
                patch_payloads.append({
                    "api_url_pattern": _url_pattern_from(source_url),
                    "field_name": field_name,
                    "json_path": raw_path,
                    "confidence": float(rf.confidence),
                    "parser_fix": getattr(rf, "parser_fix", None),
                    "_envelope_hash": env_hash,
                })
    except Exception as exc:
        log.debug("F2 null_field_recovery hook failed for %s: %s", canonical_id, exc)


def _write_property_report(
    scrape_result: dict[str, Any],
    property_record: dict[str, Any],
    run_dir: Path,
    canonical_id: str,
    run_date: str,
) -> None:
    """Write a per-property markdown report under ``{run_dir}/property_reports/``.

    Delegates to :func:`scripts.scrape_report.generate_property_report`, the
    same writer ``daily_runner`` uses, so report format stays consistent
    across the two runners. Jugnu passes the formatted v1/v2 record as
    ``property_record`` so the metadata section can render v2-specific
    fields (apartment_id, pmc, website_design, concessions).

    Jugnu has no legacy state-store diff, so ``unit_diff`` is empty. L4
    validation output (``scrape_result["_validated"]``) is translated into
    lightweight issue objects so the validation section still populates.
    Never raises — report generation is best-effort observability.
    """
    try:
        try:
            from scripts.reports.per_property import generate_property_report
        except ImportError:
            from ma_poc.scripts.reports.per_property import generate_property_report  # type: ignore[no-redef]
    except ImportError as exc:
        log.debug("scrape_report unavailable — skipping report for %s: %s", canonical_id, exc)
        return

    try:
        from ma_poc.data_provider.dtos import IssueEntry
    except ImportError:
        from data_provider.dtos import IssueEntry  # type: ignore[no-redef]

    validated = scrape_result.get("_validated") or {}
    issues: list[Any] = []
    for rej in validated.get("rejected") or []:
        msg = rej.get("reason") if isinstance(rej, dict) else str(rej)
        issue = IssueEntry(
            severity="ERROR",
            code="VALIDATION_REJECTED",
            message=str(msg)[:200],
            canonical_id=canonical_id,
        )
        issues.append(issue)
        _append_issue_to_run(run_dir, issue)
    for fl in validated.get("flagged") or []:
        msg = fl.get("flag") if isinstance(fl, dict) else str(fl)
        issue = IssueEntry(
            severity="WARNING",
            code="VALIDATION_FLAGGED",
            message=str(msg)[:200],
            canonical_id=canonical_id,
        )
        issues.append(issue)
        _append_issue_to_run(run_dir, issue)

    unit_diff: dict[str, list] = {
        "new": [],
        "updated": [],
        "unchanged": [],
        "disappeared": [],
    }

    try:
        generate_property_report(
            scrape_result=scrape_result,
            property_record=property_record,
            unit_diff=unit_diff,
            per_prop_issues=issues,
            run_dir=run_dir,
            canonical_id=canonical_id,
            run_date=run_date,
        )
    except Exception as exc:
        log.warning("property report generation failed for %s: %s", canonical_id, exc)


def _append_issue_to_run(run_dir: Path, issue: Any) -> None:
    """Append one IssueEntry JSON line to {run_dir}/issues.jsonl.

    Never raises — issue writes are best-effort observability.
    """
    try:
        issues_path = run_dir / "issues.jsonl"
        issues_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(issue.model_dump(mode="json"), ensure_ascii=False)
        with open(issues_path, "a", encoding="utf-8") as _f:
            _f.write(line + "\n")
    except Exception as exc:
        log.debug("_append_issue_to_run failed: %s", exc)


def _make_failed_record(
    property_id: str,
    url: str,
    error: str,
    schema_version: str,
    csv_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a failed property record in the appropriate schema.

    Args:
        property_id: Canonical property ID.
        url: Property URL.
        error: Error message.
        schema_version: "v1" or "v2".
        csv_row: Optional CSV row dict (output of ``CatalogRow.as_csv_row()``).
            When provided, CSV identity fields (name, address, city, state,
            zip, phone, pmc) are carried into the failed record so the
            downstream upsert into the ``properties`` table doesn't clobber
            previously-good data with NULL (and so first-time-failing
            properties still get a real name+address in the DB).

    Returns:
        Failed property dict.
    """
    meta = {
        "canonical_id": property_id,
        "scrape_tier_used": "FAILED",
        "scrape_errors": [error],
        "carry_forward_used": False,
    }
    csv_row = csv_row or {}
    # Helper: pick the first non-empty value across V2 + Title-Case CSV aliases.
    def _from_csv(*keys: str) -> Any:
        for k in keys:
            v = csv_row.get(k)
            if v not in (None, ""):
                return v
        return None

    if schema_version == "v2":
        try:
            apartment_id = int(property_id)
        except (ValueError, TypeError):
            apartment_id = None
        return {
            "apartment_id": apartment_id,
            "proj_name": _from_csv("proj_name", "Property Name", "name", "Name"),
            "address": _from_csv("address", "Property Address", "Address"),
            "city": _from_csv("city", "City"),
            "state": _from_csv("state", "State"),
            "zip_code": _from_csv("zip_code", "ZIP Code", "zip", "Zip"),
            "country": _from_csv("country", "Country"),
            "phone": _from_csv("phone", "Phone"),
            "email_address": _from_csv("email_address", "Email"),
            "website": url,
            "pmc": _from_csv("pmc", "Management Company"),
            "website_design": None,
            "concessions": None,
            "units": [],
            "_meta": meta,
        }
    return {
        "_meta": meta,
        "units": [],
        "Property Name": _from_csv("Property Name", "proj_name", "name", "Name"),
        "Property Address": _from_csv("Property Address", "address", "Address"),
        "City": _from_csv("City", "city"),
        "State": _from_csv("State", "state"),
        "ZIP Code": _from_csv("ZIP Code", "zip_code", "zip", "Zip"),
        "Phone": _from_csv("Phone", "phone"),
        "Management Company": _from_csv("Management Company", "pmc"),
        "Website": url,
    }


# ---------------------------------------------------------------------------
# V2 formatting helpers
# ---------------------------------------------------------------------------


def _normalize_beds(val: Any) -> int | None:
    """Convert bedroom value to integer. Studio -> 0, clamp [0, 7].

    Returns ``None`` when the source emitted nothing. Previously this
    defaulted to 0, which silently collapsed "studio confirmed" and
    "not extracted" into the same value — making it impossible to spot
    upstream parser gaps in the downstream data.
    """
    if val is None or val == "":
        return None
    s = str(val).strip().lower()
    if s in ("studio", "s"):
        return 0
    try:
        return max(0, min(int(float(s)), 7))
    except (ValueError, TypeError):
        return None


def _normalize_baths(val: Any) -> float | None:
    """Convert bathroom value to nearest 0.5 multiple, clamp [0, 10].

    Returns ``None`` when the source emitted nothing (same rationale as
    ``_normalize_beds``). Previously defaulted to 1.0.
    """
    if val is None or val == "":
        return None
    try:
        n = float(str(val).strip())
        return max(0.0, min(round(n * 2) / 2, 10.0))
    except (ValueError, TypeError):
        return None


def _format_zip(val: Any) -> str | None:
    """Extract first 5 digits from a ZIP code."""
    if val is None:
        return None
    s = str(val).strip()
    m = _re.search(r"\d{5}", s)
    if m:
        return m.group(0)
    digits = _re.sub(r"\D", "", s)
    return digits.zfill(5)[:5] if digits else None


#: Producer wrappings around rent values — "From $1,450", "Starting at",
#: "As low as 1450/mo". Stripped greedily from the start; case-insensitive.
_RENT_PREFIX_RE = _re.compile(
    r"^(?:from|starting[\s\-]?(?:at|from)?|as[\s\-]?low[\s\-]?as|"
    r"only|just|priced[\s\-]?at|rent[\s\-]?from)[\s:\-]+",
    _re.IGNORECASE,
)

#: Producer suffixes that don't change the rent amount — "/month",
#: "per mo", "USD". Stripped after prefix removal.
_RENT_SUFFIX_RE = _re.compile(
    r"[\s\-]*(?:/?(?:month|mo|monthly)|per[\s\-]?(?:month|mo)|usd|cad|\+)\b.*$",
    _re.IGNORECASE,
)

#: Placeholders that explicitly mean "no rent available". Distinct from a
#: format mismatch — both map to None but the producer's intent is
#: preserved by the explicit match.
_RENT_ABSENT_TOKENS: frozenset[str] = frozenset({
    "call", "contact", "inquire", "call for price", "call for pricing",
    "tbd", "tba", "n/a", "na", "varies", "market", "market rent",
    "see leasing office", "-", "--", "0", "$0",
})


#: Canonical map of producer status strings to the v2 enumeration.
#: Producers emit a wide variety of words ("AVAILABLE", "Available now",
#: "Open", "Vacant", "Leased", "Reserved") — collapse them here so
#: downstream gates see a stable vocabulary. Unknown values pass through
#: uppercased so we don't lose unforeseen producer signals.
#:
#: 2026-05-20: added the long-tail forms observed in the cloud run telemetry
#: (``available_ready`` from RealPage feeds, ``schema.org/InStock`` style
#: URLs from JSON-LD extractors). The ``_STATUS_NORMALIZE_RE`` helper
#: handles Schema.org URLs by stripping to the local-name suffix before
#: lookup.
_AVAILABILITY_STATUS_MAP: dict[str, str] = {
    "available": "AVAILABLE",
    "avail": "AVAILABLE",
    "vacant": "AVAILABLE",
    "open": "AVAILABLE",
    "ready": "AVAILABLE",
    "now": "AVAILABLE",
    "true": "AVAILABLE",
    "yes": "AVAILABLE",
    "y": "AVAILABLE",
    "1": "AVAILABLE",
    "available_ready": "AVAILABLE",
    "availableready": "AVAILABLE",
    "ready_now": "AVAILABLE",
    "readynow": "AVAILABLE",
    "in_stock": "AVAILABLE",
    "instock": "AVAILABLE",
    "preorder": "COMING_SOON",
    "pre_order": "COMING_SOON",
    "unavailable": "UNAVAILABLE",
    "unavail": "UNAVAILABLE",
    "leased": "UNAVAILABLE",
    "rented": "UNAVAILABLE",
    "occupied": "UNAVAILABLE",
    "taken": "UNAVAILABLE",
    "reserved": "UNAVAILABLE",
    "pending": "UNAVAILABLE",
    "applied": "UNAVAILABLE",
    "false": "UNAVAILABLE",
    "no": "UNAVAILABLE",
    "n": "UNAVAILABLE",
    "0": "UNAVAILABLE",
    "out_of_stock": "UNAVAILABLE",
    "outofstock": "UNAVAILABLE",
    "soldout": "UNAVAILABLE",
    "sold_out": "UNAVAILABLE",
    "discontinued": "UNAVAILABLE",
    "waitlist": "WAITLIST",
    "wait list": "WAITLIST",
    "wait-list": "WAITLIST",
    "limitedavailability": "WAITLIST",
    "limited_availability": "WAITLIST",
    "coming soon": "COMING_SOON",
    "comingsoon": "COMING_SOON",
    "coming_soon": "COMING_SOON",
    "future": "COMING_SOON",
    "tba": "UNKNOWN",
    "tbd": "UNKNOWN",
    "n/a": "UNKNOWN",
    "na": "UNKNOWN",
}

#: Schema.org availability URIs land in the status slot when a JSON-LD
#: extractor copies the value through verbatim. Strip everything up to
#: the local name (``https://schema.org/InStock`` → ``instock``) so the
#: lowercase lookup in ``_AVAILABILITY_STATUS_MAP`` resolves correctly.
_SCHEMA_ORG_AVAILABILITY_RE = _re.compile(
    r"^https?://schema\.org/(.+?)/?$", _re.IGNORECASE
)


def _normalize_availability_status(
    raw: str,
    *,
    raw_available_date: Any = None,
    normalized_available_date: str | None = None,
    scrape_ts: datetime | None = None,
    rent_low: Any = None,
    rent_high: Any = None,
) -> str | None:
    """Canonicalise a producer status string into AVAILABLE/UNAVAILABLE/etc.

    Order of precedence:
        1. Explicit producer string via the alias map — fast path.
        2. Implicit "available now" signal: ``raw_available_date`` is a
           known "now" token AND we normalised it to today's date. This
           covers AppFolio's pattern of emitting ``"Available Now"`` in
           the date field WITHOUT a separate status field; without this
           fallback the v2 row would lose the AVAILABLE signal.
        3. Implicit availability from a future date: when an
           ``available_date`` resolved to today or later but no explicit
           status was set, treat as AVAILABLE.
        4. **Implicit availability from rent presence** (2026-05-21): when
           the unit has rent but no explicit status and no date signal,
           default to AVAILABLE. The reasoning: the unit reached the v2
           output because the extractor pulled it from a property
           website's availability feed (``/availability``,
           ``/units``, embedded SSR inventory, …) with a rent value
           attached. Producers don't list rent for occupied units they
           aren't trying to lease. In the 2026-05-19 cloud run, 12,346
           of 14,084 TIER_1_API null-status units (87.7 %) had rent set;
           99.6 % of TIER_1_API null-status units overall had rent —
           the missing status is a producer-side gap, not a "we don't
           know" signal.
        5. None when there's no signal at all (rather than guessing).

    Unknown explicit values pass through uppercased so we don't drop
    producer signals we haven't seen yet.
    """
    raw_s = (raw or "").strip().lower()
    if raw_s:
        # Schema.org URI passthrough: JSON-LD extractors sometimes copy
        # ``offers[].availability: "https://schema.org/InStock"`` verbatim
        # into the status slot. Strip to the local name before lookup so
        # the alias map resolves cleanly instead of shipping the URL.
        schema_match = _SCHEMA_ORG_AVAILABILITY_RE.match(raw_s)
        if schema_match:
            raw_s = schema_match.group(1).lower()
        mapped = _AVAILABILITY_STATUS_MAP.get(raw_s)
        if mapped is not None:
            return mapped
        # Unknown producer string — keep it, but normalise casing so
        # downstream string comparisons are stable.
        return raw_s.upper()

    # No explicit status — try to infer from the date field.
    raw_date = str(raw_available_date or "").strip().lower()
    raw_date = _DATE_PREFIX_RE.sub("", raw_date).strip()
    if raw_date in _DATE_NOW_TOKENS:
        return "AVAILABLE"
    if normalized_available_date and scrape_ts is not None:
        try:
            avail = datetime.strptime(normalized_available_date, "%Y-%m-%d").date()
            if avail >= scrape_ts.date():
                return "AVAILABLE"
        except (ValueError, TypeError):
            pass

    # 2026-05-21: rent-presence inference. A unit that reached the v2
    # output with a non-trivial rent value was, by definition, pulled
    # from an availability feed — producer just didn't stamp the
    # status column. ``> 1`` mirrors ``_format_rent``'s sanity floor;
    # bools are excluded because ``True > 1`` evaluates True in Python.
    for rv in (rent_low, rent_high):
        if rv is None or isinstance(rv, bool):
            continue
        try:
            if float(rv) > 1:
                return "AVAILABLE"
        except (TypeError, ValueError):
            continue

    return None


def _format_rent(val: Any) -> float | None:
    """Clean a rent value to a float dollar amount. None if unparseable.

    Accepts:
        - int / float scalars (passed through if > 1)
        - "$1,450", "1450.00", "1,450 USD" — currency symbols and grouping
          stripped via _money_to_int-style cleanup
        - Producer prefixes: "From $1,450", "Starting at 1450", "As low
          as $1,200"
        - Producer suffixes: "$1,450/month", "1450/mo", "1450 per month"
        - Range strings: "$1,200 - $1,500" -> 1200.0 (low end). This
          mirrors how unit-level callers want the LOW value when only a
          range is available; the v2 ``rent_high`` field receives the
          high end via a separate code path that picks up
          ``market_rent_high``.
    Returns None for empty / placeholder ("Call", "TBD", "Inquire") /
    format mismatch.

    Origin: same incident chain as :func:`_format_date_str` — adapter
    output is permissive but the v2 normaliser was strict, dropping
    legitimate values. Hardened 2026-05-19 in tandem with the date
    normaliser; same regex-prefix-stripping approach.
    """
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        # bool is an int subclass; True / False are not rents.
        return None
    if isinstance(val, (int, float)):
        return float(val) if val > 1 else None

    s = str(val).strip()
    if not s:
        return None
    if s.lower() in _RENT_ABSENT_TOKENS:
        return None

    s = _RENT_PREFIX_RE.sub("", s).strip()
    s = _RENT_SUFFIX_RE.sub("", s).strip()

    # Range "$1,200 - $1,500" / "1200-1500" — take the low end. We split
    # on a hyphen with optional surrounding whitespace; a bare hyphen
    # inside a number ("12-34") won't appear in rents because real
    # values are >= 200.
    range_match = _re.match(r"^(.+?)[\s]*[-–—][\s]*(.+)$", s)
    if range_match:
        s = range_match.group(1).strip()

    cleaned = _re.sub(r"[^\d.]", "", s)
    if not cleaned or cleaned == ".":
        return None
    try:
        n = float(cleaned)
        return n if n > 1 else None
    except (ValueError, TypeError):
        return None


#: Producer suffixes attached to sqft values — "sqft", "sq ft", "square
#: feet", "SF". Stripped (case-insensitive) before numeric coercion so
#: ``"850 sqft"`` parses cleanly. Greedy match to consume trailing
#: punctuation / unit-marker characters.
_AREA_SUFFIX_RE = _re.compile(
    r"[\s\-]*(?:square[\s\-]?(?:feet|foot|ft)|sq\.?[\s\-]?ft\.?|"
    r"sqft|sf|s\.?f\.?|ft²|ft2)\b.*$",
    _re.IGNORECASE,
)


def _format_area(val: Any) -> int:
    """Convert sqft to int. Keeps -1 as the "absent" sentinel.

    Sanity bounds: a real apartment floor-plan area is between 150 and 10,000
    sqft. Anything outside that is garbage (bedroom counts, floor numbers,
    truncated values like "070") and gets coerced to -1. Previously any
    positive integer was accepted, which is why the 2026-04-19 run had area
    values of 9, 12, 50, 70, 100, etc. passed through as "successful".

    Beyond integer/float coercion, accepts:
        - ``"850 sqft"``, ``"850 sq ft"``, ``"850 square feet"``,
          ``"850 SF"``, ``"850 ft²"`` — suffix stripped via
          :data:`_AREA_SUFFIX_RE` before numeric coercion.
        - Range strings ``"850 - 950 sqft"`` — takes the LOW end, mirroring
          the rent-range handling so unit-level analytics get a
          consistent "smallest claimable" value.
    """
    if val is None or val == -1:
        return -1
    s = str(val).strip()
    if not s or s == "-1":
        return -1

    s = _AREA_SUFFIX_RE.sub("", s).strip()
    range_match = _re.match(r"^(.+?)[\s]*[-–—][\s]*(.+)$", s)
    if range_match:
        s = range_match.group(1).strip()

    cleaned = _re.sub(r"[^\d.]", "", s)
    if not cleaned or cleaned == ".":
        return -1
    try:
        n = int(float(cleaned))
    except (ValueError, TypeError):
        return -1
    if 150 <= n <= 10_000:
        return n
    return -1


def _format_date_str(val: Any, *, today: date | None = None) -> str | None:
    """Thin back-compat wrapper over :func:`ma_poc.extraction.dates.format_loose_date`.

    Kept under the underscore-prefixed name because existing tests import
    this symbol directly off the jugnu module. The implementation moved
    to ``ma_poc.extraction.dates`` so the L4 schema gate can share it
    without an upstream ``scripts``-import circularity.

    Origin: PID 67736 (live210main.com, AppFolio, 308 units) on
    2026-05-18 emitted ``"Available 7/4/26"`` for every available unit;
    the pre-fix function returned None for all of them so the v2 output
    shipped ``available_date=null`` despite the source having the data.
    """
    return _format_loose_date_impl(val, today=today)


def _normalize_raw_date(*candidates: Any) -> str | None:
    """Return the first candidate as a clean (whitespace-collapsed) string, or None.

    Used to populate ``available_date_raw`` on the V2 unit — preserves
    the producer's literal availability string verbatim (apart from
    whitespace normalisation) so analytics can see "Available 7/24",
    "Late August", etc. even when the typed ``available_date`` had to
    be null. Returns None when no candidate yields a non-empty string.
    """
    for c in candidates:
        if c is None:
            continue
        s = str(c).strip()
        if not s:
            continue
        # Collapse internal whitespace so DOM-injected ``\n\t`` runs
        # don't bloat the column. Storage layer clips to 64 chars.
        return _re.sub(r"\s+", " ", s)
    return None


def _safe_int_gt1(val: Any) -> int | None:
    """Integer > 1 or None."""
    if val is None:
        return None
    try:
        n = int(float(str(val)))
        return n if n > 1 else None
    except (ValueError, TypeError):
        return None


def _merge_with_existing_properties(
    path: Path,
    new_properties: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge ``new_properties`` on top of any existing ``properties.json``.

    Keys on ``_meta.canonical_id``. New entries replace existing ones with
    the same canonical id; entries not touched by this run are preserved.
    This is what makes ``--start-index`` (and crash-resumes) additive
    instead of clobbering prior batches in the same run directory.

    Returns the merged list. Returns ``new_properties`` unchanged if the
    existing file is missing, empty, or malformed — never raises.
    """
    existing: list[dict[str, Any]] = []
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            if raw.strip():
                loaded = json.loads(raw)
                if isinstance(loaded, list):
                    existing = [r for r in loaded if isinstance(r, dict)]
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "Existing %s unreadable (%s) — overwriting with new batch only",
                path,
                exc,
            )
            return list(new_properties)

    if not existing:
        return list(new_properties)

    def _cid(rec: dict[str, Any]) -> str | None:
        meta = rec.get("_meta") or {}
        cid = meta.get("canonical_id")
        return str(cid) if cid else None

    new_by_cid: dict[str, dict[str, Any]] = {}
    new_without_cid: list[dict[str, Any]] = []
    for rec in new_properties:
        cid = _cid(rec)
        if cid:
            new_by_cid[cid] = rec
        else:
            new_without_cid.append(rec)

    merged: list[dict[str, Any]] = []
    seen_cids: set[str] = set()
    for rec in existing:
        cid = _cid(rec)
        if cid and cid in new_by_cid:
            merged.append(new_by_cid[cid])
            seen_cids.add(cid)
        else:
            merged.append(rec)

    for cid, rec in new_by_cid.items():
        if cid not in seen_cids:
            merged.append(rec)

    merged.extend(new_without_cid)

    log.info(
        "properties.json merge: existing=%d, new=%d, replaced=%d, total=%d",
        len(existing),
        len(new_properties),
        len(seen_cids),
        len(merged),
    )
    return merged


def _write_properties_incremental(path: Path, properties: list[dict[str, Any]]) -> None:
    """Atomically write properties JSON.

    Writes to ``{path}.tmp`` then ``os.replace()`` — so a SIGKILL mid-
    write leaves either the prior complete file or nothing, never a
    half-written (unparseable) JSON. The sync step reads this file
    and the shard's data is lost if the parser raises, so atomicity
    matters even though this is normally called once at end-of-run.

    Args:
        path: Output file path.
        properties: Properties to write (already merged with prior contents
            by the caller — this writer is a plain overwrite).
    """
    try:
        import os as _os

        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(properties, indent=2, default=str),
            encoding="utf-8",
        )
        _os.replace(tmp, path)
    except Exception as exc:
        log.warning("Failed to write incremental properties: %s", exc)


class _SimpleProfileStore:
    """Profile store adapter — delegates to a durable backing store.

    In production (DATA_PROVIDER=postgres), the backing store is the
    SqlProfileStore from data_provider.sql.stores so profile state survives
    Cloud Run container teardown. The previous implementation was filesystem-
    only, writing to ``/app/ma_poc/config/profiles/`` — a path that

      • is excluded from the docker image by ``.dockerignore``
        (``config/profiles/`` is listed), so every container started with an
        empty profile dir, AND
      • is ephemeral once the Cloud Run task exits.

    Net effect: every property was bootstrapped COLD on every daily run, so
    ``llm_field_mappings`` and ``dom_hints.field_selectors`` never replayed.
    The profile_replay sub-tier in GenericAdapter recorded
    ``outcome=skipped reason="no saved mappings"`` 462/462 times in
    shard_0 of 2026-05-08 — every property re-paid the LLM tax daily.

    Selection rules:
      • If ``backing`` is provided (an ``IProfileStore``), use it directly.
        Production wires this with ``DataProvider.profiles`` (PG-backed).
      • Otherwise fall back to the filesystem store at ``profiles_dir``.
        This is what local-dev / pytest paths get when no DataProvider is
        wired in — same behaviour as before.

    Save semantics: ``save`` and ``put`` are aliased so this object can be
    passed directly to ``services.profile_updater.update_profile_after_extraction``
    (which calls ``store.save(profile)``) or to any code expecting the
    ``IProfileStore.put`` contract.
    """

    def __init__(
        self,
        profiles_dir: Path,
        *,
        backing: Any | None = None,
    ) -> None:
        # Lazy imports keep the cold-start cost off importers that don't
        # actually exercise the profile loop (e.g. test helpers).
        from services.profile_store import ProfileStore  # type: ignore[import-not-found]

        self._fs_backing = ProfileStore(profiles_dir)
        # When a DataProvider-backed store is supplied (production), prefer
        # it. The FS store is kept as the bootstrap fallback so local-dev
        # without a DB still works unchanged.
        self._backing: Any = backing if backing is not None else self._fs_backing
        self._uses_data_provider = backing is not None

    def get_profile(self, property_id: str) -> Any:
        """Return a ScrapeProfile (not a plain dict). None if not found.

        Tries the durable backing first; on any read error, falls back to
        the FS store so a transient DB blip doesn't tank the run. The
        FS-only path keeps the historical behaviour for local-dev.
        """
        try:
            if self._uses_data_provider:
                # IProfileStore.get is the canonical read API.
                return self._backing.get(property_id)
            return self._backing.load(property_id)
        except Exception as exc:
            log.warning(
                "profile load failed for %s via %s: %s — falling back to FS",
                property_id,
                "data_provider" if self._uses_data_provider else "fs",
                exc,
            )
            if self._uses_data_provider:
                try:
                    return self._fs_backing.load(property_id)
                except Exception:
                    return None
            return None

    def bootstrap(self, property_id: str, meta: dict[str, Any], website: str) -> Any:
        """Create a COLD profile from CSV metadata + URL-based PMS detection.

        Builds the ScrapeProfile directly rather than using
        ``ProfileStore.bootstrap_from_meta`` because that helper references
        fields that drifted out of the current ``DomHints`` model. The
        bootstrap is persisted via ``save()`` so the same code path works
        for both FS and DB backings.
        """
        try:
            from models.scrape_profile import (  # type: ignore[import-not-found]
                ApiHints,
                DomHints,
                NavigationConfig,
                ScrapeProfile,
                detect_platform,
            )

            platform = detect_platform(website) if website else None
            nav = NavigationConfig()
            if website:
                nav.entry_url = website
            api_hints = ApiHints()
            if platform:
                api_hints.api_provider = platform
            profile = ScrapeProfile(
                canonical_id=property_id,
                version=1,
                updated_by="BOOTSTRAP",
                navigation=nav,
                api_hints=api_hints,
                dom_hints=DomHints(),
            )
            self.save(profile)
            return profile
        except Exception as exc:
            log.debug("profile bootstrap failed for %s: %s", property_id, exc)
            return None

    def save(self, profile: Any) -> None:
        """Persist the profile via the durable backing.

        FS backing exposes ``save``; IProfileStore exposes ``put``.
        We dispatch on whichever is wired so callers (notably
        ``profile_updater.update_profile_after_extraction``) can stay
        backing-agnostic.
        """
        try:
            if self._uses_data_provider:
                self._backing.put(profile)
            else:
                self._backing.save(profile)
        except Exception as exc:
            log.warning("profile save failed: %s", exc)
            if self._uses_data_provider:
                # Last-ditch FS write so we don't lose the update entirely
                # on a DB blip. Next sync will reconcile.
                try:
                    self._fs_backing.save(profile)
                except Exception as fs_exc:
                    log.warning("FS fallback save also failed: %s", fs_exc)

    # ``profile_updater`` calls ``store.save(profile)`` on whatever object
    # is passed as the 4th arg. With the legacy FS store that was the
    # ``services.profile_store.ProfileStore`` instance (``self._backing``);
    # with the DB-backed path we need ``self`` so the dispatch logic in
    # ``save()`` (FS vs DB) runs. ``backing`` therefore returns ``self``
    # for the data-provider path and the underlying FS store for the
    # legacy path. Either way the consumer just calls ``.save(profile)``.
    @property
    def backing(self) -> Any:
        return self if self._uses_data_provider else self._fs_backing

    # IProfileStore alias — lets external code that wants the contract
    # interface call ``store.put(profile)`` instead of ``store.save(profile)``.
    def put(self, profile: Any) -> None:
        self.save(profile)


def _build_profile_store(profiles_dir: Path) -> _SimpleProfileStore:
    """Construct the runner's profile store with the right backing.

    Resolution order:
      1. Use ``get_data_provider().profiles`` when a DataProvider is
         configured (DATA_PROVIDER=postgres/sqlite/dual). This is the
         production path and the one that closes the self-learning loop:
         profiles persist in Postgres across Cloud Run task boundaries.
      2. Fall back to the legacy FS-only store at ``profiles_dir``.
         This keeps local-dev (DATA_PROVIDER unset / =filesystem)
         behaving identically to before.

    Failures in step 1 are non-fatal — the runner still produces output,
    just without the durable profile loop. We log loudly so the regression
    is visible in shard logs rather than silent.
    """
    pg_backing: Any = None
    try:
        provider_name = (os.getenv("DATA_PROVIDER") or "filesystem").strip().lower()
        if provider_name in ("postgres", "pg", "sqlite", "dual"):
            from data_provider.factory import (  # type: ignore[import-not-found]
                get_data_provider,
            )

            provider = get_data_provider()
            pg_backing = provider.profiles
            log.info(
                "Profile store wired to data_provider=%s (durable across runs)",
                provider.name,
            )
    except Exception as exc:
        # Fall through to FS — never block the runner because PG wiring
        # blew up. The end-of-shard sync still has its own retry path.
        log.warning(
            "Failed to wire profile store to data_provider; falling back to FS: %s",
            exc,
        )
        pg_backing = None
    return _SimpleProfileStore(profiles_dir, backing=pg_backing)


def main() -> int:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Jugnu integrated runner")
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help=(
            "Optional CSV override. When set, reads the catalog from this "
            "file instead of the DataProvider's catalog (which is the "
            "`properties` table when DATA_PROVIDER=postgres/sqlite). Use "
            "this for dev / back-compat with the legacy CSV workflow."
        ),
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--data-dir", type=Path, default=_MA_POC_ROOT / "data")
    parser.add_argument("--run-date", type=str, default=None)
    parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="Zero-based catalog row index to start scraping from (skips earlier rows).",
    )
    parser.add_argument(
        "--shard-index",
        type=int,
        default=None,
        help=(
            "Zero-based shard index for distributed runs. Requires "
            "--shard-count. Replaces the CSV-slicing pattern previously "
            "used in jugnu_shard_entry.py."
        ),
    )
    parser.add_argument(
        "--shard-count",
        type=int,
        default=None,
        help="Total number of shards in the run.",
    )
    parser.add_argument(
        "--schema-version",
        choices=["v1", "v2"],
        default=None,
        help="Output schema version (default: env SCHEMA_VERSION or v1)",
    )
    parser.add_argument(
        "--force-scrape",
        action="store_true",
        default=False,
        help=(
            "Bypass change-detection for every property; always issue a full "
            "RENDER task. Intended for canary replays and per-property forensics. "
            "Do not use in production shards."
        ),
    )
    args = parser.parse_args()

    if (args.shard_index is None) != (args.shard_count is None):
        parser.error("--shard-index and --shard-count must be provided together")

    schema_version = _resolve_schema_version(args)

    report = asyncio.run(
        run_jugnu(
            csv_path=args.csv,
            data_dir=args.data_dir,
            limit=args.limit,
            run_date=args.run_date,
            schema_version=schema_version,
            start_index=args.start_index,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
            force_scrape=args.force_scrape,
        )
    )

    print(f"Run complete: {report['totals']['succeeded']}/{report['totals']['properties']} succeeded")
    return 0 if report["totals"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
