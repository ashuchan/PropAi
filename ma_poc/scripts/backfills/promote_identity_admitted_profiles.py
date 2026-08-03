"""Promote locally materialized, identity-admitted warm profiles to GCS.

This is the write companion to ``materialize_strict_warm_profiles``.  It
accepts only profiles whose ledger row is ``ADMIT`` and whose serialized hash
matches ``sanitized_profile_sha256``.  A dry run freezes both the candidate and
every overlapping target object.  Execution requires that reviewed manifest,
revalidates all target generations before the first write, and then delegates
to the existing create-or-field-merge writer.

The snapshot's ``target-before/`` directory plus ``create_ids`` is a complete
rollback input: restore the pinned overlap objects and remove only the listed
objects that this execution created.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.scripts.backfills.promote_strict_canary_profiles import (
    FrozenProfile,
    immediate_numeric_profile_ids,
    parse_gcs_uri,
    promote_one,
    reusable_route_signals,
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _ledger_rows(path: Path) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        property_id = int(row["property_id"])
        if property_id in rows:
            raise RuntimeError(f"duplicate_candidate_ledger_property:{property_id}")
        rows[property_id] = row
    return rows


def freeze_local_candidate(
    profiles_dir: Path,
    ledger_path: Path,
) -> dict[int, FrozenProfile]:
    ledger = _ledger_rows(ledger_path)
    admitted_ids = {property_id for property_id, row in ledger.items() if row.get("status") == "ADMIT"}
    frozen: dict[int, FrozenProfile] = {}
    for path in sorted(profiles_dir.glob("*.json"), key=lambda item: int(item.stem)):
        property_id = int(path.stem)
        row = ledger.get(property_id)
        if row is None or row.get("status") != "ADMIT":
            raise RuntimeError(f"profile_without_admit_ledger:{property_id}")
        raw = path.read_bytes()
        actual_sha = _sha256(raw)
        expected_sha = str(row.get("sanitized_profile_sha256") or "")
        if actual_sha != expected_sha:
            raise RuntimeError(
                f"candidate_profile_sha256_mismatch:{property_id}:expected={expected_sha}:actual={actual_sha}"
            )
        profile = ScrapeProfile.model_validate_json(raw)
        if str(profile.canonical_id) != str(property_id):
            raise RuntimeError(f"candidate_canonical_id_mismatch:{property_id}")
        signals = reusable_route_signals(profile)
        if not signals:
            raise RuntimeError(f"admitted_profile_has_no_replay_route:{property_id}")
        frozen[property_id] = FrozenProfile(
            property_id=property_id,
            raw=raw,
            generation=0,
            sha256=actual_sha,
            route_signals=signals,
        )
    missing = admitted_ids - set(frozen)
    if missing:
        raise RuntimeError(
            "admitted_ledger_profile_missing:" + ",".join(str(value) for value in sorted(missing))
        )
    return frozen


def freeze_target_before(
    bucket: Any,
    prefix: str,
    property_ids: set[int],
    output_dir: Path,
) -> dict[int, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)

    def freeze(property_id: int) -> tuple[int, dict[str, Any], bytes]:
        blob = bucket.blob(f"{prefix}{property_id}.json")
        blob.reload()
        generation = int(blob.generation or 0)
        raw = blob.download_as_bytes(if_generation_match=generation)
        profile = ScrapeProfile.model_validate_json(raw)
        if str(profile.canonical_id) != str(property_id):
            raise RuntimeError(f"target_canonical_id_mismatch:{property_id}")
        return property_id, {"generation": generation, "sha256": _sha256(raw)}, raw

    frozen: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(freeze, property_id) for property_id in sorted(property_ids)]
        for future in as_completed(futures):
            property_id, metadata, raw = future.result()
            frozen[property_id] = metadata
            (output_dir / f"{property_id}.json").write_bytes(raw)
    return frozen


def verify_reviewed_candidate(
    manifest_path: Path,
    frozen: dict[int, FrozenProfile],
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = {
        str(property_id): {
            "sha256": profile.sha256,
            "route_signals": list(profile.route_signals),
        }
        for property_id, profile in sorted(frozen.items())
    }
    if actual != (manifest.get("source_profiles") or {}):
        raise RuntimeError("identity_candidate_changed_since_review")
    return manifest


def verify_target_before(
    bucket: Any,
    prefix: str,
    manifest: dict[str, Any],
    candidate_ids: set[int],
) -> tuple[set[int], set[int]]:
    existing = immediate_numeric_profile_ids(bucket, prefix)
    create_ids = candidate_ids - existing
    overlap_ids = candidate_ids & existing
    expected_create = {int(value) for value in manifest.get("create_ids") or []}
    expected_overlap = {int(value) for value in manifest.get("overlap_ids") or []}
    if (create_ids, overlap_ids) != (expected_create, expected_overlap):
        raise RuntimeError("target_profile_plan_changed_since_review")
    expected_targets = manifest.get("target_before") or {}
    for property_id in sorted(overlap_ids):
        blob = bucket.blob(f"{prefix}{property_id}.json")
        blob.reload()
        generation = int(blob.generation or 0)
        expected = expected_targets.get(str(property_id)) or {}
        if generation != int(expected.get("generation") or 0):
            raise RuntimeError(f"target_generation_changed_since_review:{property_id}")
        raw = blob.download_as_bytes(if_generation_match=generation)
        if _sha256(raw) != expected.get("sha256"):
            raise RuntimeError(f"target_hash_changed_since_review:{property_id}")
    return create_ids, overlap_ids


def _source_manifest(frozen: dict[int, FrozenProfile]) -> dict[str, dict[str, Any]]:
    return {
        str(property_id): {
            "sha256": profile.sha256,
            "route_signals": list(profile.route_signals),
        }
        for property_id, profile in sorted(frozen.items())
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="jugnu-494013")
    parser.add_argument("--profiles-dir", required=True, type=Path)
    parser.add_argument("--identity-ledger", required=True, type=Path)
    parser.add_argument("--target-profile-prefix", required=True)
    parser.add_argument("--snapshot-dir", required=True, type=Path)
    parser.add_argument("--expected-snapshot-manifest", type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from google.cloud import storage

    bucket_name, target_prefix = parse_gcs_uri(args.target_profile_prefix)
    bucket = storage.Client(project=args.project).bucket(bucket_name)
    frozen = freeze_local_candidate(args.profiles_dir, args.identity_ledger)
    candidate_ids = set(frozen)
    args.snapshot_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    if args.execute:
        if args.expected_snapshot_manifest is None:
            raise RuntimeError("execute_requires_expected_snapshot_manifest")
        reviewed = verify_reviewed_candidate(args.expected_snapshot_manifest, frozen)
        create_ids, overlap_ids = verify_target_before(
            bucket,
            target_prefix,
            reviewed,
            candidate_ids,
        )
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = [
                executor.submit(
                    promote_one,
                    bucket,
                    target_prefix,
                    frozen[property_id],
                    existed_before=property_id in overlap_ids,
                    expected_target_generation=(
                        int((reviewed.get("target_before") or {})[str(property_id)]["generation"])
                        if property_id in overlap_ids
                        else None
                    ),
                )
                for property_id in sorted(candidate_ids)
            ]
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda row: int(row["property_id"]))
        failed = [
            row
            for row in results
            if row.get("status") not in {"created", "overlap_field_merged", "overlap_unchanged"}
        ]
        if failed:
            raise RuntimeError(f"profile_promotion_incomplete:{len(failed)}")
        target_before = reviewed.get("target_before") or {}
        manifest_name = "promotion_manifest_execute.json"
    else:
        existing = immediate_numeric_profile_ids(bucket, target_prefix)
        create_ids = candidate_ids - existing
        overlap_ids = candidate_ids & existing
        target_before = {
            str(property_id): metadata
            for property_id, metadata in sorted(
                freeze_target_before(
                    bucket,
                    target_prefix,
                    overlap_ids,
                    args.snapshot_dir / "target-before",
                ).items()
            )
        }
        manifest_name = "promotion_manifest.json"

    # Retain the exact candidate beside the reviewed target snapshot.
    candidate_snapshot = args.snapshot_dir / "profiles"
    candidate_snapshot.mkdir(parents=True, exist_ok=True)
    for property_id, profile in sorted(frozen.items()):
        (candidate_snapshot / f"{property_id}.json").write_bytes(profile.raw)

    manifest = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "mode": "execute" if args.execute else "dry_run",
        "gate": "identity_materializer_status_ADMIT_and_sanitized_sha256_match",
        "project": args.project,
        "target_profile_prefix": args.target_profile_prefix,
        "identity_ledger": str(args.identity_ledger.resolve()),
        "candidate_profiles": len(frozen),
        "create_ids": sorted(create_ids),
        "overlap_ids": sorted(overlap_ids),
        "source_profiles": _source_manifest(frozen),
        "target_before": target_before,
        "rollback": {
            "restore_overlap_from": "target-before/",
            "remove_created_ids": sorted(create_ids),
        },
        "write_results": results,
        "write_status_counts": dict(sorted(Counter(str(row.get("status")) for row in results).items())),
    }
    output = args.snapshot_dir / manifest_name
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "mode": manifest["mode"],
                "candidate_profiles": len(frozen),
                "create_only_planned": len(create_ids),
                "overlap_field_merge_planned": len(overlap_ids),
                "write_status_counts": manifest["write_status_counts"],
                "manifest": str(output),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
