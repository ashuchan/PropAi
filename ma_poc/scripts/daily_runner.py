"""
Daily multi-property runner with identity resolution, state, and reporting.
============================================================================

Pipeline for each daily run:

  1. Load CSV, resolve canonical_id per row (5-tier cascade).
  2. Detect duplicates: hard (same id), soft (same address, different id),
     geo (same coordinates, different id). Logged as validation issues.
  3. Load yesterday's state (property_index + unit_index).
  4. For each resolved row:
       a. Try to scrape. On exception → log PIPELINE_EXCEPTION, continue.
       b. Transform raw API bodies to target-schema units.
       c. Validate units (schema + rent range + dates + duplicate unit_ids).
       d. If scrape failed or units empty and the property existed yesterday
          → carry-forward yesterday's units with carryforward_days += 1.
       e. Diff today's units against the unit_index → new/updated/unchanged/
          disappeared. Update index.
       f. Build target-schema property record.
       g. Incremental write to data/runs/{date}/properties.json every property
          so an interrupted run still leaves a usable file behind.
  5. Detect disappeared properties (in state yesterday, not in today's CSV).
  6. Save state. Write report.json + report.md + issues.jsonl.
  7. Exit code 0 unless the run itself could not start.

Never-fail contract:
  - Every scrape wrapped in try/except; no single property can crash the run.
  - State-file writes use atomic temp-file + rename.
  - Every error is logged with enough detail to reproduce (row_index,
    canonical_id, scrape errors, validation code, full traceback).

Directory layout (all relative to --data-dir, default ./data):
  data/runs/{YYYY-MM-DD}/properties.json     # main output (array)
  data/runs/{YYYY-MM-DD}/report.json         # structured report
  data/runs/{YYYY-MM-DD}/report.md           # human report
  data/runs/{YYYY-MM-DD}/issues.jsonl        # one issue per line
  data/runs/{YYYY-MM-DD}/raw_api/{cid}.json  # raw API bodies (debug)
  data/state/property_index.json             # persistent, updated every run
  data/state/unit_index.json
  data/latest_run.json                       # pointer to most recent run

Usage:
  python scripts/daily_runner.py
  python scripts/daily_runner.py --csv config/properties.csv --limit 5
  python scripts/daily_runner.py --run-date 2026-04-12 --data-dir ./data
  python scripts/daily_runner.py --proxy http://user:pass@host:port
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

# Load .env early so API keys (ANTHROPIC_API_KEY, AZURE_OPENAI_API_KEY, etc.)
# are available before any LLM provider is instantiated.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Force UTF-8 stdout on Windows so emoji prints don't crash the run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

# Make sibling script modules importable regardless of invocation cwd.
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent  # ma_poc/
for _p in (_HERE, _PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import validation as V  # noqa: E402
from concurrency import SystemResources  # noqa: E402
from entrata import scrape  # noqa: E402
from identity import (  # noqa: E402
    ADDRESS_KEYS,
    CITY_KEYS,
    LAT_KEYS,
    LNG_KEYS,
    NAME_KEYS,
    PROPERTY_ID_KEYS,
    STATE_KEYS,
    UNIQUE_ID_KEYS,
    WEBSITE_KEYS,
    ZIP_KEYS,
    PropertyIdentity,
    csv_get,
    detect_duplicates,
    resolve_identity,
)
from scrape_properties import (  # noqa: E402
    _clean,
    aggregate_unit_stats,
    transform_units_from_scrape,
)
from state_store import StateStore  # noqa: E402

# Profile system imports — lazy-loaded to avoid hard dependency.
# Profile failures must never crash the pipeline.
try:
    from services.profile_store import ProfileStore
    from services.profile_updater import update_profile_after_extraction
    from services.drift_detector import detect_drift, apply_drift_demotion
    _PROFILES_AVAILABLE = True
except ImportError:
    _PROFILES_AVAILABLE = False

# ── Logging ───────────────────────────────────────────────────────────────────

log = logging.getLogger("daily_runner")

def _configure_logging(run_dir: Path) -> None:
    """Console + run-scoped log file. Existing handlers cleared so re-runs are clean."""
    log.handlers.clear()
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.addHandler(ch)

    run_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(run_dir / "runner.log", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

# ── CSV reading ───────────────────────────────────────────────────────────────

def read_properties_csv(path: Path) -> list[dict]:
    """UTF-8-BOM tolerant CSV read. Returns list of dict rows."""
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    log.info(f"Loaded {len(rows)} rows from {path}")
    return rows

# ── CSV → output property record ──────────────────────────────────────────────

# Full target-schema field list in the order requested by the user.
TARGET_PROPERTY_FIELDS = [
    "Property Name", "Type", "Unique ID", "Average Unit Size (SF)",
    "Property ID", "Census Block Id", "City",
    "Construction Finish Date", "Construction Start Date",
    "Development Company", "Latitude", "Longitude",
    "Management Company", "Market Name", "Property Owner",
    "Property Address", "Property Status", "Property Type",
    "Region", "Renovation Finish", "Renovation Start",
    "State", "Stories", "Submarket Name", "Total Units",
    "Tract Code", "Year Built", "ZIP Code", "Lease Start Date",
    "First Move-In Date", "Property Style", "Update Date", "Unit Mix",
    "Asset Grade in Submarket", "Asset Grade in Market",
    "Phone", "Website",
    "Property Image URL", "Property Gallery URLs",
]

# Field groups that are pass-through from CSV; runner never tries to extract them.
EXTERNAL_ONLY_FIELDS = {
    "Census Block Id", "Tract Code",
    "Construction Start Date", "Construction Finish Date",
    "Renovation Start", "Renovation Finish",
    "Development Company", "Property Owner",
    "Region", "Market Name", "Submarket Name",
    "Asset Grade in Submarket", "Asset Grade in Market",
    "Lease Start Date",
}

def _f(row: dict, *keys: str) -> Any:
    """Return cleaned CSV value or None."""
    return _clean(csv_get(row, *keys)) or None

def _num(row: dict, *keys: str) -> float | None:
    v = csv_get(row, *keys)
    if not v:
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None

def build_property_record(
    row: dict,
    ident: PropertyIdentity,
    scrape_result: dict,
    target_units: list[dict],
    state_snapshot: dict | None,
    carry_forward_used: bool,
) -> dict:
    """
    Produce one target-schema property record. CSV values always take precedence
    for fields that exist in the CSV; scraped values fill in only for fields the
    CSV left blank. Computed aggregates (Average Unit Size, Unit Mix) come from
    today's target_units.
    """
    md = scrape_result.get("property_metadata") or {}
    stats = aggregate_unit_stats(target_units)

    def pick(csv_val: Any, scraped_val: Any) -> Any:
        return csv_val if csv_val not in (None, "", "null", "None") else _clean(scraped_val)

    rec: dict[str, Any] = {f: None for f in TARGET_PROPERTY_FIELDS}

    # ── Identity ─────────────────────────────────────────────────────────────
    rec["Unique ID"]   = _f(row, *UNIQUE_ID_KEYS) or ident.canonical_id
    rec["Property ID"] = _f(row, *PROPERTY_ID_KEYS) or ident.canonical_id

    # ── Identity + name ─────────────────────────────────────────────────────
    rec["Property Name"] = pick(_f(row, *NAME_KEYS), md.get("name") or md.get("title"))
    rec["Type"]          = _f(row, "Type")
    rec["Property Type"] = _f(row, "Property Type")
    rec["Property Style"] = _f(row, "Property Style") or _f(row, "Building Type")
    rec["Property Status"] = _f(row, "Property Status") or "Active"

    # ── Location ────────────────────────────────────────────────────────────
    rec["Property Address"] = pick(_f(row, *ADDRESS_KEYS), md.get("address"))
    rec["City"]             = pick(_f(row, *CITY_KEYS),    md.get("city"))
    rec["State"]            = pick(_f(row, *STATE_KEYS),   md.get("state"))
    rec["ZIP Code"]         = pick(_f(row, *ZIP_KEYS),     md.get("zip"))
    rec["Latitude"]         = _num(row, *LAT_KEYS)  if csv_get(row, *LAT_KEYS)  else md.get("latitude")
    rec["Longitude"]        = _num(row, *LNG_KEYS)  if csv_get(row, *LNG_KEYS)  else md.get("longitude")

    # ── Structure (from CSV, website rarely has these) ──────────────────────
    rec["Year Built"]       = _num(row, "Year Built") or md.get("year_built")
    rec["Stories"]          = _num(row, "Stories")     or md.get("stories")

    # ── Operations ──────────────────────────────────────────────────────────
    rec["Management Company"] = _f(row, "Management Company")
    rec["Phone"]              = pick(_f(row, "Phone"), md.get("telephone"))
    rec["Website"]            = _f(row, *WEBSITE_KEYS) or scrape_result.get("base_url")

    # ── Images (scraped from OpenGraph / JSON-LD) ──────────────────────────
    rec["Property Image URL"]    = md.get("image_url") or None
    rec["Property Gallery URLs"] = md.get("gallery_urls") or []

    # ── Aggregates from scraped units (computed every run, always wins) ────
    rec["Average Unit Size (SF)"] = stats["average_unit_size_sf"] or _num(row, "Average Unit Size (SF)")
    rec["Total Units"]            = stats["total_units_found"] or _num(row, "Total Units")
    rec["Unit Mix"]               = stats["unit_mix"] or _f(row, "Unit Mix")
    rec["First Move-In Date"]     = stats["first_move_in_date"] or _f(row, "First Move-In Date")

    # ── External-only fields (pass-through from CSV, never scraped) ────────
    for f in EXTERNAL_ONLY_FIELDS:
        rec[f] = _f(row, f)

    rec["Update Date"] = date.today().isoformat()

    rec["units"] = target_units

    # ── Runtime diagnostics (always last so they're easy to find) ──────────
    rec["_meta"] = {
        "canonical_id":      ident.canonical_id,
        "identity_source":   ident.id_source,
        "identity_confidence": ident.confidence,
        "address_fp":        ident.address_fp,
        "geo_fp":            ident.geo_fp,
        "website_fp":        ident.website_fp,
        "scrape_tier_used":  scrape_result.get("extraction_tier_used"),
        "scrape_errors":     scrape_result.get("errors") or [],
        "apis_intercepted":  len(scrape_result.get("_raw_api_responses") or []),
        "units_extracted":   len(target_units),
        "carry_forward_used": carry_forward_used,
        "was_known":         bool(state_snapshot),
    }
    return rec

# ── Run orchestrator ──────────────────────────────────────────────────────────

async def _scrape_one(url: str, proxy: str | None, timeout_s: int,
                      profile: Any = None,
                      expected_total_units: int | None = None,
                      property_city: str | None = None) -> dict:
    """Run scrape() with a hard timeout so a stuck page can never hang the run."""
    try:
        return await asyncio.wait_for(
            scrape(url, proxy=proxy, profile=profile,
                   expected_total_units=expected_total_units,
                   property_city=property_city),
            timeout=timeout_s,
        )
    except TimeoutError:
        return {"errors": [f"scrape timeout after {timeout_s}s"], "base_url": url,
                "_timeout": True}


def _scrape_in_thread(
    url: str,
    proxy: str | None,
    timeout_s: int,
    property_id: str = "unknown",
    profile: Any = None,
    expected_total_units: int | None = None,
    property_city: str | None = None,
) -> dict:
    """
    Run a single scrape in its own thread with its own event loop.

    Each thread gets an independent asyncio event loop and Playwright
    instance, giving true OS-level parallelism instead of single-threaded
    async concurrency.

    ``property_id`` is injected into the result so LLM interaction records
    (Tier 6 / Tier 7) carry the canonical ID for cost accounting.
    ``profile`` is the ScrapeProfile used for tier-skip routing.
    """
    if not url:
        return {"errors": ["no URL"], "base_url": "", "_property_id": property_id,
                "_llm_interactions": []}
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            _scrape_one(url, proxy, timeout_s, profile=profile,
                        expected_total_units=expected_total_units,
                        property_city=property_city),
        )
        # Stamp the canonical property ID so entrata.py's LLM tiers can
        # reference it when building interaction records.
        if isinstance(result, dict):
            result["_property_id"] = property_id
        return result
    except Exception as e:
        return {"errors": [str(e)], "base_url": url, "_exception": e,
                "_property_id": property_id, "_llm_interactions": []}
    finally:
        # Drain pending tasks (e.g., Playwright/httpx internal aclose coroutines)
        # before closing the loop. Without this, background ``AsyncClient.aclose()``
        # tasks fire after ``loop.close()`` and spam "Event loop is closed" errors.
        _close_event_loop(loop)


def _close_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Shutdown a worker event loop cleanly.

    Cancels any tasks still pending, waits for them to finish (best-effort),
    shuts down async generators, then closes the loop. This prevents
    ``RuntimeError: Event loop is closed`` noise from httpx's garbage-collected
    ``AsyncClient`` instances whose ``aclose()`` coroutines would otherwise be
    scheduled on an already-closed loop.
    """
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True),
            )
        loop.run_until_complete(loop.shutdown_asyncgens())
    except Exception:
        # Best-effort cleanup — never let drain errors mask the scrape result.
        pass
    finally:
        loop.close()

