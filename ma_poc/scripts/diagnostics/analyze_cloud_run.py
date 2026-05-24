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
#
# 2026-05-13 port (MAY13_API_TIER_PORT_PLAN.md): added entries for the 10
# new Tier-1 adapters + the new Entrata/OneSite/AppFolio variant labels
# emitted by the per-adapter improvements. Without these entries the
# downstream platform-tier breakdown silently bucketed properties using
# new adapters as "other", under-counting per-PMS yield in the daily
# report and in canary_diff.py's per-PMS gate.
PLATFORM_TIERS = {
    # Original 9 entries (pre-May-13).
    "TIER_1_API_ENTRATA": "entrata",
    "TIER_1_API_APPFOLIO": "appfolio",
    "TIER_1_API_ONESITE": "onesite",
    "TIER_1_API_AMLI_NEXT_DATA": "amli",
    "TIER_1_API_RENTCAFE": "rentcafe",
    "TIER_1_API_SIGHTMAP": "sightmap",
    "TIER_1_API_AVALONBAY": "avalonbay",
    "SYNDICATION_ONLY_SQUARESPACE": "squarespace",
    "SYNDICATION_ONLY_WIX": "wix",
    # May-13 port: 10 new Tier-1 adapters (Commits 10-13).
    "TIER_1_API_REALPAGE_OLL": "realpage_oll",
    "TIER_1_API_G5": "g5",
    "TIER_1_API_KNOCK": "knock",
    "TIER_1_API_CORTLAND": "cortland",
    "TIER_1_API_EQUITY": "equity",
    "TIER_1_API_RENTMANAGER": "rentmanager",
    "TIER_1_API_IRVINE": "irvine",
    "TIER_1_API_APTS247": "apts247",
    "TIER_1_API_ESSEX": "essex",
    "TIER_1_API_MAAC": "maac",
    "TIER_1_API_RENTVISION": "rentvision",
    # May-13 port: new variant labels (Commits 5, 6, 8, 9).
    "TIER_1_API_ENTRATA_PROBE": "entrata",        # 5-path probe success
    "TIER_1_DOM_ENTRATA_WP": "entrata",           # WordPress fallback
    "TIER_1_DOM_ENTRATA_PROSPECTPORTAL": "entrata",  # PP HTML fragment
    "TIER_1_API_ONESITE_EMPTY": "onesite",        # parsed but validity-rejected
    "TIER_1_API_ONESITE_NO_RESPONSE": "onesite",  # no RealPage responses
    "TIER_1_DOM_APPFOLIO_EMBED": "appfolio",      # cross-origin iframe
    "TIER_1_DOM_APPFOLIO_DETAIL": "appfolio",     # listings/detail/<uuid> parser
    "TIER_1_DOM_APPFOLIO_SSR": "appfolio",        # F11 SSR fallback
    "TIER_1_DOM_RENTCAFE_HOSTED": "rentcafe",     # hosted-table parser
    "TIER_1_DOM_RENTCAFE_NESTIN": "rentcafe",     # Nestin per-plan recovery
    # 2026-05-21 (P0 follow-up): WP-probe recovery is now stamped distinctly.
    "TIER_1_API_RENTCAFE_WP_PROBE": "rentcafe",
    "TIER_1_API_RENTCAFE_SECURECAFE": "rentcafe",
    "TIER_1_API_RESMAN": "resman",
    "TIER_1_DOM_ENCORESKYLINE_TEMPLATE": "encoreskyline_template",
    "TIER_1_DOM_RENTMANAGER_ILOVELEASING": "rentmanager",
    # Equity adapter emits these three labels (success / empty / no_response).
    "TIER_1_API_EQUITY_EMPTY": "equity",
    "TIER_1_API_EQUITY_NO_RESPONSE": "equity",
}

# Outcomes that mean "we never got data". UNREACHABLE = pre-extraction termination,
# NO_DATA = page loaded but every tier failed.
TERMINAL_FAILURE_VERDICTS = {"FAILED_NO_DATA", "FAILED_UNREACHABLE"}

