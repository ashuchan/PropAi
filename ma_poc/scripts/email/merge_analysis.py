"""Email the unit-merge-failure analysis for a given run_date.

Thin orchestrator. The real work is split across two reusable modules:

  * :mod:`ma_poc.services.merge_analysis` — pulls findings from the DB,
    returns a :class:`MergeAnalysis` dataclass. Reusable from CLIs, CI
    gates, dashboards.
  * :mod:`ma_poc.scripts.email_html` — generic Gmail MCP send wrapper
    shared with ``email_daily_report.py``.

This file owns: CLI parsing, run-date resolution, HTML rendering specific
to the merge-analysis email shape, and wiring those three layers together.

Usage
-----

    # Latest run (today UTC, falling back to most recent)
    python scripts/email_merge_analysis.py

    # Specific run
    python scripts/email_merge_analysis.py --run-date 2026-05-05

    # Render only — print HTML to stdout, do not send
    python scripts/email_merge_analysis.py --dry-run

    # Override recipients (defaults to REPORT_RECIPIENTS env var)
    python scripts/email_merge_analysis.py --recipients a@x.com,b@y.com
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import sys
from pathlib import Path

_MA_POC_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _MA_POC_ROOT.parent
for _p in (_MA_POC_ROOT, _REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from dotenv import load_dotenv  # noqa: E402

from scripts.email.daily import (  # noqa: E402
    _open_provider,
    _parse_recipients,
    _resolve_run_date,
)
from scripts.email._client import send_html_email  # noqa: E402
from services.merge_analysis import (  # noqa: E402
    DisappearedHistoryRow,
    IdentityMix,
    MergeAnalysis,
    MergeAnalyzer,
    MergeYield,
    PhenotypeDupeRow,
    TierRow,
    ZeroMergeRow,
)

log = logging.getLogger("email_merge_analysis")


# ── HTML helpers (kept tiny so render_html() is readable) ───────────────────


def _e(s: object) -> str:
    return html.escape(str(s) if s is not None else "")


def _fmt_int(n: object) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def _pct(num: float | int, den: float | int) -> str:
    try:
        n, d = float(num), float(den)
    except (TypeError, ValueError):
        return "—"
    return f"{100.0 * n / d:.1f}%" if d > 0 else "—"


def _row_class(pct: float, *, bad_above: float, warn_above: float) -> str:
    if pct > bad_above:
        return "bad"
    if pct > warn_above:
        return "warn"
    return "ok"


# ── Section renderers ──────────────────────────────────────────────────────
#
# One function per section, each takes only the slice of MergeAnalysis it
# needs. Keeps render_html() readable as a TOC of sections.


_STYLE = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       font-size: 14px; line-height: 1.5; color: #1d2333; background: #f6f7f9;
       margin: 0; padding: 18px; }
.wrap { max-width: 820px; margin: 0 auto; background: #fff; border: 1px solid #d8dee8;
        border-radius: 8px; padding: 22px 26px; }
h1 { font-size: 20px; margin: 0 0 4px 0; }
h2 { font-size: 16px; margin: 22px 0 8px 0; padding-top: 14px;
     border-top: 2px solid #d0d6e0; }
.sub { color: #6b7280; font-size: 12px; margin-bottom: 14px; }
.kpis { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 4px 0; }
.kpi { padding: 6px 12px; border-radius: 4px; background: #1e2632; color: #fff;
       font-size: 13px; }
.kpi.bad { background: #8b0a0a; }
.kpi.warn { background: #8a5e0a; }
.kpi.ok { background: #0c5d2a; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 10px 0; font-size: 13px; }
th, td { padding: 5px 9px; border: 1px solid #d8dee8; text-align: left; vertical-align: top; }
th { background: #eef1f6; font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.bad td { background: rgba(255,59,48,0.10); }
tr.warn td { background: rgba(255,149,0,0.08); }
tr.ok td { background: rgba(52,199,89,0.08); }
.code { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 12px; }
.foot { margin-top: 22px; color: #64748b; font-size: 11px; }
.cause { background: #fff8f0; border-left: 4px solid #ff9500; padding: 8px 12px;
         margin: 8px 0; border-radius: 0 4px 4px 0; }
.cause h3 { margin: 0 0 4px 0; font-size: 13px; }
.cause p { margin: 0; }
"""


