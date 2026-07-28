"""Probe whether a public property site exposes apartment-level identity.

Acceptance criteria (2026-07-28 unit-identity reachability probe):
* Read only ``probe-cohort-unit-identity-892.csv`` from the immutable
  2026-07-27 Canary worklist and execute bands A, B, C, then D.
* Traverse only public marketing, floor-plan, detail, availability-control,
  iframe, and observed public-portal surfaces in a property-isolated browser.
* Publish a positive finding only when a real apartment anchor and its own
  numeric rent appear in one public DOM/API row.  Never accept inferred IDs,
  plan IDs, availability copy, or plan-price ranges as proof.
* Persist one sanitized JSONL checkpoint per completed property with a GCS
  generation guard.  A failed or incomplete route walk is explicitly
  ``COULD_NOT_ESTABLISH``; it is never silently converted into absence.

The underlying browser explorer deliberately keeps response bodies and cookies in
memory only.  This wrapper persists the route and parser target necessary for
adapter work, not session state or a static move-in date.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage  # type: ignore[import-untyped]

from ma_poc.fetch.browser_pool import BrowserContextPool
from ma_poc.fetch.stealth import IdentityPool
from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.scripts.diagnostics.browser_endpoint_discovery import (
    BrowserEndpointProbeResult,
    _capture_browser_property,
    discovery_proxy_config,
    sanitized_xhr_path,
    without_resman_date_scope,
)
from ma_poc.scripts.diagnostics.cohort_endpoint_route_plan import (
    RoutePlanRecord,
    normalize_public_url,
)
from ma_poc.scripts.diagnostics.cohort_endpoint_route_plan import (
    make_route_record as make_discovery_route_record,
)

_WORKFLOW_VERSION = "unit-identity-reachability-probe-v1"
_BAND_ORDER = {
    "A_success_no_anchor": 0,
    "B_all_synthetic": 1,
    "C_resistant_plan_level": 2,
    "D_never_any_unit": 3,
}
_REQUIRED_WORKLIST_COLUMNS = frozenset(
    {"apartmentid", "band", "website", "verdict", "tier", "n_real", "n_syn", "ever_gold"}
)
_TRANSIENT_QUERY_KEYS = frozenset(
    {
        "date",
        "moveindate",
        "move_in_date",
        "refreshpricing",
        "refresh_pricing",
        "_gl",
        "gclid",
        "fbclid",
    }
)


class ReachabilityOutcome(StrEnum):
    """The only durable outcomes allowed by the unit-identity brief."""

    PUBLISHES_UNIT_IDENTITY = "PUBLISHES_UNIT_IDENTITY"
    NO_PUBLIC_UNIT_IDENTITY = "NO_PUBLIC_UNIT_IDENTITY"
    COULD_NOT_ESTABLISH = "COULD_NOT_ESTABLISH"


@dataclass(frozen=True, slots=True)
class ProbeWorkItem:
    """One validated immutable-cohort row and its historical quality context."""

    apartment_id: str
    band: str
    website: str
    verdict: str
    tier: str
    n_real: int
    n_syn: int
    ever_gold: bool


class HyperbrowserContextPool:
    """One disposable Hyperbrowser residential session per probed property.

    The browser explorer only needs ``acquire()``, ``release()``, and
    ``close()``.  This small adapter preserves its property-isolation contract
    while replacing the local browser/Bright route with a Hyperbrowser cloud
    browser.  Session IDs, CDP URLs, cookies, and rendered bodies stay only in
    process memory and are destroyed by ``release``.
    """

    def __init__(self) -> None:
        """Initialize an empty page-to-session ownership map."""
        self._sessions: dict[int, Any] = {}

    async def acquire(self, _identity: Any, *, proxy: Any = None) -> Any:
        """Create one Hyperbrowser render session and return its first page."""
        del proxy
        from ma_poc.fetch.hyperbrowser_backend import _HbSession

        session = _HbSession("render")
        page = await session.open()
        self._sessions[id(page)] = session
        return page

    async def release(self, page: Any) -> None:
        """Stop the Hyperbrowser session that owns ``page`` exactly once."""
        session = self._sessions.pop(id(page), None)
        if session is not None:
            await session.close()

    async def close(self) -> None:
        """Best-effort stop all still-owned sessions after a batch exception."""
        sessions = list(self._sessions.values())
        self._sessions.clear()
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    """Split a complete ``gs://bucket/object`` URI.

    Args:
        uri: Fully-qualified GCS URI.

    Returns:
        Bucket and object/prefix strings.

    Raises:
        ValueError: If ``uri`` is not a complete GCS URI.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise ValueError(f"not_a_gcs_uri:{uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def _int_value(raw: str, *, field: str, apartment_id: str) -> int:
    """Parse a non-negative worklist count with contextual validation errors."""
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"invalid_{field}:{apartment_id}:{raw!r}") from exc
    if value < 0:
        raise ValueError(f"negative_{field}:{apartment_id}:{raw!r}")
    return value


