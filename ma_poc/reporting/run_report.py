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

log = logging.getLogger(__name__)


# Event kinds we use to classify failures. Source of truth:
# ma_poc/observability/events.py.
_BOT_KINDS = frozenset({"fetch.bot_blocked"})
_CAPTCHA_KINDS = frozenset({"fetch.captcha_detected"})


def _scan_event_ledger(
    run_dir: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Read events.jsonl and return (bot_blocked_by_pid, captcha_by_pid).

    Each value is keyed by property_id and holds the first-seen event
    payload (url, attempt, ts, etc.) for downstream reporting. Returns
    empty dicts when the ledger is missing or unreadable — never raises.
    """
    bot: dict[str, dict[str, Any]] = {}
    captcha: dict[str, dict[str, Any]] = {}
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return bot, captcha
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
            if kind in _BOT_KINDS and pid not in bot:
                bot[pid] = {
                    "property_id": pid,
                    "url": evt.get("url"),
                    "attempt": evt.get("attempt"),
                    "ts": evt.get("ts"),
                    "kind": kind,
                }
            elif kind in _CAPTCHA_KINDS and pid not in captcha:
                captcha[pid] = {
                    "property_id": pid,
                    "url": evt.get("url"),
                    "provider": evt.get("provider"),
                    "attempt": evt.get("attempt"),
                    "ts": evt.get("ts"),
                    "kind": kind,
                }
    except Exception as exc:  # noqa: BLE001
        log.warning("run_report: failed to scan event ledger %s: %s", events_path, exc)
    return bot, captcha


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
        # Verdict is the authoritative success/fail signal — tier_used can
        # say TIER_1_API on a carry-forward record, so tier string alone is
        # not a failure indicator.
        verdict = meta.get("verdict") or ""
        if verdict.startswith("FAILED") or "FAIL" in str(tier).upper():
            failed += 1
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

    # ── Authoritative bot/captcha classification from the event ledger ─────
    # The verdict-string heuristic below is best-effort — the events.jsonl
    # ledger is the source of truth for fetch outcomes. We use it to (a)
    # produce a per-property list of bot/captcha-blocked properties, and (b)
    # classify pre-extraction terminations precisely instead of dumping
    # everything into "fetch_other".
    bot_blocked_by_pid, captcha_by_pid = _scan_event_ledger(run_dir)

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

    report = {
        "run_date": run_date,
        "generated_at": datetime.now(UTC).isoformat(),
        "totals": {
            "properties": total,
            "succeeded": total - failed,
            "failed": failed,
            "carry_forward": carry_forward,
            "success_rate_pct": round(success_rate, 2),
        },
        # F7: no_body_short_circuit removed — moved to pre_extraction_terminations
        "tier_distribution": dict(real_tier_counts.most_common()),
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
        f"- Success rate: {success_rate:.1f}%",
        "",
        "## Tier Distribution",
        "",
        "| Tier | Count |",
        "|---|---|",
    ]
    for tier, count in tier_counts.most_common():
        md_lines.append(f"| {tier} | {count} |")

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
