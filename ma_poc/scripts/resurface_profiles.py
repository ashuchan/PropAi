#!/usr/bin/env python
"""Durable hot-URL maintenance — the recurring half of the sweep-and-seed loop.

Context (2026-07-19)
--------------------
The pipeline caches a per-property data-surface URL (``winning_page_url`` +
``api_hints.known_endpoints``) and goes straight to it on the next run, skipping
detect→crawl→timeout. That cache is durable while the surface is stable — but PMS
vendors MIGRATE surfaces (SecureCafe ``availableunits.aspx`` → Applicant-portal
SPA; FortressTech ``embed.`` → ``availability.``), and when they do, the cached
URL silently 404s / returns an empty shell and the property fails forever with no
signal.

This job is the migration detector that keeps the cache honest. For every profile
with a cached surface it re-probes the URL (free ``probe_get``, no paid tier) and:

  * ALIVE  (200 + unit signal) → keep.
  * DEAD   (404/410, or 200 with no unit signal → migrated/empty) → INVALIDATE the
    stale surface (clear ``winning_page_url`` / drop the dead endpoint) so the live
    pipeline RE-DISCOVERS it next run, and append it to a ``stale_surfaces.jsonl``
    triage queue (a migrated surface may need a new parser — that becomes a
    reviewed task, not a silent failure).

It does NOT do new-surface discovery — that rides the live pipeline's own
success-learning now that the adapters + render-on-empty are live. This closes the
other half of the loop: success → cache; migration → invalidate + flag.

Run it after each daily run (see CronCreate wiring) over the live profile set
pulled from ``PROFILE_GCS_PREFIX``.

Usage
-----
  python ma_poc/scripts/resurface_profiles.py \
      --profiles-dir /path/to/profiles \
      --stale-log    /path/to/stale_surfaces.jsonl \
      [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
_MA_POC = _HERE.parents[1]
_ROOT = _MA_POC.parent
# When run as __main__, Python puts this script's own dir (ma_poc/scripts/) on
# sys.path[0], where its bare-name modules (identity.py, validation.py, …) shadow
# imports pulled in transitively by probe_get — the probe then raises and every
# surface is falsely marked dead. Drop the script dir before adding the roots.
_SCRIPTDIR = str(_HERE.parent)
sys.path[:] = [p for p in sys.path if p not in ("", _SCRIPTDIR)]
for _p in (str(_ROOT), str(_MA_POC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.llm_api_rescue import response_looks_like_units  # noqa: E402
from services.profile_store import ProfileStore  # noqa: E402

# Only a DEFINITIVELY-gone status invalidates a seed. 403 (walled), 5xx
# (transient), 3xx, and probe errors are NOT migrations — invalidating on them
# would wipe good seeds on a transient blip, so those keep the seed.
_GONE_STATUSES = frozenset({404, 410, 451})


def _probe(url: str) -> tuple[int, str]:
    """Free probe (curl_cffi, no paid unlocker). Returns (status, body). Never raises.

    status 0 means the probe itself errored (network/transient) — the caller
    treats that as UNKNOWN and keeps the seed, never as dead.
    """
    try:
        from ma_poc.pms.adapters._probe import probe_get

        r = probe_get(url, timeout=20, unlocker=False)
        return int(getattr(r, "status_code", 0) or 0), (getattr(r, "text", "") or "")
    except Exception:  # noqa: BLE001
        return 0, ""


def surface_is_alive(url: str) -> bool:
    """Whether the cached surface should be KEPT (True) or invalidated (False).

    Conservative by design — only a definitive migration invalidates a seed:
      * status 0 (probe error / transient) → KEEP (never destroy on uncertainty).
      * 404/410/451 (gone)                 → INVALIDATE.
      * 200 with a unit signal             → KEEP (roster still there).
      * 200 with NO unit signal            → INVALIDATE (migrated to empty shell).
      * anything else (403 walled, 5xx, 3xx) → KEEP (not a migration).
    """
    status, body = _probe(url)
    if status == 0:
        return True
    if status in _GONE_STATUSES:
        return False
    if status == 200:
        return response_looks_like_units(body)
    return True


def resurface(
    profiles_dir: Path,
    stale_log: Path,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    store = ProfileStore(profiles_dir)
    stats = {
        "checked": 0,
        "no_surface": 0,
        "alive": 0,
        "invalidated_winning_url": 0,
        "invalidated_endpoints": 0,
        "profiles_modified": 0,
    }
    stale: list[dict[str, Any]] = []
    files = sorted(p for p in profiles_dir.glob("*.json") if p.name != "_seed_summary.json")
    if limit is not None:
        files = files[:limit]

    for path in files:
        cid = path.stem
        try:
            profile = store.load(cid)
        except Exception:  # noqa: BLE001
            continue
        if profile is None:
            continue

        wpu = profile.navigation.winning_page_url or ""
        endpoints = list(profile.api_hints.known_endpoints)
        if not wpu and not endpoints:
            stats["no_surface"] += 1
            continue

        stats["checked"] += 1
        modified = False

        if wpu:
            if surface_is_alive(wpu):
                stats["alive"] += 1
            else:
                stale.append({"canonical_id": cid, "kind": "winning_page_url", "url": wpu})
                stats["invalidated_winning_url"] += 1
                if not dry_run:
                    profile.navigation.winning_page_url = None
                    # also drop it from availability_links so it isn't re-promoted
                    profile.navigation.availability_links = [
                        u for u in profile.navigation.availability_links if u != wpu
                    ]
                modified = True

        surviving_endpoints = []
        for ep in endpoints:
            if surface_is_alive(ep.url_pattern):
                stats["alive"] += 1
                surviving_endpoints.append(ep)
            else:
                stale.append({"canonical_id": cid, "kind": "known_endpoint", "url": ep.url_pattern})
                stats["invalidated_endpoints"] += 1
                modified = True
        if modified and not dry_run and len(surviving_endpoints) != len(endpoints):
            profile.api_hints.known_endpoints = surviving_endpoints

        if modified:
            stats["profiles_modified"] += 1
            if not dry_run:
                profile.updated_by = "RESURFACE_MAINT_2026-07-19"
                store.save(profile)

    if stale and not dry_run:
        stale_log.parent.mkdir(parents=True, exist_ok=True)
        with stale_log.open("a", encoding="utf-8") as fh:
            for rec in stale:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")

    stats["stale_flagged"] = len(stale)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profiles-dir", required=True, type=Path)
    ap.add_argument("--stale-log", required=True, type=Path)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    stats = resurface(args.profiles_dir, args.stale_log, args.limit, args.dry_run)
    print(json.dumps(stats, indent=2, sort_keys=True))
    print(
        f"\nResurfaced {stats['checked']} cached profiles: "
        f"{stats['alive']} alive, {stats['stale_flagged']} migrated/stale "
        f"({'DRY-RUN, no writes' if args.dry_run else 'invalidated + flagged'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
