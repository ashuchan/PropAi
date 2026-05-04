"""Phase 0 CSV-mapping verification.

Validates that ``Floorplan- comparisons.csv`` rows can be joined to the
production property roster (``properties.csv``) by some stable column.
Persists the chosen mapping to ``config/csv_floorplan_mapping.json`` so
the runtime ``FloorplanCatalog`` does not have to recompute the join.

The script computes coverage for three candidate columns
(``apartmentid``, ``Property ID``, ``Unique ID``) and chooses the one
with the highest match rate against the floor-plan CSV's ``apartmentid``
column. If no candidate clears 90 % coverage of the production roster,
the script writes ``chosen_field=null`` and the runtime bypasses the
snap step (H6).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_FLOORPLAN_CSV = _CONFIG_DIR / "Floorplan- comparisons.csv"
_PROPERTIES_CSV = _CONFIG_DIR / "properties.csv"
_MAPPING_OUT = _CONFIG_DIR / "csv_floorplan_mapping.json"

# H6: candidates evaluated in order. The first one to clear MIN_COVERAGE
# wins; ties broken by candidate order (which mirrors the spec).
_CANDIDATE_FIELDS: tuple[str, ...] = ("apartmentid", "Property ID", "Unique ID")
MIN_COVERAGE = 0.90


@dataclass(frozen=True)
class Coverage:
    field: str
    matched: int
    total_property_rows: int
    total_floorplan_apartmentids: int

    @property
    def ratio(self) -> float:
        if self.total_property_rows == 0:
            return 0.0
        return self.matched / self.total_property_rows


def _read_apartmentids(path: Path) -> set[str]:
    """Collect unique non-empty apartmentid values from the floor-plan CSV."""
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "apartmentid" not in reader.fieldnames:
            raise RuntimeError(
                f"{path.name} missing required column 'apartmentid'; "
                f"got {reader.fieldnames!r}"
            )
        for row in reader:
            v = (row.get("apartmentid") or "").strip()
            if v:
                seen.add(v)
    return seen


def _coverage_for(properties_path: Path, candidate: str, floorplan_ids: set[str]) -> Coverage:
    matched = 0
    total = 0
    with properties_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or candidate not in reader.fieldnames:
            return Coverage(candidate, 0, 0, len(floorplan_ids))
        for row in reader:
            total += 1
            v = (row.get(candidate) or "").strip()
            if v and v in floorplan_ids:
                matched += 1
    return Coverage(candidate, matched, total, len(floorplan_ids))


def _list_unmatched(
    properties_csv: Path,
    chosen_field: str,
    floorplan_ids: set[str],
) -> tuple[list[dict[str, str]], list[str]]:
    """Return (properties_without_floorplan_match, floorplan_ids_unused).

    First list: rows from ``properties_csv`` whose ``chosen_field`` value is
    missing from the floor-plan CSV. Second list: floor-plan apartmentids
    that don't appear in the property roster — these are the only
    candidates an operator could re-bind the unmatched properties to.
    """
    unmatched_props: list[dict[str, str]] = []
    used_ids: set[str] = set()
    with properties_csv.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            v = (row.get(chosen_field) or "").strip()
            if not v:
                unmatched_props.append(dict(row))
                continue
            if v in floorplan_ids:
                used_ids.add(v)
            else:
                unmatched_props.append(dict(row))
    unused_ids = sorted(floorplan_ids - used_ids)
    return unmatched_props, unused_ids


def verify(
    floorplan_csv: Path = _FLOORPLAN_CSV,
    properties_csv: Path = _PROPERTIES_CSV,
    out_path: Path = _MAPPING_OUT,
    *,
    min_coverage: float = MIN_COVERAGE,
) -> dict:
    """Run the verification, write the mapping JSON, return the payload.

    The returned dict shape:
        {
            "chosen_field": <candidate or None>,
            "coverage": {"field": <name>, "matched": int, "total": int, "ratio": float},
            "candidates": [<Coverage as dict>, ...],
            "min_coverage": float,
            "floorplan_apartmentids_total": int,
            "unmatched_properties": [{<row>}, ...],
            "floorplan_ids_unused_in_roster": [<id>, ...],
        }

    The two trailing lists let an operator reconcile the long tail manually.
    The floor-plan CSV does not carry name/address columns, so a name+address
    fallback is not possible from the file alone — these lists are the
    actionable handoff for a human pass.
    """
    if not floorplan_csv.exists():
        raise FileNotFoundError(f"Floor-plan CSV not found at {floorplan_csv}")
    if not properties_csv.exists():
        raise FileNotFoundError(f"Properties CSV not found at {properties_csv}")

    floorplan_ids = _read_apartmentids(floorplan_csv)
    coverages: list[Coverage] = [
        _coverage_for(properties_csv, c, floorplan_ids) for c in _CANDIDATE_FIELDS
    ]

    chosen: Coverage | None = None
    for cov in coverages:
        if cov.ratio >= min_coverage:
            chosen = cov
            break

    unmatched_props: list[dict[str, str]] = []
    unused_fp_ids: list[str] = []
    if chosen is not None:
        unmatched_props, unused_fp_ids = _list_unmatched(
            properties_csv, chosen.field, floorplan_ids
        )

    payload = {
        "chosen_field": chosen.field if chosen is not None else None,
        "coverage": (
            {
                "field": chosen.field,
                "matched": chosen.matched,
                "total": chosen.total_property_rows,
                "ratio": round(chosen.ratio, 4),
            }
            if chosen is not None
            else None
        ),
        "candidates": [
            {
                "field": c.field,
                "matched": c.matched,
                "total": c.total_property_rows,
                "ratio": round(c.ratio, 4),
            }
            for c in coverages
        ],
        "min_coverage": min_coverage,
        "floorplan_apartmentids_total": len(floorplan_ids),
        "unmatched_properties": unmatched_props,
        "floorplan_ids_unused_in_roster": unused_fp_ids,
    }

    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--floorplan-csv", type=Path, default=_FLOORPLAN_CSV)
    parser.add_argument("--properties-csv", type=Path, default=_PROPERTIES_CSV)
    parser.add_argument("--out", type=Path, default=_MAPPING_OUT)
    parser.add_argument("--min-coverage", type=float, default=MIN_COVERAGE)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    payload = verify(
        floorplan_csv=args.floorplan_csv,
        properties_csv=args.properties_csv,
        out_path=args.out,
        min_coverage=args.min_coverage,
    )
    print(json.dumps(payload, indent=2))
    if payload["chosen_field"] is None:
        log.warning(
            "No candidate cleared %.1f%% coverage. Snap step will be bypassed.",
            args.min_coverage * 100,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
