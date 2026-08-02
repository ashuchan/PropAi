from __future__ import annotations

import json

from ma_poc.scripts.diagnostics.audit_july_gcp_profile_evidence import profile_routes
from ma_poc.scripts.diagnostics.audit_live_warm_profile_winners import (
    _appfolio_scope_decision,
    _is_public_http_url,
    discover_winner_routes,
)

_APPFOLIO_HTML = """
<article class="listing-item result js-listing-item" data-listing-id="233">
  <div class="js-listing-blurb-rent">$1,335</div>
  <div class="js-listing-blurb-bed-bath">3 bd / 2 ba</div>
  <div class="js-listing-square-feet">Square Feet: 1,342</div>
  <div class="js-listing-available">5/22/26</div>
  <div class="js-listing-address"><span>5312 Gatehouse Dr Apt 3, Columbus, OH 43213</span></div>
</article>
<article class="listing-item result js-listing-item" data-listing-id="265">
  <div class="js-listing-blurb-rent">$1,500</div>
  <div class="js-listing-blurb-bed-bath">1 bd / 1 ba</div>
  <div class="js-listing-square-feet">Square Feet: 540</div>
  <div class="js-listing-available">6/15/26</div>
  <div class="js-listing-address"><span>456 Wrong Ave, Columbus, OH 43210</span></div>
</article>
"""


def test_public_url_gate_rejects_local_and_private_targets() -> None:
    assert _is_public_http_url("https://example.com/floorplans")
    assert not _is_public_http_url("file:///etc/passwd")
    assert not _is_public_http_url("http://localhost:8080/private")
    assert not _is_public_http_url("http://127.0.0.1/private")
    assert not _is_public_http_url("http://169.254.169.254/metadata")


def test_appfolio_portfolio_route_is_bound_by_filtered_listing_address() -> None:
    configured = {
        "name": "Estates on Main",
        "address": "5312 Gatehouse Dr",
        "city": "Columbus",
        "state": "OH",
        "zip": "43213",
    }
    decision, scope = _appfolio_scope_decision(
        _APPFOLIO_HTML, "https://tenant.appfolio.com/listings", configured
    )
    assert decision is not None
    assert decision["status"] == "MATCH"
    assert "appfolio_listing_address_scope" in decision["evidence"]
    assert scope == {
        "listing_rows": 2,
        "distinct_addresses": 2,
        "judgeable_addresses": 2,
        "matched_addresses": 1,
    }


def test_discovery_prefers_stored_winner_over_historical_alternate(tmp_path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    profile = {
        "navigation": {
            "winning_page_url": "https://current.example.com/units",
            "availability_links": ["https://historic.example.com/units"],
        },
        "api_hints": {},
    }
    (profiles / "7.json").write_text(json.dumps(profile), encoding="utf-8")
    routes = profile_routes("7", profile)
    archive = {
        "property_id": "7",
        "profile_routes": [
            {
                "route_sha256": route.sha256,
                "historical_winner": route.source == "navigation.availability_links",
                "identity": {"status": "UNKNOWN"},
            }
            for route in routes
        ],
    }
    ledger = tmp_path / "archive.jsonl"
    ledger.write_text(json.dumps(archive) + "\n", encoding="utf-8")

    discovered, skipped = discover_winner_routes(profiles, ledger)
    assert len(discovered) == 1
    assert discovered[0].route.source == "navigation.winning_page_url"
    assert skipped["lower_priority_candidate"] == 1
