from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.scripts.diagnostics.materialize_worker_route_evidence import run


def _write_ledger(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _row(property_id: str, artifact: str, source_url: str) -> dict[str, str]:
    return {
        "property_id": property_id,
        "artifact": artifact,
        "source_urls": source_url,
        "property_identity_match": "True",
        "contamination_verdict": "pass_exact_property",
        "native_identity_rows": "2",
        "native_positive_rent_rows": "2",
        "local_validation": "repeat3",
        "evidence_lane": "test_lane",
    }


def _profile(directory: Path, property_id: str, winner: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    profile = ScrapeProfile(canonical_id=property_id)
    profile.navigation.winning_page_url = winner
    (directory / f"{property_id}.json").write_text(profile.model_dump_json(indent=2), encoding="utf-8")


def test_exact_archived_producing_route_becomes_match(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "evidence.json").write_text("{}", encoding="utf-8")
    route = "https://property.example.com/availableunits?propertyId=7"
    _profile(profiles, "7", route)
    ledger = tmp_path / "strict.csv"
    _write_ledger(ledger, [_row("7", "/old/path/evidence.json", route)])

    summary = run(
        argparse.Namespace(
            profiles_dir=profiles,
            strict_ledger=[ledger],
            artifact_root=artifacts,
            output_dir=tmp_path / "output",
        )
    )

    assert summary["matched_properties"] == 1
    record = json.loads((tmp_path / "output" / "archive-evidence-ledger.jsonl").read_text())
    assert record["profile_routes"][0]["identity"]["status"] == "MATCH"
    assert "https://" not in json.dumps(record)


def test_unmatched_route_stays_unknown(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "evidence.json").write_text("{}", encoding="utf-8")
    _profile(profiles, "7", "https://property.example.com/floorplans")
    ledger = tmp_path / "strict.csv"
    _write_ledger(
        ledger,
        [_row("7", "evidence.json", "https://property.example.com/availableunits")],
    )

    summary = run(
        argparse.Namespace(
            profiles_dir=profiles,
            strict_ledger=[ledger],
            artifact_root=artifacts,
            output_dir=tmp_path / "output",
        )
    )

    assert summary["matched_properties"] == 0
    assert summary["withheld_counts"] == {"profile_has_no_exact_producing_route": 1}


def test_missing_artifact_is_withheld_even_when_url_matches(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    route = "https://property.example.com/availableunits"
    _profile(profiles, "7", route)
    ledger = tmp_path / "strict.csv"
    _write_ledger(ledger, [_row("7", "missing.json", route)])

    summary = run(
        argparse.Namespace(
            profiles_dir=profiles,
            strict_ledger=[ledger],
            artifact_root=artifacts,
            output_dir=tmp_path / "output",
        )
    )

    assert summary["matched_properties"] == 0
    assert summary["scope"]["evidence_records"] == 0
    assert summary["withheld_counts"] == {"evidence_artifact_missing_or_ambiguous": 1}
