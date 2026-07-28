"""Browser-assisted reconciliation of public inventory against one Jugnu run.

Acceptance criteria:
* Read the selected immutable Jugnu run outputs without launching Jugnu.
* Open each public property site in an isolated local browser context on the
  device's direct outbound IP; never share cookies or browser state.
* Record every concrete browser-observed unit ID, rent, and area available on
  the explored public inventory surface, then compare it with the run output.
* Treat a browser-observed missing unit or missing/different rent or area as a
  discrepancy.  Never infer a unit from a floor plan or synthetic identifier.
* Do not claim full inventory completeness without an operator-visible count
  or an independently complete public response; record scope as unproven.
* Write resumable, durable GCS checkpoints with public URLs and public listing
  fields only.  Do not persist profiles, cookies, response bodies, or dates.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from google.cloud import storage  # type: ignore[import-untyped]

from ma_poc.fetch.browser_pool import BrowserContextPool
from ma_poc.fetch.proxy.base import ProxyConfig, ProxyTier
from ma_poc.fetch.stealth import IdentityPool
from ma_poc.pms.adapters._parsing import money_to_int
from ma_poc.scripts.diagnostics.browser_endpoint_discovery import (
    BrowserEndpointProbeResult,
    _capture_browser_property,
)
from ma_poc.scripts.diagnostics.cohort_endpoint_route_plan import (
    DiscoveryRoute,
    RoutePlanRecord,
    _parse_gcs_uri,
    normalize_public_url,
)
from ma_poc.validation.unit_completeness import normalise_unit_key

_WORKFLOW_VERSION = "browser-inventory-completeness-v2"
_DEFAULT_RUN_GCS_URI = "gs://jugnu-canary/runs/2026-07-27-full-0d54ca7/"
_DEFAULT_CHECKPOINT_GCS_PREFIX = (
    "gs://jugnu-canary/investigations/2026-07-27-browser-inventory-completeness/"
)


@dataclass(frozen=True, slots=True)
class InventoryTarget:
    """One public property and its immutable run-output unit rows."""

    canonical_id: str
    name: str
    website: str
    verdict: str
    platform: str
    units: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ComparableUnit:
    """One public unit record reduced to the fields compared by this audit."""

    unit_key: str
    unit_id: str
    rent: int | None
    area: int | None

    def as_dict(self) -> dict[str, Any]:
        """Return a stable, public-safe checkpoint representation."""
        return {
            "unit_key": self.unit_key,
            "unit_id": self.unit_id,
            "rent": self.rent,
            "area": self.area,
        }


def _as_int(value: Any) -> int | None:
    """Return a positive integer from a scalar field, otherwise ``None``."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 0 else None
    return money_to_int(str(value))


