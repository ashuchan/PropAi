from __future__ import annotations

from ma_poc.scripts.diagnostics.audit_july_gcp_profile_evidence import profile_routes
from ma_poc.scripts.diagnostics.materialize_strict_warm_profiles import (
    route_decisions,
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
