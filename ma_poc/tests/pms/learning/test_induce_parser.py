"""Tests for the API-supervised parser inducer (POC).

Covers: JSON-path induction (flat array), DOM/CSS induction (attrs + #NNN
unit-number text), the value-normalization matchers, key-affinity tiebreak,
and — most importantly — the marketing-unit-# fidelity GATE that rejects a
parser which doesn't reproduce the gold roster.
"""
from __future__ import annotations

import json

from ma_poc.pms.learning import (
    induce_dom_selectors,
    induce_fallback_parser,
    induce_json_field_mapping,
    replay,
    validate_induction,
)

# ── JSON induction ───────────────────────────────────────────────────────────

_API_BODY = {
    "result": {
        "units": [
            {"name": "221", "floorplan_name": "The Oxford", "beds": 1, "baths": 1,
             "sqft": 786, "minimum_rent": 2579, "maximum_rent": 9497,
             "availability_date": "2026-03-03T00:00:00.000Z"},
            {"name": "225", "floorplan_name": "The Oxford", "beds": 1, "baths": 2,
             "sqft": 790, "minimum_rent": 2600, "maximum_rent": 9500,
             "availability_date": "2026-04-01T00:00:00.000Z"},
            {"name": "644", "floorplan_name": "The Dover", "beds": 2, "baths": 2,
             "sqft": 1100, "minimum_rent": 3100, "maximum_rent": 9900,
             "availability_date": "2026-05-15T00:00:00.000Z"},
        ]
    }
}

_GOLD_JSON = [
    {"unit_number": "221", "market_rent_low": 2579, "market_rent_high": 9497,
     "bedrooms": 1, "bathrooms": 1, "sqft": 786, "floor_plan_name": "The Oxford",
     "availability_date": "2026-03-03"},
    {"unit_number": "225", "market_rent_low": 2600, "market_rent_high": 9500,
     "bedrooms": 1, "bathrooms": 2, "sqft": 790, "floor_plan_name": "The Oxford",
     "availability_date": "2026-04-01"},
    {"unit_number": "644", "market_rent_low": 3100, "market_rent_high": 9900,
     "bedrooms": 2, "bathrooms": 2, "sqft": 1100, "floor_plan_name": "The Dover",
     "availability_date": "2026-05-15"},
]


def test_json_induction_finds_envelope_and_keys() -> None:
    parser, rep = induce_json_field_mapping(_GOLD_JSON, _API_BODY, "funnel/x")
    assert parser is not None
    assert parser.kind == "json"
    assert parser.envelope == "result.units"
    jp = parser.to_llm_field_mapping()["json_paths"]
    assert jp["unit_number"] == "name"
    assert jp["rent_low"] == "minimum_rent"
    assert jp["sqft"] == "sqft"
    assert jp["floor_plan_name"] == "floorplan_name"
    assert rep.passed and rep.coverage == 1.0 and rep.id_fidelity == 1.0


def test_json_affinity_disambiguates_beds_vs_baths() -> None:
    # unit 221 has beds == baths == 1, so a naive value match is ambiguous;
    # key-name affinity must still map bathrooms -> "baths", rent_high -> "maximum_rent".
    parser, _ = induce_json_field_mapping(_GOLD_JSON, _API_BODY, "x")
    assert parser is not None
    jp = parser.to_llm_field_mapping()["json_paths"]
    assert jp["bathrooms"] == "baths"
    assert jp["bedrooms"] == "beds"
    assert jp["rent_high"] == "maximum_rent"


def test_json_replay_reproduces_marketing_numbers() -> None:
    parser, _ = induce_json_field_mapping(_GOLD_JSON, _API_BODY, "x")
    assert parser is not None
    rows = replay(parser, _API_BODY)
    assert {r["unit_number"] for r in rows} == {"221", "225", "644"}


def test_json_replay_from_raw_string_body() -> None:
    parser, _ = induce_json_field_mapping(_GOLD_JSON, _API_BODY, "x")
    assert parser is not None
    rows = replay(parser, json.dumps(_API_BODY))
    assert len(rows) == 3