# Named-fix telemetry — codenames that map to the (event_kind, predicate)
# patterns we expect to see when a known fix is exercised. Values are
# human-readable descriptions; the matching logic lives in
# ``_record_named_fix_event``. Add a new entry whenever a fix lands so the
# next day's diff makes the dead-code state visible (e.g. Bug 9 Entrata
# probe shipped 2026-05-09 but never fired in the 2026-05-10 run because
# ``page=None`` in the production runner — invisible without this).
NAMED_FIX_PATTERNS: dict[str, str] = {
    "entrata_probe_won": "Bug 9: Entrata direct-endpoint probe won the extraction (TIER_1_API_ENTRATA_PROBE)",
    "rate_limited_proxy_escalation": "Bug 6: forced proxy after RATE_LIMITED on direct connection",
    "rescue_skipped_captcha": "F1.2: rescue gate short-circuited on captcha-detected body",
    "rescue_gate_onesite": "F1.3: rescue allow-list extended to onesite (new attempts on this adapter)",
    "rescue_gate_amli": "F1.3: rescue allow-list extended to amli (new attempts on this adapter)",
    "appfolio_detail_won": "Bug 7: AppFolio detail-page parser won (TIER_1_DOM_APPFOLIO_DETAIL)",
}


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
    verdict: str | None = None  # SUCCESS / SUCCESS_PLAN_LEVEL / SUCCESS_PARTIAL / FAILED_NO_DATA / FAILED_UNREACHABLE / CARRY_FORWARD / PARTIAL / DEAD_URL
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
    # Bug #7 + #11 fix (2026-05-16): entry-page CF detection, separate from
    # the aggregate above which collects any CF hit including hop-side.
    # Distinguishes "entry page is CF-walled" (real SLO signal) from
    # "Entrata deep-link is CF-protected but entry loaded fine" (extraction
    # gap, not an infra problem). Canonical case: 174/175 TIER_1_API_ENTRATA
    # failures on 2026-05-16 had bot_blocked=True from hop-side CF; only 1
    # actually had entry-page CF. The conflated label drove operators to
    # blame CF/proxy when the real bug was iframe-harvest miss.
    entry_captcha_detected: bool = False
    entry_bot_blocked: bool = False
    llm_rescue_attempted: bool = False
    llm_rescue_succeeded: bool = False
    llm_rescue_source_adapter: str | None = None  # which adapter the rescue gate was opened for
    llm_gate_relaxed_reason: str | None = None  # reason from extract.llm_gate_relaxed event
    llm_cost: float = 0.0
    link_hops_attempted: int = 0
    link_hops_recovered: int = 0
    issue_codes: list[str] = field(default_factory=list)

    # Body characterisation signals from extract.html_characterized — used
    # by is_marketing_shell() to flag pages that loaded fine but have no
    # parseable rent data (large body, low text, no rent/jsonld signals).
    text_bytes: int | None = None
    rent_signal_count: int | None = None
    jsonld_types: list[str] = field(default_factory=list)
    fingerprints_matched: list[str] = field(default_factory=list)
    # Per-tier attempt outcomes (tier_key → outcome string) from
    # extract.tier_attempted events. Used to reconstruct the cascade path
    # for forensic property reports.
    tier_attempts: dict[str, str] = field(default_factory=dict)

    # T2 (2026-05-20): date-gap sub-cause classification inputs. Populated
    # from extract.date_presence_summary OR (when that's absent — pre-T2
    # cloud runs) from per-property max across all extract.html_characterized
    # events for the property.
    max_date_iso_seen: int = 0
    max_date_us_seen: int = 0
    max_date_named_seen: int = 0
    max_available_now_seen: int = 0
    max_move_in_kw_seen: int = 0
    max_data_avail_attrs_seen: int = 0
    n_units_with_date: int = 0
    n_units_status_available: int = 0

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
        # 2026-05-17 — match the production success classifier in
        # reporting/verdict.py:_SUCCESS_VERDICTS. SUCCESS_PARTIAL is the
        # timeout-rescue success verdict (real units buffered before the
        # per-property wallclock fired). The bare ``PARTIAL`` verdict
        # (validation-majority-rejected) intentionally STAYS OUT of the
        # success set — its units are suspect (>50% gate-rejected).
        return self.verdict in {"SUCCESS", "SUCCESS_PLAN_LEVEL", "SUCCESS_PARTIAL"}

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
    # 2026-05-17 — timeout-rescue successes (verdict ``SUCCESS_PARTIAL``):
    # the per-property wallclock fired before the cascade completed but
    # the link-hop accumulator buffered ≥1 valid unit. Tracked separately
    # so the dashboard can show the rescue population alongside clean
    # SUCCESS. Counted toward ``properties_succeeded``.
    properties_success_partial: int = 0
    properties_success_partial_units_total: int = 0
    # Validation-majority-rejected (verdict ``PARTIAL``). NOT a success —
    # the schema gate dropped >50% of rows so the surviving units are
    # suspect. Tracked so the dashboard can surface this population
    # (data-quality alert) without conflating it with timeout-rescue.
    properties_partial_validation_rejected: int = 0
    properties_dead_url: int = 0
    success_rate_pct: float = 0.0
    llm_cost_total: float = 0.0
    slo_breaches: list[dict[str, Any]] = field(default_factory=list)
    tier_distribution: Counter = field(default_factory=Counter)
    fetch_signatures: Counter = field(default_factory=Counter)
    failure_terminal_tiers: Counter = field(default_factory=Counter)
    # Per-adapter LLM rescue effectiveness — gates rescue ROI debates after
    # the 2026-05-09 → 2026-05-10 regression where the relaxed rescue gate
    # tripled attempts but only added 12 successes ($4 cost, ~0 gain).
    rescue_attempted_by_adapter: Counter = field(default_factory=Counter)
    rescue_succeeded_by_adapter: Counter = field(default_factory=Counter)
    rescue_total_cost: float = 0.0
    # Named-fix telemetry — count of "this fix path actually fired" events
    # so we can detect dead-code fixes (the Bug 9 Entrata probe shipped but
    # never executes because page=None in the production runner).
    named_fix_events: Counter = field(default_factory=Counter)

    # PR 3 (2026-05-10) — Persistence health (self-learning loop SLO).
    # Counted from events.jsonl across all shards. The dashboard section
    # at the top of summary.md cross-references these against thresholds
    # so writer-broken regressions surface day 1.
    mapping_save_dropped: Counter = field(default_factory=Counter)  # reason → count
    profile_update_failed: int = 0
    startup_probe_ok: int = 0
    startup_probe_failed: int = 0
    profile_replay_hits: int = 0
    profile_replay_miss_with_saved: int = 0
    field_patch_hits: int = 0
    field_patch_drift: int = 0
    llm_gate_relaxed: int = 0
    # Optional: populated when --check-db is passed and DB is reachable.
    # Maps query name → list of result rows. None when DB check skipped.
    db_persistence_health: dict[str, list[dict[str, Any]]] | None = None


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
            _is_entry_attempt = (ev.get("attempt") or 1) == 1 and o.fetch_outcome is None
            if _is_entry_attempt:
                o.fetch_outcome = ev.get("outcome")
                o.fetch_error_signature = ev.get("error_signature")
                o.fetch_status = ev.get("status")
                o.final_url = ev.get("final_url")
                o.body_bytes = ev.get("body_bytes")
                if ev.get("captcha_detected"):
                    o.captcha_detected = True
                    o.entry_captcha_detected = True
                # Bug #7/#11 fix (2026-05-16): mark entry-level bot block
                # so the pattern classifier can distinguish from hop-side
                # CF (which sets only the aggregate ``bot_blocked``).
                if ev.get("outcome") == "BOT_BLOCKED":
                    o.entry_bot_blocked = True
                    o.bot_blocked = True
        elif kind == "fetch.bot_blocked":
            o.bot_blocked = True
        elif kind == "fetch.captcha_detected":
            o.captcha_detected = True
        elif kind == "extract.detector_signals":
            fps = ev.get("fingerprints_matched")
            if isinstance(fps, list):
                o.fingerprints_matched = list(fps)
        elif kind == "extract.html_characterized":
            tb = ev.get("text_bytes")
            if isinstance(tb, int):
                o.text_bytes = tb
            rs = ev.get("rent_signal_count")
            if isinstance(rs, int):
                o.rent_signal_count = rs
            jt = ev.get("jsonld_types")
            if isinstance(jt, list):
                # Dedupe but preserve order so the marketing-shell pattern
                # (ApartmentComplex with no Apartment/Offer siblings) stays
                # distinguishable from properties whose JSON-LD has both.
                seen: set[str] = set()
                o.jsonld_types = [
                    t for t in jt
                    if isinstance(t, str) and not (t in seen or seen.add(t))
                ]
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
            # F2 rescue gate emits ``source_adapter`` so we can attribute
            # cost to the resolved adapter, not the (often demoted) PMS.
            if ev.get("source_adapter"):
                o.llm_rescue_source_adapter = ev["source_adapter"]
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
        elif kind == "extract.html_characterized":
            # T1 (2026-05-20): track the max date-signal count seen across
            # every captured HTML body for this property. Used by the
            # date-gap sub-cause classifier (Sub-cause B = all zero).
            def _max_int(field: str, current: int) -> int:
                v = ev.get(field)
                if v is None:
                    return current
                try:
                    return max(current, int(v))
                except (TypeError, ValueError):
                    return current

            o.max_date_iso_seen = _max_int("date_iso_count", o.max_date_iso_seen)
            o.max_date_us_seen = _max_int("date_us_count", o.max_date_us_seen)
            o.max_date_named_seen = _max_int("date_named_count", o.max_date_named_seen)
            o.max_available_now_seen = _max_int("available_now_count", o.max_available_now_seen)
            o.max_move_in_kw_seen = _max_int("move_in_keyword_count", o.max_move_in_kw_seen)
            o.max_data_avail_attrs_seen = _max_int("data_avail_attr_count", o.max_data_avail_attrs_seen)
        elif kind == "extract.date_presence_summary":
            # T2 (2026-05-20): authoritative per-property roll-up. When
            # present, override the html_characterized-derived maxes.
            for src, dst_attr in (
                ("max_date_iso_seen", "max_date_iso_seen"),
                ("max_date_us_seen", "max_date_us_seen"),
                ("max_date_named_seen", "max_date_named_seen"),
                ("max_available_now_seen", "max_available_now_seen"),
                ("max_move_in_keyword_seen", "max_move_in_kw_seen"),
                ("max_data_avail_attrs_seen", "max_data_avail_attrs_seen"),
                ("n_units_with_date", "n_units_with_date"),
                ("n_units_status_available", "n_units_status_available"),
            ):
                v = ev.get(src)
                if v is not None:
                    try:
                        setattr(o, dst_attr, int(v))
                    except (TypeError, ValueError):
                        pass

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

    # Recompute the verdict splits from event-level verdicts so the
    # numbers reconcile with per-property categorisation.
    #
    # 2026-05-17: ``SUCCESS_PARTIAL`` (timeout-rescued success) is a
    # success-class verdict and counts toward ``properties_succeeded``.
    # The bare ``PARTIAL`` verdict (validation-majority-rejected) is
    # tracked under ``properties_partial_validation_rejected`` and does
    # NOT count as success — its rows are suspect (>50% gate-rejected).
    # The runner's report.json (``totals.succeeded``) already includes
    # SUCCESS_PARTIAL via the run_report classifier; this top-up branch
    # handles older runs / fresh-data cases.
    verdicts = Counter(o.verdict for o in all_outcomes.values())
    stats.properties_failed_no_data = verdicts.get("FAILED_NO_DATA", 0)
    stats.properties_failed_unreachable = verdicts.get("FAILED_UNREACHABLE", 0)
    stats.properties_success_partial = verdicts.get("SUCCESS_PARTIAL", 0)
    stats.properties_partial_validation_rejected = verdicts.get("PARTIAL", 0)
    stats.properties_dead_url = verdicts.get("DEAD_URL", 0)
    # Sum units carried by SUCCESS_PARTIAL records — emitted on the
    # ``output.property_emitted`` event payload as the ``units`` field.
    stats.properties_success_partial_units_total = sum(
        int(o.units or 0)
        for o in all_outcomes.values()
        if (o.verdict or "") == "SUCCESS_PARTIAL"
    )
    # Top up the succeeded count to include SUCCESS_PARTIAL when the
    # report.json's totals.succeeded didn't (older runs / runs before the
    # verdict.py update).
    _succeeded_floor = (
        verdicts.get("SUCCESS", 0)
        + verdicts.get("SUCCESS_PLAN_LEVEL", 0)
        + stats.properties_success_partial
    )
    if _succeeded_floor > stats.properties_succeeded:
        stats.properties_succeeded = _succeeded_floor
    stats.properties_failed_other = max(
        0,
        stats.properties_total
        - stats.properties_succeeded
        - stats.properties_failed_no_data
        - stats.properties_failed_unreachable
        - stats.properties_partial_validation_rejected
        - stats.properties_dead_url,
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
        # Rescue effectiveness — keyed on the gate's resolved adapter so we
        # can detect when expanding the allow-list (F1.3 onesite/amli on
        # 2026-05-09) inflates attempts without producing successes.
        if o.llm_rescue_attempted:
            adapter_key = o.llm_rescue_source_adapter or "unknown"
            stats.rescue_attempted_by_adapter[adapter_key] += 1
            if o.llm_rescue_succeeded:
                stats.rescue_succeeded_by_adapter[adapter_key] += 1
            stats.rescue_total_cost += o.llm_cost

    return stats, all_outcomes


# ---------------------------------------------------------------------------
# Date-gap sub-cause classifier (T3, 2026-05-20)
# ---------------------------------------------------------------------------


_SUCCESS_VERDICTS_FOR_DATE_GAP: tuple[str, ...] = (
    "SUCCESS",
    "SUCCESS_PARTIAL",
    "SUCCESS_PLAN_LEVEL",
)


def classify_date_gap(o: PropertyOutcome) -> str | None:
    """Classify a property into A/B/C/D/E for the avail-date sub-cause split.

    Returns None when the property doesn't have an avail-date gap (date
    fill ≥ 50%, or didn't ship units, or failed). See playbook §19 for
    the decision tree.

    A_API_FLOORPLANS_ONLY — TIER_1_API_* tier won; producer side returned
        plan-level only (no per-unit dates). Fix: F7a per-unit endpoint probe.

    B_PAGE_NO_DATES — zero date signals in any captured HTML for this
        property. Page genuinely doesn't display per-unit dates. Fix:
        F7b accept + document; per-unit info is gated behind portal.

    C_LLM_SECTION_MISSED — TIER_4_LLM_DOM tier won AND date signals were
        present in captured HTML. The LLM section picker handed an
        adjacent-but-narrower DOM region that excluded the date column.
        Fix: F7c LLM_DOM section widening.

    D_DOM_ATTRS_IGNORED — TIER_1_API_* tier won AND data-availability
        attrs exist in captured HTML. The DOM has dates the API path
        didn't read. Fix: F7d (already shipped for OneSite) +
        analogous for other PMS.

    E_AVAILABLE_NOW_NO_FALLBACK — "Available Now" text seen but no ISO
        date and date fill < 50%. The formatter's today's-date fallback
        for "Available Now" / "now" should have fired but didn't —
        likely because available_date_raw was never set. Investigation
        needed (alias drift? producer-side regression?).
    """
    if o.verdict not in _SUCCESS_VERDICTS_FOR_DATE_GAP:
        return None
    if o.units <= 0:
        return None
    # The T2 `extract.date_presence_summary` event populates
    # `n_units_with_date` directly. For pre-T2 runs (cloud data emitted
    # before this telemetry shipped) the field stays 0; we can't tell
    # those apart from a real "all units missing dates" case using
    # `n_with_date` alone. The distinguishing signal is whether ANY
    # T1/T2 evidence was recorded — when `max_*_seen` and `issue_codes`
    # are both blank we skip (return None) so old runs don't fill the
    # bucket with false positives.
    n_with_date = o.n_units_with_date
    fill_ratio = n_with_date / o.units if o.units else 0
    if fill_ratio >= 0.5:
        return None
    has_t1_telemetry = (
        o.max_date_iso_seen > 0
        or o.max_date_us_seen > 0
        or o.max_date_named_seen > 0
        or o.max_data_avail_attrs_seen > 0
        or o.max_available_now_seen > 0
        or o.max_move_in_kw_seen > 0
    )
    has_t2_or_t4 = (
        n_with_date > 0
        or "DATE_GAP_PAGE_NO_DATES" in o.issue_codes
    )
    if not has_t1_telemetry and not has_t2_or_t4:
        # Pre-T1/T2 cloud run — no evidence either way. Don't fabricate
        # a classification.
        return None

    # Has date signals anywhere in captured HTML?
    has_iso_or_us_or_named = (
        o.max_date_iso_seen > 0
        or o.max_date_us_seen > 0
        or o.max_date_named_seen > 0
    )
    has_any_date_signal = (
        has_iso_or_us_or_named or o.max_data_avail_attrs_seen > 0
    )
    has_only_avail_now = o.max_available_now_seen > 0 and not has_any_date_signal

    tier = o.terminal_tier or ""

    # D — TIER_1_API_* with data-availability attrs in DOM.
    if tier.startswith("TIER_1_API") and o.max_data_avail_attrs_seen > 0:
        return "D_DOM_ATTRS_IGNORED"

    # B — zero date signals AND no available-now text.
    if not has_any_date_signal and not has_only_avail_now:
        return "B_PAGE_NO_DATES"

    # E — available-now text but no date shape AND fill < 50%.
    if has_only_avail_now:
        return "E_AVAILABLE_NOW_NO_FALLBACK"

    # C — TIER_4_LLM_DOM AND dates exist in HTML (LLM missed the section).
    if tier == "TIER_4_LLM_DOM" and has_iso_or_us_or_named:
        return "C_LLM_SECTION_MISSED"

    # A — TIER_1_API_* (any) AND no data-availability attrs (DOM clean,
    # API only returned plan-level).
    if tier.startswith("TIER_1_API") or tier == "TIER_1_API":
        return "A_API_FLOORPLANS_ONLY"

    # Unclassified — could be TIER_3_DOM with a wider gap, TIER_1_5_EMBEDDED
    # with no date keys in the embedded blob, etc. Surface as OTHER so the
    # next investigation knows where to look.
    return "OTHER"


def _render_date_gap_subcause_section(outcomes: dict[str, PropertyOutcome]) -> list[str]:
    """Render the date-gap sub-cause split as markdown lines.

    Returns an empty list when no avail-date-gap properties are found
    (e.g. an old run without T1/T2 telemetry).
    """
    classified: list[tuple[str, PropertyOutcome]] = []
    for o in outcomes.values():
        c = classify_date_gap(o)
        if c is not None:
            classified.append((c, o))

    if not classified:
        return []

    by_cause: dict[str, list[PropertyOutcome]] = {}
    for cause, o in classified:
        by_cause.setdefault(cause, []).append(o)

    ordered_causes = [
        ("A_API_FLOORPLANS_ONLY", "F7a per-unit endpoint probe"),
        ("B_PAGE_NO_DATES", "F7b — accept; per-unit data not exposed"),
        ("C_LLM_SECTION_MISSED", "F7c LLM_DOM section widening"),
        ("D_DOM_ATTRS_IGNORED", "F7d (shipped for OneSite 2026-05-20)"),
        ("E_AVAILABLE_NOW_NO_FALLBACK", "Investigate — formatter fallback miss"),
        ("OTHER", "Investigate — unclassified gap"),
    ]
    lines = [
        "",
        "## Date-completeness sub-cause split (avail-date-only-gap properties)",
        "",
        f"**Total avail-date-only-gap properties: {len(classified)}**.",
        "Classification uses the T1 (`extract.html_characterized.date_*_count`) and ",
        "T2 (`extract.date_presence_summary`) telemetry shipped 2026-05-20. See ",
        "[playbook §19](failed_no_data_debugging_playbook.md) for the decision tree.",
        "",
        "| Sub-cause | Count | % | Coverage |",
        "|---|---:|---:|---|",
    ]
    total = len(classified)
    for cause, coverage in ordered_causes:
        bucket = by_cause.get(cause, [])
        n = len(bucket)
        pct = (n / total * 100) if total else 0
        lines.append(f"| {cause} | {n} | {pct:.1f}% | {coverage} |")
    lines.append("")

    # Sample PIDs per bucket — 5 each.
    for cause, _ in ordered_causes:
        bucket = by_cause.get(cause, [])
        if not bucket:
            continue
        sample = bucket[:5]
        lines.append(f"**Sample {cause} PIDs:**")
        lines.append("")
        for o in sample:
            url = o.url or o.final_url or ""
            tier = o.terminal_tier or "-"
            fill = (o.n_units_with_date / o.units * 100) if o.units else 0
            lines.append(
                f"- `{o.property_id}` ({tier}) — {o.units} units, "
                f"{fill:.0f}% date-fill — {url[:80]}"
            )
        lines.append("")

    return lines


# ---------------------------------------------------------------------------
# Pattern categorisation
# ---------------------------------------------------------------------------


def is_marketing_shell(o: PropertyOutcome) -> bool:
    """Detect the "marketing-shell" failure class observed in the 2026-05-10 run.

    Pattern: substantial body bytes, low text bytes, ApartmentComplex
    JSON-LD present without sibling Apartment/Offer/FloorPlan, zero or one
    rent-token signals. These are property pages whose actual unit data
    lives behind a leasing portal (Entrata module, OneSite portal, etc.)
    that we never load — the homepage is just a marketing wrapper.

    Concrete examples from 2026-05-10: 1701arch.com → livethearch.com,
    22slate.com, liveatmountainview.com, ashfordcasaserena.com — all four
    have ApartmentComplex JSON-LD with only ImageObject siblings and no
    Apartment/Offer/FloorPlan. They all fail at TIER_1_API_ENTRATA today.

    Counts as marketing-shell when ALL of:
      - body_bytes > 50 KB but text_bytes < 10 KB (mostly script/style)
      - rent_signal_count <= 1
      - jsonld_types includes "ApartmentComplex" but NOT
        Apartment/Offer/FloorPlan/RentalOffer
    """
    if not o.body_bytes or not o.text_bytes:
        return False
    if o.body_bytes < 50_000 or o.text_bytes >= 10_000:
        return False
    if (o.rent_signal_count or 0) > 1:
        return False
    if "ApartmentComplex" not in o.jsonld_types:
        return False
    real_unit_schemas = {"Apartment", "Offer", "FloorPlan", "RentalOffer"}
    if any(t in real_unit_schemas for t in o.jsonld_types):
        return False
    return True


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
    # Bug #7 fix (2026-05-16): split CF detection into entry-level (real
    # SLO signal) vs aggregate (entry OR any hop). 174 of 175 Entrata
    # failures on 2026-05-16 had ``bot_blocked=True`` from hop-side CF
    # while the entry page loaded fine — labelling them ``cloudflare_
    # entrata`` misdirects triage to fetch/proxy infrastructure when the
    # actual bug is portal-iframe harvest (Bug #4/#5).
    entry_cf_blocked = (
        o.fetch_error_signature == "CF_CHALLENGE"
        or o.entry_captcha_detected
        or o.entry_bot_blocked
    )
    cf_blocked_anywhere = (
        o.fetch_error_signature == "CF_CHALLENGE"
        or o.captcha_detected
        or o.bot_blocked
    )
    tier = o.terminal_tier or ""

    if tier == "LLM_GATE_NO_BODY" or "LLM_GATE_NO_BODY" in tier:
        return ("P8", "llm_gate_no_body")

    if entry_cf_blocked and (tier == "TIER_1_API_ENTRATA" or "no_body" in tier or o.verdict == "FAILED_UNREACHABLE"):
        return ("P2", "cloudflare_entrata")
    # Bug #7 fix: explicit bucket for entrata sites whose entry loaded OK
    # but the deep-link/widget hop hit CF. These are extraction gaps
    # (iframe-harvest miss, hop-redirect-loop), not infra failures.
    if cf_blocked_anywhere and tier == "TIER_1_API_ENTRATA" and not entry_cf_blocked:
        return ("P2", "entrata_subpage_cf")

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


# PR 3 (2026-05-10) — Persistence health SLO thresholds. Tuned for the
# post-PR-1+2 baseline; revisit after Canary 1.
#
# - mapping_save_drop_rate (drops / (drops + replay_hits)) > 0.5 → ALERT
# - profile_replay_hit_rate (hits / (hits + miss_with_saved)) < 0.3 → ALERT
# - profile_update_failed > 100 → ALERT (silent persistence failures)
# - startup_probe_failed > 0 → ALERT (deploy-time guard fired)
SLO_MAPPING_SAVE_DROP_RATE_MAX = 0.5
SLO_PROFILE_REPLAY_HIT_RATE_MIN = 0.3
SLO_PROFILE_UPDATE_FAILED_MAX = 100
SLO_STARTUP_PROBE_FAILED_MAX = 0


def _slo_marker(ok: bool) -> str:
    return "OK" if ok else "ALERT"


def render_persistence_health_md(stats: RunStats) -> list[str]:
    """Render the persistence-health SLO section.

    Returns a list of markdown lines (caller appends to its accumulator).
    Always renders, even when all counters are zero — a zero startup-probe
    count when shards_seen > 0 is itself a signal (probe wasn't deployed
    yet OR was disabled via env).
    """
    drops_total = sum(stats.mapping_save_dropped.values())
    replay_total = stats.profile_replay_hits + stats.profile_replay_miss_with_saved
    save_attempts = drops_total + stats.profile_replay_hits  # rough denominator
    drop_rate = (drops_total / save_attempts) if save_attempts else 0.0
    replay_hit_rate = (stats.profile_replay_hits / replay_total) if replay_total else 0.0

    drop_ok = drop_rate <= SLO_MAPPING_SAVE_DROP_RATE_MAX
    replay_ok = (
        replay_hit_rate >= SLO_PROFILE_REPLAY_HIT_RATE_MIN
        if replay_total > 0
        else True  # no signal yet — don't alarm
    )
    update_ok = stats.profile_update_failed <= SLO_PROFILE_UPDATE_FAILED_MAX
    probe_ok = stats.startup_probe_failed <= SLO_STARTUP_PROBE_FAILED_MAX

    lines = [
        "## Persistence health (self-learning loop SLO)",
        "",
        "Counted from per-shard `events.jsonl`. Cross-references the channel-by-channel "
        "DB row counts in `scripts/diagnostics/profile_persistence_health.sql` (run via "
        "`db_query.py`). When a row is **ALERT**, the runner is silently dropping or "
        "the loop has regressed — page someone.",
        "",
        "| Metric | Today | Threshold | Status |",
        "|---|---|---|---|",
        f"| `MAPPING_SAVE_DROPPED` total | {drops_total} | — | — |",
        f"| `mapping_save_drop_rate` | {drop_rate:.1%} | < {SLO_MAPPING_SAVE_DROP_RATE_MAX:.0%} | {_slo_marker(drop_ok)} |",
        f"| `PROFILE_REPLAY_HIT` count | {stats.profile_replay_hits} | — | — |",
        f"| `profile_replay_hit_rate` | {replay_hit_rate:.1%} | ≥ {SLO_PROFILE_REPLAY_HIT_RATE_MIN:.0%} | {_slo_marker(replay_ok)} |",
        f"| `PROFILE_UPDATE_FAILED` count | {stats.profile_update_failed} | ≤ {SLO_PROFILE_UPDATE_FAILED_MAX} | {_slo_marker(update_ok)} |",
        f"| `STARTUP_PROBE_OK` count | {stats.startup_probe_ok} | ≥ shards_seen | — |",
        f"| `STARTUP_PROBE_FAILED` count | {stats.startup_probe_failed} | == 0 | {_slo_marker(probe_ok)} |",
        f"| `FIELD_PATCH_HIT` count | {stats.field_patch_hits} | — | — |",
        f"| `FIELD_PATCH_DRIFT` count | {stats.field_patch_drift} | — | — |",
        f"| `LLM_GATE_RELAXED` count | {stats.llm_gate_relaxed} | — | — |",
        "",
    ]

    # MAPPING_SAVE_DROPPED breakdown by reason (reason cardinality is small —
    # 3 reasons today: empty_pattern, empty_paths_and_envelope, disabled_by_flag).
    if stats.mapping_save_dropped:
        lines += [
            "### MAPPING_SAVE_DROPPED reasons",
            "",
            "| Reason | Count |",
            "|---|---|",
        ]
        for reason, n in stats.mapping_save_dropped.most_common():
            lines.append(f"| `{reason}` | {n} |")
        lines.append("")

    # DB section (only when --check-db was passed and the query succeeded)
    if stats.db_persistence_health:
        lines += [
            "### DB row counts (Q4 channel asymmetry)",
            "",
            "Same query is at `scripts/diagnostics/profile_persistence_health.sql :: Q4_channel_row_counts`. "
            "When two channels differ by >100x and they share `update_profile_after_extraction` "
            "as their writer, a writer is broken — see `project_self_learning_loop_arch.md` for "
            "the diagnostic discipline.",
            "",
        ]
        for query_name, rows in stats.db_persistence_health.items():
            if not rows:
                continue
            lines.append(f"#### {query_name}")
            lines.append("")
            cols = list(rows[0].keys())
            lines.append("| " + " | ".join(cols) + " |")
            lines.append("|" + "|".join("---" for _ in cols) + "|")
            for r in rows:
                lines.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
            lines.append("")

    # Page the operator: any ALERT row gets a loud final note so it's
    # impossible to miss when skimming the report.
    alerts = []
    if not drop_ok:
        alerts.append(f"mapping_save_drop_rate {drop_rate:.1%} > {SLO_MAPPING_SAVE_DROP_RATE_MAX:.0%}")
    if replay_total > 0 and not replay_ok:
        alerts.append(f"profile_replay_hit_rate {replay_hit_rate:.1%} < {SLO_PROFILE_REPLAY_HIT_RATE_MIN:.0%}")
    if not update_ok:
        alerts.append(f"profile_update_failed {stats.profile_update_failed} > {SLO_PROFILE_UPDATE_FAILED_MAX}")
    if not probe_ok:
        alerts.append(f"startup_probe_failed {stats.startup_probe_failed} > 0 — RUNNER FAILED DEPLOY GUARD")
    if alerts:
        lines.append("> **🚨 PERSISTENCE-LOOP ALERT — page on-call.**")
        for a in alerts:
            lines.append(f"> - {a}")
        lines.append("")

    return lines


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
        # Bug #8 fix (2026-05-16): clamp denominator to max(seen, expected)
        # so we never render confusing "100 / 20" when the CLI invocation
        # forgot --expected-shards. The shards_seen count is authoritative.
        f"**Shards seen:** {len(stats.shards_seen)} / {max(stats.shards_expected, len(stats.shards_seen))}",
        "",
        "## Top-line numbers",
        "",
        f"- **Properties processed:** {stats.properties_total}",
        f"- **SUCCESS (incl. SUCCESS_PARTIAL):** {stats.properties_succeeded} ({stats.success_rate_pct}%)",
        f"  ↳ of which SUCCESS_PARTIAL (timeout-rescued, real units shipped): "
        f"**{stats.properties_success_partial}** ({stats.properties_success_partial_units_total} units total)",
        f"- **FAILED_NO_DATA:** {stats.properties_failed_no_data}",
        f"- **FAILED_UNREACHABLE:** {stats.properties_failed_unreachable}",
        f"- **PARTIAL (validation-majority-rejected — data-quality alert):** "
        f"{stats.properties_partial_validation_rejected}",
        f"- **DEAD_URL:** {stats.properties_dead_url}",
        f"- **Other (no verdict match — should be 0):** {stats.properties_failed_other}",
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

    # PR 3 (2026-05-10): persistence-health SLO section. Goes near the top
    # so a regression of the self-learning loop surfaces in the first
    # screen of summary.md, not below 200 lines of failure tables.
    lines += render_persistence_health_md(stats)

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

    # Bug #11 fix (2026-05-16): entry-vs-subpath bot-block split. The raw
    # ``fetch_signatures`` above counts only the FIRST fetch attempt per
    # property — that's correct for entry-level. But the per-property
    # ``bot_blocked`` boolean (used by the pattern classifier and the
    # ``bot_blocked_properties.json`` artifact) gets set by any hop-side
    # CF hit, inflating the apparent CF-block rate. Surface the split
    # explicitly here so operators triaging a SLO miss know which is
    # which.
    _entry_bb = sum(1 for o in outcomes.values() if o.entry_bot_blocked)
    _entry_cd = sum(1 for o in outcomes.values() if o.entry_captcha_detected)
    _agg_bb = sum(1 for o in outcomes.values() if o.bot_blocked)
    _agg_cd = sum(1 for o in outcomes.values() if o.captcha_detected)
    lines += [
        "",
        "## Bot-block / captcha — entry vs subpath split",
        "",
        "| Signal | Entry-level (SLO signal) | Anywhere (entry OR hop) | Δ (subpath-only) |",
        "|---|---|---|---|",
        f"| BOT_BLOCKED | {_entry_bb} | {_agg_bb} | {_agg_bb - _entry_bb} |",
        f"| captcha_detected | {_entry_cd} | {_agg_cd} | {_agg_cd - _entry_cd} |",
        "",
        "> Subpath-only counts are extraction gaps (iframe-harvest miss, "
        "hop-redirect-loop), not fetch-infrastructure problems. Treat them "
        "as adapter bugs, not proxy issues.",
    ]

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

    # T3 (2026-05-20): Date-completeness sub-cause split. Reads the per-
    # property date-shape signals collected from extract.html_characterized
    # + extract.date_presence_summary events. Classifies each SUCCESS
    # property with date-fill < 50% as A/B/C/D/E (see playbook §19).
    lines += _render_date_gap_subcause_section(outcomes)

    # LLM rescue effectiveness — surfaces ROI per source adapter so a
    # gate-widening change (e.g. F1.3 adding onesite/amli) is visible the
    # next morning rather than only in the LLM-cost line.
    if stats.rescue_attempted_by_adapter:
        total_attempts = sum(stats.rescue_attempted_by_adapter.values())
        total_succ = sum(stats.rescue_succeeded_by_adapter.values())
        roi_pct = 100.0 * total_succ / total_attempts if total_attempts else 0.0
        lines += [
            "## LLM rescue effectiveness (F2 gate)",
            "",
            f"Total attempts **{total_attempts}** · successes **{total_succ}** · "
            f"ROI **{roi_pct:.1f}%** · spent **${stats.rescue_total_cost:.2f}**",
            "",
            "| Source adapter | Attempts | Successes | ROI |",
            "|---|---|---|---|",
        ]
        for adapter in sorted(
            stats.rescue_attempted_by_adapter,
            key=lambda a: -stats.rescue_attempted_by_adapter[a],
        ):
            att = stats.rescue_attempted_by_adapter[adapter]
            succ = stats.rescue_succeeded_by_adapter.get(adapter, 0)
            r = 100.0 * succ / att if att else 0.0
            lines.append(f"| `{adapter}` | {att} | {succ} | {r:.1f}% |")
        lines.append("")

    # Named-fix telemetry — count of "claimed fix actually fired" events.
    # Zero in any cell means the fix code path is dead in production —
    # treat that as a regression even when the headline numbers look fine.
    if NAMED_FIX_PATTERNS:
        lines += [
            "## Named-fix exercise counts",
            "",
            "Each row is a known fix path with the count of events showing it actually fired this run. "
            "**Zero** = dead code (fix shipped but never executes); compare day-over-day to detect regressions.",
            "",
            "| Fix codename | Description | Fired |",
            "|---|---|---|",
        ]
        for codename, desc in NAMED_FIX_PATTERNS.items():
            n = stats.named_fix_events.get(codename, 0)
            marker = " :warning:" if n == 0 else ""
            lines.append(f"| `{codename}` | {desc} | {n}{marker} |")
        lines.append("")

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

    # Marketing-shell pattern — the dominant 2026-05-10 P4 cluster. Pages
    # that have an ApartmentComplex JSON-LD + heavy script payload but no
    # extractable unit data on the entry URL itself.
    shells = [o for o in failed if is_marketing_shell(o)]
    if shells:
        terminal_breakdown: Counter = Counter(o.terminal_tier or "(none)" for o in shells)
        # Did the LLM tiers actually fire on these, or were they gated off?
        # Distinguishes "we tried LLM and it returned empty" from "LLM was
        # blocked by the gate" — different fixes apply to each.
        llm_ran = sum(
            1 for o in shells
            if o.tier_attempts.get("generic:llm") == "ran_empty"
            or o.tier_attempts.get("generic:llm_dom_targeted") == "ran_empty"
        )
        llm_skipped_gate = sum(
            1 for o in shells
            if o.tier_attempts.get("generic:llm") == "skipped"
            or o.tier_attempts.get("generic:llm_dom_targeted") == "skipped"
        )
        lines += [
            f"## Marketing-shell pattern (Entrata-class regression): {len(shells)} properties",
            "",
            "ApartmentComplex JSON-LD + heavy script + 0–1 rent tokens. Entry URL is a "
            "marketing wrapper; real unit data lives on a leasing portal we never load.",
            "",
            f"- LLM tier ran but returned empty: **{llm_ran}** (need a probe / sub-page strategy, not more LLM)",
            f"- LLM tier was gated off: **{llm_skipped_gate}** (need to relax the gate for this signature)",
            "",
            "| Terminal tier | Count |",
            "|---|---|",
        ]
        for tier, n in terminal_breakdown.most_common():
            lines.append(f"| `{tier}` | {n} |")
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
    """Write one row per failed property — the canonical failure-list source.

    Restored after commit f11b6dc added ``render_successes_csv`` for the
    canary regression basket but left ``write_outputs`` calling
    ``render_failures_csv``, which had been removed by the same commit's
    rename. Without this, ``analyze_cloud_run`` crashes at the end of
    every invocation with ``NameError: name 'render_failures_csv' is
    not defined``. Schema mirrors the commit-6cf2389 version (24 cols).
    """
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
            # Bug #11 fix (2026-05-16): entry-vs-aggregate split surfaced in
            # failures.csv so triage can sort to "real entry-CF" cases
            # without grep-fighting the hop-side label leak.
            "entry_captcha_detected": o.entry_captcha_detected,
            "entry_bot_blocked": o.entry_bot_blocked,
            "llm_rescue_attempted": o.llm_rescue_attempted,
            "llm_rescue_succeeded": o.llm_rescue_succeeded,
            "llm_rescue_source_adapter": o.llm_rescue_source_adapter or "",
            "llm_cost": round(o.llm_cost, 5),
            "link_hops_attempted": o.link_hops_attempted,
            "link_hops_recovered": o.link_hops_recovered,
            "issue_codes": "|".join(o.issue_codes),
            "pattern_id": pattern_id,
            "pattern_sub": sub,
            "marketing_shell": is_marketing_shell(o),
            "text_bytes": o.text_bytes or 0,
            "rent_signal_count": o.rent_signal_count if o.rent_signal_count is not None else "",
            "jsonld_types": "|".join(o.jsonld_types),
            "fingerprints_matched": "|".join(o.fingerprints_matched),
            "llm_tier_outcome": o.tier_attempts.get("generic:llm", ""),
            "llm_dom_tier_outcome": o.tier_attempts.get("generic:llm_dom_targeted", ""),
            "llm_gate_relaxed_reason": o.llm_gate_relaxed_reason or "",
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


def render_successes_csv(outcomes: dict[str, PropertyOutcome], out_path: Path) -> None:
    """Write one row per successful property for the canary regression basket.

    Schema mirrors failures.csv with the addition of a ``units`` column so the
    canary report can show cloud_units for regression-sentinel properties.

    The success predicate is delegated to ``PropertyOutcome.succeeded``
    so new verdict classes (``SUCCESS_PLAN_LEVEL``, ``SUCCESS_PARTIAL``)
    flow through automatically. ``CARRY_FORWARD`` is admitted separately
    — it isn't a fresh-scrape success (so production's
    ``reporting.verdict._SUCCESS_VERDICTS`` excludes it from the success
    rate) but downstream canary sentinels need CARRY_FORWARD rows in
    successes.csv to detect regressions in the carry-forward path
    itself. The previous local literal ``{"SUCCESS", "CARRY_FORWARD"}``
    silently dropped SUCCESS_PLAN_LEVEL / SUCCESS_PARTIAL — verified on
    2026-05-18 when PID 17102 (irvinecompanyapartments.com,
    SUCCESS_PLAN_LEVEL) was absent from both successes.csv and
    failures.csv.
    """
    rows = []
    for o in outcomes.values():
        if not (o.succeeded or o.verdict == "CARRY_FORWARD"):
            continue
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
            "units": o.units,
            "captcha_detected": o.captcha_detected,
            "bot_blocked": o.bot_blocked,
            # Bug #11 fix (2026-05-16): entry-vs-aggregate split surfaced in
            # failures.csv so triage can sort to "real entry-CF" cases
            # without grep-fighting the hop-side label leak.
            "entry_captcha_detected": o.entry_captcha_detected,
            "entry_bot_blocked": o.entry_bot_blocked,
            "llm_rescue_attempted": o.llm_rescue_attempted,
            "llm_rescue_succeeded": o.llm_rescue_succeeded,
            "llm_cost": round(o.llm_cost, 5),
            "link_hops_attempted": o.link_hops_attempted,
            "link_hops_recovered": o.link_hops_recovered,
            "issue_codes": "|".join(o.issue_codes),
            "final_url": o.final_url or "",
        })
    rows.sort(key=lambda r: (r["terminal_tier"], r["pms_detected"], r["property_id"]))

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
        f"| Succeeded (incl. SUCCESS_PARTIAL) | {today_stats.properties_succeeded} | {prev_stats.properties_succeeded} | {today_stats.properties_succeeded - prev_stats.properties_succeeded:+d} |",
        f"|   ↳ SUCCESS_PARTIAL (timeout-rescued with units) | {today_stats.properties_success_partial} | {prev_stats.properties_success_partial} | {today_stats.properties_success_partial - prev_stats.properties_success_partial:+d} |",
        f"| Failed (no data) | {today_stats.properties_failed_no_data} | {prev_stats.properties_failed_no_data} | {today_stats.properties_failed_no_data - prev_stats.properties_failed_no_data:+d} |",
        f"| Failed (unreachable) | {today_stats.properties_failed_unreachable} | {prev_stats.properties_failed_unreachable} | {today_stats.properties_failed_unreachable - prev_stats.properties_failed_unreachable:+d} |",
        f"| PARTIAL (validation-majority-rejected) | {today_stats.properties_partial_validation_rejected} | {prev_stats.properties_partial_validation_rejected} | {today_stats.properties_partial_validation_rejected - prev_stats.properties_partial_validation_rejected:+d} |",
        f"| Dead URL | {today_stats.properties_dead_url} | {prev_stats.properties_dead_url} | {today_stats.properties_dead_url - prev_stats.properties_dead_url:+d} |",
        f"| Other (verdict mismatch — should be 0) | {today_stats.properties_failed_other} | {prev_stats.properties_failed_other} | {today_stats.properties_failed_other - prev_stats.properties_failed_other:+d} |",
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
    render_successes_csv(outcomes, out_dir / "successes.csv")

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
        "rescue_attempted_by_adapter": dict(stats.rescue_attempted_by_adapter),
        "rescue_succeeded_by_adapter": dict(stats.rescue_succeeded_by_adapter),
        "rescue_total_cost": round(stats.rescue_total_cost, 4),
        "named_fix_events": dict(stats.named_fix_events),
        "slo_breaches": stats.slo_breaches,
        # PR 3 (2026-05-10): persistence-health metrics for downstream
        # alerting / dashboards. Same numbers the markdown table renders;
        # JSON form is the canonical machine-readable surface.
        "persistence_health": {
            "mapping_save_dropped": dict(stats.mapping_save_dropped),
            "profile_update_failed": stats.profile_update_failed,
            "startup_probe_ok": stats.startup_probe_ok,
            "startup_probe_failed": stats.startup_probe_failed,
            "profile_replay_hits": stats.profile_replay_hits,
            "profile_replay_miss_with_saved": stats.profile_replay_miss_with_saved,
            "field_patch_hits": stats.field_patch_hits,
            "field_patch_drift": stats.field_patch_drift,
            "llm_gate_relaxed": stats.llm_gate_relaxed,
            "db": stats.db_persistence_health,
        },
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
        default=100,
        help="Expected shard count (for missing-shard detection). Default "
        "100 matches production shard fan-out as of 2026-05. Bug #8 fix "
        "(2026-05-16): old default of 20 caused summary.md to render "
        "'Shards seen: 100 / 20' when invoked without --expected-shards, "
        "which read as a regression to anyone skimming the report.",
    )
    p.add_argument(
        "--check-db",
        action="store_true",
        help=(
            "Run profile_persistence_health.sql against the live DB and "
            "include results in the persistence-health section of summary.md. "
            "Requires DATABASE_URL + cloud-sql-proxy reachable. Skipped "
            "silently with a warning if the DB is unreachable."
        ),
    )
    return p.parse_args()


