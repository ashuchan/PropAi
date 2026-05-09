"""Per-property failure debug bundles for a given run_date.

For every property that did **not** succeed on the requested day, this script
gathers every available debug breadcrumb from Cloud SQL **and** Cloud Storage
into one self-contained bundle per ``canonical_id`` so an operator can triage
without hunting across consoles.

What's collected per failed property
------------------------------------

Cloud SQL (``DATABASE_URL`` / ``CLOUD_SQL_INSTANCE``):

  • ``properties``           — current identity + ``last_scrape_status`` /
                                ``last_units_count``
  • ``run_ledger``           — this run's status, counts, scraped URL,
                                carry-forward + scrape-failed flags
  • ``run_issues``           — every error / warning row for the cid
  • ``scrape_events``        — last attempts (default 10): outcome,
                                ``failure_reason``, ``page_load_ms``,
                                ``extraction_tier``, proxy + vision flags
  • ``scrape_profiles``      — full profile JSON (maturity, ``preferred_tier``,
                                ``blocked_endpoints``, ``llm_field_mappings``,
                                ``explored_links``, ``known_endpoints``,
                                ``confidence``)
  • ``property_snapshots``   — full payload INCLUDING the internal debug keys:
                                ``_meta``, ``_extract_result``,
                                ``_explored_links``, ``_raw_api_responses``,
                                ``_winning_page_url``, ``_llm_interactions``,
                                ``_llm_navigation_hints``,
                                ``_llm_analysis_results``,
                                ``_filtered_apis``, ``links_found``,
                                ``property_links_crawled``
  • ``dlq_entries``          — if the property is currently parked, why and
                                when it'll retry
  • ``llm_property_details`` — per-property LLM call summary for this run
  • ``llm_diagnostics``      — every diagnostic blob (one per ``kind``)

Cloud Storage (``gs://{BUCKET_NAME}/runs/{date}/shard_*/``) — when
``--include-gcs`` is passed or SQL has aged the run out (3-day retention):

  • ``property_reports/{cid}.md`` — phase-by-phase markdown narrative
                                     produced by ``scrape_report.py``
  • ``raw_api/{cid}.json``        — every URL the page hit, response bodies,
                                     and the URLs that were filtered out by
                                     the false-positive blocklist
  • ``llm_report/{cid}.json``     — full LLM prompts + responses + tokens +
                                     cost per call

These are copied **verbatim** into the per-property bundle so the bundle is
fully self-contained — no follow-up ``gsutil`` needed.

Output layout
-------------

::

    {out_dir}/
        index.md                       # Top-level summary + per-cid links
        index.json                     # Same data as index.md, structured
        {canonical_id}/
            summary.md                 # Human-readable triage summary
            summary.json               # Same fields, structured
            scrape_profile.json        # Full profile payload
            scrape_events.jsonl        # Latest events
            run_issues.jsonl           # All issue rows for this cid
            llm_property_detail.json   # If present in SQL
            llm_diagnostics.jsonl      # If present in SQL
            property_snapshot.json     # Full payload incl. internal keys
            dlq_entry.json             # If currently parked
            (gcs) property_report.md   # Copied from shard if --include-gcs
            (gcs) raw_api.json         # Copied from shard if --include-gcs
            (gcs) llm_report.json      # Copied from shard if --include-gcs

Default ``out_dir`` is ``ma_poc/data/runs/{run_date}/failure_debug/``.

Resolution order for run_date (mirrors ``email_daily_failures_report.py``):

  1. ``--run-date YYYY-MM-DD`` if provided.
  2. Today (UTC) if there's a row for it in ``runs``.
  3. Newest entry in ``runs.list_runs()``.
  4. Newest ``YYYY-MM-DD/`` prefix in ``gs://{BUCKET_NAME}/runs/`` if SQL is
     empty AND ``BUCKET_NAME`` is set.

Usage
-----

::

    # Today UTC, latest run, SQL only
    python scripts/failure_debug_summary.py

    # Specific date, full bundle (SQL + GCS artifacts)
    python scripts/failure_debug_summary.py --run-date 2026-05-07 --include-gcs

    # Backdated past the SQL retention window — forces GCS read
    python scripts/failure_debug_summary.py --run-date 2026-05-01 --use-gcs

    # Only specific properties (still ignored if they actually succeeded)
    python scripts/failure_debug_summary.py --canonical-ids 12345,67890

    # Pin output dir
    python scripts/failure_debug_summary.py --out-dir C:/tmp/failures

    # Override DB (asyncpg URLs auto-translate to pg8000)
    python scripts/failure_debug_summary.py \\
        --database-url postgresql+pg8000://user:pass@host:5432/proppy

Required env (already in ``ma_poc/.env``):

  DATABASE_URL          postgresql+pg8000://... (or asyncpg, auto-translated)
  CLOUD_SQL_INSTANCE    project:region:instance — used on Cloud Run
  BUCKET_NAME           For the GCS path. Without it ``--include-gcs`` and
                        ``--use-gcs`` are no-ops.
  DATA_PROVIDER         postgres | sqlite

Run it on Cloud Run via ``jugnu-adhoc-{env}`` — see
``gcp/CLAUDE_ADHOC_RUNNER.md`` for the console + gcloud invocation.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_MA_POC_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _MA_POC_ROOT.parent
for _p in (_MA_POC_ROOT, _REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from data_provider.sql.models import (  # noqa: E402
    DlqEntryRow,
    LlmDiagnosticRow,
    LlmPropertyDetailRow,
    PropertyRow,
    PropertySnapshotRow,
    RunIssueRow,
    RunLedgerRow,
    ScrapeEventRow,
    ScrapeProfileRow,
)
from data_provider.sql.provider import SqlDataProvider  # noqa: E402
from scripts.email.daily_failures import (  # noqa: E402
    _find_latest_gcs_run,
    _gcs_run_dir_exists,
)
from scripts.email.daily import _open_provider  # noqa: E402

log = logging.getLogger("failure_debug_summary")


# ── Run-date resolution ──────────────────────────────────────────────────────


def _resolve_run_date(
    provider: SqlDataProvider,
    requested: str | None,
    *,
    bucket: str | None,
) -> tuple[str, bool]:
    """Pick the run_date to summarise + flag whether GCS has the shards."""
    sql_dates: list[str] = []
    try:
        sql_dates = provider.runs.list_runs()
    except Exception as exc:  # noqa: BLE001
        log.warning("runs.list_runs() failed (%s); will try GCS fallback", exc)

    today = datetime.now(UTC).date().isoformat()

    chosen: str | None = None
    if requested:
        chosen = requested
    elif today in sql_dates:
        chosen = today
    elif sql_dates:
        chosen = sql_dates[-1]
        log.warning("No run for %s yet; using latest SQL run %s", today, chosen)

    if chosen is None and bucket:
        chosen = _find_latest_gcs_run(bucket)
        if chosen:
            log.warning("Cloud SQL has no runs; using latest GCS run %s", chosen)

    if chosen is None:
        raise RuntimeError(
            "No runs found in Cloud SQL or GCS. Set BUCKET_NAME and confirm "
            "the runner is uploading to gs://{bucket}/runs/{date}/."
        )

    gcs_present = _gcs_run_dir_exists(bucket, chosen) if bucket else False
    return chosen, gcs_present


# ── Cloud SQL reads ──────────────────────────────────────────────────────────


def _list_failed_cids(
    session: Session, run_date: str, only_cids: set[str] | None
) -> list[tuple[str, RunLedgerRow]]:
    """Return ``(cid, ledger_row)`` for every property whose ledger status is
    not ``SUCCESS``. ``CARRY_FORWARD`` and ``FAILED*`` variants are included —
    the operator usually wants to see those too. Ordered by ledger ``seq``.
    """
    rows = session.execute(
        select(RunLedgerRow)
        .where(RunLedgerRow.run_date == run_date)
        .order_by(RunLedgerRow.seq.asc())
    ).scalars().all()

    out: list[tuple[str, RunLedgerRow]] = []
    for r in rows:
        cid = r.canonical_id or ""
        if not cid:
            continue
        status = (r.status or "").upper()
        if not status or status == "SUCCESS":
            continue
        if only_cids is not None and cid not in only_cids:
            continue
        out.append((cid, r))
    return out


def _fetch_property_row(session: Session, cid: str) -> PropertyRow | None:
    return session.execute(
        select(PropertyRow).where(PropertyRow.canonical_id == cid)
    ).scalar_one_or_none()


def _fetch_run_issues(session: Session, run_date: str, cid: str) -> list[RunIssueRow]:
    return list(session.execute(
        select(RunIssueRow)
        .where(RunIssueRow.run_date == run_date)
        .where(RunIssueRow.canonical_id == cid)
        .order_by(RunIssueRow.seq.asc())
    ).scalars())


def _fetch_scrape_events(
    session: Session, cid: str, *, limit: int
) -> list[ScrapeEventRow]:
    """Latest ``limit`` scrape_events (newest first). property_id == cid in
    the upserted state, so we filter on that column directly. Subject to the
    3-day retention sweep, so older runs may return nothing here."""
    return list(session.execute(
        select(ScrapeEventRow)
        .where(ScrapeEventRow.property_id == cid)
        .order_by(ScrapeEventRow.scrape_timestamp.desc())
        .limit(limit)
    ).scalars())


def _fetch_scrape_profile(session: Session, cid: str) -> ScrapeProfileRow | None:
    return session.execute(
        select(ScrapeProfileRow).where(ScrapeProfileRow.canonical_id == cid)
    ).scalar_one_or_none()


def _fetch_snapshot(
    session: Session, run_date: str, cid: str
) -> PropertySnapshotRow | None:
    return session.execute(
        select(PropertySnapshotRow)
        .where(PropertySnapshotRow.run_date == run_date)
        .where(PropertySnapshotRow.canonical_id == cid)
    ).scalar_one_or_none()


def _fetch_dlq_entry(session: Session, cid: str) -> DlqEntryRow | None:
    return session.execute(
        select(DlqEntryRow).where(DlqEntryRow.property_id == cid)
    ).scalar_one_or_none()


def _fetch_llm_detail(
    session: Session, run_date: str, cid: str
) -> LlmPropertyDetailRow | None:
    return session.execute(
        select(LlmPropertyDetailRow)
        .where(LlmPropertyDetailRow.run_date == run_date)
        .where(LlmPropertyDetailRow.property_id == cid)
    ).scalar_one_or_none()


def _fetch_llm_diagnostics(
    session: Session, run_date: str, cid: str
) -> list[LlmDiagnosticRow]:
    return list(session.execute(
        select(LlmDiagnosticRow)
        .where(LlmDiagnosticRow.run_date == run_date)
        .where(LlmDiagnosticRow.property_id == cid)
        .order_by(LlmDiagnosticRow.kind.asc())
    ).scalars())


# ── Cloud Storage reads ──────────────────────────────────────────────────────


def _download_run_dir(bucket: str, run_date: str) -> Path:
    """Download every shard under ``gs://{bucket}/runs/{run_date}/`` into a
    temp dir. Returns the local root. Caller is responsible for cleanup.
    """
    from ma_poc.storage import gcs

    work_dir = Path(tempfile.mkdtemp(prefix=f"failure_debug_{run_date}_"))
    uri_prefix = f"gs://{bucket}/runs/{run_date}/"
    log.info("Downloading %s into %s", uri_prefix, work_dir)
    count = gcs.download_prefix(uri_prefix, work_dir)
    log.info("Downloaded %d objects from %s", count, uri_prefix)
    return work_dir


def _find_in_shards(work_dir: Path, sub_path: str) -> Path | None:
    """Search every ``shard_*/`` subdir for ``sub_path`` (e.g.
    ``raw_api/12345.json``). Returns the first hit or None."""
    if not work_dir.exists():
        return None
    for shard_dir in sorted(work_dir.iterdir()):
        if not shard_dir.is_dir() or not shard_dir.name.startswith("shard_"):
            continue
        candidate = shard_dir / sub_path
        if candidate.exists():
            return candidate
    return None


# ── Bundle builders ──────────────────────────────────────────────────────────


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a SQLAlchemy ORM row to a JSON-friendly dict.

    Skips SQLAlchemy internals, stringifies ``datetime`` so ``json.dumps`` is
    happy. Pydantic ``mode="json"`` doesn't apply here — these are ORM rows.
    """
    if row is None:
        return {}
    out: dict[str, Any] = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, datetime):
            out[column.name] = value.isoformat()
        else:
            out[column.name] = value
    return out


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip().replace("\n", " ").replace("\r", " ")
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _build_summary_dict(
    *,
    run_date: str,
    cid: str,
    ledger: RunLedgerRow,
    prop: PropertyRow | None,
    issues: list[RunIssueRow],
    events: list[ScrapeEventRow],
    profile: ScrapeProfileRow | None,
    snapshot: PropertySnapshotRow | None,
    dlq: DlqEntryRow | None,
    llm_detail: LlmPropertyDetailRow | None,
    llm_diagnostics: list[LlmDiagnosticRow],
    gcs_artifacts: dict[str, str],
) -> dict[str, Any]:
    """Assemble the per-property structured summary."""
    payload = (snapshot.payload if snapshot else None) or {}
    meta_block = payload.get("_meta") or {}
    extract_block = payload.get("_extract_result") or {}
    explored_links = payload.get("_explored_links") or {}
    raw_apis = payload.get("_raw_api_responses") or []
    filtered_apis = payload.get("_filtered_apis") or []
    links_found = payload.get("links_found") or []
    crawled_links = payload.get("property_links_crawled") or []
    llm_interactions = payload.get("_llm_interactions") or []
    llm_analysis = payload.get("_llm_analysis_results") or {}
    nav_hints = payload.get("_llm_navigation_hints") or []
    winning_page = payload.get("_winning_page_url") or ""

    profile_payload = (profile.payload if profile else None) or {}
    confidence = profile_payload.get("confidence") or {}
    api_hints = profile_payload.get("api_hints") or {}
    navigation = profile_payload.get("navigation") or {}

    latest_event = events[0] if events else None

    return {
        "canonical_id": cid,
        "run_date": run_date,
        "identity": {
            "property_name": (prop.proj_name if prop else None) or "",
            "address": (prop.address if prop else None) or "",
            "city": (prop.city if prop else None) or "",
            "state": (prop.state if prop else None) or "",
            "zip_code": (prop.zip_code if prop else None) or "",
            "website": (prop.website if prop else None) or ledger.url or "",
            "pmc": (prop.pmc if prop else None) or "",
            "last_scrape_status": (prop.last_scrape_status if prop else None) or "",
            "last_units_count": prop.last_units_count if prop else None,
            "first_seen_date": (prop.first_seen_date if prop else None) or "",
        },
        "ledger": {
            "status": (ledger.status or "").upper(),
            "url": ledger.url or "",
            "units_count": ledger.units_count or 0,
            "carry_forward_used": bool(ledger.carry_forward_used),
            "scrape_failed": bool(ledger.scrape_failed),
            "error_count": ledger.error_count or 0,
            "warning_count": ledger.warning_count or 0,
            "timestamp": ledger.timestamp or "",
            "extra": ledger.extra or {},
        },
        "verdict": {
            "verdict": meta_block.get("verdict") or "",
            "tier_used": (
                meta_block.get("scrape_tier_used")
                or extract_block.get("tier_used")
                or ""
            ),
            "extract_errors": extract_block.get("errors") or [],
            "llm_calls": extract_block.get("llm_calls"),
            "llm_cost_usd": extract_block.get("llm_cost_usd"),
            "vision_calls": extract_block.get("vision_calls"),
            "vision_cost_usd": extract_block.get("vision_cost_usd"),
        },
        "issues": [
            {
                "seq": i.seq,
                "severity": i.severity,
                "code": i.code,
                "message": _truncate(i.message or "", 600),
                "details": i.details or {},
                "timestamp": i.timestamp or "",
            }
            for i in issues
        ],
        "issue_code_counts": dict(Counter(i.code for i in issues if i.code)),
        "scrape_events": [_row_to_dict(e) for e in events],
        "latest_event": (
            {
                "scrape_timestamp": latest_event.scrape_timestamp.isoformat()
                if latest_event and latest_event.scrape_timestamp
                else "",
                "scrape_outcome": latest_event.scrape_outcome if latest_event else "",
                "failure_reason": _truncate(
                    (latest_event.failure_reason if latest_event else "") or "", 1000
                ),
                "extraction_tier": latest_event.extraction_tier if latest_event else None,
                "page_load_ms": latest_event.page_load_ms if latest_event else None,
                "proxy_used": bool(latest_event.proxy_used) if latest_event else False,
                "proxy_provider": (latest_event.proxy_provider if latest_event else "") or "",
                "vision_fallback_used": bool(latest_event.vision_fallback_used)
                if latest_event
                else False,
                "confidence_score": latest_event.confidence_score if latest_event else None,
            }
            if latest_event
            else {}
        ),
        "urls": {
            "scraped_url": ledger.url or "",
            "winning_page_url": winning_page,
            "links_found_count": len(links_found),
            "links_crawled_count": len(crawled_links),
            "explored_count": len(explored_links),
            "explored_with_data": [k for k, v in explored_links.items() if v],
            "explored_no_data": [k for k, v in explored_links.items() if not v],
            "links_found": list(links_found)[:50],
            "links_crawled": list(crawled_links)[:50],
            "navigation_hints": list(nav_hints)[:20],
        },
        "apis": {
            "captured_count": len(raw_apis),
            "filtered_count": len(filtered_apis),
            "captured_urls": [
                a.get("url", "") for a in raw_apis if isinstance(a, dict)
            ][:50],
            "filtered_urls": list(filtered_apis)[:50],
            "llm_analysis_results": {
                k: (v if isinstance(v, str) else "<mapping_object>")
                for k, v in llm_analysis.items()
            },
        },
        "llm": {
            "interactions_count": len(llm_interactions),
            "interactions_summary": [
                {
                    "tier": x.get("tier"),
                    "call_type": x.get("call_type"),
                    "model": x.get("model"),
                    "tokens_total": x.get("tokens_total"),
                    "cost_usd": x.get("cost_usd"),
                    "success": x.get("success"),
                    "error": _truncate(str(x.get("error") or ""), 200),
                }
                for x in llm_interactions
                if isinstance(x, dict)
            ][:20],
            "property_detail_present": llm_detail is not None,
            "diagnostic_kinds": [d.kind for d in llm_diagnostics],
        },
        "profile": {
            "present": profile is not None,
            "version": profile.version if profile else None,
            "updated_by": profile.updated_by if profile else "",
            "updated_at": (
                profile.updated_at.isoformat()
                if profile and profile.updated_at
                else ""
            ),
            "maturity": confidence.get("maturity") or "",
            "preferred_tier": confidence.get("preferred_tier"),
            "last_success_tier": confidence.get("last_success_tier"),
            "consecutive_successes": confidence.get("consecutive_successes"),
            "consecutive_failures": confidence.get("consecutive_failures"),
            "last_unit_count": confidence.get("last_unit_count"),
            "api_provider": api_hints.get("api_provider") or "",
            "known_endpoints_count": len(api_hints.get("known_endpoints") or []),
            "blocked_endpoints_count": len(api_hints.get("blocked_endpoints") or []),
            "blocked_endpoints_top": [
                {"url": b.get("url_pattern"), "reason": b.get("reason")}
                for b in (api_hints.get("blocked_endpoints") or [])[:10]
                if isinstance(b, dict)
            ],
            "llm_field_mappings_count": len(api_hints.get("llm_field_mappings") or []),
            "navigation_entry_url": navigation.get("entry_url") or "",
            "navigation_winning_url": navigation.get("winning_page_url") or "",
        },
        "dlq": (
            {
                "parked_at": dlq.parked_at,
                "reason": dlq.reason,
                "last_error_signature": dlq.last_error_signature,
                "retry_at": dlq.retry_at,
            }
            if dlq
            else None
        ),
        "gcs_artifacts": gcs_artifacts,
    }


