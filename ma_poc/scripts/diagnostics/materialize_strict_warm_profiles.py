"""Build a local strict warm-profile candidate from positive route identity.

This command never reads or writes a profile store.  It combines the archived
GCP/live-metadata route ledger with the ordinary-GET winner audit, removes every
unresolved or mismatched replay route, clears unbound navigation/source hints,
and writes schema-valid profiles only when at least one positively identified
route remains.

The durable admission ledger contains hashes and verdicts only.  Sanitized
profile JSON can contain public widget credentials, so its ``profiles/``
directory must remain git-ignored.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.scripts.diagnostics.audit_july_gcp_profile_evidence import (
    MATCH,
    MISMATCH,
    ProfileRoute,
    _absolute_url,
    profile_routes,
)

_MATERIALIZER_VERSION = "strict-warm-profile-candidate-v2"


def _jsonl_by_property(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        rows[str(value["property_id"])] = value
    return rows


def _live_by_route(paths: list[Path]) -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            rows[(str(value["property_id"]), str(value["route_sha256"]))] = value
    return rows


def route_decisions(
    property_id: str,
    archive_row: dict[str, Any],
    live_by_route: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    decisions: dict[str, dict[str, Any]] = {}
    for route in archive_row.get("profile_routes") or []:
        route_hash = str(route["route_sha256"])
        archive_identity = route.get("identity") or {}
        archive_status = str(archive_identity.get("status") or "UNKNOWN")
        live = live_by_route.get((property_id, route_hash)) or {}
        live_status = str((live.get("decision") or {}).get("status") or "")
        if live_status in {MATCH, MISMATCH}:
            status = live_status
            source = "live_winner_route"
        elif archive_status in {MATCH, MISMATCH}:
            status = archive_status
            source = str(archive_identity.get("evidence_source") or "archive_route_identity")
        else:
            status = "UNRESOLVED"
            source = "live_fetch_failed" if live_status == "FETCH_FAILED" else "no_positive_identity"
        decisions[route_hash] = {
            "status": status,
            "evidence_source": source,
            "historical_winner": bool(route.get("historical_winner")),
            "profile_source": route.get("source"),
        }
    return decisions


def _route_sha(property_id: str, source: str, value: Any, entry_url: str) -> str:
    return ProfileRoute(property_id, source, _absolute_url(value, entry_url)).sha256


def sanitize_profile(property_id: str, profile: dict[str, Any], admitted_hashes: set[str]) -> dict[str, Any]:
    sanitized = copy.deepcopy(profile)
    navigation = sanitized.setdefault("navigation", {})
    api = sanitized.setdefault("api_hints", {})
    entry = _absolute_url(navigation.get("entry_url"))

    winning = navigation.get("winning_page_url")
    if (
        winning
        and _route_sha(property_id, "navigation.winning_page_url", winning, entry) not in admitted_hashes
    ):
        navigation["winning_page_url"] = None

    availability_path = navigation.get("availability_page_path")
    if (
        availability_path
        and _route_sha(property_id, "navigation.availability_page_path", availability_path, entry)
        not in admitted_hashes
    ):
        navigation["availability_page_path"] = None

    navigation["availability_links"] = [
        value
        for value in navigation.get("availability_links") or []
        if _route_sha(property_id, "navigation.availability_links", value, entry) in admitted_hashes
    ]
    # These collections can steer future discovery but are not independently
    # bound to the admitted unit source.
    navigation["last_navigation_hints"] = []
    navigation["explored_links"] = []

    api["widget_endpoints"] = [
        value
        for value in api.get("widget_endpoints") or []
        if _route_sha(property_id, "api_hints.widget_endpoints", value, entry) in admitted_hashes
    ]
    api["known_endpoints"] = [
        item
        for item in api.get("known_endpoints") or []
        if isinstance(item, dict)
        and _route_sha(
            property_id,
            "api_hints.known_endpoints",
            item.get("url_pattern") or item.get("url"),
            entry,
        )
        in admitted_hashes
    ]
    for field in ("llm_field_mappings", "field_patches"):
        api[field] = [
            item
            for item in api.get(field) or []
            if isinstance(item, dict)
            and _route_sha(
                property_id,
                f"api_hints.{field}",
                item.get("api_url_pattern"),
                entry,
            )
            in admitted_hashes
        ]
    api["blocked_endpoints"] = []
    api["source_observations"] = []
    api["wait_for_url_pattern"] = None
    return sanitized


def run(args: argparse.Namespace) -> dict[str, Any]:
    archive = _jsonl_by_property(args.archive_ledger)
    live = _live_by_route(args.live_winner_ledger)
    source_paths = sorted(args.profiles_dir.glob("*.json"), key=lambda item: int(item.stem))
    output_profiles = args.output_dir / "profiles"
    output_profiles.mkdir(parents=True, exist_ok=True)
    for stale in output_profiles.glob("*.json"):
        stale.unlink()

    ledger_rows: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    evidence_counts: Counter[str] = Counter()
    admitted_with_mismatch_removed = 0
    admitted_with_unresolved_removed = 0
    retained_route_total = 0
    for path in source_paths:
        property_id = path.stem
        source_raw = path.read_bytes()
        profile = json.loads(source_raw)
        decisions = route_decisions(property_id, archive[property_id], live)
        admitted_hashes = {
            route_hash for route_hash, decision in decisions.items() if decision["status"] == MATCH
        }
        mismatch_hashes = {
            route_hash for route_hash, decision in decisions.items() if decision["status"] == MISMATCH
        }
        unresolved_hashes = set(decisions) - admitted_hashes - mismatch_hashes
        if admitted_hashes:
            profile_status = "ADMIT"
        elif mismatch_hashes:
            profile_status = "QUARANTINE"
        else:
            profile_status = "REVIEW"

        output_sha = None
        retained_count = 0
        if profile_status == "ADMIT":
            sanitized = sanitize_profile(property_id, profile, admitted_hashes)
            retained = profile_routes(property_id, sanitized)
            retained_hashes = {route.sha256 for route in retained}
            if not retained_hashes or not retained_hashes.issubset(admitted_hashes):
                raise RuntimeError(f"sanitization_route_mismatch:{property_id}")
            validated = ScrapeProfile.model_validate(sanitized).model_dump(mode="json")
            serialized = json.dumps(validated, indent=2, sort_keys=True) + "\n"
            (output_profiles / path.name).write_text(serialized, encoding="utf-8")
            output_sha = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            retained_count = len(retained_hashes)
            retained_route_total += retained_count
            admitted_with_mismatch_removed += bool(mismatch_hashes)
            admitted_with_unresolved_removed += bool(unresolved_hashes)

        for decision in decisions.values():
            route_counts[decision["status"]] += 1
            evidence_counts[decision["evidence_source"]] += 1
        status_counts[profile_status] += 1
        ledger_rows.append(
            {
                "materializer_version": _MATERIALIZER_VERSION,
                "property_id": property_id,
                "status": profile_status,
                "source_profile_sha256": hashlib.sha256(source_raw).hexdigest(),
                "sanitized_profile_sha256": output_sha,
                "route_counts": {
                    "admitted": len(admitted_hashes),
                    "mismatch": len(mismatch_hashes),
                    "unresolved": len(unresolved_hashes),
                    "retained": retained_count,
                },
                "admitted_route_hashes": sorted(admitted_hashes),
                "removed_mismatch_route_hashes": sorted(mismatch_hashes),
                "removed_unresolved_route_hashes": sorted(unresolved_hashes),
            }
        )

    ledger_path = args.output_dir / "strict-profile-ledger.jsonl"
    ledger_path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in ledger_rows),
        encoding="utf-8",
    )
    summary = {
        "materializer_version": _MATERIALIZER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {"source_profiles": len(source_paths)},
        "profile_status_counts": dict(sorted(status_counts.items())),
        "route_status_counts": dict(sorted(route_counts.items())),
        "evidence_source_counts": dict(sorted(evidence_counts.items())),
        "output": {
            "profiles": len(list(output_profiles.glob("*.json"))),
            "ledger": ledger_path.name,
            "retained_routes": retained_route_total,
        },
        "sanitization": {
            "admitted_profiles_with_mismatch_routes_removed": admitted_with_mismatch_removed,
            "admitted_profiles_with_unresolved_routes_removed": admitted_with_unresolved_removed,
        },
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-dir", type=Path, required=True)
    parser.add_argument("--archive-ledger", type=Path, required=True)
    parser.add_argument("--live-winner-ledger", type=Path, nargs="+", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    print(json.dumps(run(parse_args()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
