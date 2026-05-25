"""Probe runner specialised for the sqft=-1 cohort.

For each prop: fetch landing + key subpages, then search the responses
for ANY sqft marker — numeric values followed by sq/sqft/ft²/ft2/
'square feet'/'square ft'. If markers exist, sqft IS published —
adapter missed it. If none, true operator-data-gap.

Output verdicts:
  SQFT_FOUND_AT_{path}   — adapter miss, fixable
  SQFT_TRULY_ABSENT      — true operator-data-gap (flag, don't ship)
  BLOCKED_*              — fetch failure, defer
  FETCH_ERROR            — DNS / hard fail
"""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from curl_cffi import requests as r

PROBE_DIR = Path(__file__).parent / "artifacts" / "probe"
TIMEOUT = 12.0

# Sqft signature — number (2-5 digits) followed by sqft variants
SQFT_MARKERS = [
    re.compile(r"\b(\d{2,5})\s*(?:sq\s*\.?\s*ft|sqft|sq\.?\s*feet|square\s*feet|square\s*ft)\b", re.IGNORECASE),
    re.compile(r"\b(\d{2,5})\s*ft[²2]", re.IGNORECASE),
    # Schema/JSON markers
    re.compile(r'"(?:sqft|square_?feet|area_?sqft|floor_?size|size)"\s*:\s*"?(\d{2,5})"?', re.IGNORECASE),
    re.compile(r'data-(?:sqft|square[-_]?feet|area)="(\d{2,5})"', re.IGNORECASE),
]


def _fetch(url: str, *, timeout: float = TIMEOUT) -> tuple[int, str]:
    try:
        rr = r.get(url, impersonate="chrome120", timeout=timeout, allow_redirects=True)
        return rr.status_code, rr.text or ""
    except Exception as exc:
        return 0, f"__ERR__: {exc!s}"


def _sqft_hits(text: str) -> dict:
    """Return summary of sqft markers in text."""
    if not text:
        return {"count": 0, "samples": [], "min": None, "max": None}
    vals = []
    for pat in SQFT_MARKERS:
        for m in pat.finditer(text):
            try:
                v = int(m.group(1))
                if 100 <= v <= 9999:  # plausibility filter
                    vals.append(v)
            except ValueError:
                pass
    if not vals:
        return {"count": 0, "samples": [], "min": None, "max": None}
    return {
        "count": len(vals),
        "samples": sorted(set(vals))[:8],
        "min": min(vals),
        "max": max(vals),
    }


def probe(prop: dict) -> dict:
    pid = prop["apartment_id"]
    base = prop.get("website") or ""
    if not base:
        return {"pid": pid, "verdict": "NO_URL", "ts": datetime.now(timezone.utc).isoformat()}
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    out = {
        "pid": pid,
        "name": prop.get("name"),
        "url": base,
        "tier_observed": prop.get("tier_observed"),
        "cohort": prop.get("_probe_cohort"),
        "tier_cohort": prop.get("_probe_tier"),
        "pool_size": prop.get("_pool_size"),
        "neg1_unit_count": prop.get("_neg1_unit_count"),
        "ts": datetime.now(timezone.utc).isoformat(),
        "paths": {},
    }

    paths_to_try = [
        ("landing", base),
        ("/floorplans", urljoin(origin + "/", "floorplans")),
        ("/floor-plans", urljoin(origin + "/", "floor-plans")),
        ("/availability", urljoin(origin + "/", "availability")),
        ("/listings", urljoin(origin + "/", "listings")),
    ]

    any_hit = False
    for label, url in paths_to_try:
        st, body = _fetch(url)
        hits = _sqft_hits(body) if st == 200 else {"count": 0, "samples": []}
        out["paths"][label] = {
            "url": url,
            "status": st,
            "size": len(body) if body else 0,
            "sqft_hits": hits,
        }
        if hits["count"] >= 3:  # at least 3 sqft mentions => real signal
            any_hit = True

    # Verdict
    if out["paths"]["landing"]["status"] in (403, 401, 429, 503):
        out["verdict"] = f"BLOCKED_HTTP_{out['paths']['landing']['status']}"
    elif out["paths"]["landing"]["status"] == 0:
        out["verdict"] = "FETCH_ERROR"
    elif any_hit:
        # Find which path had hits
        hit_paths = [
            label for label, info in out["paths"].items()
            if info["sqft_hits"]["count"] >= 3
        ]
        out["verdict"] = f"SQFT_FOUND_AT_{','.join(hit_paths)}"
    else:
        out["verdict"] = "SQFT_TRULY_ABSENT"
    return out


def main() -> None:
    cohort = sys.argv[1] if len(sys.argv) > 1 else "sqft_neg1"
    worklist_p = PROBE_DIR / f"{cohort}_worklist.jsonl"
    results_p = PROBE_DIR / f"{cohort}_results.jsonl"

    worklist = []
    with worklist_p.open() as f:
        for line in f:
            if line.strip():
                worklist.append(json.loads(line))

    # Resume
    done = set()
    if results_p.exists():
        with results_p.open() as f:
            for line in f:
                try:
                    done.add(json.loads(line).get("pid"))
                except Exception:
                    pass
    pending = [p for p in worklist if p.get("apartment_id") not in done]
    print(f"[{cohort}] worklist={len(worklist)}  done={len(done)}  pending={len(pending)}")
    if not pending:
        return

    t0 = time.time()
    with results_p.open("a") as fout, ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(probe, p): p for p in pending}
        for i, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            fout.write(json.dumps(res, default=str) + "\n")
            fout.flush()
            print(f"  [{i:>2}/{len(pending)}] pid={res.get('pid')} verdict={res.get('verdict')}")
    print(f"[{cohort}] done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
