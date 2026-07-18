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
    _PROP_ID_RE,
    EssexAdapter,
    build_unit_id_to_name_map,
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


# ─────────────────────────────────────────────────────────────────────
# 2026-05-24 — per-unit fallback hardening: use the bulk-response's
# unit_id → name map so the per-unit /availability endpoint doesn't
# ship the 7-digit internal unit_id as unit_number.
#
# Live-verified across 10 Essex properties (pid 491713/510844/510849/
# 510892/510898/513997/514248/514264/514272/547482): bulk SPA response
# carries name='G104'/'B303'/'099'/'PH-E' etc. The per-unit endpoint
# only carries unit_id=6302046 (internal). Map keeps the displayed
# value flowing even on fallback.
# ─────────────────────────────────────────────────────────────────────


_BULK_WITH_NAMES = {
    "success": True,
    "result": {
        "floorplans": [
            {
                "floorplan_id": 2101784,
                "name": "A1",
                "units": [
                    {"unit_id": 6302379, "name": "G104", "minimum_rent": 2487},
                    {"unit_id": 6302046, "name": "B303", "minimum_rent": 2137},
                    {"unit_id": 6301713, "name": "099", "minimum_rent": 2277},
                ],
            }
        ]
    },
}


def test_build_unit_id_to_name_map_walks_floorplans_units() -> None:
    m = build_unit_id_to_name_map(_BULK_WITH_NAMES)
    assert m == {
        "6302379": "G104",
        "6302046": "B303",
        "6301713": "099",
    }


def test_build_unit_id_to_name_map_handles_malformed() -> None:
    assert build_unit_id_to_name_map(None) == {}
    assert build_unit_id_to_name_map({}) == {}
    assert build_unit_id_to_name_map({"result": "notadict"}) == {}
    assert build_unit_id_to_name_map({"result": {"floorplans": "notalist"}}) == {}
    assert build_unit_id_to_name_map({"result": {"floorplans": [None, 42]}}) == {}
    assert build_unit_id_to_name_map(
        {"result": {"floorplans": [{"units": "notalist"}]}}
    ) == {}
    assert build_unit_id_to_name_map(
        {"result": {"floorplans": [{"units": [{"unit_id": 1}]}]}}
    ) == {}  # missing name → skipped


def test_per_unit_fallback_uses_bulk_map_when_provided() -> None:
    """The audit-prevention case: per-unit /availability response
    only has unit_id=6302379, but bulk_map says it's 'G104'.
    parse_essex_availability MUST ship 'G104' as unit_number."""
    units = parse_essex_availability(
        _REAL,
        "https://essex/x/units/6302379/availability",
        unit_id_to_name={"6302379": "G104"},
    )
    assert len(units) == 1
    assert units[0]["unit_number"] == "G104", (
        f"per-unit fallback should resolve via bulk map; got "
        f"{units[0]['unit_number']!r} — the unit_id leak is back."
    )


def test_per_unit_fallback_falls_back_to_unit_id_when_map_missing() -> None:
    """When no map is supplied (legacy callers / no bulk available),
    preserve the prior behaviour of shipping the internal unit_id."""
    units = parse_essex_availability(_REAL, "x")
    assert units[0]["unit_number"] == "6302379"  # legacy fallback


def test_per_unit_fallback_falls_back_to_unit_id_when_map_lacks_id() -> None:
    """Map present but doesn't include this unit_id → fall back to id."""
    units = parse_essex_availability(
        _REAL, "x", unit_id_to_name={"9999999": "OTHER"}
    )
    assert units[0]["unit_number"] == "6302379"


def test_per_unit_fallback_handles_empty_map() -> None:
    units = parse_essex_availability(_REAL, "x", unit_id_to_name={})
    assert units[0]["unit_number"] == "6302379"


class TestPropertyIdExtraction:
    """2026-07-18: essexapartmenthomes.com migrated to the Next.js App Router.
    The propertyId now lives in an ``__next_f`` streaming blob with
    BACKSLASH-ESCAPED JSON quotes (``\\"propertyId\\":\\"514264\\"``). The old
    literal-quote regex matched 0/23 live props → every Essex property fell to
    FAILED_NO_DATA. These lock the backslash-tolerant pattern (validated live
    23/23, 310 units)."""

    def test_extracts_escaped_quote_approuter_form(self) -> None:
        # verbatim shape from the live __next_f blob
        html = r'Center\",\"propertyId\":\"514264\",\"propertyCode\":\"p0523894\"'
        m = _PROP_ID_RE.search(html)
        assert m is not None and m.group(1) == "514264"

    def test_still_extracts_legacy_literal_quote_form(self) -> None:
        m = _PROP_ID_RE.search('foo "propertyId":"492967" bar')
        assert m is not None and m.group(1) == "492967"

    def test_extracts_from_api_path(self) -> None:
        m = _PROP_ID_RE.search("GET /api/properties/510892/availability?format=spa")
        assert m is not None and m.group(1) == "510892"

    def test_does_not_capture_property_code_decoy(self) -> None:
        # propertyCode "p0523894" is a decoy (the bulk API 404s on it); the
        # pattern anchors on propertyId, so a code-only blob yields no match.
        m = _PROP_ID_RE.search(r'\"propertyCode\":\"p0523894\"')
        assert m is None
