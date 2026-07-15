"""Deterministic ($0) induced-mapping learning wired into profile_updater.

learn_induced_field_mappings() induces a JSON field mapping from this run's
gold units + raw API bodies and persists it as an LlmFieldMapping — so a
later run replays it with zero LLM cost. The inducer's fidelity gate means
only mappings that reproduce the marketing unit# roster are saved.
"""
from __future__ import annotations

from models.scrape_profile import ScrapeProfile
from services.profile_updater import learn_induced_field_mappings

# Funnel-shaped API body + the gold units a Tier-1 extraction produced from it.
_BODY = {
    "result": {
        "units": [
            {"name": "221", "minimum_rent": 2579, "beds": 1, "baths": 1, "sqft": 786},
            {"name": "225", "minimum_rent": 2600, "beds": 1, "baths": 2, "sqft": 790},
            {"name": "644", "minimum_rent": 3100, "beds": 2, "baths": 2, "sqft": 1100},
        ]
    }
}
_GOLD = [
    {"unit_number": "221", "market_rent_low": 2579, "bedrooms": 1, "sqft": 786},
    {"unit_number": "225", "market_rent_low": 2600, "bedrooms": 1, "sqft": 790},
    {"unit_number": "644", "market_rent_low": 3100, "bedrooms": 2, "sqft": 1100},
]
_URL = "https://demo.example/api/v1/units"


def test_induces_and_persists_json_mapping() -> None:
    p = ScrapeProfile(canonical_id="induce-001")
    assert not p.api_hints.llm_field_mappings

    saved = learn_induced_field_mappings(p, _GOLD, {_URL: _BODY})

    assert saved == 1
    maps = p.api_hints.llm_field_mappings
    assert len(maps) == 1
    m = maps[0]
    assert m.response_envelope == "result.units"
    assert m.json_paths.get("unit_number") == "name"
    assert m.json_paths.get("rent_low") == "minimum_rent"


def test_no_units_saves_nothing() -> None:
    p = ScrapeProfile(canonical_id="induce-002")
    assert learn_induced_field_mappings(p, [], {_URL: _BODY}) == 0
    assert not p.api_hints.llm_field_mappings


def test_mismatched_body_saves_nothing() -> None:
    # gold roster the body cannot reproduce → fidelity gate rejects → no save
    p = ScrapeProfile(canonical_id="induce-003")
    other = {"result": {"units": [{"name": "999", "minimum_rent": 1, "beds": 9}]}}
    assert learn_induced_field_mappings(p, _GOLD, {_URL: other}) == 0
    assert not p.api_hints.llm_field_mappings


def test_flag_off_disables_learning(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_INDUCED_MAPPING_LEARNING", "false")
    p = ScrapeProfile(canonical_id="induce-004")
    assert learn_induced_field_mappings(p, _GOLD, {_URL: _BODY}) == 0
    assert not p.api_hints.llm_field_mappings
