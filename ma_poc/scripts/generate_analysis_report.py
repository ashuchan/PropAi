#!/usr/bin/env python3
"""Render a self-contained HTML analysis report for a universe-vs-legacy run.

Reads the per_property/*.json files produced by ``universe_experiment.py``
and emits a single HTML file with one collapsible panel per property:
side-by-side legacy + universe outcomes, the full pre-rank universe, the
LLM ranker output, the queue, every sub-universe, every per-call prompt
and raw response (universe only — legacy gets summary stats only), and
the final unit diff.

Usage:
    python scripts/generate_analysis_report.py \
        ma_poc/data/experiments/universe_vs_legacy_20260426_112121 \
        --out ma_poc/data/experiments/universe_vs_legacy_20260426_112121/analysis_report.html

    # Combine multiple experiment dirs in one report
    python scripts/generate_analysis_report.py \
        ma_poc/data/experiments/universe_vs_legacy_20260426_112121 \
        ma_poc/data/experiments/universe_vs_legacy_20260426_112134 \
        --out ma_poc/data/experiments/combined_analysis.html
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _read_property(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _load_records(dirs: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in dirs:
        per = d / "per_property"
        if not per.is_dir():
            print(f"warning: {per} not a directory, skipping", file=sys.stderr)
            continue
        for f in sorted(per.glob("*.json")):
            rec = _read_property(f)
            rec["_experiment"] = d.name
            rows.append(rec)
    return rows


def _diagnosis(rec: dict[str, Any]) -> str:
    leg = rec.get("legacy") or {}
    uni = rec.get("universe") or {}
    ud = rec.get("universe_debug") or {}
    leg_units = len(leg.get("units") or [])
    uni_units = len(uni.get("units") or [])
    stop = ud.get("stop_reason", "")

    if not ud:
        if leg_units > 0 and uni_units == 0:
            return f"universe didn't fire; legacy hit {leg.get('tier_used','')}"
        if leg_units == 0 and uni_units == 0:
            return "both pipelines extracted 0 units (likely site-level failure)"
        return "tie — both pipelines hit deterministic tier"

    if leg_units > 0 and uni_units == 0:
        return f"REGRESSION — universe stopped on {stop!r} while legacy hit {leg.get('tier_used','')} ({leg_units} units)"
    if uni_units > leg_units and leg_units == 0:
        return f"WIN — universe extracted {uni_units}; legacy got 0"
    if uni_units > leg_units:
        return f"WIN — universe +{uni_units - leg_units} units"
    if uni_units == leg_units and leg_units > 0:
        return "tie with units"
    if uni_units == 0 and leg_units == 0:
        return f"both 0 — universe stop_reason={stop!r}"
    return f"universe={uni_units}, legacy={leg_units}, stop={stop!r}"


# ---------------------------------------------------------------------------
# HTML rendering helpers — keep escaping centralized so a stray \" in a
# raw LLM response can't break the report layout
# ---------------------------------------------------------------------------


def E(s: Any) -> str:
    return html.escape(str(s) if s is not None else "")


def details(summary: str, body: str, *, open_: bool = False, cls: str = "") -> str:
    o = " open" if open_ else ""
    c = f' class="{cls}"' if cls else ""
    return f"<details{o}{c}><summary>{summary}</summary>{body}</details>"


def code_block(s: str, lang: str = "") -> str:
    cls = f' class="lang-{lang}"' if lang else ""
    return f'<pre{cls}>{E(s)}</pre>'


def kv_table(rows: list[tuple[str, Any]]) -> str:
    out = ['<table class="kv">']
    for k, v in rows:
        out.append(f"<tr><th>{E(k)}</th><td>{E(v)}</td></tr>")
    out.append("</table>")
    return "".join(out)


# ---------------------------------------------------------------------------
# Per-section renderers
# ---------------------------------------------------------------------------


def render_sample_meta(rec: dict[str, Any]) -> str:
    s = rec.get("sample") or {}
    return kv_table(
        [
            ("Experiment", rec.get("_experiment", "")),
            ("Canonical ID", s.get("canonical_id", "")),
            ("Property name", s.get("property_name", "")),
            ("URL", f'<a href="{E(s.get("url",""))}" target="_blank">{E(s.get("url",""))}</a>'),
            ("Bucket", s.get("bucket", "")),
            ("Sample tier hint", s.get("tier_used", "")),
            ("Sample units count", s.get("units_count", "")),
        ]
    )


def render_outcome_summary(side: dict[str, Any], stop_reason: str = "") -> str:
    rows: list[tuple[str, Any]] = [
        ("tier_used", side.get("tier_used", "") or "(none)"),
        ("units extracted", len(side.get("units") or [])),
        ("LLM cost (USD)", f"${float(side.get('llm_cost_usd') or 0):.4f}"),
        ("LLM calls", side.get("llm_calls", 0)),
        ("wall-clock ms", side.get("wall_clock_ms", 0)),
        ("errors", "; ".join(side.get("errors") or []) or "(none)"),
        ("notes", side.get("notes", "") or "(none)"),
    ]
    if stop_reason:
        rows.append(("stop_reason", stop_reason))
    return kv_table(rows)


def render_pre_rank_universe(ud: dict[str, Any]) -> str:
    snap = ud.get("pre_rank_universe") or {}
    if not snap:
        return "<p><em>No pre-rank snapshot — debug flag not enabled?</em></p>"

    apis = snap.get("api_candidates") or []
    intl = snap.get("internal_links") or []
    ext = snap.get("external_links") or []

    def api_row(c: dict[str, Any]) -> str:
        return (
            f"<tr><td class=mono>{E(c.get('url',''))}</td>"
            f"<td>{E(c.get('has_unit_signals'))}</td>"
            f"<td>{E(c.get('body_bytes',0))}</td>"
            f"<td>{E(c.get('base_score',0))}</td>"
            f"<td class=mono>{E((c.get('preview_first_300') or '')[:200])}</td></tr>"
        )

    def link_row(c: dict[str, Any]) -> str:
        return (
            f"<tr><td class=mono>{E(c.get('url',''))}</td>"
            f"<td>{E(c.get('anchor',''))}</td>"
            f"<td>{E(c.get('base_score',0))}</td>"
            f"<td>{E(c.get('is_portal'))}</td></tr>"
        )

    api_html = (
        '<table class="universe"><thead><tr><th>URL</th><th>signals</th><th>bytes</th><th>base</th><th>preview</th></tr></thead>'
        + "<tbody>" + "".join(api_row(c) for c in apis) + "</tbody></table>"
    )
    intl_html = (
        '<table class="universe"><thead><tr><th>URL</th><th>anchor</th><th>base</th><th>portal</th></tr></thead>'
        + "<tbody>" + "".join(link_row(c) for c in intl) + "</tbody></table>"
    )
    ext_html = (
        '<table class="universe"><thead><tr><th>URL</th><th>anchor</th><th>base</th><th>portal</th></tr></thead>'
        + "<tbody>" + "".join(link_row(c) for c in ext) + "</tbody></table>"
    )

    counts = (
        f"<p>source_page_url: <code>{E(snap.get('source_page_url',''))}</code> · "
        f"blocked_api_count: <strong>{E(snap.get('blocked_api_count',0))}</strong> · "
        f"skipped_link_count: <strong>{E(snap.get('skipped_link_count',0))}</strong></p>"
    )

    return (
        counts
        + details(f"APIs gathered ({len(apis)})", api_html)
        + details(f"Internal links ({len(intl)})", intl_html)
        + details(f"External links ({len(ext)})", ext_html)
    )


def render_llm_ranking(ud: dict[str, Any]) -> str:
    ranked = ud.get("llm_ranking") or []
    if not ranked:
        return "<p><em>No LLM ranking output captured.</em></p>"

    out = [
        '<table class="ranking"><thead><tr>'
        "<th>#</th><th>conf</th><th>kind</th><th>URL</th><th>reason</th></tr></thead><tbody>"
    ]
    for i, r in enumerate(ranked, 1):
        conf = float(r.get("confidence") or 0)
        bg = _confidence_bg(conf)
        out.append(
            f'<tr><td>{i}</td>'
            f'<td style="background:{bg}">{conf:.2f}</td>'
            f"<td>{E(r.get('kind',''))}</td>"
            f"<td class=mono>{E(r.get('url',''))}</td>"
            f"<td>{E(r.get('reason',''))}</td></tr>"
        )
    out.append("</tbody></table>")
    return "".join(out)


def render_initial_queue(ud: dict[str, Any]) -> str:
    q = ud.get("initial_queue") or []
    if not q:
        return "<p><em>Empty queue.</em></p>"
    out = [
        '<table class="queue"><thead><tr>'
        "<th>#</th><th>eff</th><th>kind</th><th>llm</th><th>base</th><th>URL</th></tr></thead><tbody>"
    ]
    for i, c in enumerate(q[:80], 1):
        eff = float(c.get("effective_score") or 0)
        out.append(
            f'<tr><td>{i}</td>'
            f'<td style="background:{_confidence_bg(eff)}">{eff:.2f}</td>'
            f"<td>{E(c.get('kind',''))}</td>"
            f"<td>{E(c.get('llm_score'))}</td>"
            f"<td>{E(c.get('base_score'))}</td>"
            f"<td class=mono>{E(c.get('url',''))}</td></tr>"
        )
    out.append("</tbody></table>")
    if len(q) > 80:
        out.append(f"<p><em>… {len(q)-80} more rows truncated.</em></p>")
    return "".join(out)


def render_sub_universes(ud: dict[str, Any]) -> str:
    subs = ud.get("sub_universes") or []
    queues = ud.get("sub_queues") or []
    if not subs and not queues:
        return "<p><em>No sub-universes — explorer didn't fetch any links.</em></p>"
    by_link = {s.get("parent_link"): s for s in subs}
    queue_by_link: dict[str, list[dict[str, Any]]] = {}
    for q in queues:
        queue_by_link.setdefault(q.get("parent_link", ""), []).append(q)

    blocks: list[str] = []
    for link, snap in by_link.items():
        s = snap.get("snapshot") or {}
        deduped = snap.get("deduped_against_parent", 0)

        body_parts = [
            f"<p>deduped against parent: <strong>{E(deduped)}</strong></p>",
            f"<p>blocked_api_count: <strong>{E(s.get('blocked_api_count',0))}</strong> · "
            f"skipped_link_count: <strong>{E(s.get('skipped_link_count',0))}</strong></p>",
        ]

        # APIs in sub-universe
        apis = s.get("api_candidates") or []
        if apis:
            api_rows = "".join(
                f"<tr><td class=mono>{E(c.get('url',''))}</td>"
                f"<td>{E(c.get('has_unit_signals'))}</td>"
                f"<td>{E(c.get('base_score',0))}</td>"
                f"<td>{E(c.get('llm_score'))}</td>"
                f"<td class=mono>{E((c.get('preview_first_300') or '')[:160])}</td></tr>"
                for c in apis
            )
            body_parts.append(
                details(
                    f"Sub-APIs ({len(apis)})",
                    f'<table class="universe"><thead><tr><th>URL</th><th>signals</th><th>base</th><th>llm</th><th>preview</th></tr></thead><tbody>{api_rows}</tbody></table>',
                )
            )

        intl = s.get("internal_links") or []
        if intl:
            link_rows = "".join(
                f"<tr><td class=mono>{E(c.get('url',''))}</td>"
                f"<td>{E(c.get('anchor',''))}</td>"
                f"<td>{E(c.get('base_score',0))}</td></tr>"
                for c in intl
            )
            body_parts.append(
                details(
                    f"Sub internal-links ({len(intl)})",
                    f'<table class="universe"><thead><tr><th>URL</th><th>anchor</th><th>base</th></tr></thead><tbody>{link_rows}</tbody></table>',
                )
            )

        # Sub-queue chosen
        for q in queue_by_link.get(link, []):
            qrows = q.get("queue") or []
            if qrows:
                rows = "".join(
                    f"<tr><td>{E(c.get('kind',''))}</td>"
                    f"<td>{float(c.get('effective_score') or 0):.2f}</td>"
                    f"<td>{E(c.get('llm_score'))}</td>"
                    f"<td>{E(c.get('has_unit_signals'))}</td>"
                    f"<td class=mono>{E(c.get('url',''))}</td></tr>"
                    for c in qrows
                )
                body_parts.append(
                    details(
                        f"Sub-queue chosen ({len(qrows)} item(s))",
                        f'<table class="queue"><thead><tr><th>kind</th><th>eff</th><th>llm</th><th>signals</th><th>URL</th></tr></thead><tbody>{rows}</tbody></table>',
                        open_=True,
                    )
                )

        blocks.append(
            details(
                f"Sub-universe for <code>{E(link)}</code>",
                "".join(body_parts),
            )
        )

    return "".join(blocks)


def render_universe_llm_calls(ud: dict[str, Any]) -> str:
    calls = ud.get("llm_calls") or []
    if not calls:
        return "<p><em>No LLM calls captured.</em></p>"
    out = []
    for i, c in enumerate(calls, 1):
        role = c.get("role", "")
        target = c.get("target_url", "") or ""
        cost = float(c.get("cost_usd") or 0)
        success = c.get("success")
        title = (
            f"#{i} <strong>{E(role)}</strong> · "
            f"tier={E(c.get('tier',''))} · "
            f"tokens {E(c.get('tokens_input',0))}→{E(c.get('tokens_output',0))} · "
            f"${cost:.4f} · "
            f"{E(c.get('latency_ms',0))}ms · "
            f"success={E(success)}"
        )
        if target:
            title += f" · target <code class=mono>{E(target)}</code>"

        body = []
        if c.get("error"):
            body.append(f'<p class="err"><strong>error:</strong> {E(c.get("error"))}</p>')
        body.append(
            details("System prompt", code_block(c.get("system_prompt") or ""))
        )
        body.append(
            details(
                "User prompt (truncated to 500 chars by interaction logger)",
                code_block(c.get("user_prompt_truncated_500") or ""),
            )
        )
        body.append(details("Raw response", code_block(c.get("raw_response") or "")))

        out.append(details(title, "".join(body)))
    return "".join(out)


def render_unit_diff(rec: dict[str, Any]) -> str:
    leg_units = (rec.get("legacy") or {}).get("units") or []
    uni_units = (rec.get("universe") or {}).get("units") or []

    def key(u: dict[str, Any]) -> str:
        return str(
            u.get("unit_id")
            or f"{u.get('floor_plan_name','')}|{u.get('bedrooms','')}|{u.get('bathrooms','')}|{u.get('sqft','')}"
        )

    leg_by = {key(u): u for u in leg_units}
    uni_by = {key(u): u for u in uni_units}
    all_keys = sorted(set(leg_by) | set(uni_by))

    rows = []
    for k in all_keys:
        l = leg_by.get(k)
        u = uni_by.get(k)
        if l and u:
            cls = "tie"
            tag = "both"
        elif l:
            cls = "loss"
            tag = "legacy only"
        else:
            cls = "win"
            tag = "universe only"

        def cell(o: dict[str, Any] | None, fld: str) -> str:
            if o is None:
                return "<td>—</td>"
            return f"<td>{E(o.get(fld))}</td>"

        rows.append(
            f'<tr class="{cls}"><td>{tag}</td><td class=mono>{E(k)}</td>'
            + cell(l or u, "floor_plan_name")
            + cell(l or u, "bedrooms")
            + cell(l or u, "bathrooms")
            + cell(l or u, "sqft")
            + cell(l or u, "market_rent_low")
            + cell(l or u, "market_rent_high")
            + cell(l or u, "available_date")
            + "</tr>"
        )

    if not rows:
        return "<p><em>Both pipelines extracted 0 units.</em></p>"
    return (
        '<table class="diff"><thead><tr>'
        "<th>side</th><th>key</th><th>plan</th><th>beds</th><th>baths</th><th>sqft</th>"
        "<th>rent low</th><th>rent high</th><th>avail</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


# ---------------------------------------------------------------------------
# Per-property panel
# ---------------------------------------------------------------------------


def render_property_panel(rec: dict[str, Any], anchor_id: str) -> str:
    s = rec.get("sample") or {}
    cmp_ = rec.get("comparison") or {}
    leg = rec.get("legacy") or {}
    uni = rec.get("universe") or {}
    ud = rec.get("universe_debug") or {}

    diag = _diagnosis(rec)
    if cmp_.get("regression_flag"):
        verdict_cls = "verdict-loss"
        verdict_label = "REGRESSION"
    elif cmp_.get("win_flag"):
        verdict_cls = "verdict-win"
        verdict_label = "WIN"
    else:
        verdict_cls = "verdict-tie"
        verdict_label = "TIE"

    cost_delta = float(cmp_.get("cost_delta_usd") or 0)
    unit_delta = int(cmp_.get("unit_count_delta") or 0)
    latency_delta_s = float(cmp_.get("latency_delta_ms") or 0) / 1000.0

    summary_strip = (
        f'<div class="strip">'
        f'<span class="tag {verdict_cls}">{verdict_label}</span> '
        f'<span class="diag">{E(diag)}</span> '
        f'<span class="kpi">Δunits {unit_delta:+d}</span> '
        f'<span class="kpi">Δcost ${cost_delta:+.4f}</span> '
        f'<span class="kpi">Δlatency {latency_delta_s:+.1f}s</span> '
        f'<span class="kpi">universe LLM calls {E(uni.get("llm_calls",0))}</span> '
        f"</div>"
    )

    # Two-column body: legacy | universe
    legacy_col = (
        "<h4>Legacy outcome (summary only)</h4>"
        + render_outcome_summary(leg)
    )
    universe_col = (
        "<h4>Universe outcome</h4>"
        + render_outcome_summary(uni, stop_reason=ud.get("stop_reason", ""))
    )

    body = (
        f'<div id="{E(anchor_id)}">'
        + f"<h2>{E(s.get('canonical_id',''))} · {E(s.get('property_name',''))}</h2>"
        + summary_strip
        + details("Sample / meta", render_sample_meta(rec), open_=False)
        + '<div class="two-col">'
        + f'<div class="col">{legacy_col}</div>'
        + f'<div class="col">{universe_col}</div>'
        + "</div>"
        + details(
            "Pre-rank site universe (raw gather)",
            render_pre_rank_universe(ud),
        )
        + details("LLM ranker output (universe rank)", render_llm_ranking(ud))
        + details("Initial queue (post-rank ordering)", render_initial_queue(ud))
        + details("Sub-universes (per explored link)", render_sub_universes(ud))
        + details(
            "All universe LLM calls (full prompts + raw responses)",
            render_universe_llm_calls(ud),
        )
        + details("Unit diff", render_unit_diff(rec), open_=True)
        + "</div>"
    )
    return body


# ---------------------------------------------------------------------------
# Aggregate / top-of-page
# ---------------------------------------------------------------------------


def render_aggregate(rows: list[dict[str, Any]]) -> str:
    n = len(rows)
    leg_total_cost = sum(float((r.get("legacy") or {}).get("llm_cost_usd") or 0) for r in rows)
    uni_total_cost = sum(float((r.get("universe") or {}).get("llm_cost_usd") or 0) for r in rows)
    leg_total_calls = sum(int((r.get("legacy") or {}).get("llm_calls") or 0) for r in rows)
    uni_total_calls = sum(int((r.get("universe") or {}).get("llm_calls") or 0) for r in rows)
    leg_units = sum(len((r.get("legacy") or {}).get("units") or []) for r in rows)
    uni_units = sum(len((r.get("universe") or {}).get("units") or []) for r in rows)
    wins = sum(1 for r in rows if (r.get("comparison") or {}).get("win_flag"))
    regressions = sum(1 for r in rows if (r.get("comparison") or {}).get("regression_flag"))
    ties = n - wins - regressions

    return kv_table(
        [
            ("Properties compared", n),
            ("Wins / Regressions / Ties", f"{wins} / {regressions} / {ties}"),
            ("Legacy total cost", f"${leg_total_cost:.4f}"),
            ("Universe total cost", f"${uni_total_cost:.4f}"),
            (
                "Cost delta",
                f"${uni_total_cost - leg_total_cost:+.4f} "
                f"({(uni_total_cost / max(leg_total_cost, 0.0001)):.2f}× legacy)",
            ),
            ("Legacy total LLM calls", leg_total_calls),
            ("Universe total LLM calls", uni_total_calls),
            ("Legacy total units", leg_units),
            ("Universe total units", uni_units),
            ("Units delta", f"{uni_units - leg_units:+d}"),
        ]
    )


def render_index(rows: list[dict[str, Any]]) -> str:
    out = [
        '<table class="index"><thead><tr>'
        "<th>#</th><th>verdict</th><th>ID</th><th>name</th>"
        "<th>leg-u</th><th>leg-$</th><th>uni-u</th><th>uni-$</th>"
        "<th>Δ$</th><th>stop</th><th>diagnosis</th></tr></thead><tbody>"
    ]
    for i, r in enumerate(rows, 1):
        s = r.get("sample") or {}
        cmp_ = r.get("comparison") or {}
        leg = r.get("legacy") or {}
        uni = r.get("universe") or {}
        ud = r.get("universe_debug") or {}

        if cmp_.get("regression_flag"):
            cls = "loss"
        elif cmp_.get("win_flag"):
            cls = "win"
        else:
            cls = "tie"

        cost_delta = float(uni.get("llm_cost_usd") or 0) - float(leg.get("llm_cost_usd") or 0)
        anchor = f'p{i}-{s.get("canonical_id","")}'
        out.append(
            f'<tr class="{cls}"><td>{i}</td>'
            f'<td>{cls.upper()}</td>'
            f'<td class=mono>{E(s.get("canonical_id",""))}</td>'
            f'<td><a href="#{E(anchor)}">{E((s.get("property_name") or "")[:40])}</a></td>'
            f'<td>{len(leg.get("units") or [])}</td>'
            f'<td>${float(leg.get("llm_cost_usd") or 0):.4f}</td>'
            f'<td>{len(uni.get("units") or [])}</td>'
            f'<td>${float(uni.get("llm_cost_usd") or 0):.4f}</td>'
            f'<td>${cost_delta:+.4f}</td>'
            f'<td>{E(ud.get("stop_reason",""))}</td>'
            f'<td>{E(_diagnosis(r))}</td></tr>'
        )
    out.append("</tbody></table>")
    return "".join(out)


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------


def _confidence_bg(c: float) -> str:
    """Linear gradient from red (0.0) → yellow (0.5) → green (1.0)."""
    c = max(0.0, min(1.0, c))
    if c < 0.5:
        # red→yellow
        r = 255
        g = int(80 + (175 * (c / 0.5)))
        b = 80
    else:
        # yellow→green
        r = int(255 - (180 * ((c - 0.5) / 0.5)))
        g = 230
        b = 80
    return f"rgba({r},{g},{b},0.45)"


# ---------------------------------------------------------------------------
# Top-level CSS (inline so the report stays single-file)
# ---------------------------------------------------------------------------


STYLE = """
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 14px;
  line-height: 1.5;
  margin: 0;
  padding: 24px;
  background: #f6f7f9;
  color: #1d2333;
}
h1 { margin-top: 0; }
h2 { margin-top: 32px; padding-top: 16px; border-top: 2px solid #d0d6e0; }
h3, h4 { margin: 12px 0 4px 0; }
.mono, code, pre { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
pre {
  background: #11151c;
  color: #e3e8f0;
  padding: 10px 12px;
  border-radius: 4px;
  font-size: 12px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 400px;
  overflow-y: auto;
}
.kpi {
  display: inline-block;
  margin-right: 12px;
  padding: 2px 8px;
  border-radius: 3px;
  background: #fff;
  border: 1px solid #d0d6e0;
}
.tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 3px;
  font-weight: 600;
  margin-right: 12px;
}
.verdict-win  { background: #d6f4dc; color: #0c5d2a; }
.verdict-loss { background: #ffd7d7; color: #8b0a0a; }
.verdict-tie  { background: #e6e9f0; color: #2c344a; }
.diag { color: #404a64; }
.strip { padding: 8px 0 12px 0; }

.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 12px 0; }
.col { background: #fff; padding: 12px 16px; border-radius: 6px; border: 1px solid #d8dee8; }
@media (max-width: 900px) { .two-col { grid-template-columns: 1fr; } }

table { border-collapse: collapse; margin: 6px 0 12px 0; max-width: 100%; }
table th, table td { padding: 4px 10px; border: 1px solid #d8dee8; text-align: left; vertical-align: top; }
table th { background: #eef1f6; font-weight: 600; }
table.kv th { width: 220px; }
table.diff td:nth-child(2),
table.universe td:first-child,
table.queue td:last-child,
table.ranking td:nth-child(4) {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 12px;
  word-break: break-all;
  max-width: 600px;
}

table.index th, table.index td { white-space: nowrap; }
table.index td:nth-child(11) { white-space: normal; max-width: 480px; }
tr.win  { background: rgba(52, 199, 89, 0.10); }
tr.loss { background: rgba(255, 59, 48, 0.10); }
tr.tie  { background: rgba(0, 0, 0, 0.0); }
.err { color: #8b0a0a; }

details { background: #fff; padding: 6px 10px; margin: 4px 0; border: 1px solid #d8dee8; border-radius: 4px; }
details > summary { cursor: pointer; user-select: none; padding: 4px 0; font-weight: 500; }
details[open] > summary { border-bottom: 1px solid #eef1f6; margin-bottom: 8px; }
details details { background: #fbfcfd; }
"""


# ---------------------------------------------------------------------------
# Top-level renderer
# ---------------------------------------------------------------------------


def render_report(rows: list[dict[str, Any]], title: str) -> str:
    rows_sorted = sorted(
        rows,
        key=lambda r: (
            not (r.get("comparison") or {}).get("regression_flag"),
            not (r.get("comparison") or {}).get("win_flag"),
            (r.get("sample") or {}).get("canonical_id", ""),
        ),
    )

    sections = [
        f"<h1>{E(title)}</h1>",
        "<h2>Aggregate</h2>",
        render_aggregate(rows_sorted),
        "<h2>Property index (sorted: regressions → wins → ties)</h2>",
        render_index(rows_sorted),
        "<h2>Per-property detail</h2>",
    ]

    for i, r in enumerate(rows_sorted, 1):
        s = r.get("sample") or {}
        anchor = f'p{i}-{s.get("canonical_id","")}'
        sections.append(render_property_panel(r, anchor))

    return (
        "<!doctype html><html><head>"
        '<meta charset="utf-8">'
        f"<title>{E(title)}</title>"
        f"<style>{STYLE}</style>"
        "</head><body>"
        + "".join(sections)
        + "</body></html>"
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("dirs", nargs="+", help="experiment directories to combine")
    p.add_argument("--out", required=True, help="output HTML path")
    p.add_argument(
        "--title",
        default="Universe vs Legacy — analysis report",
        help="Top-of-page heading",
    )
    args = p.parse_args()

    dirs = [Path(d) for d in args.dirs]
    rows = _load_records(dirs)
    if not rows:
        print("no per_property records found", file=sys.stderr)
        return 1

    html_text = render_report(rows, args.title)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_text, encoding="utf-8")
    size_kb = out.stat().st_size / 1024
    print(f"wrote {out}  ({len(rows)} properties, {size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
