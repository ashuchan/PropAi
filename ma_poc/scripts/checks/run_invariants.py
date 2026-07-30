"""Run the whole-run invariants over a COMPLETE, assembled run.

Why this exists as a separate entrypoint rather than living in the runner.

``run_jugnu`` already calls ``_emit_run_invariant_issues`` at end-of-run, but a
production run is sharded across many Cloud Run tasks, each with its own
filesystem and its own slice of the corpus. That call therefore compares only
the properties one task emitted, which makes it 98% blind to the very defect it
was written for. Measured by replaying the 22 known collision groups from
``run-2026-07-27-full-0d54ca7`` (4,982 properties, 100 shards) against that
run's real shard assignment:

    groups a per-shard check catches :  1 / 22
    duplicate rows surfaced         :     76
    duplicate rows MISSED           :  3,793

Every large group is fully spread — the 294-row x7 AppFolio payload spans 7 of
7 shards, Redwood's 149-row pair spans 2 of 2. Sharding behaves like a hash, so
two colliding properties co-locate with probability ~1/n_shards.

The cross-run half is worse than narrow, it is inert: only
``PROFILE_GCS_PREFIX`` is synced down to a task, never prior ``runs/``, so
``_find_prior_run_properties`` returns None on every shard task and envelope
drift never executes in production at all.

There is no post-assembly hook to move the call into — ``sync/run_to_pg.py`` is
also invoked per shard. Shards only converge once someone collects them, which
is exactly when this script should run.

Exit codes follow the ``checks/`` convention: 0 clean, 1 could not run
(no data), 2 findings.

Usage:
    python -m ma_poc.scripts.checks.run_invariants --run-dir data/v2/runs/2026-07-30
    python -m ma_poc.scripts.checks.run_invariants --run-dir <cur> --prior-run-dir <prev>
    python -m ma_poc.scripts.checks.run_invariants --run-dir <dir> --json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from ma_poc.validation.run_invariants import (
    DEFAULT_ENVELOPE_RETENTION,
    DEFAULT_MIN_ROWS_FOR_COLLISION,
    find_envelope_drift,
    find_identical_payload_groups,
)

_WIDTH = 78


def load_run_properties(run_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Union every ``properties.json`` under *run_dir*, across every layout.

    THREE layouts exist in the wild and all must work, because which one you get
    depends on how the run was collected rather than on anything semantic:

    * ``{run_dir}/shard_*/properties.json`` — a collected production run. Each
      Cloud Run task uploads its whole run dir to
      ``gs://{bucket}/runs/{date}/shard_{task_idx}/`` (``shard_entry.py``), so
      this is what a downloaded run looks like.
    * ``{run_dir}/shard_*.json``            — the flattened shape. Precedent:
      ``scripts/backfill_winning_url_from_events.py`` handles both, which is how
      we know both occur.
    * ``{run_dir}/properties.json``         — a single-process or local run, and
      what ``retry_entry.py`` produces when it coalesces shards.

    Recurses as a last resort so an unexpected nesting depth degrades to
    "slower" rather than "silently reads nothing". Reading nothing is the
    failure mode that matters here: it would print a clean bill of health for a
    run this script never actually looked at.

    Args:
        run_dir: A run directory, or anything containing properties.json files.

    Returns:
        (properties, sources) where sources are the files actually read,
        relative to *run_dir* — printed so a surprising population size can be
        traced to the files behind it.
    """
    paths = sorted(run_dir.glob("shard_*/properties.json"))
    if not paths:
        paths = sorted(run_dir.glob("shard_*.json"))
    if not paths:
        direct = run_dir / "properties.json"
        paths = [direct] if direct.is_file() else sorted(run_dir.rglob("properties.json"))

    props: list[dict[str, Any]] = []
    sources: list[str] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  WARN unreadable, skipped: {path} ({exc})")
            continue
        if isinstance(data, list):
            props.extend(p for p in data if isinstance(p, dict))
            sources.append(str(path.relative_to(run_dir)))
    return props, sources


