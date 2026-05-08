"""Escalation baseline metrics script.

Reads the most recent run under data/runs/ and produces ESCALATION_BASELINE.md
with:
  - Outcome distribution (OK, BOT_BLOCKED, RATE_LIMITED, TRANSIENT, HARD_FAIL,
    PROXY_ERROR, NOT_MODIFIED) at fetch level
  - Block-signature breakdown of BOT_BLOCKED outcomes (regex over raw HTML)
  - Property success rate: with-proxy vs no-proxy
  - Cost baseline: proxy MB and LLM cost from run report
  - Top 20 blocked domains

Usage:
    python scripts/escalation_baseline.py
    python scripts/escalation_baseline.py --data-dir ./data --out docs/ESCALATION_BASELINE.md
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger(__name__)

# Block-signature patterns — mirrors Phase E2 BLOCK_SIGNATURES table.
# Used here for best-effort signature detection in raw HTML.
BLOCK_SIGNATURES: list[tuple[str, list[bytes]]] = [
    ("cf_turnstile", [
        b"challenges.cloudflare.com/turnstile",
        b"cf-turnstile-response",
    ]),
    ("cf_challenge", [
        b"Just a moment",
        b"cf-chl-bypass",
        b"cdn-cgi/challenge-platform",
        b"__cf_chl_",
    ]),
    ("px_block", [
        b"px-captcha",
        b"_pxhd",
        b"PerimeterX",
        b"px-cdn.net",
    ]),
    ("datadome", [
        b"datadome",
        b"dd_cookie",
        b"geo.captcha-delivery.com",
    ]),
    ("akamai_bm", [
        b"_abck",
        b"ak_bmsc",
        b"reference #18.",
    ]),
    ("hcaptcha", [
        b"hcaptcha.com/captcha",
        b"h-captcha",
    ]),
    ("recaptcha", [
        b"recaptcha/api2",
        b"g-recaptcha",
    ]),
    ("imperva", [
        b"_Incapsula_Resource",
        b"visid_incap_",
    ]),
    ("generic_403", []),
]


def _match_signature(body: bytes) -> str:
    """Return the first matching block signature, or 'unknown'."""
    scan = body[:65536]
    for name, patterns in BLOCK_SIGNATURES:
        if name == "generic_403":
            continue
        if any(p in scan for p in patterns):
            return name
    return "generic_403"


def _find_most_recent_run(data_dir: Path) -> Path | None:
    runs_dir = data_dir / "runs"
    if not runs_dir.exists():
        return None
    candidates = sorted(
        (d for d in runs_dir.iterdir() if d.is_dir()),
        key=lambda d: d.name,
    )
    return candidates[-1] if candidates else None


def _load_report(run_dir: Path) -> dict:
    report_path = run_dir / "report.json"
    if report_path.exists():
        try:
            return json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _load_events(run_dir: Path) -> list[dict]:
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return []
    events: list[dict] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            pass
    return events


def _load_properties(run_dir: Path) -> list[dict]:
    props_path = run_dir / "properties.json"
    if not props_path.exists():
        return []
    try:
        data = json.loads(props_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _scan_raw_html_for_signatures(run_dir: Path) -> dict[str, int]:
    """Scan raw HTML files for block signatures. Returns signature → count."""
    sig_counts: Counter[str] = Counter()
    raw_dir = run_dir / "raw_html"
    if not raw_dir.exists():
        return {}
    for date_dir in raw_dir.iterdir():
        if not date_dir.is_dir():
            continue
        for html_file in date_dir.glob("*.html.gz"):
            try:
                body = gzip.decompress(html_file.read_bytes())
                sig = _match_signature(body)
                sig_counts[sig] += 1
            except Exception:
                pass
    return dict(sig_counts)


def _extract_outcome_distribution(events: list[dict]) -> dict[str, int]:
    """Count fetch outcomes from FETCH_COMPLETED events."""
    counts: Counter[str] = Counter()
    for ev in events:
        if ev.get("kind") == "fetch.completed":
            outcome = ev.get("outcome", "UNKNOWN")
            counts[outcome.upper()] += 1
    return dict(counts)


def _extract_bot_blocked_domains(events: list[dict]) -> Counter[str]:
    """Count BOT_BLOCKED hits by host."""
    host_counts: Counter[str] = Counter()
    for ev in events:
        if ev.get("kind") == "fetch.completed" and ev.get("outcome", "").upper() == "BOT_BLOCKED":
            url = ev.get("final_url") or ev.get("url") or ""
            if url:
                try:
                    from urllib.parse import urlparse
                    host = urlparse(url).netloc
                    if host:
                        host_counts[host] += 1
                except Exception:
                    pass
    return host_counts


def _extract_proxy_success_rates(events: list[dict]) -> dict[str, dict[str, int]]:
    """Split outcomes by whether a proxy was used."""
    with_proxy: Counter[str] = Counter()
    no_proxy: Counter[str] = Counter()
    for ev in events:
        if ev.get("kind") != "fetch.completed":
            continue
        outcome = ev.get("outcome", "UNKNOWN").upper()
        if ev.get("proxy_used"):
            with_proxy[outcome] += 1
        else:
            no_proxy[outcome] += 1
    return {"with_proxy": dict(with_proxy), "no_proxy": dict(no_proxy)}


def build_baseline(data_dir: Path) -> str:
    """Build the ESCALATION_BASELINE.md content."""
    run_dir = _find_most_recent_run(data_dir)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    if run_dir is None:
        return _empty_baseline(ts)

    events = _load_events(run_dir)
    report = _load_report(run_dir)
    sig_counts = _scan_raw_html_for_signatures(run_dir)
    outcome_dist = _extract_outcome_distribution(events)
    bot_domains = _extract_bot_blocked_domains(events)
    proxy_rates = _extract_proxy_success_rates(events)

    lines: list[str] = [
        "# Escalation Baseline",
        f"\n_Captured: {ts} — Run: {run_dir.name}_\n",
        "---\n",
    ]

    # Section 1: Outcome distribution
    lines.append("## 1. Fetch Outcome Distribution\n")
    all_outcomes = ["OK", "NOT_MODIFIED", "BOT_BLOCKED", "RATE_LIMITED", "TRANSIENT", "HARD_FAIL", "PROXY_ERROR"]
    if outcome_dist:
        total = sum(outcome_dist.values())
        lines.append("| Outcome | Count | % |")
        lines.append("|---------|-------|---|")
        for o in all_outcomes:
            c = outcome_dist.get(o, 0)
            pct = f"{100 * c / total:.1f}" if total else "0.0"
            lines.append(f"| {o} | {c} | {pct}% |")
        other = {k: v for k, v in outcome_dist.items() if k not in all_outcomes}
        for o, c in sorted(other.items()):
            pct = f"{100 * c / total:.1f}" if total else "0.0"
            lines.append(f"| {o} | {c} | {pct}% |")
        lines.append(f"\n_Total fetch events: {total}_\n")
    else:
        lines.append("_No fetch events found in this run._\n")

    # Section 2: Block-signature breakdown
    lines.append("## 2. Block-Signature Breakdown (BOT_BLOCKED)\n")
    if sig_counts:
        total_blocked = sum(sig_counts.values())
        lines.append("| Signature | Count | % of blocked |")
        lines.append("|-----------|-------|--------------|")
        for sig, count in sorted(sig_counts.items(), key=lambda x: -x[1]):
            pct = f"{100 * count / total_blocked:.1f}" if total_blocked else "0.0"
            lines.append(f"| {sig} | {count} | {pct}% |")
        lines.append(f"\n_Total raw HTML files with signatures: {total_blocked}_\n")
    else:
        lines.append("_No raw HTML files found or no BOT_BLOCKED signatures detected._\n")

    # Section 3: Property success rate by proxy state
    lines.append("## 3. Property Success Rate by Proxy State\n")
    for label, counts in [("With proxy", proxy_rates["with_proxy"]), ("No proxy", proxy_rates["no_proxy"])]:
        total = sum(counts.values())
        ok = counts.get("OK", 0) + counts.get("NOT_MODIFIED", 0)
        pct = f"{100 * ok / total:.1f}" if total else "0.0"
        lines.append(f"**{label}**: {ok}/{total} success ({pct}%)")
        if counts:
            for o, c in sorted(counts.items(), key=lambda x: -x[1]):
                lines.append(f"  - {o}: {c}")
        lines.append("")

    # Section 4: Cost baseline
    lines.append("## 4. Cost Baseline\n")
    proxy_mb = report.get("proxy_mb_used") or report.get("total_proxy_mb", 0)
    llm_cost = report.get("llm_cost_usd") or report.get("total_llm_cost_usd", 0.0)
    props_total = report.get("total_properties") or report.get("properties_total", 0)
    props_success = report.get("successful_properties") or report.get("properties_success", 0)

    lines.append(f"- Total properties: {props_total}")
    lines.append(f"- Successful properties: {props_success}")
    lines.append(f"- Proxy MB used: {proxy_mb}")
    lines.append(f"- LLM cost USD: {llm_cost}")
    lines.append(f"- Proxy cost estimated USD: $0.00 (datacenter pool; MB billing not tracked yet)\n")

    # Section 5: Top 20 blocked domains
    lines.append("## 5. Top 20 Blocked Domains\n")
    if bot_domains:
        lines.append("| Domain | BOT_BLOCKED count |")
        lines.append("|--------|------------------|")
        for domain, count in bot_domains.most_common(20):
            lines.append(f"| {domain} | {count} |")
    else:
        lines.append("_No BOT_BLOCKED events found in this run._\n")

    lines.append("\n---\n_End of escalation baseline._\n")
    return "\n".join(lines)


def _empty_baseline(ts: str) -> str:
    """Return a valid baseline doc when no run data exists."""
    return f"""# Escalation Baseline

_Captured: {ts} — No run data found_

---

## 1. Fetch Outcome Distribution

_No data/runs/ directory found. Run the daily runner first._

## 2. Block-Signature Breakdown (BOT_BLOCKED)

_No data available._

## 3. Property Success Rate by Proxy State

_No data available._

## 4. Cost Baseline

- Total properties: 0
- Successful properties: 0
- Proxy MB used: 0
- LLM cost USD: 0.0

## 5. Top 20 Blocked Domains

_No data available._

---
_End of escalation baseline._
"""


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Generate ESCALATION_BASELINE.md from most recent run")
    parser.add_argument("--data-dir", default=str(ROOT / "data"), help="Path to data/ directory")
    parser.add_argument("--out", default=str(ROOT / "docs" / "ESCALATION_BASELINE.md"), help="Output path")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    log.info("Building escalation baseline from %s", data_dir)
    content = build_baseline(data_dir)

    out_path.write_text(content, encoding="utf-8")
    log.info("Wrote %s (%d bytes)", out_path, len(content))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
