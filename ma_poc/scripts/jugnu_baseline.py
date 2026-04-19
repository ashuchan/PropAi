"""
Jugnu J0 — capture baseline metrics from the latest run.

Usage: python scripts/jugnu_baseline.py [--run-dir data/runs/2026-04-17]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("jugnu_baseline")

_MA_POC_ROOT = Path(__file__).resolve().parent.parent  # ma_poc/


def _schema_data_root(data_dir: Path) -> Path:
    """Return data/v2/ or data/ depending on SCHEMA_VERSION env var."""
    version = os.getenv("SCHEMA_VERSION", "v1").strip().lower()
    return data_dir / "v2" if version == "v2" else data_dir


@dataclass(frozen=True)
class BaselineMetrics:
    """Immutable baseline metrics captured from a single run."""

    run_dir: str
    totals: dict[str, Any]
    tier_distribution: dict[str, Any]
    llm_cost: dict[str, Any]
    failure_signatures: list[dict[str, Any]]
    profile_maturity: dict[str, Any]
    timing: dict[str, Any]
    change_detection: dict[str, Any]


def find_latest_run_dir(runs_root: Path) -> Path:
    """Return the most recent run directory by lexicographic name sort.

    Raises FileNotFoundError if no run directories exist.
    """
    if not runs_root.exists():
        raise FileNotFoundError(f"Runs directory does not exist: {runs_root}")
    dirs = sorted(
        [d for d in runs_root.iterdir() if d.is_dir()],
        key=lambda p: p.name,
    )
    if not dirs:
        raise FileNotFoundError(f"No run directories found in {runs_root}")
    return dirs[-1]


def load_properties_json(run_dir: Path) -> list[dict[str, Any]]:
    """Load properties.json from a run directory.

    Returns an empty list if the file is missing or malformed.
    """
    path = run_dir / "properties.json"
    if not path.exists():
        log.warning("properties.json not found in %s", run_dir)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data  # type: ignore[return-value]
        log.warning("properties.json is not a list, got %s", type(data).__name__)
        return []
    except json.JSONDecodeError as exc:
        log.warning("Failed to parse properties.json: %s", exc)
        return []


def compute_totals(props: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute run-level totals: total, ok, failed, carry-forward counts."""
    total = len(props)
    failed = 0
    ok = 0
    carry_forward = 0
    for p in props:
        meta = p.get("_meta", {})
        tier = meta.get("scrape_tier_used", "")
        if tier and "FAIL" in str(tier).upper():
            failed += 1
        elif meta.get("carry_forward_used"):
            carry_forward += 1
        elif tier:
            ok += 1
        else:
            failed += 1  # None tier = failed
    failure_rate = (failed / total * 100) if total > 0 else 0.0
    return {
        "csv_rows": total,
        "properties_ok": ok,
        "properties_failed": failed,
        "failure_rate_pct": round(failure_rate, 2),
        "properties_carry_forward": carry_forward,
        "properties_dlq_eligible": failed,  # simplified: all current failures
    }


def compute_tier_distribution(props: list[dict[str, Any]]) -> dict[str, Any]:
    """Count properties by extraction tier used."""
    counter: Counter[str] = Counter()
    for p in props:
        tier = p.get("_meta", {}).get("scrape_tier_used") or "UNKNOWN"
        counter[tier] += 1
    total = len(props) or 1
    distribution = {
        tier: {"count": count, "pct": round(count / total * 100, 1)}
        for tier, count in counter.most_common()
    }
    return distribution


def compute_llm_cost(
    props: list[dict[str, Any]], run_dir: Path
) -> dict[str, Any]:
    """Compute LLM cost breakdown from llm_report.json or property records."""
    result: dict[str, Any] = {
        "total_cost_usd": 0.0,
        "properties_with_llm_calls": 0,
        "properties_with_vision_calls": 0,
        "avg_cost_per_llm_property": 0.0,
        "wasted_llm_calls": [],
        "wasted_count": 0,
    }
    llm_report_path = run_dir / "llm_report.json"
    if llm_report_path.exists():
        try:
            lr = json.loads(llm_report_path.read_text(encoding="utf-8"))
            summary = lr.get("summary", {})
            result["total_cost_usd"] = summary.get("total_cost_usd", 0.0)
            result["properties_with_llm_calls"] = summary.get(
                "properties_with_calls", 0
            )
            by_prop = lr.get("by_property", [])
            # by_property can be a list of dicts or a dict keyed by pid
            items: list[tuple[str, dict]] = []
            if isinstance(by_prop, dict):
                items = list(by_prop.items())
            elif isinstance(by_prop, list):
                items = [
                    (p.get("property_id", "unknown"), p) for p in by_prop
                ]
            for pid, pdata in items:
                cost = pdata.get("cost_usd", 0.0)
                if not isinstance(cost, (int, float)):
                    cost = 0.0
                matching = [
                    p for p in props
                    if p.get("_meta", {}).get("canonical_id") == pid
                ]
                if matching:
                    units = matching[0].get("units", [])
                    if not units and cost > 0:
                        result["wasted_llm_calls"].append(
                            {"canonical_id": pid, "cost_usd": cost}
                        )
        except (json.JSONDecodeError, KeyError) as exc:
            log.warning("Failed to parse llm_report.json: %s", exc)
    else:
        # Fallback: sum from _llm_interactions in properties
        for p in props:
            interactions = p.get("_llm_interactions", [])
            if interactions:
                result["properties_with_llm_calls"] += 1
                cost = sum(i.get("cost_usd", 0) for i in interactions)
                result["total_cost_usd"] += cost
                if not p.get("units") and cost > 0:
                    cid = p.get("_meta", {}).get("canonical_id", "unknown")
                    result["wasted_llm_calls"].append(
                        {"canonical_id": cid, "cost_usd": cost}
                    )

    result["wasted_count"] = len(result["wasted_llm_calls"])
    llm_count = result["properties_with_llm_calls"] or 1
    result["avg_cost_per_llm_property"] = round(
        result["total_cost_usd"] / llm_count, 4
    )
    return result


