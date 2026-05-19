"""Backfill profile ``winning_page_url`` from a prior run's events.jsonl.

2026-05-19. The 15b7aab canary run never persisted ``winning_page_url``
into the profile store (config/profiles is .dockerignored + ephemeral),
so yesterday's hard-won routing knowledge was thrown away — every
property re-cold-starts and the slow generic cascade re-times-out the
~79 dead-zone. ``winning_page_url`` is the ONE field dispatch uses to
skip the cascade (scraper.py:1740/1809/1950), so reconstructing it from
``events.jsonl`` and seeding the (JSON-on-GCS) profile store makes the
next run start warm — recovering the speedup without a re-scrape.

QUALITY GATE (per "only well-captured, not all 3,700"): a property is
seeded ONLY if its prior extraction passed the strict bar — non-LLM
deterministic tier, not partial/plan, >=80% of units have a real
``unit_id`` + ``rent_low>0``, >=80% have ``beds``. Seeding a mediocre
extraction's URL would lock in mediocrity.

HEURISTIC + HONESTY: events.jsonl does not explicitly tag "this URL
produced the accepted units". We approximate winning_page_url as the
last data-bearing fetch (``extract.link_hop_fetched`` / ``fetch.completed``)
for the property. This is APPROXIMATE — a wrong url makes next run waste
one hop then fall back to the cascade (degraded, not broken). DRY-RUN by
default; ``--apply`` writes. Always eyeball the printed sample first.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def _real_uid(u: dict[str, Any]) -> bool:
    x = str(u.get("unit_id") or "")
    return (
        bool(x)
        and x not in ("y", "")
        and not x.startswith("unkeyable_")
        and not x.lower().startswith("inferred")
    )


def _captured_well(p: dict[str, Any]) -> bool:
    """The strict 'unit-level data captured well' bar (locked at 2,558)."""
    er = p.get("_extract_result") or {}
    t = str(er.get("tier_used") or "")
    m = p.get("_meta") or {}
    us = p.get("units") or []
    if not (t.startswith("TIER_") and us):
        return False
    if t.startswith("TIER_4"):  # LLM tier is not 'genuine'
        return False
    if m.get("partial_recovery"):
        return False
    if str(m.get("verdict", "")).upper() in (
        "SUCCESS_PARTIAL",
        "SUCCESS_PLAN_LEVEL",
    ):
        return False
    nu = len(us)
    good = sum(
        1
        for u in us
        if _real_uid(u)
        and str(u.get("rent_low") or "").strip() not in ("", "None", "0")
    )
    beds_ok = sum(1 for u in us if u.get("beds") is not None)
    return good / nu >= 0.8 and beds_ok / nu >= 0.8 and good >= 1


def _winning_url_from_events(events_path: Path) -> dict[str, str]:
    """property_id -> approx winning page url (last data-bearing fetch).

    Priority: last ``extract.link_hop_fetched`` (the data-bearing hop),
    else last ``fetch.completed`` url. Skips obvious non-listing paths.
    """
    # Dry-run on real shard_0 data showed the naive "last hop" lands on
    # non-listing pages (e.g. gscapts.com/schedule-a-tour/). Harden:
    #   - never accept a clearly-non-inventory page,
    #   - PREFER a hop whose path looks like inventory
    #     (floorplan/availability/units/models/apartments),
    #   - fall back to last inventory-ish fetch, then last hop, then fetch.
    # Dry-run at full scale exposed more non-inventory landings to skip:
    # /application/, /lease/, /check-availability (forms, not listings),
    # plus generic region-index pages.
    _SKIP = (
        "/rental-scam", "/privacy", "/accessibility", "/sitemap",
        "/schedule-a-tour", "/schedule-tour", "/contact", "/tour",
        "/gallery", "/amenities", "/neighborhood", "/resident", "/apply",
        "/blog", "/about", "/photos", "/specials",
        "/application", "/lease", "/check-availability", "/checkavailability",
        "/virtual-tour", "/faq", "/reviews",
    )
    _INV = (
        "floorplan", "floor-plan", "floor_plan", "availability", "available",
        "/units", "/models", "apartments", "rates", "pricing", "/rent",
    )
    inv_hop: dict[str, str] = {}
    any_hop: dict[str, str] = {}
    inv_fetch: dict[str, str] = {}
    if not events_path.exists():
        return {}
    with events_path.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except Exception:
                continue
            pid = str(e.get("property_id") or "")
            url = (e.get("url") or "")
            if not pid or not url:
                continue
            low = url.lower()
            if any(s in low for s in _SKIP):
                continue
            is_inv = any(tok in low for tok in _INV)
            k = e.get("kind", "")
            if k == "extract.link_hop_fetched":
                any_hop[pid] = url
                if is_inv:
                    inv_hop[pid] = url
            elif k == "fetch.completed" and is_inv:
                inv_fetch[pid] = url
    # priority: inventory-looking hop > inventory-looking fetch > any hop
    out = dict(any_hop)
    out.update(inv_fetch)
    out.update(inv_hop)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--run-dir",
        required=True,
        help="dir of shard_*.json (properties) OR a flat dir of *.json shards",
    )
    ap.add_argument(
        "--events-dir",
        required=True,
        help="dir containing the run's events.jsonl files "
        "(e.g. shard_*/events.jsonl pulled from GCS)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="actually write seed profiles (default: DRY-RUN)",
    )
    ap.add_argument(
        "--profiles-dir",
        default="ma_poc/config/profiles",
        help="ProfileStore base dir (synced to GCS by the runner)",
    )
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    shard_files = sorted(run_dir.glob("shard_*.json")) or sorted(
        run_dir.glob("shard_*/properties.json")
    )
    if not shard_files:
        print(f"no shard property files under {run_dir}", file=sys.stderr)
        return 2

    # 1. captured-well set
    well: dict[str, dict[str, Any]] = {}
    n_props = 0
    for f in shard_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for p in data:
            n_props += 1
            if _captured_well(p):
                well[str(p.get("apartment_id"))] = p
    print(f"properties scanned : {n_props:,}")
    print(f"captured-well (gate): {len(well):,}")

    # 2. reconstruct winning url from events
    ev_files = sorted(Path(args.events_dir).glob("shard_*/events.jsonl")) or sorted(
        Path(args.events_dir).glob("*.jsonl")
    )
    url_map: dict[str, str] = {}
    for ef in ev_files:
        url_map.update(_winning_url_from_events(ef))
    print(f"events files read  : {len(ev_files)}")

    seeded = {
        pid: url_map[pid] for pid in well if pid in url_map and url_map[pid]
    }
    miss = [pid for pid in well if pid not in seeded]
    print(f"captured-well WITH reconstructable winning_url: {len(seeded):,}")
    print(f"captured-well WITHOUT (no usable event url)    : {len(miss):,}")
    host = Counter(u.split("/")[2] for u in seeded.values() if "/" in u)
    print("top winning hosts:", host.most_common(6))
    print("\n--- VALIDATION SAMPLE (eyeball before --apply) ---")
    for pid in list(seeded)[:12]:
        print(f"  {pid:8} {well[pid].get('website'):42} -> {seeded[pid]}")

    if not args.apply:
        print(
            f"\nDRY-RUN. Would seed {len(seeded):,} profiles. "
            "Re-run with --apply after validating the sample."
        )
        return 0

    # 3. apply: reuse ProfileStore.bootstrap_from_meta then set winning url
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from services.profile_store import ProfileStore  # type: ignore

    store = ProfileStore(Path(args.profiles_dir))
    wrote = 0
    for pid, url in seeded.items():
        try:
            site = well[pid].get("website") or ""
            prof = store.bootstrap_from_meta(pid, {}, site)
            prof.navigation.winning_page_url = url
            prof.updated_by = "BACKFILL_2026-05-19_events"
            store.save(prof)
            wrote += 1
        except Exception as exc:  # never abort the batch on one bad row
            print(f"  WARN {pid}: {exc}", file=sys.stderr)
    print(f"\nAPPLIED: wrote {wrote:,} seed profiles to {args.profiles_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
