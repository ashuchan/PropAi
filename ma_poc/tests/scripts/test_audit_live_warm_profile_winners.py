from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.scripts.diagnostics.audit_july_gcp_profile_evidence import profile_routes
from ma_poc.scripts.diagnostics.audit_live_warm_profile_winners import (
    _appfolio_scope_decision,
    _is_public_http_url,
    _retry_keys,
    audit_route,
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


def test_discovery_can_return_every_unresolved_public_route(tmp_path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    profile = {
        "navigation": {
            "winning_page_url": "https://current.example.com/units",
            "availability_links": [
                "https://historic.example.com/units",
                "https://alternate.example.com/units",
            ],
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
                "historical_winner": route.url == "https://historic.example.com/units",
                "identity": {"status": "UNKNOWN"},
            }
            for route in routes
        ],
    }
    ledger = tmp_path / "archive.jsonl"
    ledger.write_text(json.dumps(archive) + "\n", encoding="utf-8")

    discovered, skipped = discover_winner_routes(
        profiles,
        ledger,
        all_unresolved_routes=True,
    )

    assert {item.route.url for item in discovered} == {
        "https://current.example.com/units",
        "https://historic.example.com/units",
        "https://alternate.example.com/units",
    }
    assert skipped["lower_priority_candidate"] == 0


def test_retry_keys_select_only_requested_prior_decisions(tmp_path: Path) -> None:
    ledger = tmp_path / "direct.jsonl"
    ledger.write_text(
        "\n".join(
            json.dumps(
                {
                    "property_id": property_id,
                    "route_sha256": route_hash,
                    "decision": {"status": status},
                }
            )
            for property_id, route_hash, status in (
                ("7", "a" * 64, "FETCH_FAILED"),
                ("8", "b" * 64, "UNKNOWN"),
                ("9", "c" * 64, "MATCH"),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert _retry_keys([ledger], {"FETCH_FAILED", "UNKNOWN"}) == {
        f"7|{'a' * 64}",
        f"8|{'b' * 64}",
    }


def test_hyperbrowser_audit_uses_clean_backend_and_preserves_identity_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = {
        "navigation": {"winning_page_url": "https://property.example.com/units"},
        "api_hints": {},
    }
    selected = profile_routes("7", profile)[0]
    from ma_poc.scripts.diagnostics.audit_live_warm_profile_winners import WinnerRoute

    winner = WinnerRoute("7", selected, False)

    async def fake_hb_raw_get(
        url: str,
        property_id: str,
        **kwargs: object,
    ) -> tuple[int, str]:
        assert url == selected.url
        assert property_id == "7"
        assert kwargs["priority"] is True
        return 200, '<script type="application/ld+json">{"name":"Test Property"}</script>'

    monkeypatch.setattr(
        "ma_poc.fetch.hyperbrowser_backend.hb_raw_get",
        fake_hb_raw_get,
    )

    record = audit_route(
        winner,
        {"name": "Test Property", "address": "", "city": "", "state": "", "zip": ""},
        1.0,
        "hyperbrowser",
    )

    assert record["fetch_backend"] == "hyperbrowser"
    assert record["http_status"] == 200
    assert record["decision"]["status"] == "MATCH"
    assert record["decision"]["evidence"] == ["name_exact"]