def work_item_from_row(row: dict[str, str]) -> ProbeWorkItem:
    """Validate and normalize one immutable reachability-worklist row.

    Args:
        row: CSV row from the 892-property worklist.

    Returns:
        A canonical, public-only work item.

    Raises:
        ValueError: If any required identity, band, or public website is invalid.
    """
    apartment_id = str(row.get("apartmentid") or "").strip()
    if not apartment_id:
        raise ValueError("missing_apartmentid")
    band = str(row.get("band") or "").strip()
    if band not in _BAND_ORDER:
        raise ValueError(f"unknown_band:{apartment_id}:{band!r}")
    website = normalize_public_url(str(row.get("website") or ""))
    if website is None:
        raise ValueError(f"invalid_website:{apartment_id}")
    return ProbeWorkItem(
        apartment_id=apartment_id,
        band=band,
        website=website,
        verdict=str(row.get("verdict") or "").strip(),
        tier=str(row.get("tier") or "").strip(),
        n_real=_int_value(str(row.get("n_real") or ""), field="n_real", apartment_id=apartment_id),
        n_syn=_int_value(str(row.get("n_syn") or ""), field="n_syn", apartment_id=apartment_id),
        ever_gold=str(row.get("ever_gold") or "").strip().lower() in {"1", "true", "yes"},
    )


def ordered_work_items(rows: list[dict[str, str]]) -> list[ProbeWorkItem]:
    """Return validated work items sorted A→B→C→D and stable within each band."""
    items = [work_item_from_row(row) for row in rows]
    indexed_items = sorted(
        enumerate(items), key=lambda indexed: (_BAND_ORDER[indexed[1].band], indexed[0])
    )
    return [item for _, item in indexed_items]


def band_batches(items: list[ProbeWorkItem]) -> list[list[ProbeWorkItem]]:
    """Group an already-selected worklist into non-empty A→B→C→D batches.

    Browser tasks inside a band may run concurrently, but a later band must
    not acquire a context while a higher-priority false-success band remains
    outstanding.  This makes the ten-hour cutoff informative rather than a
    sample accidentally dominated by easier plan-level properties.
    """
    return [[item for item in items if item.band == band] for band in _BAND_ORDER if any(
        item.band == band for item in items
    )]


def _load_worklist(client: storage.Client, uri: str) -> list[dict[str, str]]:
    """Load the exact immutable 892-property CSV from GCS.

    Raises:
        ValueError: If the CSV does not contain the brief's required schema.
    """
    bucket, object_name = _parse_gcs_uri(uri)
    rows = list(csv.DictReader(io.StringIO(client.bucket(bucket).blob(object_name).download_as_text())))
    columns = set(rows[0]) if rows else set()
    missing = sorted(_REQUIRED_WORKLIST_COLUMNS - columns)
    if missing:
        raise ValueError(f"worklist_missing_columns:{','.join(missing)}")
    return rows


def _profile_blob(client: storage.Client, prefix_uri: str, apartment_id: str) -> storage.Blob:
    """Return the canonical profile object without reading or mutating it."""
    bucket, prefix = _parse_gcs_uri(prefix_uri)
    return client.bucket(bucket).blob(f"{prefix.rstrip('/')}/{apartment_id}.json".lstrip("/"))


def _load_profile(client: storage.Client, prefix_uri: str, apartment_id: str) -> ScrapeProfile | None:
    """Best-effort load a profile so observed portal routes can seed traversal."""
    try:
        return ScrapeProfile.model_validate_json(_profile_blob(client, prefix_uri, apartment_id).download_as_bytes())
    except Exception:
        return None


