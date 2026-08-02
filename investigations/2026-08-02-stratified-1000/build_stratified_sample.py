#!/usr/bin/env python3
"""Build a deterministic 1,000-property, evidence-aware canary cohort.

The source catalog has no multifamily asset-type column.  For this validation,
``property_type`` therefore means the prior strict output class: unit level,
plan level, failed no data, unreachable, dead URL, or no data published.

The cohort has two layers:

* a mandatory census of every property in the 49-finding remediation ledger,
  plus explicit candidates for adapters that had no attributed winner; and
* a deterministic supplement that restores fleet-level property-type shares,
  covers every adapter x property-type cell observed in the 4,982-property
  benchmark, and balances geography within those strata.

This script is local-only.  It never uploads, builds, deploys, or starts a job.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
CATALOG_CSV = REPO_ROOT / "ma_poc/config/properties.csv"
BENCHMARK_ROOT = HERE / "source-benchmark"
BENCHMARK_URI = "gs://jugnu-canary/runs/2026-08-01-consolidated-strict-fa1afb7/"
AFFECTED_ROOT = HERE.parent / "2026-08-01-consolidated-canary" / "affected-property-manifest-v1"
AFFECTED_INDEX = AFFECTED_ROOT / "launch_index.csv"
ADAPTER_MATRIX = HERE.parent / "2026-08-01-consolidated-canary" / "ADAPTER_COVERAGE_MATRIX.md"
OUTPUT_ROOT = HERE / "manifest-v1"
SAMPLE_SIZE = 1_000
SEED = "stratified-1000-adapter-type-2026-08-02-v1"
CATALOG_COLUMNS = ("apartmentid", "name", "address", "city", "state", "zip", "website")

# The August 1 benchmark had no attributed output under these registered
# adapters.  These known properties exercise their detection/route boundary in
# the next run.  They are coverage candidates, not claims that the adapter must
# win (a stronger correct adapter may legitimately win).
ROUTE_COVERAGE_CANDIDATES: Mapping[str, tuple[str, ...]] = {
    "reinhold": ("36175",),  # Chocolate Works
    "touchtour": ("24928", "26151", "27595"),  # Summer Winds, Madera, Positano
    "rentcafe_unit_roster": ("4904", "5974", "60750"),
    "imt_spaces": ("41185",),  # Gallery 421
    "equity_apartments": ("2955", "7797", "8418"),
}


@dataclass(frozen=True)
class Property:
    property_id: str
    catalog: dict[str, str]
    prior_adapter: str
    prior_verdict: str
    prior_winning_tier: str
    prior_detected_pms: str
    prior_unit_count: int
    prior_plan_count: int
    state: str

    @property
    def stratum(self) -> tuple[str, str]:
        return self.prior_adapter, self.prior_verdict


def _stable_key(*parts: object) -> str:
    payload = "\x1f".join(str(part) for part in (SEED, *parts))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_catalog() -> dict[str, dict[str, str]]:
    with CATALOG_CSV.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 4_982:
        raise RuntimeError(f"expected 4,982 catalog rows, found {len(rows)}")
    result = {row["apartmentid"].strip(): {key: row.get(key, "") for key in CATALOG_COLUMNS} for row in rows}
    if len(result) != len(rows):
        raise RuntimeError("catalog apartmentid values are not unique")
    return result


def _benchmark_files() -> list[Path]:
    files = list(BENCHMARK_ROOT.glob("shard_*/properties.json"))
    files.sort(key=lambda path: int(path.parent.name.removeprefix("shard_")))
    if len(files) != 250:
        raise RuntimeError(
            f"expected 250 downloaded benchmark property files, found {len(files)}; "
            "download properties.json from the immutable August 1 run first"
        )
    return files


def _load_population(catalog: Mapping[str, dict[str, str]]) -> tuple[dict[str, Property], list[Path]]:
    properties: dict[str, Property] = {}
    files = _benchmark_files()
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise RuntimeError(f"{path} is not a property list")
        for raw in payload:
            property_id = str(raw.get("apartment_id", "")).strip()
            if not property_id or property_id not in catalog:
                raise RuntimeError(f"benchmark property {property_id!r} is absent from catalog")
            if property_id in properties:
                raise RuntimeError(f"duplicate benchmark property {property_id}")
            meta = raw.get("_meta") or {}
            provenance = meta.get("provenance") or {}
            adapter = str(provenance.get("adapter") or provenance.get("detected_pms") or "UNATTRIBUTED")
            verdict = str(meta.get("verdict") or "UNKNOWN")
            properties[property_id] = Property(
                property_id=property_id,
                catalog=dict(catalog[property_id]),
                prior_adapter=adapter,
                prior_verdict=verdict,
                prior_winning_tier=str(provenance.get("winning_tier") or ""),
                prior_detected_pms=str(provenance.get("detected_pms") or ""),
                prior_unit_count=len(raw.get("units") or []),
                prior_plan_count=len(raw.get("floor_plans") or []),
                state=(catalog[property_id].get("state") or "UNKNOWN").strip() or "UNKNOWN",
            )
    if set(properties) != set(catalog):
        missing = sorted(set(catalog) - set(properties))
        raise RuntimeError(f"benchmark does not cover the catalog; missing {missing[:10]}")
    return properties, files


def _largest_remainder(counts: Mapping[str, int], total: int) -> dict[str, int]:
    population = sum(counts.values())
    if total < 0 or total > population:
        raise ValueError(f"invalid allocation total={total} population={population}")
    if population == 0:
        return {key: 0 for key in counts}
    exact = {key: total * value / population for key, value in counts.items()}
    result = {key: min(value, int(exact[key])) for key, value in counts.items()}
    remaining = total - sum(result.values())
    order = sorted(
        counts,
        key=lambda key: (exact[key] - int(exact[key]), counts[key], _stable_key("allocation", key)),
        reverse=True,
    )
    while remaining:
        progressed = False
        for key in order:
            if result[key] >= counts[key]:
                continue
            result[key] += 1
            remaining -= 1
            progressed = True
            if not remaining:
                break
        if not progressed:
            raise RuntimeError("allocation exhausted capacity")
    return result


def _affected_metadata() -> dict[str, dict[str, str]]:
    with AFFECTED_INDEX.open(newline="", encoding="utf-8") as handle:
        return {row["apartmentid"]: row for row in csv.DictReader(handle)}


def _registered_adapters() -> tuple[str, ...]:
    adapters: list[str] = []
    in_registered = False
    for line in ADAPTER_MATRIX.read_text(encoding="utf-8").splitlines():
        if line == "## Registered adapters":
            in_registered = True
            continue
        if in_registered and line.startswith("## "):
            break
        match = re.match(r"^\|\s*\d+\s*\|\s*`([^`]+)`", line)
        if in_registered and match:
            adapters.append(match.group(1))
    if len(adapters) != 47:
        raise RuntimeError(f"expected 47 registered adapters, found {len(adapters)}")
    return tuple(adapters)


def _choose_one(
    candidates: Iterable[Property],
    *,
    state_counts: Counter[str],
    state_targets: Mapping[str, int],
    reason: str,
) -> Property:
    pool = list(candidates)
    if not pool:
        raise RuntimeError(f"no candidate remains for {reason}")

    def score(prop: Property) -> tuple[float, int, str]:
        target = max(1, state_targets.get(prop.state, 0))
        deficit_ratio = (state_targets.get(prop.state, 0) - state_counts[prop.state]) / target
        return deficit_ratio, -state_counts[prop.state], _stable_key(reason, prop.property_id)

    return max(pool, key=score)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build() -> dict[str, object]:
    catalog = _read_catalog()
    population, benchmark_files = _load_population(catalog)
    affected = _affected_metadata()
    registered = _registered_adapters()

    verdict_population = Counter(prop.prior_verdict for prop in population.values())
    verdict_targets = _largest_remainder(verdict_population, SAMPLE_SIZE)
    state_population = Counter(prop.state for prop in population.values())
    state_targets = _largest_remainder(state_population, SAMPLE_SIZE)

    selected: set[str] = set()
    reasons: defaultdict[str, set[str]] = defaultdict(set)
    route_tags: defaultdict[str, set[str]] = defaultdict(set)
    state_counts: Counter[str] = Counter()
    verdict_counts: Counter[str] = Counter()
    stratum_counts: Counter[tuple[str, str]] = Counter()
    adapter_counts: Counter[str] = Counter()

    def add(prop: Property, reason: str) -> None:
        reasons[prop.property_id].add(reason)
        if prop.property_id in selected:
            return
        if verdict_counts[prop.prior_verdict] >= verdict_targets[prop.prior_verdict]:
            raise RuntimeError(
                f"mandatory coverage exceeds target for {prop.prior_verdict}: {prop.property_id} ({reason})"
            )
        selected.add(prop.property_id)
        state_counts[prop.state] += 1
        verdict_counts[prop.prior_verdict] += 1
        stratum_counts[prop.stratum] += 1
        adapter_counts[prop.prior_adapter] += 1

    # Layer 1: complete evidence-backed remediation census.
    for property_id in sorted(affected, key=lambda value: int(value)):
        add(population[property_id], "finding_evidence_census")

    # Layer 1b: explicit route candidates for the five registered N0 adapters.
    for adapter, property_ids in ROUTE_COVERAGE_CANDIDATES.items():
        for property_id in property_ids:
            route_tags[property_id].add(adapter)
            add(population[property_id], f"route_candidate:{adapter}")

    # Layer 2a: guarantee every adapter x property-type cell observed in the
    # benchmark.  Rare cells are selected first.
    by_stratum: defaultdict[tuple[str, str], list[Property]] = defaultdict(list)
    for prop in population.values():
        by_stratum[prop.stratum].append(prop)
    for stratum, members in sorted(by_stratum.items(), key=lambda item: (len(item[1]), item[0])):
        if stratum_counts[stratum]:
            continue
        adapter, verdict = stratum
        if verdict_counts[verdict] >= verdict_targets[verdict]:
            raise RuntimeError(f"no type quota remains to cover observed stratum {stratum}")
        candidate = _choose_one(
            (prop for prop in members if prop.property_id not in selected),
            state_counts=state_counts,
            state_targets=state_targets,
            reason=f"observed_stratum:{adapter}:{verdict}",
        )
        add(candidate, "observed_adapter_type_stratum")

    # Layer 2b: at least three properties per observed adapter where possible.
    by_adapter: defaultdict[str, list[Property]] = defaultdict(list)
    for prop in population.values():
        by_adapter[prop.prior_adapter].append(prop)
    for adapter, members in sorted(by_adapter.items(), key=lambda item: (len(item[1]), item[0])):
        minimum = min(3, len(members))
        while adapter_counts[adapter] < minimum:
            candidates = [
                prop
                for prop in members
                if prop.property_id not in selected
                and verdict_counts[prop.prior_verdict] < verdict_targets[prop.prior_verdict]
            ]
            candidate = _choose_one(
                candidates,
                state_counts=state_counts,
                state_targets=state_targets,
                reason=f"adapter_floor:{adapter}:{adapter_counts[adapter]}",
            )
            add(candidate, "observed_adapter_floor")

    # Layer 2c: retain at least one property from every catalog state/territory
    # (and the catalog's UNKNOWN bucket) without changing type quotas.
    for state in sorted(state_population):
        if state_counts[state]:
            continue
        candidates = [
            prop
            for prop in population.values()
            if prop.state == state
            and prop.property_id not in selected
            and verdict_counts[prop.prior_verdict] < verdict_targets[prop.prior_verdict]
        ]
        candidate = _choose_one(
            candidates,
            state_counts=state_counts,
            state_targets=state_targets,
            reason=f"state_floor:{state}",
        )
        add(candidate, "state_floor")

    # Layer 3: fill each remaining property-type quota proportionally across
    # the available adapter strata.  Candidate choice prefers states that are
    # still below their fleet-derived target.
    for verdict in sorted(verdict_targets):
        need = verdict_targets[verdict] - verdict_counts[verdict]
        if need <= 0:
            continue
        remaining_by_adapter: dict[str, list[Property]] = defaultdict(list)
        for prop in population.values():
            if prop.prior_verdict == verdict and prop.property_id not in selected:
                remaining_by_adapter[prop.prior_adapter].append(prop)
        adapter_capacity = {adapter: len(members) for adapter, members in remaining_by_adapter.items()}
        adapter_quota = _largest_remainder(adapter_capacity, need)
        slots = [
            adapter
            for adapter, count in adapter_quota.items()
            for _ in range(count)
        ]
        slots.sort(key=lambda adapter: (len(remaining_by_adapter[adapter]), adapter))
        for slot_index, adapter in enumerate(slots):
            candidate = _choose_one(
                (prop for prop in remaining_by_adapter[adapter] if prop.property_id not in selected),
                state_counts=state_counts,
                state_targets=state_targets,
                reason=f"proportional_fill:{verdict}:{adapter}:{slot_index}",
            )
            add(candidate, "proportional_adapter_type_fill")

    if len(selected) != SAMPLE_SIZE:
        raise RuntimeError(f"expected {SAMPLE_SIZE} selected properties, found {len(selected)}")
    if verdict_counts != Counter(verdict_targets):
        raise RuntimeError(f"type quotas not met: actual={verdict_counts} target={verdict_targets}")
    if set(stratum_counts) != set(by_stratum):
        raise RuntimeError("not every observed adapter x property-type stratum is represented")
    if set(state_counts) != set(state_population):
        raise RuntimeError("not every catalog state bucket is represented")

    selected_props = [population[property_id] for property_id in selected]
    selected_props.sort(key=lambda prop: _stable_key("launch_order", prop.property_id))

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    properties_path = OUTPUT_ROOT / "properties.csv"
    with properties_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CATALOG_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(prop.catalog for prop in selected_props)

    ledger_columns = (
        *CATALOG_COLUMNS,
        "prior_adapter",
        "prior_property_type",
        "prior_winning_tier",
        "prior_detected_pms",
        "prior_unit_count",
        "prior_plan_count",
        "selection_layers",
        "finding_ids",
        "finding_adapters",
        "route_coverage_adapters",
        "selection_sha256",
    )
    ledger_path = OUTPUT_ROOT / "sample_ledger.csv"
    with ledger_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ledger_columns, lineterminator="\n")
        writer.writeheader()
        for prop in selected_props:
            affected_row = affected.get(prop.property_id, {})
            writer.writerow(
                {
                    **prop.catalog,
                    "prior_adapter": prop.prior_adapter,
                    "prior_property_type": prop.prior_verdict,
                    "prior_winning_tier": prop.prior_winning_tier,
                    "prior_detected_pms": prop.prior_detected_pms,
                    "prior_unit_count": prop.prior_unit_count,
                    "prior_plan_count": prop.prior_plan_count,
                    "selection_layers": ";".join(sorted(reasons[prop.property_id])),
                    "finding_ids": affected_row.get("finding_ids", ""),
                    "finding_adapters": affected_row.get("adapters", ""),
                    "route_coverage_adapters": ";".join(sorted(route_tags[prop.property_id])),
                    "selection_sha256": _stable_key("property", prop.property_id),
                }
            )

    def write_coverage(name: str, population_counts: Counter[str], sample_counts: Counter[str]) -> None:
        path = OUTPUT_ROOT / name
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=("stratum", "population_count", "sample_count", "population_share", "sample_share"),
                lineterminator="\n",
            )
            writer.writeheader()
            for key in sorted(population_counts):
                writer.writerow(
                    {
                        "stratum": key,
                        "population_count": population_counts[key],
                        "sample_count": sample_counts[key],
                        "population_share": f"{population_counts[key] / len(population):.8f}",
                        "sample_share": f"{sample_counts[key] / SAMPLE_SIZE:.8f}",
                    }
                )

    sample_adapter_counts = Counter(prop.prior_adapter for prop in selected_props)
    sample_state_counts = Counter(prop.state for prop in selected_props)
    write_coverage("adapter_coverage.csv", Counter(prop.prior_adapter for prop in population.values()), sample_adapter_counts)
    write_coverage("property_type_coverage.csv", verdict_population, verdict_counts)
    write_coverage("state_coverage.csv", state_population, sample_state_counts)

    represented_adapter_routes = set(sample_adapter_counts)
    represented_adapter_routes.update(adapter for tags in route_tags.values() for adapter in tags)
    registered_missing = sorted(set(registered) - represented_adapter_routes)
    if registered_missing:
        raise RuntimeError(f"registered adapters lack winner or route-candidate coverage: {registered_missing}")

    benchmark_digest_payload = []
    for path in benchmark_files:
        benchmark_digest_payload.append(f"{path.relative_to(BENCHMARK_ROOT)}:{_sha256(path)}")
    benchmark_digest = hashlib.sha256("\n".join(benchmark_digest_payload).encode("utf-8")).hexdigest()

    summary: dict[str, object] = {
        "manifest_version": "stratified-1000-adapter-type-v1",
        "sample_size": len(selected_props),
        "population_size": len(population),
        "seed": SEED,
        "property_type_definition": "prior strict canary verdict/output class; source catalog has no asset-type field",
        "source_benchmark_uri": BENCHMARK_URI,
        "source_benchmark_properties_digest": benchmark_digest,
        "source_catalog_sha256": _sha256(CATALOG_CSV),
        "affected_manifest_sha256": _sha256(AFFECTED_INDEX),
        "mandatory_finding_property_count": sum(
            "finding_evidence_census" in reasons[prop.property_id] for prop in selected_props
        ),
        "explicit_route_candidate_property_count": sum(bool(route_tags[prop.property_id]) for prop in selected_props),
        "observed_adapter_count": len(by_adapter),
        "observed_adapter_type_stratum_count": len(by_stratum),
        "represented_observed_adapter_count": len(sample_adapter_counts),
        "represented_observed_adapter_type_stratum_count": len(stratum_counts),
        "registered_adapter_count": len(registered),
        "registered_adapters_missing_coverage": registered_missing,
        "state_bucket_count": len(state_population),
        "represented_state_bucket_count": len(sample_state_counts),
        "population_by_property_type": dict(sorted(verdict_population.items())),
        "target_by_property_type": dict(sorted(verdict_targets.items())),
        "sample_by_property_type": dict(sorted(verdict_counts.items())),
        "sample_by_adapter": dict(sorted(sample_adapter_counts.items())),
        "route_coverage_candidates": {key: list(value) for key, value in ROUTE_COVERAGE_CANDIDATES.items()},
        "launch_performed": False,
    }
    summary_path = OUTPUT_ROOT / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    artifact_paths = sorted(
        path for path in OUTPUT_ROOT.iterdir() if path.is_file() and path.name != "SHA256SUMS.json"
    )
    checksums = {path.name: _sha256(path) for path in artifact_paths}
    (OUTPUT_ROOT / "SHA256SUMS.json").write_text(
        json.dumps(checksums, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


if __name__ == "__main__":
    print(json.dumps(build(), indent=2, sort_keys=True))