def compute_failure_signatures(
    props: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group failed properties by error signature (first 80 chars)."""
    sig_groups: dict[str, list[str]] = {}
    for p in props:
        meta = p.get("_meta", {})
        tier = meta.get("scrape_tier_used") or ""
        if "FAIL" not in str(tier).upper() and tier:
            continue
        errors = meta.get("scrape_errors", [])
        sig = str(errors[0])[:80] if errors else "NO_ERROR_MESSAGE"
        cid = meta.get("canonical_id", "unknown")
        sig_groups.setdefault(sig, []).append(cid)
    return [
        {
            "signature": sig,
            "count": len(cids),
            "sample_cids": cids[:5],
        }
        for sig, cids in sorted(sig_groups.items(), key=lambda x: -len(x[1]))
    ]


def compute_profile_maturity(profiles_dir: Path) -> dict[str, Any]:
    """Count profiles by maturity level and null api_provider."""
    result: dict[str, Any] = {
        "COLD": 0,
        "WARM": 0,
        "HOT": 0,
        "total": 0,
        "api_provider_null": 0,
        "api_provider_null_pct": 0.0,
    }
    if not profiles_dir.exists():
        log.warning("Profiles directory does not exist: %s", profiles_dir)
        return result
    profile_files = list(profiles_dir.glob("*.json"))
    result["total"] = len(profile_files)
    for pf in profile_files:
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
            maturity = (
                data.get("confidence", {}).get("maturity", "COLD")
                if isinstance(data.get("confidence"), dict)
                else "COLD"
            )
            result[maturity] = result.get(maturity, 0) + 1
            api_hints = data.get("api_hints", {})
            provider = api_hints.get("api_provider") if api_hints else None
            if not provider:
                result["api_provider_null"] += 1
        except (json.JSONDecodeError, KeyError):
            result["COLD"] += 1
    total = result["total"] or 1
    result["api_provider_null_pct"] = round(
        result["api_provider_null"] / total * 100, 1
    )
    return result


def compute_timing(
    props: list[dict[str, Any]], run_dir: Path
) -> dict[str, Any]:
    """Compute p50/p95 scrape durations if timing data is available."""
    durations: list[float] = []
    for p in props:
        meta = p.get("_meta", {})
        elapsed = meta.get("elapsed_ms") or meta.get("scrape_duration_ms")
        if elapsed is not None:
            durations.append(float(elapsed) / 1000.0)
    if not durations:
        return {"p50_seconds": None, "p95_seconds": None, "count": 0}
    durations.sort()
    n = len(durations)
    p50 = durations[int(n * 0.5)]
    p95 = durations[int(n * 0.95)]
    return {
        "p50_seconds": round(p50, 2),
        "p95_seconds": round(p95, 2),
        "count": n,
    }


def write_markdown(metrics: BaselineMetrics, out_path: Path) -> None:
    """Write the baseline markdown report to disk."""
    t = metrics.totals
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"# Jugnu Baseline — captured {now}",
        "",
        f"Source run: `{metrics.run_dir}`",
        "",
        "## 1. Totals",
        "",
        "| Metric | Value | Notes |",
        "|---|---|---|",
        f"| CSV rows | {t['csv_rows']} | |",
        f"| Properties OK | {t['properties_ok']} | |",
        f"| Properties failed | {t['properties_failed']} | |",
        f"| Failure rate | {t['failure_rate_pct']}% | |",
        f"| Carry-forward | {t['properties_carry_forward']} | |",
        f"| DLQ eligible | {t['properties_dlq_eligible']} | |",
        "",
        "## 2. Tier distribution",
        "",
        "| Tier | Count | % |",
        "|---|---|---|",
    ]
    for tier, info in metrics.tier_distribution.items():
        lines.append(f"| {tier} | {info['count']} | {info['pct']}% |")
    lines.append("")
    lines.append("## 3. LLM cost")
    lines.append("")
    lc = metrics.llm_cost
    lines.extend([
        "| Metric | Value |",
        "|---|---|",
        f"| Total cost | ${lc['total_cost_usd']:.4f} |",
        f"| Properties with LLM calls | {lc['properties_with_llm_calls']} |",
        f"| Properties with Vision calls | {lc['properties_with_vision_calls']} |",
        f"| Avg cost per LLM property | ${lc['avg_cost_per_llm_property']:.4f} |",
        "",
        "### Wasted LLM calls",
        "",
        f"Count: {lc['wasted_count']}",
        "",
    ])
    for w in lc["wasted_llm_calls"][:10]:
        lines.append(f"- `{w['canonical_id']}` — ${w['cost_usd']:.4f}")
    lines.append("")
    lines.append("## 4. Failure signatures")
    lines.append("")
    lines.append("| Signature | Count | Sample CIDs |")
    lines.append("|---|---|---|")
    for fs in metrics.failure_signatures:
        cids = ", ".join(f"`{c}`" for c in fs["sample_cids"])
        lines.append(f"| {fs['signature']} | {fs['count']} | {cids} |")
    lines.append("")
    lines.append("## 5. Profile maturity")
    lines.append("")
    pm = metrics.profile_maturity
    total_p = pm["total"] or 1
    lines.extend([
        "| Maturity | Count | % |",
        "|---|---|---|",
        f"| COLD | {pm['COLD']} | {pm['COLD']/total_p*100:.1f}% |",
        f"| WARM | {pm['WARM']} | {pm['WARM']/total_p*100:.1f}% |",
        f"| HOT | {pm['HOT']} | {pm['HOT']/total_p*100:.1f}% |",
        "",
        f"Properties with `api_provider == null`: {pm['api_provider_null']}"
        f" ({pm['api_provider_null_pct']}%).",
        "",
    ])
    lines.append("## 6. Timing")
    lines.append("")
    tm = metrics.timing
    if tm["p50_seconds"] is not None:
        lines.append(f"- P50: {tm['p50_seconds']}s")
        lines.append(f"- P95: {tm['p95_seconds']}s")
        lines.append(f"- Sample count: {tm['count']}")
    else:
        lines.append("No per-property timing data available.")
    lines.append("")
    lines.append("## 7. Change detection")
    lines.append("")
    lines.append("Current skip rate: 0% (not implemented).")
    lines.append("")
    lines.append("## 8. Targets for Jugnu (to be filled by the human)")
    lines.append("")
    lines.extend([
        "| Metric | Current (J0) | Target (post-J9) |",
        "|---|---|---|",
        f"| Success rate | {100 - t['failure_rate_pct']}% | >= 95% |",
        f"| LLM cost / run | ${lc['total_cost_usd']:.4f} |"
        f" <= ${lc['total_cost_usd'] * 0.1:.4f} |",
        f"| Wasted LLM calls | {lc['wasted_count']} | 0 |",
        f"| api_provider == null | {pm['api_provider_null_pct']}% | < 10% |",
        "| Change-detection skip | 0% | >= 30% |",
        f"| Failure rate | {t['failure_rate_pct']}% | <= 5% |",
        "",
    ])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Wrote baseline markdown to %s", out_path)


def write_json(metrics: BaselineMetrics, out_path: Path) -> None:
    """Write the baseline metrics as machine-readable JSON."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(asdict(metrics), indent=2, default=str),
        encoding="utf-8",
    )
    log.info("Wrote baseline JSON to %s", out_path)


