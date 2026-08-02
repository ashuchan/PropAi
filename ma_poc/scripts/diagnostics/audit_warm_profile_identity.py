"""Read-only identity audit for reusable warm-profile vendor routes.

This is deliberately not a scraper or canary.  It loads a frozen local profile
snapshot, discovers only vendor routes whose response publishes property
metadata, performs ordinary public GETs, and compares that metadata with the
configured cohort name/address.  Web Unlocker and profile-store writes are
always disabled.

The durable ledger never stores endpoint URLs.  Some SightMap routes include a
public access token in the path, so records use a provider-specific locator
that omits that token plus a SHA-256 fingerprint of the complete route.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ma_poc.pms.property_identity import (
    MATCH,
    MISMATCH,
    evaluate_observed_from_csv,
    knock_observed_identity,
    sightmap_observed_identity,
)
from ma_poc.pms.source_provenance import response_sha256

_AUDIT_VERSION = "warm-profile-identity-audit-v1"
_SIGHTMAP_RE = re.compile(
    r"https?://sightmap\.com/app/api/v1/[^/?#]+/sightmaps/(?P<asset>\d+)",
    re.IGNORECASE,
)
_KNOCK_COMMUNITY_RE = re.compile(
    r"https?://doorway-api\.knockrentals\.com/v1/property/community/"
    r"(?P<community>[a-z0-9]+)",
    re.IGNORECASE,
)
_KNOCK_NUMERIC_RE = re.compile(
    r"https?://doorway-api\.knockrentals\.com/v1/property/(?P<property>\d+)"
    r"(?:/units)?(?:[/?#]|$)",
    re.IGNORECASE,
)
_EDIFICE_HOST = "edificecms.com"
_EDIFICE_PATH = "/myresman/public/api/front/floorplans"


@dataclass(frozen=True, slots=True)
class AuditRoute:
    """One unique self-describing route retained by a property profile."""

    property_id: str
    provider: str
    route_kind: str
    locator: str
    url: str
    sources: tuple[str, ...]

    @property
    def route_sha256(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()

    @property
    def key(self) -> str:
        return f"{self.property_id}|{self.provider}|{self.route_kind}|{self.locator}"


def _absolute_url(value: Any) -> str:
    raw = str(value or "").strip().replace("\\/", "/")
    if raw and "://" not in raw and "." in raw.split("/", 1)[0]:
        return "https://" + raw
    return raw


def _profile_urls(profile: dict[str, Any]) -> list[tuple[str, str]]:
    """Return every replayable URL-bearing profile field with its source."""

    found: list[tuple[str, str]] = []
    navigation = profile.get("navigation") or {}
    api = profile.get("api_hints") or {}

    def add(source: str, value: Any) -> None:
        url = _absolute_url(value)
        if url:
            found.append((source, url))

    add("winning_page_url", navigation.get("winning_page_url"))
    for value in navigation.get("availability_links") or []:
        add("availability_links", value)
    for value in api.get("widget_endpoints") or []:
        add("widget_endpoints", value)
    for value in api.get("known_endpoints") or []:
        if isinstance(value, dict):
            add("known_endpoints", value.get("url_pattern") or value.get("url"))
    for field, source in (
        ("llm_field_mappings", "llm_field_mappings"),
        ("field_patches", "field_patches"),
    ):
        for value in api.get(field) or []:
            if isinstance(value, dict):
                add(source, value.get("api_url_pattern"))
    return found


def _classified_route(
    property_id: str, source: str, raw_url: str
) -> tuple[tuple[str, str, str], str, str] | None:
    """Return ``(dedupe key, metadata URL, source)`` for a supported route."""

    sightmap = _SIGHTMAP_RE.search(raw_url)
    if sightmap:
        asset_id = sightmap.group("asset")
        # Keep the exact URL for the request: the preceding path segment is a
        # vendor access token and must never be emitted into the ledger.
        url = raw_url.split("#", 1)[0]
        return ("sightmap", "asset", f"asset:{asset_id}"), url, source

    community = _KNOCK_COMMUNITY_RE.search(raw_url)
    if community:
        community_id = community.group("community").lower()
        url = "https://doorway-api.knockrentals.com/v1/property/community/" + community_id
        return (
            (
                "knock",
                "community",
                f"community:{community_id}",
            ),
            url,
            source,
        )

    numeric = _KNOCK_NUMERIC_RE.search(raw_url)
    if numeric:
        numeric_id = numeric.group("property")
        url = f"https://doorway-api.knockrentals.com/v1/property/{numeric_id}"
        return (
            (
                "knock",
                "numeric_property",
                f"property:{numeric_id}",
            ),
            url,
            source,
        )

    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return None
    if (
        parsed.hostname
        and parsed.hostname.casefold() == _EDIFICE_HOST
        and parsed.path.rstrip("/") == _EDIFICE_PATH
    ):
        property_ids = parse_qs(parsed.query).get("property_id") or []
        if not property_ids:
            return None
        edifice_id = property_ids[0].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", edifice_id):
            return None
        url = (
            "https://edificecms.com/myresman/public/api/front/floorplans"
            f"?action=get_floorplans&property_id={edifice_id}"
        )
        return (
            (
                "edifice",
                "property",
                f"property:{edifice_id}",
            ),
            url,
            source,
        )
    return None


def discover_routes(property_id: str, profile: dict[str, Any]) -> list[AuditRoute]:
    """Discover and deduplicate every supported route in one profile."""

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for source, raw_url in _profile_urls(profile):
        classified = _classified_route(property_id, source, raw_url)
        if classified is None:
            continue
        key, url, signal = classified
        item = grouped.setdefault(key, {"url": url, "sources": set()})
        item["sources"].add(signal)
    return [
        AuditRoute(
            property_id=property_id,
            provider=key[0],
            route_kind=key[1],
            locator=key[2],
            url=value["url"],
            sources=tuple(sorted(value["sources"])),
        )
        for key, value in sorted(grouped.items())
    ]


def _cohort_rows(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        property_id = str(
            row.get("apartmentid") or row.get("apartment_id") or row.get("property_id") or ""
        ).strip()
        if property_id:
            result[property_id] = row
    return result


def load_inventory(
    profiles_dir: Path, cohort_csv: Path, providers: set[str]
) -> tuple[list[AuditRoute], dict[str, dict[str, str]]]:
    cohort = _cohort_rows(cohort_csv)
    routes: list[AuditRoute] = []
    for path in sorted(profiles_dir.glob("*.json"), key=lambda item: item.stem):
        property_id = path.stem
        if property_id not in cohort:
            raise RuntimeError(f"profile_not_in_cohort:{property_id}")
        profile = json.loads(path.read_text(encoding="utf-8"))
        routes.extend(route for route in discover_routes(property_id, profile) if route.provider in providers)
    return routes, cohort


def _observed_identity(provider: str, body: Any) -> dict[str, str]:
    if provider == "sightmap":
        return sightmap_observed_identity(body)
    if provider == "knock":
        return knock_observed_identity(body)
    if provider == "edifice" and isinstance(body, dict):
        value = body.get("property")
        if isinstance(value, dict):
            return {
                "name": str(value.get("name") or value.get("title") or "").strip(),
                "address": str(value.get("address") or "").strip(),
                "city": str(value.get("city") or "").strip(),
                "state": str(value.get("state") or "").strip(),
                "zip": str(value.get("zip") or value.get("postal_code") or "").strip(),
            }
        return {"name": str(value or "").strip(), "address": "", "city": "", "state": "", "zip": ""}
    return {"name": "", "address": "", "city": "", "state": "", "zip": ""}


def audit_route(route: AuditRoute, configured: dict[str, str], timeout: float) -> dict[str, Any]:
    """Fetch and evaluate one route without returning or persisting its URL."""

    from ma_poc.pms.adapters._probe import probe_get

    base: dict[str, Any] = {
        "audit_version": _AUDIT_VERSION,
        "captured_at": datetime.now(UTC).isoformat(),
        "property_id": route.property_id,
        "provider": route.provider,
        "route_kind": route.route_kind,
        "route_locator": route.locator,
        "route_sha256": route.route_sha256,
        "profile_sources": list(route.sources),
        "configured": {
            "name": configured.get("name") or configured.get("Property Name") or "",
            "address": configured.get("address") or configured.get("Address") or "",
            "city": configured.get("city") or configured.get("City") or "",
            "state": configured.get("state") or configured.get("State") or "",
            "zip": configured.get("zip") or configured.get("Zip") or "",
        },
    }
    try:
        response = probe_get(
            route.url,
            unlocker=False,
            retries=1,
            timeout=timeout,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        text = getattr(response, "text", "") or ""
        base["http_status"] = status
        base["response_bytes"] = len(text.encode("utf-8", errors="replace"))
        base["response_sha256"] = response_sha256(text)
        if status != 200 or not text:
            base["decision"] = {
                "status": "FETCH_FAILED",
                "evidence": [f"http_status_{status}" if status else "empty_response"],
            }
            return base
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            base["decision"] = {
                "status": "INVALID_RESPONSE",
                "evidence": ["non_json_response"],
            }
            return base
        observed = _observed_identity(route.provider, body)
        decision = evaluate_observed_from_csv(configured, observed)
        base["observed"] = observed
        base["decision"] = decision.to_dict()
        return base
    except Exception as exc:  # noqa: BLE001 - an audit failure must be durable
        base["http_status"] = 0
        base["decision"] = {
            "status": "FETCH_FAILED",
            # Never persist exception strings: transport errors may echo the
            # complete SightMap URL including its path token.
            "evidence": [f"exception:{type(exc).__name__}"],
        }
        return base


def _profile_status(records: list[dict[str, Any]]) -> str:
    statuses = [str((record.get("decision") or {}).get("status") or "UNKNOWN") for record in records]
    if any(status == MISMATCH for status in statuses):
        return MISMATCH
    if statuses and all(status == MATCH for status in statuses):
        return MATCH
    return "UNRESOLVED"


def build_summary(routes: list[AuditRoute], records: list[dict[str, Any]]) -> dict[str, Any]:
    route_status = Counter(
        str((record.get("decision") or {}).get("status") or "UNKNOWN") for record in records
    )
    route_provider = Counter(route.provider for route in routes)
    by_profile: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_profile[str(record.get("property_id"))].append(record)
    profile_status = Counter(_profile_status(items) for items in by_profile.values())
    expected_profiles = {route.property_id for route in routes}
    completed_keys = {
        f"{record.get('property_id')}|{record.get('provider')}|"
        f"{record.get('route_kind')}|{record.get('route_locator')}"
        for record in records
    }
    mismatch_details = [
        {
            "property_id": record.get("property_id"),
            "provider": record.get("provider"),
            "route_kind": record.get("route_kind"),
            "route_locator": record.get("route_locator"),
            "configured": record.get("configured"),
            "observed": record.get("observed"),
            "evidence": (record.get("decision") or {}).get("evidence"),
        }
        for record in records
        if (record.get("decision") or {}).get("status") == MISMATCH
    ]
    return {
        "audit_version": _AUDIT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "profiles": len(expected_profiles),
            "routes": len(routes),
            "route_counts_by_provider": dict(sorted(route_provider.items())),
        },
        "completion": {
            "completed_routes": len(completed_keys),
            "expected_routes": len(routes),
            "complete": len(completed_keys) == len(routes),
        },
        "route_status_counts": dict(sorted(route_status.items())),
        "profile_status_counts": dict(sorted(profile_status.items())),
        "mismatches": mismatch_details,
    }


def _read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("audit_version") == _AUDIT_VERSION:
            records.append(value)
    return records


def _record_key(record: dict[str, Any]) -> str:
    return (
        f"{record.get('property_id')}|{record.get('provider')}|"
        f"{record.get('route_kind')}|{record.get('route_locator')}"
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    providers = {item.strip().lower() for item in args.providers.split(",") if item.strip()}
    unknown = providers - {"sightmap", "knock", "edifice"}
    if unknown:
        raise ValueError(f"unsupported_providers:{','.join(sorted(unknown))}")

    # Cost/safety boundary: this diagnostic must not inherit credentials for
    # a residential proxy or Web Unlocker from the caller's environment.
    os.environ.pop("PROBE_PROXY_URL", None)
    os.environ.pop("WEB_UNLOCKER_KEY", None)

    routes, cohort = load_inventory(args.profiles_dir, args.cohort_csv, providers)
    if args.max_profiles:
        selected = sorted({route.property_id for route in routes})[: args.max_profiles]
        selected_set = set(selected)
        routes = [route for route in routes if route.property_id in selected_set]
    profile_count = len({route.property_id for route in routes})
    print(
        f"inventory profiles={profile_count} routes={len(routes)} providers={','.join(sorted(providers))}",
        flush=True,
    )
    if args.expected_profiles and profile_count != args.expected_profiles:
        raise RuntimeError(f"profile_count_mismatch expected={args.expected_profiles} actual={profile_count}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = args.output_dir / "route-ledger.jsonl"
    summary_path = args.output_dir / "summary.json"
    if args.restart:
        ledger_path.unlink(missing_ok=True)
        summary_path.unlink(missing_ok=True)
    existing = _read_records(ledger_path)
    desired = {route.key for route in routes}
    records_by_key = {_record_key(record): record for record in existing if _record_key(record) in desired}

    remaining = [route for route in routes if route.key not in records_by_key]
    if args.inventory_only:
        summary = build_summary(routes, list(records_by_key.values()))
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return summary

    with (
        ledger_path.open("a", encoding="utf-8") as ledger,
        ThreadPoolExecutor(max_workers=args.workers) as executor,
    ):
        futures = {
            executor.submit(audit_route, route, cohort[route.property_id], args.timeout): route
            for route in remaining
        }
        completed = 0
        for future in as_completed(futures):
            route = futures[future]
            record = future.result()
            records_by_key[route.key] = record
            ledger.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            completed += 1
            if completed % 25 == 0 or completed == len(remaining):
                ledger.flush()
            if completed % 100 == 0 or completed == len(remaining):
                counts = Counter(
                    (value.get("decision") or {}).get("status") for value in records_by_key.values()
                )
                print(
                    f"progress completed={len(records_by_key)}/{len(routes)} "
                    f"statuses={dict(sorted(counts.items()))}",
                    flush=True,
                )

    records = sorted(
        records_by_key.values(),
        key=lambda value: (
            int(str(value.get("property_id") or "0")),
            str(value.get("provider") or ""),
            str(value.get("route_kind") or ""),
            str(value.get("route_locator") or ""),
        ),
    )
    ledger_path.write_text(
        "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
    summary = build_summary(routes, records)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-dir", type=Path, required=True)
    parser.add_argument("--cohort-csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--providers", default="sightmap,knock,edifice")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--expected-profiles", type=int, default=0)
    parser.add_argument("--max-profiles", type=int, default=0)
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--restart", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = run(args)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
