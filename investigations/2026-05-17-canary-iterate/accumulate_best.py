#!/usr/bin/env python3
"""Persistent per-website best-result accumulator for canary iterations.

Reads a completed canary run's per-shard ``properties.json`` from
``gs://jugnu-canary/runs/<run_date>/shard_*/`` and merges every SUCCESS
into a running ledger at ``gs://jugnu-canary/best_results.jsonl``,
keyed by canonical_id.

"Best" = the SUCCESS with the most units; ties broken by most recent
run_date. Failures never overwrite a prior success — once a site has
been scraped successfully by any iteration, that data is preserved even
if a later code change regresses it.

Each ledger line:
  {
    "canonical_id": "231155",
    "website": "https://www.oakbendcommons.com/",
    "best_units": 12,
    "best_tier": "TIER_1_KNOCK_API",
    "best_run": "2026-05-17-canary1",
    "first_success_run": "2026-05-17-canary1",
    "success_count": 2,            # how many iterations have succeeded
    "history": [                   # one entry per run that produced a verdict
      {"run": "2026-05-17-canary1", "verdict": "SUCCESS",
       "tier": "TIER_1_KNOCK_API", "units": 12}
    ]
  }

Usage:
  python accumulate_best.py <run_date> [--bucket gs://jugnu-canary] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

DEFAULT_BUCKET = "gs://jugnu-canary"


def _gcs_cat(uri: str) -> bytes | None:
    p = subprocess.run(
        ["gcloud", "storage", "cat", uri],
        capture_output=True,
        timeout=120,
    )
    return p.stdout if p.returncode == 0 else None


def _gcs_ls(uri: str) -> list[str]:
    p = subprocess.run(
        ["gcloud", "storage", "ls", uri],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if p.returncode != 0:
        return []
    return [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]


def _load_ledger(bucket: str) -> dict[str, dict[str, Any]]:
    raw = _gcs_cat(f"{bucket}/best_results.jsonl")
    ledger: dict[str, dict[str, Any]] = {}
    if not raw:
        return ledger
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        cid = str(rec.get("canonical_id", ""))
        if cid:
            ledger[cid] = rec
    return ledger


def _iter_run_properties(bucket: str, run_date: str) -> list[dict[str, Any]]:
    """Yield every property dict across all shards for a run."""
    shard_dirs = _gcs_ls(f"{bucket}/runs/{run_date}/")
    out: list[dict[str, Any]] = []
    for d in shard_dirs:
        if not d.rstrip("/").endswith(tuple(f"shard_{i}" for i in range(200))):
            # tolerate any shard_* dir name
            if "/shard_" not in d:
                continue
        raw = _gcs_cat(f"{d}properties.json")
        if not raw:
            continue
        try:
            props = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(props, list):
            out.extend(props)
    return out


def merge(run_date: str, bucket: str, dry_run: bool) -> int:
    ledger = _load_ledger(bucket)
    props = _iter_run_properties(bucket, run_date)
    if not props:
        print(f"No properties.json found for run {run_date} in {bucket}", file=sys.stderr)
        return 1

    new_success = improved = unchanged = regressions_shielded = 0

    for p in props:
        meta = p.get("_meta", {}) or {}
        er = p.get("_extract_result", {}) or {}
        cid = str(meta.get("canonical_id") or "")
        if not cid:
            continue
        verdict = meta.get("verdict", "")
        tier = er.get("tier_used", "")
        unit_list = p.get("units", []) or []
        units = len(unit_list)
        website = p.get("website", "")

        # Unit-level = real unit_id (not synthesized "inferred_") AND a
        # concrete rent. Floorplan-only stubs (JSON-LD aggregates) carry
        # inferred ids / null rent and do NOT meet the goal.
        def _is_unit_level(u: dict[str, Any]) -> bool:
            uid = str(u.get("unit_id", "") or "")
            if not uid or uid.startswith("inferred_"):
                return False
            return u.get("rent_low") is not None or u.get("rent_high") is not None

        unit_level_n = sum(1 for u in unit_list if _is_unit_level(u))
        # A real success requires at least one true unit-level row.
        is_success = verdict == "SUCCESS" and unit_level_n > 0
        floorplan_only = verdict == "SUCCESS" and units > 0 and unit_level_n == 0

        # Tier class — Tier-1 deterministic API is the goal; LLM is a
        # fallback that still flags "needs a Tier-1 path".
        t = (tier or "").upper()
        if t.startswith("TIER_1"):
            tier_class = "TIER1"
        elif "LLM" in t:
            tier_class = "LLM"
        elif t.startswith(("TIER_2", "TIER_3", "TIER_MERGED")):
            tier_class = "T2_T3"
        else:
            tier_class = "OTHER"
        # Even a unit-level LLM/T2/T3 win is not the goal — keep hunting Tier-1.
        needs_tier1 = is_success and tier_class != "TIER1"

        # Correctness sanity on the unit-level rows: rent in a plausible
        # US multifamily band, beds/baths/sqft sane. Garbage never banked.
        def _sane(u: dict[str, Any]) -> bool:
            r = u.get("rent_low") or u.get("rent_high")
            try:
                r = float(r) if r is not None else None
            except (TypeError, ValueError):
                return False
            if r is None or not (200 <= r <= 25000):
                return False
            b = u.get("beds")
            if b is not None and not (0 <= b <= 8):
                return False
            a = u.get("area")
            if a is not None and a not in (-1, 0) and not (80 <= a <= 12000):
                return False
            return True

        sane_n = sum(1 for u in unit_list if _is_unit_level(u) and _sane(u))
        correctness_ok = is_success and sane_n >= max(1, unit_level_n // 2)

        entry = ledger.get(cid)
        status = (
            "UNIT_LEVEL" if is_success
            else "FLOORPLAN_ONLY" if floorplan_only
            else "FAILED"
        )
        hist_item = {
            "run": run_date,
            "verdict": verdict,
            "tier": tier,
            "tier_class": tier_class,
            "status": status,
            "units_total": units,
            "units_unit_level": unit_level_n,
            "units_sane": sane_n,
            "needs_tier1": needs_tier1,
            "correctness_ok": correctness_ok,
        }

        if entry is None:
            entry = {
                "canonical_id": cid,
                "website": website,
                "best_units": unit_level_n if is_success else 0,
                "best_tier": tier if is_success else "",
                "best_tier_class": tier_class if is_success else "",
                "best_run": run_date if is_success else "",
                "first_success_run": run_date if is_success else "",
                "success_count": 1 if is_success else 0,
                "ever_floorplan_only": floorplan_only,
                # The real objective: a Tier-1, unit-level, sane scrape.
                "needs_tier1": needs_tier1,
                "goal_met": is_success and tier_class == "TIER1" and correctness_ok,
                "history": [hist_item],
            }
            ledger[cid] = entry
            if is_success:
                new_success += 1
            continue

        entry["history"].append(hist_item)
        if website and not entry.get("website"):
            entry["website"] = website
        if floorplan_only:
            entry["ever_floorplan_only"] = True

        if is_success:
            entry["success_count"] = entry.get("success_count", 0) + 1
            if not entry.get("first_success_run"):
                entry["first_success_run"] = run_date
            prev_class = entry.get("best_tier_class", "")
            # Promotion priority: a TIER1 win beats a non-TIER1 stored
            # best even with fewer rows (deterministic+correct > LLM).
            # Otherwise: more unit-level rows wins.
            upgrade_to_tier1 = tier_class == "TIER1" and prev_class != "TIER1"
            more_rows = unit_level_n > entry.get("best_units", 0)
            if upgrade_to_tier1 or (
                more_rows and not (prev_class == "TIER1" and tier_class != "TIER1")
            ):
                entry["best_units"] = unit_level_n
                entry["best_tier"] = tier
                entry["best_tier_class"] = tier_class
                entry["best_run"] = run_date
                improved += 1
            else:
                unchanged += 1
            # Recompute objective flags from the current best.
            entry["needs_tier1"] = entry.get("best_tier_class") != "TIER1"
            if tier_class == "TIER1" and correctness_ok:
                entry["goal_met"] = True
        else:
            # No unit-level data this run but we already have a stored
            # unit-level success → ledger keeps the old best (shielded).
            if entry.get("best_units", 0) > 0:
                regressions_shielded += 1

    lines = [json.dumps(ledger[c], ensure_ascii=False) for c in sorted(ledger)]
    have = sum(1 for c in ledger if ledger[c].get("best_units", 0) > 0)
    goal_met = sum(1 for c in ledger if ledger[c].get("goal_met"))
    llm_only = sum(
        1 for c in ledger
        if ledger[c].get("best_units", 0) > 0 and ledger[c].get("best_tier_class") == "LLM"
    )
    t2t3_only = sum(
        1 for c in ledger
        if ledger[c].get("best_units", 0) > 0
        and ledger[c].get("best_tier_class") in ("T2_T3", "OTHER")
    )
    fp_only_now = sum(
        1 for c in ledger
        if ledger[c].get("best_units", 0) == 0 and ledger[c].get("ever_floorplan_only")
    )
    body = "\n".join(lines) + "\n"

    print(f"Run {run_date}: {len(props)} props processed")
    print(f"  new UNIT-LEVEL successes:   {new_success}")
    print(f"  improved (tier-up/more rows): {improved}")
    print(f"  unchanged:                 {unchanged}")
    print(f"  regressions shielded:      {regressions_shielded}")
    print(f"Ledger: {len(ledger)} sites")
    print(f"  GOAL MET (Tier-1 + unit-level + sane): {goal_met}")
    print(f"  unit-level via LLM (needs Tier-1 path): {llm_only}")
    print(f"  unit-level via T2/T3 (needs Tier-1 path): {t2t3_only}")
    print(f"  any unit-level on record: {have}")
    print(f"  stuck floorplan-only: {fp_only_now}")

    if dry_run:
        print("[dry-run] not writing ledger")
        return 0

    tmp = "/tmp/best_results.jsonl"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    up = subprocess.run(
        ["gcloud", "storage", "cp", tmp, f"{bucket}/best_results.jsonl"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if up.returncode != 0:
        print(f"Upload failed: {up.stderr}", file=sys.stderr)
        return 1
    print(f"Ledger written → {bucket}/best_results.jsonl")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_date", help="e.g. 2026-05-17-canary1")
    ap.add_argument("--bucket", default=DEFAULT_BUCKET)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    return merge(a.run_date, a.bucket, a.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
