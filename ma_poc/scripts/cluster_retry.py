"""Cluster-retry tool for PMC-portal failures.

Usage:
  # Analyze failure clusters in a run
  python scripts/cluster_retry.py --analyze data/runs/2026-04-20/properties.json

  # Retry properties from a specific registered domain
  python scripts/cluster_retry.py --retry gscapts.com --run-date 2026-04-21
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

try:
    import tldextract  # type: ignore[import]

    _HAS_TLDEXTRACT = True
except ImportError:
    _HAS_TLDEXTRACT = False


def registered_domain(url: str) -> str:
    """Extract the registered domain (e.g. 'gscapts.com') from a URL.

    Uses tldextract when available for accurate eTLD handling.
    Falls back to the last two labels of the hostname.
    """
    if not url:
        return ""
    try:
        if _HAS_TLDEXTRACT:
            ext = tldextract.extract(url)
            if ext.domain and ext.suffix:
                return f"{ext.domain}.{ext.suffix}"
        host = urlparse(url).hostname or ""
        parts = host.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return host
    except Exception:
        return ""


def _truncate(text: str, n: int = 500) -> str:
    return text[:n] + ("..." if len(text) > n else "")


def analyze(properties_json_path: Path) -> dict[str, Any]:
    """Read a properties.json and group failures by registered domain.

    Returns a dict with cluster analysis; also writes a markdown report
    alongside the input file.
    """
    data: list[dict] = json.loads(properties_json_path.read_text(encoding="utf-8"))

    # Group all properties by registered domain (not just failures)
    by_domain: dict[str, list[dict]] = defaultdict(list)
    for prop in data:
        website = (
            prop.get("Website") or prop.get("website") or prop.get("base_url") or prop.get("_base_url") or ""
        )
        domain = registered_domain(website)
        if domain:
            by_domain[domain].append(prop)

    # Count failures per domain
    domain_stats: list[dict[str, Any]] = []
    for domain, props in sorted(by_domain.items()):
        meta_list = [p.get("_meta", {}) or {} for p in props]
        failure_count = sum(1 for m in meta_list if str(m.get("verdict", "")).startswith("FAILED"))
        if failure_count == 0:
            continue
        # Tier distribution
        tier_counter: Counter[str] = Counter()
        for m in meta_list:
            tier = m.get("scrape_tier_used") or "UNKNOWN"
            tier_counter[tier] += 1
        # Sample API body
        sample_body = ""
        for prop in props:
            raw_apis = prop.get("_raw_api_responses") or prop.get("api_calls_intercepted") or []
            if raw_apis:
                if isinstance(raw_apis[0], dict):
                    body = raw_apis[0].get("body")
                    if body:
                        sample_body = _truncate(json.dumps(body) if not isinstance(body, str) else body)
                elif isinstance(raw_apis[0], str):
                    sample_body = _truncate(raw_apis[0])
                if sample_body:
                    break
        recommendation = (
            "dedicated_adapter"
            if failure_count >= 3 and not sample_body
            else "pms_hints_needed"
            if failure_count >= 3
            else "generic_adapter_fix"
        )
        domain_stats.append(
            {
                "domain": domain,
                "total_properties": len(props),
                "failure_count": failure_count,
                "tier_distribution": dict(tier_counter.most_common()),
                "sample_body": sample_body,
                "recommendation": recommendation,
            }
        )

    # Sort by failure_count descending
    domain_stats.sort(key=lambda d: -d["failure_count"])

    analysis: dict[str, Any] = {"clusters": domain_stats}

    # Write markdown report
    report_path = properties_json_path.parent / "cluster_analysis.md"
    lines = [
        "# Cluster Analysis",
        "",
        f"Source: {properties_json_path}",
        "",
        "## Failure Clusters",
        "",
    ]
    for stat in domain_stats:
        lines.extend(
            [
                f"### {stat['domain']} — {stat['failure_count']} failures / {stat['total_properties']} total",
                "",
                f"**Tier distribution:** {stat['tier_distribution']}",
                "",
                f"**Recommendation:** {stat['recommendation']}",
                "",
            ]
        )
        if stat["sample_body"]:
            lines.extend(
                [
                    "**Sample API body (first 500 chars):**",
                    "",
                    f"```\n{stat['sample_body']}\n```",
                    "",
                ]
            )

    report_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Cluster analysis written to %s", report_path)
    print(f"Analysis written to {report_path}")

    return analysis


def retry(domain: str, run_date: str | None = None) -> None:
    """Re-run the Jugnu pipeline filtered to properties matching the given domain.

    This re-imports jugnu_runner's core logic rather than spawning a subprocess
    so the process stays in-tree and environment is shared.
    """
    try:
        import scripts.jugnu_runner as runner  # type: ignore[import]
    except ImportError:
        print("Error: scripts/jugnu_runner.py not importable. Run from the ma_poc/ directory.")
        sys.exit(1)

    print(f"Retry: filtering to domain '{domain}'")
    # jugnu_runner entry points vary — call the module-level main if available
    if hasattr(runner, "main"):
        sys.argv = ["jugnu_runner.py", "--filter-domain", domain]
        if run_date:
            sys.argv += ["--run-date", run_date]
        try:
            runner.main()
        except SystemExit:
            pass
    else:
        print("jugnu_runner.main() not found — cannot retry programmatically")


def filter_by_domain(properties: list[dict], domain: str) -> list[dict]:
    """Return only properties whose website's registered domain matches."""
    result = []
    for prop in properties:
        website = (
            prop.get("Website") or prop.get("website") or prop.get("base_url") or prop.get("_base_url") or ""
        )
        if registered_domain(website) == domain:
            result.append(prop)
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Cluster retry tool for PMC-portal failures")
    parser.add_argument("--analyze", metavar="PATH", help="Path to properties.json to analyze")
    parser.add_argument("--retry", metavar="DOMAIN", help="Registered domain to retry")
    parser.add_argument("--run-date", metavar="DATE", help="Run date override (YYYY-MM-DD)")
    args = parser.parse_args()

    if args.analyze:
        path = Path(args.analyze)
        if not path.exists():
            print(f"File not found: {path}")
            sys.exit(1)
        result = analyze(path)
        print(json.dumps(result, indent=2))
    elif args.retry:
        retry(args.retry, args.run_date)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
