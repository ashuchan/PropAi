"""
Jugnu J8 — Integrated daily runner using Jugnu L1-L5 layers.

This wraps the existing daily_runner.py flow with Jugnu's:
  - L2 Scheduler for task generation
  - L1 Fetcher for HTTP/Playwright requests
  - L3 Scraper (via scrape_jugnu) with short-circuit on non-OK fetch
  - L4 Validation with schema gate + identity fallback
  - L5 Observability (event ledger, cost ledger, SLO checks)
  - L2 Carry-forward safety net on failures

Supports both v1 and v2 output schemas via --schema-version flag.

Usage:
  python scripts/jugnu_runner.py --csv config/properties.csv --limit 20
  python scripts/jugnu_runner.py --csv config/properties.csv --schema-version v2
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

# Ensure ma_poc is importable regardless of working directory.
# _repo_root lets ``from ma_poc.pms...`` resolve; _MA_POC_ROOT lets
# ``from services.profile_store...`` and ``from models....`` resolve
# (those packages live directly under ma_poc/, not ma_poc/ma_poc/).
_repo_root = Path(__file__).resolve().parent.parent.parent
_MA_POC_ROOT = Path(__file__).resolve().parent.parent  # ma_poc/
for _p in (_repo_root, _MA_POC_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("jugnu_runner")


def _resolve_schema_version(args: Any = None) -> str:
    """Resolve schema version from CLI args > env > default.

    Args:
        args: argparse namespace with optional ``schema_version`` attribute.

    Returns:
        ``"v1"`` or ``"v2"``.
    """
    if args and getattr(args, "schema_version", None):
        return args.schema_version
    return os.getenv("SCHEMA_VERSION", "v1").strip().lower()


def _resolve_data_dirs(
    data_dir: Path,
    schema_version: str,
    run_date: str,
) -> tuple[Path, Path, Path, Path]:
    """Resolve schema-namespaced data directories.

    V1 uses data/runs/{date}/ and data/state/ (legacy flat layout).
    V2 uses data/v2/runs/{date}/ and data/v2/state/.

    Args:
        data_dir: Base data directory.
        schema_version: "v1" or "v2".
        run_date: Date string for this run.

    Returns:
        (run_dir, state_dir, cache_dir, schema_root)
    """
    if schema_version == "v2":
        schema_root = data_dir / "v2"
    else:
        schema_root = data_dir

    run_dir = schema_root / "runs" / run_date
    state_dir = schema_root / "state"
    cache_dir = schema_root / "cache"

    run_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    return run_dir, state_dir, cache_dir, schema_root


async def run_jugnu(
    csv_path: Path,
    data_dir: Path = _MA_POC_ROOT / "data",
    limit: int | None = None,
    proxy: str | None = None,
    run_date: str | None = None,
    schema_version: str = "v1",
    start_index: int = 0,
) -> dict[str, Any]:
    """Run the Jugnu integrated pipeline.

    Args:
        csv_path: Path to properties CSV.
        data_dir: Base data directory.
        limit: Max properties to process.
        proxy: Proxy URL.
        run_date: Override run date (YYYY-MM-DD).
        schema_version: "v1" or "v2" output format.
        start_index: Zero-based row index in the CSV to start scraping from.
            Rows before this index are skipped. ``limit`` still caps the
            number of rows processed *after* skipping.

    Returns:
        Run summary dict.
    """
    from ma_poc.discovery.change_detector import decide as decide_change
    from ma_poc.discovery.contracts import CrawlTask
    from ma_poc.discovery.dlq import Dlq
    from ma_poc.discovery.frontier import Frontier
    from ma_poc.discovery.scheduler import Scheduler
    from ma_poc.discovery.sitemap import SitemapConsumer
    from ma_poc.fetch import fetch as jugnu_fetch
    from ma_poc.fetch.conditional import ConditionalCache
    from ma_poc.observability import events
    from ma_poc.observability.cost_ledger import CostLedger
    from ma_poc.observability.slo_watcher import check as slo_check
    from ma_poc.reporting.run_report import build as build_run_report

    # Setup
    today = run_date or date.today().isoformat()
    run_dir, state_dir, cache_dir, schema_root = _resolve_data_dirs(
        data_dir,
        schema_version,
        today,
    )
    run_id = f"{today}_{uuid.uuid4().hex[:8]}"

    log.info("Schema version: %s", schema_version)
    log.info("Run directory: %s", run_dir)

    # Configure observability
    events.configure(run_dir, run_id)
    cost_ledger = CostLedger(run_dir / "cost_ledger.db")

    # Load CSV
    rows = _load_csv(csv_path, limit, start_index=start_index)
    log.info(
        "Loaded %d properties from %s (start_index=%d, limit=%s)",
        len(rows),
        csv_path,
        start_index,
        limit,
    )

    # Setup L2 components
    frontier = Frontier(state_dir / "frontier.sqlite")
    dlq = Dlq(state_dir / "dlq.jsonl")
    cond_cache = ConditionalCache(cache_dir / "conditional.sqlite")
    sitemap = SitemapConsumer(fetcher=jugnu_fetch, cond_cache=cond_cache)

    # Dummy profile store (reads from ma_poc/config/profiles/ if available)
    profile_store = _SimpleProfileStore(_MA_POC_ROOT / "config" / "profiles")

    scheduler = Scheduler(
        frontier=frontier,
        dlq=dlq,
        sitemap=sitemap,
        profile_store=profile_store,
        change_detector_fn=decide_change,
    )

    # Build tasks
    tasks: list[CrawlTask] = []
    async for task in scheduler.build_tasks(rows):
        tasks.append(task)
    log.info("Scheduled %d tasks", len(tasks))

    # Build CSV lookup for output formatting
    csv_lookup = {row["property_id"]: row for row in rows}

    # Determine concurrency pool size
    from ma_poc.scripts.concurrency import AsyncPool, SystemResources

    res = SystemResources.detect()
    pool_size = res.optimal_pool_size()
    log.info("System resources: %s → pool_size=%d", res.summary(), pool_size)
    pool = AsyncPool(pool_size)

    async def _process_one(task: Any) -> dict[str, Any]:
        log.info("Processing %s (%s)", task.property_id, task.url)
        try:
            csv_row = csv_lookup.get(task.property_id, {})
            result = await _process_property(
                task,
                cost_ledger,
                profile_store,
                frontier,
                dlq,
                data_dir,
                csv_row=csv_row,
                run_dir=run_dir,
            )
            formatted = _format_output(result, csv_row, schema_version)
            # F2: Null Field Recovery — runs on v2 units with null rent_low
            # / unit_id produced by Tier-1 adapters. Applies high-confidence
            # recoveries in place on the unit dicts.
            if schema_version == "v2":
                await _run_null_field_recovery(
                    result,
                    formatted,
                    run_dir,
                    task.property_id,
                )
            # Per-property report — same format as daily_runner emits, but
            # sourced from jugnu's raw scrape_result + formatted v1/v2 record
            # so v2 metadata (apartment_id/pmc/website_design/concessions)
            # and v2 unit fields (beds/baths/rent_low/rent_high) render.
            _write_property_report(
                result,
                formatted,
                run_dir,
                task.property_id,
                today,
            )
            return formatted
        except Exception as exc:
            log.error("Property %s crashed: %s", task.property_id, exc)
            return _make_failed_record(
                task.property_id,
                task.url,
                str(exc),
                schema_version,
            )

    results = await pool.map(_process_one, [(t,) for t in tasks])

    # Collect results and write output
    properties: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            log.error("Task returned exception: %s", r)
            continue
        properties.append(r)

    # Merge with any properties.json already in run_dir so partial / resumed
    # runs (e.g. --start-index 100 after a prior --start-index 0 --limit 100)
    # don't clobber the earlier batch. Reports run off the merged list so the
    # totals reflect everything in the run dir, not just this invocation.
    properties_path = run_dir / "properties.json"
    merged_properties = _merge_with_existing_properties(properties_path, properties)
    _write_properties_incremental(properties_path, merged_properties)

    # Run-level reporting
    cost_rollup = cost_ledger.total()
    slo_violations = slo_check(cost_rollup, merged_properties)
    report = build_run_report(merged_properties, run_dir, today, cost_rollup, slo_violations)

    # Cleanup
    cost_ledger.close()
    frontier.close()
    cond_cache.close()
    events.shutdown()

    log.info(
        "Jugnu run complete: this batch=%d, run-dir total=%d, failed=%d",
        len(properties),
        len(merged_properties),
        report["totals"]["failed"],
    )
    return report


async def _process_property(
    task: Any,
    cost_ledger: Any,
    profile_store: Any,
    frontier: Any,
    dlq: Any,
    data_dir: Path,
    csv_row: dict[str, Any] | None = None,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    """Process a single property through L1-L4.

    Args:
        task: CrawlTask for this property.
        cost_ledger: CostLedger for recording costs.
        profile_store: Profile store.
        frontier: Frontier for recording outcomes.
        dlq: DLQ for parking/unparking.
        data_dir: Base data directory.

    Returns:
        Property result dict (internal format).
    """
    from ma_poc.discovery.carry_forward import should_carry_forward
    from ma_poc.fetch import fetch as jugnu_fetch
    from ma_poc.observability.events import EventKind, emit
    from ma_poc.pms.scraper import scrape_jugnu
    from ma_poc.reporting.verdict import compute as compute_verdict
    from ma_poc.validation.orchestrator import validate

    # L1: Fetch
    fetch_result = await jugnu_fetch(task)
    frontier.mark_attempt(task.url, fetch_result.outcome)

    # Check carry-forward need
    outcome_val = fetch_result.outcome.value
    if not fetch_result.ok():
        should_cf, reason = should_carry_forward(None, fetch_outcome=outcome_val)
        if should_cf:
            # Try carry-forward from prior state
            from ma_poc.discovery.carry_forward import carry_forward_property
            from ma_poc.scripts.state_store import StateStore

            try:
                state_store = StateStore(data_dir / "state")
                cf_record = carry_forward_property(
                    task.property_id, data_dir / "runs" / "latest", state_store, reason
                )
                if cf_record:
                    # Stamp a SUCCESS verdict so run_report counts this
                    # correctly. Without this the verdict stays None and
                    # the dashboard shows "verdict=None" for carry-forward
                    # properties — confusing because they DO have units.
                    cf_meta = cf_record.setdefault("_meta", {}) or cf_record["_meta"]
                    cf_meta.setdefault("canonical_id", task.property_id)
                    cf_meta["verdict"] = "SUCCESS"
                    cf_meta.setdefault("verdict_reason", "carry_forward_applied")
                    return cf_record
            except Exception:
                pass

    # L3: Extract
    # Bootstrap a COLD profile the first time we see a property so the
    # adapter dispatch has maturity/preferred_tier hints to work with and
    # so the self-learning loop has a target to update below.
    profile = profile_store.get_profile(task.property_id)
    if profile is None and hasattr(profile_store, "bootstrap"):
        profile = profile_store.bootstrap(task.property_id, {}, task.url)
    result = await scrape_jugnu(
        task=task,
        fetch_result=fetch_result,
        page=None,  # Would be provided in full RENDER mode
        profile=profile,
        csv_row=csv_row,
    )

    # ── Profile self-learning loop ────────────────────────────────────
    # After every scrape, update what the profile knows: winning URL,
    # known_endpoints, blocked_endpoints, consecutive_successes/failures,
    # maturity promotion/demotion. Then run drift detection to demote
    # profiles whose extraction regressed (unit count dropped, all rents
    # null, repeated timeouts) — same semantics as daily_runner.
    if profile is not None:
        try:
            from services.drift_detector import apply_drift_demotion, detect_drift
            from services.profile_updater import update_profile_after_extraction

            units_extracted = len(result.get("units") or [])
            profile = update_profile_after_extraction(
                profile,
                result,
                units_extracted,
                profile_store.backing if hasattr(profile_store, "backing") else profile_store,
            )
            drift_detected, reasons = detect_drift(profile, units_extracted, result)
            if drift_detected:
                profile = apply_drift_demotion(profile, reasons)
            if hasattr(profile_store, "save"):
                profile_store.save(profile)
        except Exception as exc:
            log.debug("profile update failed for %s: %s", task.property_id, exc)

    # L4: Validate
    extract_result = result.get("_extract_result")
    if extract_result:
        validated = validate(extract_result)
        result["_validated"] = validated.to_dict()

        # Record costs
        if hasattr(extract_result, "llm_cost_usd") and extract_result.llm_cost_usd > 0:
            pms = result.get("_detected_pms", {}).get("pms", "unknown")
            tier = result.get("extraction_tier_used", "unknown")
            cost_ledger.record_llm(
                task.property_id,
                pms,
                tier,
                extract_result.llm_cost_usd,
                "gpt-4o-mini",
                0,  # tokens not tracked at this level
            )

    # Verdict
    verdict = compute_verdict(
        fetch_outcome=outcome_val,
        extract_result=extract_result,
        carry_forward_applied=result.get("_meta", {}).get("carry_forward_used", False),
    )
    meta = result.setdefault("_meta", {})
    meta["canonical_id"] = task.property_id
    meta["verdict"] = verdict.verdict.value
    meta["verdict_reason"] = verdict.reason

    emit(
        EventKind.PROPERTY_EMITTED,
        task.property_id,
        verdict=verdict.verdict.value,
        units=len(result.get("units", [])),
    )

    # ── F1: Adapter Debugger ──────────────────────────────────────────────
    # Runs once per FAILED_NO_DATA on TIER_1_* tiers. Gated by existing
    # diagnosis file (one diagnosis per property per run is enough).
    _tier_used = ""
    if extract_result is not None:
        _tier_used = getattr(extract_result, "tier_used", "") or ""
    if run_dir is not None and verdict.verdict.value == "FAILED_NO_DATA" and _tier_used.startswith("TIER_1_"):
        try:
            from ma_poc.services.llm_diagnostics import (
                adapter_debugger,
                get_adapter_parser_source,
            )

            _raw_apis = result.get("_raw_api_responses") or []
            _KNOWN_ADAPTERS = {
                "rentcafe",
                "sightmap",
                "appfolio",
                "entrata",
                "onesite",
                "realpage",
                "generic",
            }
            _adapter_name = (
                _tier_used.replace("TIER_1_API_", "").replace("TIER_1_API", "generic").lower()
            ) or "generic"
            if _adapter_name not in _KNOWN_ADAPTERS:
                _adapter_name = "generic"
            _parser_source = get_adapter_parser_source(_adapter_name)
            _diag_dir = run_dir / "llm_diagnostics"
            _diag_path = _diag_dir / f"{task.property_id}_adapter_debug.json"

            if _diag_path.exists():
                log.debug("F1 diagnosis already exists for %s, skipping", task.property_id)
            else:
                _csv_row = csv_row or {}
                for _resp in _raw_apis[:3]:
                    _diag = await adapter_debugger(
                        property_id=task.property_id,
                        adapter_name=_adapter_name,
                        adapter_parser_source=_parser_source,
                        api_response=_resp,
                        property_context={
                            "property_name": str(
                                _csv_row.get("name")
                                or _csv_row.get("Name")
                                or _csv_row.get("proj_name")
                                or ""
                            ),
                            "website": str(_csv_row.get("website") or _csv_row.get("Website") or ""),
                            "city": str(_csv_row.get("city") or _csv_row.get("City") or ""),
                            "state": str(_csv_row.get("state") or _csv_row.get("State") or ""),
                        },
                        output_dir=_diag_dir,
                    )
                    if _diag is not None:
                        log.info(
                            "  F1 diagnosis for %s: %s (can_auto_fix=%s, recoverable=%d units)",
                            task.property_id,
                            _diag.failure_category,
                            _diag.can_auto_fix,
                            _diag.estimated_units_recoverable,
                        )
                        _interaction = getattr(_diag, "_llm_interaction", None)
                        if isinstance(_interaction, dict):
                            result.setdefault("_llm_interactions", []).append(_interaction)
                        break
        except Exception as _exc:
            log.debug("F1 adapter_debugger hook failed for %s: %s", task.property_id, _exc)

    return result


# ---------------------------------------------------------------------------
# Output formatting — v1 / v2
# ---------------------------------------------------------------------------


def _format_output(
    result: dict[str, Any],
    csv_row: dict[str, Any],
    schema_version: str,
) -> dict[str, Any]:
    """Format a raw Jugnu result into the target output schema.

    Args:
        result: Internal result dict from _process_property.
        csv_row: Original CSV row for this property (for field enrichment).
        schema_version: "v1" or "v2".

    Returns:
        Formatted property dict.
    """
    if schema_version == "v2":
        return _format_v2(result, csv_row)
    return _format_v1(result, csv_row)


def _format_v1(result: dict[str, Any], csv_row: dict[str, Any]) -> dict[str, Any]:
    """Format internal result as v1 (46-key schema).

    Produces the same structure as daily_runner.build_property_record but
    without requiring PropertyIdentity — uses CSV row + scrape metadata.
    """
    meta = result.get("_meta", {})
    md = result.get("property_metadata") or {}
    units = result.get("units", [])
    canonical_id = meta.get("canonical_id", "")

    def _csv(key: str) -> Any:
        """Get a cleaned CSV value."""
        v = csv_row.get(key)
        if v in (None, "", "null", "None"):
            return None
        return str(v).strip() if isinstance(v, str) else v

    def _pick(csv_val: Any, scraped_val: Any) -> Any:
        if csv_val not in (None, "", "null", "None"):
            return csv_val
        return scraped_val if scraped_val not in (None, "", "null", "None") else None

    # Compute aggregates from units
    total_units = len(units) if units else None
    avg_sqft = None
    if units:
        sqfts = [u.get("sqft") or u.get("area") or u.get("_sqft") for u in units]
        sqfts = [s for s in sqfts if s and isinstance(s, (int, float)) and s > 0]
        if sqfts:
            avg_sqft = round(sum(sqfts) / len(sqfts))

    rec: dict[str, Any] = {
        "Property Name": _pick(
            _csv("name") or _csv("Property Name"),
            md.get("name") or md.get("title"),
        ),
        "Type": _csv("Type") or _csv("type"),
        "Unique ID": _csv("apartmentid") or _csv("Unique ID") or canonical_id,
        "Property ID": _csv("Property ID") or _csv("apartmentid") or canonical_id,
        "Property Address": _pick(
            _csv("address") or _csv("Property Address"),
            md.get("address"),
        ),
        "City": _pick(_csv("city") or _csv("City"), md.get("city")),
        "State": _pick(_csv("state") or _csv("State"), md.get("state")),
        "ZIP Code": _pick(_csv("zip") or _csv("ZIP Code"), md.get("zip")),
        "Latitude": md.get("latitude"),
        "Longitude": md.get("longitude"),
        "Management Company": _csv("Management Company"),
        "Phone": _pick(_csv("Phone"), md.get("telephone")),
        "Website": _csv("website") or _csv("Website") or result.get("base_url"),
        "Year Built": md.get("year_built"),
        "Stories": md.get("stories"),
        "Total Units": total_units,
        "Average Unit Size (SF)": avg_sqft,
        "Unit Mix": None,
        "First Move-In Date": None,
        "Property Type": _csv("Property Type"),
        "Property Status": _csv("Property Status") or "Active",
        "Property Style": _csv("Property Style") or _csv("Building Type"),
        "Property Image URL": md.get("image_url"),
        "Property Gallery URLs": md.get("gallery_urls") or [],
        "Update Date": date.today().isoformat(),
        # External-only fields (CSV passthrough)
        "Census Block Id": _csv("Census Block Id"),
        "Tract Code": _csv("Tract Code"),
        "Construction Start Date": _csv("Construction Start Date"),
        "Construction Finish Date": _csv("Construction Finish Date"),
        "Renovation Start": _csv("Renovation Start"),
        "Renovation Finish": _csv("Renovation Finish"),
        "Development Company": _csv("Development Company"),
        "Property Owner": _csv("Property Owner"),
        "Region": _csv("Region"),
        "Market Name": _csv("Market Name"),
        "Submarket Name": _csv("Submarket Name"),
        "Asset Grade in Submarket": _csv("Asset Grade in Submarket"),
        "Asset Grade in Market": _csv("Asset Grade in Market"),
        "Lease Start Date": _csv("Lease Start Date"),
        # Units
        "units": units,
        # Metadata
        "_meta": meta,
        "_extract_result": _extract_result_summary(result),
    }
    return rec


def _extract_result_summary(result: dict[str, Any]) -> dict[str, Any] | None:
    """Project ``result["_extract_result"]`` down to a JSON-safe dict.

    Why: ``run_report.py`` and ``slo_watcher.py`` key off
    ``_extract_result.tier_used`` to compute tier_distribution. Without
    this projection the emitted property record has no ``_extract_result``
    key at all (the dataclass lives on the in-process result dict but never
    reaches ``properties.json``), so every run reports ``tier=UNKNOWN``.
    """
    er = result.get("_extract_result")
    if er is None:
        return None
    if isinstance(er, dict):
        tier = er.get("tier_used")
        llm_cost = er.get("llm_cost_usd", 0.0)
    else:
        tier = getattr(er, "tier_used", None)
        llm_cost = getattr(er, "llm_cost_usd", 0.0)
    return {
        "tier_used": tier,
        "llm_cost_usd": llm_cost,
    }


def _format_v2(result: dict[str, Any], csv_row: dict[str, Any]) -> dict[str, Any]:
    """Format internal result as v2 (flat schema with normalized units).

    Uses the same logic as schema_v2.build_v2_property but without
    requiring PropertyIdentity.
    """
    meta = result.get("_meta", {})
    md = result.get("property_metadata") or {}
    units = result.get("units", [])
    meta.get("canonical_id", "")
    scrape_ts = datetime.now(UTC)

    def _csv(key: str) -> Any:
        v = csv_row.get(key)
        if v in (None, "", "null", "None"):
            return None
        return str(v).strip() if isinstance(v, str) else v

    def _pick(csv_val: Any, scraped_val: Any) -> Any:
        if csv_val not in (None, "", "null", "None"):
            return csv_val
        return scraped_val if scraped_val not in (None, "", "null", "None") else None

    # apartment_id as integer
    aid = _csv("apartmentid") or _csv("apartment_id") or _csv("Unique ID")
    try:
        apartment_id = int(float(str(aid).replace(",", ""))) if aid else None
    except (ValueError, TypeError):
        apartment_id = None

    # Platform / website design
    platform = (
        result.get("platform_detected")
        or (md.get("api_provider") if md else None)
        or meta.get("scrape_tier_used", "")
    )
    _platform_labels = {
        "entrata": "Powered by Entrata",
        "rentcafe": "Powered by RentCafe",
        "appfolio": "Powered by AppFolio",
        "yardi": "Powered by RentCafe (Yardi)",
        "realpage": "Powered by RealPage",
        "sightmap": "Powered by SightMap",
    }
    website_design = _platform_labels.get(str(platform).lower(), platform or None)

    concessions_text = result.get("concessions_text") or md.get("concessions")

    prop: dict[str, Any] = {
        "apartment_id": apartment_id,
        "proj_name": _pick(
            _csv("name") or _csv("Name"),
            md.get("name") or md.get("title"),
        ),
        "address": _pick(_csv("address") or _csv("Address"), md.get("address")),
        "city": _pick(_csv("city") or _csv("City"), md.get("city")),
        "state": _pick(_csv("state") or _csv("State"), md.get("state")),
        "zip_code": _format_zip(_pick(_csv("zip") or _csv("Zip"), md.get("zip"))),
        "country": md.get("country"),
        "phone": _pick(_csv("Phone") or _csv("phone"), md.get("telephone")),
        "email_address": md.get("email") or md.get("email_address"),
        "website": _csv("website") or _csv("Website") or result.get("base_url"),
        "pmc": _pick(_csv("Management Company") or _csv("pmc"), md.get("management_company")),
        "website_design": website_design,
        "concessions": concessions_text,
        "units": [_format_v2_unit(u, scrape_ts) for u in units],
        # Keep _meta for internal tracking (stripped on final delivery)
        "_meta": meta,
        "_extract_result": _extract_result_summary(result),
    }
    return prop


def _format_v2_unit(unit: dict[str, Any], scrape_ts: datetime) -> dict[str, Any]:
    """Format a single unit to v2 schema.

    Phase 1 fixes:
    - Alias ``unit_number`` to ``unit_id`` so API/DOM extractors that emit
      ``unit_number`` (the adapter convention) don't silently lose identity.
    - Parse ``rent_range`` string (e.g. "$1,200 - $1,500") as a fallback when
      numeric ``market_rent_low/high`` are missing. This recovers rent on
      TIER_1_API and TIER_2_JSONLD extractions that only produce the string.
    - Plumb ``lease_term`` / ``move_in_date`` with a broader key fallback so
      parsers can start populating them without another format change.
    """
    beds_raw = unit.get("_bedrooms") or unit.get("bedrooms") or unit.get("beds")
    baths_raw = unit.get("_bathrooms") or unit.get("bathrooms") or unit.get("baths")
    fp_name = unit.get("_floor_plan") or unit.get("floor_plan_name") or unit.get("floorplan_name")
    sqft = unit.get("_sqft") or unit.get("sqft") or unit.get("area")

    # unit_id alias: prefer an explicit unit_id but fall back to unit_number
    uid = unit.get("unit_id") or unit.get("unit_number") or unit.get("_unit_number")

    # Phase 5 junk filter: belt-and-braces with the adapter-level filter.
    # If an adapter outside GenericAdapter emitted a CMS-module plan name
    # or a stop-word unit number, scrub them here before the v2 record
    # ships downstream.
    try:
        from ma_poc.pms.adapters._parsing import is_junk_floor_plan, is_junk_unit_number

        if is_junk_floor_plan(fp_name):
            fp_name = None
        if is_junk_unit_number(uid):
            uid = None
    except Exception:
        pass

    # rent: numeric first, parse rent_range string if needed.
    rent_lo_raw = unit.get("market_rent_low") or unit.get("asking_rent")
    rent_hi_raw = unit.get("market_rent_high") or unit.get("asking_rent")
    if rent_lo_raw is None and rent_hi_raw is None:
        rent_range = unit.get("rent_range")
        if rent_range:
            try:
                from ma_poc.pms.adapters._parsing import parse_rent_range

                rent_lo_raw, rent_hi_raw = parse_rent_range(str(rent_range))
            except Exception:
                pass

    return {
        "beds": _normalize_beds(beds_raw),
        "baths": _normalize_baths(baths_raw),
        "floor_plan_name": fp_name or None,
        "area": _format_area(sqft),
        "unit_id": str(uid) if uid not in (None, "", "null") else None,
        "rent_low": _format_rent(rent_lo_raw),
        "rent_high": _format_rent(rent_hi_raw),
        "date_captured": scrape_ts.strftime("%Y-%m-%d %H:%M:%S"),
        "available_date": _format_date_str(unit.get("available_date")),
        "lease_term": _safe_int_gt1(unit.get("lease_term") or unit.get("_lease_term")),
        "move_in_date": _format_date_str(unit.get("move_in_date") or unit.get("_move_in_date")),
    }


async def _run_null_field_recovery(
    scrape_result: dict[str, Any],
    formatted: dict[str, Any],
    run_dir: Path,
    canonical_id: str,
) -> None:
    """Call LLM null-field recovery on v2 units missing rent_low or unit_id.

    Only runs for Tier-1 adapters with raw API responses captured. Applies
    high-confidence (>=0.85) recoveries in place on the formatted unit dicts.
    Best-effort — never raises.
    """
    try:
        meta = scrape_result.get("_meta", {}) or {}
        extract = scrape_result.get("_extract_result")
        tier = ""
        if extract is not None:
            tier = getattr(extract, "tier_used", "") or ""
        if not tier:
            tier = str(meta.get("scrape_tier_used", "") or "")

        raw_apis = scrape_result.get("_raw_api_responses") or []
        if not tier.startswith("TIER_1_") or not raw_apis:
            return

        units = formatted.get("units") or []
        null_units = [u for u in units if u.get("rent_low") is None or u.get("unit_id") is None]
        if not null_units:
            return

        from ma_poc.services.llm_diagnostics import (
            _build_parser_logic_summary,
            null_field_recovery,
        )

        adapter_name = tier.replace("TIER_1_API_", "").lower() or "generic"
        diag_dir = run_dir / "llm_diagnostics"

        source_body = raw_apis[0].get("body") if raw_apis else {}
        source_items: list[Any] = []
        if isinstance(source_body, list):
            source_items = source_body
        elif isinstance(source_body, dict):
            for k in ("data", "results", "floorplans", "FloorplanList", "Result"):
                v = source_body.get(k)
                if isinstance(v, list):
                    source_items = v
                    break

        property_context = {
            "property_name": str(formatted.get("proj_name") or ""),
            "website": str(formatted.get("website") or ""),
            "city": str(formatted.get("city") or ""),
            "state": str(formatted.get("state") or ""),
        }

        for i, unit in enumerate(null_units[:5]):
            fragment = source_items[i] if i < len(source_items) else source_body
            recovery = await null_field_recovery(
                property_id=canonical_id,
                partial_unit=unit,
                source_fragment=fragment,
                tier_used=tier,
                parser_logic_summary=_build_parser_logic_summary(adapter_name, tier),
                property_context=property_context,
                output_dir=diag_dir,
            )
            if recovery is None:
                continue
            interaction = getattr(recovery, "_llm_interaction", None)
            if isinstance(interaction, dict):
                scrape_result.setdefault("_llm_interactions", []).append(interaction)
            for rf in recovery.recovered_fields:
                if rf.confidence < 0.85 or rf.recovered_value is None:
                    continue
                if rf.field_name == "rent_low" and unit.get("rent_low") is None:
                    try:
                        unit["rent_low"] = _format_rent(rf.recovered_value)
                    except Exception:
                        pass
                elif rf.field_name == "rent_high" and unit.get("rent_high") is None:
                    try:
                        unit["rent_high"] = _format_rent(rf.recovered_value)
                    except Exception:
                        pass
                elif rf.field_name == "unit_id" and unit.get("unit_id") is None:
                    unit["unit_id"] = str(rf.recovered_value)
                elif rf.field_name == "floor_plan_name" and unit.get("floor_plan_name") is None:
                    unit["floor_plan_name"] = str(rf.recovered_value)
    except Exception as exc:
        log.debug("F2 null_field_recovery hook failed for %s: %s", canonical_id, exc)


def _write_property_report(
    scrape_result: dict[str, Any],
    property_record: dict[str, Any],
    run_dir: Path,
    canonical_id: str,
    run_date: str,
) -> None:
    """Write a per-property markdown report under ``{run_dir}/property_reports/``.

    Delegates to :func:`scripts.scrape_report.generate_property_report`, the
    same writer ``daily_runner`` uses, so report format stays consistent
    across the two runners. Jugnu passes the formatted v1/v2 record as
    ``property_record`` so the metadata section can render v2-specific
    fields (apartment_id, pmc, website_design, concessions).

    Jugnu has no legacy state-store diff, so ``unit_diff`` is empty. L4
    validation output (``scrape_result["_validated"]``) is translated into
    lightweight issue objects so the validation section still populates.
    Never raises — report generation is best-effort observability.
    """
    try:
        try:
            from scripts.scrape_report import generate_property_report
        except ImportError:
            from ma_poc.scripts.scrape_report import generate_property_report  # type: ignore[no-redef]
    except ImportError as exc:
        log.debug("scrape_report unavailable — skipping report for %s: %s", canonical_id, exc)
        return

    from types import SimpleNamespace

    validated = scrape_result.get("_validated") or {}
    issues: list[Any] = []
    for rej in validated.get("rejected") or []:
        msg = rej.get("reason") if isinstance(rej, dict) else str(rej)
        issues.append(
            SimpleNamespace(
                severity="ERROR",
                code="VALIDATION_REJECTED",
                message=str(msg)[:200],
            )
        )
    for fl in validated.get("flagged") or []:
        msg = fl.get("flag") if isinstance(fl, dict) else str(fl)
        issues.append(
            SimpleNamespace(
                severity="WARNING",
                code="VALIDATION_FLAGGED",
                message=str(msg)[:200],
            )
        )

    unit_diff: dict[str, list] = {
        "new": [],
        "updated": [],
        "unchanged": [],
        "disappeared": [],
    }

    try:
        generate_property_report(
            scrape_result=scrape_result,
            property_record=property_record,
            unit_diff=unit_diff,
            per_prop_issues=issues,
            run_dir=run_dir,
            canonical_id=canonical_id,
            run_date=run_date,
        )
    except Exception as exc:
        log.warning("property report generation failed for %s: %s", canonical_id, exc)


def _make_failed_record(
    property_id: str,
    url: str,
    error: str,
    schema_version: str,
) -> dict[str, Any]:
    """Create a failed property record in the appropriate schema.

    Args:
        property_id: Canonical property ID.
        url: Property URL.
        error: Error message.
        schema_version: "v1" or "v2".

    Returns:
        Failed property dict.
    """
    meta = {
        "canonical_id": property_id,
        "scrape_tier_used": "FAILED",
        "scrape_errors": [error],
        "carry_forward_used": False,
    }
    if schema_version == "v2":
        try:
            apartment_id = int(property_id)
        except (ValueError, TypeError):
            apartment_id = None
        return {
            "apartment_id": apartment_id,
            "proj_name": None,
            "address": None,
            "city": None,
            "state": None,
            "zip_code": None,
            "country": None,
            "phone": None,
            "email_address": None,
            "website": url,
            "pmc": None,
            "website_design": None,
            "concessions": None,
            "units": [],
            "_meta": meta,
        }
    return {
        "_meta": meta,
        "units": [],
        "Website": url,
    }


# ---------------------------------------------------------------------------
# V2 formatting helpers
# ---------------------------------------------------------------------------

import re as _re


def _normalize_beds(val: Any) -> int | None:
    """Convert bedroom value to integer. Studio -> 0, clamp [0, 7].

    Returns ``None`` when the source emitted nothing. Previously this
    defaulted to 0, which silently collapsed "studio confirmed" and
    "not extracted" into the same value — making it impossible to spot
    upstream parser gaps in the downstream data.
    """
    if val is None or val == "":
        return None
    s = str(val).strip().lower()
    if s in ("studio", "s"):
        return 0
    try:
        return max(0, min(int(float(s)), 7))
    except (ValueError, TypeError):
        return None


def _normalize_baths(val: Any) -> float | None:
    """Convert bathroom value to nearest 0.5 multiple, clamp [0, 10].

    Returns ``None`` when the source emitted nothing (same rationale as
    ``_normalize_beds``). Previously defaulted to 1.0.
    """
    if val is None or val == "":
        return None
    try:
        n = float(str(val).strip())
        return max(0.0, min(round(n * 2) / 2, 10.0))
    except (ValueError, TypeError):
        return None


def _format_zip(val: Any) -> str | None:
    """Extract first 5 digits from a ZIP code."""
    if val is None:
        return None
    s = str(val).strip()
    m = _re.search(r"\d{5}", s)
    if m:
        return m.group(0)
    digits = _re.sub(r"\D", "", s)
    return digits.zfill(5)[:5] if digits else None


def _format_rent(val: Any) -> float | None:
    """Clean rent value. Must be > 1 or None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if val > 1 else None
    s = str(val).strip().replace("$", "").replace(",", "")
    try:
        n = float(s)
        return n if n > 1 else None
    except (ValueError, TypeError):
        return None


