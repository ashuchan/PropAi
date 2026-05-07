"""Email a senior-architect SOLID refactor plan for ma_poc/.

Reuses the data-provider connection helpers and Gmail MCP send path from
``email_daily_report.py`` so this script ships under the same recipients,
sender, and credentials configuration (REPORT_RECIPIENTS / GMAIL_MCP_*).

Body content is static — derived from a one-off architectural review of
the bulky modules under ma_poc/ (jugnu_runner, entrata, daily_runner,
retry_runner, generic adapter, llm_extractor, scrape_properties).

Usage
-----

    # Send to REPORT_RECIPIENTS configured in ma_poc/.env
    python scripts/email_refactor_plan.py

    # Render only — print HTML to stdout, do not send
    python scripts/email_refactor_plan.py --dry-run

    # Override recipients
    python scripts/email_refactor_plan.py --recipients a@x.com,b@y.com
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_MA_POC_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _MA_POC_ROOT.parent
for _p in (_MA_POC_ROOT, _REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import os  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

from scripts.email.daily import (  # noqa: E402
    _html_to_plain,
    _parse_recipients,
    _send_via_mcp,
)

log = logging.getLogger("email_refactor_plan")


_STYLE = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       font-size: 14px; line-height: 1.55; color: #1d2333; background: #f6f7f9;
       margin: 0; padding: 18px; }
.wrap { max-width: 880px; margin: 0 auto; background: #fff; border: 1px solid #d8dee8;
        border-radius: 8px; padding: 24px 28px; }
h1 { font-size: 21px; margin: 0 0 4px 0; }
h2 { font-size: 16px; margin: 26px 0 8px 0; padding-top: 14px;
     border-top: 2px solid #d0d6e0; }
h3 { font-size: 14px; margin: 14px 0 6px 0; color: #1e2a44; }
.sub { color: #6b7280; font-size: 12px; margin-bottom: 14px; }
.kpis { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 4px 0; }
.kpi { padding: 6px 12px; border-radius: 4px; background: #1e2632; color: #fff;
       font-size: 13px; }
.kpi.bad { background: #8b0a0a; }
.kpi.warn { background: #8a5e0a; }
.kpi.ok { background: #0c5d2a; }
.kpi b { color: #fff; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 10px 0; font-size: 13px; }
th, td { padding: 6px 9px; border: 1px solid #d8dee8; text-align: left; vertical-align: top; }
th { background: #eef1f6; font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.bad td { background: rgba(255,59,48,0.08); }
tr.warn td { background: rgba(255,149,0,0.07); }
tr.ok td { background: rgba(52,199,89,0.07); }
.code { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
        font-size: 12px; }
.foot { margin-top: 26px; color: #64748b; font-size: 11px; }
.cause { background: #f4f7fb; border-left: 4px solid #2563eb; padding: 10px 14px;
         margin: 8px 0; border-radius: 0 4px 4px 0; }
.cause h3 { margin: 0 0 4px 0; font-size: 13px; }
.cause p { margin: 0; }
ul, ol { margin: 4px 0 10px 22px; }
li { margin: 3px 0; }
.tag { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px;
       background: #e2e8f0; color: #1e293b; margin-right: 4px; }
.tag.srp { background: #dbeafe; color: #1e3a8a; }
.tag.ocp { background: #dcfce7; color: #14532d; }
.tag.lsp { background: #fef3c7; color: #78350f; }
.tag.isp { background: #fce7f3; color: #831843; }
.tag.dip { background: #ede9fe; color: #4c1d95; }
"""


