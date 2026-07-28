"""Build the exact cohort-wide queue for platform-agnostic endpoint discovery.

Acceptance criteria (2026-07-27 cohort-wide endpoint discovery):
* Read only the requested 602-property GCS cohort, never a broader fallback.
* Make one durable, resumable route record per cohort property before browser
  work begins; include only public URLs and no cookies or dated requests.
* Route all PMS families to an appropriate discovery lane, but do not claim
  an endpoint or unit success until a later strict unit-id-plus-rent proof.
* Leave scrape profiles unchanged; endpoint/profile persistence belongs only
  to verified discovery workers with generation guards.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from google.cloud import storage  # type: ignore[import-untyped]

from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.services.endpoint_discovery_profiles import durable_endpoint_template


class DiscoveryRoute(StrEnum):
    """Platform-agnostic work lanes used by the browser discovery worker."""

    KNOWN_ENDPOINT_REVALIDATE = "KNOWN_ENDPOINT_REVALIDATE"
    RESMAN_PORTAL_DISCOVERY = "RESMAN_PORTAL_DISCOVERY"
    RENTCAFE_PORTAL_DISCOVERY = "RENTCAFE_PORTAL_DISCOVERY"
    ENTRATA_BROWSER_XHR_DISCOVERY = "ENTRATA_BROWSER_XHR_DISCOVERY"
    APPFOLIO_BROWSER_XHR_DISCOVERY = "APPFOLIO_BROWSER_XHR_DISCOVERY"
    REALPAGE_BROWSER_XHR_DISCOVERY = "REALPAGE_BROWSER_XHR_DISCOVERY"
    GENERIC_BROWSER_XHR_DISCOVERY = "GENERIC_BROWSER_XHR_DISCOVERY"


@dataclass(frozen=True, slots=True)
class RoutePlanRecord:
    """A public-only per-property work item for endpoint discovery."""

    canonical_id: str
    source_url: str
    public_url_candidates: tuple[str, ...]
    detected_platform: str
    known_endpoint_count: int
    route: DiscoveryRoute
    known_endpoint_templates: tuple[str, ...] = ()


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    """Split a ``gs://bucket/object`` URI and reject incomplete inputs."""
    parsed = urlparse(uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise ValueError(f"not_a_gcs_uri:{uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def normalize_public_url(raw_url: str) -> str | None:
    """Return an absolute public URL, adding HTTPS for a bare marketing host.

    The source cohort contains a small number of otherwise valid marketing
    hosts such as ``www.reserveatlenoxpark.net/floorplans/#/`` without a URL
    scheme.  Treating those as malformed removed the only public starting
    route before browser discovery began.  Relative paths and arbitrary text
    remain invalid: this normalizer only accepts host-like values with a dot.
    """
    value = str(raw_url or "").strip()
    if not value or value.startswith(("/", "#")) or any(char.isspace() for char in value):
        return None
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    if parsed.scheme or parsed.netloc:
        return None
    normalized = urlparse(f"https://{value}")
    host = normalized.hostname or ""
    if not normalized.netloc or "." not in host:
        return None
    return normalized.geturl()


def _public_urls(source_url: str, profile: ScrapeProfile | None) -> tuple[str, ...]:
    """Return bounded, de-duplicated public navigation candidates in priority order."""
    raw_urls: list[str] = []
    if profile is not None:
        raw_urls.append(profile.navigation.winning_page_url or "")
        raw_urls.extend(profile.navigation.availability_links or [])
    raw_urls.append(source_url)
    public: list[str] = []
    for raw in raw_urls:
        normalized = normalize_public_url(raw)
        if normalized is None:
            continue
        if normalized not in public:
            public.append(normalized)
        if len(public) == 8:
            break
    return tuple(public)


def _platform(profile: ScrapeProfile | None) -> str:
    """Return a lower-case saved PMS platform, or ``unknown``."""
    if profile is None:
        return "unknown"
    saved = str(profile.dom_hints.platform_detected or "").strip().lower()
    if saved:
        return saved
    detection = profile.confidence.last_success_detection
    if isinstance(detection, dict):
        for key in ("pms_name", "platform", "name"):
            value = str(detection.get(key) or "").strip().lower()
            if value:
                return value
    return "unknown"


def _durable_known_endpoint_templates(profile: ScrapeProfile | None) -> tuple[str, ...]:
    """Expose only safe saved endpoint templates to the replay worker.

    A route record may carry durable template knowledge, but never a prior
    request date, cookie, or response body.  Each template still requires a
    fresh strict replay before it can be counted as verified.
    """
    if profile is None:
        return ()
    templates: list[str] = []
    for endpoint in profile.api_hints.known_endpoints:
        template = str(endpoint.url_pattern or "").strip()
        if durable_endpoint_template(template) and template not in templates:
            templates.append(template)
    return tuple(templates)


def select_route(profile: ScrapeProfile | None, public_urls: tuple[str, ...]) -> DiscoveryRoute:
    """Choose a discovery lane from saved platform and public URL evidence.

    This is routing only: even a pre-existing endpoint needs fresh strict
    revalidation before it can be reported or persisted as verified.
    """
    if profile is not None and profile.api_hints.known_endpoints:
        return DiscoveryRoute.KNOWN_ENDPOINT_REVALIDATE
    platform = _platform(profile)
    url_text = " ".join(public_urls).lower()
    if platform == "resman" or "myresman.com" in url_text:
        return DiscoveryRoute.RESMAN_PORTAL_DISCOVERY
    if platform in {"rentcafe", "yardi"} or any(
        marker in url_text for marker in ("securecafe.com", "rentcafe.com")
    ):
        return DiscoveryRoute.RENTCAFE_PORTAL_DISCOVERY
    if platform in {"entrata", "prospectportal"} or any(
        marker in url_text for marker in ("prospectportal.com", "/conventional/")
    ):
        return DiscoveryRoute.ENTRATA_BROWSER_XHR_DISCOVERY
    if platform == "appfolio" or "appfolio.com" in url_text:
        return DiscoveryRoute.APPFOLIO_BROWSER_XHR_DISCOVERY
    if platform in {"realpage", "onesite"} or "onlineleasing.realpage.com" in url_text:
        return DiscoveryRoute.REALPAGE_BROWSER_XHR_DISCOVERY
    return DiscoveryRoute.GENERIC_BROWSER_XHR_DISCOVERY


def make_route_record(source_row: dict[str, str], profile: ScrapeProfile | None) -> RoutePlanRecord:
    """Create one public-only route record from a cohort row and profile."""
    canonical_id = str(source_row.get("property_id") or "").strip()
    source_url = normalize_public_url(str(source_row.get("url") or "")) or ""
    public_urls = _public_urls(source_url, profile)
    return RoutePlanRecord(
        canonical_id=canonical_id,
        source_url=source_url,
        public_url_candidates=public_urls,
        detected_platform=_platform(profile),
        known_endpoint_count=len(profile.api_hints.known_endpoints) if profile else 0,
        route=select_route(profile, public_urls),
        known_endpoint_templates=_durable_known_endpoint_templates(profile),
    )


def _load_cohort(client: storage.Client, cohort_uri: str) -> list[dict[str, str]]:
    """Load the immutable requested cohort in its original declared order."""
    bucket_name, object_name = _parse_gcs_uri(cohort_uri)
    payload = client.bucket(bucket_name).blob(object_name).download_as_text()
    return list(csv.DictReader(io.StringIO(payload)))


def _load_profile(client: storage.Client, profile_prefix: str, canonical_id: str) -> ScrapeProfile | None:
    """Best-effort load of one GCS profile; missing data remains routable."""
    bucket_name, prefix = _parse_gcs_uri(profile_prefix)
    try:
        body = (
            client.bucket(bucket_name)
            .blob(f"{prefix.rstrip('/')}/{canonical_id}.json".lstrip("/"))
            .download_as_bytes()
        )
        return ScrapeProfile.model_validate_json(body)
    except Exception:
        return None


async def build_route_plan(args: argparse.Namespace) -> dict[str, Any]:
    """Build and atomically write a complete 602-property route-plan checkpoint."""
    client = storage.Client(project=args.project)
    cohort = await asyncio.to_thread(_load_cohort, client, args.cohort_gcs_uri)
    semaphore = asyncio.Semaphore(args.profile_read_concurrency)

    async def _one(row: dict[str, str]) -> RoutePlanRecord:
        canonical_id = str(row.get("property_id") or "").strip()
        async with semaphore:
            profile = await asyncio.to_thread(_load_profile, client, args.profile_gcs_prefix, canonical_id)
        return make_route_record(row, profile)

    records = await asyncio.gather(*(_one(row) for row in cohort))
    route_counts = Counter(record.route.value for record in records)
    bucket_name, prefix = _parse_gcs_uri(args.route_plan_gcs_prefix)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{prefix.rstrip('/')}/cohort-route-plan-{stamp}.jsonl".lstrip("/")
    rows = (
        json.dumps(
            {
                "workflow_version": "cohort-route-plan-v1",
                **asdict(record),
                "route": record.route.value,
                "planned_at": datetime.now(UTC).isoformat(),
            },
            separators=(",", ":"),
        )
        + "\n"
        for record in records
    )
    client.bucket(bucket_name).blob(name).upload_from_string(
        "".join(rows), content_type="application/x-ndjson", if_generation_match=0
    )
    return {
        "cohort_size": len(cohort),
        "route_counts": dict(sorted(route_counts.items())),
        "checkpoint": f"gs://{bucket_name}/{name}",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse route-plan inputs while pinning the requested cohort by default."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cohort-gcs-uri",
        default="gs://jugnu-canary/property-list/plancohort-plan-level-602-deep-probe-v1.csv",
    )
    parser.add_argument("--profile-gcs-prefix", default="gs://jugnu-canary/profiles/plancohort-run/")
    parser.add_argument(
        "--route-plan-gcs-prefix",
        default="gs://jugnu-canary/investigations/2026-07-26-unit-endpoint-discovery/",
    )
    parser.add_argument("--project", default="jugnu-494013")
    parser.add_argument("--profile-read-concurrency", type=int, default=16)
    args = parser.parse_args(argv)
    if args.profile_read_concurrency < 1:
        parser.error("--profile-read-concurrency must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    """Build the route plan and print its durable checkpoint summary."""
    print(json.dumps(asyncio.run(build_route_plan(parse_args(argv))), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
