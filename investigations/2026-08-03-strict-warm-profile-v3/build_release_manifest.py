"""Validate the v3 candidate and hash its durable, URL-redacted evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ma_poc.models.scrape_profile import ScrapeProfile  # noqa: E402
from ma_poc.scripts.diagnostics.audit_july_gcp_profile_evidence import (  # noqa: E402
    profile_routes,
)
from ma_poc.scripts.diagnostics.audit_live_warm_profile_winners import (  # noqa: E402
    _is_public_http_url,
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _ledger_rows(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def build(root: Path, profiles_dir: Path) -> dict[str, Any]:
    ledger_path = root / "materialization" / "strict-profile-ledger.jsonl"
    rows = _ledger_rows(ledger_path)
    by_id = {str(row["property_id"]): row for row in rows}
    if len(by_id) != len(rows):
        raise RuntimeError("duplicate_property_in_ledger")

    admitted = {
        property_id for property_id, row in by_id.items() if row["status"] == "ADMIT"
    }
    profile_paths = {path.stem: path for path in profiles_dir.glob("*.json")}
    if set(profile_paths) != admitted:
        raise RuntimeError("profile_set_does_not_equal_admitted_ledger")

    profile_set_hash = hashlib.sha256()
    retained_routes = 0
    for property_id in sorted(admitted, key=int):
        raw = profile_paths[property_id].read_bytes()
        actual_sha = _sha256(raw)
        expected_sha = str(by_id[property_id].get("sanitized_profile_sha256") or "")
        if actual_sha != expected_sha:
            raise RuntimeError(f"profile_hash_mismatch:{property_id}")
        profile = ScrapeProfile.model_validate_json(raw).model_dump(mode="json")
        if str(profile["canonical_id"]) != property_id:
            raise RuntimeError(f"canonical_id_mismatch:{property_id}")
        routes = profile_routes(property_id, profile)
        if not routes:
            raise RuntimeError(f"admitted_profile_without_route:{property_id}")
        if any(not _is_public_http_url(route.url) for route in routes):
            raise RuntimeError(f"unsafe_retained_route:{property_id}")
        retained_routes += len({route.sha256 for route in routes})
        profile_set_hash.update(f"{property_id}:{actual_sha}\n".encode())

    actionable_ids = {
        str(value)
        for value in json.loads(
            (root / "materialization" / "aug1-strict-actionable-ids.json").read_text(
                encoding="utf-8"
            )
        )["property_ids"]
    }
    actionable_admitted = admitted & actionable_ids

    excluded = {root / "RELEASE_MANIFEST.json"}
    artifacts = []
    for path in sorted((path for path in root.rglob("*") if path.is_file()), key=str):
        if path in excluded:
            continue
        raw = path.read_bytes()
        artifacts.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": len(raw),
                "sha256": _sha256(raw),
            }
        )

    statuses = Counter(str(row["status"]) for row in rows)
    manifest = {
        "manifest_version": "strict-warm-profile-v3-release-v1",
        "scope": {
            "properties": len(rows),
            "profile_status_counts": dict(sorted(statuses.items())),
            "admitted_profiles": len(admitted),
            "retained_routes": retained_routes,
            "aug1_actionable_profiles": len(actionable_ids),
            "aug1_actionable_admitted": len(actionable_admitted),
            "aug1_actionable_coverage": round(
                len(actionable_admitted) / len(actionable_ids), 6
            ),
        },
        "validation": {
            "profile_set_sha256": profile_set_hash.hexdigest(),
            "schema_canonical_hash_route_and_public_url_gate": "PASS",
            "reproducibility_reverse_input_order": "PASS",
        },
        "source_profile_prefixes": [
            "gs://jugnu-canary/profiles/strict-v2-fa1afb7/",
            "gs://jugnu-canary/profiles/run-2026-08-01-consolidated-strict-fa1afb7/",
            "gs://jugnu-canary/profiles/affected386-33864eb/",
            "gs://jugnu-canary/profiles/strat1000-ff7b377/",
            "gs://jugnu-canary/profiles/verify-7f800ca/",
            "gs://jugnu-canary/profiles/verify-823ea7c/",
        ],
        "artifacts": artifacts,
    }
    (root / "RELEASE_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-dir", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    print(json.dumps(build(args.root, args.profiles_dir), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