def main() -> int:
    """Entry point: parse args, compute metrics, write outputs."""
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Jugnu J0 — baseline metrics")
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Explicit run directory (default: latest in data/runs/)",
    )
    parser.add_argument(
        "--runs-root",
        type=Path,
        default=_schema_data_root(_MA_POC_ROOT / "data") / "runs",
        help="Root directory containing run directories (default: "
             "ma_poc/data/runs or ma_poc/data/v2/runs per SCHEMA_VERSION env)",
    )
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=_MA_POC_ROOT / "config" / "profiles",
        help="Directory containing profile JSON files (default: ma_poc/config/profiles)",
    )
    args = parser.parse_args()

    run_dir = args.run_dir or find_latest_run_dir(args.runs_root)
    log.info("Using run directory: %s", run_dir)

    props = load_properties_json(run_dir)
    if not props:
        log.error("No properties loaded — cannot compute baseline.")
        return 1

    metrics = BaselineMetrics(
        run_dir=str(run_dir),
        totals=compute_totals(props),
        tier_distribution=compute_tier_distribution(props),
        llm_cost=compute_llm_cost(props, run_dir),
        failure_signatures=compute_failure_signatures(props),
        profile_maturity=compute_profile_maturity(args.profiles_dir),
        timing=compute_timing(props, run_dir),
        change_detection={"skip_rate_pct": 0.0, "note": "not implemented"},
    )

    run_date = Path(run_dir).name
    write_json(metrics, Path(f"data/baseline/{run_date}.json"))
    write_markdown(metrics, Path("docs/JUGNU_BASELINE.md"))

    log.info("Baseline capture complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
