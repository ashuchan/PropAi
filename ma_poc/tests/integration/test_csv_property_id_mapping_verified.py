"""Phase 0 gate: ``scripts/checks/csv_mapping.py`` produces a mapping file with
a single non-null ``chosen_field`` value, and the resulting coverage clears
the spec's threshold (H6).
"""

from __future__ import annotations

import json
from pathlib import Path

from ma_poc.scripts.checks.csv_mapping import MIN_COVERAGE, verify


def test_csv_property_id_mapping_verified(tmp_path: Path) -> None:
    """The production CSV pair must produce a chosen_field with ≥90 % coverage."""
    fp_csv = (
        Path(__file__).resolve().parent.parent.parent
        / "config"
        / "Floorplan-comparisons.csv"
    )
    props_csv = (
        Path(__file__).resolve().parent.parent.parent / "config" / "properties.csv"
    )
    out_path = tmp_path / "csv_floorplan_mapping.json"
    payload = verify(
        floorplan_csv=fp_csv,
        properties_csv=props_csv,
        out_path=out_path,
    )

    assert payload["chosen_field"] is not None, (
        "Phase 0 must select a non-null chosen_field. Coverage payload: "
        f"{payload}"
    )
    cov = payload["coverage"]
    assert cov is not None
    assert cov["ratio"] >= MIN_COVERAGE, (
        f"Coverage {cov['ratio']:.4f} below threshold {MIN_COVERAGE} — "
        f"snap step would be bypassed in production. Payload: {payload}"
    )

    # The file must exist on disk so FloorplanCatalog can load it.
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert on_disk["chosen_field"] == payload["chosen_field"]