# ── nested / grouped (join) JSON induction ───────────────────────────────────
# Units nested one level down under a group array (units-per-floorplan), the
# apts247 objects[].units[] shape. The inducer must flatten via a [*] wildcard.
_NESTED_BODY = {
    "meta": {"total": 3},
    "objects": [
        {"id": "FP1", "name": "The Oak", "bed": 1,
         "units": [
             {"number": "101", "price": 1500, "sqft": 700},
             {"number": "205", "price": 1550, "sqft": 700},
         ]},
        {"id": "FP2", "name": "The Elm", "bed": 2,
         "units": [
             {"number": "310", "price": 2100, "sqft": 1000},
         ]},
    ],
}
_NESTED_GOLD = [
    {"unit_number": "101", "market_rent_low": 1500, "sqft": 700},
    {"unit_number": "205", "market_rent_low": 1550, "sqft": 700},
    {"unit_number": "310", "market_rent_low": 2100, "sqft": 1000},
]


def test_nested_group_array_induction() -> None:
    parser, rep = induce_json_field_mapping(_NESTED_GOLD, _NESTED_BODY, "apts247/x")
    assert parser is not None
    # grouped envelope with the [*] wildcard
    assert parser.envelope == "objects[*].units"
    jp = parser.to_llm_field_mapping()["json_paths"]
    assert jp["unit_number"] == "number"
    assert jp["rent_low"] == "price"
    assert rep.passed and rep.coverage == 1.0 and rep.id_fidelity == 1.0


def test_nested_group_replay_flattens_all_units() -> None:
    parser, _ = induce_json_field_mapping(_NESTED_GOLD, _NESTED_BODY, "x")
    assert parser is not None
    rows = replay(parser, _NESTED_BODY)
    assert {r["unit_number"] for r in rows} == {"101", "205", "310"}


def test_nested_array_under_fixed_index() -> None:
    # AMLI shape: the unit groups live under a specific list index
    # (queries[2].state.data[*].units), not index 0.
    body = {"queries": [
        {"state": {"data": "noise"}},
        {"state": {"data": [{"x": 1}]}},
        {"state": {"data": [
            {"planId": "A", "units": [{"unitNumber": "12A", "rent": 1800}]},
            {"planId": "B", "units": [{"unitNumber": "34B", "rent": 2400}]},
        ]}},
    ]}
    gold = [
        {"unit_number": "12A", "market_rent_low": 1800},
        {"unit_number": "34B", "market_rent_low": 2400},
    ]
    parser, rep = induce_json_field_mapping(gold, body, "amli/x")
    assert parser is not None
    assert "[*]" in parser.envelope and "queries[2]" in parser.envelope
    assert rep.passed
    assert {r["unit_number"] for r in replay(parser, body)} == {"12A", "34B"}


# ── DOM induction ────────────────────────────────────────────────────────────

_WP = """
<div class="rm-ua-container">
  <a class="individual-item" data-bed="0" data-date="2026/08" data-rent="1030.00" href="/d/?uid=1">
    <div class="detail-content"><h2>The Pearl <span>8150 W 30 1/2 St, #308</span></h2>
    <div class="unit-specs">Beds 0 Bath 1.0 Rent $1,030.00</div></div></a>
  <a class="individual-item" data-bed="2" data-date="2026/06" data-rent="1445.00" href="/d/?uid=2">
    <div class="detail-content"><h2>The Emerald <span>8150 W 30 1/2 St, #209</span></h2>
    <div class="unit-specs">Beds 2 Bath 1.5 Rent $1,445.00</div></div></a>
</div>"""

_GOLD_DOM = [
    {"unit_number": "308", "market_rent_low": 1030, "bedrooms": "0", "availability_date": "2026-08-01"},
    {"unit_number": "209", "market_rent_low": 1445, "bedrooms": "2", "availability_date": "2026-06-01"},
]


def test_dom_induction_picks_field_rich_container() -> None:
    parser, rep = induce_dom_selectors(_GOLD_DOM, _WP)
    assert parser is not None
    # must pick the <a> carrying the data-* attrs, not the inner text <div>
    assert parser.container == "a.individual-item"
    assert rep.passed