def _first_value(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first non-empty value among *keys* in a listing row."""
    for key in keys:
        value = row.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def comparable_unit(row: dict[str, Any]) -> ComparableUnit | None:
    """Normalize one run or browser row without allowing plan-row identity.

    Args:
        row: Source listing row in a Jugnu or browser-parser field shape.

    Returns:
        A conservative normalized unit or ``None`` when the row lacks a real
        unit identity or is explicitly marked as a floor-plan record.
    """
    quality = str(row.get("data_quality_flag") or "").upper()
    tier = str(row.get("extraction_tier") or "").upper()
    if bool(row.get("is_floor_plan_level")) or "PLAN_LEVEL" in quality or "PLAN_LEVEL" in tier:
        return None
    raw_id = _first_value(
        row,
        (
            "unit_id",
            "unit_number",
            "unit_name",
            "apartment_number",
            "apartment",
        ),
    )
    key = normalise_unit_key(raw_id)
    if key is None:
        return None
    rent = _as_int(
        _first_value(
            row,
            (
                "rent_low",
                "market_rent_low",
                "asking_rent",
                "rent",
                "price",
                "rent_range",
            ),
        )
    )
    area = _as_int(_first_value(row, ("area", "sqft", "square_feet", "squareFootage")))
    return ComparableUnit(unit_key=key, unit_id=str(raw_id).strip(), rent=rent, area=area)


def merge_comparable_units(rows: Iterable[dict[str, Any]]) -> dict[str, ComparableUnit]:
    """Merge duplicate representations of public units without fabricating fields."""
    merged: dict[str, ComparableUnit] = {}
    for row in rows:
        item = comparable_unit(row)
        if item is None:
            continue
        prior = merged.get(item.unit_key)
        if prior is None:
            merged[item.unit_key] = item
            continue
        merged[item.unit_key] = ComparableUnit(
            unit_key=item.unit_key,
            unit_id=prior.unit_id or item.unit_id,
            rent=prior.rent if prior.rent is not None else item.rent,
            area=prior.area if prior.area is not None else item.area,
        )
    return merged


def compare_inventory(
    captured_rows: Iterable[dict[str, Any]], observed_rows: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Compare exact public unit IDs plus published rent and area fields.

    A field is checked only where the browser exposed it.  This avoids falsely
    calling a source deficient because an operator omitted area from the unit
    card, while still exposing when the production output missed a field that
    the current public inventory does display.
    """
    captured = merge_comparable_units(captured_rows)
    observed = merge_comparable_units(observed_rows)
    missing = sorted(set(observed) - set(captured))
    extra = sorted(set(captured) - set(observed))
    field_mismatches: list[dict[str, Any]] = []
    for key in sorted(set(captured) & set(observed)):
        output = captured[key]
        browser = observed[key]
        for field in ("rent", "area"):
            expected = getattr(browser, field)
            actual = getattr(output, field)
            if expected is not None and actual != expected:
                field_mismatches.append(
                    {
                        "unit_key": key,
                        "unit_id": browser.unit_id,
                        "field": field,
                        "browser": expected,
                        "run": actual,
                    }
                )
    return {
        "captured_units": [captured[key].as_dict() for key in sorted(captured)],
        "observed_units": [observed[key].as_dict() for key in sorted(observed)],
        "missing_observed_unit_keys": missing,
        "captured_not_observed_unit_keys": extra,
        "field_mismatches": field_mismatches,
    }


def _target_from_property(row: dict[str, Any]) -> InventoryTarget | None:
    """Build one audit target from a single immutable Jugnu property output."""
    meta_raw = row.get("_meta")
    meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
    canonical_id = str(meta.get("canonical_id") or row.get("apartment_id") or "").strip()
    website = normalize_public_url(str(row.get("website") or ""))
    units = row.get("units")
    if not canonical_id or website is None or not isinstance(units, list):
        return None
    provenance_raw = meta.get("provenance")
    provenance: dict[str, Any] = (
        provenance_raw if isinstance(provenance_raw, dict) else {}
    )
    return InventoryTarget(
        canonical_id=canonical_id,
        name=str(row.get("proj_name") or canonical_id),
        website=website,
        verdict=str(meta.get("verdict") or "UNKNOWN"),
        platform=str(provenance.get("detected_pms") or "unknown").lower(),
        units=tuple(item for item in units if isinstance(item, dict)),
    )


def load_run_targets(client: storage.Client, run_gcs_uri: str) -> list[InventoryTarget]:
    """Load and de-duplicate public property outputs from a completed Jugnu run."""
    bucket_name, prefix = _parse_gcs_uri(run_gcs_uri)
    targets: dict[str, InventoryTarget] = {}
    for blob in client.list_blobs(bucket_name, prefix=prefix.rstrip("/") + "/"):
        if not blob.name.endswith("/properties.json"):
            continue
        try:
            payload = json.loads(blob.download_as_bytes())
        except Exception:
            continue
        if not isinstance(payload, list):
            continue
        for row in payload:
            if not isinstance(row, dict):
                continue
            target = _target_from_property(row)
            if target is not None:
                targets.setdefault(target.canonical_id, target)
    return list(targets.values())


def roster_date_from_run_uri(run_gcs_uri: str) -> date | None:
    """Infer the immutable run date from a standard Jugnu ``runs/YYYY-MM-DD`` URI."""
    match = re.search(r"/runs/(\d{4}-\d{2}-\d{2})(?:[-/]|$)", run_gcs_uri)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def completed_ids_from_payloads(payloads: Iterable[str]) -> set[str]:
    """Return unique canonical IDs in prior durable batch checkpoints."""
    completed: set[str] = set()
    for payload in payloads:
        for line in payload.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and str(row.get("workflow_version") or "") == _WORKFLOW_VERSION:
                canonical_id = str(row.get("canonical_id") or "").strip()
                if canonical_id:
                    completed.add(canonical_id)
    return completed


def load_completed_ids(client: storage.Client, checkpoint_gcs_prefix: str) -> set[str]:
    """Load prior inventory-validation checkpoints, ignoring unreadable blobs."""
    bucket_name, prefix = _parse_gcs_uri(checkpoint_gcs_prefix)
    payloads: list[str] = []
    for blob in client.list_blobs(bucket_name, prefix=prefix.rstrip("/") + "/batch-"):
        try:
            payloads.append(blob.download_as_text())
        except Exception:
            continue
    return completed_ids_from_payloads(payloads)


def select_targets(
    targets: Iterable[InventoryTarget],
    completed_ids: set[str],
    *,
    limit: int,
    seed: str,
    verdicts: set[str],
    include_empty_run_output: bool = False,
    stratified: bool = False,
) -> list[InventoryTarget]:
    """Select a deterministic random sample after filtering completed rows.

    Empty-output properties are useful negative-path diagnostics but cannot
    validate row-level ID/rent/area coverage.  They are excluded by default so
    the 500-property denominator measures actual emitted unit inventory.
    """
    eligible = [
        target
        for target in targets
        if target.canonical_id not in completed_ids
        and (not verdicts or target.verdict in verdicts)
        and (include_empty_run_output or bool(merge_comparable_units(target.units)))
    ]
    def sort_key(target: InventoryTarget) -> str:
        """Return the deterministic sample ordering key for one target."""
        return hashlib.sha256(f"{seed}|{target.canonical_id}".encode()).hexdigest()

    eligible.sort(key=sort_key)
    if not stratified:
        return eligible[:limit]

    # A second validation pass should not become 500 copies of the dominant
    # platform/outcome. Round-robin across the observed platform + run-verdict
    # cohorts, with deterministic sampling inside each cohort. Smaller cohorts
    # are deliberately represented before the remaining capacity is consumed.
    buckets: dict[tuple[str, str], list[InventoryTarget]] = {}
    for target in eligible:
        buckets.setdefault((target.platform, target.verdict), []).append(target)
    selected: list[InventoryTarget] = []
    while len(selected) < limit:
        added = False
        for key in sorted(buckets):
            bucket = buckets[key]
            if not bucket:
                continue
            selected.append(bucket.pop(0))
            added = True
            if len(selected) == limit:
                break
        if not added:
            break
    return selected


def _status_for(
    *,
    browser_classification: str,
    comparison: dict[str, Any],
    api_scope_unverified: bool,
) -> str:
    """Classify comparison evidence without falsely asserting exhaustiveness."""
    if browser_classification == "ACCESS_BLOCKED":
        return "ACCESS_BLOCKED"
    if api_scope_unverified:
        return "API_SCOPE_UNVERIFIED"
    if not comparison["observed_units"]:
        return "NO_CURRENT_UNIT_ROSTER"
    if comparison["missing_observed_unit_keys"]:
        return "BROWSER_OBSERVED_GAP"
    if comparison["field_mismatches"]:
        return "FIELD_MISMATCH"
    if comparison["captured_not_observed_unit_keys"]:
        return "OUTPUT_EXTRA_OR_SCOPE_PARTIAL"
    return "OBSERVED_MATCH_SCOPE_UNPROVEN"


def current_roster_rows(
    probe: BrowserEndpointProbeResult,
) -> tuple[tuple[dict[str, Any], ...], bool]:
    """Return only date-scoped rendered rows that are safe for comparison.

    Endpoint discovery intentionally captures every qualifying XHR reached
    during a public journey.  Those payloads can include future inventory,
    stale cache entries, or units outside the move-in-date selection currently
    rendered to the renter.  They are useful discovery evidence, but they are
    not a complete-inventory oracle.  A completeness comparison may therefore
    use only strict DOM rows from the final rendered availability surface.

    Args:
        probe: One isolated browser discovery result.

    Returns:
        The rendered strict rows and whether non-rendered API rows were held
        out as scope-unverified evidence.
    """
    dom_rows = tuple(probe.strict_dom_rows)
    return dom_rows, bool(probe.strict_api_rows) and not dom_rows


async def validate_target(
    *,
    pool: BrowserContextPool,
    identities: IdentityPool,
    target: InventoryTarget,
    property_timeout_seconds: int,
    roster_date: date | None,
) -> dict[str, Any]:
    """Render one site locally and compare its public unit inventory to output."""
    record = RoutePlanRecord(
        canonical_id=target.canonical_id,
        source_url=target.website,
        public_url_candidates=(target.website,),
        detected_platform=target.platform,
        known_endpoint_count=0,
        route=DiscoveryRoute.GENERIC_BROWSER_XHR_DISCOVERY,
    )
    identity = identities.pick_chrome_only(sticky_key=target.canonical_id)
    page = await pool.acquire(identity, proxy=ProxyConfig(tier=ProxyTier.DIRECT))
    try:
        probe = await asyncio.wait_for(
            _capture_browser_property(
                pool=pool,
                identities=identities,
                record=record,
                page=page,
                direct_device_ip=True,
                roster_date=roster_date,
            ),
            timeout=property_timeout_seconds,
        )
    except TimeoutError:
        return {
            "workflow_version": _WORKFLOW_VERSION,
            "canonical_id": target.canonical_id,
            "property_name": target.name,
            "marketing_url": target.website,
            "run_verdict": target.verdict,
            "browser_network_mode": "direct_device_ip",
            "status": "TIMEOUT",
            "error": "property-timeout",
            "observed_at": datetime.now(UTC).isoformat(),
        }
    finally:
        await pool.release(page)

    observed_rows, api_scope_unverified = current_roster_rows(probe)
    comparison = compare_inventory(target.units, observed_rows)
    status = _status_for(
        browser_classification=probe.classification.value,
        comparison=comparison,
        api_scope_unverified=api_scope_unverified,
    )
    return {
        "workflow_version": _WORKFLOW_VERSION,
        "canonical_id": target.canonical_id,
        "property_name": target.name,
        "marketing_url": target.website,
        "run_verdict": target.verdict,
        "run_platform": target.platform,
        "browser_network_mode": "direct_device_ip",
        "status": status,
        "browser_classification": probe.classification.value,
        "browser_warm_page_url": probe.warm_page_url,
        "browser_unit_page_url": probe.dom_proof_page_url,
        "observed_scope": probe.dom_roster_scope,
        "roster_date_mode": "run_date" if roster_date is not None else "unproven",
        "run_unit_count": len(comparison["captured_units"]),
        "browser_unit_count": len(comparison["observed_units"]),
        "browser_dom_strict_row_count": len(probe.strict_dom_rows),
        "browser_api_rows_held_scope_unverified": len(probe.strict_api_rows),
        **comparison,
        "controls_clicked": probe.controls_clicked,
        "frames_seen": probe.frames_seen,
        "xhr_total_seen": probe.xhr_total_seen,
        "web_unlocker_used": bool(probe.web_unlocker_attempted_paths),
        "web_unlocker_attempted_paths": list(probe.web_unlocker_attempted_paths),
        "error": probe.error,
        "observed_at": datetime.now(UTC).isoformat(),
    }


def write_checkpoint(client: storage.Client, prefix_uri: str, rows: list[dict[str, Any]]) -> str:
    """Atomically write one durable inventory-validation batch checkpoint."""
    bucket_name, prefix = _parse_gcs_uri(prefix_uri)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256("|".join(row["canonical_id"] for row in rows).encode()).hexdigest()[:12]
    name = f"{prefix.rstrip('/')}/batch-{stamp}-{digest}.jsonl".lstrip("/")
    client.bucket(bucket_name).blob(name).upload_from_string(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        content_type="application/x-ndjson",
        if_generation_match=0,
    )
    return f"gs://{bucket_name}/{name}"


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """Run a bounded, resumable direct-device browser completeness audit."""
    os.environ["WEB_UNLOCKER_MAX_CALLS_PER_JOB"] = str(args.web_unlocker_max_calls)
    client = storage.Client(project=args.project)
    targets = await asyncio.to_thread(load_run_targets, client, args.run_gcs_uri)
    completed = await asyncio.to_thread(load_completed_ids, client, args.checkpoint_gcs_prefix)
    selected = select_targets(
        targets,
        completed,
        limit=args.limit,
        seed=args.seed,
        verdicts=set() if args.all_verdicts else set(args.verdict),
        include_empty_run_output=args.include_empty_run_output,
        stratified=args.stratified,
    )
    roster_date = (
        date.fromisoformat(args.roster_date)
        if args.roster_date is not None
        else roster_date_from_run_uri(args.run_gcs_uri)
    )
    pool = BrowserContextPool(max_contexts=args.concurrency)
    identities = IdentityPool()
    all_rows: list[dict[str, Any]] = []
    checkpoints: list[str] = []
    try:
        for start in range(0, len(selected), args.batch_size):
            batch = selected[start : start + args.batch_size]
            rows = await asyncio.gather(
                *(
                    validate_target(
                        pool=pool,
                        identities=identities,
                        target=target,
                        property_timeout_seconds=args.property_timeout_seconds,
                        roster_date=roster_date,
                    )
                    for target in batch
                )
            )
            checkpoints.append(await asyncio.to_thread(write_checkpoint, client, args.checkpoint_gcs_prefix, rows))
            all_rows.extend(rows)
    finally:
        await pool.close()
    statuses = Counter(str(row["status"]) for row in all_rows)
    return {
        "workflow_version": _WORKFLOW_VERSION,
        "run_gcs_uri": args.run_gcs_uri,
        "roster_date_mode": "run_date" if roster_date is not None else "unproven",
        "run_targets": len(targets),
        "previously_completed": len(completed),
        "selection_strategy": "platform_verdict_stratified" if args.stratified else "deterministic_random",
        "selected": len(selected),
        "completed": len(all_rows),
        "status_counts": dict(sorted(statuses.items())),
        "checkpoints": checkpoints,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse local browser-inventory validation arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-gcs-uri", default=_DEFAULT_RUN_GCS_URI)
    parser.add_argument("--checkpoint-gcs-prefix", default=_DEFAULT_CHECKPOINT_GCS_PREFIX)
    parser.add_argument("--project", default="jugnu-494013")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--property-timeout-seconds", type=int, default=180)
    parser.add_argument("--web-unlocker-max-calls", type=int, default=25)
    parser.add_argument(
        "--roster-date",
        default=None,
        help="Optional YYYY-MM-DD public move-in date; default infers it from --run-gcs-uri.",
    )
    parser.add_argument("--seed", default="browser-completeness-v1")
    parser.add_argument(
        "--verdict",
        action="append",
        default=["SUCCESS"],
        help="Run verdict to sample; repeat for multiple values. Default: SUCCESS.",
    )
    parser.add_argument(
        "--all-verdicts",
        action="store_true",
        help="Include every immutable canary verdict rather than only SUCCESS.",
    )
    parser.add_argument(
        "--stratified",
        action="store_true",
        help="Round-robin across platform and immutable run-verdict cohorts.",
    )
    parser.add_argument(
        "--include-empty-run-output",
        action="store_true",
        help="Include zero-unit outputs for negative-path checks; excluded from row-coverage sampling by default.",
    )
    args = parser.parse_args(argv)
    if min(args.limit, args.batch_size, args.concurrency, args.property_timeout_seconds) < 1:
        parser.error("limit, batch size, concurrency, and timeout must be positive")
    if args.web_unlocker_max_calls < 0:
        parser.error("web unlocker cap cannot be negative")
    if args.roster_date is not None:
        try:
            date.fromisoformat(args.roster_date)
        except ValueError:
            parser.error("roster-date must be YYYY-MM-DD")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run the local audit and print only its durable summary."""
    print(json.dumps(asyncio.run(run(parse_args(argv))), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