def make_route_record(item: ProbeWorkItem, profile: ScrapeProfile | None) -> RoutePlanRecord:
    """Build a public route record from the exact worklist URL plus known public links."""
    return make_discovery_route_record(
        {"property_id": item.apartment_id, "url": item.website},
        profile,
    )


def _checkpoint_blob(client: storage.Client, prefix_uri: str, apartment_id: str) -> storage.Blob:
    """Return the one-record JSONL checkpoint object for a property."""
    bucket, prefix = _parse_gcs_uri(prefix_uri)
    return client.bucket(bucket).blob(f"{prefix.rstrip('/')}/properties/{apartment_id}.jsonl".lstrip("/"))


def _completed_ids_from_payloads(payloads: list[str]) -> set[str]:
    """Return every property with a valid durable checkpoint, including inconclusive ones."""
    completed: set[str] = set()
    for payload in payloads:
        for line in payload.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("workflow_version") != _WORKFLOW_VERSION:
                continue
            apartment_id = str(row.get("apartment_id") or "").strip()
            if apartment_id:
                completed.add(apartment_id)
    return completed


def _load_completed_ids(client: storage.Client, prefix_uri: str) -> set[str]:
    """Load this workflow's per-property checkpoints for safe resume semantics."""
    bucket, prefix = _parse_gcs_uri(prefix_uri)
    payloads: list[str] = []
    for blob in client.list_blobs(bucket, prefix=f"{prefix.rstrip('/')}/properties/"):
        if not blob.name.endswith(".jsonl"):
            continue
        try:
            payloads.append(blob.download_as_text())
        except Exception:
            continue
    return _completed_ids_from_payloads(payloads)


def _safe_public_url(url: str | None) -> str | None:
    """Remove runtime dates, tracking, and sensitive query values from persisted routes."""
    if not url:
        return None
    stripped = without_resman_date_scope(url)
    parsed = urlparse(stripped)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRANSIENT_QUERY_KEYS
    ]
    return urlunparse(parsed._replace(query=urlencode(query)))


def _numeric_rent(row: dict[str, Any]) -> int | float | None:
    """Return one already-normalized numeric rent from a strict proof row."""
    for key in ("market_rent_low", "asking_rent", "market_rent_high"):
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _sample_rows(result: BrowserEndpointProbeResult) -> list[dict[str, Any]]:
    """Return up to three unique strict rows, preferring direct XHR proof."""
    samples: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in [*result.strict_api_rows, *result.strict_dom_rows]:
        anchor = str(row.get("unit_number") or "").strip()
        if not anchor or anchor in seen or _numeric_rent(row) is None:
            continue
        seen.add(anchor)
        samples.append(row)
        if len(samples) == 3:
            break
    return samples


def _surfaces_traversed(result: BrowserEndpointProbeResult) -> list[str]:
    """Return every sanitized public URL the explorer recorded as visited/discovered."""
    surfaces: list[str] = []
    for raw in [
        *result.warm_urls_tried,
        *result.detail_urls_tried,
        *result.public_portal_links_observed,
    ]:
        sanitized = _safe_public_url(raw)
        if sanitized and sanitized not in surfaces:
            surfaces.append(sanitized)
    return surfaces


def _route_walk_complete(result: BrowserEndpointProbeResult) -> bool:
    """Return whether bounded traversal supports the expensive conclusion of absence.

    The browser explorer intentionally caps controls, detail links, and portal
    links.  Hitting a cap means there may be an unvisited surface, so the
    negative remains inconclusive.  The same holds for unresolved public
    blocks, an exhausted Unlocker allowance, or a non-settled SPA.
    """
    if result.error or result.blocked_public_paths or result.web_unlocker_budget_exhausted:
        return False
    if not result.networkidle_reached or result.navigation_levels_reached < 1:
        return False
    if result.controls_matched > result.controls_clicked:
        return False
    if len(result.detail_urls_tried) >= 3 or len(result.public_portal_links_observed) >= 4:
        return False
    return bool(_surfaces_traversed(result))


def reachability_outcome(result: BrowserEndpointProbeResult) -> ReachabilityOutcome:
    """Classify one browser result without treating incomplete traversal as absence."""
    if _sample_rows(result):
        return ReachabilityOutcome.PUBLISHES_UNIT_IDENTITY
    if _route_walk_complete(result):
        return ReachabilityOutcome.NO_PUBLIC_UNIT_IDENTITY
    return ReachabilityOutcome.COULD_NOT_ESTABLISH


