#!/usr/bin/env python3
"""Build a deterministic manifest for the retained campaign analysis files.

Only Git-indexed files are admitted. This intentionally excludes the ignored
one-time GCP mirrors, raw profiles, bytecode, local databases, and credentials.
The manifest excludes itself so its digest does not recurse.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).with_name("ANALYSIS_ARTIFACT_MANIFEST.json")
ROOTS = (
    "investigations/2026-08-01-availability-date",
    "investigations/2026-08-01-failed-no-data",
    "investigations/2026-08-01-consolidated-canary",
    "investigations/2026-08-02-stratified-1000",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    command = ["git", "ls-files", "--", *ROOTS]
    listed = subprocess.run(
        command,
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    output_relative = OUTPUT.relative_to(REPO).as_posix()
    paths = sorted(path for path in listed if path != output_relative)

    records: list[dict[str, object]] = []
    by_root: Counter[str] = Counter()
    total_bytes = 0
    for relative in paths:
        path = REPO / relative
        if not path.is_file():
            raise FileNotFoundError(path)
        size = path.stat().st_size
        root = next((candidate for candidate in ROOTS if relative.startswith(candidate + "/")), "other")
        by_root[root] += 1
        total_bytes += size
        records.append(
            {
                "bytes": size,
                "path": relative,
                "sha256": sha256(path),
            }
        )

    payload = {
        "manifest_version": "propai_campaign_analysis_artifacts_v1",
        "scope": {
            "file_count": len(records),
            "roots": {root: by_root[root] for root in ROOTS},
            "total_bytes": total_bytes,
        },
        "exclusions": [
            "this manifest (to avoid recursive hashing)",
            "git-ignored one-time GCP run mirrors",
            "raw profiles and credential-bearing payloads",
            "bytecode, local databases, dependency directories, and caches",
        ],
        "files": records,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload["scope"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
