"""Reduce a reviewed promotion result to URL-free immutable GCS evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def build(
    promotion_manifest: Path,
    release_manifest: Path,
    output: Path,
    source_commit: str,
) -> dict[str, Any]:
    promotion_raw = promotion_manifest.read_bytes()
    promotion = json.loads(promotion_raw)
    release = json.loads(release_manifest.read_text(encoding="utf-8"))
    results = sorted(
        promotion.get("write_results") or [], key=lambda row: int(row["property_id"])
    )
    expected = int(release["scope"]["admitted_profiles"])
    if promotion.get("mode") != "execute" or len(results) != expected:
        raise RuntimeError("promotion_result_count_or_mode_mismatch")
    if (
        promotion.get("overlap_ids")
        or len(promotion.get("create_ids") or []) != expected
    ):
        raise RuntimeError("immutable_seed_was_not_create_only")
    if any(
        row.get("status") != "created"
        or row.get("source_sha256") != row.get("stored_sha256")
        or int(row.get("generation") or 0) <= 0
        for row in results
    ):
        raise RuntimeError("promotion_object_verification_failed")

    manifest = {
        "manifest_version": "strict-warm-profile-v3-gcs-release-v1",
        "source_commit": source_commit,
        "profile_set_sha256": release["validation"]["profile_set_sha256"],
        "target_profile_prefix": promotion["target_profile_prefix"],
        "gate": promotion["gate"],
        "published_at_utc": promotion["generated_at_utc"],
        "objects": [
            {
                "property_id": int(row["property_id"]),
                "generation": int(row["generation"]),
                "sha256": row["stored_sha256"],
            }
            for row in results
        ],
        "validation": {
            "candidate_profiles": expected,
            "create_only_objects": len(results),
            "overlap_objects": 0,
            "source_equals_stored_hashes": "PASS",
            "remote_numeric_object_set": "PASS",
            "promotion_manifest_sha256": _sha256(promotion_raw),
        },
    }
    output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--promotion-manifest", type=Path, required=True)
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            build(
                args.promotion_manifest,
                args.release_manifest,
                args.output,
                args.source_commit,
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
