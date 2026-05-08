"""deploy_csv_sync.py — sync the committed properties.csv to GCS.

Called from the deploy workflow. Also runnable locally for testing.

Validates before uploading:
  - File exists and is readable
  - Has the expected header row
  - Non-empty rows
  - No duplicate canonical_ids

Uploads two objects:
  - ``property-list/properties.csv`` — the full CSV the nightly scrape reads.
  - ``canary/properties.csv``        — the first ``CANARY_ROWS`` rows,
    consumed by ``triggers/smoke.py`` for the deploy-time canary run.

Usage:
  python scripts/sync/csv_to_gcs.py --env staging
  python scripts/sync/csv_to_gcs.py --env prod --path properties.csv
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import tempfile
from pathlib import Path

# Canonical header for the property-list CSV
EXPECTED_HEADER = ["canonical_id", "url", "pms_hint"]


def validate(path: Path) -> None:
    if not path.exists():
        sys.exit(f"CSV not found: {path}")
    if not path.is_file():
        sys.exit(f"Not a file: {path}")

    with path.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)

    # Accept the canonical header or common variants
    if header != EXPECTED_HEADER:
        # Also accept header-less or extended CSVs as long as first column looks like an ID
        if not header or (
            header[0].lower() not in ("canonical_id", "id", "property_id", "unique_id", "apartmentid")
        ):
            sys.exit(f"CSV header mismatch.\n  Expected: {EXPECTED_HEADER}\n  Got:      {header}")
        print(
            f"WARNING: CSV header {header!r} differs from canonical {EXPECTED_HEADER!r}; proceeding",
            file=sys.stderr,
        )

    # Second pass: count rows and check for duplicate canonical_ids
    with path.open(newline="") as f:
        dict_reader = csv.DictReader(f)
        seen: set[str] = set()
        n = 0
        for row_num, row in enumerate(dict_reader, start=2):
            # Skip blank rows
            if not any(row.values()):
                continue
            # Use first column value as the identifier
            first_col = next(iter(row.values()), "")
            if first_col in seen:
                sys.exit(f"Duplicate canonical_id '{first_col}' at row {row_num}")
            seen.add(first_col)
            n += 1

    if n == 0:
        sys.exit("CSV has zero data rows")

    print(f"✓ {n} properties, no duplicates", file=sys.stderr)


# CI passes the short env name ("prod"/"staging") to match image-tag
# conventions, but the GCS bucket is named from the TF var `env`, which
# is "production" in prod.tfvars. Mirrors _trigger_common.tf_env_for —
# kept local here to avoid pulling the CLI shim into a simple gsutil
# script. Keep in sync with infra/terraform/envs/*.tfvars.
_TF_ENV: dict[str, str] = {"staging": "staging", "prod": "production"}


# Number of data rows (excluding header) pushed into canary/properties.csv.
# trigger_smoke.py sets LIMIT=3 on the smoke run, so 5 is a conservative
# buffer that keeps the canary small without risk of a bad-row shadow.
CANARY_ROWS = 5


def _gsutil_cp(src: Path, dest_uri: str) -> None:
    subprocess.check_call(
        [
            "gsutil",
            "-h",
            "Cache-Control:no-cache",
            "cp",
            str(src),
            dest_uri,
        ]
    )
    print(f"✓ uploaded to {dest_uri}", file=sys.stderr)


def upload(path: Path, env: str) -> None:
    tf_env = _TF_ENV.get(env, env)
    bucket = f"gs://jugnu-raw-{tf_env}"
    _gsutil_cp(path, f"{bucket}/property-list/properties.csv")

    # Also refresh the canary file that trigger_smoke.py reads. The smoke
    # job fails with 404 at fetch time if this object is missing, so it
    # has to be written on every deploy — not just once by hand.
    with tempfile.NamedTemporaryFile(
        mode="w", newline="", suffix=".csv", delete=False
    ) as tmp:
        canary_path = Path(tmp.name)
        try:
            with path.open(newline="") as src:
                reader = csv.reader(src)
                writer = csv.writer(tmp)
                # Preserve the header exactly as-is.
                header = next(reader, None)
                if header is not None:
                    writer.writerow(header)
                for i, row in enumerate(reader):
                    if i >= CANARY_ROWS:
                        break
                    writer.writerow(row)
            tmp.flush()
            tmp.close()
            _gsutil_cp(canary_path, f"{bucket}/canary/properties.csv")
        finally:
            canary_path.unlink(missing_ok=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Sync property-list CSV to GCS.")
    p.add_argument("--env", choices=["staging", "prod"], required=True)
    p.add_argument(
        "--path",
        default="properties.csv",
        help="Local CSV path (default: properties.csv at repo root)",
    )
    args = p.parse_args()
    csv_path = Path(args.path)
    validate(csv_path)
    upload(csv_path, args.env)


if __name__ == "__main__":
    main()