def _format_area(val: Any) -> int:
    """Convert sqft to int. Keeps -1 as the "absent" sentinel.

    Sanity bounds: a real apartment floor-plan area is between 150 and 10,000
    sqft. Anything outside that is garbage (bedroom counts, floor numbers,
    truncated values like "070") and gets coerced to -1. Previously any
    positive integer was accepted, which is why the 2026-04-19 run had area
    values of 9, 12, 50, 70, 100, etc. passed through as "successful".
    """
    if val is None or val == -1:
        return -1
    try:
        n = int(float(str(val)))
    except (ValueError, TypeError):
        return -1
    if 150 <= n <= 10_000:
        return n
    return -1


def _format_date_str(val: Any) -> str | None:
    """Normalize date to YYYY-MM-DD. None if unparseable."""
    if val is None or val == "":
        return None
    s = str(val).strip()
    if _re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    if len(s) >= 10 and _re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _safe_int_gt1(val: Any) -> int | None:
    """Integer > 1 or None."""
    if val is None:
        return None
    try:
        n = int(float(str(val)))
        return n if n > 1 else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------


def _load_csv(
    csv_path: Path,
    limit: int | None = None,
    start_index: int = 0,
) -> list[dict[str, Any]]:
    """Load CSV rows with flexible column names.

    Args:
        csv_path: Path to the CSV file.
        limit: Max rows to load *after* applying ``start_index``.
        start_index: Zero-based row index to start from. Rows before this
            index are skipped. The resulting ``property_id`` fallback still
            uses the original row index so IDs stay stable across resumed
            runs.

    Returns:
        List of row dicts.
    """
    if start_index < 0:
        raise ValueError(f"start_index must be >= 0, got {start_index}")

    rows: list[dict[str, Any]] = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if i < start_index:
                continue
            if limit and len(rows) >= limit:
                break
            # Normalize column names
            normalized: dict[str, Any] = {}
            for k, v in row.items():
                normalized[k] = v
            # Ensure property_id and url exist
            if "property_id" not in normalized:
                normalized["property_id"] = (
                    normalized.get("Unique ID")
                    or normalized.get("Property ID")
                    or normalized.get("apartmentid")
                    or f"row_{i}"
                )
            if "url" not in normalized:
                normalized["url"] = normalized.get("Website") or normalized.get("website") or ""
            rows.append(normalized)
    return rows


