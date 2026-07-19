#!/usr/bin/env python
"""Seed ScrapeProfiles with the CONFIRMED hot data-surface URL per property.

Context (2026-07-19)
--------------------
The pipeline already caches and prioritises a per-property hot URL:
``ScrapeProfile.navigation.winning_page_url`` = "the exact URL that produced
units last time", ranked FIRST in the next run's link-hop crawl (skip everything
else once it delivers >1 units). BUT it is written ONLY on a pipeline WIN — so
the timeout / generic-misroute cohort, which never won, has no cached hot URL
and re-pays the full detect→crawl→600s-timeout every run.

The 2026-07-18 roster-confirmation sweep (workflow wbt9bsyro) produced 625
probe-CONFIRMED hot URLs OUT-OF-BAND — surfaces the pipeline could never have
cached itself. This script seeds them into ScrapeProfiles so the next run skips
detection + crawl + timeout and goes straight to the confirmed surface:

  * PAGE surfaces  (on-site.com/online_app3, securecafe availableunits.aspx,
                    myresman Portal, marketing detail pages)
       → ``navigation.winning_page_url`` (+ ``availability_links``)
  * API  surfaces  (sightmap app/api, knock doorway-api, graphql, /api/v1)
       → ``api_hints.known_endpoints`` (+ ``api_hints.api_provider``)

Only rows that were CONFIRMED (roster unit|plan) AND whose target adapter
EXISTS are seeded — a hot URL saves the fetch, not the extraction, so a surface
with no adapter would just fail differently. Profiles are promoted COLD→WARM so
the "skip crawl once winning_page_url delivers >1 units" fast-path engages.

Merge-safe: with ``--merge-dir`` it loads any existing profile for a canonical_id
and fills the hot-URL slots WITHOUT clobbering organically-learned state (an
existing winning_page_url is kept — it is proven/fresher). Without it, fresh
profiles are created.

Usage
-----
  python ma_poc/scripts/seed_profiles_from_confirmation.py \
      --confirmed  investigations/2026-07-18-roster-confirmation/confirmed_rosters.jsonl \
      --worklist   investigations/2026-07-18-timeout-grind/worklist_415.jsonl \
      --worklist   investigations/2026-07-18-generic-family-grind/worklist_generic.jsonl \
      --out        investigations/2026-07-18-roster-confirmation/seeded_profiles \
      [--merge-dir /path/to/pulled/live/profiles]

Then upload ``--out`` to ``PROFILE_GCS_PREFIX`` for the next canary.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

# The model uses ``ma_poc.`` imports; profile_store uses bare ``models``/``services``.
# Put BOTH the worktree root and the ma_poc dir on the path so either resolves.
_HERE = Path(__file__).resolve()
_MA_POC = _HERE.parents[1]
_ROOT = _MA_POC.parent
for _p in (str(_ROOT), str(_MA_POC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models.scrape_profile import (  # noqa: E402
    ApiEndpoint,
    ProfileMaturity,
    ScrapeProfile,
)
from services.profile_store import ProfileStore  # noqa: E402

SEED_TAG = "SEED_ROSTER_CONFIRMATION_2026-07-19"

# Substrings that mark a data-surface URL as an API endpoint (→ known_endpoints)
# rather than a navigable page (→ winning_page_url).
_API_MARKERS = (
    "sightmap.com/app/api",
    "sightmap.com/api",
    "doorway-api.knockrentals",
    "/graphql",
    "c-leasestar-api",
    "api.ws.realpage",
    "inventory.g5marketingcloud",
    "rentdynamics.com",
    "/api/v1/",
    "api-v3.peek.us",
    "/rapi/",
    "admin-ajax.php",
)


def _norm_pms(adapter_target: str) -> str:
    """Normalise a free-text adapter_target to a bare pms slug."""
    t = (adapter_target or "").strip().lower()
    if not t:
        return ""
    # take the token before any space / paren / slash / comma
    for sep in (" (", "(", ",", "/", " "):
        if sep in t:
            t = t.split(sep, 1)[0]
    return t.strip("_ ").strip()


def _is_api_surface(url: str) -> bool:
    u = (url or "").lower()
    return any(m in u for m in _API_MARKERS)


def _http(url: str) -> bool:
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def _load_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _url_key(url: str) -> str:
    """Loose join key: host+path, scheme/www/trailing-slash-insensitive."""
    try:
        p = urllib.parse.urlparse((url or "").strip())
        host = (p.netloc or "").lower().removeprefix("www.")
        path = (p.path or "").rstrip("/").lower()
        return f"{host}{path}"
    except ValueError:
        return (url or "").strip().lower()


def build_url_to_cid(worklists: list[Path]) -> dict[str, str]:
    m: dict[str, str] = {}
    for wl in worklists:
        for rec in _load_jsonl(wl):
            cid = str(rec.get("cid") or rec.get("canonical_id") or "").strip()
            url = rec.get("url") or ""
            if cid and url:
                m.setdefault(_url_key(url), cid)
    return m


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirmed", required=True, type=Path)
    ap.add_argument("--worklist", action="append", required=True, type=Path, dest="worklists")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--merge-dir", type=Path, default=None,
                    help="dir of existing profiles to merge into (won't clobber organic winners)")
    args = ap.parse_args()

    url2cid = build_url_to_cid(args.worklists)
    confirmed = _load_jsonl(args.confirmed)
    out_store = ProfileStore(args.out)
    merge_store = ProfileStore(args.merge_dir) if args.merge_dir else None
    now = datetime.utcnow()

    stats = {
        "confirmed_total": len(confirmed),
        "seeded": 0,
        "page_surface": 0,
        "api_surface": 0,
        "skip_not_confirmed": 0,
        "skip_no_adapter": 0,
        "skip_no_surface": 0,
        "skip_no_cid": 0,
        "by_pms": {},
    }

    for rec in confirmed:
        roster = rec.get("roster")
        if roster not in ("unit", "plan"):
            stats["skip_not_confirmed"] += 1
            continue
        if rec.get("adapter_exists") is not True:
            stats["skip_no_adapter"] += 1
            continue
        surface = rec.get("data_surface_url") or ""
        if not _http(surface):
            stats["skip_no_surface"] += 1
            continue
        cid = url2cid.get(_url_key(rec.get("url") or ""))
        if not cid:
            stats["skip_no_cid"] += 1
            continue

        # Accumulate: a prior seed this run (property with >1 confirmed surface)
        # → an existing live profile (--merge-dir) → else a fresh profile.
        profile = (
            out_store.load(cid)
            or (merge_store.load(cid) if merge_store else None)
            or ScrapeProfile(canonical_id=cid)
        )
        pms = _norm_pms(rec.get("adapter_target") or "")

        if not profile.navigation.entry_url:
            profile.navigation.entry_url = rec.get("url") or None

        if _is_api_surface(surface):
            stats["api_surface"] += 1
            if surface not in [e.url_pattern for e in profile.api_hints.known_endpoints]:
                profile.api_hints.known_endpoints.append(
                    ApiEndpoint(url_pattern=surface, provider=pms or None)
                )
            if not profile.api_hints.api_provider or profile.api_hints.api_provider == "unknown":
                profile.api_hints.api_provider = pms or "unknown"
        else:
            stats["page_surface"] += 1
            # Keep an existing organic winner (proven/fresher); else seed ours.
            if not profile.navigation.winning_page_url:
                profile.navigation.winning_page_url = surface
            if surface not in profile.navigation.availability_links:
                profile.navigation.availability_links.append(surface)

        if pms and not profile.dom_hints.platform_detected:
            profile.dom_hints.platform_detected = pms
        # Promote COLD→WARM so the "skip crawl once winning_page_url delivers
        # >1 units" fast-path engages; never demote an already-WARM/HOT profile.
        if profile.confidence.maturity == ProfileMaturity.COLD:
            profile.confidence.maturity = ProfileMaturity.WARM
        if roster == "unit" and profile.confidence.preferred_tier is None:
            profile.confidence.preferred_tier = 1

        profile.updated_by = SEED_TAG
        profile.updated_at = now
        out_store.save(profile)

        stats["seeded"] += 1
        stats["by_pms"][pms or "?"] = stats["by_pms"].get(pms or "?", 0) + 1

    summary_path = args.out / "_seed_summary.json"
    summary_path.write_text(json.dumps(stats, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(stats, indent=2, sort_keys=True))
    print(f"\nSeeded {stats['seeded']} profiles → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
