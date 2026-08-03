from __future__ import annotations

from ma_poc.scripts.diagnostics.audit_warm_profile_identity import (
    _observed_identity,
    _profile_status,
    discover_routes,
)


def test_discovers_routes_without_persisting_sightmap_path_token() -> None:
    profile = {
        "navigation": {
            "winning_page_url": "https://doorway-api.knockrentals.com/v1/property/community/abc123",
            "availability_links": [],
        },
        "api_hints": {
            "known_endpoints": [
                {"url_pattern": "https://doorway-api.knockrentals.com/v1/property/2025269/units"},
                {"url_pattern": "https://sightmap.com/app/api/v1/do-not-persist/sightmaps/117233"},
            ],
            "widget_endpoints": [],
            "llm_field_mappings": [],
            "field_patches": [],
        },
    }
    routes = discover_routes("42", profile)
    assert {(route.provider, route.route_kind, route.locator) for route in routes} == {
        ("knock", "community", "community:abc123"),
        ("knock", "numeric_property", "property:2025269"),
        ("sightmap", "asset", "asset:117233"),
    }
    assert all("do-not-persist" not in route.locator for route in routes)


def test_edifice_identity_uses_published_property_name() -> None:
    assert _observed_identity("edifice", {"property": "Cobblestone Apartments"}) == {
        "name": "Cobblestone Apartments",
        "address": "",
        "city": "",
        "state": "",
        "zip": "",
    }


def test_profile_requires_every_route_to_match() -> None:
    match = {"decision": {"status": "MATCH"}}
    unknown = {"decision": {"status": "UNKNOWN"}}
    mismatch = {"decision": {"status": "MISMATCH"}}
    assert _profile_status([match, match]) == "MATCH"
    assert _profile_status([match, unknown]) == "UNRESOLVED"
    assert _profile_status([match, mismatch]) == "MISMATCH"