_HTML = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<title>ma_poc/ — SOLID refactor plan</title>'
    f'<style>{_STYLE}</style></head><body><div class="wrap">'

    '<h1>ma_poc/ — SOLID refactor plan</h1>'
    '<div class="sub">Senior-architect / distributed-systems review of the '
    'bulky modules in <span class="code">ma_poc/</span>. Carves the current '
    'monoliths (jugnu_runner, entrata scraper, daily_runner, retry_runner, '
    'generic adapter, llm_extractor) into bounded, single-responsibility '
    'modules so the codebase is testable, swappable, and ready to run on '
    'distributed workers.</div>'

    '<div class="kpis">'
    '<span class="kpi bad"><b>7</b> god-modules &gt; 800 lines</span>'
    '<span class="kpi bad"><b>~12,000</b> lines in top-7 alone</span>'
    '<span class="kpi warn"><b>2</b> parallel pipelines (legacy + Jugnu)</span>'
    '<span class="kpi"><b>8</b> bounded contexts proposed</span>'
    '<span class="kpi ok"><b>8</b> incremental PRs (no big bang)</span>'
    '<span class="kpi ok"><b>&lt;400</b> lines per refactored module target</span>'
    '</div>'

    # ── Section 1: Diagnosis ──
    '<h2>1. Diagnosis — where the bulk lives</h2>'
    '<div class="sub">Top-7 by line count, with the SOLID violation that '
    'drives each split. Line numbers are from the current main branch '
    '(2026-05-06).</div>'
    '<table><thead><tr>'
    '<th>Module</th><th class="num">Lines</th>'
    '<th>Primary SOLID violation</th><th>Smell</th>'
    '</tr></thead><tbody>'
    '<tr class="bad"><td><span class="code">scripts/entrata.py</span></td>'
    '<td class="num">3,156</td>'
    '<td><span class="tag srp">SRP</span> God function</td>'
    '<td><span class="code">scrape()</span> spans ~700 lines, 7 phases inlined, '
    '100+ state vars, 4 nested LLM fallbacks.</td></tr>'
    '<tr class="bad"><td><span class="code">pms/adapters/generic.py</span></td>'
    '<td class="num">2,145</td>'
    '<td><span class="tag srp">SRP</span> <span class="tag dip">DIP</span> '
    'God class</td>'
    '<td><span class="code">GenericAdapter._extract_inner()</span> has 7 '
    'sub-tiers (0–6c) as inline conditionals. LLM, prompt strings, and '
    'merge rules all baked in.</td></tr>'
    '<tr class="bad"><td><span class="code">scripts/jugnu_runner.py</span></td>'
    '<td class="num">1,608</td>'
    '<td><span class="tag srp">SRP</span> Monolithic orchestrator</td>'
    '<td><span class="code">run_jugnu()</span> wires 5 layers + CSV + format '
    'routing + report. <span class="code">_process_property()</span> alone is '
    '270 lines.</td></tr>'
    '<tr class="bad"><td><span class="code">scripts/daily_runner.py</span></td>'
    '<td class="num">1,410</td>'
    '<td><span class="tag srp">SRP</span> Pipeline-as-function</td>'
    '<td><span class="code">run_daily()</span> is 827 lines: CSV → identity → '
    'dedup → state load → concurrent scrape → diff → carry-fwd → record build → '
    'report → state write.</td></tr>'
    '<tr class="bad"><td><span class="code">scripts/retry_runner.py</span></td>'
    '<td class="num">1,097</td>'
    '<td><span class="tag dip">DIP</span> Tight coupling via re-exports</td>'
    '<td>Imports 6 internals from <span class="code">daily_runner</span> '
    '(<span class="code">_scrape_in_thread</span>, <span class="code">'
    'TARGET_PROPERTY_FIELDS</span>, …). 700-line <span class="code">'
    'run_retry()</span> mirrors <span class="code">run_daily()</span>.</td></tr>'
    '<tr class="warn"><td><span class="code">scripts/scrape_properties.py</span></td>'
    '<td class="num">959</td>'
    '<td><span class="tag ocp">OCP</span> Host-specific parsers scattered</td>'
    '<td>Four <span class="code">_*_units_from_body()</span> functions '
    '(SightMap / RealPage / AvalonBay / generic) — no common interface. '
    'Same logic re-implemented in <span class="code">entrata.py</span>.</td></tr>'
    '<tr class="warn"><td><span class="code">services/llm_extractor.py</span></td>'
    '<td class="num">865</td>'
    '<td><span class="tag isp">ISP</span> Fat interface</td>'
    '<td>Three modes (<span class="code">extract_with_llm</span>, <span '
    'class="code">analyze_api_with_llm</span>, <span class="code">'
    'analyze_dom_with_llm</span>) on one module surface. Prompt strings '
    'hardcoded. Provider not injectable.</td></tr>'
    '</tbody></table>'

    # ── Section 2: Target architecture ──
    '<h2>2. Target architecture — 8 bounded contexts</h2>'
    '<div class="sub">Each context has one reason to change and a stable '
    'public interface. Internal classes stay private. No context imports '
    'siblings\' internals.</div>'

    '<div class="cause"><h3>1. <span class="code">orchestration/</span> — '
    'pipeline composition only</h3><p>'
    '<span class="code">pipeline.py</span> (generic <span class="code">'
    'Pipeline[T]</span> with <span class="code">Stage</span> protocol), '
    '<span class="code">stages/</span> (one stage class per concern), '
    '<span class="code">runners/</span> (thin DailyRunner, JugnuRunner, '
    'RetryRunner — each &lt;150 lines), <span class="code">'
    'concurrency_manager.py</span> (owns AsyncPool + ThreadedPool from '
    'the current <span class="code">concurrency.py</span>), <span class="code">'
    'cli.py</span> (argparse isolated from business logic).'
    '</p></div>'

    '<div class="cause"><h3>2. <span class="code">extraction/engine/</span> — '
    'replaces <span class="code">scripts/entrata.py</span></h3><p>'
    '<span class="code">browser_session.py</span> (Playwright lifecycle, '
    'context-per-property, <span class="code">finally</span>-close), '
    '<span class="code">network_capture.py</span> (XHR/fetch interception, '
    'instance-scoped buffer per Bug-Hunt #2), <span class="code">'
    'noise_filter.py</span> (<span class="code">_FALSE_POSITIVE_HOSTS</span> + '
    '<span class="code">_FALSE_POSITIVE_PATH_FRAGMENTS</span> + profile '
    'blocklists), <span class="code">link_explorer.py</span> (prioritization '
    '+ bounded BFS), <span class="code">metadata_extractor.py</span> '
    '(name/address/phone/geo from og:* + JSON-LD + footers). The 7 phases '
    'become 7 classes under <span class="code">phases/</span> (<span '
    'class="code">Phase1HomepageLoad … Phase7Finalize</span>) sharing a '
    '<span class="code">PhaseContext</span> dataclass. <span class="code">'
    'Scraper</span> facade composes them via injected <span class="code">'
    'PhaseRegistry</span>.'
    '</p></div>'

    '<div class="cause"><h3>3. <span class="code">pms/adapters/generic/</span> — '
    'split <span class="code">generic.py</span></h3><p>'
    '<span class="code">tiers/</span> (one class per sub-tier 0–6c, each '
    '&lt;300 lines, all implement <span class="code">Tier</span> protocol), '
    '<span class="code">tier_orchestrator.py</span> (sequencer + the current '
    '<span class="code">_assess_and_decide()</span> as injectable strategy), '
    '<span class="code">merger.py</span> (the current <span class="code">'
    '_merge_field_values()</span> as a composable rule chain), <span '
    'class="code">prompt_loader.py</span> (loads from <span class="code">'
    'config/prompts/*.txt</span> — no inline 200-line strings), <span '
    'class="code">gates.py</span> (<span class="code">skip_llm</span> / '
    '<span class="code">LLM_GATE_RELAXED</span> logic).'
    '</p></div>'

    '<div class="cause"><h3>4. <span class="code">extraction/parsers/</span> — '
    'consolidate host-specific parsers</h3><p>One module per host, all '
    'implementing a common <span class="code">UnitParser</span> interface: '
    '<span class="code">parsers/sightmap.py</span>, <span class="code">'
    'parsers/realpage.py</span>, <span class="code">parsers/avalon.py</span>, '
    '<span class="code">parsers/onesite.py</span>, <span class="code">'
    'parsers/generic_api.py</span>. <span class="code">parser_registry.py</span> '
    'maps host fingerprint → parser. <span class="tag ocp">OCP</span> Adding '
    'a new PMS = drop a new file; no edits to <span class="code">scrape()</span>.'
    '</p></div>'

    '<div class="cause"><h3>5. <span class="code">services/llm/</span> — '
    'explode <span class="code">llm_extractor.py</span></h3><p>'
    '<span class="code">prompts.py</span> (templates loaded from '
    '<span class="code">config/prompts/</span>), <span class="code">'
    'monolithic_extractor.py</span>, <span class="code">api_analyzer.py</span>, '
    '<span class="code">dom_analyzer.py</span> (each implements its own '
    'narrow interface, <span class="tag isp">ISP</span>), <span class="code">'
    'mapping_replay.py</span> (deterministic replay of <span class="code">'
    'LlmFieldMapping</span> — zero-cost path), <span class="code">'
    'field_normalizer.py</span> (the current 90-line <span class="code">'
    '_normalize_units</span> as a configuration-driven mapper), <span '
    'class="code">retry_policy.py</span> (tenacity wrapper with jitter — '
    'closes Bug-Hunt #13 retry storms).'
    '</p></div>'

    '<div class="cause"><h3>6. <span class="code">state/</span> — pull state '
    'concerns out of <span class="code">daily_runner.py</span></h3><p>'
    '<span class="code">csv_loader.py</span> (UTF-8-BOM read, schema '
    'inference), <span class="code">identity_resolver.py</span> (the 5-tier '
    'cascade as a chain of <span class="code">Resolver</span> implementations '
    '— <span class="tag ocp">OCP</span> for new fingerprint sources), <span '
    'class="code">dedup_detector.py</span> (hard / soft / geo duplicates), '
    '<span class="code">record_builder.py</span> (the current <span '
    'class="code">build_property_record</span>, separated from CSV '
    'enrichment), <span class="code">state_diff.py</span> '
    '(new/updated/unchanged/disappeared logic), <span class="code">'
    'carry_forward.py</span> (failed-scrape replay rules).'
    '</p></div>'

    '<div class="cause"><h3>7. <span class="code">reporting/</span> — '
    'consolidate the <span class="code">*_report.py</span> family</h3><p>'
    'Move <span class="code">scripts/scrape_report.py</span>, '
    '<span class="code">scripts/generate_daily_report.py</span>, '
    '<span class="code">scripts/health_report.py</span> into '
    '<span class="code">reporting/</span>. Inside: <span class="code">'
    'aggregator.py</span> (replaces <span class="code">aggregate_unit_stats</span>; '
    'tracks stats during the main loop instead of a second pass), <span '
    'class="code">markdown_writer.py</span>, <span class="code">'
    'json_writer.py</span>, <span class="code">verdict.py</span> (already '
    'exists — keep), <span class="code">slo_section.py</span>.'
    '</p></div>'

    '<div class="cause"><h3>8. <span class="code">scripts/</span> — collapse '
    'to thin entrypoints</h3><p>Each runner becomes &lt;150 lines: load env, '
    'parse args, build a <span class="code">Pipeline</span>, run, exit. '
    '<span class="code">entrata.py</span> retired (deprecated wrapper for one '
    'release calling new <span class="code">Scraper</span>). <span '
    'class="code">retry_runner.py</span> becomes a <span class="code">'
    'RetryPipeline</span> that extends <span class="code">DailyPipeline</span> '
    'with a row-filter strategy — eliminates the 6-symbol re-export coupling.'
    '</p></div>'

    # ── Section 3: SOLID applied ──
    '<h2>3. SOLID — what each principle buys us</h2>'
    '<table><thead><tr><th>Principle</th><th>Today\'s pain</th>'
    '<th>After refactor</th></tr></thead><tbody>'
    '<tr><td><span class="tag srp">SRP</span> Single Responsibility</td>'
    '<td><span class="code">scrape()</span> 700 lines across 7 phases; '
    '<span class="code">run_daily()</span> 827 lines across 12 concerns.</td>'
    '<td>Every module ≤400 lines, ≤3 public methods. Each phase, parser, '
    'and stage is independently testable.</td></tr>'
    '<tr><td><span class="tag ocp">OCP</span> Open/Closed</td>'
    '<td>New PMS = edit <span class="code">CONTAINER_SELECTORS</span>, '
    '<span class="code">_UNIT_ID_KEYS</span>, <span class="code">'
    'ENTRATA_API_PATTERNS</span> in <span class="code">entrata.py</span>.</td>'
    '<td>New PMS = drop <span class="code">parsers/&lt;host&gt;.py</span>, '
    'register in <span class="code">parser_registry.py</span>. No edits to '
    'extraction engine.</td></tr>'
    '<tr><td><span class="tag lsp">LSP</span> Liskov</td>'
    '<td>No interfaces today — adapters carry adapter-specific debug fields '
    'in result dicts (<span class="code">_winning_url</span>, <span '
    'class="code">_llm_interactions</span>).</td>'
    '<td>All <span class="code">UnitParser</span>, <span class="code">'
    'PhaseStage</span>, <span class="code">Tier</span>, <span class="code">'
    'LLMExtractor</span> implementations substitutable behind their '
    'protocol; debug data on the <span class="code">PhaseContext</span> not '
    'the result.</td></tr>'
    '<tr><td><span class="tag isp">ISP</span> Interface Segregation</td>'
    '<td><span class="code">llm_extractor.py</span> exposes 3 unrelated '
    'modes; callers depend on the whole module.</td>'
    '<td>Three narrow protocols: <span class="code">MonolithicExtractor</span>, '
    '<span class="code">ApiAnalyzer</span>, <span class="code">DomAnalyzer</span>. '
    'Generic adapter takes only what it uses.</td></tr>'
    '<tr><td><span class="tag dip">DIP</span> Dependency Inversion</td>'
    '<td>Profiles read from <span class="code">config/profiles/</span> '
    'directly inside <span class="code">scrape()</span>; LLM provider '
    'imported at module top.</td>'
    '<td>Orchestrators depend on <span class="code">ProfileStore</span>, '
    '<span class="code">StateStore</span>, <span class="code">LLMExtractor</span>, '
    '<span class="code">ProxyPool</span> protocols. Concrete impls injected '
    'at the runner. Swap to Postgres / Redis / cloud providers without '
    'touching the engine.</td></tr>'
    '</tbody></table>'

    # ── Section 4: Migration plan ──
    '<h2>4. Migration plan — 8 incremental PRs, no big bang</h2>'
    '<div class="sub">Each PR is shippable in isolation. Each ends with the '
    'CLAUDE.md gates: full <span class="code">pytest</span> green, '
    '<span class="code">mypy --strict</span> + <span class="code">ruff</span> '
    'clean, <span class="code">smoke_test.py</span> passing 5/5, and a 50-property '
    'golden-run diff against the prior baseline output.</div>'
    '<table><thead><tr><th>PR</th><th>Scope</th><th>Risk</th>'
    '<th>Behavioural-parity check</th></tr></thead><tbody>'
    '<tr><td><b>PR-1</b></td>'
    '<td>Extract <span class="code">BrowserSession</span> + <span class="code">'
    'NetworkCapture</span> + <span class="code">NoiseFilter</span> from '
    '<span class="code">entrata.py</span>. No behavioural change — only '
    'structural.</td>'
    '<td class="ok">Low</td>'
    '<td>Byte-equal <span class="code">properties.json</span> on 50-row '
    'golden run.</td></tr>'
    '<tr><td><b>PR-2</b></td>'
    '<td>Extract <span class="code">Phase1HomepageLoad</span> and <span '
    'class="code">Phase2NoiseFilter</span> as classes. Wire through new '
    '<span class="code">PhaseRegistry</span>.</td>'
    '<td class="warn">Med</td>'
    '<td>Per-property unit count parity; tier distribution parity (PR-03 AC '
    'in CLAUDE.md).</td></tr>'
    '<tr><td><b>PR-3</b></td>'
    '<td>Extract Phases 3–7. <span class="code">scrape()</span> becomes a '
    '15-line orchestration of phase classes. Retire <span class="code">'
    'entrata.py</span> as a deprecated wrapper.</td>'
    '<td class="bad">High</td>'
    '<td>50-property golden diff + Vision banner-capture rate ≥ 95% (PR-04 '
    'AC).</td></tr>'
    '<tr><td><b>PR-4</b></td>'
    '<td>Split <span class="code">generic.py</span> into <span class="code">'
    'tiers/</span> + <span class="code">tier_orchestrator.py</span>. Move '
    'prompt strings to <span class="code">config/prompts/</span>.</td>'
    '<td class="warn">Med</td>'
    '<td>LLM-call count per property unchanged ±5%. <span class="code">'
    'cost_ledger.db</span> spend within ±$0.05 per 50-property run.</td></tr>'
    '<tr><td><b>PR-5</b></td>'
    '<td>Consolidate host-specific parsers under <span class="code">'
    'extraction/parsers/</span> behind <span class="code">UnitParser</span> '
    'protocol. Remove the duplicate copies in <span class="code">'
    'scrape_properties.py</span> and <span class="code">entrata.py</span>.</td>'
    '<td class="warn">Med</td>'
    '<td>SightMap + RealPage + AvalonBay unit counts per property '
    'unchanged.</td></tr>'
    '<tr><td><b>PR-6</b></td>'
    '<td>Refactor <span class="code">llm_extractor.py</span> into '
    '<span class="code">services/llm/</span>. Three narrow protocols, '
    'externalized prompts, retry policy module. Provider injected via '
    'factory.</td>'
    '<td class="warn">Med</td>'
    '<td>Profile-replay determinism: pre/post-PR <span class="code">'
    'apply_saved_mapping()</span> output byte-equal on 100 saved '
    'mappings.</td></tr>'
    '<tr><td><b>PR-7</b></td>'
    '<td>Decompose <span class="code">daily_runner.py</span> into <span '
    'class="code">state/</span> + <span class="code">orchestration/</span> + '
    '<span class="code">reporting/</span>. <span class="code">run_daily()</span> '
    'becomes a 30-line <span class="code">DailyPipeline.execute()</span>.</td>'
    '<td class="bad">High</td>'
    '<td><span class="code">property_index.json</span> + <span class="code">'
    'unit_index.json</span> structural diff = empty after a full run.</td></tr>'
    '<tr><td><b>PR-8</b></td>'
    '<td>Collapse <span class="code">retry_runner.py</span> into a <span '
    'class="code">RetryPipeline</span> = <span class="code">DailyPipeline</span> '
    '+ row-filter strategy. Drop the 6-symbol re-export coupling.</td>'
    '<td class="ok">Low</td>'
    '<td>Resume + retry-errors modes produce identical ledger merges as '
    'today on a synthetic run with seeded failures.</td></tr>'
    '</tbody></table>'

    # ── Section 5: Metrics ──
    '<h2>5. Metrics — before and after</h2>'
    '<table><thead><tr><th>Module</th><th class="num">Before</th>'
    '<th class="num">After (target)</th><th>Strategy</th></tr></thead><tbody>'
    '<tr><td><span class="code">scripts/entrata.py</span></td>'
    '<td class="num">3,156</td>'
    '<td class="num">retired</td>'
    '<td>Split into ~12 modules &lt;300 lines each under <span class="code">'
    'extraction/engine/</span>.</td></tr>'
    '<tr><td><span class="code">pms/adapters/generic.py</span></td>'
    '<td class="num">2,145</td>'
    '<td class="num">&lt;400</td>'
    '<td>Orchestrator only; 7 tier modules &lt;300 each.</td></tr>'
    '<tr><td><span class="code">scripts/jugnu_runner.py</span></td>'
    '<td class="num">1,608</td>'
    '<td class="num">&lt;200</td>'
    '<td>Entrypoint only; pipeline + stages &lt;250 each.</td></tr>'
    '<tr><td><span class="code">scripts/daily_runner.py</span></td>'
    '<td class="num">1,410</td>'
    '<td class="num">&lt;200</td>'
    '<td>Entrypoint only; 5 stage modules &lt;300 each in <span class="code">'
    'state/</span>.</td></tr>'
    '<tr><td><span class="code">scripts/retry_runner.py</span></td>'
    '<td class="num">1,097</td>'
    '<td class="num">&lt;150</td>'
    '<td>Extends <span class="code">DailyPipeline</span> with row-filter '
    'strategy.</td></tr>'
    '<tr><td><span class="code">scripts/scrape_properties.py</span></td>'
    '<td class="num">959</td>'
    '<td class="num">&lt;200</td>'
    '<td>Host parsers move to <span class="code">extraction/parsers/</span>; '
    'this becomes a thin batch entrypoint.</td></tr>'
    '<tr><td><span class="code">services/llm_extractor.py</span></td>'
    '<td class="num">865</td>'
    '<td class="num">&lt;150</td>'
    '<td>Facade only; 4 sub-modules &lt;250 each in <span class="code">'
    'services/llm/</span>.</td></tr>'
    '<tr><td><b>Top-7 total</b></td>'
    '<td class="num"><b>~12,240</b></td>'
    '<td class="num"><b>~3,500</b></td>'
    '<td>~70% reduction via deduplication (host parsers, rent extraction, '
    'identity resolution duplicated today across <span class="code">'
    'entrata.py</span>, <span class="code">scrape_properties.py</span>, '
    'and <span class="code">generic.py</span>).</td></tr>'
    '</tbody></table>'

    # ── Section 6: Distributed-systems wins ──
    '<h2>6. Distributed-systems concerns this refactor unblocks</h2>'
    '<div class="sub">Carving these seams is not just hygiene — it removes '
    'specific blockers for running ma_poc on distributed workers.</div>'
    '<ol>'
    '<li><b>Stateless scrape units.</b> Today <span class="code">entrata.py</span> '
    'reads/writes <span class="code">config/profiles/{canonical_id}.json</span> '
    'directly. With <span class="code">ProfileStore</span> behind a protocol, '
    'scrapes can run on Cloud Run / K8s jobs against a Postgres-backed or '
    'Redis-backed profile store — no shared filesystem required.</li>'
    '<li><b>Shard-safe upserts.</b> The memory note '
    '<span class="code">project_postgres_retention_policy</span> records that '
    'today the same property scraped by two shards causes pass-2 to flip '
    'pass-1\'s units to <span class="code">disappeared_since</span> '
    'immediately. Pulling state into <span class="code">state/</span> behind a '
    'transactional <span class="code">StateStore</span> interface makes '
    '<span class="code">(canonical_id, run_date)</span> tracking a first-class '
    'concern.</li>'
    '<li><b>Adaptive backpressure.</b> <span class="code">'
    'ConcurrencyManager</span> isolates pool sizing. Today '
    '<span class="code">MAX_CONCURRENT_BROWSERS</span> is static; with the '
    'manager owning rate-limiter feedback, we can throttle on proxy 429s '
    'without touching <span class="code">scrape()</span>.</li>'
    '<li><b>Replayable extraction.</b> Phase-as-class lets us run a single '
    'phase against cached <span class="code">data/raw_html/</span> + '
    '<span class="code">raw_api/</span> snapshots — closes the debugging '
    'loop for accuracy regressions without re-fetching.</li>'
    '<li><b>Multi-tenant safety.</b> Once <span class="code">ProfileStore</span> '
    'and <span class="code">StateStore</span> are protocols, per-tenant '
    'namespacing is a constructor argument, not a code change.</li>'
    '<li><b>Provider hot-swap.</b> <span class="code">LLMExtractor</span> '
    'protocol decouples adapter logic from Anthropic vs. Azure vs. (future) '
    'on-prem; cost-routing decisions move to a single dispatch module.</li>'
    '</ol>'

    # ── Section 7: Risks ──
    '<h2>7. Risk register + mitigations</h2>'
    '<div class="cause"><h3>R1 — Behavioural drift in <span class="code">'
    'scrape()</span></h3><p>The 7-phase function has accumulated edge cases '
    '(HTTP→HTTPS normalization, <span class="code">MAX_CRAWL_PAGES=10</span>, '
    'expander click swallows exceptions, redirect capture for portal '
    'iframes). <b>Mitigation:</b> snapshot a 50-property golden run before '
    'PR-1 and assert byte-equal <span class="code">properties.json</span> '
    'after each PR through PR-3.</p></div>'
    '<div class="cause"><h3>R2 — Profile-format compatibility</h3><p>'
    'Per-property profiles in <span class="code">config/profiles/</span> '
    'must deserialize cleanly against new code. <b>Mitigation:</b> add a '
    'version shim in <span class="code">profile_store.py</span>; do not '
    'change the on-disk schema in this refactor.</p></div>'
    '<div class="cause"><h3>R3 — Test-coverage gap on <span class="code">'
    'pms/adapters/generic.py</span></h3><p>The exploration showed only ~4 '
    'tests under <span class="code">tests/pms/</span>. Splitting a 2,145-line '
    'class behind that thin a coverage net is dangerous. '
    '<b>Mitigation:</b> add a baseline integration test asserting unit '
    'counts on 10 representative URLs <i>before</i> PR-4 ships.</p></div>'
    '<div class="cause"><h3>R4 — Two pipelines stay separate</h3><p>'
    'Memory says Jugnu is the recommended path, but <span class="code">'
    'daily_runner.py</span> is still in production use. <b>Mitigation:</b> '
    'do <i>not</i> attempt a unification in this refactor. Both runners '
    'consume the same <span class="code">extraction/</span>, <span class="code">'
    'state/</span>, <span class="code">reporting/</span> modules but '
    'remain distinct entrypoints.</p></div>'
    '<div class="cause"><h3>R5 — Postgres retention contract</h3><p>'
    'Memory <span class="code">project_postgres_retention_policy</span> '
    'pins a 3-day rolling cap and the v2-strict schema. <b>Mitigation:</b> '
    '<span class="code">StateStore</span> protocol must keep '
    '<span class="code">_apply_retention()</span> invariants intact; '
    '<span class="code">sync_run_to_pg.py</span> stays untouched in PR-1 '
    'through PR-7.</p></div>'

    # ── Section 8: Out of scope ──
    '<h2>8. Explicitly out of scope</h2>'
    '<ul>'
    '<li>Merging legacy + Jugnu pipelines (separate, larger initiative).</li>'
    '<li>Changing the on-disk profile or state schemas.</li>'
    '<li>Migrating <span class="code">extraction/</span> Phase A '
    'BeautifulSoup stack (already inactive — leave as-is or archive '
    'separately).</li>'
    '<li>Frontend / TS API changes (different bounded context).</li>'
    '<li>Postgres schema migrations beyond what each PR\'s tests need.</li>'
    '</ul>'

    '<div class="foot">Sent automatically by '
    '<span class="code">ma_poc/scripts/email_refactor_plan.py</span>. '
    'Inventory derived from a senior-architect read of the bulky modules; '
    'send path reuses the Gmail MCP wiring from '
    '<span class="code">email_daily_report.py</span>.</div>'

    '</div></body></html>'
)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv(_MA_POC_ROOT / ".env")

    parser = argparse.ArgumentParser(
        description="Email the ma_poc/ SOLID refactor plan."
    )
    parser.add_argument("--recipients", default=None,
                        help="Comma-separated. Defaults to REPORT_RECIPIENTS env var.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Render HTML to stdout and skip the Gmail MCP send.")
    args = parser.parse_args(argv)

    recipients = _parse_recipients(args.recipients) or _parse_recipients(
        os.getenv("REPORT_RECIPIENTS")
    )
    if not recipients and not args.dry_run:
        log.error("No recipients. Set REPORT_RECIPIENTS in .env or pass --recipients.")
        return 1

    sender_name = os.getenv("REPORT_SENDER_NAME") or "PropAi Architecture Reviews"
    subject = "ma_poc/ — SOLID refactor plan (jugnu_runner / scraper / retry / generic adapter)"

    if args.dry_run:
        sys.stdout.write(_HTML)
        sys.stdout.write("\n")
        log.info("Dry run — would send to: %s", ", ".join(recipients) or "(none)")
        return 0

    try:
        result = asyncio.run(
            _send_via_mcp(
                recipients=recipients,
                subject=subject,
                html_body=_HTML,
                sender_name=sender_name,
            )
        )
    except Exception as exc:
        log.exception("Gmail MCP send failed: %s", exc)
        return 1

    if result.get("isError"):
        log.error("Gmail MCP returned an error: %s", result.get("content"))
        return 1

    log.info(
        "Sent refactor plan to %d recipient(s). MCP response: %s",
        len(recipients), result.get("content") or "(empty)",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Plain-text fallback uses the same helper as the daily report.
_ = _html_to_plain