def _dedupe_by_id(properties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per property id, later shards winning.

    A property can legitimately appear twice — a retry shard, or an overlapping
    collection. Feeding both copies to the collision check would make every
    duplicated property collide with ITSELF and manufacture findings, which is
    the fastest way to get a real detector switched off.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for prop in properties:
        pid = str(prop.get("apartment_id") or prop.get("property_id") or id(prop))
        by_id[pid] = prop
    return list(by_id.values())


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Assembled run directory (contains shard_*/properties.json or properties.json)",
    )
    parser.add_argument(
        "--prior-run-dir",
        type=Path,
        default=None,
        help="Previous run, for the envelope-drift half. Omitted = that half is SKIPPED, not passed.",
    )
    parser.add_argument(
        "--min-rows",
        type=int,
        default=DEFAULT_MIN_ROWS_FOR_COLLISION,
        help=f"Minimum payload rows for a collision to count (default {DEFAULT_MIN_ROWS_FOR_COLLISION})",
    )
    parser.add_argument(
        "--retention",
        type=float,
        default=DEFAULT_ENVELOPE_RETENTION,
        help=f"Envelope-width retention floor (default {DEFAULT_ENVELOPE_RETENTION})",
    )
    parser.add_argument(
        "--max-print",
        type=int,
        default=40,
        help="Cap on rows printed per section. Anything suppressed is stated explicitly.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON to stdout instead of a table")
    args = parser.parse_args(argv)

    if not args.run_dir.is_dir():
        print(f"NOT A DIRECTORY: {args.run_dir}")
        return 1

    properties, sources = load_run_properties(args.run_dir)
    if not properties:
        print(f"NO PROPERTIES FOUND under {args.run_dir}")
        print("  expected shard_*/properties.json or properties.json")
        return 1

    n_raw = len(properties)
    properties = _dedupe_by_id(properties)

    groups = find_identical_payload_groups(properties, min_rows=args.min_rows)
    duplicate_rows = sum(g.n_rows * (len(g.property_ids) - 1) for g in groups)
    affected = {pid for g in groups for pid in g.property_ids}

    drifts: list[Any] = []
    prior_n = 0
    drift_checked = False
    if args.prior_run_dir is not None:
        if not args.prior_run_dir.is_dir():
            print(f"PRIOR RUN NOT A DIRECTORY: {args.prior_run_dir}")
            return 1
        prior, _ = load_run_properties(args.prior_run_dir)
        prior = _dedupe_by_id(prior)
        prior_n = len(prior)
        if prior:
            drift_checked = True
            drifts = find_envelope_drift(properties, prior, retention=args.retention)
        else:
            print(f"  WARN prior run has no properties: {args.prior_run_dir}")

    if args.json:
        print(
            json.dumps(
                {
                    "run_dir": str(args.run_dir),
                    "scope": "complete_run",
                    "n_files_read": len(sources),
                    "n_properties_raw": n_raw,
                    "n_properties_compared": len(properties),
                    "identical_payload_checked": True,
                    "identical_payload_groups": [
                        {
                            "property_ids": g.property_ids,
                            "property_names": g.property_names,
                            "n_rows": g.n_rows,
                            "detected_pms": g.detected_pms,
                            "differing_detection": g.is_suspicious,
                        }
                        for g in groups
                    ],
                    "duplicate_rows": duplicate_rows,
                    "n_properties_affected": len(affected),
                    "envelope_drift_checked": drift_checked,
                    "n_prior_properties": prior_n,
                    "envelope_drift": [
                        {
                            "property_id": d.property_id,
                            "property_name": d.property_name,
                            "findings": d.findings,
                        }
                        for d in drifts
                    ],
                },
                indent=2,
            )
        )
        return 0 if not groups and not drifts else 2

    print("=" * _WIDTH)
    print(f"RUN INVARIANTS — {args.run_dir}")
    print("=" * _WIDTH)
    print(f"files read              {len(sources)}")
    print(
        f"properties compared     {len(properties)}"
        + (f"  ({n_raw} before dedupe)" if n_raw != len(properties) else "")
    )
    print("scope                   complete_run")
    print()

    print("-" * _WIDTH)
    print("CROSS-PROPERTY IDENTICAL PAYLOAD")
    print("-" * _WIDTH)
    if not groups:
        print(f"  none — {len(properties)} properties compared")
    else:
        print(f"  {len(groups)} group(s) · {len(affected)} properties · {duplicate_rows:,} duplicate rows")
        print()
        print(f"  {'ROWS':>6} {'x':>3}  {'PMS':<26} {'DIFF-DETECT':<12} PROPERTIES")
        for g in groups[: args.max_print]:
            pms = ",".join(sorted(set(g.detected_pms)))[:26]
            names = ", ".join(n[:18] for n in g.property_names[:4])
            if len(g.property_names) > 4:
                names += f", +{len(g.property_names) - 4} more"
            print(f"  {g.n_rows:>6} {len(g.property_ids):>3}  {pms:<26} {'YES' if g.is_suspicious else '':<12} {names}")
        if len(groups) > args.max_print:
            print(f"  ... {len(groups) - args.max_print} further group(s) not shown (--max-print)")
    print()

    print("-" * _WIDTH)
    print("PUBLISHED ENVELOPE DRIFT")
    print("-" * _WIDTH)
    if not drift_checked:
        # Never let a skipped check read as a passed one.
        reason = (
            "no --prior-run-dir given"
            if args.prior_run_dir is None
            else f"prior run empty: {args.prior_run_dir}"
        )
        print(f"  SKIPPED — {reason}")
    elif not drifts:
        print(f"  none — compared against {prior_n} prior-run properties")
    else:
        print(f"  {len(drifts)} property(ies) drifted (vs {prior_n} prior-run properties)")
        print()
        for d in drifts[: args.max_print]:
            print(f"  {d.property_id:>9}  {d.property_name[:30]:<32} {'; '.join(d.findings)}")
        if len(drifts) > args.max_print:
            print(f"  ... {len(drifts) - args.max_print} further property(ies) not shown (--max-print)")
    print()

    print("=" * _WIDTH)
    total = len(groups) + len(drifts)
    print(f"FINDINGS: {total}" + ("" if drift_checked else "   (envelope drift NOT checked)"))
    print("=" * _WIDTH)
    return 0 if total == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