def _merge_with_existing_properties(
    path: Path,
    new_properties: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge ``new_properties`` on top of any existing ``properties.json``.

    Keys on ``_meta.canonical_id``. New entries replace existing ones with
    the same canonical id; entries not touched by this run are preserved.
    This is what makes ``--start-index`` (and crash-resumes) additive
    instead of clobbering prior batches in the same run directory.

    Returns the merged list. Returns ``new_properties`` unchanged if the
    existing file is missing, empty, or malformed — never raises.
    """
    existing: list[dict[str, Any]] = []
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            if raw.strip():
                loaded = json.loads(raw)
                if isinstance(loaded, list):
                    existing = [r for r in loaded if isinstance(r, dict)]
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "Existing %s unreadable (%s) — overwriting with new batch only",
                path,
                exc,
            )
            return list(new_properties)

    if not existing:
        return list(new_properties)

    def _cid(rec: dict[str, Any]) -> str | None:
        meta = rec.get("_meta") or {}
        cid = meta.get("canonical_id")
        return str(cid) if cid else None

    new_by_cid: dict[str, dict[str, Any]] = {}
    new_without_cid: list[dict[str, Any]] = []
    for rec in new_properties:
        cid = _cid(rec)
        if cid:
            new_by_cid[cid] = rec
        else:
            new_without_cid.append(rec)

    merged: list[dict[str, Any]] = []
    seen_cids: set[str] = set()
    for rec in existing:
        cid = _cid(rec)
        if cid and cid in new_by_cid:
            merged.append(new_by_cid[cid])
            seen_cids.add(cid)
        else:
            merged.append(rec)

    for cid, rec in new_by_cid.items():
        if cid not in seen_cids:
            merged.append(rec)

    merged.extend(new_without_cid)

    log.info(
        "properties.json merge: existing=%d, new=%d, replaced=%d, total=%d",
        len(existing),
        len(new_properties),
        len(seen_cids),
        len(merged),
    )
    return merged


def _write_properties_incremental(path: Path, properties: list[dict[str, Any]]) -> None:
    """Write properties JSON.

    Args:
        path: Output file path.
        properties: Properties to write (already merged with prior contents
            by the caller — this writer is a plain overwrite).
    """
    try:
        path.write_text(
            json.dumps(properties, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as exc:
        log.warning("Failed to write incremental properties: %s", exc)


class _SimpleProfileStore:
    """Adapter around services.profile_store.ProfileStore.

    Provides ``get_profile(property_id)`` for back-compat with the dispatch
    sites that expected the old read-only shim, while delegating storage,
    bootstrap, drift detection, and post-extraction updates to the full
    self-learning service layer that daily_runner uses.

    This means Jugnu now:
      - loads real ScrapeProfile objects (ProfileMaturity, preferred_tier,
        known_endpoints, blocked_endpoints, etc.)
      - updates profiles after each scrape via update_profile_after_extraction
      - runs drift detection to demote HOT/WARM profiles that regressed

    The bootstrap path creates a COLD profile from URL-based PMS detection
    when a property is scraped for the first time — same as daily_runner.
    """

    def __init__(self, profiles_dir: Path) -> None:
        # Lazy imports so importing this module doesn't drag in the services
        # layer (and its deps) unless the profile loop is actually used.
        from services.profile_store import ProfileStore  # type: ignore[import-not-found]

        self._backing = ProfileStore(profiles_dir)

    def get_profile(self, property_id: str) -> Any:
        """Return a ScrapeProfile (not a plain dict). None if not found."""
        try:
            return self._backing.load(property_id)
        except Exception as exc:
            log.debug("profile load failed for %s: %s", property_id, exc)
            return None

    def bootstrap(self, property_id: str, meta: dict[str, Any], website: str) -> Any:
        """Create a COLD profile from CSV metadata + URL-based PMS detection.

        Builds the ScrapeProfile directly rather than using
        ``ProfileStore.bootstrap_from_meta`` because that helper references
        fields that drifted out of the current ``DomHints`` model. Keeps
        this path self-contained so Jugnu isn't blocked by upstream bugs.
        """
        try:
            from models.scrape_profile import (  # type: ignore[import-not-found]
                ApiHints,
                DomHints,
                NavigationConfig,
                ScrapeProfile,
                detect_platform,
            )

            platform = detect_platform(website) if website else None
            nav = NavigationConfig()
            if website:
                nav.entry_url = website
            api_hints = ApiHints()
            if platform:
                api_hints.api_provider = platform
            profile = ScrapeProfile(
                canonical_id=property_id,
                version=1,
                updated_by="BOOTSTRAP",
                navigation=nav,
                api_hints=api_hints,
                dom_hints=DomHints(),
            )
            self._backing.save(profile)
            return profile
        except Exception as exc:
            log.debug("profile bootstrap failed for %s: %s", property_id, exc)
            return None

    def save(self, profile: Any) -> None:
        try:
            self._backing.save(profile)
        except Exception as exc:
            log.warning("profile save failed: %s", exc)

    @property
    def backing(self) -> Any:
        """Raw ProfileStore — for APIs that expect the full service."""
        return self._backing


def main() -> int:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Jugnu integrated runner")
    parser.add_argument("--csv", type=Path, default=_MA_POC_ROOT / "config" / "properties.csv")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--proxy", type=str, default=None)
    parser.add_argument("--data-dir", type=Path, default=_MA_POC_ROOT / "data")
    parser.add_argument("--run-date", type=str, default=None)
    parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="Zero-based CSV row index to start scraping from (skips earlier rows).",
    )
    parser.add_argument(
        "--schema-version",
        choices=["v1", "v2"],
        default=None,
        help="Output schema version (default: env SCHEMA_VERSION or v1)",
    )
    args = parser.parse_args()

    schema_version = _resolve_schema_version(args)

    report = asyncio.run(
        run_jugnu(
            csv_path=args.csv,
            data_dir=args.data_dir,
            limit=args.limit,
            proxy=args.proxy,
            run_date=args.run_date,
            schema_version=schema_version,
            start_index=args.start_index,
        )
    )

    print(f"Run complete: {report['totals']['succeeded']}/{report['totals']['properties']} succeeded")
    return 0 if report["totals"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
