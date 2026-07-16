"""Run-level report builder — produces markdown + JSON summary.

Consumes cost ledger, SLO watcher, and all property results to produce
the run-level summary report.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ``resolve_verdict`` lives in reporting/verdict.py so both run_report
# and slo_watcher can share the dual-source resolution semantics. See
# Bug A v0.2 in docs/2026_05_11_regressions_fix_design.md.
from ma_poc.reporting.verdict import resolve_verdict

log = logging.getLogger(__name__)


# Event kinds we use to classify failures. Source of truth:
# ma_poc/observability/events.py.
_BOT_KINDS = frozenset({"fetch.bot_blocked"})
_CAPTCHA_KINDS = frozenset({"fetch.captcha_detected"})
_EMIT_KIND = "output.property_emitted"


def _scan_event_ledger(
    run_dir: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    """Read events.jsonl and return (bot_blocked_by_pid, captcha_by_pid, verdict_by_pid).

    Each value in the first two dicts is keyed by property_id and holds
    the first-seen event payload (url, attempt, ts, etc.) for downstream
    reporting. ``verdict_by_pid`` is the canonical per-property verdict
    captured from the ``output.property_emitted`` event, which the runner
    emits at the same moment it writes ``_meta.verdict`` to the result
    dict. Both sources should agree by construction; events is the
    authoritative one because it is emitted unconditionally regardless of
    downstream serialisation. ``run_report.build`` prefers events over
    ``_meta.verdict`` to make the headline metric robust against any
    ``_meta`` corruption (see Bug A 2026-05-11 — formatter dropped the
    verdict between writer and on-disk JSON).

    Returns empty containers when the ledger is missing or unreadable —
    never raises.
    """
    bot: dict[str, dict[str, Any]] = {}
    captcha: dict[str, dict[str, Any]] = {}
    verdict_by_pid: dict[str, str] = {}
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return bot, captcha, verdict_by_pid
    try:
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = evt.get("kind")
            pid = evt.get("property_id")
            if not pid:
                continue
            pid_str = str(pid)
            if kind in _BOT_KINDS and pid_str not in bot:
                bot[pid_str] = {
                    "property_id": pid_str,
                    "url": evt.get("url"),
                    "attempt": evt.get("attempt"),
                    "ts": evt.get("ts"),
                    "kind": kind,
                }
            elif kind in _CAPTCHA_KINDS and pid_str not in captcha:
                captcha[pid_str] = {
                    "property_id": pid_str,
                    "url": evt.get("url"),
                    "provider": evt.get("provider"),
                    "attempt": evt.get("attempt"),
                    "ts": evt.get("ts"),
                    "kind": kind,
                }
            elif kind == _EMIT_KIND:
                # The runner emits this exactly once per property at the
                # tail of _process_property. If duplicates appear (a
                # future change emits multiple), last-write-wins matches
                # the verdict the runner ultimately settled on.
                emitted = evt.get("verdict")
                if isinstance(emitted, str) and emitted:
                    verdict_by_pid[pid_str] = emitted
    except Exception as exc:  # noqa: BLE001
        log.warning("run_report: failed to scan event ledger %s: %s", events_path, exc)
    return bot, captcha, verdict_by_pid


# Fields whose fill-rate the dashboard reports. Kept small + high-signal.
_FILL_FIELDS: tuple[str, ...] = (
    "unit_id", "beds", "baths", "area", "rent_low", "available_date",
    "availability_status", "floor_plan_name", "floor", "building",
    "concession_text", "source_ids",
)


def _report_tier_family(tier: str) -> str:
    """Collapse an extraction_tier code to a coarse family for the dashboard.

    Local copy (reporting must not import from scripts.runners — that would
    invert the layer dependency); mirrors jugnu._tier_family."""
    t = (tier or "").upper()
    if not t:
        return "NONE"
    if "LLM" in t:
        return "LLM"
    if "API" in t:
        return "API"
    if "JSONLD" in t or "JSON_LD" in t:
        return "JSONLD"
    if "EMBEDDED" in t:
        return "EMBEDDED"
    if "DOM" in t or "TEXT" in t:
        return "DOM"
    return "OTHER"


def build(
    properties: list[dict[str, Any]],
    run_dir: Path,
    run_date: str,
    cost_rollup: dict[str, float] | None = None,
    slo_violations: list[Any] | None = None,
) -> dict[str, Any]:
    """Build the run-level report.

    Args:
        properties: List of property result dicts.
        run_dir: Path to today's run directory.
        run_date: Date string for this run.
        cost_rollup: Cost totals from CostLedger.total().
        slo_violations: SLO violations from slo_watcher.check().

    Returns:
        Report dict with summary metrics.
    """
    total = len(properties)
    tier_counts: Counter[str] = Counter()
    failed = 0
    carry_forward = 0
    # 2026-05-27: operator-transparent zero-inventory state (waitlist /
    # fully-leased / "no available units"). Counted toward the success
    # numerator (see verdict.verdict_is_success) but surfaced separately
    # in the report so dashboards can split "real extraction wins" from
    # "operator says nothing to lease".
    operator_transparency = 0
    # 2026-05-27 follow-up: per-property trace + phrase histogram so the
    # next-canary report lets a human grep WHICH props were classified
    # and WHICH operator-transparency phrase fired most. Without these
    # the bulk `operator_transparency` count is opaque — you can see "47
    # classified" but can't tell whether the regex hit the right cohort.
    operator_transparency_list: list[dict[str, str]] = []
    operator_transparency_phrases: Counter[str] = Counter()

    # Read events.jsonl once up front. ``verdict_by_pid`` is the
    # authoritative secondary source consulted alongside ``_meta.verdict``
    # below; ``bot_blocked_by_pid`` / ``captcha_by_pid`` feed the
    # pre-extraction termination classifier further down. See
    # docs/2026_05_11_regressions_fix_design.md (Bug A v0.1) for why the
    # event ledger is the authoritative source for the headline metric.
    bot_blocked_by_pid, captcha_by_pid, verdict_by_pid = _scan_event_ledger(run_dir)

    for p in properties:
        meta = p.get("_meta", {}) or {}
        # Jugnu writes extraction tier on _extract_result; legacy path writes
        # it on _meta.scrape_tier_used. Read both so the same report works for
        # either pipeline.
        extract_result = p.get("_extract_result") or {}
        tier = (
            meta.get("scrape_tier_used")
            or (
                extract_result.get("tier_used")
                if isinstance(extract_result, dict)
                else getattr(extract_result, "tier_used", None)
            )
            or "UNKNOWN"
        )
        tier_counts[tier] += 1
        # Verdict resolution — events.jsonl is the canonical source
        # (emitted unconditionally by the runner at PROPERTY_EMITTED),
        # ``_meta.verdict`` is the mirror written into properties.json.
        # When they disagree the event wins and a warning is logged; the
        # case mainly matters when ``_meta.verdict`` is missing because a
        # downstream serialisation step dropped it (Bug A 2026-05-11).
        # ``canonical_id`` is the key the runner emits with; ``property_id``
        # covers the legacy daily_runner shape.
        pid = str(
            meta.get("canonical_id")
            or meta.get("property_id")
            or p.get("property_id")
            or ""
        )
        verdict = resolve_verdict(
            meta_verdict=str(meta.get("verdict") or ""),
            event_verdict=verdict_by_pid.get(pid) if pid else None,
            pid=pid,
        )
        # ``FAIL`` substring guard preserved for the small set of records
        # (e.g. ``_make_failed_record`` early returns) that bypass both
        # the verdict-writer AND the emit event — they carry tier="FAILED"
        # and no other failure signal.
        if verdict.startswith("FAILED") or "FAIL" in str(tier).upper():
            failed += 1
        elif verdict == "SUCCESS_NO_AVAILABILITY":
            operator_transparency += 1
            # Pull the matched phrase off the first unit's source_ids —
            # set there by `_no_availability.placeholder_record()`. Falls
            # back to "" if the operator-transparency verdict was emitted
            # by a path that didn't go through the placeholder unit (e.g.
            # the RentCafe-side detector in chip/rentcafe-anchor-walk).
            units = p.get("units") or []
            phrase = ""
            if units and isinstance(units[0], dict):
                sid = units[0].get("source_ids") or {}
                phrase = str(sid.get("matched_phrase") or "")
            operator_transparency_phrases[phrase or "(no phrase recorded)"] += 1
            operator_transparency_list.append(
                {
                    "property_id": pid,
                    "url": str(p.get("Website") or p.get("url") or ""),
                    "matched_phrase": phrase,
                }
            )
        if meta.get("carry_forward_used"):
            carry_forward += 1

    success_rate = ((total - failed) / total * 100) if total > 0 else 0

    # F7: separate pre-extraction terminations from real tier outcomes.
    _PRE_EXTRACTION_TIERS = frozenset(
        {
            "generic:no_body_short_circuit",
        }
    )
    pre_extraction: Counter[str] = Counter()
    real_tier_counts: Counter[str] = Counter()
    for tier, count in tier_counts.items():
        if tier in _PRE_EXTRACTION_TIERS:
            pre_extraction[tier] += count
        else:
            real_tier_counts[tier] += count

    # ``bot_blocked_by_pid`` / ``captcha_by_pid`` were populated by the
    # single ``_scan_event_ledger`` call at the top of this function.
    # Used here to classify pre-extraction terminations precisely instead
    # of dumping everything into "fetch_other".

    # Map fetch-outcome short-circuit tiers to descriptive keys.
    pre_extraction_terminations: dict[str, int] = {}
    for tier, _count in pre_extraction.items():
        if tier == "generic:no_body_short_circuit":
            # Distribute across fetch outcome types by inspecting property
            # _metas + the event ledger. Event-ledger classification wins
            # over verdict strings when both are present.
            for p in properties:
                meta = p.get("_meta", {}) or {}
                pid = str(meta.get("property_id") or p.get("property_id") or "")
                err = " ".join(meta.get("errors", []) + (p.get("errors") or []))
                if pid and pid in bot_blocked_by_pid:
                    pre_extraction_terminations.setdefault("fetch_bot_blocked", 0)
                    pre_extraction_terminations["fetch_bot_blocked"] += 1
                elif pid and pid in captcha_by_pid:
                    pre_extraction_terminations.setdefault("fetch_captcha_detected", 0)
                    pre_extraction_terminations["fetch_captcha_detected"] += 1
                elif "TRANSIENT" in err:
                    pre_extraction_terminations.setdefault("fetch_transient", 0)
                    pre_extraction_terminations["fetch_transient"] += 1
                elif "HARD_FAIL" in err:
                    pre_extraction_terminations.setdefault("fetch_hard_fail", 0)
                    pre_extraction_terminations["fetch_hard_fail"] += 1
                elif "BOT_BLOCKED" in err or "bot_blocked" in err.lower():
                    pre_extraction_terminations.setdefault("fetch_bot_blocked", 0)
                    pre_extraction_terminations["fetch_bot_blocked"] += 1
                elif "CAPTCHA" in err.upper():
                    pre_extraction_terminations.setdefault("fetch_captcha_detected", 0)
                    pre_extraction_terminations["fetch_captcha_detected"] += 1
                else:
                    pre_extraction_terminations.setdefault("fetch_other", 0)
                    pre_extraction_terminations["fetch_other"] += 1

    bot_blocked_list = sorted(bot_blocked_by_pid.values(), key=lambda r: r["property_id"])
    captcha_list = sorted(captcha_by_pid.values(), key=lambda r: r["property_id"])

    # Output-surfacing dashboard (2026-07-16): verdict spread, per-field fill
    # rates, and a data-quality mix computed from the DELIVERED units — so a
    # reviewer sees coverage + trust at a glance without re-parsing
    # properties.json. Reads per-unit ``extraction_tier`` (surfaced by
    # schema_v2._format_v2_unit) to split real Tier-1 rows from LLM / plan-level.
    verdict_distribution: Counter[str] = Counter()
    fill_counts: dict[str, int] = dict.fromkeys(_FILL_FIELDS, 0)
    tier_family_units: Counter[str] = Counter()
    n_units = n_real_id = n_synth_id = n_plan_level = 0
    for p in properties:
        v = str((p.get("_meta", {}) or {}).get("verdict") or "UNKNOWN")
        verdict_distribution[v] += 1
        for u in (p.get("units") or []):
            n_units += 1
            for k in _FILL_FIELDS:
                if u.get(k) not in (None, "", "null", [], {}):
                    fill_counts[k] += 1
            u_tier = str(u.get("extraction_tier") or "")
            tier_family_units[_report_tier_family(u_tier)] += 1
            if u.get("is_floor_plan_level") or u_tier.upper().endswith("_PLAN_LEVEL"):
                n_plan_level += 1
            uid = str(u.get("unit_id") or "")
            if uid.startswith(("inferred_", "unkeyable_")):
                n_synth_id += 1
            elif uid:
                n_real_id += 1
    field_fill_rates = {
        k: (round(100 * fill_counts[k] / n_units, 1) if n_units else 0.0)
        for k in _FILL_FIELDS
    }
    data_quality = {
        "total_units": n_units,
        "by_tier_family": dict(tier_family_units.most_common()),
        "real_id_units": n_real_id,
        "synthetic_id_units": n_synth_id,
        "plan_level_units": n_plan_level,
    }

    report = {
        "run_date": run_date,
        "generated_at": datetime.now(UTC).isoformat(),
        "totals": {
            "properties": total,
            "succeeded": total - failed,
            "failed": failed,
            "carry_forward": carry_forward,
            "operator_transparency": operator_transparency,
            "success_rate_pct": round(success_rate, 2),
        },
        # 2026-05-27 follow-up: full per-property trace + phrase histogram
        # for SUCCESS_NO_AVAILABILITY. Lets a reviewer answer "WHICH
        # properties got classified and WHICH phrase fired most" without
        # re-grepping properties.json.
        "operator_transparency_detail": {
            "count": operator_transparency,
            "phrase_histogram": dict(operator_transparency_phrases.most_common()),
            "properties": sorted(operator_transparency_list, key=lambda r: r["property_id"]),
        },
        # F7: no_body_short_circuit removed — moved to pre_extraction_terminations
        "tier_distribution": dict(real_tier_counts.most_common()),
        # Output-surfacing dashboard (2026-07-16).
        "verdict_distribution": dict(verdict_distribution.most_common()),
        "field_fill_rates": field_fill_rates,
        "data_quality": data_quality,
        "pre_extraction_terminations": pre_extraction_terminations,
        # 2026-05-04: bot/captcha summaries at run-level. Full per-property
        # detail is also written to bot_blocked_properties.json so the
        # operator can grep / re-shard without parsing report.json.
        "fetch_bot_blocked": {
            "count": len(bot_blocked_list),
            "property_ids": [r["property_id"] for r in bot_blocked_list],
        },
        "fetch_captcha_detected": {
            "count": len(captcha_list),
            "property_ids": [r["property_id"] for r in captcha_list],
        },
        # F4: fields that are tracked in the schema but not currently extracted
        "non_extracted_fields": [
            "lease_term",
            "move_in_date",
            "pmc",
            "website_design",
            "phone",
            "email_address",
            "concessions",
        ],
        "cost": cost_rollup or {},
        "slo_violations": [
            {"name": v.name, "threshold": v.threshold, "observed": v.observed} for v in (slo_violations or [])
        ],
    }

    # Standalone artifact: full per-property detail for blocked properties.
    # This is the file the user asked for ("a separate list of all
    # properties getting blocked due to bot or captcha"). Written even
    # when the lists are empty so consumers can rely on its existence.
    blocked_path = run_dir / "bot_blocked_properties.json"
    blocked_payload = {
        "run_date": run_date,
        "generated_at": datetime.now(UTC).isoformat(),
        "bot_blocked": bot_blocked_list,
        "captcha_detected": captcha_list,
    }
    blocked_path.write_text(json.dumps(blocked_payload, indent=2, default=str), encoding="utf-8")

    # Write JSON
    json_path = run_dir / "report.json"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    # Write Markdown
    md_path = run_dir / "report.md"
    md_lines = [
        f"# Run Report — {run_date}",
        "",
        "## Totals",
        "",
        f"- Properties: {total}",
        f"- Succeeded: {total - failed}",
        f"- Failed: {failed}",
        f"- Carry-forward: {carry_forward}",
        f"- Operator transparency (zero-inventory): {operator_transparency}",
        f"- Success rate: {success_rate:.1f}%",
        "",
        "## Tier Distribution",
        "",
        "| Tier | Count |",
        "|---|---|",
    ]
    for tier, count in tier_counts.most_common():
        md_lines.append(f"| {tier} | {count} |")

    # Output-surfacing dashboard: data-quality mix + per-field fill rates.
    md_lines.extend(
        [
            "",
            "## Data Quality",
            "",
            f"- Units: {n_units}",
            f"- Real-id units: {n_real_id}  ·  synthetic-id: {n_synth_id}  ·  "
            f"plan-level: {n_plan_level}",
            f"- By tier family: {dict(tier_family_units.most_common())}",
            "",
            "### Field fill rates",
            "",
            "| Field | Fill % |",
            "|---|---|",
        ]
    )
    for _fld, _rate in field_fill_rates.items():
        md_lines.append(f"| {_fld} | {_rate}% |")

    # 2026-05-27 follow-up: surface operator-transparency cohort with
    # the matched phrase histogram + per-PID list. Skip the section
    # entirely when zero properties classified — keeps the report clean
    # for runs where the detector didn't fire.
    if operator_transparency:
        md_lines.extend(
            [
                "",
                "## Operator Transparency (zero-inventory)",
                "",
                f"- Total: {operator_transparency}",
                "",
                "### Phrase histogram",
                "",
                "| Matched phrase | Count |",
                "|---|---|",
            ]
        )
        for phrase, count in operator_transparency_phrases.most_common():
            # Escape pipes so a phrase containing `|` doesn't break the table.
            safe = phrase.replace("|", r"\|") if phrase else "(no phrase recorded)"
            md_lines.append(f"| {safe} | {count} |")
        md_lines.extend(
            [
                "",
                "### Property IDs",
                "",
            ]
        )
        for r in sorted(operator_transparency_list, key=lambda x: x["property_id"]):
            phrase_note = f' — "{r["matched_phrase"]}"' if r["matched_phrase"] else ""
            md_lines.append(
                f"- {r['property_id']} — {r['url'] or '(no url)'}{phrase_note}"
            )

    md_lines.extend(
        [
            "",
            "## Bot / CAPTCHA Blocked",
            "",
            f"- Bot-blocked properties: {len(bot_blocked_list)}",
            f"- CAPTCHA-detected properties: {len(captcha_list)}",
        ]
    )
    if bot_blocked_list:
        md_lines.extend(
            [
                "",
                "### Bot-blocked property IDs",
                "",
            ]
        )
        for r in bot_blocked_list:
            md_lines.append(f"- {r['property_id']} — {r.get('url') or '(no url)'}")
    if captcha_list:
        md_lines.extend(
            [
                "",
                "### CAPTCHA-detected property IDs",
                "",
            ]
        )
        for r in captcha_list:
            md_lines.append(f"- {r['property_id']} — {r.get('url') or '(no url)'}")

    md_lines.extend(
        [
            "",
            "## Cost",
            "",
        ]
    )
    for cat, amount in (cost_rollup or {}).items():
        md_lines.append(f"- {cat}: ${amount:.4f}")

    md_lines.extend(
        [
            "",
            "## SLO Status",
            "",
        ]
    )
    if slo_violations:
        for v in slo_violations:
            md_lines.append(f"- **{v.name}**: observed={v.observed:.4f}, threshold={v.threshold}")
    else:
        md_lines.append("All SLOs green.")

    md_lines.append("")
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    # Phase 6/7: emit amenities and concessions observation reports
    # (observation-only, no validation gates depend on them — H7).
    try:
        from ma_poc.reporting.observation_reports import (
            build_amenities_report,
            build_concessions_report,
        )

        build_amenities_report(properties, run_dir, run_date)
        build_concessions_report(properties, run_dir, run_date)
    except Exception as exc:  # noqa: BLE001 — never fail the run on a report
        log.warning("observation reports failed: %s", exc)

    log.info("Run report written to %s", run_dir)
    return report
