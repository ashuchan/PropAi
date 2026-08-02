from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

from ma_poc.models.scrape_profile import ApiEndpoint, ScrapeProfile
from ma_poc.scripts.diagnostics.audit_july_gcp_profile_evidence import profile_routes
from ma_poc.scripts.diagnostics.materialize_strict_warm_profiles import (
    load_required_property_ids,
    parse_args,
    route_decisions,
    run,
    sanitize_profile,
)


def _profile() -> dict:
    return {
        "navigation": {
            "entry_url": "https://property.example.com/",
            "winning_page_url": "https://property.example.com/units",
            "availability_page_path": "/stale",
            "availability_links": ["https://wrong.example.com/units"],
            "last_navigation_hints": ["/unbound-hint"],
            "explored_links": ["/old"],
        },
        "api_hints": {
            "known_endpoints": [
                {"url_pattern": "https://property.example.com/units"},
                {"url_pattern": "https://wrong.example.com/units"},
            ],
            "widget_endpoints": [],
            "llm_field_mappings": [],
            "field_patches": [],
            "blocked_endpoints": [{"url_pattern": "https://blocked.example.com"}],
            "source_observations": [{"source": "unbound"}],
            "wait_for_url_pattern": "units",
        },
    }


def test_live_winner_decision_overrides_unknown_archive() -> None:
    profile = _profile()
    routes = profile_routes("7", profile)
    archive = {
        "profile_routes": [
            {
                "route_sha256": route.sha256,
                "source": route.source,
                "historical_winner": route.source == "navigation.winning_page_url",
                "identity": {"status": "UNKNOWN", "evidence_source": "none"},
            }
            for route in routes
        ]
    }
    winner_hash = next(route.sha256 for route in routes if route.source == "navigation.winning_page_url")
    decisions = route_decisions(
        "7",
        archive,
        {("7", winner_hash): {"decision": {"status": "MATCH"}}},
    )
    assert decisions[winner_hash]["status"] == "MATCH"
    assert decisions[winner_hash]["evidence_source"] == "live_winner_route"


@pytest.mark.parametrize(
    ("archive_status", "live_status"),
    [("MISMATCH", "MATCH"), ("MATCH", "MISMATCH")],
)
def test_route_decisions_mismatch_dominates_cross_source_conflict(
    archive_status: str,
    live_status: str,
) -> None:
    property_id = "7"
    route_hash = "a" * 64
    archive = {
        "profile_routes": [
            {
                "route_sha256": route_hash,
                "source": "navigation.winning_page_url",
                "identity": {"status": archive_status, "evidence_source": "archive"},
            }
        ]
    }
    live = {(property_id, route_hash): {"decision": {"status": live_status, "evidence_source": "live"}}}

    decisions = route_decisions(property_id, archive, live)

    assert decisions[route_hash]["status"] == "MISMATCH"
    assert decisions[route_hash]["evidence_source"] == "archive_live_identity_conflict"


def test_sanitize_profile_retains_only_positive_routes_and_clears_unbound_hints() -> None:
    profile = _profile()
    routes = profile_routes("7", profile)
    winner_hash = next(route.sha256 for route in routes if route.source == "navigation.winning_page_url")
    sanitized = sanitize_profile("7", profile, {winner_hash})

    retained = profile_routes("7", sanitized)
    assert {route.sha256 for route in retained} == {winner_hash}
    assert sanitized["navigation"]["availability_page_path"] is None
    assert sanitized["navigation"]["availability_links"] == []
    assert sanitized["navigation"]["last_navigation_hints"] == []
    assert sanitized["api_hints"]["blocked_endpoints"] == []
    assert sanitized["api_hints"]["source_observations"] == []
    assert sanitized["api_hints"]["wait_for_url_pattern"] is None


def test_archive_match_is_not_inferred_from_unit_content() -> None:
    """Only an explicit identity verdict can admit a route.

    Unit IDs, rents, counts, and comparison-feed agreement are deliberately
    absent from the materializer contract because any unit roster can belong
    to the wrong property and an external comparison feed can also be wrong.
    """

    profile = _profile()
    route = next(
        item for item in profile_routes("7", profile) if item.source == "navigation.winning_page_url"
    )
    decisions = route_decisions(
        "7",
        {
            "profile_routes": [
                {
                    "route_sha256": route.sha256,
                    "source": route.source,
                    "historical_winner": True,
                    "identity": {"status": "UNKNOWN", "evidence_source": "none"},
                    "unit_count": 100,
                    "unit_ids_overlap": 100,
                }
            ]
        },
        {},
    )

    assert decisions[route.sha256]["status"] == "UNRESOLVED"


def _write_profile(
    directory: Path,
    property_id: str,
    *,
    winner: str | None = None,
    endpoint: str | None = None,
) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    profile = ScrapeProfile(canonical_id=property_id)
    profile.navigation.entry_url = f"https://property-{property_id}.example.com/"
    profile.navigation.winning_page_url = winner
    if endpoint:
        profile.api_hints.known_endpoints = [ApiEndpoint(url_pattern=endpoint)]
    raw = profile.model_dump_json(indent=2)
    (directory / f"{property_id}.json").write_text(raw, encoding="utf-8")
    return json.loads(raw)


