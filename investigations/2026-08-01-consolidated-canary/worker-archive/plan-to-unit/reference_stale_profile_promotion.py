from __future__ import annotations

import argparse
import csv
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage
from ma_poc.models.scrape_profile import ScrapeProfile


ROOT = Path("/private/tmp/propai-plan60.UpxU1A")
COHORT = ROOT / "plan60_549.csv"
EXPECTED_COHORT_SHA256 = (
    "b40f11a8329c751e6d1ba4bf7eda16e8139eb3422c2acb3de0856dae8755e0c8"
)
BUCKET = "jugnu-canary"
RUN_OUTPUT_PREFIX = "runs/2026-08-01-plan60-full549-v2/"
RUN_PROFILE_PREFIX = "profiles/plan60-full549-v2/"
MAIN_PROFILE_PREFIX = "profiles/"
EXPLICIT_SHAPE_OVERCOUNTS = {18684, 30734, 218853}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def property_id(row: dict) -> int:
    raw = row.get("apartment_id") or row.get("apartmentid") or row.get(
        "property_id"
    )
    return int(raw)


def strict_success(row: dict) -> bool:
    units = row.get("units")
    meta = row.get("_meta") or {}
    provenance = meta.get("provenance") or {}
    quality = provenance.get("data_quality") or {}
    return bool(
        isinstance(units, list)
        and units
        and meta.get("verdict") == "SUCCESS"
        and int(quality.get("real_id_units") or 0) > 0
    )


def actionable(profile: dict) -> bool:
    navigation = profile.get("navigation") or {}
    api_hints = profile.get("api_hints") or {}
    dom_hints = profile.get("dom_hints") or {}
    return bool(
        navigation.get("winning_page_url")
        or navigation.get("availability_page_path")
        or api_hints.get("known_endpoints")
        or api_hints.get("widget_endpoints")
        or dom_hints.get("platform_detected")
    )


def load_cohort_ids() -> set[int]:
    if hashlib.sha256(COHORT.read_bytes()).hexdigest() != EXPECTED_COHORT_SHA256:
        raise RuntimeError("exact 549 cohort hash mismatch")
    with COHORT.open(encoding="utf-8-sig", newline="") as handle:
        ids = {property_id(row) for row in csv.DictReader(handle)}
    if len(ids) != 549:
        raise RuntimeError(f"expected 549 unique cohort IDs, found {len(ids)}")
    return ids


def load_strict_ids(bucket: storage.Bucket, cohort_ids: set[int]) -> set[int]:
    blobs = sorted(
        (
            blob
            for blob in bucket.list_blobs(prefix=RUN_OUTPUT_PREFIX)
            if blob.name.endswith("/properties.json")
        ),
        key=lambda blob: blob.name,
    )
    if len(blobs) != 98:
        raise RuntimeError(f"expected 98 completed shard outputs, found {len(blobs)}")

    def download(blob: storage.Blob) -> tuple[str, list[dict]]:
        payload = json.loads(blob.download_as_bytes())
        if not isinstance(payload, list):
            raise RuntimeError(f"non-list shard payload: {blob.name}")
        return blob.name, [row for row in payload if isinstance(row, dict)]

    properties: list[dict] = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(download, blob) for blob in blobs]
        for future in as_completed(futures):
            _, rows = future.result()
            properties.extend(rows)

    ids = [property_id(row) for row in properties]
    if len(ids) != 538 or len(set(ids)) != 538:
        raise RuntimeError(
            f"unexpected output cardinality rows={len(ids)} unique={len(set(ids))}"
        )
    if not set(ids).issubset(cohort_ids):
        raise RuntimeError("run output contains an out-of-cohort property")
    strict = {property_id(row) for row in properties if strict_success(row)}
    if len(strict) != 303:
        raise RuntimeError(f"strict SUCCESS/real-ID gate returned {len(strict)}, not 303")
    if strict & EXPLICIT_SHAPE_OVERCOUNTS:
        raise RuntimeError("shape overcount unexpectedly passed corrected strict gate")
    return strict


def load_actionable_profiles(
    bucket: storage.Bucket, strict_ids: set[int]
) -> tuple[dict[int, bytes], dict[int, str], set[int]]:
    profile_bytes: dict[int, bytes] = {}
    hashes: dict[int, str] = {}
    bootstrap_only: set[int] = set()
    for pid in sorted(strict_ids):
        blob = bucket.blob(f"{RUN_PROFILE_PREFIX}{pid}.json")
        raw = blob.download_as_bytes()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError(f"profile {pid} is not a JSON object")
        if str(payload.get("canonical_id") or "") != str(pid):
            raise RuntimeError(f"profile canonical_id mismatch for {pid}")
        ScrapeProfile.model_validate(payload)
        if actionable(payload):
            profile_bytes[pid] = raw
            hashes[pid] = sha256_bytes(raw)
        else:
            bootstrap_only.add(pid)
    if len(profile_bytes) != 215 or len(bootstrap_only) != 88:
        raise RuntimeError(
            "expected 215 actionable and 88 bootstrap-only strict profiles, got "
            f"{len(profile_bytes)} and {len(bootstrap_only)}"
        )
    return profile_bytes, hashes, bootstrap_only


def main_profile_ids(bucket: storage.Bucket) -> set[int]:
    ids: set[int] = set()
    for blob in bucket.list_blobs(prefix=MAIN_PROFILE_PREFIX, delimiter="/"):
        relative = blob.name.removeprefix(MAIN_PROFILE_PREFIX)
        if relative.endswith(".json") and relative[:-5].isdigit():
            ids.add(int(relative[:-5]))
    return ids


