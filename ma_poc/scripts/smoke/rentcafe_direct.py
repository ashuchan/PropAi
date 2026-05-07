#!/usr/bin/env python3
"""F7 — production smoke for the RentCafe direct path.

Runs :func:`fetch_direct` (with a resolve-then-cache fallback) against
50 properties from the 2026-05-04 RentCafe-blocked set, writes one
result entry per property to ``data/smoke/rentcafe_direct_smoke.json``.

The H9 invariant — ≥30/50 must produce ≥1 unit — is enforced by the
test in ``tests/integration/test_rentcafe_direct_smoke.py``.

This script is run **manually**, not as part of the gate, because:

  1. It calls real network endpoints — the aggregator + Cloudflare.
  2. It must run from production-equivalent egress (Cloud Run) for the
     IP_REPUTATION-vs-TLS_FINGERPRINT distinction to be meaningful (see
     §8.8 — laptop runs can pass while production fails).

Inputs:

  * ``data/runs/<latest>/bot_blocked_properties_latest.json``
    (or any path passed via ``--blocked-input``).
  * Production property metadata (``--csv``) for name / city / zip /
    website lookup.

Output:

  ``data/smoke/rentcafe_direct_smoke.json``: list of 50 entries shaped:

  ``{canonical_id, resolution, direct_tier, units_extracted, elapsed_ms}``

  where ``resolution`` is one of
  ``EXACT | ZIP_AND_NAME_PREFIX | ZIP_ONLY | NONE | CACHED``.

Usage:
    python -m ma_poc.scripts.smoke_rentcafe_direct \\
        --csv config/properties.csv \\
        --blocked-input data/runs/2026-05-04/bot_blocked_properties_latest.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# parents[0]=scripts, [1]=ma_poc, [2]=PropAi
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MA_POC_ROOT = Path(__file__).resolve().parents[1]
for _p in (_REPO_ROOT, _MA_POC_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ma_poc.pms.adapters.rentcafe import (  # noqa: E402
    _is_rentcafe_response,
    _unwrap_rentcafe_list,
    parse_rentcafe_floorplans,
)
from ma_poc.pms.rentcafe_direct import (  # noqa: E402
    fetch_direct,
    resolve_property_id,
)

log = logging.getLogger("smoke_rentcafe_direct")

DEFAULT_OUTPUT = _MA_POC_ROOT / "data" / "smoke" / "rentcafe_direct_smoke.json"


def _normalize_zip(z: str | None) -> str:
    if not z:
        return ""
    return str(z).strip().split("-")[0][:5]


def _load_blocked(path: Path) -> list[dict[str, Any]]:
    """Load the bot-blocked-properties artifact, return list of dicts."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        # Some artifacts wrap entries under "properties"
        for k in ("properties", "blocked", "items"):
            v = raw.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def _load_csv(path: Path) -> dict[str, dict[str, Any]]:
    """Load CSV indexed by canonical id (best-effort across column names)."""
    import csv as _csv

    by_id: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = _csv.DictReader(f)
        for row in reader:
            cid = (
                row.get("apartmentid")
                or row.get("Unique ID")
                or row.get("Property ID")
                or row.get("property_id")
            )
            if cid:
                by_id[str(cid).strip()] = row
    return by_id


async def _smoke_one(
    canonical_id: str,
    csv_row: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run resolve-then-fetch for one property; return one smoke-output entry."""
    row = csv_row or {}
    property_name = (
        row.get("name") or row.get("Name") or row.get("Property Name") or ""
    ).strip()
    city = (row.get("city") or row.get("City") or "").strip()
    zip_code = _normalize_zip(row.get("zip") or row.get("Zip") or row.get("ZIP Code"))
    website = (row.get("website") or row.get("Website") or "").strip()

    vanity_domain: str | None = None
    if website:
        try:
            from urllib.parse import urlparse

            vanity_domain = urlparse(website).hostname
        except Exception:
            vanity_domain = None

    # No live cache lookup — smoke always resolves fresh.
    resolution_label = "NONE"
    pid: str | None = None
    if property_name and zip_code:
        try:
            r = await resolve_property_id(
                property_name=property_name,
                city=city,
                zip_code=zip_code,
                vanity_domain=vanity_domain,
            )
            pid = r.property_id
            resolution_label = r.confidence
        except Exception as exc:
            log.warning("resolve raised for %s: %s", canonical_id, exc)

    if pid is None:
        return {
            "canonical_id": canonical_id,
            "resolution": "NONE",
            "direct_tier": "TIER_1_API_RENTCAFE_DIRECT_PROPERTY_ID_RESOLUTION_FAILED",
            "units_extracted": 0,
            "elapsed_ms": 0,
        }

    direct = await fetch_direct(pid)
    units_extracted = 0
    if direct.api_responses:
        body = direct.api_responses[0].get("body")
        if _is_rentcafe_response(body):
            items = _unwrap_rentcafe_list(body) or []
            if items:
                units = parse_rentcafe_floorplans(items, direct.api_responses[0].get("url", ""))
                units_extracted = len(units)
    return {
        "canonical_id": canonical_id,
        "resolution": resolution_label,
        "direct_tier": direct.tier_code,
        "units_extracted": units_extracted,
        "elapsed_ms": direct.elapsed_ms,
    }


async def main_async(
    blocked_input: Path,
    csv_path: Path,
    output_path: Path,
    sample_size: int = 50,
) -> int:
    blocked = _load_blocked(blocked_input)
    if not blocked:
        print(f"FATAL: no blocked-properties found in {blocked_input}", file=sys.stderr)
        return 2

    # Filter to RentCafe-family entries — match on URL host. The
    # analysis methodology used the same heuristic.
    rc_blocked: list[dict[str, Any]] = []
    for r in blocked:
        url = (r.get("url") or r.get("website") or r.get("Website") or "").lower()
        if "rentcafe.com" in url or "securecafe.com" in url:
            rc_blocked.append(r)

    if len(rc_blocked) < sample_size:
        print(
            f"WARN: only {len(rc_blocked)} RentCafe-family blocked properties "
            f"available; sample_size capped at that.",
            file=sys.stderr,
        )
        sample_size = len(rc_blocked)

    sample = rc_blocked[:sample_size]
    csv_index = _load_csv(csv_path)

    results: list[dict[str, Any]] = []
    for entry in sample:
        cid = (
            entry.get("canonical_id")
            or entry.get("property_id")
            or entry.get("apartmentid")
            or ""
        )
        cid = str(cid).strip()
        if not cid:
            continue
        csv_row = csv_index.get(cid)
        out = await _smoke_one(cid, csv_row)
        results.append(out)
        log.info(
            "  %s -> %s / %s / %d units / %d ms",
            cid,
            out["resolution"],
            out["direct_tier"],
            out["units_extracted"],
            out["elapsed_ms"],
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    successes = sum(1 for r in results if r["units_extracted"] > 0)
    failure_modes = Counter(
        r["direct_tier"] for r in results if r["units_extracted"] == 0
    )
    print(f"\nSmoke result: {successes}/{len(results)} succeeded.")
    if failure_modes:
        print(f"Failure modes: {dict(failure_modes)}")
    print(f"Output: {output_path}")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--csv", type=Path, default=_MA_POC_ROOT / "config" / "properties.csv")
    p.add_argument("--blocked-input", type=Path, required=True)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--sample-size", type=int, default=50)
    args = p.parse_args()
    sys.exit(
        asyncio.run(
            main_async(
                blocked_input=args.blocked_input,
                csv_path=args.csv,
                output_path=args.output,
                sample_size=args.sample_size,
            )
        )
    )


if __name__ == "__main__":
    main()
