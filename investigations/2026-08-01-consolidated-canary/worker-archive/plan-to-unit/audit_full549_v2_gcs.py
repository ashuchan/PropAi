from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path("/private/tmp/propai-plan60.UpxU1A")
PREFIX = "gs://jugnu-canary/runs/2026-08-01-plan60-full549-v2"
COHORT = ROOT / "plan60_549.csv"
OUTPUT = ROOT / "full549_v2_strict_output_audit.json"


def run(*args: str) -> str:
    result = subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.stdout


def fetch_json(uri: str) -> tuple[str, object]:
    return uri, json.loads(run("gcloud", "storage", "cat", uri))


def property_id(row: dict) -> int | None:
    raw = row.get("apartment_id") or row.get("apartmentid") or row.get("property_id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def source_ids(row: dict) -> dict:
    value = row.get("source_ids")
    return value if isinstance(value, dict) else {}


def is_real_unit_anchor(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("is_floor_plan_level") is True:
        return False
    flags = str(row.get("data_quality_flag") or "").upper()
    if "PLAN_LEVEL_NO_UNIT_ANCHOR" in flags:
        return False
    candidates = [
        row.get("unit_id"),
        row.get("unit_name"),
        row.get("unit_number"),
        row.get("unit_id_raw"),
        row.get("unit_name_raw"),
        row.get("unit_number_raw"),
        *source_ids(row).values(),
    ]
    for raw in candidates:
        value = str(raw or "").strip()
        if not value:
            continue
        if value.casefold().startswith(("inferred_", "plan_", "floorplan_")):
            continue
        return True
    return False


def strict_units(prop: dict) -> list[dict]:
    units = prop.get("units")
    if not isinstance(units, list):
        return []
    return [row for row in units if isinstance(row, dict) and is_real_unit_anchor(row)]


def main() -> None:
    with COHORT.open(encoding="utf-8-sig", newline="") as handle:
        cohort_rows = list(csv.DictReader(handle))
    cohort_ids = {
        int(row.get("apartmentid") or row.get("apartment_id") or row.get("property_id"))
        for row in cohort_rows
    }

    uris = [
        line.strip()
        for line in run(
            "gcloud",
            "storage",
            "ls",
            f"{PREFIX}/shard_*/properties.json",
        ).splitlines()
        if line.strip()
    ]
    payloads: dict[str, object] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(fetch_json, uri): uri for uri in uris}
        for future in as_completed(futures):
            uri = futures[future]
            try:
                fetched_uri, value = future.result()
                payloads[fetched_uri] = value
            except Exception as exc:  # noqa: BLE001
                errors[uri] = f"{type(exc).__name__}: {str(exc)[:300]}"

    properties: list[dict] = []
    shard_rows: dict[str, int] = {}
    for uri, payload in payloads.items():
        rows = payload if isinstance(payload, list) else []
        valid_rows = [row for row in rows if isinstance(row, dict)]
        properties.extend(valid_rows)
        shard_match = re.search(r"/shard_(\d+)/", uri)
        shard_rows[shard_match.group(1) if shard_match else uri] = len(valid_rows)

    ids = [pid for prop in properties if (pid := property_id(prop)) is not None]
    id_counts = Counter(ids)
    by_id = {
        pid: prop
        for prop in properties
        if (pid := property_id(prop)) is not None and pid in cohort_ids
    }
    strict_by_id = {
        pid: strict_units(prop)
        for pid, prop in by_id.items()
        if strict_units(prop)
    }
    nonempty_units = {
        pid
        for pid, prop in by_id.items()
        if isinstance(prop.get("units"), list) and prop.get("units")
    }
    audit = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_prefix": PREFIX,
        "cohort_csv": str(COHORT),
        "cohort_csv_sha256": hashlib.sha256(COHORT.read_bytes()).hexdigest(),
        "cohort_unique_ids": len(cohort_ids),
        "properties_json_objects": len(uris),
        "properties_json_downloaded": len(payloads),
        "download_errors": errors,
        "shard_row_counts": dict(sorted(shard_rows.items(), key=lambda kv: int(kv[0]))),
        "output_property_rows": len(properties),
        "output_unique_ids": len(set(ids)),
        "output_unique_cohort_ids": len(by_id),
        "missing_cohort_ids": sorted(cohort_ids - set(by_id)),
        "extra_output_ids": sorted(set(ids) - cohort_ids),
        "duplicate_output_ids": {
            str(pid): count for pid, count in sorted(id_counts.items()) if count != 1
        },
        "properties_with_any_units_array_rows": len(nonempty_units),
        "strict_native_unit_properties": len(strict_by_id),
        "strict_native_unit_percent_of_549": round(len(strict_by_id) / 549 * 100, 4),
        "strict_native_unit_ids": sorted(strict_by_id),
        "strict_unit_row_count": sum(len(rows) for rows in strict_by_id.values()),
        "properties_with_nonempty_units_but_no_strict_anchor": sorted(nonempty_units - set(strict_by_id)),
        "verdict_counts_from_shape": {
            "strict_unit_level": len(strict_by_id),
            "non_strict_or_no_units": len(by_id) - len(strict_by_id),
            "missing_output": len(cohort_ids - set(by_id)),
        },
    }
    OUTPUT.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
    print(json.dumps(audit, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