def _render_kpis(my: MergeYield, dis_units: int, idmix: IdentityMix) -> str:
    cls = "bad" if my.loss_pct > 30 else "warn" if my.loss_pct > 10 else "ok"
    dis_cls = "bad" if dis_units > 5000 else "warn"
    return (
        '<div class="kpis">'
        f'<span class="kpi">{_fmt_int(my.props_total)} properties</span>'
        f'<span class="kpi">{_fmt_int(my.units_in_snap_total)} units captured</span>'
        f'<span class="kpi {cls}"><b>{_fmt_int(my.units_lost)}</b> '
        f'dropped during merge ({my.loss_pct:.1f}%)</span>'
        f'<span class="kpi {dis_cls}"><b>{_fmt_int(dis_units)}</b> '
        'units flagged disappeared</span>'
        f'<span class="kpi bad"><b>{_fmt_int(idmix.no_unit_id)}</b> records w/o unit_id</span>'
        '</div>'
    )


def _render_yield(my: MergeYield) -> str:
    rows = [
        ("Properties in snapshot", _fmt_int(my.props_total), ""),
        ("Properties — 0 of N units survived merge", _fmt_int(my.prop_zero_merged), "bad"),
        ("Properties — partial merge", _fmt_int(my.prop_partial), "warn"),
        ("Properties — full merge", _fmt_int(my.prop_full), "ok"),
        ("Units captured (snapshot)", _fmt_int(my.units_in_snap_total), ""),
        ("Units merged into canonical table", _fmt_int(my.units_in_table_total), ""),
        ("Units silently dropped",
         f"{_fmt_int(my.units_lost)} ({my.loss_pct:.1f}%)",
         "bad" if my.units_lost else "ok"),
    ]
    body = "".join(
        f'<tr class="{cls}"><th>{_e(label)}</th><td class="num">{val}</td></tr>'
        for label, val, cls in rows
    )
    return f'<table>{body}</table>'


def _render_id_mix(idmix: IdentityMix) -> str:
    rows = [
        ("No unit_id (silently dropped by upsert_units)", idmix.no_unit_id, "bad"),
        ("inferred_* (SHA256 fallback — fragile)", idmix.inferred_id, "warn"),
        ("Natural ID (stable)", idmix.natural_id, "ok"),
    ]
    body = "".join(
        f'<tr class="{cls}"><th>{_e(label)}</th>'
        f'<td class="num">{_fmt_int(v)}</td>'
        f'<td class="num">{_pct(v, idmix.total)}</td></tr>'
        for label, v, cls in rows
    )
    return f'<table>{body}</table>'


def _render_tiers(rows: list[TierRow]) -> str:
    if not rows:
        return "<p><em>No tier data.</em></p>"
    body = "".join(
        f'<tr class="{_row_class(r.pct_no_id, bad_above=60, warn_above=20)}">'
        f'<td class="code">{_e(r.tier)}</td>'
        f'<td class="num">{_fmt_int(r.props)}</td>'
        f'<td class="num">{_fmt_int(r.units_total)}</td>'
        f'<td class="num">{_fmt_int(r.units_no_id)}</td>'
        f'<td class="num">{r.pct_no_id:.1f}%</td>'
        '</tr>'
        for r in rows
    )
    return (
        '<table><thead><tr>'
        '<th>tier</th><th>props</th><th>units</th>'
        '<th>units w/o unit_id</th><th>%</th>'
        '</tr></thead><tbody>' + body + '</tbody></table>'
    )


def _render_zero_merge(rows: list[ZeroMergeRow]) -> str:
    if not rows:
        return '<p><em>No zero-merge properties — all snapshots merged at least one unit.</em></p>'
    body = "".join(
        f'<tr><td class="code">{_e(r.canonical_id)}</td>'
        f'<td class="num">{_fmt_int(r.units_in_snap)}</td>'
        f'<td class="code">{_e(r.tier)}</td></tr>'
        for r in rows
    )
    return (
        '<table><thead><tr>'
        '<th>canonical_id</th><th>units captured</th><th>tier</th>'
        '</tr></thead><tbody>' + body + '</tbody></table>'
    )


def _render_dupes(rows: list[PhenotypeDupeRow]) -> str:
    if not rows:
        return '<p><em>No physical-phenotype duplication detected.</em></p>'
    body = "".join(
        f'<tr><td class="code">{_e(r.canonical_id)}</td>'
        f'<td class="num">{_fmt_int(r.records)}</td>'
        f'<td class="num">{_fmt_int(r.distinct_phys)}</td>'
        f'<td class="num">{_fmt_int(r.excess)}</td></tr>'
        for r in rows
    )
    return (
        '<table><thead><tr>'
        '<th>canonical_id</th><th>records</th>'
        '<th>distinct phenotypes</th><th>excess</th>'
        '</tr></thead><tbody>' + body + '</tbody></table>'
    )


