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
import contextlib
import json
import logging
import os
import re as _re
import sys
import time as _time
import uuid
from dataclasses import replace as _dc_replace
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
from ma_poc.core.identity import (  # noqa: E402, I001  (intentional: must follow the sys.path bootstrap above)
    assign_fallback_unit_id,
)

log = logging.getLogger("jugnu_runner")

# 2026-05-19 capture-first grounding aid. The Entrata/OneSite/RentCafe/Knock
# per-unit API JSON is never browser-exposed (server-rendered / 3rd-party
# widgets), so source_ids field names for those PMS can't be grounded from
# live probing — only from a real intercepted body. Archive a small capped
# SAMPLE of winning raw API responses for those tiers so the NEXT run yields
# groundable JSON. Capped per shard-process per adapter (grounding needs a
# handful, not the fleet); idempotent; non-fatal. Mirrors the existing
# llm_diagnostics dump precedent.
_API_SAMPLE_CAP_PER_ADAPTER = 15
_API_SAMPLE_COUNTS: dict[str, int] = {}
_API_SAMPLE_TIER_MARKERS = ("ENTRATA", "ONESITE", "RENTCAFE", "KNOCK")


# 2026-05-19 JSON-on-GCS profile persistence (interim, no database).
# config/profiles/ is .dockerignored AND Cloud Run task FS is ephemeral, so
# the FS ProfileStore otherwise bootstraps COLD every run (documented bug).
# Syncing the per-property {canonical_id}.json files to/from a stable GCS
# prefix makes the self-learning loop durable across runs with zero DB —
# per-property objects mean parallel shards never contend. Env-gated
# (PROFILE_GCS_PREFIX, e.g. gs://jugnu-canary/profiles/) and fully
# non-fatal: a sync failure must never break or fail the run.
_PROFILE_GCS_PREFIX = os.getenv("PROFILE_GCS_PREFIX", "").strip()
# Watermark captured right after the warm-start pull completes. The end-of-run
# push uploads only profiles whose mtime exceeds this, so a shard re-uploads
# ONLY the ~1/N slice it actually modified — never the whole downloaded tree.
# Without this, parallel shards clobber each other's fresh writes with the
# stale seed copies they each downloaded (last-writer-wins); the 2026-07-19
# canary banked only 310/4,982 profiles (6.2%) because of exactly this race.
_PROFILE_PULL_WATERMARK: float | None = None


def _pull_profiles_from_gcs(profiles_dir: Path) -> None:
    """Warm-start: pull persisted profile JSONs from GCS before processing."""
    global _PROFILE_PULL_WATERMARK
    if not _PROFILE_GCS_PREFIX:
        return
    try:
        from ma_poc.storage import gcs

        n = gcs.download_prefix(_PROFILE_GCS_PREFIX, profiles_dir)
        # Stamp the watermark AFTER the download so every just-downloaded file
        # has mtime <= watermark and is excluded from the push unless the run
        # re-saves it. Small margin added so coarse-resolution filesystems
        # don't misclassify a download that finished in the same clock tick.
        _PROFILE_PULL_WATERMARK = _time.time() + 0.001
        log.info(
            "profile warm-start: pulled %d profiles from %s (push-watermark set)",
            n, _PROFILE_GCS_PREFIX,
        )
    except Exception as exc:  # never block the runner on a sync blip
        log.warning("profile GCS pull failed (cold start this run): %s", exc)


def _push_profiles_to_gcs(profiles_dir: Path) -> None:
    """Persist this run's learned/updated profile JSONs back to GCS.

    Shard-safe: uploads only profiles modified after the warm-start pull
    watermark (i.e. the ones THIS shard actually re-saved), so concurrent
    shards never overwrite each other's learning. When no pull happened
    (cold/local run, watermark None) it uploads everything, unchanged.
    """
    if not _PROFILE_GCS_PREFIX:
        return
    try:
        from ma_poc.storage import gcs

        n = gcs.upload_prefix(
            profiles_dir,
            _PROFILE_GCS_PREFIX,
            only_modified_after=_PROFILE_PULL_WATERMARK,
        )
        log.info(
            "profile persistence: pushed %d modified profiles to %s",
            n, _PROFILE_GCS_PREFIX,
        )
    except Exception as exc:
        log.warning("profile GCS push failed (learning not persisted): %s", exc)


def _profile_push_interval_s() -> float:
    """Seconds between periodic profile flushes. ``0`` disables (end-of-run only)."""
    try:
        return max(0.0, float(os.environ.get("PROFILE_PUSH_INTERVAL_S", "300")))
    except (TypeError, ValueError):
        return 300.0


async def _periodic_profile_push(profiles_dir: Path, interval_s: float) -> None:
    """Flush learned profiles to GCS every *interval_s* until cancelled.

    Why this exists (2026-07-25): profile learning was pushed exactly ONCE, after
    the whole property loop. Profiles are written to the container's local
    ``/tmp``, which dies with the task — so **task completion was the unit of
    durability**. When all 24 tasks of the 5k canary were killed at the Cloud Run
    7200s task timeout, ~4,471 properties' worth of learning was lost and GCS
    still held only the previous day's 251 profiles. A task killed at 99% lost
    100% of its learning.

    Flushing periodically makes a killed task cost only the last interval. The
    upload is watermark-based and shard-safe (see ``_push_profiles_to_gcs``), so
    repeated calls only re-send what this shard actually modified.

    The upload is BLOCKING network I/O, so it runs via ``asyncio.to_thread`` —
    calling it inline would freeze every property coroutine sharing this loop
    (the event-loop starvation this pipeline was just fixed for).

    Never raises: cancellation is expected (the caller cancels at end of run) and
    any push error is already swallowed by ``_push_profiles_to_gcs``.
    """
    while True:
        try:
            await asyncio.sleep(interval_s)
            await asyncio.to_thread(_push_profiles_to_gcs, profiles_dir)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover — defensive only
            log.warning("periodic profile push failed (non-fatal): %s", exc)


def _resurface_stale_profiles(profiles_dir: Path, run_dir: Path) -> None:
    """End-of-run migration detector: re-probe cached hot URLs and invalidate the
    ones that have migrated so the pipeline re-discovers them next run.

    Strided over ``RESURFACE_STRIDE`` days (default 7) — the offset is derived
    from the run date so consecutive days cover the whole cache. Flag-gated
    (``ENABLE_RESURFACE_MAINTENANCE``) and fully non-fatal: a maintenance error
    must never break or fail the run. Runs before the GCS push so the cleaned
    profiles persist.
    """
    from ma_poc.config.feature_flags import enable_resurface_maintenance

    if not enable_resurface_maintenance() or not _PROFILE_GCS_PREFIX:
        return
    try:
        import datetime as _dt

        from ma_poc.scripts.resurface_profiles import resurface

        stride = max(1, int(os.getenv("RESURFACE_STRIDE", "7")))
        try:
            offset = _dt.date.fromisoformat(run_dir.name).toordinal() % stride
        except ValueError:
            offset = 0
        stats = resurface(
            profiles_dir,
            run_dir / "stale_surfaces.jsonl",
            stride=stride,
            offset=offset,
        )
        log.info(
            "resurface maintenance (stride=%d offset=%d): checked=%d alive=%d "
            "migrated=%d (winning=%d endpoints=%d)",
            stride, offset, stats.get("checked", 0), stats.get("alive", 0),
            stats.get("stale_flagged", 0), stats.get("invalidated_winning_url", 0),
            stats.get("invalidated_endpoints", 0),
        )
    except Exception as exc:  # never block the runner on a maintenance blip
        log.warning("resurface maintenance failed (non-fatal): %s", exc)


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


# ── End-of-run transient retry pass (opt-in) ─────────────────────────────────
# Re-runs ONLY the transient-class fetch failures once, in-process, after the
# main pass — natural output consolidation (the retry results overlay the same
# in-memory `results` list, so the single properties.json write already
# reflects them), and by the time a large run finishes the transient condition
# that failed an early property (5xx / timeout / rate-limit / proxy hiccup /
# empty body) has usually cleared. Deliberately EXCLUDES:
#   - BOT_BLOCKED  — won't clear on the same burned proxy pool this soon; that's
#                    the render / proxy-rotation lever, not a time-based retry.
#   - DEAD_URL     — terminal (404 / soft-404 / NXDOMAIN).
#   - HARD_FAIL    — SSL / non-retriable 4xx.
#   - FAILED_NO_DATA — the page was REACHED; empty extraction is extraction-side,
#                    a re-fetch won't help.
# Gated off by default → zero behaviour change until ENABLE_END_OF_RUN_RETRY.
# A single pass only (no loop) → bounded cost on the (small) transient cohort.
_TRANSIENT_RETRY_OUTCOMES: tuple[str, ...] = (
    "TRANSIENT",
    "RATE_LIMITED",
    "PROXY_ERROR",
    "EMPTY_BODY",
)


def _end_of_run_retry_enabled() -> bool:
    """True when the opt-in end-of-run transient retry pass is enabled."""
    return os.getenv("ENABLE_END_OF_RUN_RETRY", "false").strip().lower() == "true"


