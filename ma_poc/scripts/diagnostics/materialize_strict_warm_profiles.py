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
import csv
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.scripts.backfills.promote_strict_canary_profiles import merge_reusable_routes
from ma_poc.scripts.diagnostics.audit_july_gcp_profile_evidence import (
    MATCH,
    MISMATCH,
    ProfileRoute,
    _absolute_url,
    profile_routes,
)

_MATERIALIZER_VERSION = "strict-warm-profile-candidate-v3"
_REQUIRED_ID_KEYS = (
    "property_id",
    "apartment_id",
    "apartmentid",
    "canonical_id",
)
_REQUIRED_ID_COLLECTION_KEYS = (
    "create_ids",
    "overlap_ids",
    "property_ids",
    "required_ids",
)


def _path_list(value: Path | list[Path] | tuple[Path, ...]) -> list[Path]:
    if isinstance(value, Path):
        return [value]
    return list(value)


def _explicit_identity(route: dict[str, Any]) -> tuple[str, str]:
    identity = route.get("identity") or {}
    status = str(identity.get("status") or "UNKNOWN")
    source = str(identity.get("evidence_source") or "archive_route_identity")
    return status, source


def _archive_by_property(paths: list[Path]) -> dict[str, dict[str, Any]]:
    """Union archived identity ledgers without allowing order-dependent verdicts."""
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for path in sorted(paths, key=lambda item: str(item.resolve())):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            property_id = str(value["property_id"])
            routes = grouped.setdefault(property_id, {})
            for route in value.get("profile_routes") or []:
                route_hash = str(route["route_sha256"])
                current = routes.get(route_hash)
                if current is None:
                    routes[route_hash] = copy.deepcopy(route)
                    continue
                statuses = {_explicit_identity(current)[0], _explicit_identity(route)[0]}
                explicit = statuses & {MATCH, MISMATCH}
                if explicit == {MATCH, MISMATCH}:
                    current["identity"] = {
                        "status": MISMATCH,
                        "evidence_source": "archive_identity_conflict",
                    }
                elif MISMATCH in explicit:
                    current["identity"] = {
                        "status": MISMATCH,
                        "evidence_source": _explicit_identity(route)[1]
                        if _explicit_identity(route)[0] == MISMATCH
                        else _explicit_identity(current)[1],
                    }
                elif MATCH in explicit:
                    current["identity"] = {
                        "status": MATCH,
                        "evidence_source": _explicit_identity(route)[1]
                        if _explicit_identity(route)[0] == MATCH
                        else _explicit_identity(current)[1],
                    }
                current["historical_winner"] = bool(
                    current.get("historical_winner") or route.get("historical_winner")
                )
    return {property_id: {"profile_routes": list(routes.values())} for property_id, routes in grouped.items()}


def _live_by_route(paths: list[Path]) -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted(paths, key=lambda item: str(item.resolve())):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            key = (str(value["property_id"]), str(value["route_sha256"]))
            current = rows.get(key)
            if current is None:
                rows[key] = value
                continue
            current_status = str((current.get("decision") or {}).get("status") or "")
            incoming_status = str((value.get("decision") or {}).get("status") or "")
            if {current_status, incoming_status} == {MATCH, MISMATCH}:
                rows[key] = {
                    **current,
                    "decision": {
                        "status": MISMATCH,
                        "evidence_source": "live_identity_conflict",
                    },
                }
            elif incoming_status in {MATCH, MISMATCH} and current_status not in {MATCH, MISMATCH}:
                rows[key] = value
    return rows


def route_decisions(
    property_id: str,
    archive_row: dict[str, Any],
    live_by_route: dict[tuple[str, str], dict[str, Any]],
    routes: list[ProfileRoute] | None = None,
) -> dict[str, dict[str, Any]]:
    decisions: dict[str, dict[str, Any]] = {}
    archived = {str(route["route_sha256"]): route for route in archive_row.get("profile_routes") or []}
    candidates: list[dict[str, Any]]
    if routes is None:
        candidates = list(archived.values())
    else:
        candidates = [
            {
                **archived.get(route.sha256, {}),
                "route_sha256": route.sha256,
                "source": route.source,
            }
            for route in routes
        ]
    for route in candidates:
        route_hash = str(route["route_sha256"])
        archive_identity = route.get("identity") or {}
        archive_status = str(archive_identity.get("status") or "UNKNOWN")
        live = live_by_route.get((property_id, route_hash)) or {}
        live_status = str((live.get("decision") or {}).get("status") or "")
        # Mismatch is safety-dominant across evidence sources. A newer
        # positive response cannot silently make an archived wrong-property
        # binding safe; the conflict must remain quarantined for review.
        if MISMATCH in {archive_status, live_status}:
            status = MISMATCH
            source = (
                "archive_live_identity_conflict"
                if MATCH in {archive_status, live_status}
                else (
                    "live_winner_route"
                    if live_status == MISMATCH
                    else str(archive_identity.get("evidence_source") or "archive_route_identity")
                )
            )
        elif MATCH in {archive_status, live_status}:
            status = MATCH
            source = (
                "live_winner_route"
                if live_status == MATCH
                else str(archive_identity.get("evidence_source") or "archive_route_identity")
            )
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


