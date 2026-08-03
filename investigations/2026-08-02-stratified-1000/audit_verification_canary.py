#!/usr/bin/env python3
"""Audit the cost-bounded post-fix canary entirely from its local mirror."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from audit_stratified_canary import (
    NEGATIVE_STATUSES,
    adapter,
    apartment_id,
    is_synthetic,
    load_properties,
    manifest_for_property,
    preformat_natural_identity_matches,
    provenance,
    read_csv,
    text,
    verdict,
)


HERE = Path(__file__).resolve().parent
VERIFY = HERE / "verification-v1"
DEFAULT_RUN_DIR = VERIFY / "canary-output"
DEFAULT_OUTPUT_DIR = VERIFY / "post-run-verification"
DEFAULT_PROPERTIES = VERIFY / "properties.csv"
DEFAULT_LEDGER = VERIFY / "verification-ledger.csv"
CAPTURE_DATE = date(2026, 8, 2)


@dataclass
class Issue:
    severity: str
    cluster: str
    apartment_id: str
    name: str
    adapter: str
    verdict: str
    code: str
    observed: str
    expected: str


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def issue(
    issues: list[Issue],
    prop: dict[str, Any] | None,
    clusters: set[str],
    severity: str,
    code: str,
    observed: Any,
    expected: str,
    *,
    pid: str = "",
    name: str = "",
) -> None:
    value = prop or {}
    issues.append(
        Issue(
            severity=severity,
            cluster="|".join(sorted(clusters)) or "run_integrity",
            apartment_id=pid or apartment_id(value),
            name=name or text(value.get("proj_name") or value.get("name")),
            adapter=adapter(value) if value else "",
            verdict=verdict(value) if value else "",
            code=code,
            observed=text(observed),
            expected=expected,
        )
    )


def archive_hashes(manifest: dict[str, Any] | None) -> set[str]:
    result: set[str] = set()
    for row in (manifest or {}).get("responses") or []:
        if not isinstance(row, dict):
            continue
        for key in ("source_response_sha256", "archive_payload_sha256"):
            value = text(row.get(key)).casefold()
            if value:
                result.add(value)
    return result


def preformat_rows(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        row
        for row in (snapshot or {}).get("units_pre_format") or []
        if isinstance(row, dict)
    ]


def natural_preformat_count(snapshot: dict[str, Any] | None) -> int:
    rows = preformat_rows(snapshot)
    return sum(
        bool(
            text(
                row.get("unit_number")
                or row.get("_unit_number")
                or row.get("unitNumber")
                or row.get("apartment_number")
            )
        )
        for row in rows
    )


def entrata_parallel_count(units: list[dict[str, Any]]) -> int:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit in units:
        source_id = text(unit.get("source_unit_id")).casefold()
        if source_id:
            grouped[source_id].append(unit)
    duplicates = 0
    for rows in grouped.values():
        tiers = {text(row.get("extraction_tier")).upper() for row in rows}
        modern = "TIER_1_DOM_ENTRATA_MODERN" in tiers
        per_plan = any(value.startswith("TIER_1_DOM_ENTRATA_PP_") for value in tiers)
        blank_modern = any(
            text(row.get("extraction_tier")).upper() == "TIER_1_DOM_ENTRATA_MODERN"
            and not text(row.get("building_id") or row.get("building"))
            for row in rows
        )
        if len(rows) > 1 and modern and per_plan and blank_modern:
            duplicates += len(rows) - 1
    return duplicates


def available_date_is_capture(unit: dict[str, Any]) -> bool:
    return text(unit.get("available_date"))[:10] == CAPTURE_DATE.isoformat()


def markdown_table(headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(text(value).replace("|", "\\|") for value in row) + " |")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--properties", type=Path, default=DEFAULT_PROPERTIES)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--launch-manifest", type=Path)
    parser.add_argument(
        "--supplemental-run-dir",
        action="append",
        type=Path,
        default=[],
        help="additional immutable run mirror(s), with cross-run duplicates rejected",
    )
    parser.add_argument(
        "--supplemental-launch-manifest",
        action="append",
        type=Path,
        default=[],
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    expected_rows = read_csv(args.properties)
    expected = {text(row["apartmentid"]): row for row in expected_rows}
    ledger = read_csv(args.ledger)
    clusters_by_pid: dict[str, set[str]] = defaultdict(set)
    roles_by_cluster_pid: dict[tuple[str, str], str] = {}
    for row in ledger:
        pid = text(row["apartmentid"])
        cluster = text(row["cluster"])
        clusters_by_pid[pid].add(cluster)
        roles_by_cluster_pid[(cluster, pid)] = text(row["role"])

    loaded, source_paths, duplicates = load_properties(args.run_dir)
    for supplemental_dir in args.supplemental_run_dir:
        extra, extra_sources, extra_duplicates = load_properties(supplemental_dir)
        duplicates.extend(extra_duplicates)
        for pid, prop in extra.items():
            if pid in loaded:
                duplicates.append(pid)
                continue
            loaded[pid] = prop
            source_paths[pid] = extra_sources[pid]
    current = {pid: value for pid, value in loaded.items() if pid in expected}
    missing = sorted(set(expected) - set(current), key=int)
    unexpected = sorted(set(loaded) - set(expected), key=int)
    issues: list[Issue] = []
    for pid in missing:
        issue(
            issues,
            None,
            clusters_by_pid[pid],
            "critical",
            "EXPECTED_PROPERTY_MISSING",
            "no output row",
            "exactly one output row",
            pid=pid,
            name=expected[pid]["name"],
        )
    for pid in sorted(set(duplicates), key=int):
        issue(
            issues,
            current.get(pid),
            clusters_by_pid.get(pid, {"run_integrity"}),
            "critical",
            "DUPLICATE_PROPERTY_OUTPUT",
            duplicates.count(pid) + 1,
            "one output row",
            pid=pid,
            name=expected.get(pid, {}).get("name", ""),
        )

    observations: dict[str, dict[str, Any]] = {}
    town_center_baseline_hashes: set[str] = set()
    baseline_loaded, _, _ = load_properties(HERE / "canary-output")
    for unit in baseline_loaded.get("280355", {}).get("units") or []:
        value = text(unit.get("source_response_sha256")).casefold()
        if value:
            town_center_baseline_hashes.add(value)

    for pid, prop in sorted(current.items(), key=lambda item: int(item[0])):
        clusters = clusters_by_pid[pid]
        units = [row for row in prop.get("units") or [] if isinstance(row, dict)]
        plans = [row for row in prop.get("floor_plans") or [] if isinstance(row, dict)]
        manifest, snapshot, archive_problems = manifest_for_property(prop, source_paths[pid])
        hashes = archive_hashes(manifest)
        unit_hashes = {
            text(row.get("source_response_sha256")).casefold()
            for row in units
            if text(row.get("source_response_sha256"))
        }
        missing_hashes = sorted(unit_hashes - hashes)
        synthetic = [row for row in units if is_synthetic(row)]
        natural_rows = preformat_rows(snapshot)
        avoidable_synthetic = sum(
            bool(preformat_natural_identity_matches(row, natural_rows))
            for row in synthetic
        )
        canonical_ids = [text(row.get("unit_id")) for row in units]
        duplicate_ids = len(canonical_ids) - len(set(canonical_ids))
        parallel = entrata_parallel_count(units)
        negative_rows = [
            row
            for row in units
            if text(row.get("availability_status")).upper() in NEGATIVE_STATUSES
        ]
        manufactured_negative = [
            row
            for row in negative_rows
            if text(row.get("availability_date_provenance"))
            in {"available_now", "capture_date_default"}
            or (
                available_date_is_capture(row)
                and text(row.get("availability_date_provenance")) != "explicit_future"
            )
        ]
        unresolved_area = [
            row
            for row in units
            if row.get("area") == -1
            and not (
                isinstance(row.get("area_low"), (int, float))
                and isinstance(row.get("area_high"), (int, float))
                and row["area_low"] > 0
                and row["area_high"] >= row["area_low"]
            )
            and not text(row.get("area_absence"))
        ]

        observations[pid] = {
            "apartment_id": pid,
            "name": expected[pid]["name"],
            "adapter": adapter(prop),
            "verdict": verdict(prop),
            "unit_count": len(units),
            "plan_count": len(plans),
            "synthetic_ids": len(synthetic),
            "avoidable_synthetic_ids": avoidable_synthetic,
            "duplicate_unit_ids": duplicate_ids,
            "entrata_parallel_duplicates": parallel,
            "negative_status_rows": len(negative_rows),
            "manufactured_negative_dates": len(manufactured_negative),
            "building_id_rows": sum(bool(text(row.get("building_id"))) for row in units),
            "rent_range_rows": sum(bool(text(row.get("rent_range"))) for row in units),
            "unresolved_area_rows": len(unresolved_area),
            "unit_source_hashes": len(unit_hashes),
            "unarchived_unit_source_hashes": len(missing_hashes),
            "archive_source_count": len((manifest or {}).get("responses") or []),
            "snapshot_present": int(snapshot is not None),
            "natural_preformat_rows": natural_preformat_count(snapshot),
        }

        if archive_problems:
            issue(
                issues,
                prop,
                clusters,
                "critical" if "timeout_diagnostics" in clusters else "high",
                "OFFLINE_ARCHIVE_INVALID",
                "; ".join(archive_problems),
                "valid source manifest, bodies, hashes, and extraction snapshot",
            )
        if snapshot is None:
            issue(
                issues,
                prop,
                clusters,
                "critical" if "timeout_diagnostics" in clusters else "high",
                "EXTRACTION_SNAPSHOT_MISSING",
                "missing",
                "snapshot present for every terminal result",
            )
        if missing_hashes:
            issue(
                issues,
                prop,
                clusters,
                "high",
                "SOURCE_HASH_NOT_ARCHIVED",
                missing_hashes[:8],
                "every unit lineage hash resolves to an archived source response",
            )
        if duplicate_ids:
            issue(
                issues,
                prop,
                clusters,
                "critical",
                "DUPLICATE_CANONICAL_UNIT_ID",
                duplicate_ids,
                "zero duplicate IDs per property",
            )
        if avoidable_synthetic:
            issue(
                issues,
                prop,
                clusters,
                "high",
                "AVOIDABLE_SYNTHETIC_UNIT_ID",
                avoidable_synthetic,
                "natural pre-format apartment number selected first",
            )
        if parallel:
            issue(
                issues,
                prop,
                clusters,
                "critical",
                "ENTRATA_PARALLEL_ROSTER_DUPLICATE",
                parallel,
                "one coherent comparable Entrata roster family",
            )
        if manufactured_negative:
            issue(
                issues,
                prop,
                clusters,
                "critical",
                "NEGATIVE_STATUS_CAPTURE_DATE",
                len(manufactured_negative),
                "negative status suppresses relative capture-date availability",
            )
        if unresolved_area:
            issue(
                issues,
                prop,
                clusters,
                "high",
                "UNEXPLAINED_AREA_MINUS_ONE",
                len(unresolved_area),
                "valid range or explicit area_absence provenance",
            )

        if "entrata_parallel_roster" in clusters and adapter(prop) == "entrata" and units:
            lineage_missing = sum(
                not all(
                    text(row.get(field))
                    for field in (
                        "source_response_sha256",
                        "source_response_url",
                        "source_record_locator",
                    )
                )
                for row in units
                if "ENTRATA" in text(row.get("extraction_tier")).upper()
            )
            if lineage_missing:
                issue(
                    issues,
                    prop,
                    clusters,
                    "high",
                    "ENTRATA_UNIT_LINEAGE_MISSING",
                    lineage_missing,
                    "hash, URL, and record locator retained for each recovered Entrata row",
                )

        if "dead_entry_salvage" in clusters and units and verdict(prop) != "SUCCESS":
            issue(
                issues,
                prop,
                clusters,
                "critical",
                "RECOVERED_UNITS_HAVE_FAILURE_VERDICT",
                f"{verdict(prop)} with {len(units)} units",
                "SUCCESS",
            )

        if "managebuilding_archive" in clusters:
            role = roles_by_cluster_pid[("managebuilding_archive", pid)]
            target_route = "MANAGEBUILDING" in text(provenance(prop).get("winning_tier")).upper()
            if role == "affected" and target_route and units and not unit_hashes:
                issue(
                    issues,
                    prop,
                    clusters,
                    "high",
                    "MANAGEBUILDING_LINEAGE_MISSING",
                    "no unit source hashes",
                    "actual rentals-index response hash on every unit",
                )
            if role == "control":
                contaminated = sorted(unit_hashes & town_center_baseline_hashes)
                if contaminated:
                    issue(
                        issues,
                        prop,
                        clusters,
                        "critical",
                        "TOWN_CENTER_RESPONSE_ADMITTED_BY_CONTROL",
                        contaminated,
                        "no cross-property response-hash overlap",
                    )

    issue_by_pid: dict[str, list[Issue]] = defaultdict(list)
    for row in issues:
        issue_by_pid[row.apartment_id].append(row)

    case_rows: list[dict[str, Any]] = []
    for row in ledger:
        pid = text(row["apartmentid"])
        cluster = text(row["cluster"])
        prop = current.get(pid)
        observation = observations.get(pid, {})
        relevant = [
            item
            for item in issue_by_pid.get(pid, [])
            if cluster in item.cluster.split("|") or item.cluster == "run_integrity"
        ]
        exercised = False
        if prop:
            units = prop.get("units") or []
            if cluster == "identity_natural_id":
                exercised = bool(units and observation.get("natural_preformat_rows"))
            elif cluster == "entrata_parallel_roster":
                exercised = adapter(prop) == "entrata" and bool(units)
            elif cluster == "negative_status_date":
                if pid == "2709":
                    exercised = any(
                        text(unit.get("availability_date_provenance")) == "available_now"
                        for unit in units
                    )
                else:
                    exercised = bool(observation.get("negative_status_rows"))
            elif cluster == "dead_entry_salvage":
                exercised = bool(
                    units
                    and text((provenance(prop).get("fetch") or {}).get("outcome"))
                    == "DEAD_URL"
                )
            elif cluster == "timeout_diagnostics":
                exercised = any(
                    "per_property_timeout" in text(value)
                    for value in (prop.get("_meta") or {}).get("scrape_errors") or []
                )
            elif cluster == "managebuilding_archive":
                exercised = "MANAGEBUILDING" in text(
                    provenance(prop).get("winning_tier")
                ).upper()
        status = (
            "FAIL_OUTPUT_CONTRACT"
            if relevant
            else "PASS_RUNTIME_EXERCISED"
            if exercised
            else "NOT_TARGET_ROUTE_EXERCISED"
        )
        case_rows.append(
            {
                "cluster": cluster,
                "apartment_id": pid,
                "name": row["name"],
                "role": row["role"],
                "adapter": observation.get("adapter", ""),
                "verdict": observation.get("verdict", "MISSING"),
                "unit_count": observation.get("unit_count", 0),
                "plan_count": observation.get("plan_count", 0),
                "runtime_exercised": int(exercised),
                "blocking_issue_count": len(relevant),
                "status": status,
                "post_fix_gate": row["post_fix_gate"],
            }
        )

    cluster_rows: list[dict[str, Any]] = []
    for cluster in sorted({row["cluster"] for row in case_rows}):
        rows = [row for row in case_rows if row["cluster"] == cluster]
        affected = [row for row in rows if row["role"] == "affected"]
        blocking = sum(int(row["blocking_issue_count"]) for row in rows)
        exercised = sum(int(row["runtime_exercised"]) for row in rows)
        affected_exercised = sum(int(row["runtime_exercised"]) for row in affected)
        status = (
            "FAIL_OUTPUT_CONTRACT"
            if blocking
            else "PASS_RUNTIME_EXERCISED"
            if affected_exercised == len(affected)
            else "PARTIAL_RUNTIME_EXERCISED"
            if exercised
            else "NOT_TARGET_ROUTE_EXERCISED"
        )
        cluster_rows.append(
            {
                "cluster": cluster,
                "properties": len(rows),
                "affected_properties": len(affected),
                "runtime_exercised": exercised,
                "affected_runtime_exercised": affected_exercised,
                "blocking_issues": blocking,
                "status": status,
            }
        )

    severity = Counter(row.severity for row in issues)
    verdicts = Counter(row["verdict"] for row in observations.values())
    total_units = sum(int(row["unit_count"]) for row in observations.values())
    total_synthetic = sum(int(row["synthetic_ids"]) for row in observations.values())
    total_unresolved_area = sum(int(row["unresolved_area_rows"]) for row in observations.values())
    summary: dict[str, Any] = {
        "run": {
            "expected_properties": len(expected),
            "output_properties": len(current),
            "missing_properties": missing,
            "unexpected_properties": unexpected,
            "duplicate_properties": sorted(set(duplicates), key=int),
        },
        "results": {
            "verdict_counts": dict(sorted(verdicts.items())),
            "unit_rows": total_units,
            "synthetic_id_rows": total_synthetic,
            "unresolved_area_rows": total_unresolved_area,
            "building_id_rows": sum(
                int(row["building_id_rows"]) for row in observations.values()
            ),
            "rent_range_rows": sum(int(row["rent_range_rows"]) for row in observations.values()),
            "snapshots": sum(int(row["snapshot_present"]) for row in observations.values()),
        },
        "issues": {
            "severity_counts": dict(sorted(severity.items())),
            "code_counts": dict(sorted(Counter(row.code for row in issues).items())),
        },
        "cluster_status": {row["cluster"]: row["status"] for row in cluster_rows},
    }
    incomplete_clusters = [
        row["cluster"]
        for row in cluster_rows
        if row["status"] != "PASS_RUNTIME_EXERCISED"
    ]
    summary["conclusion"] = (
        "HOLD_OUTPUT_DEFECTS"
        if severity["critical"] + severity["high"] or missing or duplicates
        else "PARTIAL_RUNTIME_COVERAGE"
        if incomplete_clusters
        else "PASS_ALL_AFFECTED_ROUTES_EXERCISED"
    )
    summary["incomplete_clusters"] = incomplete_clusters
    if args.launch_manifest and args.launch_manifest.is_file():
        summary["launch"] = json.loads(args.launch_manifest.read_text(encoding="utf-8"))
    summary["supplemental_launches"] = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in args.supplemental_launch_manifest
        if path.is_file()
    ]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(
        args.output_dir / "property-observations.csv",
        observations.values(),
        list(next(iter(observations.values()))) if observations else ["apartment_id"],
    )
    write_csv(
        args.output_dir / "verification-cases.csv",
        case_rows,
        list(case_rows[0]),
    )
    write_csv(
        args.output_dir / "cluster-summary.csv",
        cluster_rows,
        list(cluster_rows[0]),
    )
    write_csv(
        args.output_dir / "issues.csv",
        [asdict(row) for row in issues],
        list(asdict(Issue("", "", "", "", "", "", "", "", ""))),
    )
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    blocking = severity["critical"] + severity["high"]
    conclusion = (
        "**HOLD: critical/high verification defects were detected.**"
        if blocking or missing or duplicates
        else "**PARTIAL: no output defect recurred, but one or more affected routes were not exercised live.**"
        if incomplete_clusters
        else "**PASS: no critical/high defects; every affected fix route was exercised live.**"
    )
    report = [
        "# Post-fix affected-property canary",
        "",
        conclusion,
        "",
        "This is the 29-property follow-up gate for defects discovered by the completed "
        "stratified 1,000-property run; it is not a replacement fleet benchmark.",
        "",
        "## Outcome",
        "",
        markdown_table(
            ["Measure", "Result"],
            [
                ["Expected / output", f"{len(expected)} / {len(current)}"],
                ["Verdicts", json.dumps(dict(sorted(verdicts.items())))],
                ["Unit rows", total_units],
                ["Synthetic IDs", total_synthetic],
                ["Unresolved area rows", total_unresolved_area],
                ["Snapshots", f"{summary['results']['snapshots']} / {len(current)}"],
                ["Critical / high issues", f"{severity['critical']} / {severity['high']}"],
            ],
        ),
        "",
        "## Fix-cluster gates",
        "",
        markdown_table(
            ["Cluster", "Affected exercised", "All cases exercised", "Issues", "Status"],
            [
                [
                    row["cluster"],
                    f"{row['affected_runtime_exercised']} / {row['affected_properties']}",
                    f"{row['runtime_exercised']} / {row['properties']}",
                    row["blocking_issues"],
                    row["status"],
                ]
                for row in cluster_rows
            ],
        ),
        "",
        "`verification-cases.csv` preserves pass/fail/not-exercised status per property. "
        "`issues.csv` contains only observed output evidence; fixture proof is not labeled as live exercise.",
        "",
    ]
    (args.output_dir / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if blocking or missing or duplicates else 0


if __name__ == "__main__":
    raise SystemExit(main())