def _end_of_run_retry_delay_s() -> int:
    """Seconds to wait before the retry pass (0 = immediate). A large run's own
    duration already gives early-failed properties a long natural delay; this
    knob lets rate-limit-heavy fleets add an explicit cool-off."""
    raw = os.getenv("END_OF_RUN_RETRY_DELAY_S", "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _is_transient_fetch_failure(record: dict[str, Any]) -> bool:
    """True when a result record failed with a transient (retriable) fetch
    outcome — the only class worth an immediate second attempt."""
    meta = record.get("_meta") or {}
    if str(meta.get("verdict") or "") != "FAILED_UNREACHABLE":
        return False
    reason = str(meta.get("verdict_reason") or "")
    return any(f"fetch outcome: {o}" in reason for o in _TRANSIENT_RETRY_OUTCOMES)


def _retry_improved(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """True when the retry result is strictly better than the first-pass record.

    A retry replaces the first-pass record only if it becomes a success-class
    verdict WITHOUT losing units, or it simply gains units. The unit-count
    guard is defensive: it prevents a lower-yield retry from ever overwriting a
    richer first-pass record (in practice ``old`` is always a transient failure,
    so this only bites if the helper is reused elsewhere)."""
    from ma_poc.reporting.verdict import verdict_is_success

    old_units = len(old.get("units") or [])
    new_units = len(new.get("units") or [])
    if verdict_is_success((new.get("_meta") or {}).get("verdict")):
        return new_units >= old_units
    return new_units > old_units


def _select_transient_retry_tasks(
    tasks: list[Any], results: list[Any]
) -> list[Any]:
    """Build RETRY CrawlTasks for the transient-failed subset. ``results`` is
    order-aligned with ``tasks`` (pool.map preserves order)."""
    from ma_poc.discovery.contracts import TaskReason

    out: list[Any] = []
    for t, r in zip(tasks, results, strict=False):
        if not isinstance(r, dict):
            continue
        if _is_transient_fetch_failure(r):
            out.append(_dc_replace(t, reason=TaskReason.RETRY))
    return out


def _apply_retry_results(
    tasks: list[Any],
    results: list[Any],
    retry_tasks: list[Any],
    retry_results: list[Any],
) -> list[Any]:
    """Overlay retry results onto the first pass, replacing a record only when
    the retry improved it. Order-preserving; keyed by property_id."""
    retry_by_pid: dict[str, Any] = {}
    for t, rr in zip(retry_tasks, retry_results, strict=False):
        if isinstance(rr, dict):
            retry_by_pid[t.property_id] = rr
    merged: list[Any] = []
    for t, r in zip(tasks, results, strict=False):
        rr = retry_by_pid.get(getattr(t, "property_id", "") or "")
        if rr is not None and isinstance(r, dict) and _retry_improved(r, rr):
            merged.append(rr)
        else:
            merged.append(r)
    return merged


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

    catalog_filters = CatalogFilters(
        start_index=start_index or None,
        shard_index=shard_index,
        shard_count=shard_count,
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
    _profiles_dir = _MA_POC_ROOT / "config" / "profiles"
    # Warm-start BEFORE the store is built so get_profile() sees the
    # pulled JSONs. No-op unless PROFILE_GCS_PREFIX is set; non-fatal.
    _pull_profiles_from_gcs(_profiles_dir)
    profile_store = _build_profile_store(_profiles_dir)

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
        try:
            csv_row = csv_lookup.get(task.property_id, {})
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
        except TimeoutError as exc:
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
            # 2026-05-19: persist the discovered route/profile EVEN WHEN zero
            # units were extracted. Previously this save was gated behind
            # ``if _partial_units`` — so a property that timed out *before*
            # finishing extraction (but *after* discovering the right
            # floorplans URL / selectors) learned nothing, started cold next
            # run, slow-crawled, and timed out again forever (the ~79
            # per-property-timeout dead-zone cohort). Saving the profile
            # unconditionally breaks that vicious cycle: discovery from a
            # timed-out run accelerates the next run even if this one yielded
            # no units. Units are still only persisted when present (no blank
            # rows) — this is route/selector knowledge, not fabricated data.
            #
            # 2026-07-25 RCA: the block above was DEAD. Nothing ever wrote
            # ``partial_state["profile"]`` (verified: no writer anywhere outside
            # tests), so ``_partial_profile`` was always None and 0 "next run
            # starts warm" lines appeared across 366 timeouts in the 5k canary.
            # Timed-out properties therefore stayed COLD forever and re-paid
            # full discovery every run — the vicious cycle the 2026-05-19 note
            # claimed to have broken was still running.
            #
            # The scraper now checkpoints route knowledge into
            # ``partial_state["profile_hints"]`` via
            # ``pms.scraper.checkpoint_partial`` as soon as a page yields units.
            # Load the property's profile here and persist just that knowledge.
            # Deliberately narrow: only ``winning_page_url`` (the URL that
            # actually produced units) is written. Success counters, maturity
            # promotion and tier preference are NOT touched — this run timed
            # out, so it must not look like a success to the profile updater.
            if persist_timeout_route_hints(
                profile_store,
                task.property_id,
                _partial_state,
                unit_count=len(_partial_units),
                profile=_partial_profile,
            ):
                pass  # logged inside the helper
            failed = _make_failed_record(
                task.property_id,
                task.url,
                f"per_property_timeout:{int(PER_PROPERTY_TIMEOUT_SECONDS)}s ({type(exc).__name__})"
                + (f" — {len(_partial_units)} partial units persisted" if _partial_units else ""),
                schema_version,
            )
            # Surface partial units in the failed record so the run report can
            # show partial data rather than a zero-unit timeout row.
            if _partial_units:
                # 2026-07-11 quality sweep: a TIMEOUT salvage with ZERO
                # numeric-rent units is the "phantom roster" shape — a
                # geometry roster (all rows, no prices) salvaged before the
                # price join ran. When any unit carries a false
                # data_gaps=["rent"] / RENT_NOT_PUBLISHED flag, it suppresses
                # the verdict's no_rent_signal → SUCCESS_PLAN_LEVEL downgrade
                # and leaves every row marked AVAILABLE. Neutralise the
                # unearned flag so the salvage verdict computed below lands on
                # SUCCESS_PLAN_LEVEL (held), not an inflated unit-level
                # SUCCESS with phantom available inventory. Gated on
                # zero-numeric-rent so partial salvages that DID capture real
                # prices are left untouched. See _neutralize_unearned_rent_gap.
                if not any(
                    _salvage_unit_has_numeric_rent(_u)
                    for _u in _partial_units
                    if isinstance(_u, dict)
                ):
                    for _u in _partial_units:
                        if isinstance(_u, dict):
                            _neutralize_unearned_rent_gap(_u)
                failed["units"] = _partial_units
                failed.setdefault("_meta", {})["partial_recovery"] = True
                # 2026-07-12 (38198b6 canary QC): format the salvage like
                # every other emitted record. Pre-fix, `return failed`
                # shipped the RAW adapter dicts (rent_range /
                # market_rent_low / bed_label ...) straight into
                # properties.json — schema-inconsistent rows that v2
                # consumers read as null (libertybayclub salvage carried
                # market_rent_low=2390 + real dates, yet rent_low/
                # available_date showed empty downstream). Route through
                # _format_output — the same chokepoint as the success
                # path — so salvaged units get the full v2 transform
                # (rent parse, date resolve, fallback ids, junk filter).
                # _format_v2's setdefault contract keeps failed["_meta"]
                # the SAME object, so the salvage markers above and the
                # verdict stamped below land on the emitted record.
                if schema_version == "v2":
                    try:
                        failed = _format_output(
                            {
                                "units": _partial_units,
                                "base_url": task.url,
                                "_meta": failed["_meta"],
                            },
                            csv_row,
                            schema_version,
                        )
                    except Exception as _fmt_exc:
                        log.warning(
                            "salvage v2-format failed for %s: %s — "
                            "emitting raw units",
                            task.property_id, _fmt_exc,
                        )
            # 2026-05-27: stamp a verdict on the salvage record so run_report
            # and slo_watcher tally these properties correctly. Without this,
            # _meta.verdict is None and ~368 partial-recovery props fall into
            # the silent-success bucket (full-fleet 82.20% reported vs 89.82%
            # adjusted, jugnu-c612-may27 canary). The scrape_tier_used="FAILED"
            # salvage marker is preserved; verdict is a separate field.
            try:
                from ma_poc.reporting.verdict import compute as _compute_verdict
                # Pass partial units as a dict-shaped extract_result so
                # compute() flows past the no-records FAILED branch and
                # applies the rent-signal / inferred-UID downgrade rules
                # to decide SUCCESS vs SUCCESS_PLAN_LEVEL vs PARTIAL.
                # With zero partial units we want FAILED_NO_DATA — keep
                # extract_result=None in that case.
                _salvage_extract: Any = (
                    {"units": _partial_units} if _partial_units else None
                )
                # 2026-07-12: honor the operator-published zero-availability
                # statement seen BEFORE the timeout. scrape_jugnu/_try_link_hop
                # checkpoint the flag into _partial_state (survives coroutine
                # cancellation). Without it, a property whose entry page says
                # "no units available" but times out on a later hop was
                # stamped FAILED_NO_DATA — discarding the operator's
                # authoritative answer. With the flag and zero partial units,
                # compute() returns SUCCESS_NO_AVAILABILITY.
                _salvage_verdict = _compute_verdict(
                    fetch_outcome=None,
                    extract_result=_salvage_extract,
                    units=_partial_units or None,
                    operator_no_availability=bool(
                        _partial_state.get("operator_no_availability")
                    ),
                )
                _salvage_meta = failed.setdefault("_meta", {})
                _salvage_meta["verdict"] = _salvage_verdict.verdict.value
                _salvage_meta["verdict_reason"] = _salvage_verdict.reason
                # 2026-07-12: stamp the winning-hop tier onto _extract_result
                # so this salvage is counted at its true tier. _try_link_hop
                # checkpoints the real tier into _partial_state (survives the
                # coroutine cancellation); _format_output above rebuilt
                # _extract_result as None (the salvage input dict had none),
                # so without this the record ships tier_used=None and a real
                # Tier-1 link-hop recovery (the 231-prop timeout-salvage
                # cohort) is NOT counted as gold. Reporting reads the tier
                # from _extract_result.tier_used (see scripts/CLAUDE.md).
                _salvage_tier = _partial_state.get("tier_used")
                if _salvage_tier and _partial_units:
                    failed["_extract_result"] = {
                        "tier_used": _salvage_tier,
                        "llm_cost_usd": 0.0,
                    }
            except Exception as _vexc:
                log.warning(
                    "partial-recovery verdict compute failed for %s: %s",
                    task.property_id, _vexc,
                )
            return failed
        except Exception as exc:
            log.error("Property %s crashed: %s", task.property_id, exc)
            return _make_failed_record(
                task.property_id,
                task.url,
                str(exc),
                schema_version,
            )

    # Flush profile learning to GCS DURING the run, not only at the end. A task
    # killed mid-run (Cloud Run task timeout, preemption, crash) otherwise loses
    # every profile it learned — see _periodic_profile_push. Cancelled in the
    # finally block; the authoritative end-of-run push still runs below.
    _push_interval = _profile_push_interval_s()
    _profile_pusher: asyncio.Task[None] | None = None
    if _push_interval > 0 and _PROFILE_GCS_PREFIX:
        _profile_pusher = asyncio.create_task(
            _periodic_profile_push(_profiles_dir, _push_interval)
        )
        log.info(
            "profile persistence: periodic push every %.0fs (task-death insurance)",
            _push_interval,
        )
    try:
        results = await pool.map(_process_one, [(t,) for t in tasks])
    finally:
        if _profile_pusher is not None:
            _profile_pusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _profile_pusher

    # End-of-run transient retry pass (opt-in via ENABLE_END_OF_RUN_RETRY).
    # Re-drives ONLY the transient-class fetch failures through the SAME
    # _process_one path once; the merged results feed the single properties.json
    # write below, so recovery is consolidated with no separate run or output
    # merge. Best-effort — a failure here must never sink the whole run.
    if _end_of_run_retry_enabled():
        try:
            retry_tasks = _select_transient_retry_tasks(tasks, results)
            if retry_tasks:
                _delay = _end_of_run_retry_delay_s()
                if _delay > 0:
                    log.info(
                        "end-of-run retry: waiting %ds then re-running %d "
                        "transient-failed properties",
                        _delay, len(retry_tasks),
                    )
                    await asyncio.sleep(_delay)
                else:
                    log.info(
                        "end-of-run retry: re-running %d transient-failed properties",
                        len(retry_tasks),
                    )
                retry_results = await pool.map(
                    _process_one, [(t,) for t in retry_tasks]
                )
                _before = sum(
                    1 for r in results
                    if isinstance(r, dict) and _is_transient_fetch_failure(r)
                )
                results = _apply_retry_results(
                    tasks, results, retry_tasks, retry_results
                )
                _after = sum(
                    1 for r in results
                    if isinstance(r, dict) and _is_transient_fetch_failure(r)
                )
                log.info(
                    "end-of-run retry: recovered %d of %d transient-failed properties",
                    _before - _after, len(retry_tasks),
                )
        except Exception as _eor_exc:  # never break the run on the retry pass
            log.warning("end-of-run retry pass failed (non-fatal): %s", _eor_exc)

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

    # Durable maintenance: re-probe cached hot URLs and invalidate any that have
    # MIGRATED so the next run re-discovers them (success→cache; migration→
    # invalidate+flag). Strided/flag-gated/non-fatal. Runs BEFORE the push so the
    # cleaned profiles persist. See scripts/resurface_profiles.py.
    _resurface_stale_profiles(_profiles_dir, run_dir)

    # Persist this run's profile learnings (winning_page_url, llm field
    # mappings, dom hints) durably so the NEXT run starts warm. No-op
    # unless PROFILE_GCS_PREFIX is set; non-fatal by construction.
    _push_profiles_to_gcs(_profiles_dir)

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


def _unit_level_row_count(result: dict[str, Any]) -> int:
    """Count extracted rows carrying a real (non-empty) ``unit_number`` — i.e.
    genuine per-apartment unit-level rows, vs plan-level floorplan rows (which
    the PP_SSR / plan parsers emit with ``unit_number=""``). Used to decide
    whether a render escalation strictly improved a plan-level result. Never
    raises on malformed input."""
    n = 0
    for u in result.get("units") or []:
        try:
            if str(u.get("unit_number") or "").strip():
                n += 1
        except Exception:
            continue
    return n


def _is_plan_level(result: dict[str, Any]) -> bool:
    """True iff *result* is a SUCCESS that stalled at PLAN level: rows present
    but NONE carrying a real ``unit_number`` (floorplan placeholders). The
    generic trigger for the task #45 plan→unit render lever — platform-agnostic;
    the circuit-breaker (``_plan_render_allowed``), not tier scoping, is what
    keeps legitimately plan-only adapters from being re-rendered forever.
    Mirrors ``_compute_quality_signals``'s PLAN_LEVEL flag definition. Never
    raises."""
    if not (result.get("units") or []):
        return False  # zero-units is the render-on-empty path, not this one
    return _unit_level_row_count(result) == 0


def _is_entrata_plan_level(result: dict[str, Any]) -> bool:
    """True iff *result* is an Entrata SUCCESS that stalled at PLAN level — the
    trigger for the #42 render lever. Such a result has floorplan rows present
    but NONE carrying a real ``unit_number`` (the per-apartment jd-fp roster is
    client-rendered and the static fetch saw only ``--preload`` skeletons).
    Scoped to Entrata via the tier label so other legitimately plan-only
    adapters (generic_plan_text, etc.) are never re-rendered. Never raises."""
    extract_result = result.get("_extract_result")
    tier = (getattr(extract_result, "tier_used", "") or "") if extract_result else ""
    if not tier:
        tier = str(result.get("extraction_tier_used") or "")
    if "ENTRATA" not in tier.upper():
        return False
    return _is_plan_level(result)


def _plan_render_allowed(profile: Any) -> bool:
    """Circuit-breaker for the generic plan→unit render (task #45).

    True (render allowed) when the property has NOT yet proven itself
    plan-only: fewer than ``PLAN_RENDER_MAX_STREAK`` renders attempted-and-held.
    Once the cap is reached the property is treated as verified plan-only and
    the render stops firing — EXCEPT one fresh attempt is re-armed when the
    last attempt is older than ``PLAN_RENDER_REARM_DAYS`` (a fully-leased
    property that re-lists units recovers within a week). None-safe: a missing
    profile or quality block allows the render (COLD property, nothing proven).
    Never raises.
    """
    from ma_poc.config.feature_flags import (
        PLAN_RENDER_MAX_STREAK,
        PLAN_RENDER_REARM_DAYS,
    )

    try:
        q = getattr(profile, "quality", None)
        if q is None:
            return True
        held = int(getattr(q, "plan_render_attempts_held", 0) or 0)
        if held < PLAN_RENDER_MAX_STREAK:
            return True
        last = getattr(q, "last_plan_render_at", None)
        if last is None:
            return True  # cap claims attempts but no timestamp — allow re-probe
        age_days = (datetime.utcnow() - last).total_seconds() / 86400.0
        return age_days >= PLAN_RENDER_REARM_DAYS
    except Exception:
        return True


def _result_tier(result: dict[str, Any]) -> str:
    """The extraction tier string from a scrape result (either shape)."""
    er = result.get("_extract_result")
    tier = (getattr(er, "tier_used", "") or "") if er else ""
    return tier or str(result.get("extraction_tier_used") or "")


async def _encore_plan_render_units(
    task: Any,
    entry_html: str,
    base_url: str,
    fetch_fn: Any,
    *,
    started_monotonic: float,
    budget_guard_s: float,
    max_plans: int,
) -> list[dict[str, Any]]:
    """Encore per-plan render fan-out (task #37 Track 2b). At page=None the
    encoreskyline adapter discovers the ``/floorplans/{slug}/`` URLs from the
    body but can't fetch+click them → 0 units. This renders each plan URL via
    ``fetch_fn`` (which, with INTERACTION_REVEAL on, fires the Check-Availability
    reveal during the render) and parses the post-click roster from the rendered
    body (tag-stripped to approximate innerText). Bounded by ``max_plans`` and
    the elapsed budget guard. Returns post-processed unit dicts (deduped by
    unit_number). Never raises."""
    from ma_poc.fetch.contracts import RenderMode
    from ma_poc.pms.adapters._encoreskyline_units import parse_encoreskyline_units
    from ma_poc.pms.adapters.encoreskyline_template import _plan_urls_from_html

    try:
        plans = _plan_urls_from_html(entry_html or "", base_url)[:max_plans]
    except Exception:
        plans = []
    all_units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for plan_url in plans:
        if (_time.monotonic() - started_monotonic) >= budget_guard_s:
            break
        try:
            rtask = _dc_replace(task, url=plan_url, render_mode=RenderMode.RENDER)
            rf = await fetch_fn(rtask)
        except Exception:
            continue
        if rf is None or not getattr(rf, "ok", lambda: False)() or not getattr(rf, "body", None):
            continue
        body = rf.body
        html = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
        text = _re.sub(r"<[^>]+>", " ", html)  # innerText approximation for the parser
        try:
            parsed = parse_encoreskyline_units(text, plan_url)
        except Exception:
            parsed = []
        for u in parsed:
            k = str(u.get("unit_number") or "").strip()
            if k and k not in seen:
                seen.add(k)
                all_units.append(u)
    if not all_units:
        return []
    try:
        from ma_poc.extraction.post_process import post_process

        pp = post_process(all_units, property_id=getattr(task, "property_id", None))
        return list(pp.admitted) if pp.n_admitted > 0 else []
    except Exception:
        return all_units


def _emit_route_shadow(
    fetch_result: Any,
    result: dict[str, Any],
    task: Any,
    run_dir: Any,
    profile: Any,
    actual_tier: Any,
    actual_verdict: str,
) -> None:
    """Append one route-shadow record (agentic router Phase 2, observe-only).

    Computes what the deterministic router WOULD decide from the fetch signals and
    writes it next to what the pipeline actually did, to ``{run_dir}/route_shadow.jsonl``.
    Never acts, never raises. The divergence between ``router_action`` and
    ``actual_tier``/``actual_verdict`` on the failed cohort is the signal that tells
    us whether wiring the agent to act (Phase 3) is worth it.
    """
    try:
        import dataclasses as _dc
        import json as _json

        from ma_poc.services.route_policy import compute_signals, route

        units = result.get("units") or []
        sig = compute_signals(fetch_result, None, profile, units_extracted=len(units))
        dec = route(sig, profile)
        rec = {
            "property_id": getattr(task, "property_id", "?"),
            "signals": _dc.asdict(sig),
            "router_action": dec.action,
            "router_target": dec.target_field_group,
            "router_rationale": dec.rationale,
            "actual_tier": str(actual_tier) if actual_tier else None,
            "actual_verdict": actual_verdict,
            "actual_units": len(units),
        }
        with open(run_dir / "route_shadow.jsonl", "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec) + "\n")
    except Exception:  # observe-only — must never affect a property record
        pass


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

    # task #45: wall-clock anchor for the plan→unit render's elapsed-budget
    # guard — the whole function runs inside one 600s asyncio.wait_for, so a
    # late optional render must know how much budget is already spent.
    _pp_t0 = _time.monotonic()

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

    # Knock direct-GET shortcut (task #21, warm=fast). Knock's doorway-api is a
    # PUBLIC GET, so a WARM Knock profile can skip the ~10-45s render and GET its
    # stored /units endpoint directly (<1s). try_knock_direct parses the roster
    # with the CANONICAL parse_knock_units (real unit numbers, not synthetic
    # ids) and returns a COMPLETE result dict — used verbatim below in place of
    # scrape_jugnu (routing the raw JSON through scrape_jugnu would misroute to
    # the generic cascade → inferred_* ids). Trigger = the stored endpoint, NOT
    # api_provider (Knock profiles carry api_provider='unknown'). Self-gates on
    # ENABLE_KNOCK_DIRECT_GET (default off) + WARM/HOT + endpoint + non-empty
    # roster; None → fall through to the normal render fetch below. Never-fail.
    # Shared holder for a warm direct-GET shortcut's pre-extracted result
    # (Knock / RentCafe / … — all fit the same shape: WARM profile → stored
    # endpoint → canonical parser → complete result dict used verbatim in place
    # of scrape_jugnu). None → fall through to the normal render + scrape_jugnu.
    _direct_shortcut_result: dict[str, Any] | None = None
    if fetch_result is None:
        try:
            from ma_poc.pms.knock_direct import try_knock_direct

            _kd = await try_knock_direct(task, profile_for_dispatch, csv_row)
            if _kd is not None:
                fetch_result = _kd["fetch_result"]
                _direct_shortcut_result = _kd["result"]
        except Exception as exc:
            log.warning(
                "knock_direct dispatch failed for %s: %s — falling back",
                task.property_id,
                exc,
            )
            fetch_result = None
            _direct_shortcut_result = None

    # RentCafe/SecureCafe direct raw-GET (task #21, warm=fast). The unit-level
    # gold is on securecafe availableunits.aspx; a WARM profile stores it in
    # winning_page_url, so we fetch it via HB in-page fetch (residential proxy
    # clears CF, returns RAW HTML the canonical parser needs — no BrightData) and
    # skip the marketing-page render + link-hop. Self-gates on
    # ENABLE_RENTCAFE_DIRECT_GET (default off) + WARM/HOT + a stored securecafe
    # availableunits URL + a non-empty roster; None → fall through. Never-fail.
    if fetch_result is None and _direct_shortcut_result is None:
        try:
            from ma_poc.pms.securecafe_direct import try_rentcafe_direct

            _rc = await try_rentcafe_direct(task, profile_for_dispatch, csv_row)
            if _rc is not None:
                fetch_result = _rc["fetch_result"]
                _direct_shortcut_result = _rc["result"]
        except Exception as exc:
            log.warning(
                "rentcafe_direct dispatch failed for %s: %s — falling back",
                task.property_id,
                exc,
            )
            fetch_result = None
            _direct_shortcut_result = None

    # SightMap direct raw-GET (task #21, warm=fast). WARM profile with a stored
    # sightmap /sightmaps/ API URL → hb_raw_get (HB in-page fetch → raw JSON, CF
    # cleared, no BrightData) → parse_sightmap_payload. Content-guarded (≥1
    # rent-bearing unit) so Entrata-hint props fall through to render. Self-gates
    # on ENABLE_SIGHTMAP_DIRECT_GET (default off); None → fall through. Never-fail.
    if fetch_result is None and _direct_shortcut_result is None:
        try:
            from ma_poc.pms.sightmap_direct import try_sightmap_direct

            _sm = await try_sightmap_direct(task, profile_for_dispatch, csv_row)
            if _sm is not None:
                fetch_result = _sm["fetch_result"]
                _direct_shortcut_result = _sm["result"]
        except Exception as exc:
            log.warning(
                "sightmap_direct dispatch failed for %s: %s — falling back",
                task.property_id,
                exc,
            )
            fetch_result = None
            _direct_shortcut_result = None

    # RealPage CWS direct raw-GET (task #21, warm=fast). WARM CWS profile (stored
    # api.ws.realpage.com endpoint; OLL excluded) → hb_raw_get the property page
    # (HB clears CF, no BrightData) → generic._probe_realpage_cws harvests the
    # public apiKey + GETs /units (no proxy — Akamai auth-gated, not CF-IP-
    # blocked). Skips the render; the probe today only runs post-render. Self-
    # gates on ENABLE_REALPAGE_DIRECT_GET (default off); None → fall through.
    if fetch_result is None and _direct_shortcut_result is None:
        try:
            from ma_poc.pms.realpage_direct import try_realpage_direct

            _rp = await try_realpage_direct(task, profile_for_dispatch, csv_row)
            if _rp is not None:
                fetch_result = _rp["fetch_result"]
                _direct_shortcut_result = _rp["result"]
        except Exception as exc:
            log.warning(
                "realpage_direct dispatch failed for %s: %s — falling back",
                task.property_id,
                exc,
            )
            fetch_result = None
            _direct_shortcut_result = None

    # H4 — unconditional vanity-domain fallback. Runs whenever the
    # direct path didn't produce a usable result (any failure tier or
    # routing skip).
    if fetch_result is None:
        # Cluster #4 fix (2026-05-20, feature_fail_1429 grind): the
        # tier escalator in fetcher.fetch is gated on `profile is not
        # None`. First-run properties used to have profile_for_dispatch
        # = None at this point (bootstrap happened only after the
        # fetch, at L3 below), so the fetcher took the single-tier
        # DIRECT path with no escalation. Cloudflare-walled properties
        # (tidesateastchase, liveatpalmhaven, etc.) returned 403 →
        # BOT_BLOCKED → no_body_short_circuit. Bootstrap a COLD profile
        # here so the escalator gets a chance to fire RESIDENTIAL on
        # the first bot-block. The same profile instance is reused at
        # L3 (no extra bootstrap call there).
        if profile_for_dispatch is None and hasattr(profile_store, "bootstrap"):
            try:
                profile_for_dispatch = profile_store.bootstrap(
                    task.property_id, {}, task.url
                )
            except Exception as _bs_exc:  # defensive — never block fetch
                log.warning(
                    "profile bootstrap failed for %s: %s — fetching without profile",
                    task.property_id,
                    _bs_exc,
                )
        # L1: Fetch with escalation when profile is available.
        fetch_result = await jugnu_fetch(task, profile=profile_for_dispatch)
    frontier.mark_attempt(task.url, fetch_result.outcome)

    # Check carry-forward need
    outcome_val = fetch_result.outcome.value
    if not fetch_result.ok():
        should_cf, reason = should_carry_forward(None, fetch_outcome=outcome_val)
        if should_cf:
            # Try carry-forward from prior state
            from ma_poc.discovery.carry_forward import (
                carry_forward_property,
                carry_forward_reason_for,
            )

            # Tag interactive-challenge carry-forwards for the human-review
            # queue: the clean render tier aborts (never solves) on interactive
            # CAPTCHA / never-clearing CF JS, stamping captcha_detected. This
            # makes them filterable, distinct from routine carry-forwards.
            reason = carry_forward_reason_for(
                reason, getattr(fetch_result, "captcha_detected", False)
            )

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

    # arch-hardening #1: cluster/template cross-property warm-start. A COLD
    # property that shares a PMS client-account cluster with ≥1 HOT sibling
    # borrows the sibling's proven llm_field_mappings (and modal availability
    # path) so the $0 deterministic replay tier attempts them before any LLM
    # call. Purely additive + independently re-validated at replay → safe;
    # flag-gated (default on). Non-fatal: never blocks a scrape.
    from ma_poc.config.feature_flags import ENABLE_CLUSTER_WARM_START

    if ENABLE_CLUSTER_WARM_START and profile is not None:
        try:
            from ma_poc.services.cluster_store import warm_start_profile_from_cluster

            _borrowed = warm_start_profile_from_cluster(profile, profile_store)
            if _borrowed:
                log.info(
                    "cluster warm-start: %s borrowed %d mapping(s) from cluster %s",
                    task.property_id,
                    _borrowed,
                    (profile.cluster_key or "")[:30],
                )
        except Exception as _ws_exc:  # defensive — never block the scrape
            log.debug("cluster warm-start skipped for %s: %s", task.property_id, _ws_exc)

    if _direct_shortcut_result is not None:
        # A warm direct-GET shortcut (Knock / RentCafe) already produced a
        # complete, canonically-parsed result (real unit numbers) — use it
        # verbatim; skipping scrape_jugnu is the whole point (no render, no
        # generic-cascade misroute).
        result = _direct_shortcut_result
    else:
        result = await scrape_jugnu(
            task=task,
            fetch_result=fetch_result,
            page=None,  # Would be provided in full RENDER mode
            profile=profile,
            csv_row=csv_row,
            partial_state=partial_state,
        )

    # ── L3: render-on-empty escalation (2026-07-16, flag-gated, default off) ──
    # When a routed adapter extracts 0 units from a fetch that SUCCEEDED (a 200
    # SSR/shell body) and did NOT already render, re-fetch that property ONCE
    # via RenderMode.RENDER so the browser fires the client-side widget XHRs
    # (OneSite OLL, Entrata/nestin SPAs) and re-run extraction. Bounded to one
    # extra render per empty-GET property; the render fetch's captured XHR
    # waterfall lands in render_fetch so scrape_jugnu extracts from it.
    from ma_poc.config.feature_flags import (
        ENABLE_ENTRATA_PLAN_RENDER,
        ENABLE_PLAN_UNIT_RENDER,
        ENABLE_RENDER_ON_EMPTY,
        PLAN_RENDER_BUDGET_GUARD_S,
        hb_enabled,
    )
    from ma_poc.fetch.contracts import RenderMode

    # Three triggers share ONE render + re-extract (bounded to one/property):
    #   • render-on-empty (L3): a routed adapter extracted ZERO units.
    #   • Entrata plan→unit (#42): an Entrata SUCCESS stalled at plan-level
    #     (rows present, none with a real unit_number) — the jd-fp roster is
    #     client-rendered, so a render reveals it. See feature_flags.
    #   • generic plan→unit (#45): ANY plan-level success, gated by the
    #     per-property circuit-breaker (_plan_render_allowed — stops paying
    #     for renders on verified plan-only properties) and an elapsed-budget
    #     guard (never spends a render inside the last stretch of the 600s
    #     per-property budget, so a slow/walled property can't be converted
    #     from a plan-level SUCCESS into a timeout FAILED).
    _zero_units = not (result.get("units") or [])
    _entrata_plan = ENABLE_ENTRATA_PLAN_RENDER and _is_entrata_plan_level(result)
    _generic_plan = (
        ENABLE_PLAN_UNIT_RENDER
        and _is_plan_level(result)
        and _plan_render_allowed(profile)
        and (_time.monotonic() - _pp_t0) < PLAN_RENDER_BUDGET_GUARD_S
    )
    _plan_triggered = _entrata_plan or _generic_plan
    if (
        ((ENABLE_RENDER_ON_EMPTY and _zero_units) or _plan_triggered)
        and fetch_result.ok()
        and getattr(fetch_result, "render_mode", None) != RenderMode.RENDER
    ):
        try:
            render_task = _dc_replace(task, render_mode=RenderMode.RENDER)
            render_fetch = await jugnu_fetch(render_task, profile=profile_for_dispatch)
            if render_fetch.ok():
                render_result = await scrape_jugnu(
                    task=render_task,
                    fetch_result=render_fetch,
                    page=None,
                    profile=profile,
                    csv_row=csv_row,
                    partial_state=partial_state,
                )
                # Accept only a strict improvement: the empty case wants any
                # units; the plan-level case wants MORE real unit-level rows
                # than we already had (never regress a plan-level success).
                if (_zero_units and len(render_result.get("units") or []) > 0) or (
                    _unit_level_row_count(render_result)
                    > _unit_level_row_count(result)
                ):
                    result = render_result
                    fetch_result = render_fetch
            # task #45: record that a plan-triggered render was ATTEMPTED on
            # the surviving result (post-accept, so the stamp lands on
            # whichever dict flows to the profile updater). The updater
            # advances plan_render_attempts_held when the final result is
            # still plan-level (attempted-and-HELD) and resets it on a
            # unit-level upgrade — the feedback loop the circuit-breaker
            # converges on.
            if _plan_triggered:
                result["_plan_render_attempted"] = True
        except Exception as _roe_exc:  # never let escalation crash a property
            log.debug(
                "render-on-empty escalation failed for %s: %s",
                task.property_id,
                _roe_exc,
            )

    # ── HB-on-shell recovery (task #46, 2026-07-24 canary finding) ──
    # The CF-walled cohort (SecureCafe / SightMap / Knock) returns an
    # OK-CLASSIFIED shell: the static fetch (and the patchright render-on-empty
    # above) reach the page but the unit XHR never fires, so we still have ZERO
    # units — and because nothing was BOT_BLOCKED, the escalator's HB rung never
    # triggered (it only fires on BOT_BLOCKED). The 2026-07-24 250-prop canary
    # showed HB firing only ~10x for this exact reason. Give HB an explicit shot
    # here: it renders through a fresh cloud IP + basic stealth (clears CF by
    # WAITING — no solve, compliant) and CAPTURES the XHR into network_log, which
    # scrape_jugnu promotes to the Tier-1 API surface. Only when
    # FETCH_BACKEND=hyperbrowser (default off → BrightData runs untouched); the
    # budget guard bounds spend, and a single HB fetch is internally timeout-
    # bounded (~40-60s), so this cannot itself cause a 600s hang.
    if (
        hb_enabled()
        and not (result.get("units") or [])
        and fetch_result.ok()
        and (_time.monotonic() - _pp_t0) < PLAN_RENDER_BUDGET_GUARD_S
    ):
        try:
            from ma_poc.fetch.hyperbrowser_backend import HyperbrowserProvider

            hb_task = _dc_replace(task, render_mode=RenderMode.RENDER)
            hb_fetch = await HyperbrowserProvider(mode="render").fetch(
                hb_task, profile_for_dispatch
            )
            if hb_fetch.ok():
                hb_result = await scrape_jugnu(
                    task=hb_task,
                    fetch_result=hb_fetch,
                    page=None,
                    profile=profile,
                    csv_row=csv_row,
                    partial_state=partial_state,
                )
                if len(hb_result.get("units") or []) > 0:
                    result = hb_result
                    fetch_result = hb_fetch
        except Exception as _hb_exc:  # never let recovery crash a property
            log.debug(
                "hb-shell recovery failed for %s: %s", task.property_id, _hb_exc
            )

    # ── Track 2b (task #37): encore per-plan render fan-out ──
    # When the encoreskyline adapter discovered plan URLs from the body but
    # couldn't click them (page=None → 0 units, tier ENCORESKYLINE_*), render
    # each plan URL (INTERACTION_REVEAL fires the Check-Availability click) and
    # parse the post-click roster. Flag-gated (default off), bounded +
    # elapsed-budget-guarded, never-fail.
    from ma_poc.config.feature_flags import (
        ENABLE_ENCORE_PLAN_RENDER,
        ENCORE_PLAN_RENDER_MAX_PLANS,
        PLAN_RENDER_BUDGET_GUARD_S,
    )

    if (
        ENABLE_ENCORE_PLAN_RENDER
        and not (result.get("units") or [])
        and "ENCORESKYLINE" in _result_tier(result).upper()
        and fetch_result.ok()
        and (_time.monotonic() - _pp_t0) < PLAN_RENDER_BUDGET_GUARD_S
    ):
        try:
            _eb = fetch_result.body
            _entry_html = (
                _eb.decode("utf-8", "replace") if isinstance(_eb, bytes) else str(_eb or "")
            )
            _enc_units = await _encore_plan_render_units(
                task,
                _entry_html,
                task.url,
                lambda t: jugnu_fetch(t, profile=profile_for_dispatch),
                started_monotonic=_pp_t0,
                budget_guard_s=float(PLAN_RENDER_BUDGET_GUARD_S),
                max_plans=ENCORE_PLAN_RENDER_MAX_PLANS,
            )
            if _enc_units:
                result["units"] = _enc_units
                result["extraction_tier_used"] = "TIER_1_DOM_ENCORESKYLINE_PP_RENDER"
                log.info(
                    "encore plan-render recovered %d units for %s",
                    len(_enc_units),
                    task.property_id,
                )
        except Exception as _enc_exc:  # never let the fan-out crash a property
            log.debug("encore plan-render fan-out failed for %s: %s", task.property_id, _enc_exc)

    # F6 (H6/H11) — surface the propertyId we used (resolved or cached)
    # so the profile_updater can persist it. Read by
    # update_profile_after_extraction; only written there when the tier
    # is one of the two RentCafe-direct success codes — H13.
    if rc_direct_property_id is not None:
        result["_rentcafe_property_id"] = rc_direct_property_id

    # Phase Q (2026-07-19): surface the CSV's expected unit total so the profile
    # updater can compute coverage + flag CONTAMINATED (PMC-wide) / THIN results.
    if csv_row and "_expected_total_units" not in result:
        for _euk in ("Total Units", "Total Units (Est.)", "total_units"):
            _euv = csv_row.get(_euk)
            if _euv:
                try:
                    result["_expected_total_units"] = int(str(_euv).strip())
                    break
                except (ValueError, TypeError):
                    continue

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
    # 2026-05-20: pass the Path C plan-level fallback marker
    # (``_verdict_quality``) and the final unit list so the verdict labeler
    # honors both signals:
    #   - explicit marker → SUCCESS_PLAN_LEVEL
    #   - all-inferred_* UIDs OR no-rent-signal → SUCCESS_PLAN_LEVEL
    # See ma_poc/reporting/verdict.py:compute() for the downgrade rules.
    # 2026-05-23: pass the operator-no-availability signal — adapters
    # set ``result["_operator_no_availability"] = True`` when the page
    # carries an explicit "no units available" statement
    # (krcapartments-class cohort). The verdict layer routes those to
    # SUCCESS_NO_AVAILABILITY instead of FAILED_NO_DATA.
    verdict = compute_verdict(
        fetch_outcome=outcome_val,
        extract_result=extract_result,
        carry_forward_applied=result.get("_meta", {}).get("carry_forward_used", False),
        verdict_quality_override=result.get("_verdict_quality"),
        units=result.get("units"),
        operator_no_availability=bool(result.get("_operator_no_availability")),
    )
    meta = result.setdefault("_meta", {})
    meta["canonical_id"] = task.property_id
    meta["verdict"] = verdict.verdict.value
    meta["verdict_reason"] = verdict.reason

    # Agentic router SHADOW observer (Phase 2, flag ENABLE_ROUTE_SHADOW, default
    # off). ADDITIVE + never-fail: logs what the deterministic router WOULD decide
    # from the fetch signals next to what the pipeline actually did — observe-only,
    # never acted on. Same additive-block contract as _provenance below.
    try:
        from ma_poc.config.feature_flags import ENABLE_ROUTE_SHADOW

        if ENABLE_ROUTE_SHADOW and run_dir is not None:
            _emit_route_shadow(
                fetch_result, result, task, run_dir, profile,
                getattr(extract_result, "tier_used", None), verdict.verdict.value,
            )
    except Exception:
        pass

    # Property-level provenance — surface the diagnostics we already captured
    # (confidence, adapter/PMS, tiers attempted, fetch method/latency, and a
    # per-property data-quality mix) onto _meta.provenance. Additive +
    # never-fail, same contract as the _meta.publish_ceiling stamp below.
    try:
        meta["provenance"] = _provenance_block(
            result, csv_row or {}, fetch_result, outcome_val
        )
    except Exception:  # provenance must never sink a property record
        pass

    # 2026-07-12 (campaign Lever 6): for a zero-unit property, grade whether
    # its "no data published" claim is airtight enough to count as gold
    # under the revised gold definition (Tier-1 units OR verified-no-data at
    # 100% confidence). This is ADDITIVE — it stamps _meta.publish_ceiling +
    # an evidence bundle and NEVER changes the verdict above. The gold%
    # tally reads the grade post-hoc. The verifier's guards (rent-present /
    # embed / unit-vocab / crashed-cascade → not gold) block the false-gold
    # the rentcafe audit caught (marker string + real units). Never-fail.
    if not result.get("units"):
        try:
            from ma_poc.reporting.publish_ceiling import assess_publish_ceiling

            _pc_extract = extract_result
            _pc_plans = getattr(_pc_extract, "plan_summaries", None)
            if _pc_plans is None and isinstance(_pc_extract, dict):
                _pc_plans = _pc_extract.get("plan_summaries")
            _pc_body = getattr(fetch_result, "body", None)
            _pc_html = (
                _pc_body.decode("utf-8", "replace")
                if isinstance(_pc_body, bytes)
                else (_pc_body or "")
            )
            _pc = assess_publish_ceiling(
                units=result.get("units"),
                plan_summaries=_pc_plans,
                html_signals=result.get("_html_characterization"),
                tier_trace=result.get("_tier_attempts"),
                page_html=_pc_html,
            )
            meta["publish_ceiling"] = {
                "verdict": _pc.verdict.value,
                "gold_eligible": _pc.gold_eligible,
                "confidence": _pc.confidence,
                "reason": _pc.reason,
                "evidence": _pc.evidence,
            }
            emit(
                EventKind.PROPERTY_EMITTED,  # publish_ceiling ride-along
                task.property_id,
                publish_ceiling=_pc.verdict.value,
                pc_gold_eligible=_pc.gold_eligible,
            )
        except Exception as _pc_exc:  # never-fail contract
            log.debug(
                "publish_ceiling assessment failed for %s: %s",
                task.property_id, _pc_exc,
            )

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

    # Capture-first: archive a capped sample of winning raw API bodies for
    # the PMS whose source_ids can't be grounded from live probing. Success
    # path only (we want groundable real bodies; failures are already dumped
    # to llm_diagnostics). Capped/idempotent/non-fatal by construction.
    if (
        run_dir is not None
        and _tier_used.startswith("TIER_1")
        and any(mk in _tier_used.upper() for mk in _API_SAMPLE_TIER_MARKERS)
    ):
        try:
            _adapter = next(
                mk.lower()
                for mk in _API_SAMPLE_TIER_MARKERS
                if mk in _tier_used.upper()
            )
            if _API_SAMPLE_COUNTS.get(_adapter, 0) < _API_SAMPLE_CAP_PER_ADAPTER:
                _apis = result.get("_raw_api_responses") or []
                if _apis:
                    _dir = run_dir / "api_samples" / _adapter
                    _dir.mkdir(parents=True, exist_ok=True)
                    _path = _dir / f"{task.property_id}.json"
                    if not _path.exists():
                        _sample = [
                            {
                                "url": _r.get("url"),
                                "status": _r.get("status"),
                                "tier": _tier_used,
                                # truncate huge bodies — grounding only
                                # needs the field shape, not every unit
                                "body": str(_r.get("body"))[:250_000],
                            }
                            for _r in _apis[:3]
                            if isinstance(_r, dict)
                        ]
                        _path.write_text(
                            json.dumps(_sample, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        _API_SAMPLE_COUNTS[_adapter] = (
                            _API_SAMPLE_COUNTS.get(_adapter, 0) + 1
                        )
        except Exception as _exc:
            log.debug(
                "api_sample capture failed for %s: %s", task.property_id, _exc
            )

    return result


# ---------------------------------------------------------------------------
# Output formatting — v1 / v2
# ---------------------------------------------------------------------------


# ── Property-level provenance (surface captured-but-dropped diagnostics) ─────
# The output record historically exposed only verdict + tier_used + llm_cost.
# During a scrape we capture far more — confidence, which adapter/PMS won, the
# tiers attempted, how many APIs were intercepted, the fetch method/latency, and
# (from the units) a data-quality mix. This stamps that onto _meta.provenance so
# a consumer can judge trust/coverage without re-deriving it. Additive +
# never-fail, mirroring the adjacent _meta.publish_ceiling stamp.
def _tier_family(tier: str) -> str:
    """Collapse a specific extraction_tier code to a coarse family."""
    t = (tier or "").upper()
    if not t:
        return "NONE"
    if "LLM" in t:
        return "LLM"
    if "API" in t:
        return "API"
    if "JSONLD" in t or "JSON_LD" in t:
        return "JSONLD"
    if "EMBEDDED" in t:
        return "EMBEDDED"
    if "DOM" in t or "TEXT" in t:
        return "DOM"
    return "OTHER"


def _is_lease_up(csv_row: dict[str, Any]) -> bool:
    """True when the CSV marks this property LEASE_UP (vs STABILISED)."""
    for k in ("type", "Type"):
        v = csv_row.get(k)
        if isinstance(v, str) and "lease" in v.lower() and "up" in v.lower():
            return True
    return False


def _provenance_block(
    result: dict[str, Any],
    csv_row: dict[str, Any],
    fetch_result: Any,
    outcome_val: str | None,
) -> dict[str, Any]:
    """Compute _meta.provenance from an already-scraped result. Pure; never raises."""
    from ma_poc.core.schema_v2 import _is_floor_plan_level

    units = result.get("units") or []
    by_family: dict[str, int] = {}
    n_plan = n_synth = n_realid = 0
    for u in units:
        tier = str(u.get("extraction_tier") or u.get("_extraction_tier") or "")
        fam = _tier_family(tier)
        by_family[fam] = by_family.get(fam, 0) + 1
        # 2026-07-25: this ran on the INTERNAL unit dicts, which carry the
        # adapter's ``data_quality_flag`` (SightMap writes
        # ``SIGHTMAP_PLAN_PRESENCE``) but never an ``is_floor_plan_level`` key
        # — that key is only minted later by the v2 formatter. With a
        # ``TIER_1_API_SIGHTMAP`` tier that also fails the ``_PLAN_LEVEL``
        # suffix test, both arms missed and ``plan_level_units`` read 0 for
        # all 505 SightMap properties in the 2026-07-25 5k canary while 4,024
        # plan rows were in fact shipping. Delegate to the canonical predicate,
        # which reads the flag as well as the tier suffix.
        if _is_floor_plan_level(u) or tier.upper().endswith("_PLAN_LEVEL"):
            n_plan += 1
        uid = str(u.get("unit_id") or u.get("unit_number") or "")
        if uid.startswith(("inferred_", "unkeyable_")):
            n_synth += 1
        elif uid:
            n_realid += 1
    det = result.get("_detected_pms") or {}
    rm = getattr(fetch_result, "render_mode", None)
    return {
        "unit_count": len(units),
        "confidence": result.get("confidence"),
        "adapter": result.get("_adapter_used") or None,
        "detected_pms": det.get("pms") if isinstance(det, dict) else None,
        "winning_tier": result.get("extraction_tier_used"),
        "tiers_attempted": list(result.get("_fallback_chain") or []),
        "api_calls_intercepted": len(result.get("api_calls_intercepted") or []),
        "is_lease_up": _is_lease_up(csv_row),
        "fetch": {
            "outcome": outcome_val,
            "render_mode": getattr(rm, "value", rm),
            "proxied": bool(getattr(fetch_result, "proxy_label", None)),
            "page_load_ms": getattr(fetch_result, "elapsed_ms", None),
            # 2026-07-25 RCA: "fetch outcome: TRANSIENT" was undecomposable from
            # the run artifacts — diagnosing a 336-property regression required
            # reconstructing error classes from Cloud Logging. Persist the
            # signature, HTTP status and body size so the SAME question is
            # answerable offline from properties.json next time.
            "error_signature": getattr(fetch_result, "error_signature", None),
            "status_code": getattr(fetch_result, "status_code", None),
            "body_bytes": len(getattr(fetch_result, "body", None) or ""),
        },
        "data_quality": {
            "by_tier_family": by_family,
            "real_id_units": n_realid,
            "synthetic_id_units": n_synth,
            "plan_level_units": n_plan,
        },
    }


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

    concessions_text = result.get("concessions_text") or md.get("concessions")

    # 2026-07-12 (concession propagation gap, 94 props in the 2026-07-11
    # canary): the property-level banner has TWO sources — the scraper's
    # ``concessions_text`` (Steps 3/3b/3c, smeared to units by scraper.py
    # Step 9c) AND the page-metadata ``md["concessions"]``, which only
    # ever fed this property field: units shipped with empty concession
    # columns even though the property-level banner was captured. Backfill
    # here — the single formatting chokepoint — so EVERY source and EVERY
    # path (including timeout-salvage records that bypass Step 9c) reaches
    # the unit rows. ``enrich_unit_concession_fields`` is idempotent:
    # units that already carry text are untouched.
    if concessions_text:
        try:
            from ma_poc.pms.adapters._parsing import (
                enrich_unit_concession_fields,
            )

            for _u in units:
                if isinstance(_u, dict):
                    enrich_unit_concession_fields(
                        _u, property_concession_text=concessions_text
                    )
        except Exception:  # defensive — never block the property emit
            pass

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
        "concessions": concessions_text,
        "units": _emit_v2_units_for_property(
            [_format_v2_unit(u, scrape_ts, _v2_property_id_for_unit(meta, apartment_id)) for u in units]
        ),
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


# 2026-05-26 (canary 87b837b QC follow-up): post-extraction unit dedup
# + cross-building disambiguation. The 87b837b run had 9,773 duplicate-
# unit_id rows across 347 properties:
#   • 66.5% EXACT_DUPE  — same unit emitted twice from multi-source
#     merge (generic adapter pulling from /availability AND /floor-plans)
#   • 19.5% DIFFERENT_FLOOR_PLAN — same unit# in two different plans
#     (adapter mismerge; left for follow-up)
#   • 11.9% BUILDING_DIFFERS  — same unit# in two different buildings
#     (operator-legitimate; disambiguate by prefixing with building)
#   •  1.8% RENT_DIFFERS, 0.2% AVAIL_STATUS_DIFFERS, 0.1% OTHER —
#     snapshot conflicts; not deduped (need a tiebreaker policy).
#
# P1 (EXACT_DUPE filter):
#   Drop a row when an earlier row in the same property has byte-for-byte
#   identical values across the canonical fingerprint
#   (unit_id, beds, baths, area, rent_low, rent_high, floor_plan_name,
#    building, availability_status). First occurrence kept.
#
# P2 (BUILDING_DIFFERS disambiguation):
#   When 2+ rows share unit_id AND all rows in the collision group have
#   a non-empty building value AND the building values differ, rewrite
#   each row's unit_id to ``{building}-{unit_id}`` so downstream dedup /
#   identity / per-unit FK queries treat them as distinct entities.
#   The original value is preserved in the existing ``unit_id_raw``
#   capture-first field, so QA can audit the rewrite.
#
# Both passes run only on units that already came out of
# _format_v2_unit (post-junk-filter, post-fallback-id) — so an empty /
# ``inferred_*`` unit_id is left untouched by both.

_DEDUP_FINGERPRINT_FIELDS: tuple[str, ...] = (
    "unit_id",
    "beds",
    "baths",
    "area",
    "rent_low",
    "rent_high",
    "floor_plan_name",
    "building",
    "availability_status",
)


def _dedup_fingerprint(u: dict[str, Any]) -> tuple[Any, ...]:
    """Canonical key for P1 EXACT_DUPE detection.
    Keep simple/stable — change cascades to QC numbers."""
    return tuple(u.get(k) for k in _DEDUP_FINGERPRINT_FIELDS)


def _apply_p2_building_disambiguation(units: list[dict[str, Any]]) -> int:
    """For each unit_id that appears 2+ times in ``units`` AND has a
    non-empty distinct ``building`` on EVERY occurrence, rewrite
    each row's unit_id to ``{building}-{unit_id}``. Mutates in place.

    Returns the number of rows that were rewritten."""
    from collections import defaultdict

    groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for u in units:
        uid = u.get("unit_id")
        if uid in (None, "", "null"):
            continue
        s = str(uid).strip()
        if not s or s.startswith("inferred_"):
            continue
        groups[s].append(u)

    rewritten = 0
    for uid, group in groups.items():
        if len(group) < 2:
            continue
        # Require every member to have a non-empty building
        buildings = [(u.get("building") or "").strip() for u in group]
        if any(b == "" for b in buildings):
            continue
        # Require at least 2 distinct buildings (otherwise same-bldg
        # collisions are EXACT_DUPE-class, not BUILDING_DIFFERS)
        if len(set(buildings)) < 2:
            continue
        for u, b in zip(group, buildings, strict=False):
            new_uid = f"{b}-{uid}"
            u["unit_id"] = new_uid
            rewritten += 1
    return rewritten


def _apply_p2b_floor_plan_id_disambiguation(units: list[dict[str, Any]]) -> int:
    """DIFFERENT_FLOOR_PLAN class — same unit_id, distinct floor_plan_id
    on every occurrence. From the 87b837b QC (1,035 cases / 19.5% of
    real-ID dups), this is dominated by:

      • TIER_1_DOM_APPFOLIO_VANITY (794 = 77%) — small-property PMC
        sites where ``floor_plan_name`` is actually the street address
        and two different physical units share a unit-number (e.g.
        "712 S 11th St #3" + "2224 A St - #3" both unit_id="3").
      • TIER_1_KNOCK_API (48) — Knock CRM emits the same unit_id for
        two genuinely different floor plans (B1 + CL).
      • TIER_1_DOM_RENTCAFE_NESTIN (34) — cross-building "403" units
        with different plan names (Venice / Monaco) but null building.

    Same shape as P2 but the disambiguator is the first 8 chars of
    ``floor_plan_id`` (a deterministic hash from the adapter, stable
    across runs).

    Conservative gates mirror P2:
      • Skip if ANY member's floor_plan_id is empty/null
      • Skip if all floor_plan_ids are the same (P1 EXACT_DUPE handles)
      • Skip inferred_* and empty unit_ids

    Runs AFTER P2 (so a unit already disambiguated to ``{bldg}-{uid}``
    won't be re-prefixed) and BEFORE P1 (so distinct-plan units survive
    the fingerprint dedup).

    Returns the count of rewritten rows."""
    from collections import defaultdict

    groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for u in units:
        uid = u.get("unit_id")
        if uid in (None, "", "null"):
            continue
        s = str(uid).strip()
        if not s or s.startswith("inferred_"):
            continue
        groups[s].append(u)

    rewritten = 0
    for uid, group in groups.items():
        if len(group) < 2:
            continue
        fp_ids = [(u.get("floor_plan_id") or "").strip() for u in group]
        if any(fp == "" for fp in fp_ids):
            continue
        if len(set(fp_ids)) < 2:
            continue
        for u, fp in zip(group, fp_ids, strict=False):
            fp_short = fp[:8]
            u["unit_id"] = f"{fp_short}-{uid}"
            rewritten += 1
    return rewritten


def _apply_p1_exact_dedup(units: list[dict[str, Any]]) -> int:
    """Drop rows whose canonical fingerprint matches an earlier row in
    the same list. Mutates ``units`` in place (deletes in reverse).

    Returns the number of dropped rows."""
    seen: set[tuple[Any, ...]] = set()
    drop_indices: list[int] = []
    for i, u in enumerate(units):
        fp = _dedup_fingerprint(u)
        if fp in seen:
            drop_indices.append(i)
        else:
            seen.add(fp)
    for i in reversed(drop_indices):
        del units[i]
    return len(drop_indices)


def _apply_p3_inferred_id_collision_suffix(units: list[dict[str, Any]]) -> int:
    """2026-05-31: ``inferred_<sha>`` IDs are derived from (canonical_id,
    floor_plan_name, beds, baths) — by design, two units with the same
    plan-anchor (per ``compute_inferred_unit_id`` in _parsing.py:415) get
    the same ID. P1 EXACT_DUPE then collapses byte-identical rows. But
    when the same inferred-ID maps to 2+ rows that differ on area, rent,
    building, or availability_status (i.e. real distinct units sharing a
    plan), P1 keeps them all and downstream sees collisions:

      pid=55797 had ``inferred_abd603c978cdfc32`` × 32, byredwood
      portfolio sites had identical 122-row patterns, etc. — 3,924 extra
      collision rows across 456 props in the may13 may28 canary.

    Fix: after P1 dedup, append a STABLE per-row suffix to break the
    collisions while preserving the plan-anchor in the prefix. The suffix
    hashes only STABLE physical fields (raw area, building, floor,
    unit_number) — NOT rent/availability. (An earlier version hashed the
    full ``_dedup_fingerprint`` which INCLUDES rent_low/rent_high, so a
    unit's id changed whenever its rent moved → it churned "disappeared+new"
    in the daily diff, breaking rent-change / days-on-market tracking for
    ~2.7% of units.)

    Only fires on ``inferred_*`` IDs — real adapter-provided unit_ids
    are never modified.

    Returns count of rewritten rows."""
    import hashlib as _hl
    from collections import defaultdict

    groups: dict[str, list[int]] = defaultdict(list)
    for i, u in enumerate(units):
        uid = u.get("unit_id")
        if not isinstance(uid, str) or not uid.startswith("inferred_"):
            continue
        groups[uid].append(i)

    rewritten = 0
    # Fields that distinguish real distinct units sharing a plan-anchor WITHOUT
    # depending on mutable rent/availability. Raw ``area`` matters: the inferred
    # id buckets sqft to the nearest 10, so two units at 683 vs 691 sqft collide
    # on the prefix but differ here → a stable, rent-independent suffix.
    _STABLE_SUFFIX_FIELDS = ("area", "building", "floor", "unit_number")
    for uid, idxs in groups.items():
        if len(idxs) < 2:
            continue
        seen_base: dict[str, int] = {}
        for i in idxs:
            u = units[i]
            stable = tuple(u.get(k) for k in _STABLE_SUFFIX_FIELDS)
            base = _hl.sha256(repr(stable).encode()).hexdigest()[:6]
            # Positional tiebreak ONLY for rows identical on every stable field
            # (inherently indistinguishable without a per-unit id).
            n = seen_base.get(base, 0)
            seen_base[base] = n + 1
            sfx = base if n == 0 else f"{base}-{n}"
            u["unit_id"] = f"{uid}-{sfx}"
            rewritten += 1
    return rewritten


def _emit_v2_units_for_property(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-property post-extraction normalisation step.

    Pipeline order is important:
      1. P2 building disambiguation FIRST — rewriting unit_id to
         ``{building}-{unit_id}`` changes the fingerprint, so EXACT_DUPE
         dedup must run AFTER (otherwise a legit cross-bldg ID gets
         flagged as a dup by the bare unit_id).
      2. P2b floor_plan_id disambiguation — catches DIFFERENT_FLOOR_PLAN
         cases where building is null but the units are genuinely
         different (AppFolio address-as-fp_name, Knock cross-plan
         collisions, RentCafe cross-bldg). Runs after P2 so it doesn't
         re-prefix a unit already disambiguated by building.
      3. P1 EXACT_DUPE dedup — drops rows with byte-identical
         fingerprints across the canonical field set.
    """
    if not units:
        return units
    # P0 — fp_name canonicalization (2026-05-31): when the same
    # floor_plan_id maps to multiple case-variant names within a
    # property (e.g. 'A5' + 'a5', 'Llano' + 'LLano', Holland
    # Residential's 'C2R TWO BEDROOM RENOVATED' + 'C2r Two Bedroom
    # Renovated'), pick the most-frequent case-form and rewrite all
    # rows to it. Without this the same plan shows up as two distinct
    # entries in plan-level analytics. Runs BEFORE dedup so case-
    # variants collapse correctly under P1's fingerprint match.
    _apply_p0_fp_name_canonicalization(units)
    _apply_p2_building_disambiguation(units)
    _apply_p2b_floor_plan_id_disambiguation(units)
    _apply_p1_exact_dedup(units)
    # P3 runs LAST — only fires on inferred_* IDs that survive P1
    # because their fingerprints differ on area / rent / building.
    _apply_p3_inferred_id_collision_suffix(units)
    return units


def _apply_p0_fp_name_canonicalization(units: list[dict[str, Any]]) -> int:
    """For each distinct floor_plan_id, find the most-common case-form
    of floor_plan_name and rewrite all rows to that form.

    Tie-break (when two case-forms have equal counts): prefer the form
    with more uppercase letters in the first 3 chars (typical operator
    style — "A5" beats "a5", "C2R TWO BEDROOM" beats "C2r Two Bedroom").
    """
    from collections import Counter, defaultdict

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for u in units:
        fpid = u.get("floor_plan_id")
        fpn = u.get("floor_plan_name")
        if fpid and isinstance(fpn, str) and fpn.strip():
            groups[fpid].append(u)

    rewritten = 0
    for fpid, group in groups.items():
        names = [u.get("floor_plan_name") for u in group]
        if len(set(names)) < 2:
            continue
        counter = Counter(names)
        max_count = max(counter.values())
        tied = [n for n, c in counter.items() if c == max_count]
        if len(tied) == 1:
            canonical = tied[0]
        else:
            # Tie-break: prefer the form with more uppercase in first 3 chars
            canonical = max(
                tied,
                key=lambda s: sum(1 for c in s[:3] if c.isupper()),
            )
        for u in group:
            if u.get("floor_plan_name") != canonical:
                u["floor_plan_name"] = canonical
                rewritten += 1
    return rewritten


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
    # Delegate the available-now → scrape-date fallback to the canonical
    # resolver (single source of truth; see the available_date field below).
    from ma_poc.core.schema_v2 import _is_floor_plan_level, _resolve_available_date
    # 2026-05-19 capture-first: snapshot the ORIGINAL source value for
    # every emitted field BEFORE any inference / junk-scrub / lossy
    # formatting. Emitted as first-class ``<field>_raw`` columns at the
    # bottom so downstream QA can cross-check a normalized value against
    # what was actually extracted and recover formatter mistakes without
    # re-scraping (every bug fixed this session would have been a 1-line
    # post-process instead of a re-run). Derived/generated fields
    # (floor_plan_id, date_captured) have no source → raw is None (honest,
    # not fabricated).
    _raw_src: dict[str, Any] = {
        "beds": unit.get("_bedrooms") or unit.get("bedrooms") or unit.get("beds"),
        "baths": unit.get("_bathrooms") or unit.get("bathrooms") or unit.get("baths"),
        "floor_plan_name": (
            unit.get("_floor_plan")
            or unit.get("floor_plan_name")
            or unit.get("floorplan_name")
        ),
        "floor_plan_id": unit.get("floor_plan_id"),
        "area": unit.get("_sqft") or unit.get("sqft") or unit.get("area"),
        "unit_id": (
            unit.get("unit_id")
            or unit.get("unit_number")
            or unit.get("_unit_number")
        ),
        "rent_low": (
            unit.get("market_rent_low")
            or unit.get("asking_rent")
            or unit.get("rent_range")
        ),
        "rent_high": (
            unit.get("market_rent_high")
            or unit.get("asking_rent")
            or unit.get("rent_range")
        ),
        "floor": (
            unit.get("floor")
            or unit.get("_floor")
            or unit.get("floor_number")
            or unit.get("floorNumber")
            or unit.get("floor_no")
        ),
        "building": unit.get("building") or unit.get("_building"),
        "available_units": unit.get("available_units"),
        "date_captured": None,
        "available_date": unit.get("available_date"),
        "availability_status": (
            unit.get("availability_status") or unit.get("_availability_status")
        ),
        "lease_term": unit.get("lease_term") or unit.get("_lease_term"),
        "move_in_date": (
            unit.get("move_in_date") or unit.get("_move_in_date")
        ),
    }

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

    # 2026-05-26 (canary 87b837b QC): drop ``floor_plan_name`` when it
    # equals the unit_id. 555 units in 87b837b had identical fpn=uid
    # values like ``fpn='259'=uid='259'`` — the adapter fell back to
    # using the unit number as the plan name. That's not a real plan
    # name; it pollutes plan-level analytics and inflates plan-level
    # cardinality. Nulling lets ``compute_floor_plan_id`` below
    # derive a deterministic id from beds+baths only — units with
    # the same bed/bath count will then correctly share a plan id.
    if (
        fp_name is not None
        and uid is not None
        and str(fp_name).strip() == str(uid).strip()
    ):
        fp_name = None

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

    # rent: numeric first, parse rent_range string if needed.
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

    # 2026-05-25 (canary 1ef1060 follow-up — per-unit concession + offer
    # fields). The canonical ma_poc/core/schema_v2.py:_format_v2_unit emits
    # 10 concession + offer columns per unit (concession_text,
    # concession_text_clean, _concession_quality, concession_value,
    # concession_source, offer_banner, offer_type, offer_target,
    # offer_value, offer_conditions). The runner's _format_v2_unit was
    # divergent — it dropped all of them, even though the adapters
    # (via make_unit_dict in _parsing.py) populate concession_text and
    # offer_* on every unit when the page carries a banner. Net effect
    # in canary 1ef1060: 2,312 properties had a property-level
    # ``concessions`` banner captured but ZERO units had per-unit
    # concession/offer columns surfaced in the output.
    #
    # Sync the runner with schema_v2.py — same legacy-alias chain for
    # the raw concession text, same concession_clean + offer_extract
    # wiring. Additive: existing consumers that don't read these keys
    # are unaffected; consumers that DO (offer reporting, xlsx export,
    # downstream concession analytics) get parity with the canonical
    # schema and the prod xlsx reference shape.
    concession_text = unit.get("concession_text")
    if not isinstance(concession_text, str) or not concession_text.strip():
        concession_text = None
    if not concession_text:
        # Adapters emit concession under many names across parsers.
        # Accept any string variant; non-strings fall through to None
        # (the dict/list shapes get captured via the property-level
        # ``concessions`` field upstream).
        for _ck in (
            "concession", "concessions", "specials_description",
            "specialsDescription", "special", "specials",
            "promotion", "promo", "offer", "offers",
            "incentive", "incentives", "deal", "savings",
            "discount", "free_rent", "look_and_lease",
            "move_in_special",
        ):
            _cv = unit.get(_ck)
            if isinstance(_cv, str) and _cv.strip():
                concession_text = _cv.strip()
                break

    # Cleaned variant + quality classifier — both gated on a non-empty
    # raw text. Importing the helpers lazily keeps the cold-start cost
    # at zero for runs that never read these fields.
    concession_text_clean = None
    concession_quality = None
    if concession_text:
        try:
            from ma_poc.core.concession_clean import (
                classify_concession_quality,
                clean_concession_text,
            )
            concession_text_clean = clean_concession_text(concession_text)
            concession_quality = classify_concession_quality(concession_text)
        except Exception:
            # Never fail the unit emit on a concession-cleaning glitch —
            # the raw text always survives via concession_text below.
            concession_text_clean = None
            concession_quality = None

    # ``concession_value`` is a numeric (months free, $ off, etc.) that
    # make_unit_dict sets when the offer_extract module successfully
    # parses a structured offer. Coerce to float defensively — adapters
    # have historically emitted strings or already-floats.
    _cv_raw = unit.get("concession_value")
    concession_value: float | None
    try:
        concession_value = float(_cv_raw) if _cv_raw not in (None, "") else None
    except (TypeError, ValueError):
        concession_value = None

    out: dict[str, Any] = {
        "beds": norm_beds,
        "baths": norm_baths,
        "floor_plan_name": fp_name or None,
        "floor_plan_id": floor_plan_id,
        "area": _format_area(sqft),
        "unit_id": str(uid) if uid not in (None, "", "null") else None,
        # 2026-07-25: #36's placeholder marker was added to
        # ``ma_poc/core/schema_v2.py:_format_v2_unit`` only — but THIS is the
        # formatter the production Jugnu runner uses, so the flag never
        # reached properties.json. Result on the 2026-07-25 5k canary: 4,024
        # SightMap plan-presence rows (20.9% of all SightMap unit rows, across
        # 352 mixed properties) shipped indistinguishable from real units —
        # every one of them carrying an ``inferred_`` synthetic unit_id and
        # ``area=-1``, and ``_provenance_block``'s ``plan_level_units`` counter
        # reading 0 for all 505 SightMap properties. Keep in lock-step with
        # ``core.schema_v2._is_floor_plan_level``.
        "is_floor_plan_level": _is_floor_plan_level(unit),
        "rent_low": _format_rent(rent_lo_raw),
        "rent_high": _format_rent(rent_hi_raw),
        "floor": _format_floor(_raw_src["floor"]),
        "building": (
            None
            if not _raw_src["building"]
            or str(_raw_src["building"]).strip() == ""
            else str(_raw_src["building"]).strip()
        ),
        "available_units": (
            int(_m.group(0))
            if (_m := _re.search(r"\d+", str(_raw_src["available_units"] or "")))
            and 1 <= int(_m.group(0)) <= 10_000
            else None
        ),
        "date_captured": scrape_ts.strftime("%Y-%m-%d %H:%M:%S"),
        # 2026-07-11 quality sweep: this runner-duplicate of _format_v2_unit
        # formatted the raw date but NEVER applied the available-now →
        # scrape-date fallback that core/schema_v2._resolve_available_date
        # adds (delegated here, single source of truth — same fix pattern as
        # _format_date_str above). Without it, has-rent/AVAILABLE units that
        # ship no explicit move-in date (Knock, G5, TIER_1_5_EMBEDDED,
        # MERGED_CROSS_PAGE, RENTCAFE_LT …) kept available_date=None:
        # fleet-wide available_date was 45.6% in the 2026-07-11 canary,
        # concentrated in these tiers at 0-6% despite 99-100% rent. The
        # canonical transform applies the default; the production jugnu path
        # didn't, so it never took effect. has_rent is gated on a REAL unit
        # identity so plan-level synthetic rows don't get a fabricated stamp.
        "available_date": _resolve_available_date(
            _format_date_str(unit.get("available_date")),
            _norm_avail_status(
                unit.get("availability_status") or unit.get("_availability_status")
            ),
            scrape_ts,
            has_rent=(
                (
                    _format_rent(rent_lo_raw) is not None
                    or _format_rent(rent_hi_raw) is not None
                )
                and uid not in (None, "", "null")
            ),
        ),
        # 2026-05-26: availability_status was absent from this function
        # (present in core/schema_v2.py but never synced here).  That caused
        # 93.9% of units to have a blank availability_status in the canary
        # output.  Light normalization mirrors _norm_status() in schema_v2.py:
        # uppercase known tokens; pass raw string through for everything else.
        "availability_status": _norm_avail_status(
            unit.get("availability_status") or unit.get("_availability_status")
        ),
        "lease_term": _safe_int_gt1(unit.get("lease_term") or unit.get("_lease_term")),
        "move_in_date": _format_date_str(unit.get("move_in_date") or unit.get("_move_in_date")),
        # 2026-05-25 (canary 1ef1060 follow-up): concession + offer fields,
        # synced with ma_poc/core/schema_v2.py:_format_v2_unit. 10 new keys.
        "concession_text": concession_text,
        "concession_text_clean": concession_text_clean,
        "_concession_quality": concession_quality,
        "concession_value": concession_value,
        "concession_source": unit.get("concession_source") or None,
        "offer_banner": unit.get("offer_banner") or None,
        "offer_type": unit.get("offer_type") or None,
        "offer_target": unit.get("offer_target") or None,
        "offer_value": unit.get("offer_value") or None,
        "offer_conditions": unit.get("offer_conditions") or None,
    }

    # Merge-rescue: if no natural id survived, derive a stable inferred id
    # from physical attributes. The helper mutates ``out['unit_id']`` in place
    # and returns the resolved id (or None when even the floor plan is
    # missing — those records still skip downstream, but the per-tier "no
    # unit_id" rate drops by ~17K units/run for JSON-LD + LLM tiers).
    if not out["unit_id"]:
        assign_fallback_unit_id(out, property_id)

    # First-class raw companions for every emitted field. Uncoerced
    # (trimmed string or None) so the exact extracted value is preserved
    # for later cross-processing. Additive — never overwrites a processed
    # column; consumers that don't know these keys simply ignore them.
    for _k in list(out.keys()):
        _v = _raw_src.get(_k)
        out[f"{_k}_raw"] = (
            None if _v is None or str(_v).strip() == "" else str(_v).strip()
        )

    # Stable PMS-native ids for daily merge — carried through as-is (a
    # dict; already raw, so no string _raw companion). Empty {} when the
    # adapter hasn't been wired to populate it yet (additive, non-breaking).
    _sids = unit.get("source_ids")
    out["source_ids"] = dict(_sids) if isinstance(_sids, dict) else {}
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


def persist_timeout_route_hints(
    profile_store: Any,
    property_id: str,
    partial_state: dict[str, Any] | None,
    *,
    unit_count: int = 0,
    profile: Any | None = None,
) -> bool:
    """Persist route knowledge discovered by a property that TIMED OUT.

    Returns True when a profile was actually written.

    Why this exists (RCA 2026-07-25): the previous implementation read
    ``partial_state["profile"]``, a key **nothing ever wrote**. The live 5k
    canary logged zero "next run starts warm" lines across 366 timeouts, so
    every timed-out property stayed COLD and re-paid full discovery on the next
    run — a compounding trap that also caps the warm-profile ratio the
    fast-path levers depend on. ``pms.scraper.checkpoint_partial`` now records
    the URL that actually produced units under
    ``partial_state["profile_hints"]["winning_page_url"]``, and this persists it.

    Deliberately NARROW. Only ``navigation.winning_page_url`` is written. A run
    that timed out must not look like a success to the profile updater, so
    success counters, maturity promotion and ``preferred_tier`` are untouched —
    this is route knowledge, not an extraction outcome.

    Args:
        profile_store: store exposing ``get_profile(id)`` and ``save(profile)``.
        property_id: canonical property id.
        partial_state: the caller-scoped dict that survived coroutine
            cancellation (see ``checkpoint_partial``).
        unit_count: units salvaged, for the log line only.
        profile: an already-loaded profile, if the caller has one.

    Never raises — a persistence failure must not mask the timeout itself.
    """
    try:
        if not isinstance(partial_state, dict):
            return False
        hints = partial_state.get("profile_hints")
        if not isinstance(hints, dict):
            return False
        winning = hints.get("winning_page_url")
        if not winning or not hasattr(profile_store, "save"):
            return False

        prof = profile
        if prof is None and hasattr(profile_store, "get_profile"):
            prof = profile_store.get_profile(property_id)
        nav = getattr(prof, "navigation", None)
        if prof is None or nav is None:
            return False
        # Idempotent: an unchanged URL is not worth a version bump.
        if getattr(nav, "winning_page_url", None) == winning:
            return False

        nav.winning_page_url = winning
        profile_store.save(prof)
        log.info(
            "Property %s: persisted winning_page_url=%s from timed-out run "
            "(units=%d) — next run starts warm",
            property_id, winning, unit_count,
        )
        return True
    except Exception as exc:
        log.warning("persist_timeout_route_hints failed for %s: %s", property_id, exc)
        return False


def _salvage_unit_has_numeric_rent(unit: dict[str, Any]) -> bool:
    """True when a timeout-salvaged unit carries a *numeric* rent value.

    Mirrors the numeric arm of ``schema_gate._has_rent`` (the ``_RENT_FIELDS``
    positive-numeric check) PLUS a ``rent_range`` string parse — timeout-
    salvaged units are still in the raw ``make_unit_dict`` shape where rent
    lives under ``rent_range`` (a string like "$1,450"), not the v2
    ``rent_low``/``rent_high`` numerics. Deliberately EXCLUDES the
    ``_rent_gap_documented`` arm: a "documented" gap is exactly the false
    signal this predicate exists to see past (see
    :func:`_neutralize_unearned_rent_gap`).
    """
    for k in (
        "asking_rent", "market_rent_low", "market_rent_high",
        "rent_low", "rent_high", "rent",
    ):
        v = unit.get(k)
        if v in (None, ""):
            continue
        try:
            if float(str(v).replace(",", "").replace("$", "")) > 0:
                return True
        except (TypeError, ValueError):
            pass
    rr = unit.get("rent_range")
    if rr:
        try:
            from ma_poc.pms.adapters._parsing import parse_rent_range

            lo, hi = parse_rent_range(str(rr))
            if (lo and lo > 0) or (hi and hi > 0):
                return True
        except Exception:
            pass
    return False


def _neutralize_unearned_rent_gap(unit: dict[str, Any]) -> None:
    """Strip a false "operator doesn't publish rent" flag off a timeout salvage.

    2026-07-11 quality sweep. A per-property TIMEOUT partial-recovery is an
    INTERRUPTED extraction, not an exhausted one. SightMap (and similar)
    geometry rosters accumulated mid-hop can carry a ``data_gaps=["rent"]`` /
    ``data_quality_flag="RENT_NOT_PUBLISHED"`` flag — but ``schema_gate.
    _rent_gap_documented``'s contract is that an adapter sets it only after
    *exhausting* enrichment. The timeout pre-empted that, so on a salvage where
    NO unit carries a numeric rent the flag is a verified false positive
    (cltexchange.com unit 10113237 salvaged rent=null, but SightMap's own API
    publishes it at $1,450 / 703 sqft — we simply timed out before the price
    join). Left in place it (a) makes ``property_has_rent_signal`` return True,
    suppressing the verdict layer's ``no_rent_signal → SUCCESS_PLAN_LEVEL``
    downgrade so the property inflates into a unit-level SUCCESS, and (b) leaves
    every geometry row marked AVAILABLE — phantom priced-null inventory
    (cltexchange 647 "available" units vs ~86 genuinely leasable). Drop the
    unearned gap flag, demote availability to UNKNOWN (honest: we never
    determined it), and mark the row QA_HELD.
    """
    gaps = unit.get("data_gaps")
    if isinstance(gaps, list) and "rent" in gaps:
        unit["data_gaps"] = [g for g in gaps if g != "rent"]
    unit["data_quality_flag"] = "QA_HELD"
    unit["availability_status"] = "UNKNOWN"


def _make_failed_record(
    property_id: str,
    url: str,
    error: str,
    schema_version: str,
) -> dict[str, Any]:
    """Create a failed property record in the appropriate schema.

    Args:
        property_id: Canonical property ID.
        url: Property URL.
        error: Error message.
        schema_version: "v1" or "v2".

    Returns:
        Failed property dict.
    """
    meta = {
        "canonical_id": property_id,
        "scrape_tier_used": "FAILED",
        "scrape_errors": [error],
        "carry_forward_used": False,
    }
    if schema_version == "v2":
        try:
            apartment_id = int(property_id)
        except (ValueError, TypeError):
            apartment_id = None
        return {
            "apartment_id": apartment_id,
            "proj_name": None,
            "address": None,
            "city": None,
            "state": None,
            "zip_code": None,
            "country": None,
            "phone": None,
            "email_address": None,
            "website": url,
            "pmc": None,
            "website_design": None,
            "concessions": None,
            "units": [],
            "_meta": meta,
        }
    return {
        "_meta": meta,
        "units": [],
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


def _norm_avail_status(val: Any) -> str | None:
    """Normalise availability_status into the enum
    {AVAILABLE, UNAVAILABLE, WAITLIST, WAITLISTED, LEASED, PENDING, UNKNOWN}
    or None when unset.

    2026-05-31 QC update: the previous capture-first behaviour leaked 181
    free-text values into output ("Notice - Application Pending", "Sign
    Waitlist", "Call for Details!", "Only 1 Vacant Apartment Left!").
    Now every recognized phrase maps to its enum value, and any
    unrecognized non-empty string maps to UNKNOWN — never raw text.
    Downstream QA can still inspect the original via ``*_raw`` companion
    fields preserved by ``make_unit_dict``.

    Mirrors _norm_status() in ma_poc/core/schema_v2.py — keep in sync.
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    u = s.upper()
    # 1. Exact enum match — single source of truth for the verb set.
    if u in ("AVAILABLE", "UNAVAILABLE", "WAITLIST", "WAITLISTED",
             "LEASED", "PENDING", "UNKNOWN"):
        return u
    # 2. Phrase-based partial matches. Order matters — PENDING and
    # WAITLIST are checked before AVAILABLE because "application
    # pending" / "join waitlist" can co-occur with AVAILABLE-ish
    # framing on operator pages but the real semantic is the more
    # specific state.
    if "PENDING" in u or "APPLICATION" in u:
        return "PENDING"
    if "WAITLIST" in u or "WAIT LIST" in u or "WAIT-LIST" in u:
        return "WAITLIST"
    # UNAVAILABLE-shaped phrases checked BEFORE LEASED so "leased out"
    # / "not available" don't get caught as plain LEASED.
    if (
        "NOT AVAILABLE" in u
        or "UNAVAILABLE" in u
        or "SOLD OUT" in u
        or "LEASED OUT" in u
    ):
        return "UNAVAILABLE"
    if "LEASED" in u or "OCCUPIED" in u:
        return "LEASED"
    # "AVAILABLE NOW", "Only N Vacant", "Now Available" — count these as AVAILABLE
    if "AVAILABLE" in u or "VACANT" in u or "NOW LEASING" in u:
        return "AVAILABLE"
    # 3. Everything else (free-text noise) → UNKNOWN. Don't leak raw.
    return "UNKNOWN"


def _format_rent(val: Any) -> float | None:
    """Clean rent value. Must be > 1 or None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if val > 1 else None
    s = str(val).strip().replace("$", "").replace(",", "")
    try:
        n = float(s)
        return n if n > 1 else None
    except (ValueError, TypeError):
        # 2026-05-19: the bare float() above silently discarded valid but
        # noisy single-value rents the adapters emit ("$1450/mo",
        # "From $1,450", "Starting at $1450", "$1,450+", "1200-1400").
        # Delegate to the canonical money parser (single source of truth —
        # same pattern as the _format_date_str delegate fix). Additive:
        # only reached after float() already failed, so clean numerics are
        # byte-identical. Returns the LOW bound (the dominant discard case
        # is a single price with noise where lo == hi; a true embedded
        # range in one field is rare and lo is still correct for rent_low).
        try:
            from ma_poc.pms.adapters._parsing import parse_rent_range

            lo, _hi = parse_rent_range(str(val))
            if lo is not None and lo > 1:
                return float(lo)
        except Exception:
            pass
        return None


def _format_area(val: Any) -> int:
    """Convert sqft to int. Keeps -1 as the "absent" sentinel.

    Sanity bounds: a real apartment floor-plan area is between 150 and 10,000
    sqft. Anything outside that is garbage (bedroom counts, floor numbers,
    truncated values like "070") and gets coerced to -1. Previously any
    positive integer was accepted, which is why the 2026-04-19 run had area
    values of 9, 12, 50, 70, 100, etc. passed through as "successful".
    """
    if val is None or val == -1:
        return -1
    # 2026-05-19: ``int(float(str(val)))`` silently discarded the very
    # common comma / unit-suffixed / range sqft forms ("1,200",
    # "1,200 sq ft", "1200 sqft", "1,200-1,400") → area=-1. Pull the first
    # numeric token first (range → low bound). The 150–10,000 sanity
    # bound below is UNCHANGED — it still rejects bed counts / floor
    # numbers / truncated "070" garbage (additive: clean ints identical).
    s = str(val).replace(",", "")
    m = _re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return -1
    try:
        n = int(float(m.group(0)))
    except (ValueError, TypeError):
        return -1
    if 150 <= n <= 10_000:
        return n
    return -1


def _format_date_str(val: Any) -> str | None:
    """Normalize date to YYYY-MM-DD. None if unparseable.

    2026-05-19: delegate to ``schema_v2._format_date``. This runner had a
    DUPLICATE, narrower date parser that only accepted ISO and 4-digit-year
    ``m/d/Y`` — it silently dropped the ``"Available 7/10/26"`` /
    ``"Available Now"`` / 2-digit-year forms that AppFolio, RentCafe, Knock
    and the embedded-portal parsers actually emit. The capture-first
    widening shipped in 15b7aab only touched ``schema_v2._format_date``,
    so it never took effect on the production jugnu path and fleet-wide
    ``available_date`` stayed ~0% for those tiers. Delegating keeps a
    single source of truth; ISO and 4-digit ``m/d/Y`` behave exactly as
    before (the delegate is a strict superset).
    """
    from ma_poc.core.schema_v2 import _format_date

    return _format_date(val)


def _format_floor(val: Any) -> int | None:
    """Unit floor number, or None.

    2026-05-19: probe found the only ``floor`` values reaching output were
    5–6-digit unit/internal IDs mis-mapped into the field (a real apartment
    floor is 1–~100). Extract the leading int ("2nd", "Floor 3" → 2, 3)
    and apply a sanity bound — anything outside 1–100 is an ID/garbage and
    is rejected (same defensive shape as ``_format_area``). The raw source
    is still preserved via the ``floor_raw`` companion.
    """
    if val is None:
        return None
    m = _re.search(r"\d+", str(val))
    if not m:
        return None
    try:
        n = int(m.group(0))
    except (ValueError, TypeError):
        return None
    return n if 1 <= n <= 100 else None


def _safe_int_gt1(val: Any) -> int | None:
    """Integer > 1 or None.

    2026-05-19: extract the leading integer first — adapters commonly
    emit lease_term as "12 Months" / "12 mo" / "13-month", which the bare
    ``int(float(str(val)))`` silently dropped to None. Additive: bare
    ints/floats behave exactly as before; the ``> 1`` guard is preserved.
    """
    if val is None:
        return None
    m = _re.search(r"\d+", str(val))
    if not m:
        return None
    try:
        n = int(m.group(0))
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

    def iter_profiles_by_cluster_key(
        self, cluster_key: str, limit: int = 100
    ) -> list[Any]:
        """Delegate cluster-mate lookup to whichever backing implements it.

        Used by ``services.cluster_store`` for cross-property warm-start. The
        FS ``ProfileStore`` and the SQL ``SqlProfileStore`` both expose this;
        if a backing doesn't, we return [] so warm-start cleanly no-ops. On a
        durable-backing error we fall back to the FS store (same resilience
        contract as ``get_profile``).
        """
        if not cluster_key:
            return []
        target = self._backing
        fn = getattr(target, "iter_profiles_by_cluster_key", None)
        try:
            if callable(fn):
                return list(fn(cluster_key, limit))
        except Exception as exc:
            log.warning("cluster iter failed via backing: %s — trying FS", exc)
        if self._uses_data_provider:
            fs_fn = getattr(self._fs_backing, "iter_profiles_by_cluster_key", None)
            try:
                if callable(fs_fn):
                    return list(fs_fn(cluster_key, limit))
            except Exception:
                return []
        return []


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
