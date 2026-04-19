"""
Jugnu retry runner — retry failed properties from a prior run.

Two modes:

  --retry-errors   Retry properties that FAILED or produced 0 units in a
                   prior run.  Reads the prior run's properties.json (Jugnu
                   format) or ledger.jsonl (legacy format) to identify
                   candidates.

  --resume         Re-process everything that isn't a clean success.  Useful
                   after an interrupted run.

Both modes:
  - Create CrawlTasks with reason=RETRY and feed them through L1-L5.
  - Merge new results into the existing properties.json (replacing stale
    records, keeping untouched successes).
  - Produce an updated report.json / report.md.

Usage:
  # Retry failures from yesterday (auto-detects latest run)
  python scripts/jugnu_retry_runner.py --retry-errors

  # Retry failures from a specific date
  python scripts/jugnu_retry_runner.py --retry-errors --run-date 2026-04-17

  # Resume an interrupted run
  python scripts/jugnu_retry_runner.py --resume --run-date 2026-04-18

  # Retry with a limit
  python scripts/jugnu_retry_runner.py --retry-errors --limit 10

  # Retry using a CSV (needed when retrying legacy runs that don't store URLs)
  python scripts/jugnu_retry_runner.py --retry-errors --run-date 2026-04-17 \\
      --csv config/properties.csv
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
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
        candidates.append({
            "property_id": pid,
            "url": url,
            "prior_tier": meta.get("scrape_tier_used"),
            "prior_errors": meta.get("scrape_errors", []),
        })

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
            should_retry = (
                status == "FAILED"
                or (status in ("SUCCESS", "SUCCESS_WITH_ERRORS") and units == 0)
            )
        else:  # resume
            should_retry = status not in ("SUCCESS", "SUCCESS_WITH_ERRORS", "SKIPPED")

        if not should_retry:
            continue

        url = entry.get("url") or url_lookup.get(cid, "")
        if not url:
            log.warning("No URL for %s — skipping (provide --csv to resolve)", cid)
            continue

        candidates.append({
            "property_id": cid,
            "url": url,
            "prior_status": status,
            "prior_units": units,
        })

    return candidates


def _load_csv_lookup(csv_path: Path) -> dict[str, str]:
    """Build a {property_id: url} lookup from a CSV file.

    Args:
        csv_path: Path to properties CSV.

    Returns:
        Dict mapping property_id to URL.
    """
    import csv as csv_mod

    lookup: dict[str, str] = {}
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = csv_mod.DictReader(f)
            for row in reader:
                pid = (
                    row.get("property_id")
                    or row.get("Unique ID")
                    or row.get("Property ID")
                    or row.get("apartmentid")
                    or ""
                )
                url = (
                    row.get("url")
                    or row.get("Website")
                    or row.get("website")
                    or ""
                )
                if pid and url:
                    lookup[pid] = url
    except (OSError, KeyError) as exc:
        log.error("Could not read CSV %s: %s", csv_path, exc)
    return lookup


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
        mode: 'retry_errors' or 'resume'.
        run_date: Target run date (YYYY-MM-DD). None = latest.
        csv_path: Optional CSV for URL lookup (needed for legacy runs).
        limit: Max properties to retry.
        schema_version: "v1" or "v2" output format.

    Returns:
        Report dict.
    """
    from ma_poc.discovery.contracts import CrawlTask, TaskReason
    from ma_poc.fetch import fetch as jugnu_fetch
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

    # -- Build CSV lookups if provided --
    csv_lookup = _load_csv_lookup(csv_path) if csv_path else None
    # Full CSV row lookup for output formatting
    csv_rows: dict[str, dict[str, Any]] = {}
    if csv_path:
        import csv as csv_mod
        try:
            with open(csv_path, encoding="utf-8-sig", newline="") as f:
                for row in csv_mod.DictReader(f):
                    pid = (row.get("property_id") or row.get("Unique ID")
                           or row.get("Property ID") or row.get("apartmentid") or "")
                    if pid:
                        csv_rows[pid] = dict(row)
        except OSError:
            pass

    # -- Load candidates --
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

    if limit:
        candidates = candidates[:limit]

    log.info("Retrying %d properties (%s mode, schema %s) from %s",
             len(candidates), mode, schema_version, run_date)
    for c in candidates[:10]:
        log.info("  %s — %s", c["property_id"], c["url"][:60])
    if len(candidates) > 10:
        log.info("  ... and %d more", len(candidates) - 10)

    # -- Setup output directory (schema-namespaced) --
    today = date.today().isoformat()
    from ma_poc.scripts.jugnu_runner import _resolve_data_dirs
    output_run_dir, state_dir, cache_dir, schema_root = _resolve_data_dirs(
        data_dir, schema_version, today,
    )
    run_id = f"retry_{today}_{uuid.uuid4().hex[:8]}"

    # -- Setup L1/L5 infrastructure --
    events.configure(output_run_dir, run_id)
    cost_ledger = CostLedger(output_run_dir / "cost_ledger.db")

    from ma_poc.discovery.frontier import Frontier
    from ma_poc.discovery.dlq import Dlq

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
        tasks.append(CrawlTask(
            url=url,
            property_id=c["property_id"],
            priority=0,
            budget_ms=45_000,
            reason=TaskReason.RETRY,
            render_mode=RenderMode.RENDER,
        ))

    log.info("Created %d retry tasks", len(tasks))

    # -- Process tasks through L1-L4 concurrently --
    from ma_poc.scripts.concurrency import AsyncPool, SystemResources
    from ma_poc.scripts.jugnu_runner import (
        _format_output, _make_failed_record, _process_property,
    )

    res = SystemResources.detect()
    pool_size = res.optimal_pool_size()
    log.info("System resources: %s → pool_size=%d", res.summary(), pool_size)
    pool = AsyncPool(pool_size)

    async def _retry_one(task: Any) -> dict[str, Any]:
        log.info("Retrying %s (%s)", task.property_id, task.url)
        try:
            result = await _process_property(
                task, cost_ledger, profile_store, frontier, dlq, data_dir,
            )
            meta = result.setdefault("_meta", {})
            meta["retry"] = True
            meta["retry_source_run"] = run_date

            csv_row = csv_rows.get(task.property_id, {})
            return _format_output(result, csv_row, schema_version)
        except Exception as exc:
            log.error("Property %s crashed on retry: %s", task.property_id, exc)
            failed = _make_failed_record(
                task.property_id, task.url, str(exc), schema_version,
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

    # -- Merge into existing output --
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
        1 for p in retried_properties
        if "FAIL" not in str(p.get("_meta", {}).get("scrape_tier_used", "")).upper()
        and p.get("units")
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

    retry_report_path = output_run_dir / f"report_retry_{mode}.json"
    retry_report_path.write_text(
        json.dumps(retry_summary, indent=2, default=str),
        encoding="utf-8",
    )

    # Retry markdown.
    retry_md_path = output_run_dir / f"report_retry_{mode}.md"
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

    md_lines.extend([
        "",
        "## Merged Run Totals",
        "",
        f"- Properties: {report['totals']['properties']}",
        f"- Succeeded: {report['totals']['succeeded']}",
        f"- Failed: {report['totals']['failed']}",
        f"- Success rate: {report['totals']['success_rate_pct']}%",
        "",
    ])
    retry_md_path.write_text("\n".join(md_lines), encoding="utf-8")

    log.info("Retry report: %s", retry_report_path)

    # -- Cleanup --
    cost_ledger.close()
    frontier.close()
    cond_cache.close()
    events.shutdown()

    log.info("Retry complete: %d/%d recovered, %d still failed",
             retry_succeeded, len(retried_properties), retry_failed)
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
        "--retry-errors", action="store_true",
        help="Retry FAILED + 0-unit properties",
    )
    mode_group.add_argument(
        "--resume", action="store_true",
        help="Re-process everything that isn't a clean success",
    )
    parser.add_argument(
        "--run-date", type=str, default=None,
        help="Source run date to retry (YYYY-MM-DD). Default: latest run.",
    )
    parser.add_argument(
        "--csv", type=Path, default=None,
        help="CSV for URL lookup (needed when retrying legacy runs without URLs in output)",
    )
    parser.add_argument("--data-dir", type=Path, default=_MA_POC_ROOT / "data")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--schema-version", choices=["v1", "v2"], default=None,
        help="Output schema version (default: env SCHEMA_VERSION or v1)",
    )
    args = parser.parse_args()

    mode = "retry_errors" if args.retry_errors else "resume"

    from ma_poc.scripts.jugnu_runner import _resolve_schema_version
    schema_version = _resolve_schema_version(args)

    report = asyncio.run(run_retry(
        data_dir=args.data_dir,
        mode=mode,
        run_date=args.run_date,
        csv_path=args.csv,
        limit=args.limit,
        schema_version=schema_version,
    ))

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
