#!/usr/bin/env python3
"""Render a self-contained HTML daily report for a scraping run, sourced from
the data-provider DB (Postgres / SQLite / FS — whatever the env points to).

Reads from the data-provider rather than walking ``data/runs/{date}/`` files
directly so the same script works against the cloud-mirrored Postgres DB
and the local filesystem store transparently.

Output is a single HTML file with:
  - At-a-glance KPIs (properties, success rate, units, cost, runtime)
  - SLO panel with observed-vs-threshold rows
  - 7-run trend panel (success rate / units / LLM cost / failed count)
  - Tier distribution bars
  - Issues breakdown by severity + top codes
  - State diff (new / updated / unchanged / disappeared / carry-forward)
  - Cost breakdown including top-spend properties
  - Rent insights (median / avg / by bedroom count / histogram)
  - Availability snapshot (% with date, days-until-move-in distribution)
  - Profile maturity distribution (COLD / WARM / HOT)
  - Failed properties table
  - Per-property panels with metadata, tier, units, errors

Usage:
    # Today's run
    python scripts/generate_daily_report.py

    # Specific run date
    python scripts/generate_daily_report.py --run-date 2026-05-01

    # Custom output path
    python scripts/generate_daily_report.py --run-date 2026-05-01 \
        --out data/runs/2026-05-01/daily_report.html

    # Override DB URL (otherwise picks up DATABASE_URL / DATA_PROVIDER from env)
    python scripts/generate_daily_report.py \
        --database-url postgresql+pg8000://user:pass@localhost:5432/proppy
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MA_POC_ROOT = Path(__file__).resolve().parent.parent.parent
_REPO_ROOT = _MA_POC_ROOT.parent
# `ma_poc/` first so bare `import data_provider` resolves; the repo root
# is also needed because some modules (e.g. models.scrape_profile) reach
# back out via `from ma_poc.models...`.
for _p in (_MA_POC_ROOT, _REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from sqlalchemy import func, select  # noqa: E402

from data_provider.contracts import DataProvider  # noqa: E402
from data_provider.factory import get_data_provider, reset_cached_provider  # noqa: E402
from data_provider.sql.models import (  # noqa: E402
    DlqEntryRow,
    PropertyRow,
    ScrapeProfileRow,
    UnitRow,
)

log = logging.getLogger("generate_daily_report")


# ---------------------------------------------------------------------------
# DB access — pulls everything the report needs in one place so the rest of
# the file is pure rendering.
# ---------------------------------------------------------------------------


def _normalize_database_url(url: str | None) -> str | None:
    """Translate `+asyncpg` URLs to `+pg8000` so the sync engine works.

    The repo's `.env` ships with `postgresql+asyncpg://...` (used by the TS
    API layer); the data-provider's sync engine can't drive asyncpg, but the
    same DB is reachable via pg8000 with no other change required. This is
    the same translation the local dev workflow does manually.
    """
    if not url:
        return url
    if "+asyncpg" in url:
        return url.replace("+asyncpg", "+pg8000")
    return url


def _ensure_provider(database_url: str | None) -> DataProvider:
    if database_url:
        os.environ["DATABASE_URL"] = database_url
    else:
        env_url = os.environ.get("DATABASE_URL")
        normalized = _normalize_database_url(env_url)
        if normalized and normalized != env_url:
            os.environ["DATABASE_URL"] = normalized
    reset_cached_provider()
    return get_data_provider()


def _get_holder(provider: DataProvider) -> Any:
    """Return the SQL session holder if this provider is SQL-backed.

    Used for the rent / availability / profile aggregates that benefit from
    SQL group-bys; falls back to None for the FS provider so the script can
    still produce a report (just without those panels).
    """
    runs = getattr(provider, "runs", None)
    return getattr(runs, "_h", None)


def _safe_count(holder: Any, stmt: Any) -> int:
    if holder is None:
        return 0
    try:
        with holder.scope() as s:
            return int(s.execute(stmt).scalar() or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("count query failed: %s", e)
        return 0


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------


def collect_run_data(provider: DataProvider, run_date: str, *, trend_window: int = 7) -> dict[str, Any]:
    """Pull every artifact for ``run_date`` plus N prior runs for trends."""

    runs = provider.runs.list_runs()
    if run_date not in runs:
        raise SystemExit(
            f"run_date={run_date!r} not in DB. Available: {runs[-10:] if runs else 'none'}"
        )

    report = provider.runs.read_report(run_date)
    if report is None:
        raise SystemExit(f"No run_report for {run_date} — was the run report ever written?")

    properties = provider.runs.read_properties(run_date)
    issues = list(provider.runs.read_issues(run_date))
    ledger = list(provider.runs.read_ledger(run_date))

    # Build a map of canonical_id → ledger entry for per-property lookup.
    ledger_by_cid: dict[str, Any] = {}
    for entry in ledger:
        if entry.canonical_id:
            ledger_by_cid[entry.canonical_id] = entry

    # Issues bucketed by canonical_id for the per-property panels.
    issues_by_cid: dict[str, list[Any]] = defaultdict(list)
    for issue in issues:
        if issue.canonical_id:
            issues_by_cid[issue.canonical_id].append(issue)

    # Trend: last N runs ending at run_date (inclusive).
    if run_date in runs:
        idx = runs.index(run_date)
        trend_run_dates = runs[max(0, idx - trend_window + 1) : idx + 1]
    else:
        trend_run_dates = runs[-trend_window:]
    trend_reports: list[tuple[str, Any]] = []
    for rd in trend_run_dates:
        r = provider.runs.read_report(rd)
        if r is not None:
            trend_reports.append((rd, r))

    # SQL-backed aggregates (rent / availability / profiles / DLQ).
    holder = _get_holder(provider)
    db_aggs = collect_db_aggregates(holder)

    return {
        "run_date": run_date,
        "report": report,
        "properties": properties,
        "issues": issues,
        "ledger": ledger,
        "ledger_by_cid": ledger_by_cid,
        "issues_by_cid": issues_by_cid,
        "trend": trend_reports,
        "db": db_aggs,
        "provider_name": provider.name,
    }


def collect_db_aggregates(holder: Any) -> dict[str, Any]:
    """Cross-cutting current-state stats not stored in the per-run report."""
    out: dict[str, Any] = {
        "properties_total": 0,
        "units_total": 0,
        "units_with_rent": 0,
        "units_with_avail": 0,
        "units_disappeared": 0,
        "rent_median": None,
        "rent_avg": None,
        "rent_p10": None,
        "rent_p90": None,
        "rent_by_beds": [],
        "rent_histogram": [],
        "avail_buckets": [],
        "profile_maturity": {},
        "dlq_total": 0,
        "dlq_top_reasons": [],
    }
    if holder is None:
        return out

    try:
        with holder.scope() as s:
            out["properties_total"] = int(s.execute(select(func.count(PropertyRow.canonical_id))).scalar() or 0)
            out["units_total"] = int(s.execute(select(func.count(UnitRow.unit_id))).scalar() or 0)
            out["units_with_rent"] = int(
                s.execute(select(func.count(UnitRow.unit_id)).where(UnitRow.rent_low.isnot(None))).scalar() or 0
            )
            out["units_with_avail"] = int(
                s.execute(select(func.count(UnitRow.unit_id)).where(UnitRow.available_date.isnot(None))).scalar() or 0
            )
            out["units_disappeared"] = int(
                s.execute(select(func.count(UnitRow.unit_id)).where(UnitRow.disappeared_since.isnot(None))).scalar()
                or 0
            )

            # Rent stats — pull a sample for histogram + percentiles. SQLite
            # lacks percentile_cont so we compute in Python from the sample.
            rents: list[float] = (
                s.execute(
                    select(UnitRow.rent_low).where(
                        UnitRow.rent_low.isnot(None),
                        UnitRow.rent_low > 200,
                        UnitRow.rent_low < 50000,
                        UnitRow.disappeared_since.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            rents_f = sorted(float(r) for r in rents if r is not None)
            if rents_f:
                out["rent_median"] = statistics.median(rents_f)
                out["rent_avg"] = statistics.fmean(rents_f)
                out["rent_p10"] = rents_f[int(len(rents_f) * 0.10)]
                out["rent_p90"] = rents_f[min(len(rents_f) - 1, int(len(rents_f) * 0.90))]
                out["rent_histogram"] = _build_histogram(rents_f, bin_count=20)

            # Rent by bedroom count.
            beds_rows = s.execute(
                select(
                    UnitRow.beds,
                    func.count(UnitRow.unit_id),
                    func.avg(UnitRow.rent_low),
                )
                .where(
                    UnitRow.rent_low.isnot(None),
                    UnitRow.rent_low > 200,
                    UnitRow.disappeared_since.is_(None),
                )
                .group_by(UnitRow.beds)
                .order_by(UnitRow.beds.asc().nulls_last())
            ).all()
            out["rent_by_beds"] = [
                {"beds": row[0], "count": int(row[1] or 0), "avg_rent": float(row[2] or 0)}
                for row in beds_rows
            ]

            # Days-until-move-in buckets.
            avail_rows = (
                s.execute(
                    select(UnitRow.available_date).where(
                        UnitRow.available_date.isnot(None),
                        UnitRow.disappeared_since.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            out["avail_buckets"] = _bucket_days_until(avail_rows)

            # Profile maturity (parses the JSON payload — small enough to be
            # cheap on a few thousand rows).
            payloads = s.execute(select(ScrapeProfileRow.payload)).scalars().all()
            mat: Counter[str] = Counter()
            for payload in payloads:
                if not isinstance(payload, dict):
                    continue
                conf = (payload.get("confidence") or {})
                m = conf.get("maturity") or "UNKNOWN"
                mat[str(m)] += 1
            out["profile_maturity"] = dict(mat)

            # DLQ stats.
            out["dlq_total"] = int(s.execute(select(func.count(DlqEntryRow.property_id))).scalar() or 0)
            dlq_rows = s.execute(
                select(DlqEntryRow.reason, func.count(DlqEntryRow.property_id))
                .group_by(DlqEntryRow.reason)
                .order_by(func.count(DlqEntryRow.property_id).desc())
                .limit(10)
            ).all()
            out["dlq_top_reasons"] = [(row[0], int(row[1])) for row in dlq_rows]
    except Exception as e:  # noqa: BLE001
        log.warning("DB aggregate query failed: %s — panels will be empty", e)

    return out


def _build_histogram(values: list[float], bin_count: int = 20) -> list[dict[str, Any]]:
    if not values:
        return []
    lo, hi = values[0], values[-1]
    if lo == hi:
        return [{"lo": lo, "hi": hi, "count": len(values)}]
    bin_w = (hi - lo) / bin_count
    bins: list[dict[str, Any]] = [
        {"lo": lo + i * bin_w, "hi": lo + (i + 1) * bin_w, "count": 0} for i in range(bin_count)
    ]
    for v in values:
        idx = min(int((v - lo) / bin_w), bin_count - 1)
        bins[idx]["count"] += 1
    return bins


def _bucket_days_until(date_strings: list[str | None]) -> list[dict[str, Any]]:
    """Group available_date values into 'days until move-in' buckets."""
    today = datetime.now(timezone.utc).date()
    buckets = [
        ("now", 0, 0),
        ("≤7d", 1, 7),
        ("8–30d", 8, 30),
        ("31–60d", 31, 60),
        ("61–90d", 61, 90),
        ("90d+", 91, 10**6),
        ("past", -10**6, -1),
    ]
    counts = {label: 0 for label, _, _ in buckets}
    for raw in date_strings:
        if not raw:
            continue
        try:
            d = datetime.fromisoformat(str(raw)[:10]).date()
        except (ValueError, TypeError):
            continue
        delta = (d - today).days
        for label, lo, hi in buckets:
            if lo <= delta <= hi:
                counts[label] += 1
                break
    return [{"label": label, "count": counts[label]} for label, _, _ in buckets]


# ---------------------------------------------------------------------------
# HTML escape + small builders (centralized so unsafe strings can't break layout)
# ---------------------------------------------------------------------------


def E(s: Any) -> str:
    return html.escape(str(s) if s is not None else "")


def details(summary: str, body: str, *, open_: bool = False, cls: str = "") -> str:
    o = " open" if open_ else ""
    c = f' class="{cls}"' if cls else ""
    return f"<details{o}{c}><summary>{summary}</summary>{body}</details>"


def kv_table(rows: list[tuple[str, Any]]) -> str:
    out = ['<table class="kv">']
    for k, v in rows:
        out.append(f"<tr><th>{E(k)}</th><td>{v if isinstance(v, str) and v.startswith('<') else E(v)}</td></tr>")
    out.append("</table>")
    return "".join(out)


def bar_row(label: str, value: int, total: int, *, color: str = "#3a7bd5") -> str:
    pct = (100.0 * value / total) if total else 0.0
    return (
        f'<tr><td class="lbl">{E(label)}</td>'
        f'<td class="val">{E(value)}</td>'
        f'<td class="bar"><div style="width:{pct:.1f}%;background:{color}"></div></td>'
        f'<td class="pct">{pct:.1f}%</td></tr>'
    )


def fmt_int(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except (ValueError, TypeError):
        return str(n)


def fmt_money(n: Any) -> str:
    try:
        return f"${float(n):,.4f}"
    except (ValueError, TypeError):
        return "$0.0000"


def fmt_pct(n: Any) -> str:
    try:
        return f"{float(n):.1f}%"
    except (ValueError, TypeError):
        return "—"


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def render_header(data: dict[str, Any]) -> str:
    report = data["report"]
    body = report.model_dump(mode="json")
    started = body.get("started_at") or body.get("generated_at") or ""
    finished = body.get("finished_at") or body.get("generated_at") or ""
    duration = body.get("duration_s")
    exit_status = body.get("exit_status") or "—"

    totals = report.totals.model_dump()
    success_rate = float(totals.get("success_rate_pct") or 0)
    pass_class = "ok" if success_rate >= 95 else ("warn" if success_rate >= 80 else "fail")

    return (
        f'<header class="hdr">'
        f"<h1>Daily scrape report — {E(data['run_date'])}</h1>"
        f'<div class="hdr-meta">'
        f'<span class="kpi"><b>{fmt_int(totals.get("properties"))}</b> properties</span>'
        f'<span class="kpi {pass_class}"><b>{fmt_pct(success_rate)}</b> success</span>'
        f'<span class="kpi"><b>{fmt_int(totals.get("succeeded"))}</b> ok</span>'
        f'<span class="kpi"><b>{fmt_int(totals.get("failed"))}</b> failed</span>'
        f'<span class="kpi"><b>{fmt_int(totals.get("carry_forward"))}</b> carry-fwd</span>'
        f'<span class="kpi"><b>{fmt_money((report.cost or {}).get("llm"))}</b> LLM</span>'
        f"</div>"
        f'<div class="hdr-meta sub">'
        f'<span>started {E(started)}</span>'
        f'<span>finished {E(finished)}</span>'
        f'<span>duration {E(f"{duration:.1f}s") if isinstance(duration, (int, float)) else "—"}</span>'
        f'<span>exit <code>{E(exit_status)}</code></span>'
        f'<span>provider <code>{E(data["provider_name"])}</code></span>'
        f"</div>"
        f"</header>"
    )


def render_slo(data: dict[str, Any]) -> str:
    violations = data["report"].slo_violations or []
    if not violations:
        return '<div class="slo-ok">All SLOs within threshold ✓</div>'

    rows = []
    for v in violations:
        name = v.get("name", "?")
        thr = v.get("threshold", "?")
        obs = v.get("observed", "?")
        breach = "▲ breach"
        try:
            obs_f = float(obs)
            thr_f = float(thr)
            # Lower-is-better for cost SLOs, higher-is-better for success_rate.
            if "rate" in name and obs_f >= thr_f:
                breach = "ok"
            elif "cost" in name and obs_f <= thr_f:
                breach = "ok"
        except (ValueError, TypeError):
            pass
        cls = "ok" if breach == "ok" else "fail"
        rows.append(
            f"<tr class={cls!r}>"
            f"<td>{E(name)}</td>"
            f"<td>{E(thr)}</td>"
            f"<td>{E(obs)}</td>"
            f"<td>{breach}</td></tr>"
        )

    return (
        '<table class="slo"><thead><tr>'
        "<th>SLO</th><th>threshold</th><th>observed</th><th>state</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_trend(data: dict[str, Any]) -> str:
    """Mini bar charts for the last N runs — success%, units, LLM cost, failed."""
    trend = data["trend"]
    if not trend:
        return "<p><em>No trend data.</em></p>"

    # Extract series.
    series_success: list[tuple[str, float]] = []
    series_units: list[tuple[str, int]] = []
    series_cost: list[tuple[str, float]] = []
    series_failed: list[tuple[str, int]] = []
    for rd, r in trend:
        body = r.model_dump(mode="json")
        totals = body.get("totals") or {}
        series_success.append((rd, float(totals.get("success_rate_pct") or 0)))
        series_failed.append((rd, int(totals.get("failed") or 0)))
        # Units extracted is in extra body or state_diff if Jugnu — fall back to ledger sum.
        units = body.get("units_extracted")
        if units is None:
            sd = body.get("state_diff") or {}
            units = sd.get("units_extracted")
        series_units.append((rd, int(units or 0)))
        cost = (body.get("cost") or {}).get("llm") or 0
        series_cost.append((rd, float(cost)))

    def render_series(title: str, series: list[tuple[str, float]], fmt: str, color: str) -> str:
        max_v = max((v for _, v in series), default=0) or 1
        bars = []
        for rd, v in series:
            h = max(2, int(60 * v / max_v))
            label = rd[5:]  # MM-DD
            bars.append(
                f'<div class="bar-col" title="{E(rd)}: {fmt.format(v)}">'
                f'<div class="bar-fill" style="height:{h}px;background:{color}"></div>'
                f'<div class="bar-val">{fmt.format(v)}</div>'
                f'<div class="bar-lbl">{E(label)}</div>'
                f"</div>"
            )
        return (
            f'<div class="trend-chart"><h4>{E(title)}</h4>'
            f'<div class="bar-strip">{"".join(bars)}</div></div>'
        )

    return (
        '<div class="trend-grid">'
        + render_series("Success %", series_success, "{:.1f}", "#34c759")
        + render_series("Units extracted", series_units, "{:.0f}", "#3a7bd5")
        + render_series("LLM cost ($)", series_cost, "{:.2f}", "#ff9500")
        + render_series("Failed", series_failed, "{:.0f}", "#ff3b30")
        + "</div>"
    )


def render_tier_distribution(data: dict[str, Any]) -> str:
    tiers: dict[str, int] = data["report"].tier_distribution or {}
    if not tiers:
        return "<p><em>No tier distribution recorded.</em></p>"
    total = sum(tiers.values()) or 1
    rows = []
    for tier, n in sorted(tiers.items(), key=lambda kv: -kv[1]):
        # color-code by family
        if tier.startswith("TIER_1"):
            color = "#34c759"
        elif tier.startswith("TIER_2") or tier.startswith("TIER_1_5"):
            color = "#5ac8fa"
        elif tier.startswith("TIER_3"):
            color = "#ffcc00"
        elif tier.startswith("TIER_4"):
            color = "#ff9500"
        elif tier.startswith("TIER_5") or tier.startswith("VISION"):
            color = "#ff3b30"
        elif "SYNDICATION" in tier:
            color = "#af52de"
        else:
            color = "#8e8e93"
        rows.append(bar_row(tier, n, total, color=color))
    return (
        '<table class="bars"><thead><tr><th>tier</th><th>count</th><th>distribution</th><th>%</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>"
    )


def render_issues(data: dict[str, Any]) -> str:
    issues = data["issues"]
    if not issues:
        return '<div class="slo-ok">No issues recorded ✓</div>'

    by_sev = Counter(i.severity for i in issues)
    by_code = Counter(i.code for i in issues)

    sev_rows = "".join(
        f"<tr><td>{E(s)}</td><td>{fmt_int(by_sev[s])}</td></tr>"
        for s in ("ERROR", "WARNING", "INFO")
        if by_sev.get(s, 0) > 0
    )
    sev_table = f'<table class="kv-tight"><thead><tr><th>severity</th><th>count</th></tr></thead><tbody>{sev_rows}</tbody></table>'

    total = sum(by_code.values()) or 1
    code_rows = []
    for code, n in by_code.most_common(15):
        # Color by severity prefix heuristic.
        if "FAIL" in code or "INVALID" in code or "ERROR" in code or "EXCEPTION" in code:
            color = "#ff3b30"
        elif "DRIFT" in code or "EMPTY" in code or "TIMEOUT" in code or "DISAPPEARED" in code:
            color = "#ff9500"
        else:
            color = "#3a7bd5"
        code_rows.append(bar_row(code, n, total, color=color))
    code_table = (
        '<table class="bars"><thead><tr><th>code</th><th>count</th><th>share</th><th>%</th></tr></thead><tbody>'
        + "".join(code_rows)
        + "</tbody></table>"
    )

    return f'<div class="two-col"><div class="col">{sev_table}</div><div class="col">{code_table}</div></div>'


def render_state_diff(data: dict[str, Any]) -> str:
    body = data["report"].model_dump(mode="json")
    sd = body.get("state_diff") or {}
    if not sd:
        # Synthesize from ledger if state_diff missing (e.g. Jugnu reports).
        ledger = data["ledger"]
        cf = sum(1 for e in ledger if e.carry_forward_used)
        units_total = sum(int(e.units_count or 0) for e in ledger)
        sd = {
            "units_extracted": units_total,
            "units_new": None,
            "units_updated": None,
            "units_unchanged": None,
            "units_disappeared": None,
            "units_carried_forward": None,
            "carry_forward_count": cf,
            "new_properties": None,
            "disappeared_properties": None,
        }

    rows = [
        ("New properties", _len_or_int(sd.get("new_properties"))),
        ("Disappeared properties", _len_or_int(sd.get("disappeared_properties"))),
        ("Carry-forward count", sd.get("carry_forward_count") or 0),
        ("Units extracted", sd.get("units_extracted") or 0),
        ("Units new", sd.get("units_new")),
        ("Units updated", sd.get("units_updated")),
        ("Units unchanged", sd.get("units_unchanged")),
        ("Units disappeared", sd.get("units_disappeared")),
        ("Units carried forward", sd.get("units_carried_forward")),
    ]
    return kv_table([(k, fmt_int(v) if v is not None else "—") for k, v in rows])


def _len_or_int(x: Any) -> int:
    if isinstance(x, list):
        return len(x)
    if isinstance(x, int):
        return x
    return 0


def render_cost_panel(data: dict[str, Any]) -> str:
    report = data["report"]
    cost = report.cost or {}
    properties = data["properties"]

    # Per-property LLM cost from _extract_result.
    spends: list[tuple[str, str, float, str]] = []  # (cid, name, cost, tier)
    for prop in properties:
        meta = prop.get("_meta") or {}
        ext = prop.get("_extract_result") or {}
        c = float(ext.get("llm_cost_usd") or 0)
        if c <= 0:
            continue
        cid = meta.get("canonical_id") or prop.get("apartment_id") or ""
        name = prop.get("proj_name") or prop.get("Property Name") or "(unnamed)"
        tier = ext.get("tier_used") or "?"
        spends.append((str(cid), str(name), c, str(tier)))
    spends.sort(key=lambda t: -t[2])

    summary = kv_table(
        [
            ("LLM total", fmt_money(cost.get("llm"))),
            ("Vision total", fmt_money(cost.get("vision"))),
            ("Properties with LLM spend", fmt_int(len(spends))),
            (
                "Avg cost per LLM-touched property",
                fmt_money(sum(s[2] for s in spends) / len(spends)) if spends else "$0.0000",
            ),
            (
                "Median cost per LLM-touched property",
                fmt_money(statistics.median(s[2] for s in spends)) if spends else "$0.0000",
            ),
        ]
    )

    if not spends:
        return summary

    top_rows = "".join(
        f"<tr><td><a href='#p-{E(cid)}'>{E(cid)}</a></td><td>{E(name)}</td>"
        f"<td>{E(tier)}</td><td>{fmt_money(c)}</td></tr>"
        for cid, name, c, tier in spends[:10]
    )
    top_table = (
        '<table class="cost"><thead><tr><th>id</th><th>name</th><th>tier</th><th>cost</th></tr></thead>'
        f"<tbody>{top_rows}</tbody></table>"
    )
    return f'<div class="two-col"><div class="col">{summary}</div><div class="col"><h4>Top 10 LLM spend</h4>{top_table}</div></div>'


def render_rent_insights(data: dict[str, Any]) -> str:
    db = data["db"]
    if not db.get("rent_histogram") and not db.get("rent_by_beds"):
        return "<p><em>No rent stats — rent_low column is empty in current state tables.</em></p>"

    summary = kv_table(
        [
            ("Units total (current state)", fmt_int(db["units_total"])),
            ("Units with rent", fmt_int(db["units_with_rent"])),
            ("Median rent", f"${db['rent_median']:,.0f}" if db.get("rent_median") else "—"),
            ("Mean rent", f"${db['rent_avg']:,.0f}" if db.get("rent_avg") else "—"),
            ("p10 / p90", f"${db.get('rent_p10') or 0:,.0f} / ${db.get('rent_p90') or 0:,.0f}"),
        ]
    )

    # Rent by bedroom — bar chart.
    by_beds = db.get("rent_by_beds") or []
    if by_beds:
        max_avg = max(b["avg_rent"] for b in by_beds) or 1
        bed_rows = []
        for b in by_beds:
            label = "?" if b["beds"] is None else f"{b['beds']} BR"
            pct = 100.0 * b["avg_rent"] / max_avg
            bed_rows.append(
                f'<tr><td class="lbl">{E(label)}</td>'
                f'<td class="val">{fmt_int(b["count"])} units</td>'
                f'<td class="bar"><div style="width:{pct:.1f}%;background:#5ac8fa"></div></td>'
                f'<td class="pct">${b["avg_rent"]:,.0f}</td></tr>'
            )
        bed_table = (
            '<table class="bars"><thead><tr><th>BR</th><th>count</th><th>avg rent</th><th></th></tr></thead><tbody>'
            + "".join(bed_rows)
            + "</tbody></table>"
        )
    else:
        bed_table = "<p><em>No bedroom breakdown.</em></p>"

    # Histogram.
    hist = db.get("rent_histogram") or []
    hist_html = ""
    if hist:
        max_count = max(b["count"] for b in hist) or 1
        bars = []
        for b in hist:
            h = max(2, int(80 * b["count"] / max_count))
            bars.append(
                f'<div class="bar-col" title="${b["lo"]:,.0f}–${b["hi"]:,.0f}: {b["count"]}">'
                f'<div class="bar-fill" style="height:{h}px;background:#3a7bd5"></div>'
                f'<div class="bar-lbl">${b["lo"]:,.0f}</div></div>'
            )
        hist_html = (
            f'<div class="trend-chart"><h4>Rent distribution (histogram)</h4>'
            f'<div class="bar-strip wide">{"".join(bars)}</div></div>'
        )

    return f'<div class="two-col"><div class="col">{summary}{bed_table}</div><div class="col">{hist_html}</div></div>'


def render_availability(data: dict[str, Any]) -> str:
    db = data["db"]
    buckets = db.get("avail_buckets") or []
    if not buckets:
        return "<p><em>No availability data.</em></p>"

    avail = db.get("units_with_avail", 0)
    total = db.get("units_total", 0) or 1
    pct = 100.0 * avail / total if total else 0
    summary = kv_table(
        [
            ("Units with available_date", f"{fmt_int(avail)} / {fmt_int(total)} ({pct:.1f}%)"),
            ("Disappeared (lifetime)", fmt_int(db.get("units_disappeared"))),
        ]
    )

    max_v = max(b["count"] for b in buckets) or 1
    rows = []
    for b in buckets:
        h_pct = 100.0 * b["count"] / max_v
        rows.append(
            f'<tr><td class="lbl">{E(b["label"])}</td>'
            f'<td class="val">{fmt_int(b["count"])}</td>'
            f'<td class="bar"><div style="width:{h_pct:.1f}%;background:#34c759"></div></td></tr>'
        )
    bucket_table = (
        '<table class="bars"><thead><tr><th>bucket</th><th>count</th><th></th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>"
    )
    return f'<div class="two-col"><div class="col">{summary}</div><div class="col"><h4>Days until move-in</h4>{bucket_table}</div></div>'


def render_profile_maturity(data: dict[str, Any]) -> str:
    mat = data["db"].get("profile_maturity") or {}
    if not mat:
        return "<p><em>No profile data.</em></p>"
    total = sum(mat.values()) or 1
    color_for = {"COLD": "#5ac8fa", "WARM": "#ff9500", "HOT": "#ff3b30"}
    rows = []
    for level in ("COLD", "WARM", "HOT", "UNKNOWN"):
        if level in mat:
            rows.append(bar_row(level, mat[level], total, color=color_for.get(level, "#8e8e93")))
    # Anything else.
    for level, n in mat.items():
        if level not in ("COLD", "WARM", "HOT", "UNKNOWN"):
            rows.append(bar_row(level, n, total))
    return (
        '<table class="bars"><thead><tr><th>maturity</th><th>count</th><th>distribution</th><th>%</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>"
    )


def render_dlq(data: dict[str, Any]) -> str:
    db = data["db"]
    total = db.get("dlq_total", 0)
    reasons = db.get("dlq_top_reasons") or []
    if total == 0:
        return '<div class="slo-ok">DLQ empty ✓</div>'
    rows = "".join(
        f"<tr><td>{E(r)}</td><td>{fmt_int(n)}</td></tr>" for r, n in reasons
    )
    return (
        f"<p><strong>{fmt_int(total)}</strong> property/properties currently parked.</p>"
        '<table class="kv-tight"><thead><tr><th>reason</th><th>count</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def render_failed_table(data: dict[str, Any]) -> str:
    body = data["report"].model_dump(mode="json")
    failed = body.get("failed_properties") or []
    # Synthesize from ledger if not present.
    if not failed:
        for entry in data["ledger"]:
            if entry.scrape_failed or (entry.status and entry.status.startswith("FAIL")):
                failed.append(
                    {
                        "row_index": entry.row_index,
                        "canonical_id": entry.canonical_id,
                        "reason": entry.status or "FAILED",
                        "url": entry.url,
                    }
                )

    if not failed:
        return '<div class="slo-ok">No failed properties ✓</div>'

    rows = []
    for f in failed[:200]:
        cid = str(f.get("canonical_id") or "")
        rows.append(
            "<tr>"
            f"<td>{E(f.get('row_index'))}</td>"
            f"<td><a href='#p-{E(cid)}'><code>{E(cid)}</code></a></td>"
            f"<td>{E(f.get('reason'))}</td>"
            f"<td class=mono>{E(f.get('url',''))}</td>"
            "</tr>"
        )
    extra = "" if len(failed) <= 200 else f"<p><em>… {len(failed)-200} more truncated.</em></p>"
    return (
        '<table class="failed"><thead><tr><th>row</th><th>id</th><th>reason</th><th>url</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>"
        + extra
    )


def render_property_index(data: dict[str, Any]) -> str:
    properties = data["properties"]
    ledger_by_cid = data["ledger_by_cid"]

    rows = []
    for prop in properties:
        meta = prop.get("_meta") or {}
        ext = prop.get("_extract_result") or {}
        cid = str(meta.get("canonical_id") or prop.get("apartment_id") or "")
        name = prop.get("proj_name") or prop.get("Property Name") or ""
        city = prop.get("city") or prop.get("City") or ""
        state = prop.get("state") or prop.get("State") or ""
        units = prop.get("units") or []
        n_units = len(units)
        tier = ext.get("tier_used") or "—"
        cost = float(ext.get("llm_cost_usd") or 0)
        verdict = meta.get("verdict") or "?"
        cls = "win" if verdict == "SUCCESS" else "loss" if "FAIL" in verdict else "tie"

        ledger = ledger_by_cid.get(cid)
        cf = "✓" if ledger and ledger.carry_forward_used else ""

        rows.append(
            f"<tr class={cls!r}>"
            f'<td><a href="#p-{E(cid)}"><code>{E(cid)}</code></a></td>'
            f"<td>{E(name)}</td>"
            f"<td>{E(city)}, {E(state)}</td>"
            f"<td class=tier>{E(tier)}</td>"
            f"<td>{fmt_int(n_units)}</td>"
            f"<td>{fmt_money(cost) if cost > 0 else ''}</td>"
            f"<td>{E(verdict)}</td>"
            f"<td>{cf}</td>"
            "</tr>"
        )
    return (
        '<table class="index sortable"><thead><tr>'
        "<th>id</th><th>name</th><th>location</th><th>tier</th>"
        "<th>units</th><th>LLM $</th><th>verdict</th><th>CF</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_property_panel(prop: dict[str, Any], data: dict[str, Any]) -> str:
    meta = prop.get("_meta") or {}
    ext = prop.get("_extract_result") or {}
    cid = str(meta.get("canonical_id") or prop.get("apartment_id") or "")
    name = prop.get("proj_name") or prop.get("Property Name") or "(unnamed)"
    units = prop.get("units") or []

    metadata_rows = [
        ("Canonical ID", cid),
        ("Apartment ID", prop.get("apartment_id")),
        ("PMC", prop.get("pmc")),
        ("Address", prop.get("address")),
        (
            "Location",
            f"{prop.get('city') or ''}, {prop.get('state') or ''} {prop.get('zip_code') or ''}",
        ),
        ("Phone", prop.get("phone")),
        (
            "Website",
            f"<a href='{E(prop.get('website',''))}' target='_blank'>{E(prop.get('website',''))}</a>",
        ),
        ("Verdict", meta.get("verdict")),
        ("Verdict reason", meta.get("verdict_reason")),
        ("Tier used", ext.get("tier_used")),
        ("LLM cost", fmt_money(ext.get("llm_cost_usd"))),
    ]
    metadata = kv_table([(k, v) for k, v in metadata_rows if v not in (None, "")])

    # Units table.
    if units:
        u_rows = []
        for u in units[:50]:
            u_rows.append(
                "<tr>"
                f"<td>{E(u.get('unit_id', ''))}</td>"
                f"<td>{E(u.get('floor_plan_name', ''))}</td>"
                f"<td>{E(u.get('beds'))}</td>"
                f"<td>{E(u.get('baths'))}</td>"
                f"<td>{E(u.get('area'))}</td>"
                f"<td>{E(u.get('rent_low'))}</td>"
                f"<td>{E(u.get('rent_high'))}</td>"
                f"<td>{E(u.get('available_date'))}</td>"
                "</tr>"
            )
        units_table = (
            '<table class="units"><thead><tr>'
            "<th>unit</th><th>floor plan</th><th>beds</th><th>baths</th>"
            "<th>area</th><th>rent low</th><th>rent high</th><th>avail</th>"
            "</tr></thead><tbody>"
            + "".join(u_rows)
            + "</tbody></table>"
        )
        if len(units) > 50:
            units_table += f"<p><em>… {len(units)-50} more units truncated.</em></p>"

        # Quick rent stats per panel for a fast eyeball.
        rents = [
            float(u.get("rent_low") or 0)
            for u in units
            if u.get("rent_low") and 200 < float(u.get("rent_low") or 0) < 50000
        ]
        if rents:
            stat_strip = (
                f'<div class="strip">'
                f'<span class="kpi"><b>{len(rents)}</b> with rent</span>'
                f'<span class="kpi">median <b>${statistics.median(rents):,.0f}</b></span>'
                f'<span class="kpi">range <b>${min(rents):,.0f}–${max(rents):,.0f}</b></span>'
                f"</div>"
            )
        else:
            stat_strip = '<div class="strip"><span class="kpi">no rent values</span></div>'
    else:
        units_table = "<p><em>No units extracted.</em></p>"
        stat_strip = ""

    # Issues for this property.
    cid_issues = data["issues_by_cid"].get(cid) or []
    if cid_issues:
        i_rows = "".join(
            f"<tr class={('fail' if i.severity == 'ERROR' else 'warn')!r}>"
            f"<td>{E(i.severity)}</td>"
            f"<td><code>{E(i.code)}</code></td>"
            f"<td>{E(i.message)}</td>"
            "</tr>"
            for i in cid_issues[:30]
        )
        issues_block = details(
            f"Issues ({len(cid_issues)})",
            f'<table class="issues"><thead><tr><th>sev</th><th>code</th><th>message</th></tr></thead><tbody>{i_rows}</tbody></table>',
        )
    else:
        issues_block = ""

    verdict = meta.get("verdict") or "?"
    verdict_cls = "ok" if verdict == "SUCCESS" else "fail" if "FAIL" in verdict else "warn"
    summary_line = (
        f'<span id="p-{E(cid)}"></span>'
        f'<span class="kpi {verdict_cls}">{E(verdict)}</span> '
        f'<strong>{E(cid)}</strong> · {E(name)} · '
        f'{fmt_int(len(units))} units · '
        f'tier <code>{E(ext.get("tier_used", "—"))}</code>'
        f'{(" · " + fmt_money(ext.get("llm_cost_usd"))) if ext.get("llm_cost_usd") else ""}'
    )
    body = (
        '<div class="prop-panel">'
        + stat_strip
        + '<div class="two-col">'
        + f'<div class="col">{metadata}</div>'
        + f'<div class="col">{units_table}</div>'
        + "</div>"
        + issues_block
        + "</div>"
    )
    return details(summary_line, body, cls="prop-detail")


# ---------------------------------------------------------------------------
# Top-level CSS
# ---------------------------------------------------------------------------

STYLE = """
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       font-size: 14px; line-height: 1.5; margin: 0; padding: 24px;
       background: #f6f7f9; color: #1d2333; }