def _append_ledger(path: Path, entry: dict) -> None:
    """Append one checkpoint entry to the run ledger (crash-safe resume support)."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def load_ledger(path: Path) -> dict[str, dict]:
    """
    Read a ledger.jsonl and return the *last* entry per canonical_id.

    Later entries overwrite earlier ones so retries update the record.
    Returns {canonical_id: {status, row_index, timestamp, ...}}.
    """
    entries: dict[str, dict] = {}
    if not path.exists():
        return entries
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                cid = rec.get("canonical_id")
                if cid:
                    entries[cid] = rec
            except json.JSONDecodeError:
                continue
    return entries


def _write_issues_jsonl(path: Path, issues: list[V.ValidationIssue]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for iss in issues:
            f.write(json.dumps(iss.to_dict(), ensure_ascii=False, default=str) + "\n")

def _write_markdown_report(path: Path, report: dict) -> None:
    lines: list[str] = []
    lines.append(f"# Daily Run Report — {report['run_date']}")
    lines.append("")
    lines.append(f"- **Started:** {report['started_at']}")
    lines.append(f"- **Finished:** {report['finished_at']}")
    lines.append(f"- **Duration:** {report['duration_s']:.1f}s")
    lines.append(f"- **Exit status:** {report['exit_status']}")
    lines.append("")
    lines.append("## Totals")
    for k, v in report["totals"].items():
        lines.append(f"- {k}: **{v}**")
    lines.append("")
    lines.append("## Identity")
    ids = report["identity"]
    lines.append(f"- Resolved: **{ids['resolved']}** / Unresolved: **{ids['unresolved']}**")
    lines.append(f"- Hard duplicates (same canonical_id): **{len(ids['hard_duplicates'])}**")
    lines.append(f"- Soft duplicates (same address, different id): **{len(ids['soft_duplicates'])}**")
    lines.append("- By source: " + ", ".join(f"{k}={v}" for k, v in ids["by_source"].items()))
    lines.append("")
    lines.append("## Issues")
    lines.append(f"- Total: **{report['issues']['total']}**")
    for sev, n in report["issues"]["by_severity"].items():
        lines.append(f"  - {sev}: {n}")
    lines.append("- Top codes:")
    for code, n in list(report["issues"]["by_code"].items())[:20]:
        lines.append(f"  - `{code}`: {n}")
    lines.append("")
    lines.append("## State diff vs yesterday")
    sd = report["state_diff"]
    lines.append(f"- New properties: **{len(sd['new_properties'])}**")
    lines.append(f"- Disappeared properties: **{len(sd['disappeared_properties'])}**")
    lines.append(f"- Carry-forward used: **{sd['carry_forward_count']}** properties")
    lines.append(f"- Unit totals — extracted: {sd['units_extracted']}, "
                 f"new: {sd['units_new']}, updated: {sd['units_updated']}, "
                 f"unchanged: {sd['units_unchanged']}, disappeared: {sd['units_disappeared']}, "
                 f"carried-forward: {sd['units_carried_forward']}")
    lines.append("")
    if report["failed_properties"]:
        lines.append("## Failed properties (first 50)")
        lines.append("| Row | Canonical ID | Reason |")
        lines.append("|---|---|---|")
        for fp in report["failed_properties"][:50]:
            reason = (fp.get("reason") or "").replace("|", "\\|")[:120]
            lines.append(f"| {fp['row_index']} | `{fp.get('canonical_id') or 'unresolved'}` | {reason} |")
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

async def run_daily(
    csv_path: Path,
    run_date: str,
    data_dir: Path,
    limit: int | None,
    start_at: int,
    proxy: str | None,
    scrape_timeout_s: int,
    schema_version: str = "v1",
) -> dict:
    started_at = datetime.now(UTC)
    # Namespace runs and state by schema version so V1/V2 data never collide.
    schema_root = data_dir / schema_version
    run_dir = schema_root / "runs" / run_date
    state_dir = schema_root / "state"
    raw_dir = run_dir / "raw_api"
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    _configure_logging(run_dir)

    properties_path = run_dir / "properties.json"
    report_json     = run_dir / "report.json"
    report_md       = run_dir / "report.md"
    issues_path     = run_dir / "issues.jsonl"
    ledger_path     = run_dir / "ledger.jsonl"
    # Clear issues file at run start so re-runs don't accumulate.
    if issues_path.exists():
        issues_path.unlink()

    all_issues: list[V.ValidationIssue] = []
    failed_properties: list[dict] = []
    all_llm_interactions: list[dict] = []  # accumulated across all properties

    log.info(f"=== Daily run {run_date} → {run_dir} | schema={schema_version} ===")

    # ── 1. Load CSV ─────────────────────────────────────────────────────────
    try:
        rows = read_properties_csv(csv_path)
    except Exception as e:
        log.error(f"Fatal: could not read CSV: {e}")
        return {"exit_status": "FATAL", "error": str(e)}

    if start_at:
        rows = rows[start_at:]
    if limit:
        rows = rows[:limit]
    log.info(f"Processing {len(rows)} rows (start_at={start_at}, limit={limit})")

    # ── 2. Resolve identity for all rows up front ───────────────────────────
    identities = [resolve_identity(row) for row in rows]
    for idx, ident in enumerate(identities):
        if ident.canonical_id is None:
            iss = V.error(
                V.IDENTITY_UNRESOLVED,
                f"row {idx}: could not resolve any identity tier",
                row_index=idx,
                details={"components": ident.components, "row_snapshot": {
                    k: rows[idx].get(k) for k in (NAME_KEYS[0], UNIQUE_ID_KEYS[0], PROPERTY_ID_KEYS[0],
                                                  ADDRESS_KEYS[0], WEBSITE_KEYS[0])
                }},
            )
            all_issues.append(iss)
        elif ident.confidence < 0.70:
            all_issues.append(V.warning(
                V.IDENTITY_LOW_CONFIDENCE,
                f"row {idx}: resolved via {ident.id_source} (confidence {ident.confidence})",
                row_index=idx, canonical_id=ident.canonical_id,
                details={"source": ident.id_source},
            ))

    # ── 3. Duplicate detection ─────────────────────────────────────────────
    dup_report = detect_duplicates(identities)
    for cid, row_idxs in dup_report.hard_duplicates.items():
        all_issues.append(V.error(
            V.DUPLICATE_IDENTITY,
            f"canonical_id {cid} appears in {len(row_idxs)} rows",
            canonical_id=cid,
            details={"row_indices": row_idxs},
        ))
    for afp, row_idxs in dup_report.soft_duplicates.items():
        all_issues.append(V.warning(
            V.SOFT_DUPLICATE_ADDRESS,
            f"address_fp {afp} matched across rows with different canonical_ids",
            details={"row_indices": row_idxs,
                     "canonical_ids": [identities[i].canonical_id for i in row_idxs]},
        ))
    for gfp, row_idxs in dup_report.geo_duplicates.items():
        all_issues.append(V.warning(
            V.SOFT_DUPLICATE_GEO,
            f"geo_fp {gfp} matched across rows with different canonical_ids",
            details={"row_indices": row_idxs,
                     "canonical_ids": [identities[i].canonical_id for i in row_idxs]},
        ))

    _write_issues_jsonl(issues_path, all_issues)

    log.info(f"Identity resolution: {sum(1 for i in identities if i.canonical_id)} resolved, "
             f"{len(dup_report.unresolved_rows)} unresolved, "
             f"{len(dup_report.hard_duplicates)} hard dupes, "
             f"{len(dup_report.soft_duplicates)} soft dupes")

    # ── 4. Load state store ────────────────────────────────────────────────
    state = StateStore(state_dir)
    state.load()
    prior_property_ids = state.all_canonical_ids()
    log.info(f"State store loaded: {len(prior_property_ids)} known properties")

    # ── 4b. Load profile store (non-fatal if unavailable) ─────────────────
    profile_store = None
    if _PROFILES_AVAILABLE:
        try:
            profile_store = ProfileStore(_PROJECT_ROOT / "config" / "profiles")
            log.info("Profile store loaded")
        except Exception as e:
            log.warning(f"Profile store unavailable: {e}")

    # Track processed canonical_ids to skip hard duplicates' second+ occurrences.
    processed: set[str] = set()
    properties_out: list[dict] = []

    # ── 5. Per-property pipeline (concurrent scraping) ──────────────────────
    seen_ids_today: set[str] = set()
    units_total = {"extracted": 0, "new": 0, "updated": 0, "unchanged": 0,
                   "disappeared": 0, "carried_forward": 0}
    carry_forward_count = 0

    # ── 5a. Pre-filter: separate scrapeable rows from skippable ones ──────
    # Unresolved identities and duplicates are handled immediately.
    # Scrapeable rows are batched for concurrent scraping.
    scrapeable: list[tuple[int, dict, PropertyIdentity, str]] = []  # (idx, row, ident, url)

    for idx, (row, ident) in enumerate(zip(rows, identities, strict=False)):
        url = csv_get(row, *WEBSITE_KEYS)
        cid = ident.canonical_id

        if cid is None:
            failed_properties.append({
                "row_index": idx, "canonical_id": None,
                "reason": "IDENTITY_UNRESOLVED",
            })
            minimal = {f: None for f in TARGET_PROPERTY_FIELDS}
            minimal["Property Name"] = csv_get(row, *NAME_KEYS) or None
            minimal["Website"]       = url or None
            minimal["Update Date"]   = date.today().isoformat()
            minimal["units"]         = []
            minimal["_meta"] = {
                "canonical_id": None, "identity_source": "unresolved",
                "scrape_tier_used": None, "units_extracted": 0,
                "carry_forward_used": False, "was_known": False,
                "error": "could not resolve canonical_id from CSV row",
            }
            properties_out.append(minimal)
            _append_ledger(ledger_path, {
                "canonical_id": None, "row_index": idx,
                "status": "UNRESOLVED",
                "reason": "IDENTITY_UNRESOLVED",
                "timestamp": datetime.now(UTC).isoformat(),
            })
            continue

        if cid in processed:
            log.warning(f"  ↳ skipping duplicate canonical_id {cid} at row {idx}")
            _append_ledger(ledger_path, {
                "canonical_id": cid, "row_index": idx,
                "status": "SKIPPED",
                "reason": "DUPLICATE_CANONICAL_ID",
                "timestamp": datetime.now(UTC).isoformat(),
            })
            continue
        processed.add(cid)
        seen_ids_today.add(cid)
        scrapeable.append((idx, row, ident, url or ""))

    # ── 5b. Load profiles for each property (non-fatal) ──────────────────
    # Profiles guide tier-skip routing: HOT properties jump straight to the
    # known-good tier instead of running the full cascade every time.
    scrape_profiles: list[Any] = [None] * len(scrapeable)
    if profile_store is not None:
        for si, (idx, row, ident, url) in enumerate(scrapeable):
            cid = ident.canonical_id
            if cid:
                try:
                    scrape_profiles[si] = profile_store.load(cid)
                except Exception:
                    pass

    # ── 5c. Concurrent scraping phase (thread pool — true parallelism) ───
    res = SystemResources.detect()
    log.info(f"System resources: {res.summary()}")
    pool_size = res.optimal_pool_size()
    log.info(f"Scraping {len(scrapeable)} properties with {pool_size} threads")

    loop = asyncio.get_running_loop()
    scrape_results_raw: list[Any] = [None] * len(scrapeable)

    with ThreadPoolExecutor(
        max_workers=pool_size, thread_name_prefix="scrape"
    ) as executor:
        def _expected_units_for(row: dict) -> int | None:
            """Parse CSV 'Total Units' as an integer hint for Phase 3 gating."""
            v = _num(row, "Total Units")
            try:
                n = int(v) if v is not None else 0
                return n if n > 0 else None
            except (TypeError, ValueError):
                return None

        futures = [
            loop.run_in_executor(
                executor,
                _scrape_in_thread,
                item[3],          # url
                proxy,
                scrape_timeout_s,
                item[2].canonical_id or "unknown",  # property_id for LLM logging
                scrape_profiles[si],                 # profile for tier-skip routing
                _expected_units_for(item[1]),        # expected Total Units from CSV
                csv_get(item[1], *CITY_KEYS) or None,  # property_city for vacancy filtering
            )
            for si, item in enumerate(scrapeable)
        ]
        # return_exceptions=True so one crash doesn't cancel others.
        scrape_results_raw = await asyncio.gather(*futures, return_exceptions=True)

    # ── 5c. Sequential post-processing (state mutations are not concurrent) ──
    for task_idx, (idx, row, ident, url) in enumerate(scrapeable):
        cid = ident.canonical_id
        assert cid is not None  # guaranteed by pre-filter
        row_name = csv_get(row, *NAME_KEYS) or csv_get(row, *WEBSITE_KEYS) or f"row{idx}"

        log.info(f"[{task_idx+1}/{len(scrapeable)}] {cid} — {row_name}")

        per_prop_issues: list[V.ValidationIssue] = []

        if not url:
            per_prop_issues.append(V.error(
                V.CSV_MISSING_URL,
                f"row {idx} has no Website/URL column",
                canonical_id=cid, row_index=idx,
            ))

        # ── Collect scrape result ─────────────────────────────────────────
        scrape_result_or_exc = scrape_results_raw[task_idx]
        scrape_result: dict = {"errors": [], "base_url": url}
        scrape_failed = False

        if isinstance(scrape_result_or_exc, Exception):
            # gather(return_exceptions=True) surfaced a raw exception.
            e = scrape_result_or_exc
            tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            per_prop_issues.append(V.error(
                V.PIPELINE_EXCEPTION,
                f"exception during scrape: {e}",
                canonical_id=cid, row_index=idx,
                details={"exception": str(e), "traceback": tb[-1500:]},
            ))
            scrape_result = {"errors": [str(e)], "base_url": url}
            scrape_failed = True
        elif isinstance(scrape_result_or_exc, dict) and scrape_result_or_exc.get("_exception"):
            # _scrape_in_thread caught the exception and returned it in the dict.
            e = scrape_result_or_exc["_exception"]
            tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            per_prop_issues.append(V.error(
                V.PIPELINE_EXCEPTION,
                f"exception during scrape: {e}",
                canonical_id=cid, row_index=idx,
                details={"exception": str(e), "traceback": tb[-1500:]},
            ))
            scrape_result = scrape_result_or_exc
            scrape_failed = True
        elif not url:
            # _scrape_in_thread returned {"errors": ["no URL"], ...} — keep it.
            scrape_result = scrape_result_or_exc
            scrape_failed = True
        else:
            scrape_result = scrape_result_or_exc
            if scrape_result.get("_timeout"):
                per_prop_issues.append(V.error(
                    V.SCRAPE_TIMEOUT,
                    f"scrape timed out after {scrape_timeout_s}s",
                    canonical_id=cid, row_index=idx,
                    details={"url": url},
                ))
                scrape_failed = True
            elif scrape_result.get("errors"):
                per_prop_issues.append(V.warning(
                    V.SCRAPE_FAILED,
                    f"scrape returned errors: {scrape_result['errors'][:2]}",
                    canonical_id=cid, row_index=idx,
                    details={"errors": scrape_result["errors"]},
                ))
            # Only warn about missing APIs when the scrape actually ran
            # (not on exceptions or missing URLs — those already have their
            # own error/warning).
            if not scrape_failed and not scrape_result.get("_raw_api_responses"):
                per_prop_issues.append(V.warning(
                    V.SCRAPE_NO_APIS,
                    "no API responses intercepted — fallback tiers would be used",
                    canonical_id=cid, row_index=idx,
                ))

        scrape_apis = len(scrape_result.get('_raw_api_responses') or [])
        scrape_tier = scrape_result.get('extraction_tier_used')
        scrape_errs = scrape_result.get('errors') or []
        log.info(f"  scrape: apis={scrape_apis}, tier={scrape_tier}, failed={scrape_failed}"
                 + (f", errors={scrape_errs[:2]}" if scrape_errs else ""))

        # ── Transform ──────────────────────────────────────────────────────
        target_units: list[dict] = []
        try:
            target_units = transform_units_from_scrape(scrape_result)
        except Exception as e:
            per_prop_issues.append(V.error(
                V.PIPELINE_EXCEPTION,
                f"exception during unit transform: {e}",
                canonical_id=cid, row_index=idx,
                details={"exception": str(e), "traceback": traceback.format_exc()[-1500:]},
            ))

        # If transform produced units but the scraper didn't set a tier,
        # infer the tier from the API responses so profile updater counts it
        # as a success (e.g., SightMap units extracted by transform_units).
        if target_units and not scrape_result.get("extraction_tier_used"):
            raw_apis = scrape_result.get("_raw_api_responses") or []
            for api in raw_apis:
                api_url = api.get("url", "")
                if "sightmap.com" in api_url:
                    scrape_result["extraction_tier_used"] = "TIER_1_SIGHTMAP"
                    break
                elif "realpage.com" in api_url:
                    scrape_result["extraction_tier_used"] = "TIER_1_API"
                    break
            else:
                scrape_result["extraction_tier_used"] = "TIER_1_API"

        # Strip the internal helper fields (underscore-prefixed) that the
        # transformer uses for aggregates. We still need the originals for
        # stats, so compute stats before stripping.
        public_units = [{k: v for k, v in u.items() if not k.startswith("_")} for u in target_units]

        # ── Validate ───────────────────────────────────────────────────────
        per_prop_issues.extend(V.validate_units(public_units, cid))

        if not public_units and not scrape_failed:
            per_prop_issues.append(V.warning(
                V.UNITS_EMPTY,
                "scrape succeeded but no units were extracted",
                canonical_id=cid, row_index=idx,
                details={"apis": len(scrape_result.get("_raw_api_responses") or [])},
            ))

        # ── Carry-forward if we lost a previously-known property ───────────
        carry_forward_used = False
        if (scrape_failed or not public_units) and state.is_known(cid):
            cf_units = state.carry_forward_units(cid, run_date)
            if cf_units:
                carry_forward_used = True
                carry_forward_count += 1
                units_total["carried_forward"] += len(cf_units)
                public_units = cf_units
                # Recompute stats over the carry-forward set.
                per_prop_issues.append(V.info(
                    V.UNITS_CARRIED_FORWARD,
                    f"carried forward {len(cf_units)} units from prior state",
                    canonical_id=cid, row_index=idx,
                    details={"count": len(cf_units)},
                ))

        # ── Diff against unit state ────────────────────────────────────────
        unit_diff: dict = {"new": [], "updated": [], "unchanged": [], "disappeared": []}
        try:
            unit_diff = state.upsert_units(cid, public_units, run_date)
            units_total["extracted"]   += len(public_units)
            units_total["new"]         += len(unit_diff["new"])
            units_total["updated"]     += len(unit_diff["updated"])
            units_total["unchanged"]   += len(unit_diff["unchanged"])
            units_total["disappeared"] += len(unit_diff["disappeared"])
            if unit_diff["disappeared"]:
                per_prop_issues.append(V.info(
                    V.UNITS_DISAPPEARED,
                    f"{len(unit_diff['disappeared'])} units disappeared since last run",
                    canonical_id=cid, row_index=idx,
                    details={"unit_ids": unit_diff["disappeared"]},
                ))
        except Exception as e:
            per_prop_issues.append(V.error(
                V.PIPELINE_EXCEPTION,
                f"exception during unit diff: {e}",
                canonical_id=cid, row_index=idx,
                details={"exception": str(e)},
            ))

        # ── Upsert property-level state ────────────────────────────────────
        try:
            was_new = state.upsert_property(cid, {
                "canonical_id": cid,
                "name":         csv_get(row, *NAME_KEYS),
                "address":      csv_get(row, *ADDRESS_KEYS),
                "city":         csv_get(row, *CITY_KEYS),
                "state":        csv_get(row, *STATE_KEYS),
                "zip":          csv_get(row, *ZIP_KEYS),
                "website":      url,
                "last_scrape_status": "FAILED" if scrape_failed else "SUCCESS",
                "last_units_count":   len(public_units),
            }, run_date)
            if was_new:
                per_prop_issues.append(V.info(
                    V.PROPERTY_NEW,
                    f"new property first seen today: {cid}",
                    canonical_id=cid, row_index=idx,
                ))
        except Exception as e:
            per_prop_issues.append(V.error(
                V.PIPELINE_EXCEPTION,
                f"exception during property upsert: {e}",
                canonical_id=cid, row_index=idx,
            ))

        # ── Profile update (non-fatal) ────────────────────────────────────
        if profile_store is not None and _PROFILES_AVAILABLE:
            try:
                profile = profile_store.load(cid)
                if profile is None:
                    profile = profile_store.bootstrap_from_meta(
                        cid, dict(row), url or "",
                    )
                profile = update_profile_after_extraction(
                    profile, scrape_result, len(public_units), profile_store,
                )
                drift_detected, drift_reasons = detect_drift(
                    profile, len(public_units), scrape_result,
                )
                if drift_detected:
                    profile = apply_drift_demotion(profile, drift_reasons)
                    profile_store.save(profile)
                    per_prop_issues.append(V.warning(
                        "PROFILE_DRIFT_DETECTED",
                        f"drift detected: {'; '.join(drift_reasons)}",
                        canonical_id=cid, row_index=idx,
                    ))
            except Exception as e:
                log.warning(f"  profile update failed for {cid}: {e}")

        # ── Collect LLM interaction records for cost accounting ───────────
        llm_interactions: list[dict] = scrape_result.get("_llm_interactions") or []
        if llm_interactions:
            all_llm_interactions.extend(llm_interactions)
            try:
                from llm.interaction_logger import write_property_report
                write_property_report(cid, llm_interactions, run_dir)
                log.info(f"  LLM interactions: {len(llm_interactions)} call(s), "
                         f"total cost=${sum(i.get('cost_usd', 0) for i in llm_interactions):.5f}")
            except Exception as e:
                log.warning(f"  could not write LLM report for {cid}: {e}")

        # ── Save raw API bodies for this property (debug aid) ─────────────
        raw = scrape_result.get("_raw_api_responses")
        if raw:
            try:
                safe_cid = "".join(c if c.isalnum() or c in "-_" else "_" for c in cid)[:80]
                with open(raw_dir / f"{safe_cid}.json", "w", encoding="utf-8") as f:
                    json.dump(raw, f, indent=2, default=str)
            except Exception as e:
                log.warning(f"  could not save raw API dump for {cid}: {e}")

        # ── Build record ───────────────────────────────────────────────────
        state_snapshot = state.get_property(cid)
        rec: dict | None = None
        try:
            if schema_version == "v2":
                from schema_v2 import build_v2_property, validate_v2_property
                rec = build_v2_property(
                    row, ident, scrape_result, public_units,
                    scrape_ts=datetime.now(UTC),
                )
                v2_issues = validate_v2_property(rec, canonical_id=cid)
                per_prop_issues.extend(v2_issues)
            else:
                rec = build_property_record(
                    row, ident, scrape_result, public_units,
                    state_snapshot, carry_forward_used,
                )
            properties_out.append(rec)
        except Exception as e:
            per_prop_issues.append(V.error(
                V.PIPELINE_EXCEPTION,
                f"exception building property record: {e}",
                canonical_id=cid, row_index=idx,
                details={"exception": str(e), "traceback": traceback.format_exc()[-1500:]},
            ))
            failed_properties.append({
                "row_index": idx, "canonical_id": cid,
                "reason": f"build_property_record exception: {e}",
            })

        # ── Per-property scrape report (markdown) ─────────────────────────
        try:
            from scrape_report import generate_property_report
            rpt_path = generate_property_report(
                scrape_result, rec, unit_diff,
                per_prop_issues, run_dir, cid, run_date,
            )
            if rpt_path:
                log.info(f"  scrape report: {rpt_path.name}")
        except Exception as e:
            log.warning(f"  could not write scrape report for {cid}: {e}")

        # Track failed properties for the report summary.
        if scrape_failed and not carry_forward_used:
            failed_properties.append({
                "row_index": idx, "canonical_id": cid,
                "reason": "SCRAPE_FAILED_NO_CARRY_FORWARD",
            })

        # Flush per-property issues to the run-wide log and issues.jsonl.
        all_issues.extend(per_prop_issues)
        _write_issues_jsonl(issues_path, per_prop_issues)

        log.info(f"  → units={len(public_units)} "
                 f"(new={len(unit_diff['new'])}), "
                 f"issues={len(per_prop_issues)}, carry_forward={carry_forward_used}, "
                 f"url={url[:60] if url else 'none'}")

        # ── Checkpoint: write ledger entry ────────────────────────────────
        ledger_status = "FAILED" if (scrape_failed and not carry_forward_used) else "SUCCESS"
        has_errors = any(i.severity == "ERROR" for i in per_prop_issues)
        if has_errors and ledger_status == "SUCCESS":
            ledger_status = "SUCCESS_WITH_ERRORS"
        _append_ledger(ledger_path, {
            "canonical_id": cid, "row_index": idx,
            "status": ledger_status,
            "units_count": len(public_units),
            "carry_forward_used": carry_forward_used,
            "scrape_failed": scrape_failed,
            "error_count": sum(1 for i in per_prop_issues if i.severity == "ERROR"),
            "warning_count": sum(1 for i in per_prop_issues if i.severity == "WARNING"),
            "url": url,
            "timestamp": datetime.now(UTC).isoformat(),
        })

        # ── Incremental save ──────────────────────────────────────────────
        try:
            with open(properties_path, "w", encoding="utf-8") as f:
                json.dump(properties_out, f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            log.error(f"  ⚠ incremental save failed: {e}")

    # ── Final save so rows that `continue`d (unresolved, dup) are persisted ──
    try:
        with open(properties_path, "w", encoding="utf-8") as f:
            json.dump(properties_out, f, indent=2, ensure_ascii=False, default=str)
    except Exception as e:
        log.error(f"⚠ final properties.json save failed: {e}")

    # ── 6. Detect disappeared properties ──────────────────────────────────
    disappeared = sorted(prior_property_ids - seen_ids_today)
    for cid in disappeared:
        all_issues.append(V.warning(
            V.PROPERTY_DISAPPEARED,
            f"property {cid} was in state but not in today's CSV",
            canonical_id=cid,
        ))
    if disappeared:
        _write_issues_jsonl(issues_path,
                            [V.warning(V.PROPERTY_DISAPPEARED,
                                       f"property {c} missing from today's CSV",
                                       canonical_id=c) for c in disappeared])

    # ── 7. Save state ─────────────────────────────────────────────────────
    try:
        state.save()
        log.info(f"State saved: {len(state.property_index)} properties, "
                 f"{sum(len(u) for u in state.unit_index.values())} tracked units")
    except Exception as e:
        log.error(f"⚠ failed to save state: {e}")

    finished_at = datetime.now(UTC)

    # ── 8. Build report ───────────────────────────────────────────────────
    new_ids = sorted(seen_ids_today - prior_property_ids)
    report = {
        "run_date":    run_date,
        "started_at":  started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_s":  (finished_at - started_at).total_seconds(),
        "exit_status": "OK",
        "csv_path":    str(csv_path),
        "data_dir":    str(data_dir),
        "totals": {
            "csv_rows":                 len(rows),
            "identities_resolved":      sum(1 for i in identities if i.canonical_id),
            "properties_processed":     len(processed),
            "properties_emitted":       len(properties_out),
            "properties_failed":        len(failed_properties),
            "scrape_successes":         len(processed) - sum(1 for f in failed_properties
                                                               if "SCRAPE_FAILED" in f.get("reason", "")),
        },
        "identity": {
            "resolved":        sum(1 for i in identities if i.canonical_id),
            "unresolved":      len(dup_report.unresolved_rows),
            "hard_duplicates": dup_report.hard_duplicates,
            "soft_duplicates": dup_report.soft_duplicates,
            "geo_duplicates":  dup_report.geo_duplicates,
            "by_source":       {
                src: sum(1 for i in identities if i.id_source == src)
                for src in ("unique_id", "property_id", "address_fp",
                            "geo_fp", "website_fp", "unresolved")
            },
        },
        "issues": V.summarise_issues(all_issues),
        "state_diff": {
            "new_properties":         new_ids,
            "disappeared_properties": disappeared,
            "carry_forward_count":    carry_forward_count,
            "units_extracted":        units_total["extracted"],
            "units_new":              units_total["new"],
            "units_updated":          units_total["updated"],
            "units_unchanged":        units_total["unchanged"],
            "units_disappeared":      units_total["disappeared"],
            "units_carried_forward":  units_total["carried_forward"],
        },
        "failed_properties": failed_properties,
    }

    try:
        with open(report_json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        _write_markdown_report(report_md, report)
        log.info(f"Report written: {report_json.name}, {report_md.name}")
    except Exception as e:
        log.error(f"⚠ report write failed: {e}")

    # ── LLM cost summary (run-wide aggregate) ─────────────────────────────
    try:
        from llm.interaction_logger import write_run_summary
        write_run_summary(all_llm_interactions, run_dir)
        total_llm_cost = sum(i.get("cost_usd", 0) for i in all_llm_interactions)
        log.info(
            f"LLM report: {len(all_llm_interactions)} total call(s) across "
            f"{len({i.get('property_id') for i in all_llm_interactions})} propert(ies) | "
            f"total cost=${total_llm_cost:.5f} | "
            f"→ {run_dir / 'llm_report.json'}"
        )
    except Exception as e:
        log.warning(f"⚠ LLM run summary write failed: {e}")

    # Update latest-run pointer (schema-namespaced + global).
    try:
        pointer = {"run_date": run_date, "path": str(run_dir),
                   "schema_version": schema_version,
                   "properties": report["totals"]["properties_emitted"]}
        # Per-schema pointer
        with open(schema_root / "latest_run.json", "w", encoding="utf-8") as f:
            json.dump(pointer, f, indent=2)
        # Global pointer (whichever schema ran last)
        with open(data_dir / "latest_run.json", "w", encoding="utf-8") as f:
            json.dump(pointer, f, indent=2)
    except Exception:
        pass

    log.info(f"=== Done in {report['duration_s']:.1f}s: "
             f"{report['totals']['properties_emitted']} properties, "
             f"{units_total['extracted']} units, "
             f"{report['issues']['total']} issues ===")

    return report

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Daily multi-property runner")
    p.add_argument("--csv",      default=str(_PROJECT_ROOT / "config" / "properties.csv"),
                   help="Path to properties CSV (default: ma_poc/config/properties.csv)")
    p.add_argument("--data-dir", default=str(_PROJECT_ROOT / "data"),
                   help="Root data directory (runs/, state/ live under here; default: ma_poc/data)")
    p.add_argument("--run-date", default=None,
                   help="Override run date (YYYY-MM-DD); defaults to today")
    p.add_argument("--limit",    type=int, default=None,
                   help="Process at most N rows")
    p.add_argument("--start-at", type=int, default=0,
                   help="Skip first N rows")
    p.add_argument("--proxy",    default=None)
    p.add_argument("--scrape-timeout", type=int, default=180,
                   help="Per-property scrape timeout (seconds)")
    p.add_argument("--schema-version", choices=["v1", "v2"], default=None,
                   help="Output schema version (default: env SCHEMA_VERSION or v1)")
    args = p.parse_args()

    csv_path = Path(args.csv)
    data_dir = Path(args.data_dir)
    run_date = args.run_date or date.today().isoformat()

    from schema_v2 import get_schema_version
    schema_version = get_schema_version(args)

    try:
        report = asyncio.run(run_daily(
            csv_path, run_date, data_dir,
            args.limit, args.start_at, args.proxy,
            args.scrape_timeout,
            schema_version=schema_version,
        ))
        if report.get("exit_status") == "FATAL":
            sys.exit(2)
        # Non-zero only if nothing was emitted at all.
        if report["totals"]["properties_emitted"] == 0:
            sys.exit(1)
        sys.exit(0)
    except KeyboardInterrupt:
        log.error("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        log.error(f"Fatal: {e}")
        log.error(traceback.format_exc())
        sys.exit(2)

if __name__ == "__main__":
    main()
