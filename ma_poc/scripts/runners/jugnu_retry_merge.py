"""Jugnu retry merge — consolidate per-shard retry outputs into properties.json.

When jugnu_retry_runner.py runs as a sharded Cloud Run job (task_count > 1),
each task writes its retried records to ``properties.retry.shard{NN}.json``
instead of merging in-place. This script runs after the sharded job completes,
merges all shard files into ``properties.json`` by canonical_id, regenerates
the run report, and (optionally) syncs the result to Postgres.

Single-task. Run after every sharded retry execution. Refuses to run if
fewer shard files are present than declared by --expect-shards.

Usage:
  python scripts/jugnu_retry_merge.py --run-date 2026-04-25 --expect-shards 5
  python scripts/jugnu_retry_merge.py --run-date 2026-04-25 --expect-shards 5 --mode errors
  python scripts/jugnu_retry_merge.py --run-date 2026-04-25 --skip-sync   # skip postgres sync

Exit codes:
  0  Merge succeeded
  1  Merge failed (write error, malformed shard, etc.)
  2  Usage error
  3  Precondition failed (fewer shards than expected)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_repo_root = Path(__file__).resolve().parent.parent.parent
_MA_POC_ROOT = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

log = logging.getLogger("jugnu_retry_merge")

_SHARD_FILE_RE = re.compile(r"^properties\.retry\.shard(\d{2})\.json$")


def _find_shard_files(run_dir: Path) -> list[tuple[int, Path]]:
    """Return [(shard_index, path), ...] sorted by shard_index."""
    found: list[tuple[int, Path]] = []
    if not run_dir.exists():
        return found
    for p in run_dir.iterdir():
        if not p.is_file():
            continue
        m = _SHARD_FILE_RE.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    found.sort(key=lambda x: x[0])
    return found


def _load_shard(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Could not read shard {path}: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError(f"Shard {path} did not contain a JSON array")
    return data


def _merge_records(
    base: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge `incoming` into `base` keyed by ``_meta.canonical_id``.

    Incoming records replace base records with the same canonical_id.
    Records in base without a match are kept as-is. Incoming records that
    don't appear in base are appended.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for prop in incoming:
        meta = prop.get("_meta", {}) or {}
        pid = meta.get("canonical_id")
        if pid:
            by_id[pid] = prop

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for prop in base:
        meta = prop.get("_meta", {}) or {}
        pid = meta.get("canonical_id")
        if pid and pid in by_id:
            out.append(by_id.pop(pid))
            seen.add(pid)
        else:
            out.append(prop)
            if pid:
                seen.add(pid)
    for pid, prop in by_id.items():
        if pid not in seen:
            out.append(prop)
    return out


def _detect_overlaps(shards: list[list[dict[str, Any]]]) -> list[str]:
    """Return canonical_ids that appear in more than one shard.

    Shards are designed to be disjoint by construction (round-robin slice
    in _shard_candidates). Any overlap indicates a sharding bug or a
    retry that ran with a different task_count than the job it's merging.
    Logged as a warning; not fatal — last-shard-wins for overlaps.
    """
    seen: dict[str, int] = {}
    overlaps: list[str] = []
    for idx, shard in enumerate(shards):
        for prop in shard:
            pid = (prop.get("_meta") or {}).get("canonical_id")
            if not pid:
                continue
            if pid in seen and seen[pid] != idx:
                overlaps.append(pid)
            else:
                seen[pid] = idx
    return overlaps


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Merge sharded Jugnu retry outputs.")
    parser.add_argument(
        "--run-date",
        required=True,
        help="Run-date directory under data/runs/ holding the shard files.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_MA_POC_ROOT / "data",
        help="Base data directory (default: ma_poc/data).",
    )
    parser.add_argument(
        "--expect-shards",
        type=int,
        required=True,
        help=(
            "Number of shard files we expect to find. The merge refuses "
            "to run if fewer are present (exit 3); more is allowed but "
            "logged as a warning."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["errors", "resume", "unprocessed"],
        default="errors",
        help="Used for naming the consolidated retry report.",
    )
    parser.add_argument(
        "--schema-version",
        choices=["v1", "v2"],
        default=None,
        help="Output schema version (default: env SCHEMA_VERSION or v1).",
    )
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="Skip the postgres sync step.",
    )
    args = parser.parse_args()

    from ma_poc.scripts.runners.jugnu import _resolve_data_dirs, _resolve_schema_version

    schema_version = _resolve_schema_version(args)
    output_run_dir, _state_dir, _cache_dir, _root = _resolve_data_dirs(
        args.data_dir, schema_version, args.run_date
    )

    if not output_run_dir.exists():
        log.error("Run directory does not exist: %s", output_run_dir)
        return 3

    shard_files = _find_shard_files(output_run_dir)
    log.info("Found %d shard files in %s", len(shard_files), output_run_dir)
    for idx, p in shard_files:
        log.info("  shard %02d -> %s", idx, p.name)

    if len(shard_files) < args.expect_shards:
        log.error(
            "Expected %d shard files, found %d. Refusing to merge a partial result.",
            args.expect_shards,
            len(shard_files),
        )
        return 3
    if len(shard_files) > args.expect_shards:
        log.warning(
            "Expected %d shard files but found %d — proceeding (extra shards merge in too).",
            args.expect_shards,
            len(shard_files),
        )

    # Load existing properties.json (the pre-retry state)
    props_path = output_run_dir / "properties.json"
    base: list[dict[str, Any]] = []
    if props_path.exists():
        try:
            base = json.loads(props_path.read_text(encoding="utf-8"))
            if not isinstance(base, list):
                log.error("properties.json is not a JSON array")
                return 1
        except (json.JSONDecodeError, OSError) as exc:
            log.error("Failed to read %s: %s", props_path, exc)
            return 1
    else:
        log.warning("No existing properties.json at %s — merging shards into empty base.", props_path)

    # Load all shards
    try:
        loaded = [_load_shard(p) for _i, p in shard_files]
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    overlaps = _detect_overlaps(loaded)
    if overlaps:
        log.warning(
            "Found %d canonical_ids appearing in multiple shards (last-shard-wins): %s",
            len(overlaps),
            overlaps[:10],
        )

    # Merge each shard into base, in shard-index order.
    merged = base
    total_retried = 0
    for shard_records in loaded:
        merged = _merge_records(merged, shard_records)
        total_retried += len(shard_records)

    # Atomic write: temp + rename
    tmp_path = props_path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(merged, indent=2, default=str), encoding="utf-8")
        tmp_path.replace(props_path)
    except OSError as exc:
        log.error("Failed to write merged properties.json: %s", exc)
        return 1
    log.info("Wrote %d properties to %s", len(merged), props_path)

    # Regenerate the run report from the merged set.
    try:
        from ma_poc.observability.cost_ledger import CostLedger
        from ma_poc.observability.slo_watcher import check as slo_check
        from ma_poc.reporting.run_report import build as build_run_report

        cost_ledger_path = output_run_dir / "cost_ledger.db"
        cost_rollup: dict[str, Any] = {}
        if cost_ledger_path.exists():
            cl = CostLedger(cost_ledger_path)
            cost_rollup = cl.total()
            cl.close()

        slo_violations = slo_check(cost_rollup, merged)
        report = build_run_report(merged, output_run_dir, args.run_date, cost_rollup, slo_violations)
        log.info(
            "Regenerated report: %d properties, %d succeeded, success_rate=%s%%",
            report["totals"]["properties"],
            report["totals"]["succeeded"],
            report["totals"].get("success_rate_pct", "?"),
        )
    except Exception as exc:
        log.warning("Report regeneration failed (merge still committed): %s", exc)

    # Consolidated retry summary across shards.
    summary = {
        "merged_at": datetime.now(UTC).isoformat(),
        "run_date": args.run_date,
        "mode": args.mode,
        "shards_found": len(shard_files),
        "shards_expected": args.expect_shards,
        "overlaps": overlaps,
        "total_retried_records": total_retried,
        "merged_properties": len(merged),
    }
    summary_path = output_run_dir / f"report_retry_{args.mode}.merged.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    log.info("Wrote merge summary: %s", summary_path)

    # Optional: sync to postgres
    if not args.skip_sync:
        try:
            from ma_poc.scripts.sync_run_to_pg import sync_run_to_postgres

            log.info("Syncing merged run to postgres...")
            # The merge writes a single, consolidated properties.json — a
            # single-writer sync (no shard_id) is correct here. Per-shard
            # syncs already happened during the sharded retry; this final
            # sync ensures the merged record set is the canonical truth.
            sync_run_to_postgres(
                run_date=args.run_date,
                data_dir=output_run_dir.parent.parent,  # schema_root (data/ or data/v2/)
                config_dir=_MA_POC_ROOT / "config",
                shard_id="merge",
            )
        except Exception as exc:
            log.error("Postgres sync failed (merge still committed to filesystem): %s", exc)
            return 1

    log.info("Merge complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
