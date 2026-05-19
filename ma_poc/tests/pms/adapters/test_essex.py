"""Essex Property Trust adapter — parser + detector wiring tests.

Acceptance (2026-05-17, real DevTools-captured payload):
- /api/properties/{pid}/units/{uid}/availability response →
  one unit-level row: unit_number = unit_id, rent = the 12-month term
  on the EARLIEST AVAILABLE date, availability_date = that date.
- Leading empty terms_by_month (unit not available that day) skipped.
- All-empty terms → no row (unit not currently available).
- Detector routes host essexapartmenthomes.com → pms="essex".
"""
from __future__ import annotations

import ma_poc.pms.adapters  # noqa: F401  # populate adapter registry
from ma_poc.pms.adapters.essex import (
    EssexAdapter,
    parse_essex_availability,
)
from ma_poc.pms.adapters.registry import get_adapter
from ma_poc.pms.detector import detect_pms

# Faithful slice of the real city-view capture (prop 492967, unit
# 6302379, fp 2101784): 5/16 has empty terms (not available that day);
# 5/17 is the earliest available date; 12mo rent = 2487.
_REAL = {
    "success": True,
    "result": {
        "property_id": 492967,
        "floorplan_id": 2101784,
        "unit_id": 6302379,
        "start_date": "2026-05-16T00:00:00+00:00",
        "end_date": "2026-05-31T00:00:00+00:00",
        "pricing_by_date": [
            {"date": "2026-05-16T00:00:00+00:00", "terms_by_month": []},
            {
                "date": "2026-05-17T00:00:00+00:00",
                "terms_by_month": [
                    {"term_months": 1, "rent": "9319.00", "deposit": "600.00"},
                    {"term_months": 11, "rent": "2539.00", "deposit": "600.00"},
                    {"term_months": 12, "rent": "2487.00", "deposit": "600.00"},
                ],
            },
            {
                "date": "2026-05-18T00:00:00+00:00",
                "terms_by_month": [
                    {"term_months": 12, "rent": "2487.00", "deposit": "600.00"}
                ],
            },
        ],
    },
}


def test_parser_picks_12mo_on_earliest_available_date() -> None:
    units = parse_essex_availability(_REAL, "https://essex/x")
    assert len(units) == 1
    u = units[0]
    assert u["unit_number"] == "6302379"
    assert u["market_rent_low"] == 2487
    assert u["market_rent_high"] == 2487
    assert u["availability_date"] == "2026-05-17"  # 5/16 empty → skipped
    assert u["availability_status"] == "AVAILABLE"
    assert u["extraction_tier"] == "TIER_1_API_ESSEX"


def test_parser_falls_back_to_longest_term_when_no_12mo() -> None:
    body = {
        "success": True,
        "result": {
            "unit_id": 99,
            "floorplan_id": 7,
            "pricing_by_date": [
                {
                    "date": "2026-06-01T00:00:00+00:00",
                    "terms_by_month": [
                        {"term_months": 3, "rent": "4000.00"},
                        {"term_months": 9, "rent": "2900.00"},
                    ],
                }
            ],
        },
    }
    u = parse_essex_availability(body, "x")
    assert len(u) == 1
    assert u[0]["market_rent_low"] == 2900  # longest term (9mo), not 3mo


def test_parser_skips_unit_with_no_availability() -> None:
    body = {
        "success": True,
        "result": {
            "unit_id": 5,
            "pricing_by_date": [
                {"date": "2026-06-01T00:00:00+00:00", "terms_by_month": []},
                {"date": "2026-06-02T00:00:00+00:00", "terms_by_month": []},
            ],
        },
    }
    assert parse_essex_availability(body, "x") == []


def test_parser_malformed() -> None:
    assert parse_essex_availability({}, "x") == []
    assert parse_essex_availability({"success": True, "result": {}}, "x") == []
    assert parse_essex_availability({"result": "notadict"}, "x") == []


def test_detector_routes_essex_host() -> None:
    d = detect_pms("https://www.essexapartmenthomes.com/apartments/hayward/city-view")
    assert d.pms == "essex", d.pms


def test_adapter_registered_and_body_check() -> None:
    a = get_adapter("essex")
    assert isinstance(a, EssexAdapter)
    assert a.pms_name == "essex"
    assert "essexapartmenthomes.com" in a.static_fingerprints()
    assert a.matches_response_body(_REAL)
    assert not a.matches_response_body({"success": True})
    assert not a.matches_response_body("not a dict")