def _render_summary_md(d: dict[str, Any]) -> str:
    """Build the human-readable triage markdown for one cid."""
    cid = d["canonical_id"]
    ident = d["identity"]
    led = d["ledger"]
    ver = d["verdict"]
    urls = d["urls"]
    apis = d["apis"]
    llm = d["llm"]
    prof = d["profile"]
    last = d["latest_event"] or {}

    lines: list[str] = []
    lines.append(f"# Failure Debug — {ident.get('property_name') or cid}")
    lines.append(f"**Canonical ID:** `{cid}`  ")
    lines.append(f"**Run Date:** {d['run_date']}  ")
    lines.append(f"**URL:** {led.get('url') or ident.get('website') or '—'}  ")
    lines.append(
        f"**Location:** {ident.get('city') or '—'}, {ident.get('state') or '—'}  "
    )
    lines.append(f"**Mgmt Co:** {ident.get('pmc') or '—'}  ")
    lines.append("")
    lines.append("## Outcome")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Ledger status | `{led.get('status') or '—'}` |")
    lines.append(f"| Verdict | `{ver.get('verdict') or '—'}` |")
    lines.append(f"| Tier used | `{ver.get('tier_used') or '—'}` |")
    lines.append(f"| Carry-forward used | {led.get('carry_forward_used')} |")
    lines.append(f"| Scrape failed flag | {led.get('scrape_failed')} |")
    lines.append(f"| Units captured | {led.get('units_count')} |")
    lines.append(f"| Errors | {led.get('error_count')} |")
    lines.append(f"| Warnings | {led.get('warning_count')} |")
    if ver.get("llm_cost_usd") is not None:
        lines.append(f"| LLM cost (USD) | {ver.get('llm_cost_usd')} |")
        lines.append(f"| LLM calls | {ver.get('llm_calls')} |")
    if ver.get("vision_cost_usd") is not None:
        lines.append(f"| Vision cost (USD) | {ver.get('vision_cost_usd')} |")
        lines.append(f"| Vision calls | {ver.get('vision_calls')} |")
    lines.append("")

    lines.append("## Latest Scrape Event")
    lines.append("")
    if last:
        lines.append(f"- **At:** {last.get('scrape_timestamp') or '—'}")
        lines.append(f"- **Outcome:** `{last.get('scrape_outcome') or '—'}`")
        lines.append(f"- **Tier:** `{last.get('extraction_tier')}`")
        lines.append(f"- **Page load (ms):** {last.get('page_load_ms')}")
        lines.append(
            f"- **Proxy:** {'yes' if last.get('proxy_used') else 'no'}"
            f" ({last.get('proxy_provider') or '—'})"
        )
        lines.append(f"- **Vision fallback:** {last.get('vision_fallback_used')}")
        lines.append(f"- **Confidence:** {last.get('confidence_score')}")
        lines.append(f"- **Failure reason:** {last.get('failure_reason') or '—'}")
    else:
        lines.append("_No scrape_events row available (likely past 3-day retention)._")
    lines.append("")

    lines.append("## Issues")
    lines.append("")
    issues = d.get("issues") or []
    code_counts = d.get("issue_code_counts") or {}
    if not issues:
        lines.append("_No run_issues rows recorded for this property._")
    else:
        lines.append("**Code counts:**")
        for code, count in sorted(code_counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"- `{code}` × {count}")
        lines.append("")
        lines.append("| # | Severity | Code | Message |")
        lines.append("|---|---|---|---|")
        for i, item in enumerate(issues[:50], 1):
            msg = (item.get("message") or "").replace("|", "\\|")
            lines.append(
                f"| {i} | {item.get('severity')} | `{item.get('code')}` | {msg} |"
            )
        if len(issues) > 50:
            lines.append("")
            lines.append(f"_…and {len(issues) - 50} more issue rows._")
    lines.append("")

    lines.append("## URLs Scraped / Filtered / Explored")
    lines.append("")
    lines.append(f"- **Scraped URL:** {urls.get('scraped_url') or '—'}")
    lines.append(f"- **Winning page:** {urls.get('winning_page_url') or '—'}")
    lines.append(f"- **Links discovered:** {urls.get('links_found_count')}")
    lines.append(f"- **Links crawled:** {urls.get('links_crawled_count')}")
    lines.append(f"- **Links explored (with data):** "
                 f"{len(urls.get('explored_with_data') or [])}")
    lines.append(f"- **Links explored (no data):** "
                 f"{len(urls.get('explored_no_data') or [])}")
    explored_with = urls.get("explored_with_data") or []
    explored_no = urls.get("explored_no_data") or []
    if explored_with:
        lines.append("")
        lines.append("**Explored with data:**")
        for u in explored_with[:20]:
            lines.append(f"- {u}")
    if explored_no:
        lines.append("")
        lines.append("**Explored, no data:**")
        for u in explored_no[:20]:
            lines.append(f"- {u}")
    nav_hints = urls.get("navigation_hints") or []
    if nav_hints:
        lines.append("")
        lines.append("**Navigation hints (LLM-suggested):**")
        for u in nav_hints:
            lines.append(f"- {u}")
    lines.append("")

    lines.append("## APIs Captured / Filtered")
    lines.append("")
    lines.append(f"- **Captured:** {apis.get('captured_count')}")
    lines.append(f"- **Filtered (false-positive blocklist):** {apis.get('filtered_count')}")
    captured = apis.get("captured_urls") or []
    if captured:
        lines.append("")
        lines.append("**Captured URLs (first 50):**")
        for u in captured:
            lines.append(f"- {u}")
    filtered = apis.get("filtered_urls") or []
    if filtered:
        lines.append("")
        lines.append("**Filtered URLs (first 50):**")
        for u in filtered:
            lines.append(f"- {u}")
    lines.append("")

    lines.append("## LLM Activity")
    lines.append("")
    lines.append(f"- **Interactions on this run:** {llm.get('interactions_count')}")
    lines.append(f"- **llm_property_details row present:** {llm.get('property_detail_present')}")
    lines.append(f"- **Diagnostic kinds:** {', '.join(llm.get('diagnostic_kinds') or []) or '—'}")
    interactions = llm.get("interactions_summary") or []
    if interactions:
        lines.append("")
        lines.append("| # | Tier | Type | Model | Tokens | Cost | Success | Error |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for i, x in enumerate(interactions, 1):
            err = (x.get("error") or "").replace("|", "\\|")
            lines.append(
                f"| {i} | `{x.get('tier')}` | {x.get('call_type')} | "
                f"`{x.get('model')}` | {x.get('tokens_total')} | "
                f"{x.get('cost_usd')} | {x.get('success')} | {err} |"
            )
    lines.append("")

    lines.append("## Scrape Profile")
    lines.append("")
    if not prof.get("present"):
        lines.append("_No scrape_profile row — property is COLD or never bootstrapped._")
    else:
        lines.append(f"- **Version:** {prof.get('version')}")
        lines.append(f"- **Updated:** {prof.get('updated_at')} by `{prof.get('updated_by')}`")
        lines.append(f"- **Maturity:** `{prof.get('maturity') or '—'}`")
        lines.append(f"- **Preferred tier:** `{prof.get('preferred_tier')}`")
        lines.append(f"- **Last success tier:** `{prof.get('last_success_tier')}`")
        lines.append(
            f"- **Consecutive: successes={prof.get('consecutive_successes')} / "
            f"failures={prof.get('consecutive_failures')}**"
        )
        lines.append(f"- **Last unit count:** {prof.get('last_unit_count')}")
        lines.append(f"- **API provider:** `{prof.get('api_provider') or '—'}`")
        lines.append(f"- **Known endpoints:** {prof.get('known_endpoints_count')}")
        lines.append(f"- **Blocked endpoints:** {prof.get('blocked_endpoints_count')}")
        blocked_top = prof.get("blocked_endpoints_top") or []
        if blocked_top:
            lines.append("")
            lines.append("**Top blocked endpoints:**")
            for b in blocked_top:
                lines.append(f"- `{b.get('url')}` — {b.get('reason')}")
        lines.append(f"- **LLM field mappings:** {prof.get('llm_field_mappings_count')}")
    lines.append("")

    if d.get("dlq"):
        dlq = d["dlq"]
        lines.append("## DLQ (currently parked)")
        lines.append("")
        lines.append(f"- **Parked at:** {dlq.get('parked_at')}")
        lines.append(f"- **Reason:** `{dlq.get('reason')}`")
        lines.append(f"- **Last error signature:** {dlq.get('last_error_signature')}")
        lines.append(f"- **Retry at:** {dlq.get('retry_at')}")
        lines.append("")

    artifacts = d.get("gcs_artifacts") or {}
    if artifacts:
        lines.append("## Bundled GCS Artifacts")
        lines.append("")
        for name, rel in artifacts.items():
            lines.append(f"- **{name}:** [{rel}]({rel})")
        lines.append("")

    return "\n".join(lines)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str))
            f.write("\n")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _safe_filename(s: str) -> str:
    """Strip path-hostile characters from canonical_id for use as a dirname."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)[:128] or "_"


# ── Main per-cid pipeline ────────────────────────────────────────────────────


def _process_cid(
    *,
    session: Session,
    run_date: str,
    cid: str,
    ledger: RunLedgerRow,
    out_dir: Path,
    events_limit: int,
    gcs_work_dir: Path | None,
) -> dict[str, Any]:
    """Build a per-property bundle and return the structured summary dict."""
    cid_dir = out_dir / _safe_filename(cid)
    cid_dir.mkdir(parents=True, exist_ok=True)

    prop = _fetch_property_row(session, cid)
    issues = _fetch_run_issues(session, run_date, cid)
    events = _fetch_scrape_events(session, cid, limit=events_limit)
    profile = _fetch_scrape_profile(session, cid)
    snapshot = _fetch_snapshot(session, run_date, cid)
    dlq = _fetch_dlq_entry(session, cid)
    llm_detail = _fetch_llm_detail(session, run_date, cid)
    llm_diagnostics = _fetch_llm_diagnostics(session, run_date, cid)

    # Per-cid artifacts.
    if profile and profile.payload:
        _write_json(cid_dir / "scrape_profile.json", profile.payload)
    if events:
        _write_jsonl(cid_dir / "scrape_events.jsonl", [_row_to_dict(e) for e in events])
    if issues:
        _write_jsonl(cid_dir / "run_issues.jsonl", [_row_to_dict(i) for i in issues])
    if snapshot and snapshot.payload:
        _write_json(cid_dir / "property_snapshot.json", snapshot.payload)
    if dlq:
        _write_json(cid_dir / "dlq_entry.json", _row_to_dict(dlq))
    if llm_detail and llm_detail.payload:
        _write_json(cid_dir / "llm_property_detail.json", llm_detail.payload)
    if llm_diagnostics:
        _write_jsonl(
            cid_dir / "llm_diagnostics.jsonl",
            [
                {**_row_to_dict(d), "payload": d.payload}
                for d in llm_diagnostics
            ],
        )

    # GCS artifacts (already downloaded into gcs_work_dir).
    gcs_artifacts: dict[str, str] = {}
    if gcs_work_dir is not None:
        safe_cid = _safe_filename(cid)
        for label, sub in (
            ("property_report.md", f"property_reports/{safe_cid}.md"),
            ("raw_api.json", f"raw_api/{safe_cid}.json"),
            ("llm_report.json", f"llm_report/{safe_cid}.json"),
        ):
            src = _find_in_shards(gcs_work_dir, sub)
            if src is not None:
                dst = cid_dir / label
                shutil.copyfile(src, dst)
                gcs_artifacts[label] = label

    summary = _build_summary_dict(
        run_date=run_date,
        cid=cid,
        ledger=ledger,
        prop=prop,
        issues=issues,
        events=events,
        profile=profile,
        snapshot=snapshot,
        dlq=dlq,
        llm_detail=llm_detail,
        llm_diagnostics=llm_diagnostics,
        gcs_artifacts=gcs_artifacts,
    )
    _write_json(cid_dir / "summary.json", summary)
    (cid_dir / "summary.md").write_text(
        _render_summary_md(summary), encoding="utf-8"
    )
    return summary


# ── Index ────────────────────────────────────────────────────────────────────


def _render_index_md(
    *,
    run_date: str,
    summaries: list[dict[str, Any]],
    source_label: str,
    gcs_present: bool,
    bucket: str | None,
) -> str:
    """Top-level index summarising every cid handled, with code histogram."""
    total = len(summaries)
    code_counter: Counter[str] = Counter()
    status_counter: Counter[str] = Counter()
    tier_counter: Counter[str] = Counter()
    for s in summaries:
        for code, count in (s.get("issue_code_counts") or {}).items():
            code_counter[code] += int(count)
        status_counter[(s.get("ledger") or {}).get("status", "—") or "—"] += 1
        tier_counter[(s.get("verdict") or {}).get("tier_used", "—") or "—"] += 1

    lines: list[str] = []
    lines.append(f"# Failure Debug Index — {run_date}")
    lines.append("")
    lines.append(f"**Source:** {source_label}  ")
    lines.append(
        f"**GCS shards present:** {'yes' if gcs_present else 'no'} "
        f"(bucket={bucket or '<unset>'})  "
    )
    lines.append(f"**Failed properties:** {total}  ")
    lines.append("")

    lines.append("## Status breakdown")
    lines.append("")
    lines.append("| Status | Count |")
    lines.append("|---|---|")
    for status, count in sorted(status_counter.items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{status}` | {count} |")
    lines.append("")

    lines.append("## Tier distribution (last attempted)")
    lines.append("")
    lines.append("| Tier | Count |")
    lines.append("|---|---|")
    for tier, count in sorted(tier_counter.items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{tier}` | {count} |")
    lines.append("")

    lines.append("## Top issue codes")
    lines.append("")
    if not code_counter:
        lines.append("_No run_issues rows recorded across these failures._")
    else:
        lines.append("| Code | Count |")
        lines.append("|---|---|")
        for code, count in code_counter.most_common(25):
            lines.append(f"| `{code}` | {count} |")
    lines.append("")

    lines.append("## Failed properties")
    lines.append("")
    lines.append(
        "| Canonical ID | Property | Status | Tier | Errors | Issue codes (top) | Bundle |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for s in summaries:
        cid = s["canonical_id"]
        ident = s.get("identity") or {}
        led = s.get("ledger") or {}
        ver = s.get("verdict") or {}
        codes = s.get("issue_code_counts") or {}
        top_codes = ", ".join(
            f"{code}×{count}"
            for code, count in sorted(codes.items(), key=lambda kv: -kv[1])[:3]
        )
        bundle = f"{_safe_filename(cid)}/summary.md"
        lines.append(
            f"| `{cid}` | {ident.get('property_name') or '—'} | "
            f"`{led.get('status') or '—'}` | `{ver.get('tier_used') or '—'}` | "
            f"{led.get('error_count') or 0} | {top_codes or '—'} | "
            f"[summary]({bundle}) |"
        )
    lines.append("")
    return "\n".join(lines)


# ── Entrypoint ──────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv(_MA_POC_ROOT / ".env")

    parser = argparse.ArgumentParser(
        description="Build per-property failure debug bundles for a run_date."
    )
    parser.add_argument("--run-date", default=None,
                        help="YYYY-MM-DD. Defaults to today UTC, then latest run.")
    parser.add_argument("--canonical-ids", default=None,
                        help="Comma-separated cid filter. Skipped cids that "
                             "actually succeeded are dropped automatically.")
    parser.add_argument("--out-dir", default=None,
                        help="Output directory. Defaults to "
                             "ma_poc/data/runs/{run_date}/failure_debug/.")
    parser.add_argument("--include-gcs", action="store_true",
                        help="Download the run's GCS shards and copy "
                             "property_report.md / raw_api.json / "
                             "llm_report.json into each cid bundle.")
    parser.add_argument("--use-gcs", action="store_true",
                        help="Force GCS as the only source. Use when SQL has "
                             "aged the run out (>3 days). Implies --include-gcs.")
    parser.add_argument("--events-limit", type=int, default=10,
                        help="How many recent scrape_events rows to capture "
                             "per property (default 10).")
    parser.add_argument("--database-url", default=None,
                        help="Override DATABASE_URL (asyncpg auto-translates "
                             "to pg8000).")
    args = parser.parse_args(argv)

    bucket = (os.getenv("BUCKET_NAME") or "").strip() or None
    only_cids: set[str] | None = None
    if args.canonical_ids:
        only_cids = {c.strip() for c in args.canonical_ids.split(",") if c.strip()}

    provider = _open_provider(args.database_url)
    log.info("Data provider: %s · BUCKET_NAME=%s", provider.name, bucket or "(unset)")

    gcs_work_dir: Path | None = None
    summaries: list[dict[str, Any]] = []
    try:
        run_date, gcs_present = _resolve_run_date(provider, args.run_date, bucket=bucket)
        log.info("Resolved run_date=%s · gcs_present=%s", run_date, gcs_present)

        out_dir = (
            Path(args.out_dir) if args.out_dir
            else _MA_POC_ROOT / "data" / "runs" / run_date / "failure_debug"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        log.info("Writing bundles to %s", out_dir)

        download_gcs = args.include_gcs or args.use_gcs
        if download_gcs:
            if not bucket:
                log.warning(
                    "--include-gcs/--use-gcs requested but BUCKET_NAME is unset; "
                    "skipping GCS artifacts."
                )
            elif not gcs_present:
                log.warning(
                    "--include-gcs requested but no shards found at "
                    "gs://%s/runs/%s/; skipping GCS artifacts.",
                    bucket, run_date,
                )
            else:
                gcs_work_dir = _download_run_dir(bucket, run_date)

        source_label = "Cloud SQL"
        if args.use_gcs:
            source_label = f"Cloud Storage (gs://{bucket}/runs/{run_date}/)"

        with Session(provider.engine, future=True) as s:
            failed = _list_failed_cids(s, run_date, only_cids)
            if not failed:
                log.warning(
                    "No failed properties for run_date=%s "
                    "(SQL may have aged out — try --use-gcs).",
                    run_date,
                )

            for i, (cid, ledger) in enumerate(failed, 1):
                log.info("[%d/%d] %s · status=%s",
                         i, len(failed), cid, ledger.status)
                try:
                    summary = _process_cid(
                        session=s,
                        run_date=run_date,
                        cid=cid,
                        ledger=ledger,
                        out_dir=out_dir,
                        events_limit=args.events_limit,
                        gcs_work_dir=gcs_work_dir,
                    )
                    summaries.append(summary)
                except Exception as exc:  # noqa: BLE001 — keep going for the rest
                    log.exception("Failed to build bundle for %s: %s", cid, exc)

        index_md = _render_index_md(
            run_date=run_date,
            summaries=summaries,
            source_label=source_label,
            gcs_present=gcs_present,
            bucket=bucket,
        )
        (out_dir / "index.md").write_text(index_md, encoding="utf-8")
        _write_json(
            out_dir / "index.json",
            {
                "run_date": run_date,
                "source": source_label,
                "gcs_present": gcs_present,
                "bucket": bucket,
                "failed_count": len(summaries),
                "summaries": summaries,
            },
        )
        log.info(
            "Done. run_date=%s failed=%d out_dir=%s",
            run_date, len(summaries), out_dir,
        )
    finally:
        provider.close()
        if gcs_work_dir is not None and gcs_work_dir.exists():
            shutil.rmtree(gcs_work_dir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