def _write_archive(path: Path, property_id: str, profile: dict, status: str) -> None:
    routes = profile_routes(property_id, profile)
    row = {
        "property_id": property_id,
        "profile_routes": [
            {
                "route_sha256": route.sha256,
                "source": route.source,
                "identity": {"status": status, "evidence_source": "test_identity"},
            }
            for route in routes
        ],
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def _args(
    profile_dirs: list[Path],
    archives: list[Path],
    output_dir: Path,
    *,
    required: list[Path] | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        profiles_dir=profile_dirs,
        archive_ledger=archives,
        live_winner_ledger=[],
        required_property_ids_file=required or [],
        output_dir=output_dir,
    )


def test_run_unions_disjoint_profile_sources(tmp_path: Path) -> None:
    first = _write_profile(
        tmp_path / "profiles-a",
        "7",
        winner="https://property-7.example.com/units",
    )
    second = _write_profile(
        tmp_path / "profiles-b",
        "8",
        endpoint="https://property-8.example.com/api/units",
    )
    archive_a = tmp_path / "archive-a.jsonl"
    archive_b = tmp_path / "archive-b.jsonl"
    _write_archive(archive_a, "7", first, "MATCH")
    _write_archive(archive_b, "8", second, "MATCH")

    summary = run(
        _args(
            [tmp_path / "profiles-a", tmp_path / "profiles-b"],
            [archive_a, archive_b],
            tmp_path / "output",
        )
    )

    assert summary["scope"]["source_profile_files"] == 2
    assert summary["scope"]["source_properties"] == 2
    assert summary["profile_status_counts"] == {"ADMIT": 2}
    assert sorted(path.stem for path in (tmp_path / "output" / "profiles").glob("*.json")) == [
        "7",
        "8",
    ]


def test_duplicate_sources_merge_routes_deterministically(tmp_path: Path) -> None:
    profile_a = _write_profile(
        tmp_path / "a-profiles",
        "7",
        winner="https://property-7.example.com/floorplans",
    )
    profile_z = _write_profile(
        tmp_path / "z-profiles",
        "7",
        endpoint="https://property-7.example.com/api/units",
    )
    archive_a = tmp_path / "archive-a.jsonl"
    archive_z = tmp_path / "archive-z.jsonl"
    _write_archive(archive_a, "7", profile_a, "MATCH")
    _write_archive(archive_z, "7", profile_z, "MATCH")

    run(
        _args(
            [tmp_path / "z-profiles", tmp_path / "a-profiles"],
            [archive_z, archive_a],
            tmp_path / "output-1",
        )
    )
    run(
        _args(
            [tmp_path / "a-profiles", tmp_path / "z-profiles"],
            [archive_a, archive_z],
            tmp_path / "output-2",
        )
    )

    first = (tmp_path / "output-1" / "profiles" / "7.json").read_bytes()
    second = (tmp_path / "output-2" / "profiles" / "7.json").read_bytes()
    assert first == second
    merged = json.loads(first)
    assert merged["navigation"]["winning_page_url"].endswith("/floorplans")
    assert merged["api_hints"]["known_endpoints"][0]["url_pattern"].endswith("/api/units")
    assert (tmp_path / "output-1" / "strict-profile-ledger.jsonl").read_bytes() == (
        tmp_path / "output-2" / "strict-profile-ledger.jsonl"
    ).read_bytes()


def test_conflicting_archive_identity_quarantines_route(tmp_path: Path) -> None:
    profile = _write_profile(
        tmp_path / "profiles",
        "7",
        winner="https://property-7.example.com/units",
    )
    match = tmp_path / "match.jsonl"
    mismatch = tmp_path / "mismatch.jsonl"
    _write_archive(match, "7", profile, "MATCH")
    _write_archive(mismatch, "7", profile, "MISMATCH")

    summary = run(_args([tmp_path / "profiles"], [match, mismatch], tmp_path / "output"))

    assert summary["profile_status_counts"] == {"QUARANTINE": 1}
    assert summary["evidence_source_counts"] == {"archive_identity_conflict": 1}
    assert not list((tmp_path / "output" / "profiles").glob("*.json"))


def test_missing_archive_route_is_review_not_key_error(tmp_path: Path) -> None:
    _write_profile(
        tmp_path / "profiles",
        "7",
        winner="https://property-7.example.com/units",
    )
    empty_archive = tmp_path / "empty.jsonl"
    empty_archive.write_text("", encoding="utf-8")

    summary = run(_args([tmp_path / "profiles"], [empty_archive], tmp_path / "output"))

    assert summary["profile_status_counts"] == {"REVIEW": 1}
    assert summary["route_status_counts"] == {"UNRESOLVED": 1}


def test_required_manifest_fails_when_any_profile_is_not_admitted(tmp_path: Path) -> None:
    profile = _write_profile(
        tmp_path / "profiles",
        "7",
        winner="https://property-7.example.com/units",
    )
    archive = tmp_path / "archive.jsonl"
    _write_archive(archive, "7", profile, "MATCH")
    manifest = tmp_path / "promotion_manifest.json"
    manifest.write_text(json.dumps({"create_ids": [7], "overlap_ids": [8]}), encoding="utf-8")

    assert load_required_property_ids([manifest]) == {"7", "8"}
    with pytest.raises(RuntimeError, match="required_profile_admission_failed:8"):
        run(
            _args(
                [tmp_path / "profiles"],
                [archive],
                tmp_path / "output",
                required=[manifest],
            )
        )
    summary = json.loads((tmp_path / "output" / "summary.json").read_text(encoding="utf-8"))
    assert summary["required_admission"] == {
        "required": 2,
        "admitted": 1,
        "missing_source_ids": ["8"],
        "not_admitted_ids": ["8"],
        "passed": False,
    }


def test_cli_allows_archive_only_materialization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "materialize_strict_warm_profiles.py",
            "--profiles-dir",
            str(tmp_path / "profiles"),
            "--archive-ledger",
            str(tmp_path / "archive.jsonl"),
            "--output-dir",
            str(tmp_path / "output"),
        ],
    )

    args = parse_args()

    assert args.live_winner_ledger == []
