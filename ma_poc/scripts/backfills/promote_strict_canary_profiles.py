"""Promote reusable profiles from a strictly audited canary run.

The promotion is deliberately two-gated:

1. A property must pass the run-output contract: ``verdict == SUCCESS``, a
   non-empty unit list, and at least one real unit identity.
2. Its run profile must contain an actual replay route. A bootstrap profile
   with only entry metadata is archived but never promoted.

New target objects are create-only. Existing target profiles are field-merged
with a generation precondition: their learned winner and history are kept,
while previously unseen canary routes are appended. Every source object is
frozen by GCS generation and SHA-256 so a delayed profile flush cannot silently
change the promotion set between dry-run review and execution.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ma_poc.models.scrape_profile import ProfileMaturity, ScrapeProfile


@dataclass(frozen=True, slots=True)
class FrozenProfile:
    """One generation-pinned run profile."""

    property_id: int
    raw: bytes
    generation: int
    sha256: str
    route_signals: tuple[str, ...]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parse_gcs_uri(uri: str) -> tuple[str, str]:
    """Return ``(bucket, normalized_prefix)`` for a complete GCS URI."""
    parsed = urlparse(uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise ValueError(f"not_a_gcs_uri:{uri!r}")
    prefix = parsed.path.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return parsed.netloc, prefix


def property_id(row: dict[str, Any]) -> int:
    raw = row.get("apartment_id") or row.get("apartmentid") or row.get("property_id")
    return int(raw)


def strict_success(row: dict[str, Any]) -> bool:
    """Apply the production-shaped unit identity gate to one output row."""
    units = row.get("units")
    meta = row.get("_meta") or {}
    quality = (meta.get("provenance") or {}).get("data_quality") or {}
    return bool(
        isinstance(units, list)
        and units
        and meta.get("verdict") == "SUCCESS"
        and int(quality.get("real_id_units") or 0) > 0
    )


def reusable_route_signals(profile: ScrapeProfile) -> tuple[str, ...]:
    """Describe deterministic replay knowledge present in *profile*.

    ``platform_detected`` is intentionally absent. It is bootstrap routing,
    not proof that the canary discovered a reusable unit surface.
    """
    signals: list[str] = []
    navigation = profile.navigation
    api = profile.api_hints
    if navigation.winning_page_url:
        signals.append("winning_page_url")
    if navigation.availability_page_path:
        signals.append("availability_page_path")
    if navigation.availability_links:
        signals.append("availability_links")
    if api.known_endpoints:
        signals.append("known_endpoints")
    if api.widget_endpoints:
        signals.append("widget_endpoints")
    return tuple(signals)


def profile_is_actionable(profile: ScrapeProfile) -> bool:
    return bool(reusable_route_signals(profile))


def _append_unique(values: list[Any], additions: list[Any], *, key: Any = None) -> bool:
    """Append unseen values while retaining target order; return changed."""
    changed = False
    key_fn = key or (lambda value: value)
    seen = {key_fn(value) for value in values}
    for value in additions:
        value_key = key_fn(value)
        if value_key in seen:
            continue
        values.append(value)
        seen.add(value_key)
        changed = True
    return changed


def merge_reusable_routes(target: ScrapeProfile, source: ScrapeProfile) -> bool:
    """Merge only reusable route knowledge from *source* into *target*.

    Existing organic winners are never overwritten. A different strict-canary
    winner is retained as an availability link so it remains a second route.
    Counters, quality history, blocked endpoints, and LLM statistics remain
    target-owned.
    """
    if target.canonical_id != source.canonical_id:
        raise ValueError("canonical_id_mismatch")

    changed = False
    target_nav = target.navigation
    source_nav = source.navigation

    if not target_nav.entry_url and source_nav.entry_url:
        target_nav.entry_url = source_nav.entry_url
        changed = True

    source_winner = source_nav.winning_page_url
    if source_winner:
        if not target_nav.winning_page_url:
            target_nav.winning_page_url = source_winner
            changed = True
        elif target_nav.winning_page_url != source_winner:
            changed |= _append_unique(target_nav.availability_links, [source_winner])

    if not target_nav.availability_page_path and source_nav.availability_page_path:
        target_nav.availability_page_path = source_nav.availability_page_path
        changed = True

    changed |= _append_unique(
        target_nav.availability_links,
        list(source_nav.availability_links),
    )
    if not target_nav.requires_interaction and source_nav.requires_interaction:
        target_nav.requires_interaction = list(source_nav.requires_interaction)
        changed = True

    target_api = target.api_hints
    source_api = source.api_hints
    changed |= _append_unique(
        target_api.known_endpoints,
        list(source_api.known_endpoints),
        key=lambda endpoint: endpoint.url_pattern,
    )
    changed |= _append_unique(
        target_api.widget_endpoints,
        list(source_api.widget_endpoints),
    )
    for field_name in (
        "api_provider",
        "client_account_id",
        "wait_for_url_pattern",
        "rentcafe_property_id",
    ):
        current = getattr(target_api, field_name)
        incoming = getattr(source_api, field_name)
        if current in (None, "", "unknown") and incoming not in (
            None,
            "",
            "unknown",
        ):
            setattr(target_api, field_name, incoming)
            changed = True

    if not target.dom_hints.platform_detected and source.dom_hints.platform_detected:
        target.dom_hints.platform_detected = source.dom_hints.platform_detected
        changed = True

    if target.confidence.maturity == ProfileMaturity.COLD:
        target.confidence.maturity = ProfileMaturity.WARM
        changed = True
    if target.confidence.preferred_tier is None and source.confidence.preferred_tier is not None:
        target.confidence.preferred_tier = source.confidence.preferred_tier
        changed = True
    if target.confidence.last_success_tier is None and source.confidence.last_success_tier is not None:
        target.confidence.last_success_tier = source.confidence.last_success_tier
        changed = True

    if changed:
        target.version += 1
        target.updated_at = datetime.now(UTC).replace(tzinfo=None)
        target.updated_by = "STRICT_CANARY_ROUTE_MERGE"
    return changed


def load_cohort_ids(
    cohort_csv: Path,
    *,
    expected_sha256: str,
    expected_count: int,
) -> set[int]:
    raw = cohort_csv.read_bytes()
    actual_sha = sha256_bytes(raw)
    if actual_sha != expected_sha256:
        raise RuntimeError(f"cohort_sha256_mismatch expected={expected_sha256} actual={actual_sha}")
    with cohort_csv.open(encoding="utf-8-sig", newline="") as handle:
        ids = {property_id(row) for row in csv.DictReader(handle)}
    if len(ids) != expected_count:
        raise RuntimeError(f"cohort_count_mismatch expected={expected_count} actual={len(ids)}")
    return ids


def load_strict_ids(
    bucket: Any,
    output_prefix: str,
    cohort_ids: set[int],
    *,
    expected_shards: int,
    expected_output_rows: int,
    expected_strict: int,
    excluded_ids: set[int],
) -> set[int]:
    blobs = sorted(
        (blob for blob in bucket.list_blobs(prefix=output_prefix) if blob.name.endswith("/properties.json")),
        key=lambda blob: blob.name,
    )
    if len(blobs) != expected_shards:
        raise RuntimeError(f"shard_count_mismatch expected={expected_shards} actual={len(blobs)}")

    def download(blob: Any) -> list[dict[str, Any]]:
        payload = json.loads(blob.download_as_bytes())
        if not isinstance(payload, list):
            raise RuntimeError(f"non_list_shard_payload:{blob.name}")
        return [row for row in payload if isinstance(row, dict)]

    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(download, blob) for blob in blobs]
        for future in as_completed(futures):
            rows.extend(future.result())

    ids = [property_id(row) for row in rows]
    if len(ids) != expected_output_rows or len(set(ids)) != expected_output_rows:
        raise RuntimeError(
            "output_cardinality_mismatch "
            f"expected={expected_output_rows} rows={len(ids)} unique={len(set(ids))}"
        )
    if not set(ids).issubset(cohort_ids):
        raise RuntimeError("run_output_contains_out_of_cohort_property")
    strict_ids = {
        property_id(row) for row in rows if strict_success(row) and property_id(row) not in excluded_ids
    }
    if len(strict_ids) != expected_strict:
        raise RuntimeError(f"strict_count_mismatch expected={expected_strict} actual={len(strict_ids)}")
    return strict_ids


def freeze_profiles(
    bucket: Any,
    profile_prefix: str,
    strict_ids: set[int],
) -> tuple[dict[int, FrozenProfile], set[int]]:
    """Freeze actionable profiles and return bootstrap-only ids separately."""

    def freeze(pid: int) -> tuple[int, FrozenProfile | None]:
        blob = bucket.blob(f"{profile_prefix}{pid}.json")
        blob.reload()
        generation = int(blob.generation or 0)
        raw = blob.download_as_bytes(if_generation_match=generation)
        profile = ScrapeProfile.model_validate_json(raw)
        if str(profile.canonical_id) != str(pid):
            raise RuntimeError(f"canonical_id_mismatch:{pid}")
        signals = reusable_route_signals(profile)
        if not signals:
            return pid, None
        return pid, FrozenProfile(
            property_id=pid,
            raw=raw,
            generation=generation,
            sha256=sha256_bytes(raw),
            route_signals=signals,
        )

    actionable: dict[int, FrozenProfile] = {}
    bootstrap_only: set[int] = set()
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(freeze, pid) for pid in sorted(strict_ids)]
        for future in as_completed(futures):
            pid, frozen = future.result()
            if frozen is None:
                bootstrap_only.add(pid)
            else:
                actionable[pid] = frozen
    return actionable, bootstrap_only


def immediate_numeric_profile_ids(bucket: Any, prefix: str) -> set[int]:
    ids: set[int] = set()
    for blob in bucket.list_blobs(prefix=prefix, delimiter="/"):
        relative = blob.name.removeprefix(prefix)
        if relative.endswith(".json") and relative[:-5].isdigit():
            ids.add(int(relative[:-5]))
    return ids


def write_snapshot(
    snapshot_dir: Path,
    frozen: dict[int, FrozenProfile],
    manifest: dict[str, Any],
) -> Path:
    profile_dir = snapshot_dir / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    for pid, profile in sorted(frozen.items()):
        (profile_dir / f"{pid}.json").write_bytes(profile.raw)
    # Keep the reviewed dry-run immutable when the guarded write is executed.
    # ``--expected-snapshot-manifest`` points at the dry-run file, while the
    # write result receives its own audit record.
    manifest_name = (
        "promotion_manifest_execute.json" if manifest.get("mode") == "execute" else "promotion_manifest.json"
    )
    output = snapshot_dir / manifest_name
    output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


def verify_expected_snapshot(
    expected_manifest: Path | None,
    frozen: dict[int, FrozenProfile],
) -> None:
    if expected_manifest is None:
        return
    expected = json.loads(expected_manifest.read_text(encoding="utf-8"))
    expected_source = expected.get("source_profiles") or {}
    actual_source = {
        str(pid): {
            "generation": profile.generation,
            "sha256": profile.sha256,
            "route_signals": list(profile.route_signals),
        }
        for pid, profile in sorted(frozen.items())
    }
    if expected_source != actual_source:
        raise RuntimeError("source_profile_snapshot_changed_since_review")


def verify_expected_target_plan(
    expected_manifest: Path | None,
    actionable_ids: set[int],
    existing_before: set[int],
) -> None:
    """Require the reviewed create/merge split before mutating the target."""
    if expected_manifest is None:
        return
    expected = json.loads(expected_manifest.read_text(encoding="utf-8"))
    expected_create = {int(value) for value in expected.get("create_ids") or []}
    expected_overlap = {int(value) for value in expected.get("overlap_ids") or []}
    actual_create = actionable_ids - existing_before
    actual_overlap = actionable_ids & existing_before
    if expected_create != actual_create or expected_overlap != actual_overlap:
        raise RuntimeError("target_profile_plan_changed_since_review")


def _metadata(existing: dict[str, str] | None, source: FrozenProfile) -> dict[str, str]:
    metadata = dict(existing or {})
    metadata.update(
        {
            "profile_promotion_source": "strict_canary",
            "strict_gate": "SUCCESS_nonempty_units_real_id_units_gt_0",
            "source_generation": str(source.generation),
            "source_sha256": source.sha256,
            "promoted_at_utc": datetime.now(UTC).isoformat(),
        }
    )
    return metadata


def promote_one(
    target_bucket: Any,
    target_prefix: str,
    source: FrozenProfile,
    *,
    existed_before: bool,
    expected_target_generation: int | None = None,
) -> dict[str, Any]:
    """Create or generation-merge one profile and hash-verify the result.

    ``expected_target_generation`` pins a reviewed overlap snapshot. The
    ordinary strict-canary promoter leaves it unset for backward-compatible
    behavior; identity-admitted promotion always supplies it.
    """
    from google.api_core.exceptions import NotFound, PreconditionFailed

    pid = source.property_id
    blob = target_bucket.blob(f"{target_prefix}{pid}.json")
    try:
        if not existed_before:
            blob.metadata = _metadata(None, source)
            blob.upload_from_string(
                source.raw,
                content_type="application/json",
                if_generation_match=0,
            )
            status = "created"
        else:
            blob.reload()
            generation_before = int(blob.generation or 0)
            if expected_target_generation is not None and generation_before != expected_target_generation:
                return {
                    "property_id": pid,
                    "status": "generation_conflict_or_missing",
                    "error": "TargetGenerationChangedSinceReview",
                    "expected_generation": expected_target_generation,
                    "actual_generation": generation_before,
                    "source_sha256": source.sha256,
                }
            target_raw = blob.download_as_bytes(if_generation_match=generation_before)
            target = ScrapeProfile.model_validate_json(target_raw)
            incoming = ScrapeProfile.model_validate_json(source.raw)
            if not merge_reusable_routes(target, incoming):
                return {
                    "property_id": pid,
                    "status": "overlap_unchanged",
                    "generation": generation_before,
                    "source_sha256": source.sha256,
                }
            merged = json.dumps(
                target.model_dump(mode="json"),
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            blob.metadata = _metadata(blob.metadata, source)
            blob.upload_from_string(
                merged,
                content_type="application/json",
                if_generation_match=generation_before,
            )
            status = "overlap_field_merged"

        blob.reload()
        generation_after = int(blob.generation or 0)
        stored = blob.download_as_bytes(if_generation_match=generation_after)
        ScrapeProfile.model_validate_json(stored)
        if status == "created" and sha256_bytes(stored) != source.sha256:
            raise RuntimeError(f"created_profile_hash_mismatch:{pid}")
        return {
            "property_id": pid,
            "status": status,
            "generation": generation_after,
            "source_sha256": source.sha256,
            "stored_sha256": sha256_bytes(stored),
        }
    except (PreconditionFailed, NotFound) as exc:
        return {
            "property_id": pid,
            "status": "generation_conflict_or_missing",
            "error": type(exc).__name__,
            "source_sha256": source.sha256,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="jugnu-494013")
    parser.add_argument("--cohort-csv", required=True, type=Path)
    parser.add_argument("--cohort-sha256", required=True)
    parser.add_argument("--cohort-count", required=True, type=int)
    parser.add_argument("--source-output-prefix", required=True)
    parser.add_argument("--source-profile-prefix", required=True)
    parser.add_argument("--target-profile-prefix", required=True)
    parser.add_argument("--expected-shards", required=True, type=int)
    parser.add_argument("--expected-output-rows", required=True, type=int)
    parser.add_argument("--expected-strict", required=True, type=int)
    parser.add_argument("--exclude-id", action="append", type=int, default=[])
    parser.add_argument("--snapshot-dir", required=True, type=Path)
    parser.add_argument("--expected-snapshot-manifest", type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from google.cloud import storage

    output_bucket_name, output_prefix = parse_gcs_uri(args.source_output_prefix)
    source_bucket_name, source_profile_prefix = parse_gcs_uri(args.source_profile_prefix)
    target_bucket_name, target_profile_prefix = parse_gcs_uri(args.target_profile_prefix)
    if output_bucket_name != source_bucket_name:
        raise RuntimeError("run_outputs_and_profiles_must_share_bucket")

    client = storage.Client(project=args.project)
    source_bucket = client.bucket(source_bucket_name)
    target_bucket = client.bucket(target_bucket_name)
    cohort_ids = load_cohort_ids(
        args.cohort_csv,
        expected_sha256=args.cohort_sha256,
        expected_count=args.cohort_count,
    )
    strict_ids = load_strict_ids(
        source_bucket,
        output_prefix,
        cohort_ids,
        expected_shards=args.expected_shards,
        expected_output_rows=args.expected_output_rows,
        expected_strict=args.expected_strict,
        excluded_ids=set(args.exclude_id),
    )
    frozen, bootstrap_only = freeze_profiles(
        source_bucket,
        source_profile_prefix,
        strict_ids,
    )
    verify_expected_snapshot(args.expected_snapshot_manifest, frozen)

    existing_before = immediate_numeric_profile_ids(
        target_bucket,
        target_profile_prefix,
    )
    actionable_ids = set(frozen)
    create_ids = actionable_ids - existing_before
    overlap_ids = actionable_ids & existing_before
    verify_expected_target_plan(
        args.expected_snapshot_manifest,
        actionable_ids,
        existing_before,
    )
    results: list[dict[str, Any]] = []
    if args.execute:
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = [
                executor.submit(
                    promote_one,
                    target_bucket,
                    target_profile_prefix,
                    frozen[pid],
                    existed_before=pid in overlap_ids,
                )
                for pid in sorted(actionable_ids)
            ]
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda row: int(row["property_id"]))

    manifest = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "mode": "execute" if args.execute else "dry_run",
        "project": args.project,
        "source_output_prefix": args.source_output_prefix,
        "source_profile_prefix": args.source_profile_prefix,
        "target_profile_prefix": args.target_profile_prefix,
        "cohort_csv": str(args.cohort_csv),
        "cohort_sha256": args.cohort_sha256,
        "cohort_properties": len(cohort_ids),
        "strict_success_properties": len(strict_ids),
        "strict_gate": "units_nonempty AND verdict_SUCCESS AND real_id_units_gt_0",
        "excluded_ids": sorted(set(args.exclude_id)),
        "actionable_profiles": len(actionable_ids),
        "bootstrap_only_profiles_not_promoted": len(bootstrap_only),
        "bootstrap_only_ids": sorted(bootstrap_only),
        "target_numeric_profiles_before": len(existing_before),
        "create_only_planned": len(create_ids),
        "overlap_field_merge_planned": len(overlap_ids),
        "create_ids": sorted(create_ids),
        "overlap_ids": sorted(overlap_ids),
        "source_profiles": {
            str(pid): {
                "generation": profile.generation,
                "sha256": profile.sha256,
                "route_signals": list(profile.route_signals),
            }
            for pid, profile in sorted(frozen.items())
        },
        "write_results": results,
        "write_status_counts": {
            status: sum(row.get("status") == status for row in results)
            for status in sorted({str(row.get("status")) for row in results})
        },
    }
    output = write_snapshot(args.snapshot_dir, frozen, manifest)
    print(
        json.dumps(
            {
                "mode": manifest["mode"],
                "strict_success_properties": manifest["strict_success_properties"],
                "actionable_profiles": manifest["actionable_profiles"],
                "bootstrap_only_profiles_not_promoted": manifest["bootstrap_only_profiles_not_promoted"],
                "create_only_planned": manifest["create_only_planned"],
                "overlap_field_merge_planned": manifest["overlap_field_merge_planned"],
                "write_status_counts": manifest["write_status_counts"],
                "manifest": str(output),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
