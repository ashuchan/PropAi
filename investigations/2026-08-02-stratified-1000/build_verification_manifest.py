#!/usr/bin/env python3
"""Build the deterministic, cost-bounded canary for Aug-02 audit fixes."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CATALOG = REPO / "ma_poc" / "config" / "properties.csv"
BASELINE = HERE / "post-run-audit" / "property-ledger.csv"
OUTPUT = HERE / "verification-v1"


CASES: dict[str, dict[str, object]] = {
    "identity_natural_id": {
        "affected": [11130, 241798],
        "controls": [2709, 12727],
        "evidence": (
            "The 1,000-property run emitted 89 avoidable synthetic IDs for Mercer "
            "Park and Waterford Landings even though immutable pre-format rows "
            "contained natural apartment numbers."
        ),
        "gate": (
            "Natural pre-format apartment numbers are canonical; no inferred_ or "
            "unkeyable_ ID is emitted for the two affected properties."
        ),
    },
    "entrata_parallel_roster": {
        "affected": [5091, 26523, 29448, 37065, 48052, 48092, 62743, 280797],
        "controls": [],
        "evidence": (
            "Eight Entrata properties emitted the same visible apartment roster "
            "from both modern-card and per-plan-card response families."
        ),
        "gate": (
            "One coherent comparable Entrata family wins, canonical IDs are unique, "
            "and the actual unit-producing response is archived."
        ),
    },
    "negative_status_date": {
        "affected": [4170],
        "controls": [2709, 12727],
        "evidence": (
            "Beechwood emitted capture-date availability for eight PENDING or "
            "LEASED apartments because relative Available Now text outranked status."
        ),
        "gate": (
            "Negative status suppresses relative capture-date availability while "
            "explicit future dates and positive Available Now dates survive."
        ),
    },
    "dead_entry_salvage": {
        "affected": [4579, 12566, 36168, 46108, 218893, 226992],
        "controls": [],
        "evidence": (
            "Six properties were stamped DEAD_URL despite 107 priced physical units "
            "recovered from bounded sub-routes."
        ),
        "gate": (
            "A dead entry URL remains terminal only when no physical inventory was "
            "recovered; recovered units reach the normal success verdict."
        ),
    },
    "timeout_diagnostics": {
        "affected": [1084, 27165, 32097, 52697, 70238, 272772, 274909],
        "controls": [],
        "evidence": (
            "Seven timed-out properties lost final-count, canonical-identity, raw-source, "
            "or provenance diagnostics even after an extraction checkpoint existed."
        ),
        "gate": (
            "Every terminal result, including a timeout, writes an extraction snapshot "
            "and retains any source/provenance evidence available before cancellation."
        ),
    },
    "managebuilding_archive": {
        "affected": [280355],
        "controls": [14943, 74528],
        "evidence": (
            "Town Center emitted 17 unit lineage hashes without archiving the actual "
            "ManageBuilding response; the two controls guard property-bound routing."
        ),
        "gate": (
            "Actual public rental-index bytes and lineage are archived, and neither "
            "control admits Town Center or sibling-community inventory."
        ),
    },
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    catalog = {int(row["apartmentid"]): row for row in read_csv(CATALOG)}
    baseline = {
        int(row["apartment_id"]): row
        for row in read_csv(BASELINE)
        if row.get("apartment_id")
    }

    ledger: list[dict[str, object]] = []
    property_ids: set[int] = set()
    for cluster, definition in CASES.items():
        affected = {int(value) for value in definition["affected"]}
        controls = {int(value) for value in definition["controls"]}
        for property_id in sorted(affected | controls):
            if property_id not in catalog:
                raise RuntimeError(f"property {property_id} missing from {CATALOG}")
            property_ids.add(property_id)
            old = baseline.get(property_id, {})
            ledger.append(
                {
                    "apartmentid": property_id,
                    "name": catalog[property_id]["name"],
                    "cluster": cluster,
                    "role": "affected" if property_id in affected else "control",
                    "baseline_adapter": old.get("current_adapter", "NOT_IN_1000_RUN"),
                    "baseline_verdict": old.get("current_verdict", "NOT_IN_1000_RUN"),
                    "baseline_unit_count": old.get("unit_count", ""),
                    "evidence": definition["evidence"],
                    "post_fix_gate": definition["gate"],
                }
            )

    OUTPUT.mkdir(parents=True, exist_ok=True)
    property_fields = ["apartmentid", "name", "address", "city", "state", "zip", "website"]
    properties = [
        {field: catalog[property_id][field] for field in property_fields}
        for property_id in sorted(property_ids)
    ]
    write_csv(OUTPUT / "properties.csv", properties, property_fields)
    write_csv(
        OUTPUT / "verification-ledger.csv",
        ledger,
        [
            "apartmentid",
            "name",
            "cluster",
            "role",
            "baseline_adapter",
            "baseline_verdict",
            "baseline_unit_count",
            "evidence",
            "post_fix_gate",
        ],
    )

    cluster_counts = Counter(row["cluster"] for row in ledger)
    summary = {
        "schema_version": 1,
        "source_run": "2026-08-02-strat1000-ff7b377",
        "property_count": len(properties),
        "verification_case_count": len(ledger),
        "cluster_counts": dict(sorted(cluster_counts.items())),
        "properties_sha256": sha256(OUTPUT / "properties.csv"),
        "shared_profile_store_mutation_allowed": False,
    }
    (OUTPUT / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checksums = {
        path.name: sha256(path)
        for path in sorted(OUTPUT.glob("*"))
        if path.name != "SHA256SUMS.json"
    }
    (OUTPUT / "SHA256SUMS.json").write_text(
        json.dumps(checksums, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