def test_dom_induction_learns_attr_and_hash_rules() -> None:
    parser, _ = induce_dom_selectors(_GOLD_DOM, _WP)
    assert parser is not None
    rules = {f: (r.kind, r.ref) for f, r in parser.field_rules.items()}
    assert rules["unit_number"] == ("hash_in_text", None)
    assert rules["rent_low"] == ("attr", "data-rent")
    assert rules["bedrooms"] == ("attr", "data-bed")
    assert rules["availability_date"] == ("attr", "data-date")


def test_dom_replay_reproduces_unit_numbers() -> None:
    parser, _ = induce_dom_selectors(_GOLD_DOM, _WP)
    assert parser is not None
    rows = replay(parser, _WP)
    assert {r["unit_number"] for r in rows} == {"308", "209"}


# ── the fidelity gate ────────────────────────────────────────────────────────

def test_gate_rejects_mismatched_body() -> None:
    other = {"result": {"units": [{"name": "999", "minimum_rent": 1, "beds": 9}]}}
    parser, rep = induce_fallback_parser(_GOLD_JSON, other)
    assert parser is None
    assert not rep.passed


def test_gate_rejects_synthetic_only_gold() -> None:
    synthetic = [{"unit_id": "inferred_abc123", "market_rent_low": 2579}]
    parser, rep = induce_fallback_parser(synthetic, _API_BODY)
    assert parser is None
    assert not rep.passed


def test_validate_flags_low_coverage() -> None:
    # recovered only 1 of 3 gold -> coverage 33% -> fail
    recovered = [{"unit_number": "221"}]
    rep = validate_induction(recovered, _GOLD_JSON)
    assert not rep.passed
    assert rep.coverage < 0.8


def test_validate_flags_hallucinated_id() -> None:
    # recovered a unit_number not in gold -> id_fidelity < 1.0 -> fail
    recovered = [{"unit_number": n} for n in ("221", "225", "644", "000")]
    rep = validate_induction(recovered, _GOLD_JSON)
    assert rep.id_fidelity < 1.0
    assert not rep.passed


# ── orchestrator ─────────────────────────────────────────────────────────────

def test_fallback_prefers_json_for_json_body() -> None:
    parser, rep = induce_fallback_parser(_GOLD_JSON, _API_BODY, api_url="x")
    assert parser is not None and parser.kind == "json" and rep.passed


def test_fallback_uses_dom_for_html_body() -> None:
    parser, rep = induce_fallback_parser(_GOLD_DOM, _WP)
    assert parser is not None and parser.kind == "dom" and rep.passed


# ── persistence + DOM replay-into-pipeline ───────────────────────────────────

def test_parser_serialize_roundtrip() -> None:
    from ma_poc.pms.learning.induce_parser import parser_from_dict, parser_to_dict

    parser, _ = induce_dom_selectors(_GOLD_DOM, _WP)
    assert parser is not None
    d = parser_to_dict(parser)
    assert d["kind"] == "dom" and d["container"] == "a.individual-item"
    back = parser_from_dict(d)
    # round-trip replay reproduces the same unit numbers
    assert {r["unit_number"] for r in replay(back, _WP)} == {"308", "209"}


def test_replay_induced_dom_to_units_maps_pipeline_keys() -> None:
    from ma_poc.pms.learning import parser_to_dict, replay_induced_dom_to_units

    parser, _ = induce_dom_selectors(_GOLD_DOM, _WP)
    assert parser is not None
    units = replay_induced_dom_to_units(parser_to_dict(parser), _WP)
    assert {u["unit_number"] for u in units} == {"308", "209"}
    u0 = next(u for u in units if u["unit_number"] == "308")
    assert u0["market_rent_low"] == 1030          # rent_low → market_rent_low, int
    assert u0["extraction_tier"] == "TIER_1_INDUCED_DOM_REPLAY"


def test_replay_induced_dom_to_units_safe_on_junk() -> None:
    from ma_poc.pms.learning import replay_induced_dom_to_units

    assert replay_induced_dom_to_units({"kind": "json"}, "<html></html>") == []  # non-dom
    assert replay_induced_dom_to_units({}, "") == []                              # empty
