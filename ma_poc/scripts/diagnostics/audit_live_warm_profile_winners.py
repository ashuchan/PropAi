"""Read-only live identity audit for unresolved warm-profile winner routes.

The archived July audit identifies which retained routes still lack an
independent property identity.  This command probes only the unresolved route
that a warm profile will try first (or the archived winner when no stored
winner exists), using ordinary public GETs.  It never enables a proxy, browser,
Hyperbrowser, Web Unlocker, or a profile-store write.

Durable output contains route hashes, hosts, response hashes, and extracted
identity only.  Endpoint URLs and response bodies are never persisted.
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ma_poc.pms.source_provenance import response_sha256
from ma_poc.scripts.diagnostics.audit_july_gcp_profile_evidence import (
    MATCH,
    MISMATCH,
    ProfileRoute,
    decide_candidates,
    observed_candidates,
    parse_structured_body,
    profile_routes,
    provider_for_url,
    safe_locator,
)

_AUDIT_VERSION = "live-warm-profile-winners-v2"
_NAMED_PROVIDER_GROUPS = frozenset(
    {
        "appfolio",
        "entrata",
        "knock",
        "realpage",
        "rentcafe",
        "resman",
        "sightmap",
    }
)


@dataclass(frozen=True, slots=True)
class WinnerRoute:
    property_id: str
    route: ProfileRoute
    historical_winner: bool

    @property
    def key(self) -> str:
        return f"{self.property_id}|{self.route.sha256}"


def _provider_group(provider: str) -> str:
    if provider in _NAMED_PROVIDER_GROUPS:
        return provider
    if provider == "inventory.g5marketingcloud.com":
        return "g5_inventory"
    if provider == "www.on-site.com":
        return "onsite"
    return "property_or_operator_site"


def _appfolio_scope_decision(
    body: str, source_url: str, configured: dict[str, str]
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """Prove an AppFolio portfolio route by its published listing addresses."""

    from ma_poc.pms.adapters.appfolio import (
        _address_matches,
        _listing_address_is_judgeable,
        _listing_address_of,
        parse_appfolio_listings_ssr,
    )
    from ma_poc.pms.property_identity import evaluate_observed_from_csv

    units = parse_appfolio_listings_ssr(body, source_url)
    addresses = {_listing_address_of(unit, "floor_plan_name") for unit in units}
    addresses.discard("")
    judgeable = {address for address in addresses if _listing_address_is_judgeable(address)}
    configured_address = configured.get("address") or configured.get("Address") or ""
    configured_zip = configured.get("zip") or configured.get("Zip") or ""
    matched = sorted(
        address for address in judgeable if _address_matches(address, configured_address, configured_zip, 85)
    )
    scope = {
        "listing_rows": len(units),
        "distinct_addresses": len(addresses),
        "judgeable_addresses": len(judgeable),
        "matched_addresses": len(matched),
    }
    if not matched:
        return None, scope
    decision = evaluate_observed_from_csv(
        configured,
        {"name": "", "address": matched[0], "city": "", "state": "", "zip": ""},
    ).to_dict()
    if decision["status"] != MATCH:
        # The production AppFolio filter deliberately tolerates nearby
        # buildings on one community street.  That is useful extraction
        # behaviour but not strict identity proof, so keep it unresolved.
        return None, scope
    decision["evidence"] = list(decision.get("evidence") or []) + ["appfolio_listing_address_scope"]
    return decision, scope


def _cohort_rows(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return {
        str(row.get("apartmentid") or row.get("apartment_id") or row.get("property_id")): row for row in rows
    }


def _archive_rows(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        rows[str(value["property_id"])] = value
    return rows


def _route_archive_map(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(route.get("route_sha256")): route
        for route in row.get("profile_routes") or []
        if route.get("route_sha256")
    }


def _is_public_http_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host:
            return False
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True
        return not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )
    except ValueError:
        return False


def discover_winner_routes(
    profiles_dir: Path,
    archive_ledger: Path,
) -> tuple[list[WinnerRoute], Counter[str]]:
    archive = _archive_rows(archive_ledger)
    routes: list[WinnerRoute] = []
    skipped: Counter[str] = Counter()
    for path in sorted(profiles_dir.glob("*.json"), key=lambda item: int(item.stem)):
        property_id = path.stem
        archive_row = archive[property_id]
        by_hash = _route_archive_map(archive_row)
        candidates: list[WinnerRoute] = []
        for route in profile_routes(property_id, json.loads(path.read_text(encoding="utf-8"))):
            evidence = by_hash.get(route.sha256) or {}
            status = str((evidence.get("identity") or {}).get("status") or "UNKNOWN")
            is_stored_winner = route.source == "navigation.winning_page_url"
            is_historical_winner = bool(evidence.get("historical_winner"))
            if not (is_stored_winner or is_historical_winner):
                continue
            if status in {MATCH, MISMATCH}:
                skipped[f"already_{status.casefold()}"] += 1
                continue
            if not _is_public_http_url(route.url):
                skipped["unsafe_or_non_http"] += 1
                continue
            candidates.append(WinnerRoute(property_id, route, is_historical_winner))

        # A stored winning_page_url is what production will replay first.  If
        # absent, the exact archived winner is the next best route to validate.
        candidates.sort(
            key=lambda item: (
                item.route.source != "navigation.winning_page_url",
                not item.historical_winner,
                item.route.sha256,
            )
        )
        if candidates:
            routes.append(candidates[0])
            skipped["lower_priority_candidate"] += len(candidates) - 1
        else:
            skipped["no_unresolved_winner_route"] += 1
    return routes, skipped


def audit_route(route: WinnerRoute, configured: dict[str, str], timeout: float) -> dict[str, Any]:
    from ma_poc.pms.adapters._probe import probe_get

    parsed = urlsplit(route.route.canonical)
    provider = provider_for_url(route.route.url)
    base: dict[str, Any] = {
        "audit_version": _AUDIT_VERSION,
        "captured_at": datetime.now(UTC).isoformat(),
        "property_id": route.property_id,
        "route_sha256": route.route.sha256,
        "route_source": route.route.source,
        "historical_winner": route.historical_winner,
        "provider": provider,
        "host": parsed.hostname or None,
        "locator": safe_locator(route.route.url) or None,
    }
    try:
        response = probe_get(route.route.url, unlocker=False, retries=1, timeout=timeout)
        status = int(getattr(response, "status_code", 0) or 0)
        text = getattr(response, "text", "") or ""
        final_url = str(getattr(response, "url", "") or "")
        base.update(
            {
                "http_status": status,
                "response_bytes": len(text.encode("utf-8", errors="replace")),
                "response_sha256": response_sha256(text),
                "final_host": (urlsplit(final_url).hostname or None) if final_url else None,
            }
        )
        if status != 200 or not text:
            base["decision"] = {
                "status": "FETCH_FAILED",
                "evidence": [f"http_status_{status}" if status else "empty_response"],
            }
            return base

        body = parse_structured_body(text)
        if body is None:
            body = text
        if provider == "appfolio":
            appfolio_decision, scope = _appfolio_scope_decision(text, route.route.url, configured)
            base["provider_scope"] = scope
            if appfolio_decision is not None:
                base["candidate_count"] = scope["matched_addresses"]
                base["decision"] = appfolio_decision
                return base
        candidates = observed_candidates(provider, body)
        decision = decide_candidates(configured, candidates)
        base["candidate_count"] = len(candidates)
        base["decision"] = decision
        return base
    except Exception as exc:  # noqa: BLE001 - failure must be represented without leaking URL
        base["http_status"] = 0
        base["decision"] = {
            "status": "FETCH_FAILED",
            "evidence": [f"exception:{type(exc).__name__}"],
        }
        return base


def _read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if value.get("audit_version") == _AUDIT_VERSION:
            records.append(value)
    return records


def build_summary(
    routes: list[WinnerRoute], records: list[dict[str, Any]], skipped: Counter[str]
) -> dict[str, Any]:
    status_counts = Counter(
        str((record.get("decision") or {}).get("status") or "UNKNOWN") for record in records
    )
    provider_counts = Counter(_provider_group(provider_for_url(route.route.url)) for route in routes)
    return {
        "audit_version": _AUDIT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "routes": len(routes),
            "profiles": len({route.property_id for route in routes}),
        },
        "completion": {
            "expected_routes": len(routes),
            "completed_routes": len(records),
            "complete": len(records) == len(routes),
        },
        "status_counts": dict(sorted(status_counts.items())),
        "provider_group_counts": dict(sorted(provider_counts.items())),
        "inventory_skips": dict(sorted(skipped.items())),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    for key in ("PROBE_PROXY_URL", "WEB_UNLOCKER_KEY", "HYPERBROWSER_API_KEY"):
        os.environ.pop(key, None)

    routes, skipped = discover_winner_routes(args.profiles_dir, args.archive_ledger)
    providers = {item.strip().casefold() for item in args.providers.split(",") if item.strip()}
    if providers:
        routes = [route for route in routes if provider_for_url(route.route.url) in providers]
    if args.max_profiles:
        routes = routes[: args.max_profiles]
    if args.expected_profiles and len(routes) != args.expected_profiles:
        raise RuntimeError(f"profile_count_mismatch expected={args.expected_profiles} actual={len(routes)}")
    cohort = _cohort_rows(args.cohort_csv)
    print(
        f"inventory profiles={len(routes)} routes={len(routes)} direct_only=true paid_fallbacks=false",
        flush=True,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = args.output_dir / "live-winner-ledger.jsonl"
    summary_path = args.output_dir / "summary.json"
    if args.restart:
        ledger_path.unlink(missing_ok=True)
        summary_path.unlink(missing_ok=True)
    existing = _read_records(ledger_path)
    desired = {route.key for route in routes}
    records_by_key = {
        f"{record.get('property_id')}|{record.get('route_sha256')}": record
        for record in existing
        if f"{record.get('property_id')}|{record.get('route_sha256')}" in desired
    }
    remaining = [route for route in routes if route.key not in records_by_key]

    if not args.inventory_only:
        with (
            ledger_path.open("a", encoding="utf-8") as ledger,
            ThreadPoolExecutor(max_workers=args.workers) as executor,
        ):
            futures = {
                executor.submit(audit_route, route, cohort[route.property_id], args.timeout): route
                for route in remaining
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                route = futures[future]
                record = future.result()
                records_by_key[route.key] = record
                ledger.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
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

    records = sorted(records_by_key.values(), key=lambda value: int(value["property_id"]))
    ledger_path.write_text(
        "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
    summary = build_summary(routes, records, skipped)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-dir", type=Path, required=True)
    parser.add_argument("--cohort-csv", type=Path, required=True)
    parser.add_argument("--archive-ledger", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--providers", default="")
    parser.add_argument("--expected-profiles", type=int, default=0)
    parser.add_argument("--max-profiles", type=int, default=0)
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--restart", action="store_true")
    return parser.parse_args()


def main() -> None:
    print(json.dumps(run(parse_args()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