def _render_history(rows: list[DisappearedHistoryRow]) -> str:
    if not rows:
        return '<p><em>No disappeared-unit history.</em></p>'
    body = "".join(
        f'<tr><td>{_e(r.disappeared_since)}</td>'
        f'<td class="num">{_fmt_int(r.units)}</td>'
        f'<td class="num">{_fmt_int(r.props)}</td></tr>'
        for r in rows
    )
    return (
        '<table><thead><tr>'
        '<th>disappeared_since</th><th>units</th><th>properties</th>'
        '</tr></thead><tbody>' + body + '</tbody></table>'
    )


_ROOT_CAUSES: list[tuple[str, str]] = [
    (
        "Silent skip on missing unit_id",
        "Both <code>scripts/state_store.py</code> and "
        "<code>data_provider/sql/stores.py</code> start their loop with "
        "<code>uid = str(u.get('unit_id') or '').strip(); if not uid: continue</code>. "
        "No event, no rejected-record entry, no diff bucket — the unit just vanishes.",
    ),
    (
        "Fallback ID isn't written back to the record",
        "<code>scripts/scrape_properties.py</code> only writes the fallback "
        "onto <code>rec['unit_id']</code> when the SHA256 path succeeds. The "
        "last-resort <code>floor_plan|beds|sqft</code> key is used only as a "
        "within-run dedup key — records still reach <code>upsert_units</code> "
        "with empty <code>unit_id</code>.",
    ),
    (
        "LLM-DOM, LLM, JSON-LD, MERGED_CROSS_PAGE rarely emit unit_id",
        "Cross-tab below shows 84% – 100% of units from these tiers have no "
        "<code>unit_id</code>. PMS-specific Tier-1 adapters (SightMap, "
        "Entrata, AppFolio, AvalonBay, OneSite) hit 0% loss — they assign "
        "IDs at extraction time.",
    ),
    (
        "SHA256 fallback is brittle",
        "<code>identity_fallback._n()</code> lowercases + strips whitespace "
        "but does no semantic normalization. <code>1 Bedroom</code> vs "
        "<code>2 Bedrooms</code>, <code>A4</code> vs <code>A4-LL</code>, "
        "<code>1</code> vs <code>1.0</code>, <code>area=-1</code> vs "
        "<code>area=NULL</code> — all hash differently for the same physical "
        "unit.",
    ),
    (
        "Same property scraped multiple times in one run",
        "Multi-shard runs call <code>upsert_units</code> for the same "
        "<code>canonical_id</code> twice with different ID schemes; pass-2 "
        "evicts pass-1's IDs into <code>disappeared_since</code> "
        "immediately.",
    ),
    (
        "carryforward_days never accumulates",
        "Reset and increment paths aren't symmetric across the FS and SQL "
        "stores — every row reads <code>carryforward_days=0</code>.",
    ),
    (
        "JSON-LD extractor has no unit_id mapping",
        "<code>extraction/tier2_jsonld.py</code> emits 100% no-unit-id "
        "records.",
    ),
    (
        "Cross-page merger emits each plan twice",
        "Per the dupe table below, several properties show records / distinct "
        "phenotypes near 2.0 - the merger is concatenating partial and full "
        "records for the same plan instead of picking the more-attributed "
        "one.",
    ),
    (
        "No SLO alarm on \"scraped clean but nothing merged\"",
        "Properties with snapshot.units &gt; 0 and merged-units = 0 currently "
        "report <code>verdict=SUCCESS</code>. Need a post-merge yield gate.",
    ),
]


_FIX_PRIORITY: list[str] = [
    "Assign <code>unit_id</code> at extraction time in JSON-LD and Tier 4 "
    "LLM paths.",
    "Make <code>SqlUnitStateStore.upsert_units</code> mirror the FS store's "
    "<code>compute_fallback_unit_id</code> call.",
    "Add post-merge \"snapshot vs merged\" yield check that emits "
    "<code>next_tier_requested</code> when &gt;50% of units fail to key.",
    "Harden <code>_n()</code> in <code>identity_fallback.py</code>: strip "
    "trailing plural <code>s</code>, normalize numeric strings, treat "
    "<code>area=-1</code> as <code>NULL</code> before hashing.",
    "Make <code>upsert_units</code> idempotent across shards.",
]