def _maybe_attach_db_persistence_health(stats: RunStats) -> None:
    """If db_query.py + DATABASE_URL are reachable, populate stats.db_persistence_health.

    Best-effort. Any failure is logged to stderr and stats is left untouched —
    the persistence-health section of summary.md will still render with the
    event-side counters.
    """
    sql_path = REPO_ROOT / "scripts" / "diagnostics" / "profile_persistence_health.sql"
    if not sql_path.exists():
        print(f"[warn] --check-db: SQL file not found at {sql_path}; skipping DB section.", file=sys.stderr)
        return
    try:
        # Sibling-import: the analyzer and db_query both live under
        # scripts/diagnostics/, so importing as a sibling avoids the
        # ma_poc.* prefix issue when the analyzer is invoked from inside
        # ma_poc/ (where ma_poc isn't a package — it IS the cwd).
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from db_query import run_named  # type: ignore[import-not-found]
        finally:
            # Don't pollute sys.path beyond the import.
            sys.path.pop(0)
        # Only the channel-asymmetry query is small and fast enough for the
        # daily report. Other queries in the file are for ad-hoc inspection
        # via the standalone CLI.
        results = run_named(sql_path, query_name="Q4_channel_row_counts")
        stats.db_persistence_health = results
    except Exception as exc:
        print(f"[warn] --check-db: DB query failed ({type(exc).__name__}: {exc}); skipping DB section.", file=sys.stderr)


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
    if args.check_db:
        _maybe_attach_db_persistence_health(stats)

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