def _source_profiles(
    profile_dirs: list[Path],
) -> dict[str, list[tuple[Path, bytes, ScrapeProfile]]]:
    grouped: dict[str, list[tuple[Path, bytes, ScrapeProfile]]] = {}
    for directory in sorted(profile_dirs, key=lambda item: str(item.resolve())):
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            try:
                int(path.stem)
            except ValueError as exc:
                raise RuntimeError(f"non_numeric_profile_filename:{path}") from exc
            raw = path.read_bytes()
            profile = ScrapeProfile.model_validate_json(raw)
            property_id = path.stem
            if str(profile.canonical_id) != property_id:
                raise RuntimeError(
                    f"profile_canonical_id_mismatch:path={path}:canonical_id={profile.canonical_id}"
                )
            grouped.setdefault(property_id, []).append((path, raw, profile))
    return grouped


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _merge_source_profiles(
    property_id: str,
    candidates: list[tuple[Path, bytes, ScrapeProfile]],
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Deterministically field-merge reusable routes for one property.

    The newest successful profile owns non-route state such as confidence,
    quality, and fetch-tier history.  Older snapshots may contribute reusable
    routes, but a temporary download directory must never decide which
    snapshot wins.  Content hashes provide a stable tie-break when two stores
    carry the same ``updated_at`` value.
    """
    hashed = [(hashlib.sha256(raw).hexdigest(), path, raw, profile) for path, raw, profile in candidates]
    ordered = sorted(
        hashed,
        key=lambda item: (_naive_utc(item[3].updated_at), item[0]),
        reverse=True,
    )
    merged = ordered[0][3].model_copy(deep=True)
    original_versions = [item[3].version for item in ordered]
    original_updated = [_naive_utc(item[3].updated_at) for item in ordered]
    changed = False
    for _, _, _, incoming in ordered[1:]:
        changed |= merge_reusable_routes(merged, incoming.model_copy(deep=True))
    if len(ordered) > 1:
        # merge_reusable_routes timestamps with wall-clock time. Normalize its
        # bookkeeping so identical inputs always produce identical artifacts.
        merged.version = max(original_versions) + int(changed)
        merged.updated_at = max(original_updated)
        if changed:
            merged.updated_by = "STRICT_PROFILE_SOURCE_UNION"
    profile = merged.model_dump(mode="json")
    # This ledger is content-addressed.  Absolute scratch paths made identical
    # inputs produce different release evidence after a download was moved.
    # Duplicate copies of the same profile are intentionally collapsed: they
    # add no route knowledge and must not change the candidate digest.
    unique_sources = {
        source_sha: {
            "sha256": source_sha,
            "updated_at": profile.updated_at.isoformat(),
            "version": profile.version,
        }
        for source_sha, _, _, profile in ordered
    }
    sources = [unique_sources[source_sha] for source_sha in sorted(unique_sources)]
    source_set_sha = hashlib.sha256("\n".join(item["sha256"] for item in sources).encode("utf-8")).hexdigest()
    if str(profile.get("canonical_id")) != property_id:
        raise RuntimeError(f"merged_profile_canonical_id_mismatch:{property_id}")
    return profile, sources, source_set_sha


def _row_property_id(value: Any) -> str | None:
    if isinstance(value, (str, int)):
        raw = str(value).strip()
        return str(int(raw)) if raw.isdigit() else None
    if isinstance(value, dict):
        for key in _REQUIRED_ID_KEYS:
            raw = value.get(key)
            if raw not in (None, ""):
                return str(int(raw))
    return None


def _required_ids_from_json(value: Any) -> set[str]:
    if isinstance(value, list):
        return {property_id for item in value if (property_id := _row_property_id(item))}
    if not isinstance(value, dict):
        property_id = _row_property_id(value)
        return {property_id} if property_id else set()
    ids: set[str] = set()
    direct = _row_property_id(value)
    if direct:
        ids.add(direct)
    for key in _REQUIRED_ID_COLLECTION_KEYS:
        ids.update(_required_ids_from_json(value.get(key) or []))
    return ids


def load_required_property_ids(paths: list[Path]) -> set[str]:
    ids: set[str] = set()
    for path in sorted(paths, key=lambda item: str(item.resolve())):
        suffix = path.suffix.casefold()
        if suffix == ".jsonl":
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    ids.update(_required_ids_from_json(json.loads(line)))
        elif suffix == ".json":
            ids.update(_required_ids_from_json(json.loads(path.read_text(encoding="utf-8"))))
        elif suffix in {".csv", ".tsv"}:
            delimiter = "\t" if suffix == ".tsv" else ","
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle, delimiter=delimiter):
                    property_id = _row_property_id(row)
                    if property_id:
                        ids.add(property_id)
        else:
            for line in path.read_text(encoding="utf-8").splitlines():
                property_id = _row_property_id(line)
                if property_id:
                    ids.add(property_id)
    return ids


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
    profile_dirs = _path_list(args.profiles_dir)
    archive_paths = _path_list(args.archive_ledger)
    live_paths = _path_list(args.live_winner_ledger)
    required_paths = _path_list(getattr(args, "required_property_ids_file", []))
    archive = _archive_by_property(archive_paths)
    live = _live_by_route(live_paths)
    source_profiles = _source_profiles(profile_dirs)
    required_ids = load_required_property_ids(required_paths)
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
    status_by_property: dict[str, str] = {}
    source_snapshot_times: list[datetime] = []
    for property_id in sorted(source_profiles, key=int):
        profile, source_records, source_set_sha = _merge_source_profiles(
            property_id, source_profiles[property_id]
        )
        source_snapshot_times.append(_naive_utc(ScrapeProfile.model_validate(profile).updated_at))
        actual_routes = profile_routes(property_id, profile)
        decisions = route_decisions(
            property_id,
            archive.get(property_id, {"profile_routes": []}),
            live,
            actual_routes,
        )
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
            (output_profiles / f"{property_id}.json").write_text(serialized, encoding="utf-8")
            output_sha = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            retained_count = len(retained_hashes)
            retained_route_total += retained_count
            admitted_with_mismatch_removed += bool(mismatch_hashes)
            admitted_with_unresolved_removed += bool(unresolved_hashes)

        for decision in decisions.values():
            route_counts[decision["status"]] += 1
            evidence_counts[decision["evidence_source"]] += 1
        status_counts[profile_status] += 1
        status_by_property[property_id] = profile_status
        ledger_rows.append(
            {
                "materializer_version": _MATERIALIZER_VERSION,
                "property_id": property_id,
                "status": profile_status,
                # Kept for downstream v2 readers; in v3 this is the stable
                # hash of the complete source set, not a last-writer file.
                "source_profile_sha256": source_set_sha,
                "source_profiles": source_records,
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
    required_not_admitted = sorted(
        (property_id for property_id in required_ids if status_by_property.get(property_id) != "ADMIT"),
        key=int,
    )
    required_missing_source = sorted(required_ids - set(source_profiles), key=int)
    summary = {
        "materializer_version": _MATERIALIZER_VERSION,
        "source_snapshot_at": (
            max(source_snapshot_times).replace(tzinfo=UTC).isoformat() if source_snapshot_times else None
        ),
        "scope": {
            "source_profile_files": sum(len(items) for items in source_profiles.values()),
            "source_properties": len(source_profiles),
            "profile_dirs": [str(path.resolve()) for path in sorted(profile_dirs, key=str)],
            "archive_ledgers": [str(path.resolve()) for path in sorted(archive_paths, key=str)],
            "live_winner_ledgers": [str(path.resolve()) for path in sorted(live_paths, key=str)],
        },
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
        "required_admission": {
            "required": len(required_ids),
            "admitted": len(required_ids) - len(required_not_admitted),
            "missing_source_ids": required_missing_source,
            "not_admitted_ids": required_not_admitted,
            "passed": not required_not_admitted,
        },
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if required_not_admitted:
        raise RuntimeError("required_profile_admission_failed:" + ",".join(required_not_admitted))
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-dir", type=Path, action="append", required=True)
    parser.add_argument("--archive-ledger", type=Path, action="append", required=True)
    parser.add_argument(
        "--live-winner-ledger",
        type=Path,
        action="append",
        default=[],
        help="Optional live route-identity ledger; repeat to union multiple ledgers",
    )
    parser.add_argument(
        "--required-property-ids-file",
        type=Path,
        action="append",
        default=[],
        help="CSV/TSV/JSON/JSONL/text IDs that must all finish ADMIT",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    print(json.dumps(run(parse_args()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
