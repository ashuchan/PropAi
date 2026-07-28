"""Resumable ProspectPortal warm-page / unit-endpoint discovery canary.

Acceptance criteria (2026-07-26 endpoint-discovery recovery):
* Read only the explicitly supplied cohort; never fall back to the 1,127 list.
* Replay public warm-page availability requests through property-sticky Bright
  Data residential sessions, with bounded concurrent properties.
* Classify a direct endpoint as verified only from a real unit id plus numeric
  rent in the same parsed response row; log visible plan-level pricing
  separately so it cannot be confused with a unit result.
* Persist only verified endpoint profiles with a GCS generation guard; write a
  durable, cookie-free checkpoint for every completed attempt.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from google.cloud import storage  # type: ignore[import-untyped]  # noqa: E402

from ma_poc.models.scrape_profile import ScrapeProfile  # noqa: E402
from ma_poc.pms.adapters._prospectportal_warm_replay import (  # noqa: E402
    ProspectPortalDomRevalidationResult,
    ProspectPortalWarmReplayRequest,
    ProspectPortalWarmReplayResult,
    replay_and_revalidate_with_residential_session,
)
from ma_poc.pms.adapters.entrata import (  # noqa: E402
    find_entrata_pp_plan_links,
    parse_entrata_pp_unit_cards,
    parse_entrata_prospectportal_html,
    parse_prospectportal_unit_spaces,
)
from ma_poc.services.endpoint_discovery_profiles import (  # noqa: E402
    DiscoveryClassification,
    DiscoveryEvidence,
    persist_generation_guarded,
)

log = logging.getLogger("unit_endpoint_discovery")


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    """Split one ``gs://bucket/object`` URI without importing legacy storage."""
    parsed = urlparse(uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise ValueError(f"not_a_gcs_uri:{uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def _profile_blob(
    client: storage.Client, profile_prefix: str, canonical_id: str
) -> storage.Blob:
    """Return one profile blob under a ``gs://bucket/prefix`` namespace."""
    bucket_name, prefix = _parse_gcs_uri(profile_prefix)
    object_name = f"{prefix.rstrip('/')}/{canonical_id}.json".lstrip("/")
    return client.bucket(bucket_name).blob(object_name)


def _load_cohort(client: storage.Client, cohort_uri: str) -> list[dict[str, str]]:
    """Load the exact cohort CSV from GCS, retaining its declared row order."""
    bucket_name, object_name = _parse_gcs_uri(cohort_uri)
    text = client.bucket(bucket_name).blob(object_name).download_as_text()
    return list(csv.DictReader(io.StringIO(text)))


def _public_warm_candidate(profile: ScrapeProfile) -> str | None:
    """Return the first public ``/conventional/`` warm path the profile knows.

    Entrata ProspectPortal can be served on either its ``prospectportal.com``
    hostname or a property vanity domain. Profiles can also keep the public
    portal in ``availability_links`` while their winner remains a marketing
    floor-plan URL, so both locations must be examined.
    """
    candidates = [profile.navigation.winning_page_url or ""]
    candidates.extend(profile.navigation.availability_links or [])
    for candidate in candidates:
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and "/conventional" in parsed.path.lower():
            return candidate
    return None


def _has_verified_dynamic_vus(profile: ScrapeProfile) -> bool:
    """Return whether the profile already holds a reusable VUS template."""
    return any(
        "action=view_unit_spaces" in endpoint.url_pattern
        and "{floorplan_id}" in endpoint.url_pattern
        and "{move_in_date}" in endpoint.url_pattern
        for endpoint in profile.api_hints.known_endpoints
    )


def _classify_replay(
    result: ProspectPortalWarmReplayResult,
    dom: ProspectPortalDomRevalidationResult | None = None,
) -> DiscoveryClassification:
    """Map a completed replay to a conservative evidence classification."""
    if result.verified:
        return DiscoveryClassification.API_VERIFIED
    if dom is not None and dom.verified:
        return DiscoveryClassification.SSR_DOM_ONLY
    statuses = [status for status in result.endpoint_statuses if status is not None]
    if result.warm_status in {401, 403, 429, 503} or (
        statuses and all(status in {401, 403, 429, 503} for status in statuses)
    ):
        return DiscoveryClassification.ACCESS_BLOCKED
    return DiscoveryClassification.API_NOT_FOUND_YET


def _checkpoint_row(
    canonical_id: str,
    source_url: str,
    warm_url: str,
    result: ProspectPortalWarmReplayResult,
    classification: DiscoveryClassification,
    persistence: str,
    dom: ProspectPortalDomRevalidationResult | None = None,
    unlocker_attempted: bool = False,
    unlocker_warm_status: int | None = None,
    unlocker_template_found: bool = False,
) -> dict[str, Any]:
    """Build a durable observation without raw content, dates, or cookies."""
    return {
        "canonical_id": canonical_id,
        "source_url": source_url,
        "warm_page_url": warm_url,
        "classification": classification.value,
        "warm_status": result.warm_status,
        "floorplans_discovered": result.floorplans_discovered,
        "endpoint_statuses": list(result.endpoint_statuses),
        "strict_unit_rent_rows": len(result.verified_rows),
        "strict_dom_unit_rent_rows": len(dom.verified_rows) if dom else 0,
        "dom_plan_pages_discovered": dom.plan_pages_discovered if dom else 0,
        "dom_plan_page_statuses": list(dom.plan_page_statuses) if dom else [],
        "dom_unit_page_url": dom.unit_page_url if dom else None,
        "public_plan_pricing": {
            "status": result.public_plan_pricing.status,
            "plans_observed": result.public_plan_pricing.plans_observed,
            "plans_with_numeric_price": result.public_plan_pricing.plans_with_numeric_price,
            "price_low": result.public_plan_pricing.price_low,
            "price_high": result.public_plan_pricing.price_high,
        },
        # Endpoint templates are stored only after strict verification.
        "endpoint_template": result.discovered_endpoint_template
        if classification == DiscoveryClassification.API_VERIFIED
        else None,
        "profile_persistence": persistence,
        "unlocker_attempted": unlocker_attempted,
        "unlocker_warm_status": unlocker_warm_status,
        "unlocker_template_found": unlocker_template_found,
        "error": result.error,
        "observed_at": datetime.now(UTC).isoformat(),
    }


async def _unlocker_assist(warm_url: str) -> tuple[int | None, bool]:
    """Use Web Unlocker only to inspect a blocked public warm page.

    It deliberately does not treat an Unlocker response as a verified endpoint:
    strict endpoint proof still requires the property-sticky residential replay.
    """
    from ma_poc.pms.adapters._probe import web_unlocker_get
    from ma_poc.pms.adapters._prospectportal_warm_replay import (
        discover_endpoint_template,
    )

    response = await asyncio.to_thread(web_unlocker_get, warm_url, 120)
    status = int(getattr(response, "status_code", 0) or 0) or None
    body = str(getattr(response, "text", "") or "")
    return status, bool(status == 200 and discover_endpoint_template(warm_url, body))


async def _run_candidate(
    *,
    client: storage.Client,
    profile_prefix: str,
    cohort_row: dict[str, str],
    profile: ScrapeProfile,
    warm_url: str,
    commit_profiles: bool,
    use_web_unlocker: bool,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """Replay one warm profile and optionally generation-guard its API proof."""
    canonical_id = profile.canonical_id
    async with semaphore:
        replay, dom = await replay_and_revalidate_with_residential_session(
            ProspectPortalWarmReplayRequest(
                property_id=canonical_id,
                warm_page_url=warm_url,
            ),
            parser=parse_prospectportal_unit_spaces,
            plan_parser=parse_entrata_prospectportal_html,
            plan_link_parser=find_entrata_pp_plan_links,
            dom_unit_parser=parse_entrata_pp_unit_cards,
        )
    classification = _classify_replay(replay, dom)
    persistence = "checkpoint_only"
    unlocker_attempted = False
    unlocker_warm_status: int | None = None
    unlocker_template_found = False
    # This is a paid discovery fallback only after the property-sticky
    # residential session is blocked. It never becomes endpoint proof by itself.
    if use_web_unlocker and classification == DiscoveryClassification.ACCESS_BLOCKED:
        unlocker_attempted = True
        unlocker_warm_status, unlocker_template_found = await _unlocker_assist(warm_url)
    if commit_profiles and classification in {
        DiscoveryClassification.API_VERIFIED,
        DiscoveryClassification.SSR_DOM_ONLY,
    }:
        evidence = DiscoveryEvidence(
            canonical_id=canonical_id,
            classification=classification,
            warm_page_url=warm_url,
            strict_row_count=(
                len(replay.verified_rows)
                if classification == DiscoveryClassification.API_VERIFIED
                else len(dom.verified_rows)
            ),
            endpoint_template=replay.discovered_endpoint_template,
            endpoint_provider="entrata",
            unit_page_url=dom.unit_page_url,
        )
        blob = _profile_blob(client, profile_prefix, canonical_id)
        persistence = persist_generation_guarded(blob, evidence).outcome
    return _checkpoint_row(
        canonical_id,
        cohort_row["url"],
        warm_url,
        replay,
        classification,
        persistence,
        dom,
        unlocker_attempted,
        unlocker_warm_status,
        unlocker_template_found,
    )


def _candidate_profiles(
    client: storage.Client,
    cohort: list[dict[str, str]],
    profile_prefix: str,
    limit: int,
) -> list[tuple[dict[str, str], ScrapeProfile, str]]:
    """Select up to ``limit`` ProspectPortal warm profiles from this cohort."""
    selected: list[tuple[dict[str, str], ScrapeProfile, str]] = []
    for row in cohort:
        canonical_id = str(row.get("property_id") or "").strip()
        if not canonical_id:
            continue
        try:
            payload = _profile_blob(client, profile_prefix, canonical_id).download_as_bytes()
            profile = ScrapeProfile.model_validate_json(payload)
        except Exception:
            continue
        if _has_verified_dynamic_vus(profile):
            continue
        warm_url = _public_warm_candidate(profile)
        if warm_url is None:
            continue
        selected.append((row, profile, warm_url))
        if len(selected) >= limit:
            break
    return selected


def _write_checkpoint(
    client: storage.Client, checkpoint_prefix: str, rows: list[dict[str, Any]]
) -> str:
    """Create one immutable checkpoint object and return its GCS URI."""
    bucket_name, prefix = _parse_gcs_uri(checkpoint_prefix)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{prefix.rstrip('/')}/prospectportal-canary-{stamp}.jsonl".lstrip("/")
    body = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
    client.bucket(bucket_name).blob(name).upload_from_string(
        body, content_type="application/x-ndjson", if_generation_match=0
    )
    return f"gs://{bucket_name}/{name}"


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """Run the requested bounded canary and persist its checkpoint."""
    client = storage.Client(project=args.project)
    cohort = await asyncio.to_thread(_load_cohort, client, args.cohort_gcs_uri)
    candidates = await asyncio.to_thread(
        _candidate_profiles, client, cohort, args.profile_gcs_prefix, args.limit
    )
    semaphore = asyncio.Semaphore(args.concurrency)
    rows = await asyncio.gather(
        *(
            _run_candidate(
                client=client,
                profile_prefix=args.profile_gcs_prefix,
                cohort_row=row,
                profile=profile,
                warm_url=warm_url,
                commit_profiles=args.commit_profiles,
                use_web_unlocker=args.use_web_unlocker,
                semaphore=semaphore,
            )
            for row, profile, warm_url in candidates
        )
    )
    checkpoint = await asyncio.to_thread(
        _write_checkpoint, client, args.checkpoint_gcs_prefix, rows
    )
    summary: dict[str, int] = {}
    for row in rows:
        label = str(row["classification"])
        summary[label] = summary.get(label, 0) + 1
    return {
        "cohort_size": len(cohort),
        "candidates_selected": len(candidates),
        "completed": len(rows),
        "classifications": summary,
        "checkpoint": checkpoint,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse bounded-canary command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cohort-gcs-uri",
        default="gs://jugnu-canary/property-list/plancohort-plan-level-602-deep-probe-v1.csv",
    )
    parser.add_argument(
        "--profile-gcs-prefix",
        default="gs://jugnu-canary/profiles/plancohort-run/",
    )
    parser.add_argument(
        "--checkpoint-gcs-prefix",
        default="gs://jugnu-canary/investigations/2026-07-26-unit-endpoint-discovery/",
    )
    parser.add_argument("--project", default="jugnu-494013")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--commit-profiles", action="store_true")
    parser.add_argument(
        "--use-web-unlocker",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Inspect a blocked warm page with Web Unlocker (default: enabled).",
    )
    args = parser.parse_args(argv)
    if args.limit < 1 or args.concurrency < 1:
        parser.error("--limit and --concurrency must both be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run the canary command and print a machine-readable summary."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary = asyncio.run(run(args))
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