def _blocked_reason(result: BrowserEndpointProbeResult, outcome: ReachabilityOutcome) -> str | None:
    """Explain only an inconclusive result using sanitized, actionable telemetry."""
    if outcome != ReachabilityOutcome.COULD_NOT_ESTABLISH:
        return None
    if result.error == "property-timeout":
        return "property-timeout"
    if result.web_unlocker_budget_exhausted:
        return "web-unlocker-budget-exhausted"
    if result.blocked_public_paths:
        return "public-route-blocked"
    if result.controls_matched > result.controls_clicked:
        return "availability-control-not-fully-traversed"
    if len(result.detail_urls_tried) >= 3 or len(result.public_portal_links_observed) >= 4:
        return "public-route-traversal-cap-reached"
    if not result.networkidle_reached:
        return "browser-never-settled"
    if result.navigation_levels_reached < 1:
        return "marketing-route-not-reached"
    return "route-walk-incomplete"


def _shape(result: BrowserEndpointProbeResult) -> str | None:
    """Map sanitized proof telemetry to the brief's public shape vocabulary."""
    if result.strict_api_rows:
        return "XHR_JSON"
    if result.strict_dom_rows:
        return "IFRAME" if result.frames_seen > 1 else "DOM_TABLE"
    return None


def _selector_or_json_path(result: BrowserEndpointProbeResult) -> str | None:
    """Record the parser location that established the strict proof, not raw page data."""
    if result.strict_api_rows:
        endpoint = sanitized_xhr_path(result.endpoint_url or "")
        return f"{endpoint or 'captured XHR'} -> parse_api_responses -> unit_number + market_rent_low"
    if result.strict_dom_rows:
        if result.frames_seen > 1:
            return "inventory iframe -> strict_listing_rows(unit_number + market_rent_low)"
        return "rendered public inventory row -> strict_listing_rows(unit_number + market_rent_low)"
    return None


def _why_pipeline_missed(item: ProbeWorkItem, result: BrowserEndpointProbeResult) -> str:
    """Turn a proven public route into one concise adapter-work category."""
    if result.strict_api_rows:
        return f"{item.tier}: availability XHR was not captured or parsed as a unit roster"
    if result.frames_seen > 1:
        return f"{item.tier}: public inventory iframe was not traversed"
    if result.public_portal_links_observed:
        return f"{item.tier}: marketing-to-public-portal handoff was not followed"
    if result.controls_clicked:
        return f"{item.tier}: availability control/modal was not opened in the prior run"
    if result.detail_urls_tried:
        return f"{item.tier}: unit roster was one public floor-plan/detail hop deeper"
    return f"{item.tier}: rendered public unit rows were not recognized as unit identity"


def reachability_record(
    item: ProbeWorkItem,
    result: BrowserEndpointProbeResult,
    *,
    direct_device_ip: bool = False,
    browser_backend: str = "bright",
) -> dict[str, Any]:
    """Build the exact sanitized per-property JSONL deliverable row.

    A one-unit property cannot provide three distinct anchors.  Such a record
    retains the single observed anchor and sets ``anchor_varies_across_units``
    false rather than inventing or duplicating an ID.
    """
    outcome = reachability_outcome(result)
    samples = _sample_rows(result)
    anchors = [str(row.get("unit_number") or "").strip() for row in samples]
    api_proof = bool(result.strict_api_rows)
    proof_url = (
        sanitized_xhr_path(result.endpoint_url or "")
        if api_proof
        else _safe_public_url(result.dom_proof_page_url or result.warm_page_url)
    )
    interaction_path = [f"goto {url}" for url in _surfaces_traversed(result)]
    if result.controls_clicked:
        interaction_path.append(f"open {result.controls_clicked} public availability control(s)")
    if result.frames_seen > 1:
        interaction_path.append("inspect public inventory iframe(s)")
    return {
        "workflow_version": _WORKFLOW_VERSION,
        "browser_network_mode": (
            "hyperbrowser_residential"
            if browser_backend == "hyperbrowser"
            else "direct_device_ip"
            if direct_device_ip
            else "bright_residential"
        ),
        "apartment_id": item.apartment_id,
        "band": item.band,
        "outcome": outcome.value,
        "proof_url": proof_url if outcome == ReachabilityOutcome.PUBLISHES_UNIT_IDENTITY else None,
        "interaction_path": interaction_path,
        "shape": _shape(result) if outcome == ReachabilityOutcome.PUBLISHES_UNIT_IDENTITY else None,
        "selector_or_json_path": (
            _selector_or_json_path(result)
            if outcome == ReachabilityOutcome.PUBLISHES_UNIT_IDENTITY
            else None
        ),
        "sample_anchors": anchors,
        "sample_rent_for_anchor": _numeric_rent(samples[0]) if samples else None,
        "anchor_varies_across_units": len(set(anchors)) >= 2,
        "surfaces_traversed": _surfaces_traversed(result),
        "why_pipeline_missed_it": (
            _why_pipeline_missed(item, result)
            if outcome == ReachabilityOutcome.PUBLISHES_UNIT_IDENTITY
            else None
        ),
        "blocked_reason": _blocked_reason(result, outcome),
        "historical_verdict": item.verdict,
        "historical_tier": item.tier,
        "historical_real_anchor_rows": item.n_real,
        "historical_synthetic_rows": item.n_syn,
        "historical_ever_gold": item.ever_gold,
        "browser_classification": result.classification.value,
        "observed_at": datetime.now(UTC).isoformat(),
    }