.hdr { background: #11151c; color: #e3e8f0; padding: 18px 22px;
       border-radius: 8px; margin-bottom: 18px; }
.hdr h1 { margin: 0 0 10px 0; font-size: 20px; }
.hdr-meta { display: flex; flex-wrap: wrap; gap: 12px; }
.hdr-meta.sub { color: #aab1c2; font-size: 12px; margin-top: 8px; }
.hdr-meta.sub code { background: #1e2632; padding: 1px 5px; border-radius: 3px; }
.kpi { padding: 4px 10px; border-radius: 4px; background: #1e2632; }
.kpi b { color: #fff; }
.kpi.ok { background: #0c5d2a; }
.kpi.warn { background: #8a5e0a; }
.kpi.fail { background: #8b0a0a; }
h2 { margin-top: 28px; padding-top: 14px; border-top: 2px solid #d0d6e0;
     font-size: 18px; }
h3 { margin: 14px 0 6px 0; }
h4 { margin: 12px 0 4px 0; font-size: 13px; color: #404a64; }

.mono, code { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
              font-size: 12px; }

table { border-collapse: collapse; margin: 6px 0 12px 0; max-width: 100%; }
table th, table td { padding: 4px 10px; border: 1px solid #d8dee8;
                     text-align: left; vertical-align: top; }
table th { background: #eef1f6; font-weight: 600; }
table.kv th { width: 200px; }
table.kv-tight th, table.kv-tight td { padding: 2px 8px; }
table.bars { width: 100%; }
table.bars td.lbl { width: 32%; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
table.bars td.val { width: 12%; text-align: right; }
table.bars td.bar { width: 44%; }
table.bars td.bar > div { height: 12px; border-radius: 2px; }
table.bars td.pct { width: 12%; text-align: right; font-variant-numeric: tabular-nums; }
table.index td:nth-child(4) { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11px; }
table.units { width: 100%; }
table.units td { font-variant-numeric: tabular-nums; }
table.failed td.mono, .mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; word-break: break-all; max-width: 480px; }

tr.win { background: rgba(52, 199, 89, 0.10); }
tr.loss { background: rgba(255, 59, 48, 0.10); }
tr.tie { background: rgba(0,0,0,0); }
tr.fail { background: rgba(255, 59, 48, 0.10); }
tr.warn { background: rgba(255, 149, 0, 0.10); }
tr.ok { background: rgba(52, 199, 89, 0.10); }

.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 12px 0; }
.col { background: #fff; padding: 12px 16px; border-radius: 6px; border: 1px solid #d8dee8; }
@media (max-width: 1000px) { .two-col { grid-template-columns: 1fr; } }

.slo-ok { background: #d6f4dc; color: #0c5d2a; padding: 8px 12px;
          border-radius: 4px; display: inline-block; font-weight: 500; }
table.slo tr.ok { background: rgba(52, 199, 89, 0.10); }
table.slo tr.fail { background: rgba(255, 59, 48, 0.15); }

.strip { padding: 6px 0 10px 0; display: flex; gap: 8px; flex-wrap: wrap; }
.strip .kpi { background: #fff; border: 1px solid #d0d6e0; color: #1d2333; }
.strip .kpi b { color: #1d2333; }

.trend-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 1000px) { .trend-grid { grid-template-columns: 1fr; } }
.trend-chart { background: #fff; padding: 10px 12px; border: 1px solid #d8dee8; border-radius: 6px; }
.bar-strip { display: flex; align-items: flex-end; gap: 4px; padding: 6px 0; height: 100px; }
.bar-strip.wide { height: 110px; gap: 2px; }
.bar-col { display: flex; flex-direction: column; align-items: center; min-width: 24px; }
.bar-fill { width: 18px; border-radius: 2px 2px 0 0; }
.bar-strip.wide .bar-fill { width: 12px; }
.bar-val { font-size: 10px; color: #404a64; margin-top: 2px; font-variant-numeric: tabular-nums; }
.bar-lbl { font-size: 10px; color: #6b7280; }

.prop-panel { margin: 4px 0; padding: 10px 12px; border-left: 4px solid #3a7bd5;
              background: #fff; border-radius: 4px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
.prop-panel h3 { margin-top: 0; }
details.prop-detail { padding: 4px 8px; }
details.prop-detail > summary { font-weight: 400; }

details { background: #fff; padding: 6px 10px; margin: 4px 0;
          border: 1px solid #d8dee8; border-radius: 4px; }
details > summary { cursor: pointer; user-select: none; padding: 4px 0; font-weight: 500; }
details[open] > summary { border-bottom: 1px solid #eef1f6; margin-bottom: 8px; }

#props-toc { max-height: 600px; overflow-y: auto; }
"""


# ---------------------------------------------------------------------------
# Top-level renderer
# ---------------------------------------------------------------------------


def render_report(data: dict[str, Any]) -> str:
    sections = [
        render_header(data),
        '<h2 id="slo">SLO status</h2>',
        render_slo(data),
        '<h2 id="trend">7-run trend</h2>',
        render_trend(data),
        '<h2 id="tiers">Tier distribution</h2>',
        render_tier_distribution(data),
        '<h2 id="state">State diff</h2>',
        render_state_diff(data),
        '<h2 id="cost">Cost</h2>',
        render_cost_panel(data),
        '<h2 id="rent">Rent insights (current state)</h2>',
        render_rent_insights(data),
        '<h2 id="avail">Availability snapshot (current state)</h2>',
        render_availability(data),
        '<h2 id="profiles">Profile maturity</h2>',
        render_profile_maturity(data),
        '<h2 id="issues">Issues</h2>',
        render_issues(data),
        '<h2 id="dlq">Dead-letter queue</h2>',
        render_dlq(data),
        '<h2 id="failed">Failed properties</h2>',
        render_failed_table(data),
        '<h2 id="props">Property index</h2>',
        details(
            f"Index of {len(data['properties'])} properties (click row to jump)",
            f'<div id="props-toc">{render_property_index(data)}</div>',
            open_=False,
        ),
        '<h2 id="detail">Per-property detail</h2>',
    ]
    for prop in data["properties"]:
        sections.append(render_property_panel(prop, data))

    title = f"Daily report — {data['run_date']}"
    return (
        "<!doctype html><html><head>"
        '<meta charset="utf-8">'
        f"<title>{E(title)}</title>"
        f"<style>{STYLE}</style>"
        "</head><body>"
        + "".join(sections)
        + "</body></html>"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--run-date",
        default=datetime.now(timezone.utc).date().isoformat(),
        help="Run date (YYYY-MM-DD). Defaults to today UTC.",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Output HTML path. Defaults to data/runs/{run-date}/daily_report.html",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Override DATABASE_URL. asyncpg URLs are auto-translated to pg8000.",
    )
    p.add_argument(
        "--trend-window",
        type=int,
        default=7,
        help="Number of recent runs (including this one) to include in trend (default 7).",
    )
    args = p.parse_args()

    provider = _ensure_provider(args.database_url)
    log.info("Using data provider: %s", provider.name)

    data = collect_run_data(provider, args.run_date, trend_window=args.trend_window)

    out_path = (
        Path(args.out)
        if args.out
        else _MA_POC_ROOT / "data" / "runs" / args.run_date / "daily_report.html"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_report(data)
    out_path.write_text(html_text, encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    log.info(
        "wrote %s (%d properties, %d issues, %.0f KB)",
        out_path,
        len(data["properties"]),
        len(data["issues"]),
        size_kb,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
