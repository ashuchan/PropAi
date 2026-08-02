"""Convert strict worker recovery ledgers into route-identity evidence.

Worker ledgers certify property-scoped native rows, but warm-profile admission
is route-specific.  This command joins those ledgers to production-shaped run
profiles and marks a route ``MATCH`` only when:

* the ledger explicitly records property identity, native identity and
  positive-rent rows;
* its contamination verdict is a pass;
* the referenced evidence artifact is present in the archived worker bundle;
* the profile route is equivalent to an exact recorded unit-producing URL.

No unit IDs, rents, endpoint URLs, or response bodies are written.  Unmatched
routes remain ``UNKNOWN`` and missing evidence artifacts are withheld.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ma_poc.scripts.diagnostics.audit_july_gcp_profile_evidence import (
    MATCH,
    profile_routes,
    route_equivalent,
    safe_route_record,
)

_AUDIT_VERSION = "worker-strict-route-evidence-v1"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


def _positive_int(value: Any) -> bool:
    try:
        return int(str(value or "0")) > 0
    except ValueError:
        return False


def _read_rows(paths: list[Path]) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for path in sorted(paths, key=lambda item: str(item.resolve())):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                property_id = str(row.get("property_id") or "").strip()
                if not property_id:
                    raise RuntimeError(f"missing_property_id:{path}")
                if property_id in rows:
                    raise RuntimeError(f"duplicate_property_id:{property_id}")
                rows[property_id] = row
    return rows


def _artifact_index(root: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for path in root.rglob("*"):
        if path.is_file():
            index.setdefault(path.name, []).append(path)
    return index


def _artifact_for(
    row: dict[str, str],
    artifacts: dict[str, list[Path]],
) -> tuple[Path, str] | None:
    basename = Path(str(row.get("artifact") or "")).name
    matches = artifacts.get(basename) or []
    if len(matches) != 1:
        return None
    path = matches[0]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = str(row.get("artifact_sha256") or "").strip()
    if expected and digest != expected:
        raise RuntimeError(f"artifact_sha256_mismatch:{row.get('property_id')}:{basename}")
    return path, digest


def _source_urls(row: dict[str, str]) -> list[str]:
    raw = str(row.get("source_urls") or row.get("source_url") or "")
    return [value.strip() for value in raw.split(" | ") if value.strip()]


def _strict_claim(row: dict[str, str]) -> bool:
    return bool(
        _truthy(row.get("property_identity_match"))
        and _positive_int(row.get("native_identity_rows"))
        and _positive_int(row.get("native_positive_rent_rows"))
        and str(row.get("contamination_verdict") or "").startswith("pass_")
        and str(row.get("local_validation") or "").strip()
        and _source_urls(row)
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    rows = _read_rows(args.strict_ledger)
    artifacts = _artifact_index(args.artifact_root)
    records: list[dict[str, Any]] = []
    withheld: Counter[str] = Counter()
    route_statuses: Counter[str] = Counter()
    matched_properties = 0
    profile_properties = 0

    for property_id, row in sorted(rows.items(), key=lambda item: int(item[0])):
        profile_path = args.profiles_dir / f"{property_id}.json"
        if not profile_path.exists():
            withheld["no_actionable_run_profile"] += 1
            continue
        profile_properties += 1
        if not _strict_claim(row):
            withheld["strict_claim_incomplete"] += 1
            continue
        artifact = _artifact_for(row, artifacts)
        if artifact is None:
            withheld["evidence_artifact_missing_or_ambiguous"] += 1
            continue
        artifact_path, artifact_sha = artifact
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        urls = _source_urls(row)
        route_records: list[dict[str, Any]] = []
        matched = 0
        for route in profile_routes(property_id, profile):
            is_match = any(route_equivalent(route.url, source_url) for source_url in urls)
            identity = {
                "status": MATCH if is_match else "UNKNOWN",
                "evidence_source": (
                    "worker_strict_unit_producing_route" if is_match else "no_exact_worker_route_match"
                ),
            }
            route_record = safe_route_record(
                route,
                winner=route.source == "navigation.winning_page_url",
            )
            route_record["identity"] = identity
            route_records.append(route_record)
            route_statuses[identity["status"]] += 1
            matched += int(is_match)
        if matched:
            matched_properties += 1
        else:
            withheld["profile_has_no_exact_producing_route"] += 1
        records.append(
            {
                "audit_version": _AUDIT_VERSION,
                "property_id": property_id,
                "identity": {
                    "status": MATCH if matched else "UNKNOWN",
                    "evidence_source": (
                        "worker_strict_unit_producing_route" if matched else "no_exact_worker_route_match"
                    ),
                },
                "profile_routes": route_records,
                "evidence": {
                    "artifact_name": artifact_path.name,
                    "artifact_sha256": artifact_sha,
                    "evidence_lane": row.get("evidence_lane") or None,
                    "contamination_verdict": row.get("contamination_verdict") or None,
                    "native_identity_rows": int(row.get("native_identity_rows") or 0),
                    "native_positive_rent_rows": int(row.get("native_positive_rent_rows") or 0),
                    "source_route_count": len(urls),
                },
            }
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = args.output_dir / "archive-evidence-ledger.jsonl"
    ledger_path.write_text(
        "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
    summary = {
        "audit_version": _AUDIT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "strict_ledger_properties": len(rows),
            "properties_with_actionable_run_profile": profile_properties,
            "evidence_records": len(records),
        },
        "matched_properties": matched_properties,
        "route_status_counts": dict(sorted(route_statuses.items())),
        "withheld_counts": dict(sorted(withheld.items())),
        "output": ledger_path.name,
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-dir", type=Path, required=True)
    parser.add_argument("--strict-ledger", type=Path, action="append", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    print(json.dumps(run(parse_args()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