def upload_create_only(
    bucket: storage.Bucket,
    pid: int,
    raw: bytes,
    expected_sha256: str,
    promoted_at: str,
) -> dict[str, object]:
    blob = bucket.blob(f"{MAIN_PROFILE_PREFIX}{pid}.json")
    blob.metadata = {
        "profile_promotion_source": "plan60-full549-v2",
        "strict_gate": "SUCCESS_nonempty_units_real_id_units_gt_0",
        "source_sha256": expected_sha256,
        "promoted_at_utc": promoted_at,
    }
    try:
        blob.upload_from_string(
            raw,
            content_type="application/json",
            if_generation_match=0,
        )
    except PreconditionFailed:
        return {"property_id": pid, "status": "generation_conflict_not_overwritten"}
    blob.reload()
    downloaded = blob.download_as_bytes(if_generation_match=blob.generation)
    actual_sha256 = sha256_bytes(downloaded)
    if actual_sha256 != expected_sha256:
        raise RuntimeError(f"post-upload hash mismatch for {pid}")
    return {
        "property_id": pid,
        "status": "created_and_hash_verified",
        "generation": int(blob.generation or 0),
        "source_sha256": expected_sha256,
        "stored_sha256": actual_sha256,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    client = storage.Client(project="jugnu-494013")
    bucket = client.bucket(BUCKET)
    cohort_ids = load_cohort_ids()
    strict_ids = load_strict_ids(bucket, cohort_ids)
    profiles, hashes, bootstrap_only = load_actionable_profiles(bucket, strict_ids)
    existing_before = main_profile_ids(bucket)
    actionable_ids = set(profiles)
    new_ids = actionable_ids - existing_before
    overlap_ids = actionable_ids & existing_before
    if len(new_ids) != 206 or len(overlap_ids) != 9:
        raise RuntimeError(
            f"expected 206 create-only and 9 overlaps, got {len(new_ids)} and "
            f"{len(overlap_ids)}"
        )

    promoted_at = datetime.now(timezone.utc).isoformat()
    results: list[dict[str, object]] = []
    if args.execute:
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = {
                executor.submit(
                    upload_create_only,
                    bucket,
                    pid,
                    profiles[pid],
                    hashes[pid],
                    promoted_at,
                ): pid
                for pid in sorted(new_ids)
            }
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda row: int(row["property_id"]))

    created = {
        int(row["property_id"])
        for row in results
        if row.get("status") == "created_and_hash_verified"
    }
    conflicts = {
        int(row["property_id"])
        for row in results
        if row.get("status") == "generation_conflict_not_overwritten"
    }
    existing_after = main_profile_ids(bucket) if args.execute else existing_before
    if args.execute and not created.union(conflicts) == new_ids:
        raise RuntimeError("upload result partition does not match planned new IDs")
    if args.execute and not created.issubset(existing_after):
        raise RuntimeError("created profile missing from post-write main listing")

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "execute" if args.execute else "dry_run",
        "project": "jugnu-494013",
        "bucket": BUCKET,
        "source_profile_prefix": f"gs://{BUCKET}/{RUN_PROFILE_PREFIX}",
        "target_profile_prefix": f"gs://{BUCKET}/{MAIN_PROFILE_PREFIX}",
        "run_output_prefix": f"gs://{BUCKET}/{RUN_OUTPUT_PREFIX}",
        "cohort_csv": str(COHORT),
        "cohort_sha256": EXPECTED_COHORT_SHA256,
        "cohort_properties": len(cohort_ids),
        "strict_success_properties": len(strict_ids),
        "strict_gate": "units_nonempty AND verdict_SUCCESS AND real_id_units_gt_0",
        "explicit_shape_overcounts_excluded": sorted(EXPLICIT_SHAPE_OVERCOUNTS),
        "actionable_profiles": len(actionable_ids),
        "bootstrap_only_profiles_not_promoted": len(bootstrap_only),
        "main_numeric_profiles_before": len(existing_before),
        "create_only_planned": len(new_ids),
        "overlaps_not_overwritten": len(overlap_ids),
        "overlap_ids": sorted(overlap_ids),
        "created_and_hash_verified": len(created),
        "generation_conflicts_not_overwritten": len(conflicts),
        "main_numeric_profiles_after": len(existing_after),
        "created_ids": sorted(created),
        "conflict_ids": sorted(conflicts),
        "bootstrap_only_ids": sorted(bootstrap_only),
        "source_profile_sha256": {
            str(pid): hashes[pid] for pid in sorted(actionable_ids)
        },
        "upload_results": results,
    }
    output = ROOT / (
        "strict_actionable_profile_promotion_execute.json"
        if args.execute
        else "strict_actionable_profile_promotion_dry_run.json"
    )
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                key: manifest[key]
                for key in (
                    "mode",
                    "strict_success_properties",
                    "actionable_profiles",
                    "bootstrap_only_profiles_not_promoted",
                    "main_numeric_profiles_before",
                    "create_only_planned",
                    "overlaps_not_overwritten",
                    "created_and_hash_verified",
                    "generation_conflicts_not_overwritten",
                    "main_numeric_profiles_after",
                )
            },
            indent=2,
        )
    )
    print(f"manifest={output}")


if __name__ == "__main__":
    main()
