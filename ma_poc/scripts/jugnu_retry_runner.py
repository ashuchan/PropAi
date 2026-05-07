"""
Jugnu retry runner — retry failed properties from a prior run.

Three modes:

  --retry-errors   Retry properties that FAILED or produced 0 units in a
                   prior run.  Reads the prior run's properties.json (Jugnu
                   format) or ledger.jsonl (legacy format) to identify
                   candidates.

  --resume         Re-process everything that isn't a clean success.  Useful
                   after an interrupted run.

  --unprocessed    Pick up properties that are in the input catalog but
                   absent from the prior run's properties.json.  Used when
                   one or more scrape shards never finished (e.g. a stuck
                   Cloud Run task) so their slice produced no record at
                   all — neither --retry-errors nor --resume can see those
                   properties because they iterate properties.json. Reads
                   the catalog from the DataProvider's `properties` table
                   by default; pass --csv to override with a file.

All modes:
  - Create CrawlTasks with reason=RETRY and feed them through L1-L5.
  - Merge new results into the existing properties.json (replacing stale
    records by canonical_id, appending records that weren't there before).
  - Produce an updated report.json / report.md.

Usage:
  # Retry failures from yesterday (auto-detects latest run)
  python scripts/jugnu_retry_runner.py --retry-errors

  # Retry failures from a specific date
  python scripts/jugnu_retry_runner.py --retry-errors --run-date 2026-04-17

  # Resume an interrupted run
  python scripts/jugnu_retry_runner.py --resume --run-date 2026-04-18

  # Recover properties from a stuck shard (catalog vs properties.json diff)
  python scripts/jugnu_retry_runner.py --unprocessed --run-date 2026-04-28

  # Retry with a limit
  python scripts/jugnu_retry_runner.py --retry-errors --limit 10

  # Override the catalog source with a CSV (useful for retrying legacy
  # runs that pre-date the DB-backed catalog)
  python scripts/jugnu_retry_runner.py --retry-errors --run-date 2026-04-17 \\
      --csv config/properties.csv
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

# Ensure ma_poc is importable regardless of working directory
_repo_root = Path(__file__).resolve().parent.parent.parent
_MA_POC_ROOT = Path(__file__).resolve().parent.parent  # ma_poc/
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("jugnu_retry")


# ---------------------------------------------------------------------------
# Failure detection
# ---------------------------------------------------------------------------


def _is_failure(prop: dict[str, Any]) -> bool:
    """Return True if a property result should be retried.

    A property is considered failed if:
      - scrape_tier_used contains 'FAIL'
      - verdict contains 'FAIL'
      - units list is empty
      - scrape_errors is non-empty
    """
    meta = prop.get("_meta", {})
    tier = str(meta.get("scrape_tier_used") or "")
    verdict = str(meta.get("verdict") or "")
    units = prop.get("units", [])
    errors = meta.get("scrape_errors") or []

    if "FAIL" in tier.upper():
        return True
    if "FAIL" in verdict.upper():
        return True
    if not units:
        return True
    if errors:
        return True
    return False


def _is_not_success(prop: dict[str, Any]) -> bool:
    """Return True if a property is not a clean success (for --resume mode)."""
    meta = prop.get("_meta", {})
    tier = str(meta.get("scrape_tier_used") or "")
    verdict = str(meta.get("verdict") or "")
    units = prop.get("units", [])

    if "FAIL" in tier.upper():
        return True
    if "FAIL" in verdict.upper():
        return True
    if not units:
        return True
    return False


# ---------------------------------------------------------------------------
# Load candidates from prior run
# ---------------------------------------------------------------------------


def _load_jugnu_candidates(
    run_dir: Path,
    mode: str,
) -> list[dict[str, Any]]:
    """Load retry candidates from a Jugnu run's properties.json.

    Args:
        run_dir: Path to the prior run directory.
        mode: 'retry_errors' or 'resume'.

    Returns:
        List of dicts with 'property_id' and 'url' for each candidate.
    """
    props_path = run_dir / "properties.json"
    if not props_path.exists():
        log.warning("No properties.json found in %s", run_dir)
        return []

    try:
        props = json.loads(props_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not read %s: %s", props_path, exc)
        return []

    if not isinstance(props, list):
        log.error("properties.json is not a list")
        return []

    check_fn = _is_failure if mode == "retry_errors" else _is_not_success
    candidates: list[dict[str, Any]] = []
    for prop in props:
        if not check_fn(prop):
            continue
        meta = prop.get("_meta", {})
        pid = meta.get("canonical_id") or ""
        url = prop.get("Website") or prop.get("website") or prop.get("url") or ""
        if not pid:
            continue
        candidates.append(
            {
                "property_id": pid,
                "url": url,
                "prior_tier": meta.get("scrape_tier_used"),
                "prior_errors": meta.get("scrape_errors", []),
            }
        )

    return candidates


def _load_unprocessed_candidates(
    run_dir: Path,
    catalog_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return catalog rows whose canonical_id has NO record in properties.json.

    Used by --unprocessed mode to recover properties from a scrape run
    where one or more shards never completed (stuck task, OOM kill, 4h
    timeout). The retry runner's other modes only see properties that
    already have a record; this loader closes the gap by diffing the
    source catalog against the prior run's output.

    Match key is the catalog property identifier compared against
    ``_meta.canonical_id`` in properties.json — Jugnu writes the
    canonical_id straight through, so string equality correctly
    identifies untouched rows.

    Args:
        run_dir: Path to the prior run directory (holds properties.json).
        catalog_rows: Catalog rows (as flat dicts via PropertyToScrape.
            as_csv_row()). Source-agnostic — could be CSV-backed or
            DB-backed depending on how the caller resolved them.

    Returns:
        List of dicts (property_id, url, prior_status="UNPROCESSED") for
        every catalog row not present in properties.json.
    """
    seen_ids: set[str] = set()
    props_path = run_dir / "properties.json"
    if props_path.exists():
        try:
            props = json.loads(props_path.read_text(encoding="utf-8"))
            if isinstance(props, list):
                for prop in props:
                    pid = (prop.get("_meta") or {}).get("canonical_id") or ""
                    if pid:
                        seen_ids.add(str(pid))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning(
                "Could not read %s: %s — treating run as fully unprocessed",
                props_path,
                exc,
            )
    else:
        log.warning(
            "No properties.json at %s — treating every catalog row as unprocessed",
            props_path,
        )

    candidates: list[dict[str, Any]] = []
    skipped_no_id = 0
    skipped_no_url = 0
    for row in catalog_rows:
        pid = str(
            row.get("property_id")
            or row.get("Unique ID")
            or row.get("Property ID")
            or row.get("apartmentid")
            or ""
        ).strip()
        url = str(row.get("url") or row.get("Website") or row.get("website") or "").strip()
        if not pid:
            skipped_no_id += 1
            continue
        if not url:
            skipped_no_url += 1
            continue
        if pid in seen_ids:
            continue
        candidates.append(
            {
                "property_id": pid,
                "url": url,
                "prior_status": "UNPROCESSED",
            }
        )

    log.info(
        "Unprocessed diff: %d processed in run, %d candidates from catalog (skipped %d no-id, %d no-url)",
        len(seen_ids),
        len(candidates),
        skipped_no_id,
        skipped_no_url,
    )
    return candidates


