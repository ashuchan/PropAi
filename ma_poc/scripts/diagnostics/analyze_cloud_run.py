#!/usr/bin/env python3
"""Daily cloud-run failure analysis utility.

Reads the per-shard artifacts from `gs://jugnu-raw-production/runs/{date}/`
(or a local mirror at `c:/tmp/run-{date}/`), aggregates the run-level
totals, categorises each failed property into the 10 patterns established
by the 2026-05-08 root-cause analysis, and writes a fresh report set into
`ma_poc/data/reports/cloud_run_{date}/`.

Optional `--compare-date` triggers a day-over-day diff (regressions,
recoveries, repeat failures, new failures, fix carry-overs).

Usage:
    # Local-only (data already mirrored to c:/tmp/run-YYYY-MM-DD/)
    python scripts/diagnostics/analyze_cloud_run.py --date 2026-05-09 --compare-date 2026-05-08

    # Auto-mirror from GCS first
    python scripts/diagnostics/analyze_cloud_run.py --date 2026-05-09 --pull

The script is intentionally read-only against GCS and the local mirror;
all artifacts are written under `ma_poc/data/reports/`.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GCS_BUCKET = "gs://jugnu-raw-production"
DEFAULT_LOCAL_MIRROR = Path("c:/tmp")
DEFAULT_OUT_ROOT = REPO_ROOT / "data" / "reports"

# Tier categorisation lifted from the May 8 patterns. Update when new
# adapter tiers are introduced.
PLATFORM_TIERS = {
    "TIER_1_API_ENTRATA": "entrata",
    "TIER_1_API_APPFOLIO": "appfolio",
    "TIER_1_API_ONESITE": "onesite",
    "TIER_1_API_AMLI_NEXT_DATA": "amli",
    "TIER_1_API_RENTCAFE": "rentcafe",
    "TIER_1_API_SIGHTMAP": "sightmap",
    "TIER_1_API_AVALONBAY": "avalonbay",
    "SYNDICATION_ONLY_SQUARESPACE": "squarespace",
    "SYNDICATION_ONLY_WIX": "wix",
}

# Outcomes that mean "we never got data". UNREACHABLE = pre-extraction termination,
# NO_DATA = page loaded but every tier failed.
TERMINAL_FAILURE_VERDICTS = {"FAILED_NO_DATA", "FAILED_UNREACHABLE"}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class PropertyOutcome:
    """One row per property per run, derived from events.jsonl."""

    property_id: str
    shard: str
    url: str | None = None
    final_url: str | None = None
    verdict: str | None = None  # SUCCESS / FAILED_NO_DATA / FAILED_UNREACHABLE / CARRY_FORWARD / PARTIAL
    units: int = 0
    pms_detected: str | None = None
    adapter_selected: str | None = None
    terminal_tier: str | None = None  # tier_won (success) or last tier_failed (fail)
    fetch_outcome: str | None = None
    fetch_error_signature: str | None = None
    fetch_status: int | None = None
    body_bytes: int | None = None
    captcha_detected: bool = False
    bot_blocked: bool = False
    llm_rescue_attempted: bool = False
    llm_rescue_succeeded: bool = False
    llm_cost: float = 0.0
    link_hops_attempted: int = 0
    link_hops_recovered: int = 0
    issue_codes: list[str] = field(default_factory=list)

    @property
    def domain(self) -> str | None:
        if not self.url:
            return None
        try:
            host = urlparse(self.url).hostname or ""
        except Exception:
            return None
        # Strip leading "www." and "lp." subdomains so per-PMC clusters collapse.
        for prefix in ("www.", "lp."):
            if host.startswith(prefix):
                host = host[len(prefix):]
                break
        return host or None

    @property
    def succeeded(self) -> bool:
        return self.verdict == "SUCCESS"

    @property
    def failed(self) -> bool:
        return self.verdict in TERMINAL_FAILURE_VERDICTS


@dataclass
class RunStats:
    """Run-level aggregates derived from per-shard report.json + events."""

    run_date: str
    shards_seen: list[str]
    shards_expected: int
    properties_total: int = 0
    properties_succeeded: int = 0
    properties_failed_no_data: int = 0
    properties_failed_unreachable: int = 0
    properties_failed_other: int = 0  # counted-failed by report.json with no output.property_emitted event
    success_rate_pct: float = 0.0
    llm_cost_total: float = 0.0
    slo_breaches: list[dict[str, Any]] = field(default_factory=list)
    tier_distribution: Counter = field(default_factory=Counter)
    fetch_signatures: Counter = field(default_factory=Counter)
    failure_terminal_tiers: Counter = field(default_factory=Counter)


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def gcs_pull_run(date: str, dest_root: Path) -> Path:
    """Mirror essential per-shard files for a date from GCS to local disk."""
    dest = dest_root / f"run-{date}"
    dest.mkdir(parents=True, exist_ok=True)
    src = f"{GCS_BUCKET}/runs/{date}/"
    print(f"[gcs] mirroring {src} -> {dest}")
    # Pull everything except per-property HTML dumps and the LLM raw payloads,
    # which can be huge and are not needed for aggregate analysis.
    cmd = [
        "gcloud", "storage", "rsync", "-r", src, str(dest),
        "--exclude", r".*\.html$",
        "--exclude", r".*/property_reports/.*",
    ]
    subprocess.run(cmd, check=True)
    return dest


def list_shard_dirs(run_dir: Path) -> list[Path]:
    return sorted(
        [p for p in run_dir.iterdir() if p.is_dir() and p.name.startswith("shard_")],
        key=lambda p: int(p.name.split("_", 1)[1]) if p.name.split("_", 1)[1].isdigit() else -1,
    )


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # Tolerate truncated final lines from crashed shards (Jugnu invariant).
            continue
    return out


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Per-shard parsing
# ---------------------------------------------------------------------------


def parse_shard(shard_dir: Path) -> tuple[dict[str, PropertyOutcome], dict[str, Any] | None]:
    """Return (per-property outcomes, run-summary dict-or-None) for a shard."""
    events = load_jsonl(shard_dir / "events.jsonl")
    issues = load_jsonl(shard_dir / "issues.jsonl")
    report = load_json(shard_dir / "report.json")

    outcomes: dict[str, PropertyOutcome] = {}

    def get(pid: str) -> PropertyOutcome:
        if pid not in outcomes:
            outcomes[pid] = PropertyOutcome(property_id=pid, shard=shard_dir.name)
        return outcomes[pid]

    for ev in events:
        pid = str(ev.get("property_id") or "")
        if not pid:
            continue
        o = get(pid)
        kind = ev.get("kind")

        if kind == "fetch.started":
            # First fetch_started is the initial entry-URL fetch.
            if not o.url:
                o.url = ev.get("url")
        elif kind == "fetch.completed":
            # Carry the *last* fetch outcome. Earlier ones are usually link-hops.
            if (ev.get("attempt") or 1) == 1 and o.fetch_outcome is None:
                o.fetch_outcome = ev.get("outcome")
                o.fetch_error_signature = ev.get("error_signature")
                o.fetch_status = ev.get("status")
                o.final_url = ev.get("final_url")
                o.body_bytes = ev.get("body_bytes")
                if ev.get("captcha_detected"):
                    o.captcha_detected = True
        elif kind == "fetch.bot_blocked":
            o.bot_blocked = True
        elif kind == "fetch.captcha_detected":
            o.captcha_detected = True
        elif kind == "extract.pms_detected":
            o.pms_detected = ev.get("pms")
        elif kind == "extract.adapter_selected":
            o.adapter_selected = ev.get("adapter")
        elif kind == "extract.tier_won":
            o.terminal_tier = ev.get("tier_used")
        elif kind == "extract.tier_failed":
            # Last tier_failed wins for failures (no tier_won will overwrite).
            o.terminal_tier = ev.get("tier_used") or o.terminal_tier
        elif kind == "extract.link_hop_started":
            o.link_hops_attempted += 1
        elif kind == "extract.link_hop_recovered":
            o.link_hops_recovered += 1
        elif kind == "extract.llm_rescue_attempted":
            o.llm_rescue_attempted = True
        elif kind == "extract.llm_rescue_succeeded":
            o.llm_rescue_succeeded = True
            o.llm_cost += float(ev.get("cost") or 0.0)
            # Promote the rescue tier so we record what actually won.
            if ev.get("tier"):
                o.terminal_tier = ev["tier"]
        elif kind == "extract.llm_rescue_failed":
            o.llm_cost += float(ev.get("cost") or 0.0)
        elif kind == "output.property_emitted":
            o.verdict = ev.get("verdict")
            o.units = int(ev.get("units") or 0)

    for issue in issues:
        pid = str(issue.get("canonical_id") or "")
        if pid in outcomes and issue.get("code"):
            outcomes[pid].issue_codes.append(issue["code"])

    return outcomes, report


# ---------------------------------------------------------------------------
# Run aggregation
# ---------------------------------------------------------------------------


def aggregate_run(run_dir: Path, run_date: str, expected_shards: int = 20) -> tuple[RunStats, dict[str, PropertyOutcome]]:
    shard_dirs = list_shard_dirs(run_dir)
    stats = RunStats(
        run_date=run_date,
        shards_seen=[p.name for p in shard_dirs],
        shards_expected=expected_shards,
    )
    all_outcomes: dict[str, PropertyOutcome] = {}

    for shard_dir in shard_dirs:
        outcomes, report = parse_shard(shard_dir)
        all_outcomes.update(outcomes)
        if report:
            totals = report.get("totals") or {}
            stats.properties_total += int(totals.get("properties") or 0)
            stats.properties_succeeded += int(totals.get("succeeded") or 0)
            failed = int(totals.get("failed") or 0)
            stats.llm_cost_total += float((report.get("cost") or {}).get("llm") or 0.0)
            for breach in report.get("slo_violations") or []:
                stats.slo_breaches.append({"shard": shard_dir.name, **breach})
            for tier, n in (report.get("tier_distribution") or {}).items():
                stats.tier_distribution[tier] += int(n)
            # Split failed bucket into NO_DATA vs UNREACHABLE using events later.
            del failed  # silence linter

    # Recompute NO_DATA / UNREACHABLE split from event-level verdicts so the
    # numbers reconcile with per-property categorisation. Anything counted as
    # failed by report.json but with no output.property_emitted event lands in
    # properties_failed_other (typically per-property timeout / crash kills).
    verdicts = Counter(o.verdict for o in all_outcomes.values())
    stats.properties_failed_no_data = verdicts.get("FAILED_NO_DATA", 0)
    stats.properties_failed_unreachable = verdicts.get("FAILED_UNREACHABLE", 0)
    stats.properties_failed_other = max(
        0,
        stats.properties_total
        - stats.properties_succeeded
        - stats.properties_failed_no_data
        - stats.properties_failed_unreachable,
    )

    if stats.properties_total:
        stats.success_rate_pct = round(
            100.0 * stats.properties_succeeded / stats.properties_total, 2
        )

    for o in all_outcomes.values():
        if o.fetch_outcome:
            stats.fetch_signatures[(o.fetch_outcome, o.fetch_error_signature or "")] += 1
        if o.failed and o.terminal_tier:
            stats.failure_terminal_tiers[o.terminal_tier] += 1
        elif o.failed and not o.terminal_tier:
            # Fully unreachable — never made it to extraction.
            stats.failure_terminal_tiers["__no_extraction__"] += 1

    return stats, all_outcomes


# ---------------------------------------------------------------------------
# Pattern categorisation
# ---------------------------------------------------------------------------


def categorise_failure(o: PropertyOutcome) -> tuple[str, str]:
    """Map a failed property to (pattern_id, sub_label).

    Pattern numbering matches the cloud_run_2026-05-08 reports:
      P2 — Cloudflare-blocked (fetch CF_CHALLENGE OR captcha) ending Entrata/unreachable
      P3 — Generic TIER_1_API failure (no platform adapter)
      P4 — Entrata adapter failure that is NOT CF-blocked
      P6 — Platform-specific adapter zero (AppFolio/OneSite/AMLI/Squarespace/Wix/etc)
      P7 — Pure unreachable (verdict FAILED_UNREACHABLE) not already caught by P2
      P8 — LLM_GATE_NO_BODY (extract gate decided no body to feed)
      P10 — Quality warning UNITS_KEYLESS_HIGH (info, not failure — emitted on success too)
      Pother — anything else
    """
    cf_blocked = (
        o.fetch_error_signature == "CF_CHALLENGE"
        or o.captcha_detected
        or o.bot_blocked
    )
    tier = o.terminal_tier or ""

    if tier == "LLM_GATE_NO_BODY" or "LLM_GATE_NO_BODY" in tier:
        return ("P8", "llm_gate_no_body")

    if cf_blocked and (tier == "TIER_1_API_ENTRATA" or "no_body" in tier or o.verdict == "FAILED_UNREACHABLE"):
        return ("P2", "cloudflare_entrata")

    if tier in PLATFORM_TIERS:
        platform = PLATFORM_TIERS[tier]
        if platform == "entrata":
            return ("P4", "entrata_adapter_no_cf")
        return ("P6", f"platform_{platform}")

    if "no_body_short_circuit" in tier or o.verdict == "FAILED_UNREACHABLE":
        return ("P7", "unreachable")

    if tier == "TIER_1_API" or tier.startswith("TIER_1_API"):
        return ("P3", "generic_tier1_api")

    return ("Pother", tier or "unknown")


def cluster_by_domain(failures: list[PropertyOutcome], top_n: int = 25) -> list[tuple[str, int]]:
    counts: Counter = Counter()
    for o in failures:
        d = o.domain or "(empty/blank URL)"
        counts[d] += 1
    return counts.most_common(top_n)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_summary_md(stats: RunStats, outcomes: dict[str, PropertyOutcome]) -> str:
    failed = [o for o in outcomes.values() if o.failed]
    succeeded = [o for o in outcomes.values() if o.succeeded]

    pattern_counts: Counter = Counter()
    for o in failed:
        pid, _ = categorise_failure(o)
        pattern_counts[pid] += 1

    lines = [
        f"# Cloud run analysis — {stats.run_date}",
        "",
        f"**Generated:** {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"**Source:** `{GCS_BUCKET}/runs/{stats.run_date}/`",
        f"**Shards seen:** {len(stats.shards_seen)} / {stats.shards_expected}",
        "",
        "## Top-line numbers",
        "",
        f"- **Properties processed:** {stats.properties_total}",
        f"- **SUCCESS:** {stats.properties_succeeded} ({stats.success_rate_pct}%)",
        f"- **FAILED_NO_DATA:** {stats.properties_failed_no_data}",
        f"- **FAILED_UNREACHABLE:** {stats.properties_failed_unreachable}",
        f"- **FAILED (no `output.property_emitted` — likely killed by per-property timeout):** {stats.properties_failed_other}",
        f"- **LLM cost:** ${stats.llm_cost_total:.2f}",
        f"- **SLO breaches:** {len(stats.slo_breaches)} across all shards",
        "",
    ]

    if not stats.shards_seen or len(stats.shards_seen) < stats.shards_expected:
        missing = sorted(
            f"shard_{i}" for i in range(stats.shards_expected)
            if f"shard_{i}" not in stats.shards_seen
        )
        lines += [
            f"> **Missing shards:** {', '.join(missing)}. Investigate dispatcher logs in `ma_poc/scripts/runners/dispatcher.py`.",
            "",
        ]

    lines += [
        "## Failure breakdown by terminal tier",
        "",
        "| # | Terminal tier | Count | % of failures |",
        "|---|---|---|---|",
    ]
    total_failed = max(1, len(failed))
    for i, (tier, n) in enumerate(stats.failure_terminal_tiers.most_common(), start=1):
        pct = 100.0 * n / total_failed
        lines.append(f"| {i} | `{tier}` | {n} | {pct:.1f}% |")

    lines += [
        "",
        "## Fetch-side error signatures (first attempt per property)",
        "",
        "| Outcome | Signature | Count |",
        "|---|---|---|",
    ]
    for (outcome, sig), n in stats.fetch_signatures.most_common():
        lines.append(f"| {outcome} | {sig or '(none)'} | {n} |")

    lines += [
        "",
        "## Tier distribution (succeed + fail)",
        "",
        "| Tier | Count |",
        "|---|---|",
    ]
    for tier, n in stats.tier_distribution.most_common():
        lines.append(f"| `{tier}` | {n} |")

    lines += [
        "",
        "## Failure pattern distribution",
        "",
        "| Pattern | Failures | What it means |",
        "|---|---|---|",
        f"| P2 — Cloudflare on Entrata-style sites | {pattern_counts.get('P2', 0)} | CF challenge / captcha; rescue path doesn't fire |",
        f"| P3 — Generic `TIER_1_API` (no PMS adapter) | {pattern_counts.get('P3', 0)} | Cluster by management-company domain |",
        f"| P4 — Entrata adapter failure (non-CF) | {pattern_counts.get('P4', 0)} | Real adapter bug, not a fetch problem |",
        f"| P6 — Platform-specific adapter zero | {pattern_counts.get('P6', 0)} | AppFolio / OneSite / AMLI / Squarespace / Wix |",
        f"| P7 — Pure unreachable | {pattern_counts.get('P7', 0)} | `FAILED_UNREACHABLE` not already in P2 |",
        f"| P8 — LLM gate refused body | {pattern_counts.get('P8', 0)} | `LLM_GATE_NO_BODY` terminal |",
        f"| Pother | {pattern_counts.get('Pother', 0)} | Anything else |",
        "",
    ]

    if stats.slo_breaches:
        lines += [
            "## SLO breaches",
            "",
            "| Shard | Metric | Threshold | Observed |",
            "|---|---|---|---|",
        ]
        for b in stats.slo_breaches[:40]:
            lines.append(
                f"| {b.get('shard')} | {b.get('name')} | {b.get('threshold')} | {b.get('observed')} |"
            )
        if len(stats.slo_breaches) > 40:
            lines.append(f"| ... | _{len(stats.slo_breaches) - 40} more_ | | |")
        lines.append("")

    # Cluster Pattern 3 by domain — most actionable output.
    p3 = [o for o in failed if categorise_failure(o)[0] == "P3"]
    if p3:
        lines += [
            "## Pattern 3 — Generic TIER_1_API failures by management-company domain",
            "",
            "| Domain | Failures |",
            "|---|---|",
        ]
        for domain, n in cluster_by_domain(p3, top_n=25):
            lines.append(f"| {domain} | {n} |")
        lines.append("")

    # Pattern 6 split by sub-platform.
    p6 = [o for o in failed if categorise_failure(o)[0] == "P6"]
    if p6:
        sub = Counter(categorise_failure(o)[1] for o in p6)
        lines += [
            "## Pattern 6 — Platform-specific adapter failures",
            "",
            "| Platform | Failures |",
            "|---|---|",
        ]
        for plat, n in sub.most_common():
            lines.append(f"| {plat} | {n} |")
        lines.append("")

    # P10 quality warnings — these are signals on successful properties too.
    keyless = [o for o in outcomes.values() if "UNITS_KEYLESS_HIGH" in o.issue_codes]
    if keyless:
        lines += [
            f"## Pattern 10 — UNITS_KEYLESS_HIGH warnings: {len(keyless)} properties",
            "",
            "Quality warnings (not failures). Indicates LLM-extracted units lacked a natural identity anchor.",
            "",
        ]

    return "\n".join(lines) + "\n"


def render_failures_csv(outcomes: dict[str, PropertyOutcome], out_path: Path) -> None:
    rows = []
    for o in outcomes.values():
        if not o.failed:
            continue
        pattern_id, sub = categorise_failure(o)
        rows.append({
            "property_id": o.property_id,
            "shard": o.shard,
            "domain": o.domain or "",
            "url": o.url or "",
            "verdict": o.verdict or "",
            "terminal_tier": o.terminal_tier or "",
            "pms_detected": o.pms_detected or "",
            "adapter_selected": o.adapter_selected or "",
            "fetch_outcome": o.fetch_outcome or "",
            "fetch_error_signature": o.fetch_error_signature or "",
            "fetch_status": o.fetch_status or "",
            "body_bytes": o.body_bytes or 0,
            "captcha_detected": o.captcha_detected,
            "bot_blocked": o.bot_blocked,
            "llm_rescue_attempted": o.llm_rescue_attempted,
            "llm_rescue_succeeded": o.llm_rescue_succeeded,
            "llm_cost": round(o.llm_cost, 5),
            "link_hops_attempted": o.link_hops_attempted,
            "link_hops_recovered": o.link_hops_recovered,
            "issue_codes": "|".join(o.issue_codes),
            "pattern_id": pattern_id,
            "pattern_sub": sub,
            "final_url": o.final_url or "",
        })
    rows.sort(key=lambda r: (r["pattern_id"], r["domain"], r["property_id"]))

    if not rows:
        out_path.write_text("", encoding="utf-8")
        return

    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Day-over-day diff
# ---------------------------------------------------------------------------


def render_comparison_md(
    today_stats: RunStats,
    today_outcomes: dict[str, PropertyOutcome],
    prev_stats: RunStats,
    prev_outcomes: dict[str, PropertyOutcome],
) -> str:
    today_pids = set(today_outcomes)
    prev_pids = set(prev_outcomes)

    succ_today = {p for p, o in today_outcomes.items() if o.succeeded}
    succ_prev = {p for p, o in prev_outcomes.items() if o.succeeded}
    fail_today = {p for p, o in today_outcomes.items() if o.failed}
    fail_prev = {p for p, o in prev_outcomes.items() if o.failed}

    regressions = sorted(succ_prev & fail_today)
    recoveries = sorted(fail_prev & succ_today)
    repeat_failures = sorted(fail_prev & fail_today)
    new_failures = sorted(fail_today - prev_pids)
    dropped_from_run = sorted(prev_pids - today_pids)
    new_in_run = sorted(today_pids - prev_pids)

    lines = [
        f"# Cloud run comparison — {today_stats.run_date} vs {prev_stats.run_date}",
        "",
        f"**Generated:** {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "## Top-line delta",
        "",
        "| Metric | Today | Yesterday | Δ |",
        "|---|---|---|---|",
        f"| Properties processed | {today_stats.properties_total} | {prev_stats.properties_total} | {today_stats.properties_total - prev_stats.properties_total:+d} |",
        f"| Succeeded | {today_stats.properties_succeeded} | {prev_stats.properties_succeeded} | {today_stats.properties_succeeded - prev_stats.properties_succeeded:+d} |",
        f"| Failed (no data) | {today_stats.properties_failed_no_data} | {prev_stats.properties_failed_no_data} | {today_stats.properties_failed_no_data - prev_stats.properties_failed_no_data:+d} |",
        f"| Failed (unreachable) | {today_stats.properties_failed_unreachable} | {prev_stats.properties_failed_unreachable} | {today_stats.properties_failed_unreachable - prev_stats.properties_failed_unreachable:+d} |",
        f"| Failed (no emit — probable timeout kill) | {today_stats.properties_failed_other} | {prev_stats.properties_failed_other} | {today_stats.properties_failed_other - prev_stats.properties_failed_other:+d} |",
        f"| Success rate | {today_stats.success_rate_pct}% | {prev_stats.success_rate_pct}% | {today_stats.success_rate_pct - prev_stats.success_rate_pct:+.2f} pp |",
        f"| LLM cost (run) | ${today_stats.llm_cost_total:.2f} | ${prev_stats.llm_cost_total:.2f} | ${today_stats.llm_cost_total - prev_stats.llm_cost_total:+.2f} |",
        f"| Shards seen | {len(today_stats.shards_seen)} | {len(prev_stats.shards_seen)} | {len(today_stats.shards_seen) - len(prev_stats.shards_seen):+d} |",
        "",
        "## Failure-membership flow",
        "",
        f"- **Regressions** (passed yesterday → failed today): **{len(regressions)}**",
        f"- **Recoveries** (failed yesterday → passed today): **{len(recoveries)}**",
        f"- **Repeat failures** (failed both days): **{len(repeat_failures)}**",
        f"- **New failures** (not in yesterday's run, failed today): **{len(new_failures)}**",
        f"- **Dropped from run** (in yesterday, missing today): **{len(dropped_from_run)}**",
        f"- **New in run** (in today, missing yesterday): **{len(new_in_run)}**",
        "",
    ]

    # Tier distribution shift.
    lines += [
        "## Tier distribution shift",
        "",
        "| Tier | Today | Yesterday | Δ |",
        "|---|---|---|---|",
    ]
    all_tiers = set(today_stats.tier_distribution) | set(prev_stats.tier_distribution)
    rows: list[tuple[str, int, int, int]] = []
    for t in all_tiers:
        a = today_stats.tier_distribution.get(t, 0)
        b = prev_stats.tier_distribution.get(t, 0)
        rows.append((t, a, b, a - b))
    rows.sort(key=lambda r: -abs(r[3]))
    for tier, a, b, d in rows[:30]:
        lines.append(f"| `{tier}` | {a} | {b} | {d:+d} |")

    # Per-pattern shift.
    today_patterns = Counter(categorise_failure(today_outcomes[p])[0] for p in fail_today)
    prev_patterns = Counter(categorise_failure(prev_outcomes[p])[0] for p in fail_prev)
    lines += [
        "",
        "## Failure-pattern shift",
        "",
        "| Pattern | Today | Yesterday | Δ |",
        "|---|---|---|---|",
    ]
    for pid in sorted(set(today_patterns) | set(prev_patterns)):
        a = today_patterns.get(pid, 0)
        b = prev_patterns.get(pid, 0)
        lines.append(f"| {pid} | {a} | {b} | {a - b:+d} |")

    # Regression detail (capped at 50).
    if regressions:
        lines += [
            "",
            f"## Regressions (sample, up to 50 of {len(regressions)})",
            "",
            "| property_id | domain | yesterday tier | today terminal tier | today pattern |",
            "|---|---|---|---|---|",
        ]
        for pid in regressions[:50]:
            t = today_outcomes[pid]
            y = prev_outcomes[pid]
            pat, _sub = categorise_failure(t)
            lines.append(
                f"| {pid} | {t.domain or ''} | `{y.terminal_tier or ''}` | `{t.terminal_tier or ''}` | {pat} |"
            )

    if recoveries:
        lines += [
            "",
            f"## Recoveries (sample, up to 50 of {len(recoveries)})",
            "",
            "| property_id | domain | yesterday terminal tier | today won tier |",
            "|---|---|---|---|",
        ]
        for pid in recoveries[:50]:
            t = today_outcomes[pid]
            y = prev_outcomes[pid]
            lines.append(
                f"| {pid} | {t.domain or ''} | `{y.terminal_tier or ''}` | `{t.terminal_tier or ''}` |"
            )

    # Repeat-failure cluster — these are the hard ones to fix.
    if repeat_failures:
        domains = Counter(today_outcomes[p].domain or "(blank)" for p in repeat_failures)
        lines += [
            "",
            f"## Repeat-failure top domains ({len(repeat_failures)} total properties)",
            "",
            "| Domain | Count |",
            "|---|---|",
        ]
        for d, n in domains.most_common(20):
            lines.append(f"| {d} | {n} |")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def analyse_date(
    date: str,
    *,
    pull: bool,
    local_mirror_root: Path,
    expected_shards: int,
) -> tuple[RunStats, dict[str, PropertyOutcome], Path]:
    run_dir = local_mirror_root / f"run-{date}"
    if pull or not run_dir.exists() or not list_shard_dirs(run_dir):
        gcs_pull_run(date, local_mirror_root)
    stats, outcomes = aggregate_run(run_dir, date, expected_shards=expected_shards)
    return stats, outcomes, run_dir


def write_outputs(
    stats: RunStats,
    outcomes: dict[str, PropertyOutcome],
    out_dir: Path,
    *,
    comparison: tuple[RunStats, dict[str, PropertyOutcome]] | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.md").write_text(
        render_summary_md(stats, outcomes), encoding="utf-8"
    )
    render_failures_csv(outcomes, out_dir / "failures.csv")

    # Stable JSON for downstream automation / future deltas.
    payload = {
        "run_date": stats.run_date,
        "shards_seen": stats.shards_seen,
        "totals": {
            "properties": stats.properties_total,
            "succeeded": stats.properties_succeeded,
            "failed_no_data": stats.properties_failed_no_data,
            "failed_unreachable": stats.properties_failed_unreachable,
            "failed_other": stats.properties_failed_other,
            "success_rate_pct": stats.success_rate_pct,
        },
        "llm_cost_total": stats.llm_cost_total,
        "tier_distribution": dict(stats.tier_distribution),
        "failure_terminal_tiers": dict(stats.failure_terminal_tiers),
        "fetch_signatures": [
            {"outcome": k[0], "signature": k[1], "count": v}
            for k, v in stats.fetch_signatures.items()
        ],
        "slo_breaches": stats.slo_breaches,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )

    if comparison is not None:
        prev_stats, prev_outcomes = comparison
        comp_md = render_comparison_md(stats, outcomes, prev_stats, prev_outcomes)
        (out_dir / f"comparison_with_{prev_stats.run_date}.md").write_text(
            comp_md, encoding="utf-8"
        )

    # INDEX.md is bootstrapped on the first run and preserved thereafter so
    # human-authored narrative additions (e.g. REGRESSION_ROOT_CAUSE.md links)
    # are not clobbered by re-runs.
    index_path = out_dir / "INDEX.md"
    if not index_path.exists():
        index_lines = [
            f"# Cloud run {stats.run_date} — analysis index",
            "",
            f"Run directory mirror: `c:/tmp/run-{stats.run_date}/`",
            "",
            "## Reports",
            "",
            "- [summary.md](summary.md) — top-line metrics, fetch signatures, tier distribution, pattern breakdown",
            "- [summary.json](summary.json) — same data in machine-readable form",
            "- [failures.csv](failures.csv) — flat per-property failure table with pattern_id",
        ]
        if comparison is not None:
            prev_date = comparison[0].run_date
            index_lines.append(
                f"- [comparison_with_{prev_date}.md](comparison_with_{prev_date}.md) — day-over-day diff"
            )
        index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", required=True, help="Run date YYYY-MM-DD")
    p.add_argument("--compare-date", default=None, help="Optional prior date for diff")
    p.add_argument("--pull", action="store_true", help="Force re-pull from GCS")
    p.add_argument(
        "--local-mirror",
        default=str(DEFAULT_LOCAL_MIRROR),
        help=f"Where per-date mirrors live (default {DEFAULT_LOCAL_MIRROR})",
    )
    p.add_argument(
        "--out-dir",
        default=None,
        help="Override output dir (default ma_poc/data/reports/cloud_run_{date}/)",
    )
    p.add_argument(
        "--expected-shards",
        type=int,
        default=20,
        help="Expected shard count (for missing-shard detection)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    local_root = Path(args.local_mirror)

    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_OUT_ROOT / f"cloud_run_{args.date}"

    stats, outcomes, _ = analyse_date(
        args.date,
        pull=args.pull,
        local_mirror_root=local_root,
        expected_shards=args.expected_shards,
    )

    comparison = None
    if args.compare_date:
        prev_stats, prev_outcomes, _ = analyse_date(
            args.compare_date,
            pull=False,
            local_mirror_root=local_root,
            expected_shards=args.expected_shards,
        )
        comparison = (prev_stats, prev_outcomes)

    write_outputs(stats, outcomes, out_dir, comparison=comparison)

    print(f"[ok] {args.date}: {stats.properties_succeeded}/{stats.properties_total} succeeded "
          f"({stats.success_rate_pct}%); LLM ${stats.llm_cost_total:.2f}")
    print(f"[ok] reports written to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
