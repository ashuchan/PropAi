"""
Refactor baseline metrics.

Reads the most recent ``data/runs/*/`` directory and produces metrics that the
refactor's later phases will be measured against. Results go to stdout as
markdown and are appended to ``docs/REFACTOR_BASELINE.md``.

Metrics (see ``claude_refactor.md`` Phase 0):
  - Tier distribution from per-property reports
  - LLM cost totals from ``llm_report.json`` / per-property report
  - Failure breakdown grouped by first error string (first 80 chars)
  - Profile maturity distribution from ``config/profiles/*.json``
  - Profiles with ``api_provider == null``
  - Scrape duration avg / p95 when available
  - Redundant LLM calls: properties with UNITS_EMPTY (from issues.jsonl) that
    also made at least one LLM call. Matches the property-5317 failure mode
    where LLM ran on an SSL-error page and burned $0.01375.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TIER_RE = re.compile(r"\*\*Extraction Tier:\*\*\s*`([^`]+)`")
UNITS_RE = re.compile(r"\|\s*Units Extracted\s*\|\s*(\d+)\s*\|", re.IGNORECASE)
LLM_CALLS_RE = re.compile(r"\|\s*LLM Calls Made\s*\|\s*(\d+)\s*\|", re.IGNORECASE)
LLM_COST_RE = re.compile(r"\|\s*LLM Total Cost\s*\|\s*\$?([\d.]+)", re.IGNORECASE)
ERRORS_SECTION_RE = re.compile(r"## Errors\s*\n+\s*1\.\s*(.+)", re.IGNORECASE)


class PropertyStats:
    __slots__ = (
        "canonical_id",
        "extraction_tier",
        "units_extracted",
        "llm_calls_made",
        "llm_cost_usd",
        "first_error",
    )

    def __init__(self, canonical_id: str) -> None:
        self.canonical_id = canonical_id
        self.extraction_tier: str = "UNKNOWN"
        self.units_extracted: int = 0
        self.llm_calls_made: int = 0
        self.llm_cost_usd: float = 0.0
        self.first_error: str | None = None


def find_latest_run(runs_dir: Path) -> Path | None:
    """Return the lexicographically-last subdirectory under ``runs_dir``.

    Run directories are named ``YYYY-MM-DD`` so lexicographic sort == date
    sort. Returns None if the directory is missing or empty.
    """
    if not runs_dir.is_dir():
        return None
    candidates = sorted(p for p in runs_dir.iterdir() if p.is_dir())
    return candidates[-1] if candidates else None


def _parse_property_report(md_text: str, canonical_id: str) -> PropertyStats:
    stats = PropertyStats(canonical_id)
    m = TIER_RE.search(md_text)
    if m:
        stats.extraction_tier = m.group(1).strip()
    m = UNITS_RE.search(md_text)
    if m:
        stats.units_extracted = int(m.group(1))
    m = LLM_CALLS_RE.search(md_text)
    if m:
        stats.llm_calls_made = int(m.group(1))
    m = LLM_COST_RE.search(md_text)
    if m:
        try:
            stats.llm_cost_usd = float(m.group(1))
        except ValueError:
            stats.llm_cost_usd = 0.0
    m = ERRORS_SECTION_RE.search(md_text)
    if m:
        stats.first_error = m.group(1).strip()[:80]
    return stats


def collect_property_stats(run_dir: Path) -> list[PropertyStats]:
    reports_dir = run_dir / "property_reports"
    if not reports_dir.is_dir():
        return []
    out: list[PropertyStats] = []
    for md_path in sorted(reports_dir.glob("*.md")):
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError:
            continue
        out.append(_parse_property_report(text, md_path.stem))
    return out


def load_issues(run_dir: Path) -> list[dict[str, Any]]:
    issues_path = run_dir / "issues.jsonl"
    if not issues_path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with issues_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def load_profile_maturity(profiles_dir: Path) -> tuple[Counter[str], int, int]:
    """Returns (maturity_counter, unknown_api_provider_count, total_profiles)."""
    if not profiles_dir.is_dir():
        return Counter(), 0, 0
    maturity: Counter[str] = Counter()
    unknown_provider = 0
    total = 0
    for fp in sorted(profiles_dir.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        total += 1
        m = (data.get("confidence") or {}).get("maturity") or "UNKNOWN"
        maturity[str(m)] += 1
        provider = (data.get("api_hints") or {}).get("api_provider")
        if provider in (None, "", "unknown"):
            unknown_provider += 1
    return maturity, unknown_provider, total


def load_llm_report_duration(run_dir: Path) -> tuple[float | None, float | None]:
    """Return (avg_s, p95_s) from run-level report if available; else (None, None)."""
    report_path = run_dir / "report.json"
    if not report_path.is_file():
        return None, None
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    duration = data.get("duration_s")
    total = (data.get("totals") or {}).get("properties_processed")
    if isinstance(duration, (int, float)) and isinstance(total, int) and total > 0:
        avg = duration / total
        return float(avg), None
    return None, None


def _table(header: Iterable[str], rows: Iterable[Iterable[Any]]) -> str:
    header_list = list(header)
    sep = ["---"] * len(header_list)
    lines = ["| " + " | ".join(header_list) + " |", "| " + " | ".join(sep) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


def build_tier_table(stats_list: list[PropertyStats]) -> str:
    total = len(stats_list)
    counter = Counter(s.extraction_tier for s in stats_list)
    rows = []
    for tier, count in counter.most_common():
        pct = (100.0 * count / total) if total else 0.0
        rows.append((tier, count, f"{pct:.1f}%"))
    return _table(["Extraction Tier", "Count", "% of total"], rows)


def build_llm_cost_block(stats_list: list[PropertyStats]) -> str:
    total_cost = sum(s.llm_cost_usd for s in stats_list)
    properties_with_llm = sum(1 for s in stats_list if s.llm_calls_made > 0)
    avg_per_llm_prop = (total_cost / properties_with_llm) if properties_with_llm else 0.0
    return _table(
        ["Metric", "Value"],
        [
            ("Total LLM cost (USD)", f"${total_cost:.5f}"),
            ("Properties using LLM", properties_with_llm),
            ("Avg cost per LLM-using property", f"${avg_per_llm_prop:.5f}"),
        ],
    )


def build_failure_table(stats_list: list[PropertyStats]) -> str:
    failures = [s for s in stats_list if s.extraction_tier in {"FAILED", "UNKNOWN"}]
    counter: Counter[str] = Counter()
    for s in failures:
        key = (s.first_error or "(no error message)")[:80]
        counter[key] += 1
    rows = [(err, cnt) for err, cnt in counter.most_common()]
    return _table(["First error (<=80 chars)", "Count"], rows) if rows else "_No failures._"


def build_maturity_table(maturity: Counter[str]) -> str:
    if not maturity:
        return "_No profiles found._"
    rows = [(k, v) for k, v in maturity.most_common()]
    return _table(["Maturity", "Count"], rows)


def count_wasted_llm_calls(stats_list: list[PropertyStats], issues: list[dict[str, Any]]) -> int:
    """A property is 'wasted' if UNITS_EMPTY fires AND at least one LLM call was made.

    Captures property 5317's failure mode — LLM burned money on a page with no
    content.
    """
    empty_ids = {str(it.get("canonical_id")) for it in issues if it.get("code") == "UNITS_EMPTY"}
    return sum(1 for s in stats_list if s.canonical_id in empty_ids and s.llm_calls_made > 0)


def build_duration_block(avg_s: float | None, p95_s: float | None) -> str:
    def fmt(v: float | None) -> str:
        return f"{v:.1f}s" if v is not None else "n/a"

    return _table(["Metric", "Value"], [("Avg scrape duration", fmt(avg_s)), ("P95 scrape duration", fmt(p95_s))])


def produce_report(run_dir: Path, profiles_dir: Path) -> str:
    stats_list = collect_property_stats(run_dir)
    issues = load_issues(run_dir)
    maturity, unknown_provider, total_profiles = load_profile_maturity(profiles_dir)
    avg_s, p95_s = load_llm_report_duration(run_dir)
    wasted = count_wasted_llm_calls(stats_list, issues)
    unknown_pct = (100.0 * unknown_provider / total_profiles) if total_profiles else 0.0

    sections: list[str] = []
    sections.append(f"### Run: `{run_dir.name}`")
    sections.append(f"Properties with reports: **{len(stats_list)}**")
    sections.append("")
    sections.append("#### Tier distribution")
    sections.append(build_tier_table(stats_list))
    sections.append("")
    sections.append("#### LLM cost")
    sections.append(build_llm_cost_block(stats_list))
    sections.append("")
    sections.append("#### Failure breakdown")
    sections.append(build_failure_table(stats_list))
    sections.append("")
    sections.append("#### Profile maturity distribution")
    sections.append(build_maturity_table(maturity))
    sections.append("")
    sections.append("#### Profiles with `api_provider == null`")
    sections.append(
        _table(
            ["Metric", "Value"],
            [
                ("Profiles with unknown api_provider", unknown_provider),
                ("Total profiles", total_profiles),
                ("% unknown", f"{unknown_pct:.1f}%"),
            ],
        )
    )
    sections.append("")
    sections.append("#### Timing")
    sections.append(build_duration_block(avg_s, p95_s))
    sections.append("")
    sections.append("#### Redundant LLM calls (UNITS_EMPTY + LLM calls > 0)")
    sections.append(_table(["Metric", "Value"], [("Properties wasting LLM spend", wasted)]))
    return "\n".join(sections)


def append_to_baseline_doc(doc_path: Path, body: str) -> None:
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    marker = f"\n\n## Baseline captured {timestamp}\n\n"
    existing = doc_path.read_text(encoding="utf-8") if doc_path.is_file() else ""
    if "# Refactor Baseline" not in existing:
        existing = _default_baseline_header() + "\n"
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(existing + marker + body + "\n", encoding="utf-8")


def _safe_print(text: str) -> None:
    """Print without crashing on Windows cp1252 consoles."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


