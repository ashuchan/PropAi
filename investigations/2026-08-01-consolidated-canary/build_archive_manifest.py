"""Hash the curated worker archive without copying raw profile payloads."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "SHA256SUMS.json"


def main() -> None:
    files: dict[str, dict[str, int | str]] = {}
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path == OUTPUT:
            continue
        relative = path.relative_to(ROOT)
        if relative.parts[:2] == ("profile-snapshot", "profiles"):
            continue
        if any("_venv_accidental_" in part for part in relative.parts):
            continue
        raw = path.read_bytes()
        files[str(relative)] = {
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    payload = {
        "schema_version": "propai_worker_archive_hashes_v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "file_count": len(files),
        "files": files,
    }
    OUTPUT.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"hashed {len(files)} files -> {OUTPUT}")


if __name__ == "__main__":
    main()
