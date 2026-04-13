"""
scripts/validate_outputs.py — required Phase A metrics.

Reads data/scrape_events.jsonl. Computes the 10 metrics from CLAUDE.md and
prints a structured summary. Exit non-zero if any hard target is missed.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
EVENTS = DATA / "scrape_events.jsonl"

# Targets
TARGET_TOTAL = 500
TARGET_SUCCESS_RATE = 0.95
TARGET_TIER12_SHARE = 0.40
TARGET_SKIP_RATE_STABILISED = 0.50
TARGET_VISION_FALLBACK_RATE = 0.05
TARGET_BANNER_ATTEMPTED = 1.0
TARGET_P95_PAGE_LOAD_MS = 30_000
TARGET_PER_DOMAIN_FAIL = 0.05
TARGET_VISION_SAMPLE_RATE = (0.05, 0.10)
TARGET_VISION_AGREEMENT = 0.90


def load_events() -> list[dict[str, Any]]:
    if not EVENTS.exists():
        return []
    out: list[dict[str, Any]] = []
    with EVENTS.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = int(round((p / 100) * (len(s) - 1)))
    return s[k]


def _domain(url_or_path: str | None) -> str:
    if not url_or_path:
        return "(none)"
    return (urlparse(url_or_path).hostname or url_or_path).lower()


def main() -> int:
    events = load_events()
    if not events:
        print("NO EVENTS — run scripts/run_phase_a.py first")
        return 1

    # Last 24h
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    recent = [
        e for e in events
        if datetime.fromisoformat(e["scrape_timestamp"].replace("Z", "+00:00")) >= cutoff
    ]
    properties_24h = {e["property_id"] for e in recent}

    successes = [e for e in recent if e["scrape_outcome"] == "SUCCESS"]
    skipped = [e for e in recent if e["scrape_outcome"] == "SKIPPED"]
    success_rate = (len(successes) / len(recent)) if recent else 0.0

    tiers: dict[int, int] = defaultdict(int)
    for e in successes:
        if e.get("extraction_tier") is not None:
            tiers[int(e["extraction_tier"])] += 1
    tier12 = tiers[1] + tiers[2]
    tier12_share = (tier12 / len(successes)) if successes else 0.0

    # Skip rate STABILISED — proxy via scrape_outcome=SKIPPED for properties whose
    # property_id we cannot classify here. validate_outputs is a metrics script;
    # detailed type-aware skip rate requires the property type which is not on
    # ScrapeEvent. We compute the global skip rate as a coarse signal.
    skip_rate = (len(skipped) / len(recent)) if recent else 0.0

    vision_used = sum(1 for e in recent if e.get("vision_fallback_used"))
    vision_rate = (vision_used / len(recent)) if recent else 0.0

    non_skipped = [e for e in recent if e["scrape_outcome"] != "SKIPPED"]
    banner_rate = (
        sum(1 for e in non_skipped if e.get("banner_capture_attempted")) / len(non_skipped)
        if non_skipped else 0.0
    )

    page_loads = [float(e["page_load_ms"]) for e in recent if e.get("page_load_ms")]
    p95 = percentile(page_loads, 95)

    # Per-domain rolling 7d failure rate
    week_cutoff = datetime.now(UTC) - timedelta(days=7)
    per_dom: dict[str, list[bool]] = defaultdict(list)
    for e in events:
        if datetime.fromisoformat(e["scrape_timestamp"].replace("Z", "+00:00")) < week_cutoff:
            continue
        dom = _domain(e.get("raw_html_path") or "")
        if dom == "(none)":
            continue
        per_dom[dom].append(e["scrape_outcome"] != "FAILED")
    bad_domains = [
        d for d, oks in per_dom.items()
        if oks and ((sum(1 for ok in oks if not ok) / len(oks)) > TARGET_PER_DOMAIN_FAIL)
    ]

    sample_count = sum(1 for e in successes if e.get("accuracy_sample_selected"))
    sample_rate = (sample_count / len(successes)) if successes else 0.0

    # Vision agreement: optional — read all *_vision_comparison.json files
    agreement_rates: list[float] = []
    out_dir = DATA / "extraction_output"
    if out_dir.exists():
        for cmp_file in out_dir.rglob("*_vision_comparison.json"):
            try:
                doc = json.loads(cmp_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if doc.get("agreement_rate") is not None:
                agreement_rates.append(float(doc["agreement_rate"]))
    avg_agreement = sum(agreement_rates) / len(agreement_rates) if agreement_rates else None

    # Print + status
    rows = [
        ("Total properties scraped (24h)", len(properties_24h), f">= {TARGET_TOTAL}",
         len(properties_24h) >= TARGET_TOTAL),
        ("Overall scrape success rate", f"{success_rate:.1%}", f">= {TARGET_SUCCESS_RATE:.0%}",
         success_rate >= TARGET_SUCCESS_RATE),
        ("Tier1+2 share of successes", f"{tier12_share:.1%}", f">= {TARGET_TIER12_SHARE:.0%}",
         tier12_share >= TARGET_TIER12_SHARE),
        ("Change-detection skip rate", f"{skip_rate:.1%}", f">= {TARGET_SKIP_RATE_STABILISED:.0%} (STABILISED proxy)",
         skip_rate >= TARGET_SKIP_RATE_STABILISED),
        ("Vision Tier 5 fallback rate", f"{vision_rate:.1%}", f"<= {TARGET_VISION_FALLBACK_RATE:.0%}",
         vision_rate <= TARGET_VISION_FALLBACK_RATE),
        ("Banner capture attempted (non-skipped)", f"{banner_rate:.1%}", f"== {TARGET_BANNER_ATTEMPTED:.0%}",
         banner_rate >= TARGET_BANNER_ATTEMPTED),
        ("P95 page load (ms)", int(p95), f"< {TARGET_P95_PAGE_LOAD_MS}",
         p95 < TARGET_P95_PAGE_LOAD_MS or p95 == 0),
        ("Domains over 5% failure (7d)", bad_domains, "[]", not bad_domains),
        ("Vision sample rate", f"{sample_rate:.1%}",
         f"in [{TARGET_VISION_SAMPLE_RATE[0]:.0%}, {TARGET_VISION_SAMPLE_RATE[1]:.0%}]",
         TARGET_VISION_SAMPLE_RATE[0] <= sample_rate <= TARGET_VISION_SAMPLE_RATE[1]),
        ("Vision agreement rate", f"{avg_agreement:.1%}" if avg_agreement is not None else "n/a",
         f">= {TARGET_VISION_AGREEMENT:.0%}", avg_agreement is None or avg_agreement >= TARGET_VISION_AGREEMENT),
    ]

    print("=" * 78)
    print(f"{'METRIC':<42} {'VALUE':<18} {'TARGET':<14} OK")
    print("-" * 78)
    failures = 0
    for name, value, target, ok in rows:
        flag = "OK" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"{name:<42} {str(value):<18} {target:<14} {flag}")
    print("=" * 78)
    print(f"FAILURES: {failures}")
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