def _default_baseline_header() -> str:
    return (
        "# Refactor Baseline\n\n"
        "## Current-pipeline metrics\n\n"
        "_Filled by `scripts/refactor_baseline.py` — see appended runs below._\n\n"
        "## Known PMS distribution in current property set\n\n"
        "_Fill manually from handoff doc + CSV inspection._\n\n"
        "## Target metrics after refactor (hypothesis)\n"
        "- Tier-1 success rate: current X% → target Y%\n"
        "- LLM calls per property (median): current 0–3 → target 0\n"
        "- LLM $ per daily run: current $A → target $B\n"
        "- Redundant-call count: current N → target 0\n"
        "- `api_provider == null` profiles: current M% → target <10%\n"
    )


def run(project_root: Path) -> int:
    runs_dir = project_root / "data" / "runs"
    profiles_dir = project_root / "config" / "profiles"
    doc_path = project_root / "docs" / "REFACTOR_BASELINE.md"

    run_dir = find_latest_run(runs_dir)
    if run_dir is None:
        print(f"No runs found under {runs_dir}", file=sys.stderr)
        return 1

    body = produce_report(run_dir, profiles_dir)
    _safe_print(body)
    append_to_baseline_doc(doc_path, body)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Project root (defaults to ma_poc/)",
    )
    args = parser.parse_args(argv)
    return run(args.root)


if __name__ == "__main__":
    raise SystemExit(main())
