#!/usr/bin/env python3
"""F2 — TLS-fingerprint vs IP-reputation diagnostic.

Runs two fetches against each of 6 known-silent-403 .aspx URLs from the
2026-05-04 production run, from the same egress:

  A) httpx default TLS    (control)
  B) curl_cffi --impersonate chrome120

Result matrix:
  | A   | B   | per-URL verdict     |
  |-----|-----|---------------------|
  | 403 | 200 | TLS_FINGERPRINT     |
  | 403 | 403 | IP_REPUTATION       |
  | 200 | 200 | NOT_REPRODUCIBLE    |
  | 200 | 403 | UNEXPECTED          |

Aggregate: ≥4 of one kind → that verdict. Mixed (≥2 of each) → MIXED.
Else INCONCLUSIVE (rerun with --retries 3).

Outputs ``docs/ANTIBOT_TLS_VERDICT.md`` with structured ``verdict:`` header.

Usage:
    python -m ma_poc.scripts.diagnostics.tls_vs_ip_diagnostic [--retries N]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

try:
    from curl_cffi import requests as curl_cffi_requests  # type: ignore[import-not-found]
except ImportError:
    curl_cffi_requests = None  # type: ignore[assignment]

# parents[0]=diagnostics, [1]=scripts, [2]=ma_poc, [3]=PropAi
REPO_ROOT = Path(__file__).resolve().parents[3]
VERDICT_PATH = REPO_ROOT / "docs" / "ANTIBOT_TLS_VERDICT.md"

# Six known-silent-403 .aspx URLs from the 2026-05-04 production run analysis
# (shards 0/3/7/8). Do NOT change this list without updating the spec — the
# Drift checklist (§8.5) verifies the count is exactly 6.
DIAGNOSTIC_URLS: list[tuple[str, str]] = [
    ("shard_7", "http://www.rentcafe.com/onlineleasing/hampshire-village/floorplans.aspx"),
    ("shard_8", "http://www.rentcafe.com/onlineleasing/highview-terrace/floorplans.aspx"),
    ("shard_8", "https://villageatthegateway.securecafe.com/onlineleasing/village-at-gateways/floorplans.aspx"),
    ("shard_7", "https://theapartmentgallery.securecafe.com/onlineleasing/st-clair-terrace0/floorplans.aspx"),
    ("shard_0", "https://theapartmentgallery.securecafe.com/onlineleasing/cloister-gardens/floorplans.aspx"),
    ("shard_3", "https://livebh.com/residentservices/apartmentsforrent/userlogin.aspx"),
]


async def _fetch_httpx(url: str, timeout: float = 20.0) -> int:
    """Fetch *url* with default httpx TLS. Returns status code, or -1 on error."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as c:
            r = await c.get(url)
            return r.status_code
    except Exception:
        return -1


def _fetch_curl_cffi(url: str, timeout: float = 20.0) -> int:
    """Fetch *url* with chrome120 TLS impersonation. Returns -2 if curl_cffi unavailable."""
    if curl_cffi_requests is None:
        return -2
    try:
        r = curl_cffi_requests.get(
            url, impersonate="chrome120", timeout=timeout, allow_redirects=True
        )
        return int(r.status_code)
    except Exception:
        return -1


def _classify_pair(a: int, b: int) -> str:
    """Map (httpx_status, curl_cffi_status) to a per-URL verdict label."""
    if a < 200 or b < 200:
        return "ERROR"
    if a == 403 and b in (200, 301, 302):
        return "TLS_FINGERPRINT"
    if a == 403 and b == 403:
        return "IP_REPUTATION"
    if a in (200, 301, 302) and b in (200, 301, 302):
        return "NOT_REPRODUCIBLE"
    if a in (200, 301, 302) and b == 403:
        return "UNEXPECTED"
    return f"OTHER(a={a},b={b})"


def _aggregate_verdict(verdicts: list[str]) -> str:
    """Collapse per-URL verdicts to a single aggregate label."""
    counts = Counter(verdicts)
    if counts.get("TLS_FINGERPRINT", 0) >= 4:
        return "TLS_FINGERPRINT"
    if counts.get("IP_REPUTATION", 0) >= 4:
        return "IP_REPUTATION"
    if counts.get("TLS_FINGERPRINT", 0) >= 2 and counts.get("IP_REPUTATION", 0) >= 2:
        return "MIXED"
    if counts.get("NOT_REPRODUCIBLE", 0) >= 4:
        return "NOT_REPRODUCIBLE"
    return "INCONCLUSIVE"


def _write_verdict(per_url: list[dict[str, Any]], aggregate: str) -> None:
    """Write the structured verdict markdown to ``docs/ANTIBOT_TLS_VERDICT.md``."""
    VERDICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Anti-bot TLS vs IP diagnostic — verdict",
        "",
        f"verdict: {aggregate}",
        f"generated_at: {dt.datetime.utcnow().isoformat()}Z",
        f"sample_size: {len(per_url)}",
        "",
        "## Per-URL results",
        "",
        "| shard | url | httpx | curl_cffi | verdict |",
        "|-------|-----|------:|----------:|---------|",
    ]
    for r in per_url:
        lines.append(
            f"| {r['shard']} | `{r['url']}` | {r['a']} | {r['b']} | {r['verdict']} |"
        )
    lines += [
        "",
        "## Interpretation",
        "- TLS_FINGERPRINT — `curl_cffi --impersonate chrome120` succeeds where default `httpx` fails. "
        "DIY stealth tier (`curl_cffi`/`patchright`) is the cheap fix.",
        "- IP_REPUTATION — both fail identically. GCP egress on Cloudflare deny lists; "
        "vendor evaluation required.",
        "- MIXED — both fixes needed.",
        "- NOT_REPRODUCIBLE / INCONCLUSIVE — rerun with `--retries 3`.",
    ]
    VERDICT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main_async(retries: int = 1) -> int:
    """Run the diagnostic against all 6 URLs, write verdict, return exit code."""
    if curl_cffi_requests is None:
        print("FATAL: install curl_cffi (pip install curl_cffi)", file=sys.stderr)
        return 2

    per_url: list[dict[str, Any]] = []
    for shard, url in DIAGNOSTIC_URLS:
        a_results: list[int] = []
        b_results: list[int] = []
        for _ in range(max(1, retries)):
            a_results.append(await _fetch_httpx(url))
            b_results.append(_fetch_curl_cffi(url))
        a = Counter(a_results).most_common(1)[0][0]
        b = Counter(b_results).most_common(1)[0][0]
        v = _classify_pair(a, b)
        per_url.append({"shard": shard, "url": url, "a": a, "b": b, "verdict": v})
        print(f"  [{shard}] httpx={a} curl_cffi={b} -> {v}    {url}")

    aggregate = _aggregate_verdict([r["verdict"] for r in per_url])
    _write_verdict(per_url, aggregate)
    print(f"\nAggregate verdict: {aggregate}")
    print(f"Written to: {VERDICT_PATH}")
    return 0


def main() -> None:
    """CLI entry point."""
    p = argparse.ArgumentParser()
    p.add_argument("--retries", type=int, default=1)
    sys.exit(asyncio.run(main_async(retries=p.parse_args().retries)))


if __name__ == "__main__":
    main()