def _load_legacy_candidates(
    run_dir: Path,
    mode: str,
    csv_lookup: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Load retry candidates from a legacy run's ledger.jsonl.

    Args:
        run_dir: Path to the prior run directory.
        mode: 'retry_errors' or 'resume'.
        csv_lookup: Optional {property_id: url} mapping from CSV.

    Returns:
        List of dicts with 'property_id' and 'url' for each candidate.
    """
    ledger_path = run_dir / "ledger.jsonl"
    if not ledger_path.exists():
        return []

    # Deduplicate: last entry per canonical_id wins.
    ledger: dict[str, dict[str, Any]] = {}
    try:
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                cid = entry.get("canonical_id")
                if cid:
                    ledger[cid] = entry
            except json.JSONDecodeError:
                continue
    except OSError as exc:
        log.error("Could not read %s: %s", ledger_path, exc)
        return []

    # Also try to get URLs from properties.json if it exists.
    url_lookup: dict[str, str] = dict(csv_lookup or {})
    props_path = run_dir / "properties.json"
    if props_path.exists():
        try:
            props = json.loads(props_path.read_text(encoding="utf-8"))
            for prop in props:
                meta = prop.get("_meta", {})
                pid = meta.get("canonical_id") or ""
                url = prop.get("Website") or prop.get("website") or ""
                if pid and url:
                    url_lookup.setdefault(pid, url)
        except (json.JSONDecodeError, OSError):
            pass

    candidates: list[dict[str, Any]] = []
    for cid, entry in ledger.items():
        status = entry.get("status", "")
        units = entry.get("units_count", 0)

        if mode == "retry_errors":
            should_retry = status == "FAILED" or (status in ("SUCCESS", "SUCCESS_WITH_ERRORS") and units == 0)
        else:  # resume
            should_retry = status not in ("SUCCESS", "SUCCESS_WITH_ERRORS", "SKIPPED")

        if not should_retry:
            continue

        url = entry.get("url") or url_lookup.get(cid, "")
        if not url:
            log.warning("No URL for %s — skipping (provide --csv to resolve)", cid)
            continue

        candidates.append(
            {
                "property_id": cid,
                "url": url,
                "prior_status": status,
                "prior_units": units,
            }
        )

    return candidates


def _shard_candidates(
    candidates: list[dict[str, Any]],
    task_index: int,
    task_count: int,
) -> list[dict[str, Any]]:
    """Slice candidates for one Cloud Run task in a sharded retry execution.

    Sorts by property_id then takes every Nth row (round-robin) so that
    failure clusters by PMS platform — which sit together in
    properties.json — get spread across shards instead of concentrated
    in one. Disjoint across shards; union covers all candidates.

    Args:
        candidates: Full candidate list from _load_jugnu_candidates / _load_legacy_candidates.
        task_index: This task's CLOUD_RUN_TASK_INDEX (0-based).
        task_count: Total CLOUD_RUN_TASK_COUNT.

    Returns:
        Subset of candidates for this shard. Empty if task_index >= task_count
        or candidates is empty.
    """
    if task_count <= 1:
        return list(candidates)
    if task_index < 0 or task_index >= task_count:
        return []
    ordered = sorted(candidates, key=lambda c: str(c.get("property_id") or ""))
    return [c for i, c in enumerate(ordered) if i % task_count == task_index]


def _build_url_lookup(catalog_rows: list[dict[str, Any]]) -> dict[str, str]:
    """Build a {property_id: url} lookup from catalog rows.

    Source-agnostic — accepts the flat-dict shape produced by
    PropertyToScrape.as_csv_row(), which both the CSV and DB catalog
    sources emit.
    """
    lookup: dict[str, str] = {}
    for row in catalog_rows:
        pid = str(
            row.get("property_id")
            or row.get("Unique ID")
            or row.get("Property ID")
            or row.get("apartmentid")
            or ""
        )
        url = str(row.get("url") or row.get("Website") or row.get("website") or "")
        if pid and url:
            lookup[pid] = url
    return lookup


def _resolve_catalog_rows(csv_path: Path | None) -> list[dict[str, Any]]:
    """Resolve the input catalog as a list of flat dicts.

    When `csv_path` is set we read that file directly (back-compat). When
    None we read from the active DataProvider's catalog — same flip-rule
    used by jugnu_runner.
    """
    from data_provider.dtos import PropertyToScrape  # noqa: F401  (imported for type clarity)
    from data_provider.factory import get_data_provider
    from data_provider.filesystem import CsvPropertyCatalogSource

    if csv_path is not None:
        source = CsvPropertyCatalogSource(csv_path)
    else:
        source = get_data_provider().property_catalog
    return [p.as_csv_row() for p in source.list_active()]


def _find_latest_run_dir(data_dir: Path) -> Path | None:
    """Find the most recent run directory.

    Args:
        data_dir: Base data directory.

    Returns:
        Path to the latest run directory, or None.
    """
    runs_dir = data_dir / "runs"
    if not runs_dir.exists():
        return None
    # Also check under v1/ and v2/ subdirs (legacy schema versioning).
    search_dirs = [runs_dir]
    for sub in ("v1", "v2"):
        sub_runs = data_dir / sub / "runs"
        if sub_runs.exists():
            search_dirs.append(sub_runs)

    candidates: list[Path] = []
    for search_dir in search_dirs:
        for d in search_dir.iterdir():
            if d.is_dir() and len(d.name) == 10 and d.name[4] == "-":
                candidates.append(d)

    if not candidates:
        return None
    candidates.sort(key=lambda p: p.name, reverse=True)
    return candidates[0]


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def _merge_properties(
    existing: list[dict[str, Any]],
    retried: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge retried results into the existing property list.

    Retried properties replace their prior record by canonical_id.
    Untouched properties are preserved as-is.

    Args:
        existing: Original properties list.
        retried: Newly retried property results.

    Returns:
        Merged list.
    """
    retried_by_id: dict[str, dict[str, Any]] = {}
    for prop in retried:
        meta = prop.get("_meta", {})
        pid = meta.get("canonical_id")
        if pid:
            retried_by_id[pid] = prop

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    for prop in existing:
        meta = prop.get("_meta", {})
        pid = meta.get("canonical_id")
        if pid and pid in retried_by_id:
            merged.append(retried_by_id.pop(pid))
            seen.add(pid)
        else:
            merged.append(prop)
            if pid:
                seen.add(pid)

    # Append any retried properties that weren't in the original list.
    for pid, prop in retried_by_id.items():
        if pid not in seen:
            merged.append(prop)

    return merged


# ---------------------------------------------------------------------------
# Main retry pipeline
# ---------------------------------------------------------------------------


async def run_retry(
    data_dir: Path,
    mode: str,
    run_date: str | None = None,
    csv_path: Path | None = None,
    limit: int | None = None,
    schema_version: str = "v1",
) -> dict[str, Any]:
    """Run the Jugnu retry pipeline.

    Args:
        data_dir: Base data directory.
        mode: 'retry_errors', 'resume', or 'unprocessed'.
        run_date: Target run date (YYYY-MM-DD). None = latest.
        csv_path: Optional CSV override for the catalog source. When None
            the catalog comes from `provider.property_catalog` (the
            `properties` table for sql backends, the FS-default CSV
            otherwise). 'unprocessed' mode no longer requires --csv: an
            empty catalog from the active provider triggers FATAL.
        limit: Max properties to retry.
        schema_version: "v1" or "v2" output format.

    Returns:
        Report dict.
    """
    from ma_poc.discovery.contracts import CrawlTask, TaskReason
    from ma_poc.fetch.conditional import ConditionalCache
    from ma_poc.fetch.contracts import RenderMode
    from ma_poc.observability import events
    from ma_poc.observability.cost_ledger import CostLedger
    from ma_poc.observability.slo_watcher import check as slo_check
    from ma_poc.reporting.run_report import build as build_run_report

    # -- Locate the target run directory --
    if run_date:
        # Check multiple possible locations.
        for prefix in ("", "v1/", "v2/"):
            candidate = data_dir / prefix / "runs" / run_date
            if candidate.exists():
                source_run_dir = candidate
                break
        else:
            source_run_dir = data_dir / "runs" / run_date
            if not source_run_dir.exists():
                log.error("Run directory not found for %s", run_date)
                return {"exit_status": "FATAL", "error": f"no run dir for {run_date}"}
    else:
        source_run_dir = _find_latest_run_dir(data_dir)
        if source_run_dir is None:
            log.error("No prior runs found in %s", data_dir)
            return {"exit_status": "FATAL", "error": "no prior runs found"}
        run_date = source_run_dir.name

    log.info("Source run: %s", source_run_dir)

    # -- Resolve the input catalog — DB by default, CSV if --csv given --
    catalog_rows = _resolve_catalog_rows(csv_path)
    csv_lookup = _build_url_lookup(catalog_rows) if catalog_rows else None
    # Full row lookup for output formatting (keyed by property_id).
    csv_rows: dict[str, dict[str, Any]] = {}
    for row in catalog_rows:
        pid = str(
            row.get("property_id")
            or row.get("Unique ID")
            or row.get("Property ID")
            or row.get("apartmentid")
            or ""
        )
        if pid:
            csv_rows[pid] = dict(row)

    # -- Load candidates --
    if mode == "unprocessed":
        # Catalog-vs-properties.json diff. Distinct loader because the
        # other two modes iterate properties.json, which by definition
        # does NOT contain the records we need to find here.
        if not catalog_rows:
            log.error(
                "unprocessed mode requires a non-empty catalog "
                "(seed the `properties` table or pass --csv)"
            )
            return {
                "exit_status": "FATAL",
                "error": "unprocessed mode requires a non-empty catalog",
                "run_date": run_date,
                "mode": mode,
            }
        candidates = _load_unprocessed_candidates(source_run_dir, catalog_rows)
    else:
        # Try Jugnu format first (properties.json with _meta), fall back to legacy.
        candidates = _load_jugnu_candidates(source_run_dir, mode)
        if not candidates:
            candidates = _load_legacy_candidates(source_run_dir, mode, csv_lookup)
        if not candidates and csv_lookup:
            # Last resort: if properties.json exists but has no _meta, try to
            # match by URL from CSV.
            candidates = _load_legacy_candidates(source_run_dir, mode, csv_lookup)

    if not candidates:
        log.info("No candidates to retry.")
        return {
            "exit_status": "OK_NOTHING_TO_DO",
            "run_date": run_date,
            "mode": mode,
            "candidates": 0,
        }

    # Apply CSV URL overrides (CSV may have corrected URLs).
    if csv_lookup:
        for c in candidates:
            if c["property_id"] in csv_lookup:
                c["url"] = csv_lookup[c["property_id"]]

    # Sharding: when running under Cloud Run with task_count > 1, take only
    # this shard's slice of the candidate list. CLOUD_RUN_TASK_INDEX and
    # CLOUD_RUN_TASK_COUNT are auto-injected by Cloud Run; outside of cloud
    # they default to 0/1 (single-shard, original behaviour).
    try:
        task_index = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
        task_count = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    except ValueError:
        task_index, task_count = 0, 1
    pre_shard_count = len(candidates)
    candidates = _shard_candidates(candidates, task_index, task_count)
    if task_count > 1:
        log.info(
            "Shard %d/%d: %d/%d candidates after sharding",
            task_index,
            task_count,
            len(candidates),
            pre_shard_count,
        )

    # --limit is applied per-shard (each shard processes up to N).
    if limit:
        candidates = candidates[:limit]

    log.info(
        "Retrying %d properties (%s mode, schema %s) from %s", len(candidates), mode, schema_version, run_date
    )
    for c in candidates[:10]:
        log.info("  %s — %s", c["property_id"], c["url"][:60])
    if len(candidates) > 10:
        log.info("  ... and %d more", len(candidates) - 10)

    # -- Setup output directory (schema-namespaced) --
    today = date.today().isoformat()
    from ma_poc.scripts.jugnu_runner import _resolve_data_dirs

    output_run_dir, state_dir, cache_dir, schema_root = _resolve_data_dirs(
        data_dir,
        schema_version,
        today,
    )
    run_id = f"retry_{today}_{uuid.uuid4().hex[:8]}"

    # -- Setup L1/L5 infrastructure --
    events.configure(output_run_dir, run_id)
    cost_ledger = CostLedger(output_run_dir / "cost_ledger.db")

    from ma_poc.discovery.dlq import Dlq
    from ma_poc.discovery.frontier import Frontier

    frontier = Frontier(state_dir / "frontier.sqlite")
    dlq = Dlq(state_dir / "dlq.jsonl")
    cond_cache = ConditionalCache(cache_dir / "conditional.sqlite")

    from ma_poc.scripts.jugnu_runner import _SimpleProfileStore

    profile_store = _SimpleProfileStore(_MA_POC_ROOT / "config" / "profiles")

    # -- Create CrawlTasks --
    tasks: list[CrawlTask] = []
    for c in candidates:
        url = c["url"]
        if not url:
            log.warning("Skipping %s — no URL", c["property_id"])
            continue
        # Normalise HTTP to HTTPS.
        if url.startswith("http://"):
            url = "https://" + url[7:]
        tasks.append(
            CrawlTask(
                url=url,
                property_id=c["property_id"],
                priority=0,
                budget_ms=45_000,
                reason=TaskReason.RETRY,
                render_mode=RenderMode.RENDER,
            )
        )

    log.info("Created %d retry tasks", len(tasks))

    # -- Process tasks through L1-L4 concurrently --
    from ma_poc.scripts.concurrency import AsyncPool, SystemResources
    from ma_poc.scripts.jugnu_runner import (
        _format_output,
        _make_failed_record,
        _process_property,
    )

    res = SystemResources.detect()
    pool_size = res.optimal_pool_size()
    log.info("System resources: %s → pool_size=%d", res.summary(), pool_size)
    pool = AsyncPool(pool_size)

    async def _retry_one(task: Any) -> dict[str, Any]:
        log.info("Retrying %s (%s)", task.property_id, task.url)
        try:
            result = await _process_property(
                task,
                cost_ledger,
                profile_store,
                frontier,
                dlq,
                data_dir,
            )
            meta = result.setdefault("_meta", {})
            meta["retry"] = True
            meta["retry_source_run"] = run_date

            csv_row = csv_rows.get(task.property_id, {})
            return _format_output(result, csv_row, schema_version)
        except Exception as exc:
            log.error("Property %s crashed on retry: %s", task.property_id, exc)
            failed = _make_failed_record(
                task.property_id,
                task.url,
                str(exc),
                schema_version,
            )
            failed_meta = failed.setdefault("_meta", {})
            failed_meta["retry"] = True
            failed_meta["retry_source_run"] = run_date
            return failed

    retry_results = await pool.map(_retry_one, [(t,) for t in tasks])

    retried_properties: list[dict[str, Any]] = []
    for r in retry_results:
        if isinstance(r, Exception):
            log.error("Retry task returned exception: %s", r)
            continue
        retried_properties.append(r)

    # -- Write retried results --
    # In sharded mode (task_count > 1), each shard writes ONLY its retried
    # records to a per-shard file. The merge step (jugnu_retry_merge.py)
    # consolidates all shards back into properties.json after the job
    # finishes. This avoids torn writes from concurrent shards clobbering
    # each other's successes.
    #
    # In single-shard mode (task_count == 1, the legacy behaviour), we
    # merge in-place into properties.json exactly as before.
    if task_count > 1:
        shard_path = output_run_dir / f"properties.retry.shard{task_index:02d}.json"
        try:
            shard_path.write_text(
                json.dumps(retried_properties, indent=2, default=str),
                encoding="utf-8",
            )
            log.info(
                "Shard %d/%d wrote %d retried records to %s",
                task_index,
                task_count,
                len(retried_properties),
                shard_path,
            )
        except OSError as exc:
            log.error("Failed to write shard file %s: %s", shard_path, exc)
        merged = retried_properties  # report scope: this shard only
    else:
        existing_props: list[dict[str, Any]] = []
        output_props_path = output_run_dir / "properties.json"
        # If retrying into the same day's directory, load the existing file.
        if output_props_path.exists():
            try:
                existing_props = json.loads(output_props_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing_props = []
        # Also load from the source run if it's a different directory.
        elif source_run_dir != output_run_dir:
            source_props_path = source_run_dir / "properties.json"
            if source_props_path.exists():
                try:
                    existing_props = json.loads(source_props_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    existing_props = []

        merged = _merge_properties(existing_props, retried_properties)

        try:
            output_props_path.write_text(
                json.dumps(merged, indent=2, default=str),
                encoding="utf-8",
            )
            log.info("Wrote %d properties to %s", len(merged), output_props_path)
        except OSError as exc:
            log.error("Failed to write properties.json: %s", exc)

    # -- Report --
    cost_rollup = cost_ledger.total()
    slo_violations = slo_check(cost_rollup, merged)
    report = build_run_report(merged, output_run_dir, today, cost_rollup, slo_violations)

    # Also write a retry-specific summary.
    retry_succeeded = sum(
        1
        for p in retried_properties
        if "FAIL" not in str(p.get("_meta", {}).get("scrape_tier_used", "")).upper() and p.get("units")
    )
    retry_failed = len(retried_properties) - retry_succeeded

    retry_summary = {
        "run_date": today,
        "source_run_date": run_date,
        "mode": mode,
        "generated_at": datetime.now(UTC).isoformat(),
        "retry_totals": {
            "candidates": len(candidates),
            "tasks_created": len(tasks),
            "retried": len(retried_properties),
            "succeeded": retry_succeeded,
            "failed": retry_failed,
            "improvement": f"{retry_succeeded}/{len(retried_properties)} recovered",
        },
        "merged_totals": report["totals"],
    }

    shard_suffix = f".shard{task_index:02d}" if task_count > 1 else ""
    retry_report_path = output_run_dir / f"report_retry_{mode}{shard_suffix}.json"
    retry_report_path.write_text(
        json.dumps(retry_summary, indent=2, default=str),
        encoding="utf-8",
    )

    # Retry markdown.
    retry_md_path = output_run_dir / f"report_retry_{mode}{shard_suffix}.md"
    md_lines = [
        f"# Jugnu Retry Report — {today}",
        "",
        f"- **Mode:** {mode}",
        f"- **Source run:** {run_date}",
        f"- **Candidates:** {len(candidates)}",
        f"- **Retried:** {len(retried_properties)}",
        f"- **Recovered:** {retry_succeeded}",
        f"- **Still failed:** {retry_failed}",
        "",
        "## Retry Results",
        "",
        "| Property | Prior | After Retry | Units |",
        "|---|---|---|---|",
    ]
    for i, prop in enumerate(retried_properties):
        meta = prop.get("_meta", {})
        pid = meta.get("canonical_id", "?")
        tier = meta.get("scrape_tier_used", "?")
        verdict = meta.get("verdict", "?")
        units = len(prop.get("units", []))
        prior = candidates[i].get("prior_tier") or candidates[i].get("prior_status", "?")
        md_lines.append(f"| `{pid}` | {prior} | {verdict} ({tier}) | {units} |")

    md_lines.extend(
        [
            "",
            "## Merged Run Totals",
            "",
            f"- Properties: {report['totals']['properties']}",
            f"- Succeeded: {report['totals']['succeeded']}",
            f"- Failed: {report['totals']['failed']}",
            f"- Success rate: {report['totals']['success_rate_pct']}%",
            "",
        ]
    )
    retry_md_path.write_text("\n".join(md_lines), encoding="utf-8")

    log.info("Retry report: %s", retry_report_path)

    # -- Cleanup --
    cost_ledger.close()
    frontier.close()
    cond_cache.close()
    events.shutdown()

    log.info(
        "Retry complete: %d/%d recovered, %d still failed",
        retry_succeeded,
        len(retried_properties),
        retry_failed,
    )
    return retry_summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Jugnu retry runner — retry failed properties from a prior run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/jugnu_retry_runner.py --retry-errors
  python scripts/jugnu_retry_runner.py --retry-errors --run-date 2026-04-17
  python scripts/jugnu_retry_runner.py --resume --run-date 2026-04-18
  python scripts/jugnu_retry_runner.py --retry-errors --limit 10
  python scripts/jugnu_retry_runner.py --retry-errors --csv config/properties.csv
        """,
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--retry-errors",
        action="store_true",
        help="Retry FAILED + 0-unit properties",
    )
    mode_group.add_argument(
        "--resume",
        action="store_true",
        help="Re-process everything that isn't a clean success",
    )
    mode_group.add_argument(
        "--unprocessed",
        action="store_true",
        help=(
            "Process CSV rows that are missing from properties.json — "
            "recovers properties from a stuck/timed-out shard whose slice "
            "never reached the output file. Reads the catalog from the "
            "DataProvider by default; pass --csv to override."
        ),
    )
    parser.add_argument(
        "--run-date",
        type=str,
        default=None,
        help="Source run date to retry (YYYY-MM-DD). Default: latest run.",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help=(
            "Optional CSV override. When set, replaces the DataProvider's "
            "catalog (the `properties` table) as the source of truth for "
            "URL lookups and --unprocessed diffing. Useful for retrying "
            "legacy runs whose URLs aren't in the DB yet."
        ),
    )
    parser.add_argument("--data-dir", type=Path, default=_MA_POC_ROOT / "data")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--schema-version",
        choices=["v1", "v2"],
        default=None,
        help="Output schema version (default: env SCHEMA_VERSION or v1)",
    )
    args = parser.parse_args()

    if args.retry_errors:
        mode = "retry_errors"
    elif args.resume:
        mode = "resume"
    else:
        mode = "unprocessed"

    from ma_poc.scripts.jugnu_runner import _resolve_schema_version

    schema_version = _resolve_schema_version(args)

    report = asyncio.run(
        run_retry(
            data_dir=args.data_dir,
            mode=mode,
            run_date=args.run_date,
            csv_path=args.csv,
            limit=args.limit,
            schema_version=schema_version,
        )
    )

    exit_status = report.get("exit_status", "")
    if exit_status == "FATAL":
        print(f"Fatal: {report.get('error', 'unknown')}")
        return 2
    if exit_status == "OK_NOTHING_TO_DO":
        print("Nothing to retry.")
        return 0

    totals = report.get("retry_totals", {})
    print(f"Retry complete: {totals.get('improvement', '?')}")
    return 0 if totals.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
