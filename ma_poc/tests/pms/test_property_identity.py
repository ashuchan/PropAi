from __future__ import annotations

from ma_poc.pms.property_identity import (
    MATCH,
    MISMATCH,
    addresses_match,
    evaluate_property_identity,
    knock_observed_identity,
    names_match,
)
from ma_poc.pms.source_provenance import (
    build_unit_source_provenance,
    sanitise_source_url,
)


def test_generic_page_title_does_not_become_identity_from_url_path() -> None:
    decision = evaluate_property_identity(
        configured_name="Legacy Rose Flats",
        configured_url="https://operator.example/properties/property-detail/legacy-rose-flats",
        observed_name="Property Detail",
    )

    assert decision.status == "MISMATCH"
    assert "configured_url_slug_match" not in decision.evidence


def test_distinctive_alias_can_be_corroborated_by_configured_url() -> None:
    decision = evaluate_property_identity(
        configured_name="AvalonBay Montville",
        configured_url="https://operator.example/new-jersey/montville/avalon-montville",
        observed_name="Avalon Montville",
    )

    assert decision.status == "MATCH"
    assert "configured_url_slug_match" in decision.evidence


def test_novi_flats_does_not_match_novi_rise() -> None:
    decision = evaluate_property_identity(
        configured_name="Novi Flats",
        configured_address="25 Barbrick Ave SW",
        observed_name="NOVI Rise",
    )
    assert decision.status == MISMATCH


def test_brookside_commons_does_not_match_kelson_row() -> None:
    decision = evaluate_property_identity(
        configured_name="Brookside Commons",
        configured_address="235 Main St",
        observed_name="Kelson Row",
    )
    assert decision.status == MISMATCH


def test_roman_and_arabic_phase_are_equivalent_but_other_phase_is_rejected() -> None:
    assert names_match("Turtle Dove I", "Turtle Dove 1") == (True, "name_exact")
    ok = evaluate_property_identity(configured_name="Turtle Dove I", observed_name="Turtle Dove 1")
    bad = evaluate_property_identity(configured_name="Turtle Dove I", observed_name="Turtle Dove 2")
    assert ok.status == MATCH
    assert bad.status == MISMATCH
    assert "phase_conflict" in bad.evidence


def test_strong_address_match_allows_legitimate_branding_variant() -> None:
    decision = evaluate_property_identity(
        configured_name="Ridgewood Apartments",
        configured_address="3616 Hogans Run Road",
        observed_name="(RDG) Ridgewood Court",
        observed_address="3616 Hogans Run Rd",
    )
    assert decision.status == MATCH
    assert any(e.startswith("address_") for e in decision.evidence)


def test_address_match_requires_same_house_and_street() -> None:
    assert addresses_match("25 Barbrick Ave SW", "25 Barbrick Avenue Southwest")[0]
    assert addresses_match("25 Barbrick Ave SW", "25 Barbrick Avenue Southwest, Concord NC 28025")[0]
    assert not addresses_match("25 Barbrick Ave SW", "5150 Duke Ellington Way")[0]


def test_knock_identity_reads_current_nested_address_shape() -> None:
    observed = knock_observed_identity(
        {
            "property": {
                "data": {
                    "location": {
                        "name": "Willow Glen",
                        "address": {
                            "street": "1301 Sycamore School Rd",
                            "city": "Fort Worth",
                            "state": "TX",
                            "zip": "76134",
                        },
                    }
                }
            }
        }
    )
    assert observed == {
        "name": "Willow Glen",
        "address": "1301 Sycamore School Rd",
        "city": "Fort Worth",
        "state": "TX",
        "zip": "76134",
    }


def test_source_provenance_hashes_body_and_redacts_secrets() -> None:
    url = "https://api.example.test/units?property_id=42&apiKey=secret&token=also-secret"
    assert sanitise_source_url(url).endswith("property_id=42&apiKey=%3Credacted%3E&token=%3Credacted%3E")
    provenance = build_unit_source_provenance(
        provider="test",
        source_url=url,
        body={"units": [{"id": "101"}]},
        unit_count=1,
    )
    assert provenance["unit_count"] == 1
    assert len(provenance["response_sha256"]) == 64
    assert "secret" not in provenance["source_url"]