def _render_causes() -> str:
    return "".join(
        f'<div class="cause"><h3>{i}. {_e(title)}</h3><p>{body}</p></div>'
        for i, (title, body) in enumerate(_ROOT_CAUSES, start=1)
    )


def _render_fix_list() -> str:
    return "<ol>" + "".join(f"<li>{item}</li>" for item in _FIX_PRIORITY) + "</ol>"


# ── Top-level renderer ─────────────────────────────────────────────────────


def render_html(analysis: MergeAnalysis) -> str:
    """Render a full HTML email body from a :class:`MergeAnalysis`."""
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<title>Unit-merge analysis — {_e(analysis.run_date)}</title>'
        f'<style>{_STYLE}</style>'
        '</head><body><div class="wrap">'
        f'<h1>Unit-merge analysis — run {_e(analysis.run_date)}</h1>'
        '<div class="sub">Why captured units fail to merge against prior '
        'runs. Sourced live from Cloud SQL <code>jugnu</code> mirror '
        '(<code>property_snapshots</code>, <code>units</code>).</div>'
        + _render_kpis(analysis.merge_yield, analysis.disappeared.disappeared_units, analysis.id_mix)
        + '<h2>1. Merge yield</h2>'
        + _render_yield(analysis.merge_yield)
        + '<h2>2. Captured-unit identity mix</h2>'
        + _render_id_mix(analysis.id_mix)
        + '<h2>3. Loss by extraction tier</h2>'
        '<div class="sub">Snapshot units grouped by which extractor produced '
        'them, and how many of those units have no <code>unit_id</code> at '
        'all.</div>'
        + _render_tiers(analysis.tier_breakdown)
        + '<h2>4. Properties where 0 of N captured units survived merge</h2>'
        + _render_zero_merge(analysis.zero_merge_samples)
        + '<h2>5. In-snapshot duplicate physical phenotypes</h2>'
        + _render_dupes(analysis.phenotype_dupes)
        + '<h2>6. Disappeared-units history</h2>'
        + _render_history(analysis.disappeared_history)
        + '<h2>Root causes</h2>' + _render_causes()
        + '<h2>Quickest single-PR fixes (priority order)</h2>' + _render_fix_list()
        + '<div class="foot">Sent automatically by '
        '<span class="code">ma_poc/scripts/email_merge_analysis.py</span>.'
        '</div></div></body></html>'
    )


# ── Entrypoint ──────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv(_MA_POC_ROOT / ".env")

    parser = argparse.ArgumentParser(
        description="Email the unit-merge-failure analysis."
    )
    parser.add_argument("--run-date", default=None,
                        help="YYYY-MM-DD. Defaults to today UTC, falling back to latest run.")
    parser.add_argument("--recipients", default=None,
                        help="Comma-separated. Defaults to REPORT_RECIPIENTS env var.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Render HTML to stdout and skip the Gmail MCP send.")
    parser.add_argument("--database-url", default=None,
                        help="Override DATABASE_URL (asyncpg auto-translates to pg8000).")
    args = parser.parse_args(argv)

    recipients = (
        _parse_recipients(args.recipients)
        or _parse_recipients(os.getenv("REPORT_RECIPIENTS"))
    )
    if not recipients and not args.dry_run:
        log.error("No recipients. Set REPORT_RECIPIENTS in .env or pass --recipients.")
        return 1

    provider = _open_provider(args.database_url)
    log.info("Data provider: %s", provider.name)
    try:
        run_date = _resolve_run_date(provider, args.run_date)
        analysis = MergeAnalyzer(provider.engine).analyze(run_date)
    finally:
        provider.close()

    log.info(
        "run_date=%s props=%s units_snap=%s units_merged=%s lost=%s disappeared=%s",
        run_date,
        analysis.merge_yield.props_total,
        analysis.merge_yield.units_in_snap_total,
        analysis.merge_yield.units_in_table_total,
        analysis.merge_yield.units_lost,
        analysis.disappeared.disappeared_units,
    )

    if args.dry_run:
        sys.stdout.write(render_html(analysis))
        sys.stdout.write("\n")
        log.info("Dry run — would send to: %s", ", ".join(recipients) or "(none)")
        return 0

    result = send_html_email(
        subject=f"PropAi unit-merge analysis — {run_date}",
        html_body=render_html(analysis),
        recipients=recipients,
        sender_name=os.getenv("REPORT_SENDER_NAME"),
    )
    return 1 if result.get("isError") else 0


if __name__ == "__main__":
    sys.exit(main())
