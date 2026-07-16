"""Re-discovery queue consumer (CLI).

Reads the ``DEAD_URL``-verdict properties from a completed run (or an explicit
queue JSONL), joins property names / cities from ``config/properties.csv``,
re-derives each property's true current URL via
:mod:`ma_poc.discovery.rediscovery` (approach *a* — $0/deterministic mgmt-site
sitemap + name match), and writes the results as JSONL.

Why this exists: ``reporting.verdict.Verdict.DEAD_URL`` is terminal and routed
"to a re-discovery queue rather than the standard DLQ retry escalation" — but
that queue had no consumer. This is it.

Usage::

    # from a completed run (reads events.jsonl for DEAD_URL verdicts):
    python -m ma_poc.scripts.rediscover_dead_urls \
        --run-dir ma_poc/data/runs/2026-07-12 \
        --csv ma_poc/config/properties.csv \
        --out ma_poc/data/state/rediscovery_results.jsonl

    # from an explicit queue file (one entry dict per line):
    python -m ma_poc.scripts.rediscover_dead_urls \
        --queue queue.jsonl --csv ma_poc/config/properties.csv --out out.jsonl

Approach (b) — the gated web-search fallback — is a library capability
(``RediscoveryEngine(enable_web_search=True, search_fn=...)``) and is
intentionally NOT wired into this CLI: it needs an external search backend and
carries per-query cost. Without it, dead-DNS / dead-end-host properties are
emitted with status ``NEEDS_WEB_SEARCH`` so an operator can triage them.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from ma_poc.discovery.rediscovery import (
    RediscoveryEngine,
    RediscoveryEntry,
    RediscoveryResult,
)
from ma_poc.reporting.verdict import Verdict, scan_event_ledger_verdicts

log = logging.getLogger("rediscover_dead_urls")

_DEFAULT_CSV = "ma_poc/config/properties.csv"
_DEFAULT_OUT = "ma_poc/data/state/rediscovery_results.jsonl"


def load_csv_index(csv_path: Path) -> dict[str, dict[str, str]]:
    """Index ``properties.csv`` by ``apartmentid`` (BOM/CRLF tolerant).

    Returns ``{apartmentid: {"name", "city", "state", "website"}}``. Rows with
    no id are skipped. Never raises on a missing file — returns ``{}``.
    """
    index: dict[str, dict[str, str]] = {}
    if not csv_path.exists():
        log.warning("CSV not found: %s", csv_path)
        return index
    # utf-8-sig strips the BOM some exports prepend (Bug-hunt #10).
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            pid = (row.get("apartmentid") or row.get("property_id") or "").strip()
            if not pid:
                continue
            index[pid] = {
                "name": (row.get("name") or "").strip(),
                "city": (row.get("city") or "").strip(),
                "state": (row.get("state") or "").strip(),
                "website": (row.get("website") or row.get("url") or "").strip(),
            }
    return index


def load_entries_from_run(
    run_dir: Path, csv_index: dict[str, dict[str, str]]
) -> list[RediscoveryEntry]:
    """Build re-discovery entries from a run's ``DEAD_URL`` verdicts.

    The event ledger (``events.jsonl``) is the authoritative verdict source
    (see ``reporting.verdict.scan_event_ledger_verdicts``). Each DEAD_URL pid
    is joined to the CSV for its name / city / URL. Pids absent from the CSV
    are skipped with a warning (we cannot re-derive without a name).
    """
    verdicts = scan_event_ledger_verdicts(run_dir)
    entries: list[RediscoveryEntry] = []
    for pid, verdict in verdicts.items():
        if verdict != Verdict.DEAD_URL.value:
            continue
        meta = csv_index.get(pid)
        if meta is None or not meta.get("website"):
            log.warning("DEAD_URL pid %s missing from CSV (or no website); skipping", pid)
            continue
        entries.append(
            RediscoveryEntry(
                property_id=pid,
                name=meta["name"],
                original_url=meta["website"],
                city=meta.get("city", ""),
                state=meta.get("state", ""),
            )
        )
    return entries


def load_entries_from_queue(
    queue_path: Path, csv_index: dict[str, dict[str, str]]
) -> list[RediscoveryEntry]:
    """Build entries from an explicit queue JSONL.

    Each line is an object with at least a property id (``property_id`` or
    ``pid``). Missing ``name`` / ``city`` / ``state`` / URL fields are filled
    from the CSV by id. Malformed lines are skipped.
    """
    entries: list[RediscoveryEntry] = []
    for raw in queue_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("skipping malformed queue line: %.80s", raw)
            continue
        if not isinstance(obj, dict):
            continue
        pid = str(obj.get("property_id") or obj.get("pid") or "").strip()
        if not pid:
            continue
        meta = csv_index.get(pid, {})
        url = str(obj.get("original_url") or obj.get("url") or meta.get("website") or "").strip()
        if not url:
            log.warning("queue pid %s has no URL; skipping", pid)
            continue
        entries.append(
            RediscoveryEntry(
                property_id=pid,
                name=str(obj.get("name") or meta.get("name") or "").strip(),
                original_url=url,
                dead_reason=str(obj.get("dead_reason") or "").strip(),
                city=str(obj.get("city") or meta.get("city") or "").strip(),
                state=str(obj.get("state") or meta.get("state") or "").strip(),
            )
        )
    return entries


def write_results(results: Sequence[RediscoveryResult], out_path: Path) -> None:
    """Write results as JSONL to *out_path* (parent dirs created)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r.to_dict()) + "\n")


def summarize(results: Sequence[RediscoveryResult]) -> dict[str, int]:
    """Count results by status value."""
    return dict(Counter(r.status.value for r in results))


async def run(
    entries: Sequence[RediscoveryEntry],
    engine: RediscoveryEngine,
    out_path: Path,
    concurrency: int = 6,
) -> dict[str, int]:
    """Re-discover *entries* with *engine*, write JSONL, return the status summary."""
    results = await engine.rediscover_many(entries, concurrency=concurrency)
    write_results(results, out_path)
    return summarize(results)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Re-derive true URLs for DEAD_URL properties.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--run-dir", type=Path, help="Run dir with events.jsonl (DEAD_URL verdicts).")
    src.add_argument("--queue", type=Path, help="Explicit queue JSONL (one entry dict per line).")
    p.add_argument("--csv", type=Path, default=Path(_DEFAULT_CSV), help="properties.csv path.")
    p.add_argument("--out", type=Path, default=Path(_DEFAULT_OUT), help="Output JSONL path.")
    p.add_argument("--limit", type=int, default=0, help="Cap number of entries (0 = all).")
    p.add_argument("--concurrency", type=int, default=6, help="Max concurrent re-discoveries.")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint. Returns a process exit code."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _build_parser().parse_args(argv)

    csv_index = load_csv_index(args.csv)
    if args.run_dir is not None:
        entries = load_entries_from_run(args.run_dir, csv_index)
    else:
        entries = load_entries_from_queue(args.queue, csv_index)

    if args.limit and args.limit > 0:
        entries = entries[: args.limit]

    if not entries:
        log.info("no DEAD_URL entries to re-discover")
        write_results([], args.out)
        return 0

    log.info("re-discovering %d DEAD_URL propert%s", len(entries), "y" if len(entries) == 1 else "ies")
    engine = RediscoveryEngine()
    summary = asyncio.run(run(entries, engine, args.out, concurrency=args.concurrency))

    log.info("wrote %s", args.out)
    for status, count in sorted(summary.items()):
        log.info("  %-16s %d", status, count)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