def _timeout_result() -> BrowserEndpointProbeResult:
    """Return the explicit retry/inconclusive evidence for a property watchdog expiry."""
    from ma_poc.services.endpoint_discovery_profiles import DiscoveryClassification

    return BrowserEndpointProbeResult(
        warm_status=None,
        classification=DiscoveryClassification.API_NOT_FOUND_YET,
        error="property-timeout",
    )


async def _probe_item(
    *,
    item: ProbeWorkItem,
    record: RoutePlanRecord,
    pool: Any,
    identities: IdentityPool,
    property_timeout_seconds: int,
    direct_device_ip: bool,
    browser_backend: str,
) -> BrowserEndpointProbeResult:
    """Run the public browser traversal in one property-isolated context.

    The underlying explorer releases the acquired page in its ``finally``
    block, including when this watchdog cancels it.
    """
    try:
        identity = identities.pick_chrome_only(sticky_key=item.apartment_id)
        proxy = (
            None
            if browser_backend == "hyperbrowser"
            else discovery_proxy_config(record, direct_device_ip=direct_device_ip)
        )
        page = await pool.acquire(identity, proxy=proxy)
        return await asyncio.wait_for(
            _capture_browser_property(
                pool=pool,
                identities=identities,
                record=record,
                page=page,
                direct_device_ip=direct_device_ip,
                # HB owns the public browser route.  Its discovery path must
                # not quietly fall through to a separate Bright endpoint
                # replay, which would muddy the evidence and spend paid proxy.
                allow_known_endpoint_replay=browser_backend != "hyperbrowser",
            ),
            timeout=property_timeout_seconds,
        )
    except TimeoutError:
        return _timeout_result()
    except Exception as exc:
        from ma_poc.services.endpoint_discovery_profiles import DiscoveryClassification

        return BrowserEndpointProbeResult(
            warm_status=None,
            classification=DiscoveryClassification.API_NOT_FOUND_YET,
            error=f"probe-error:{type(exc).__name__}",
        )


