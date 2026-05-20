"""Local concession-capture canary.

Fetches a small sample of property home pages, runs the full
concession-extraction pipeline (capture-from-html + /specials probe +
clean + classify + normalize), and prints a per-property table.

Usage::

    python ma_poc/scripts/diagnostics/concession_canary.py
    python ma_poc/scripts/diagnostics/concession_canary.py --limit 20 --csv config/properties.csv

No DB writes, no Playwright. Pure httpx GET + the regex pipeline so
the result is fast and reproducible.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from pathlib import Path

# Force UTF-8 for the console so non-ASCII concession copy
# ("don't miss out", em-dashes, etc.) doesn't crash the print loop on
# Windows where the default code page is cp1252.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import httpx
import pandas as pd

# Ensure repo root is on sys.path when running this file directly.
_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ma_poc.core.concession_clean import (  # noqa: E402
    classify_concession_quality,
    clean_concession_text,
)
from ma_poc.core.concession_normalize import normalize_concession  # noqa: E402
from ma_poc.pms.scraper import _capture_concession_from_html  # noqa: E402

_DEFAULT_PROPERTIES: list[tuple[str, str]] = [
    # (label, url)
    ("Hawthorne at Traditions", "https://www.hawthorneattraditions.com/"),
    ("District Square", "https://livedistrictcollection.com/p/district-square/"),
    ("Venice Wave by Wiseman", "https://venicewave.wisemanresidential.com/"),
    ("Overland & Ayres", "https://overland-ayres.com/"),
    ("Elio at Lake Lena", "https://www.willowbridgepc.com/properties/elio-at-lake-lena-auburndale-fl/"),
    ("V by Alta", "https://vbyalta.com/"),
    ("Woodview Commons I", "https://www.woodviewapartments.com/"),
    ("Regency Townes", "https://www.regencytownesnc.com/"),
    ("San Artes", "https://sanartesapartmentsscottsdale.com/"),
    ("Intro Cleveland", "https://introcleveland.com/"),
    ("Vive", "https://livethevive.com/"),
    ("Hackney House", "https://www.hackneyhouseapartments.com/"),
]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)


async def _fetch_html(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        resp = await client.get(url)
        if resp.status_code == 200 and resp.text:
            return resp.text
    except Exception as exc:
        print(f"  [fetch_error] {type(exc).__name__}: {exc}")
    return None


async def _run_one(client: httpx.AsyncClient, label: str, url: str) -> dict:
    print(f"\n--{label} --")
    print(f"  url     : {url}")

    # Step 1: fetch home page
    html = await _fetch_html(client, url)
    if html is None:
        print("  status  : FETCH_FAILED")
        return {"label": label, "url": url, "status": "FETCH_FAILED"}

    # Step 2: try home-page capture
    raw = _capture_concession_from_html(html)
    source = "homepage"
    probe_url: str | None = None

    if not raw:
        print("  status  : NO_CONCESSION_FOUND")
        return {"label": label, "url": url, "status": "NO_CONCESSION_FOUND"}

    # Step 4: clean + classify + normalize
    cleaned = clean_concession_text(raw)
    quality = classify_concession_quality(raw)
    structured = normalize_concession(cleaned or raw)

    print(f"  source  : {source}{f' ({probe_url})' if probe_url else ''}")
    print(f"  raw     : {raw[:200]}{'...' if len(raw) > 200 else ''}")
    print(f"  cleaned : {cleaned[:200]}{'...' if len(cleaned) > 200 else ''}")
    print(f"  quality : {quality}")
    if structured:
        print(f"  struct  : {json.dumps({k: v for k, v in structured.items() if k != 'text'}, default=str)}")
    else:
        print("  struct  : None (raw preserved; structured fallback)")

    return {
        "label": label,
        "url": url,
        "status": "CONCESSION_FOUND",
        "source": source,
        "probe_url": probe_url,
        "raw": raw,
        "cleaned": cleaned,
        "quality": quality,
        "structured": structured,
    }


async def _main(targets: list[tuple[str, str]]) -> None:
    headers = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"}
    timeout = httpx.Timeout(10.0)
    results: list[dict] = []
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        for label, url in targets:
            try:
                r = await _run_one(client, label, url)
                results.append(r)
            except Exception as exc:
                print(f"  ERROR: {type(exc).__name__}: {exc}")
                results.append({"label": label, "url": url, "status": "EXCEPTION", "error": str(exc)})

    # ─--Summary --────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("CANARY SUMMARY")
    print("=" * 72)
    found = [r for r in results if r.get("status") == "CONCESSION_FOUND"]
    fetch_failed = [r for r in results if r.get("status") == "FETCH_FAILED"]
    no_concession = [r for r in results if r.get("status") == "NO_CONCESSION_FOUND"]

    print(f"  total                : {len(results)}")
    print(f"  concession found     : {len(found)}")
    print(f"     - via homepage    : {sum(1 for r in found if r.get('source') == 'homepage')}")
    print(f"  no concession found  : {len(no_concession)}")
    print(f"  fetch failed         : {len(fetch_failed)}")
    if found:
        structured_n = sum(1 for r in found if r.get("structured"))
        print(f"  structured parsed    : {structured_n} / {len(found)}")
        print(f"  raw-fallback (None struct): {len(found) - structured_n}")
        print("\n  Quality distribution:")
        from collections import Counter
        qc = Counter(r["quality"] for r in found)
        for q, n in sorted(qc.items(), key=lambda kv: -kv[1]):
            print(f"    {q:30s} {n}")

    # JSON dump for further inspection
    out = Path("data/canary_concession_results.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\n  full results written to {out}")


def _load_targets_from_csv(csv_path: str, limit: int) -> list[tuple[str, str]]:
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df = df[df["Website"].notna() & (df["Website"] != "")]
    # Lean toward Lease-Up since they're more likely to advertise concessions.
    lu = df[df["Property Status"] == "Lease-Up"].head(limit * 2 // 3)
    st = df[df["Property Status"] == "Stabilized"].head(limit - len(lu))
    rows = pd.concat([lu, st])
    return [(str(r["Property Name"]), str(r["Website"])) for _, r in rows.iterrows()]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default=None, help="Path to properties.csv")
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()

    if args.csv:
        targets = _load_targets_from_csv(args.csv, args.limit)
    else:
        targets = _DEFAULT_PROPERTIES[: args.limit]

    asyncio.run(_main(targets))
