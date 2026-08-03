from __future__ import annotations

from ma_poc.scripts.diagnostics.audit_july_gcp_profile_evidence import (
    canonical_url,
    captured_body_for_url,
    decide_candidates,
    generic_identity_candidates,
    html_identity_candidates,
    parse_report,
    parse_structured_body,
    profile_routes,
    route_equivalent,
    safe_locator,
)


def test_report_parser_links_winner_api_to_body_index() -> None:
    report = """**Extraction Tier:** `TIER_1_KNOCK_API`
**Winning Source:** https://doorway-api.knockrentals.com/v1/property/123/units
**API responses captured:** 2

### APIs Captured (from homepage load)

1. `https://doorway-api.knockrentals.com/v1/property/community/abc`
2. `https://doorway-api.knockrentals.com/v1/property/123/units`

## Raw API Inventory

### API Response Bodies

<details>
<summary>API 2: route</summary>

```json
{"units_data": {"units": []}}
```
</details>
"""
    parsed = parse_report(report)
    assert parsed["tier"] == "TIER_1_KNOCK_API"
    assert parsed["api_count"] == 2
    assert route_equivalent(parsed["winner"], parsed["api_urls"][2])
    assert parse_structured_body(parsed["body_snippets"][2]) == {"units_data": {"units": []}}


def test_captured_body_prefers_full_sample_over_report_preview() -> None:
    report = {
        "api_urls": {1: "https://api.example.test/units?property_id=7"},
        "body_snippets": {1: '{"propertyName":"preview"}'},
    }
    sample = [
        {
            "url": "https://api.example.test/units?property_id=7&cache=1",
            "body": "{'propertyName': 'full sample'}",
        }
    ]
    raw, parsed, source, provider = captured_body_for_url(
        "https://api.example.test/units?property_id=7&cache=2",
        report,
        sample,
        "example",
    )
    assert raw == "{'propertyName': 'full sample'}"
    assert parsed == {"propertyName": "full sample"}
    assert source == "api_sample"
    assert provider == "example"


def test_captured_body_uses_parseable_report_when_sample_is_truncated() -> None:
    report = {
        "api_urls": {1: "https://api.example.test/units?property_id=7"},
        "body_snippets": {1: '{"propertyName":"report"}'},
    }
    sample = [
        {
            "url": "https://api.example.test/units?property_id=7",
            "body": '{"propertyName":"truncated',
        }
    ]
    _raw, parsed, source, _provider = captured_body_for_url(
        "https://api.example.test/units?property_id=7", report, sample
    )
    assert parsed == {"propertyName": "report"}
    assert source == "report_snippet"


def test_profile_routes_resolve_relative_path_and_deduplicate() -> None:
    profile = {
        "navigation": {
            "entry_url": "https://example.test/home",
            "winning_page_url": "https://example.test/floorplans",
            "availability_page_path": "/floorplans",
            "availability_links": [],
        },
        "api_hints": {
            "known_endpoints": [{"url_pattern": "api.example.test/units?property_id=7"}],
            "widget_endpoints": [],
            "llm_field_mappings": [],
            "field_patches": [],
        },
    }
    routes = profile_routes("42", profile)
    assert len(routes) == 2
    assert canonical_url(routes[0].url).startswith("https://example.test/")


def test_route_equivalence_keeps_identity_query_conflicts_distinct() -> None:
    assert route_equivalent(
        "https://api.test/units?property_id=7&date=2026-07-31",
        "https://api.test/units?property_id=7&date=2026-08-01",
    )
    assert not route_equivalent(
        "https://api.test/units?property_id=7",
        "https://api.test/units?property_id=8",
    )


def test_safe_locator_omits_sightmap_path_token() -> None:
    url = "https://sightmap.com/app/api/v1/secret-path-token/sightmaps/117233"
    assert safe_locator(url) == "asset:117233"


def test_generic_identity_requires_property_shaped_context() -> None:
    body = {
        "response": {
            "name": "Configured Apartments",
            "address": {"street": "100 Main St", "city": "Austin", "state": "TX"},
            "floorplans": [{"name": "A1"}],
        }
    }
    assert generic_identity_candidates(body) == [
        {
            "name": "Configured Apartments",
            "address": "100 Main St",
            "city": "Austin",
            "state": "TX",
            "zip": "",
        }
    ]


def test_html_title_can_corrobate_configured_name_without_causing_negative() -> None:
    configured = {
        "name": "Centennial Place",
        "address": "7001 S Congress Ave",
        "city": "Austin",
        "state": "TX",
        "zip": "78745",
    }
    matching = html_identity_candidates(
        "<html><head><title>Rental Apartments in Austin, TX | Centennial Place</title></head></html>"
    )
    assert decide_candidates(configured, matching)["status"] == "MATCH"

    generic = html_identity_candidates("<html><head><title>Online Leasing Portal</title></head></html>")
    assert decide_candidates(configured, generic)["status"] == "UNKNOWN"


def test_html_jsonld_name_and_address_can_prove_mismatch() -> None:
    configured = {
        "name": "Centennial Place",
        "address": "7001 S Congress Ave",
        "city": "Austin",
        "state": "TX",
        "zip": "78745",
    }
    body = """<script type="application/ld+json">
    {"@type":"ApartmentComplex","name":"Other Place","address":{
      "streetAddress":"10 Wrong St","addressLocality":"Dallas",
      "addressRegion":"TX","postalCode":"75001"}}
    </script>"""
    assert decide_candidates(configured, html_identity_candidates(body))["status"] == "MISMATCH"