def _persist_checkpoint(client: storage.Client, prefix_uri: str, row: dict[str, Any]) -> str:
    """Write one immutable per-property JSONL checkpoint using generation zero.

    Raises:
        PreconditionFailed: If another worker already wrote this property.
    """
    apartment_id = str(row["apartment_id"])
    blob = _checkpoint_blob(client, prefix_uri, apartment_id)
    blob.upload_from_string(
        json.dumps(row, separators=(",", ":")) + "\n",
        content_type="application/x-ndjson",
        if_generation_match=0,
    )
    return f"gs://{blob.bucket.name}/{blob.name}"


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """Run a resumable batch of the exact 892-property reachability cohort."""
    os.environ["WEB_UNLOCKER_MAX_CALLS_PER_JOB"] = str(args.web_unlocker_max_calls)
    client = storage.Client(project=args.project)
    source_rows = await asyncio.to_thread(_load_worklist, client, args.worklist_gcs_uri)
    items = ordered_work_items(source_rows)
    if len(items) != 892:
        raise ValueError(f"unexpected_worklist_size:{len(items)}")
    if args.band:
        accepted_bands = set(args.band)
        items = [item for item in items if item.band in accepted_bands]
    completed_ids = set() if args.retry_completed else await asyncio.to_thread(
        _load_completed_ids, client, args.checkpoint_gcs_prefix
    )
    pending = [item for item in items if item.apartment_id not in completed_ids]
    selected = pending[: args.limit]
    pool: Any = (
        HyperbrowserContextPool()
        if args.browser_backend == "hyperbrowser"
        else BrowserContextPool(max_contexts=args.concurrency)
    )
    identities = IdentityPool()
    global_gate = asyncio.Semaphore(args.concurrency)
    host_gates: dict[str, asyncio.Semaphore] = {}

    async def _one(item: ProbeWorkItem) -> dict[str, Any]:
        """Load route hints, serialize one host, probe, and durably checkpoint it."""
        profile = await asyncio.to_thread(_load_profile, client, args.profile_gcs_prefix, item.apartment_id)
        route_record = make_route_record(item, profile)
        host = urlparse(route_record.source_url).netloc.lower()
        host_gate = host_gates.setdefault(host, asyncio.Semaphore(args.per_host_concurrency))
        async with global_gate, host_gate:
            result = await _probe_item(
                item=item,
                record=route_record,
                pool=pool,
                identities=identities,
                property_timeout_seconds=args.property_timeout_seconds,
                direct_device_ip=args.direct_device_ip,
                browser_backend=args.browser_backend,
            )
        row = reachability_record(
            item,
            result,
            direct_device_ip=args.direct_device_ip,
            browser_backend=args.browser_backend,
        )
        try:
            row["checkpoint"] = await asyncio.to_thread(
                _persist_checkpoint, client, args.checkpoint_gcs_prefix, row
            )
        except PreconditionFailed:
            row["checkpoint"] = "already-written-by-concurrent-worker"
        return row

    try:
        rows: list[dict[str, Any]] = []
        for batch in band_batches(selected):
            rows.extend(await asyncio.gather(*(_one(item) for item in batch)))
    finally:
        await pool.close()
    outcomes = Counter(str(row["outcome"]) for row in rows)
    return {
        "workflow_version": _WORKFLOW_VERSION,
        "worklist_size": len(items),
        "previously_completed": len(items) - len(pending),
        "selected": len(selected),
        "completed": len(rows),
        "remaining_after_batch": len(pending) - len(selected),
        "outcomes": dict(sorted(outcomes.items())),
        "checkpoint_prefix": args.checkpoint_gcs_prefix,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse a bounded, resumable public-only reachability probe command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--worklist-gcs-uri",
        default="gs://jugnu-canary/property-list/probe-cohort-unit-identity-892.csv",
    )
    parser.add_argument("--profile-gcs-prefix", default="gs://jugnu-canary/profiles/")
    parser.add_argument(
        "--checkpoint-gcs-prefix",
        default="gs://jugnu-canary/investigations/2026-07-28-unit-identity-reachability-probe/",
    )
    parser.add_argument("--project", default="jugnu-494013")
    parser.add_argument("--limit", type=int, default=892)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--per-host-concurrency", type=int, default=1)
    parser.add_argument("--property-timeout-seconds", type=int, default=300)
    parser.add_argument("--web-unlocker-max-calls", type=int, default=50)
    parser.add_argument(
        "--browser-backend",
        choices=("bright", "hyperbrowser", "direct"),
        default="bright",
        help="Property-isolated render backend; Hyperbrowser is a residential cloud browser.",
    )
    parser.add_argument("--band", action="append", choices=sorted(_BAND_ORDER), default=[])
    parser.add_argument("--retry-completed", action="store_true")
    parser.add_argument("--direct-device-ip", action="store_true")
    args = parser.parse_args(argv)
    if args.direct_device_ip:
        if args.browser_backend not in {"bright", "direct"}:
            parser.error("--direct-device-ip cannot be combined with --browser-backend hyperbrowser")
        args.browser_backend = "direct"
    elif args.browser_backend == "direct":
        args.direct_device_ip = True
    if (
        args.limit < 1
        or args.concurrency < 1
        or args.per_host_concurrency < 1
        or args.property_timeout_seconds < 1
        or args.web_unlocker_max_calls < 0
    ):
        parser.error("limits, concurrency, and timeout must be positive; unlocker cap cannot be negative")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run the probe and print a compact machine-readable summary."""
    print(json.dumps(asyncio.run(run(parse_args(argv))), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
