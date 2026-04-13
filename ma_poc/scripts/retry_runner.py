"""
Retry / resume runner for daily_runner.py pipeline.
=====================================================

Two modes of operation:

  --resume       Resume an interrupted or partially-failed run. Skips properties
                 that already have status=SUCCESS in the run ledger. Processes
                 everything else: new rows not yet attempted, FAILED, UNRESOLVED,
                 and rows missing from the ledger (interrupted mid-run).

  --retry-errors Retry properties that did not produce useful data in a previous
                 run: FAILED (scrape crashed/timed out), SUCCESS_WITH_ERRORS with
                 0 units, and SUCCESS with 0 units (all extraction tiers failed).
                 Skips properties that already have units extracted.
                 Useful for a targeted second pass after fixing parsers or timeouts.

Both modes:
  - Read the existing ledger.jsonl from data/runs/{date}/ to determine what
    was already processed and how it went.
  - Merge new results into the existing properties.json (replacing stale
    records for retried properties, appending newly-processed ones).
  - Append new ledger entries so subsequent retries see updated status.
  - Update state store as normal (upsert_property, upsert_units).
  - Produce an updated report.json / report.md reflecting the combined run.

The run date defaults to today. Use --run-date to target a specific prior run.

Usage:
  # Resume from where a run left off (skip successes, process the rest)
  python scripts/retry_runner.py --resume --csv config/properties.csv

  # Retry only the failures from today's run
  python scripts/retry_runner.py --retry-errors --csv config/properties.csv

  # Retry failures from a specific date
  python scripts/retry_runner.py --retry-errors --run-date 2026-04-12

  # Resume with a limit (process at most N remaining)
  python scripts/retry_runner.py --resume --limit 20

  # Retry errors with proxy
  python scripts/retry_runner.py --retry-errors --proxy http://user:pass@host:port
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Force UTF-8 stdout on Windows.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from daily_runner import (                                       # noqa: E402
    read_properties_csv, build_property_record, _scrape_one,
    _scrape_in_thread, _write_issues_jsonl, _append_ledger,
    load_ledger, TARGET_PROPERTY_FIELDS,
)
from concurrency import SystemResources                          # noqa: E402
from entrata import scrape                                       # noqa: E402
from identity import (                                           # noqa: E402
    resolve_identity, detect_duplicates, csv_get,
    NAME_KEYS, UNIQUE_ID_KEYS, PROPERTY_ID_KEYS,
    ADDRESS_KEYS, CITY_KEYS, STATE_KEYS, ZIP_KEYS,
    LAT_KEYS, LNG_KEYS, WEBSITE_KEYS,
)
from state_store import StateStore                               # noqa: E402
import validation as V                                           # noqa: E402
from scrape_properties import (                                  # noqa: E402
    transform_units_from_scrape, aggregate_unit_stats, _clean,
)

log = logging.getLogger("retry_runner")


def _write_retry_markdown(path: Path, report: dict) -> None:
    """Write a markdown report tailored to retry/resume runs."""
    lines: list[str] = []
    mode = report.get("retry_mode", "unknown")
    lines.append(f"# Retry Report ({mode}) — {report['run_date']}")
    lines.append("")
    lines.append(f"- **Mode:** {mode}")
    lines.append(f"- **Started:** {report['started_at']}")
    lines.append(f"- **Finished:** {report['finished_at']}")
    lines.append(f"- **Duration:** {report['duration_s']:.1f}s")
    lines.append(f"- **Exit status:** {report['exit_status']}")
    lines.append("")
    lines.append("## Totals")
    for k, v in report["totals"].items():
        lines.append(f"- {k}: **{v}**")
    lines.append("")
    lines.append("## Ledger state after retry")
    for status, count in sorted(report.get("ledger_after_retry", {}).items()):
        lines.append(f"- {status}: **{count}**")
    lines.append("")
    lines.append("## Issues (this retry pass)")
    iss = report.get("issues", {})
    lines.append(f"- Total: **{iss.get('total', 0)}**")
    for sev, n in iss.get("by_severity", {}).items():
        lines.append(f"  - {sev}: {n}")
    if iss.get("by_code"):
        lines.append("- Top codes:")
        for code, n in list(iss["by_code"].items())[:20]:
            lines.append(f"  - `{code}`: {n}")
    lines.append("")
    lines.append("## State diff")
    sd = report.get("state_diff", {})
    lines.append(f"- Carry-forward used: **{sd.get('carry_forward_count', 0)}** properties")
    lines.append(f"- Unit totals — extracted: {sd.get('units_extracted', 0)}, "
                 f"new: {sd.get('units_new', 0)}, updated: {sd.get('units_updated', 0)}, "
                 f"unchanged: {sd.get('units_unchanged', 0)}, "
                 f"disappeared: {sd.get('units_disappeared', 0)}, "
                 f"carried-forward: {sd.get('units_carried_forward', 0)}")
    lines.append("")
    if report.get("failed_properties"):
        lines.append("## Failed properties (first 50)")
        lines.append("| Row | Canonical ID | Reason |")
        lines.append("|---|---|---|")
        for fp in report["failed_properties"][:50]:
            reason = (fp.get("reason") or "").replace("|", "\\|")[:120]
            lines.append(f"| {fp['row_index']} | `{fp.get('canonical_id') or 'unresolved'}` | {reason} |")
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _configure_logging(run_dir: Path) -> None:
    log.handlers.clear()
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.addHandler(ch)

    run_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(run_dir / "retry_runner.log", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)


def _filter_rows_resume(
    rows: list[dict],
    identities: list,
    ledger: dict[str, dict],
) -> list[tuple[int, dict, Any]]:
    """
    Resume mode: return (original_index, row, identity) for rows that are NOT
    already SUCCESS in the ledger.

    A row is eligible for processing if:
      - Its canonical_id is not in the ledger at all (never attempted / interrupted)
      - Its ledger status is FAILED, UNRESOLVED, or any non-SUCCESS value
    """
    eligible = []
    for idx, (row, ident) in enumerate(zip(rows, identities)):
        cid = ident.canonical_id
        if cid is None:
            # Unresolved rows: retry if they were unresolved before (identity
            # data in CSV may have been corrected), or if not in ledger.
            eligible.append((idx, row, ident))
            continue
        entry = ledger.get(cid)
        if entry is None:
            # Never attempted — process it.
            eligible.append((idx, row, ident))
        elif entry.get("status") not in ("SUCCESS", "SUCCESS_WITH_ERRORS", "SKIPPED"):
            # Previously failed — retry.
            eligible.append((idx, row, ident))
    return eligible


def _filter_rows_retry_errors(
    rows: list[dict],
    identities: list,
    ledger: dict[str, dict],
) -> list[tuple[int, dict, Any]]:
    """
    Retry-errors mode: return rows that should be retried because the
    previous attempt did not produce useful data.

    Includes:
      - status=FAILED  (scrape crashed or timed out)
      - status=SUCCESS_WITH_ERRORS  with units_count=0
        (scrape ran but errors prevented extraction)
      - status=SUCCESS  with units_count=0
        (scrape completed cleanly but all extraction tiers failed
        to produce units — worth retrying after parser fixes)
    """
    eligible = []
    for idx, (row, ident) in enumerate(zip(rows, identities)):
        cid = ident.canonical_id
        if cid is None:
            continue  # Can't retry unresolved — no identity to match.
        entry = ledger.get(cid)
        if entry is None:
            continue
        status = entry.get("status", "")
        units = entry.get("units_count", 0)

        should_retry = (
            status == "FAILED"
            or (status == "SUCCESS_WITH_ERRORS" and units == 0)
            or (status == "SUCCESS" and units == 0)
        )
        if should_retry:
            eligible.append((idx, row, ident))
    return eligible


def _load_existing_properties(path: Path) -> list[dict]:
    """Load existing properties.json from a prior run, or empty list."""
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _merge_properties(
    existing: list[dict],
    new_records: list[dict],
) -> list[dict]:
    """
    Merge new property records into the existing list. If a property was
    retried, replace the old record; otherwise keep the original.

    Match key: _meta.canonical_id (preferred) or Unique ID.
    """
    def _get_cid(rec: dict) -> Optional[str]:
        meta = rec.get("_meta") or {}
        return meta.get("canonical_id") or rec.get("Unique ID")

    # Index new records by canonical_id for O(1) lookup.
    new_by_cid: dict[str, dict] = {}
    for rec in new_records:
        cid = _get_cid(rec)
        if cid:
            new_by_cid[cid] = rec

    merged: list[dict] = []
    seen_cids: set[str] = set()

    # Walk existing records, replacing any that were retried.
    for rec in existing:
        cid = _get_cid(rec)
        if cid and cid in new_by_cid:
            merged.append(new_by_cid.pop(cid))
            seen_cids.add(cid)
        else:
            merged.append(rec)
            if cid:
                seen_cids.add(cid)

    # Append any new records not already in the existing list (resume case).
    for cid, rec in new_by_cid.items():
        if cid not in seen_cids:
            merged.append(rec)

    return merged


async def run_retry(
    csv_path: Path,
    run_date: str,
    data_dir: Path,
    mode: str,
    limit: Optional[int],
    proxy: Optional[str],
    scrape_timeout_s: int,
) -> dict:
    """
    Execute a retry/resume pass over the pipeline.

    Args:
        mode: "resume" or "retry_errors"
    """
    started_at = datetime.now(timezone.utc)
    run_dir = data_dir / "runs" / run_date
    state_dir = data_dir / "state"
    raw_dir = run_dir / "raw_api"
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    _configure_logging(run_dir)

    properties_path = run_dir / "properties.json"
    ledger_path     = run_dir / "ledger.jsonl"
    issues_path     = run_dir / "issues.jsonl"

    all_issues: list[V.ValidationIssue] = []
    failed_properties: list[dict] = []

    # ── 1. Load CSV and resolve identities ──────────────────────────────
    try:
        rows = read_properties_csv(csv_path)
    except Exception as e:
        log.error(f"Fatal: could not read CSV: {e}")
        return {"exit_status": "FATAL", "error": str(e)}

    identities = [resolve_identity(row) for row in rows]

    # ── 2. Load existing ledger ─────────────────────────────────────────
    ledger = load_ledger(ledger_path)
    log.info(f"Loaded ledger: {len(ledger)} entries from {ledger_path.name}")

    ledger_summary = {}
    for entry in ledger.values():
        s = entry.get("status", "UNKNOWN")
        ledger_summary[s] = ledger_summary.get(s, 0) + 1
    log.info(f"Ledger breakdown: {ledger_summary}")

    # ── 3. Filter rows based on mode ────────────────────────────────────
    if mode == "resume":
        eligible = _filter_rows_resume(rows, identities, ledger)
        log.info(f"RESUME mode: {len(eligible)} rows to process "
                 f"({len(rows) - len(eligible)} already succeeded)")
    elif mode == "retry_errors":
        eligible = _filter_rows_retry_errors(rows, identities, ledger)
        # Break down WHY each row is eligible so the operator can see
        # the distribution of failure types.
        n_failed = sum(1 for _, _, ident in eligible
                       if ledger.get(ident.canonical_id or "", {}).get("status") == "FAILED")
        n_zero_units = len(eligible) - n_failed
        log.info(f"RETRY-ERRORS mode: {len(eligible)} rows to retry "
                 f"({n_failed} FAILED, {n_zero_units} SUCCESS/SUCCESS_WITH_ERRORS with 0 units)")
    else:
        log.error(f"Unknown mode: {mode}")
        return {"exit_status": "FATAL", "error": f"unknown mode: {mode}"}

    if not eligible:
        log.info("Nothing to process — all rows already succeeded or no failures to retry.")
        return {
            "exit_status": "OK_NOTHING_TO_DO",
            "run_date": run_date,
            "mode": mode,
            "rows_eligible": 0,
            "rows_total": len(rows),
        }

    if limit:
        eligible = eligible[:limit]
        log.info(f"Limited to {limit} rows")

    # ── 4. Load state store ─────────────────────────────────────────────
    state = StateStore(state_dir)
    state.load()

    # ── 5. Load existing output for merging ─────────────────────────────
    existing_properties = _load_existing_properties(properties_path)
    log.info(f"Existing properties.json has {len(existing_properties)} records")

    # ── 6. Process eligible rows ────────────────────────────────────────
    new_records: list[dict] = []
    processed_count = 0
    success_count = 0

    units_total = {"extracted": 0, "new": 0, "updated": 0, "unchanged": 0,
                   "disappeared": 0, "carried_forward": 0}
    carry_forward_count = 0

    # ── 6a. Pre-filter: separate scrapeable from immediately-skippable ──
    scrapeable: list[tuple[int, dict, Any, str]] = []  # (orig_idx, row, ident, url)

    for orig_idx, row, ident in eligible:
        url = csv_get(row, *WEBSITE_KEYS)
        cid = ident.canonical_id

        if cid is None:
            failed_properties.append({
                "row_index": orig_idx, "canonical_id": None,
                "reason": "IDENTITY_UNRESOLVED",
            })
            minimal = {f: None for f in TARGET_PROPERTY_FIELDS}
            minimal["Property Name"] = csv_get(row, *NAME_KEYS) or None
            minimal["Website"]       = url or None
            minimal["Update Date"]   = date.today().isoformat()
            minimal["units"]         = []
            minimal["_meta"] = {
                "canonical_id": None, "identity_source": "unresolved",
                "scrape_tier_used": None, "units_extracted": 0,
                "carry_forward_used": False, "was_known": False,
                "error": "could not resolve canonical_id from CSV row",
            }
            new_records.append(minimal)
            _append_ledger(ledger_path, {
                "canonical_id": None, "row_index": orig_idx,
                "status": "UNRESOLVED",
                "reason": "IDENTITY_UNRESOLVED",
                "retry_mode": mode,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            continue

        scrapeable.append((orig_idx, row, ident, url or ""))

    # ── 6b. Concurrent scraping phase (thread pool — true parallelism) ──
    sysres = SystemResources.detect()
    log.info(f"System resources: {sysres.summary()}")
    pool_size = sysres.optimal_pool_size()
    log.info(f"Scraping {len(scrapeable)} properties with {pool_size} threads")

    loop = asyncio.get_running_loop()
    scrape_results_raw: list[Any] = [None] * len(scrapeable)

    with ThreadPoolExecutor(
        max_workers=pool_size, thread_name_prefix="scrape"
    ) as executor:
        futures = [
            loop.run_in_executor(
                executor, _scrape_in_thread, item[3], proxy, scrape_timeout_s
            )
            for item in scrapeable
        ]
        scrape_results_raw = await asyncio.gather(*futures, return_exceptions=True)

    # ── 6c. Sequential post-processing (state mutations not concurrent) ──
    for task_idx, (orig_idx, row, ident, url) in enumerate(scrapeable):
        cid = ident.canonical_id
        assert cid is not None
        row_name = csv_get(row, *NAME_KEYS) or csv_get(row, *WEBSITE_KEYS) or f"row{orig_idx}"

        processed_count += 1
        log.info(f"[{processed_count}/{len(scrapeable)}] (row {orig_idx}) "
                 f"{cid} — {row_name}")

        per_prop_issues: list[V.ValidationIssue] = []

        if not url:
            per_prop_issues.append(V.error(
                V.CSV_MISSING_URL,
                f"row {orig_idx} has no Website/URL column",
                canonical_id=cid, row_index=orig_idx,
            ))

        # ── Collect scrape result ─────────────────────────────────────
        scrape_result_or_exc = scrape_results_raw[task_idx]
        scrape_result: dict = {"errors": [], "base_url": url}
        scrape_failed = False

        if isinstance(scrape_result_or_exc, Exception):
            e = scrape_result_or_exc
            tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            per_prop_issues.append(V.error(
                V.PIPELINE_EXCEPTION,
                f"exception during scrape: {e}",
                canonical_id=cid, row_index=orig_idx,
                details={"exception": str(e), "traceback": tb[-1500:]},
            ))
            scrape_result = {"errors": [str(e)], "base_url": url}
            scrape_failed = True
        elif isinstance(scrape_result_or_exc, dict) and scrape_result_or_exc.get("_exception"):
            e = scrape_result_or_exc["_exception"]
            tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            per_prop_issues.append(V.error(
                V.PIPELINE_EXCEPTION,
                f"exception during scrape: {e}",
                canonical_id=cid, row_index=orig_idx,
                details={"exception": str(e), "traceback": tb[-1500:]},
            ))
            scrape_result = scrape_result_or_exc
            scrape_failed = True
        elif not url:
            scrape_result = scrape_result_or_exc
            scrape_failed = True
        else:
            scrape_result = scrape_result_or_exc
            if scrape_result.get("_timeout"):
                per_prop_issues.append(V.error(
                    V.SCRAPE_TIMEOUT,
                    f"scrape timed out after {scrape_timeout_s}s",
                    canonical_id=cid, row_index=orig_idx,
                    details={"url": url},
                ))
                scrape_failed = True
            elif scrape_result.get("errors"):
                per_prop_issues.append(V.warning(
                    V.SCRAPE_FAILED,
                    f"scrape returned errors: {scrape_result['errors'][:2]}",
                    canonical_id=cid, row_index=orig_idx,
                    details={"errors": scrape_result["errors"]},
                ))
            if not scrape_failed and not scrape_result.get("_raw_api_responses"):
                per_prop_issues.append(V.warning(
                    V.SCRAPE_NO_APIS,
                    "no API responses intercepted — fallback tiers would be used",
                    canonical_id=cid, row_index=orig_idx,
                ))

        scrape_apis = len(scrape_result.get('_raw_api_responses') or [])
        scrape_tier = scrape_result.get('extraction_tier_used')
        scrape_errs = scrape_result.get('errors') or []
        log.info(f"  scrape: apis={scrape_apis}, tier={scrape_tier}, failed={scrape_failed}"
                 + (f", errors={scrape_errs[:2]}" if scrape_errs else ""))

        # ── Transform ─────────────────────────────────────────────────
        target_units: list[dict] = []
        try:
            target_units = transform_units_from_scrape(scrape_result)
        except Exception as e:
            per_prop_issues.append(V.error(
                V.PIPELINE_EXCEPTION,
                f"exception during unit transform: {e}",
                canonical_id=cid, row_index=orig_idx,
                details={"exception": str(e), "traceback": traceback.format_exc()[-1500:]},
            ))

        public_units = [{k: v for k, v in u.items() if not k.startswith("_")} for u in target_units]

        # ── Validate ──────────────────────────────────────────────────
        per_prop_issues.extend(V.validate_units(public_units, cid))

        if not public_units and not scrape_failed:
            per_prop_issues.append(V.warning(
                V.UNITS_EMPTY,
                "scrape succeeded but no units were extracted",
                canonical_id=cid, row_index=orig_idx,
                details={"apis": len(scrape_result.get("_raw_api_responses") or [])},
            ))

        # ── Carry-forward ─────────────────────────────────────────────
        carry_forward_used = False
        if (scrape_failed or not public_units) and state.is_known(cid):
            cf_units = state.carry_forward_units(cid, run_date)
            if cf_units:
                carry_forward_used = True
                carry_forward_count += 1
                units_total["carried_forward"] += len(cf_units)
                public_units = cf_units
                per_prop_issues.append(V.info(
                    V.UNITS_CARRIED_FORWARD,
                    f"carried forward {len(cf_units)} units from prior state",
                    canonical_id=cid, row_index=orig_idx,
                    details={"count": len(cf_units)},
                ))

        # ── Diff against unit state ───────────────────────────────────
        unit_diff = {"new": [], "updated": [], "unchanged": [], "disappeared": []}
        try:
            unit_diff = state.upsert_units(cid, public_units, run_date)
            units_total["extracted"]   += len(public_units)
            units_total["new"]         += len(unit_diff["new"])
            units_total["updated"]     += len(unit_diff["updated"])
            units_total["unchanged"]   += len(unit_diff["unchanged"])
            units_total["disappeared"] += len(unit_diff["disappeared"])
        except Exception as e:
            per_prop_issues.append(V.error(
                V.PIPELINE_EXCEPTION,
                f"exception during unit diff: {e}",
                canonical_id=cid, row_index=orig_idx,
                details={"exception": str(e)},
            ))

        # ── Upsert property state ─────────────────────────────────────
        try:
            state.upsert_property(cid, {
                "canonical_id": cid,
                "name":         csv_get(row, *NAME_KEYS),
                "address":      csv_get(row, *ADDRESS_KEYS),
                "city":         csv_get(row, *CITY_KEYS),
                "state":        csv_get(row, *STATE_KEYS),
                "zip":          csv_get(row, *ZIP_KEYS),
                "website":      url,
                "last_scrape_status": "FAILED" if scrape_failed else "SUCCESS",
                "last_units_count":   len(public_units),
            }, run_date)
        except Exception as e:
            per_prop_issues.append(V.error(
                V.PIPELINE_EXCEPTION,
                f"exception during property upsert: {e}",
                canonical_id=cid, row_index=orig_idx,
            ))

        # ── Save raw API bodies ───────────────────────────────────────
        raw = scrape_result.get("_raw_api_responses")
        if raw:
            try:
                safe_cid = "".join(c if c.isalnum() or c in "-_" else "_" for c in cid)[:80]
                with open(raw_dir / f"{safe_cid}.json", "w", encoding="utf-8") as f:
                    json.dump(raw, f, indent=2, default=str)
            except Exception as e:
                log.warning(f"  could not save raw API dump for {cid}: {e}")

        # ── Build record ──────────────────────────────────────────────
        state_snapshot = state.get_property(cid)
        try:
            rec = build_property_record(
                row, ident, scrape_result, public_units,
                state_snapshot, carry_forward_used,
            )
            new_records.append(rec)
        except Exception as e:
            per_prop_issues.append(V.error(
                V.PIPELINE_EXCEPTION,
                f"exception building property record: {e}",
                canonical_id=cid, row_index=orig_idx,
                details={"exception": str(e), "traceback": traceback.format_exc()[-1500:]},
            ))
            failed_properties.append({
                "row_index": orig_idx, "canonical_id": cid,
                "reason": f"build_property_record exception: {e}",
            })

        # Track failed properties.
        if scrape_failed and not carry_forward_used:
            failed_properties.append({
                "row_index": orig_idx, "canonical_id": cid,
                "reason": "SCRAPE_FAILED_NO_CARRY_FORWARD",
            })

        # Flush issues.
        all_issues.extend(per_prop_issues)
        _write_issues_jsonl(issues_path, per_prop_issues)

        # ── Ledger checkpoint ─────────────────────────────────────────
        ledger_status = "FAILED" if (scrape_failed and not carry_forward_used) else "SUCCESS"
        has_errors = any(i.severity == "ERROR" for i in per_prop_issues)
        if has_errors and ledger_status == "SUCCESS":
            ledger_status = "SUCCESS_WITH_ERRORS"
        _append_ledger(ledger_path, {
            "canonical_id": cid, "row_index": orig_idx,
            "status": ledger_status,
            "units_count": len(public_units),
            "carry_forward_used": carry_forward_used,
            "scrape_failed": scrape_failed,
            "error_count": sum(1 for i in per_prop_issues if i.severity == "ERROR"),
            "warning_count": sum(1 for i in per_prop_issues if i.severity == "WARNING"),
            "url": url,
            "retry_mode": mode,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        if ledger_status in ("SUCCESS", "SUCCESS_WITH_ERRORS"):
            success_count += 1

        log.info(f"  -> {ledger_status} | units={len(public_units)} "
                 f"(new={len(unit_diff['new'])}), "
                 f"issues={len(per_prop_issues)}, carry_forward={carry_forward_used}, "
                 f"url={url[:60] if url else 'none'}")

        # ── Incremental merge + save ──────────────────────────────────
        try:
            merged = _merge_properties(existing_properties, new_records)
            with open(properties_path, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            log.error(f"  incremental save failed: {e}")

    # ── 7. Final merge and save ─────────────────────────────────────────
    merged_final = _merge_properties(existing_properties, new_records)
    try:
        with open(properties_path, "w", encoding="utf-8") as f:
            json.dump(merged_final, f, indent=2, ensure_ascii=False, default=str)
        log.info(f"properties.json updated: {len(merged_final)} total records")
    except Exception as e:
        log.error(f"final properties.json save failed: {e}")

    # ── 8. Save state ───────────────────────────────────────────────────
    try:
        state.save()
        log.info(f"State saved: {len(state.property_index)} properties, "
                 f"{sum(len(u) for u in state.unit_index.values())} tracked units")
    except Exception as e:
        log.error(f"failed to save state: {e}")

    finished_at = datetime.now(timezone.utc)

    # ── 9. Build report ─────────────────────────────────────────────────
    # Reload the ledger to get the full picture (original + retry entries).
    updated_ledger = load_ledger(ledger_path)
    final_summary = {}
    for entry in updated_ledger.values():
        s = entry.get("status", "UNKNOWN")
        final_summary[s] = final_summary.get(s, 0) + 1

    report = {
        "run_date":     run_date,
        "retry_mode":   mode,
        "started_at":   started_at.isoformat(),
        "finished_at":  finished_at.isoformat(),
        "duration_s":   (finished_at - started_at).total_seconds(),
        "exit_status":  "OK",
        "csv_path":     str(csv_path),
        "data_dir":     str(data_dir),
        "totals": {
            "csv_rows_total":       len(rows),
            "rows_eligible":        len(eligible),
            "rows_processed":       processed_count,
            "rows_succeeded":       success_count,
            "rows_failed":          processed_count - success_count,
            "properties_in_output": len(merged_final),
        },
        "ledger_after_retry": final_summary,
        "issues":           V.summarise_issues(all_issues),
        "state_diff": {
            "carry_forward_count":    carry_forward_count,
            "units_extracted":        units_total["extracted"],
            "units_new":              units_total["new"],
            "units_updated":          units_total["updated"],
            "units_unchanged":        units_total["unchanged"],
            "units_disappeared":      units_total["disappeared"],
            "units_carried_forward":  units_total["carried_forward"],
        },
        "failed_properties": failed_properties,
    }

    try:
        # Write retry-specific report alongside the original.
        retry_report_path = run_dir / f"report_retry_{mode}.json"
        with open(retry_report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        # Write a retry-specific markdown report.
        retry_md_path = run_dir / f"report_retry_{mode}.md"
        _write_retry_markdown(retry_md_path, report)
        log.info(f"Reports written: {retry_report_path.name}, {retry_md_path.name}")
    except Exception as e:
        log.error(f"report write failed: {e}")

    log.info(f"=== Retry ({mode}) done in {report['duration_s']:.1f}s: "
             f"{success_count}/{processed_count} succeeded, "
             f"{len(merged_final)} total properties in output ===")
    log.info(f"Ledger state: {final_summary}")

    return report


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Retry / resume runner for daily_runner.py pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/retry_runner.py --resume
  python scripts/retry_runner.py --retry-errors
  python scripts/retry_runner.py --retry-errors --run-date 2026-04-12
  python scripts/retry_runner.py --resume --limit 20 --proxy http://host:port
        """,
    )
    mode_group = p.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--resume", action="store_true",
        help="Resume: skip successes, process everything else",
    )
    mode_group.add_argument(
        "--retry-errors", action="store_true",
        help="Retry FAILED + SUCCESS/SUCCESS_WITH_ERRORS that have 0 units",
    )
    p.add_argument("--csv",      default="config/properties.csv",
                   help="Path to properties CSV")
    p.add_argument("--data-dir", default="data",
                   help="Root data directory")
    p.add_argument("--run-date", default=None,
                   help="Target run date (YYYY-MM-DD); defaults to today")
    p.add_argument("--limit",    type=int, default=None,
                   help="Process at most N eligible rows")
    p.add_argument("--proxy",    default=None)
    p.add_argument("--scrape-timeout", type=int, default=180,
                   help="Per-property scrape timeout (seconds)")
    args = p.parse_args()

    csv_path = Path(args.csv)
    data_dir = Path(args.data_dir)
    run_date = args.run_date or date.today().isoformat()
    mode = "resume" if args.resume else "retry_errors"

    try:
        report = asyncio.run(run_retry(
            csv_path, run_date, data_dir, mode,
            args.limit, args.proxy, args.scrape_timeout,
        ))
        if report.get("exit_status") == "FATAL":
            sys.exit(2)
        if report.get("exit_status") == "OK_NOTHING_TO_DO":
            print("Nothing to do.")
            sys.exit(0)
        sys.exit(0)
    except KeyboardInterrupt:
        log.error("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        log.error(f"Fatal: {e}")
        log.error(traceback.format_exc())
        sys.exit(2)


if __name__ == "__main__":
    main()
