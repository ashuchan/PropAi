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

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import ma_poc.pms.adapters  # noqa: F401  # populate adapter registry
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.essex import (
    _PROP_ID_RE,
    EssexAdapter,
    _classify_essex_page,
    build_unit_id_to_name_map,
    parse_essex_availability,
    parse_essex_bulk,
)
from ma_poc.pms.adapters.registry import get_adapter
from ma_poc.pms.detector import detect_pms
from ma_poc.scripts.runners.jugnu import _format_v2_unit

_FIXTURES = Path(__file__).parent / "fixtures" / "essex"

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


# ─────────────────────────────────────────────────────────────────────
# 2026-08-02 complete-cohort audit remediation
# ─────────────────────────────────────────────────────────────────────


def _fixture_text(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _city_view_bulk() -> dict:
    return json.loads(_fixture_text("city_view_bulk.json"))


def _valid_page(name: str, property_id: str, path: str) -> str:
    return (
        'self.__next_f.push([1,"{\\"itemPath\\":\\"/404\\"}"]);'
        f'self.__next_f.push([1,"{{\\"itemPath\\":\\"{path}\\",'
        f'\\"fields\\":{{\\"PropertyName\\":{{\\"value\\":\\"{name}\\"}},'
        f'\\"PropertyId\\":{{\\"value\\":\\"{property_id}\\"}}}}}}"]);'
    )


def _ctx(
    body: str,
    *,
    name: str = "Belcarra",
    url: str = "https://www.essexapartmenthomes.com/apartments/bellevue/belcarra",
) -> AdapterContext:
    fetch_result = SimpleNamespace(
        body=body.encode(),
        url=url,
        final_url=url,
        status=200,
    )
    return AdapterContext(
        base_url=url,
        detected=detect_pms(url),
        profile=None,
        expected_total_units=None,
        property_id="P-ESSEX",
        property_name=name,
        fetch_result=fetch_result,
    )


class _Response:
    def __init__(
        self,
        *,
        status: int = 200,
        text: str = "",
        payload: object | None = None,
        url: str = "",
        json_error: type[Exception] | None = None,
    ) -> None:
        self.status_code = status
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self._payload = payload
        self.url = url
        self._json_error = json_error

    def json(self) -> object:
        if self._json_error is not None:
            raise self._json_error("forced invalid JSON")
        return self._payload


def test_bulk_parser_preserves_native_identity_and_exact_values_to_final() -> None:
    source_url = (
        "https://www.essexapartmenthomes.com/api/properties/492967/availability"
        "?start_date=2026-08-02&end_date=2026-10-01&format=spa"
    )
    parsed = parse_essex_bulk(
        _city_view_bulk(),
        source_url,
        property_id="492967",
        property_name="City View",
        page_url="https://www.essexapartmenthomes.com/city-view",
        page_final_url="https://www.essexapartmenthomes.com/apartments/hayward/city-view",
    )

    assert len(parsed) == 1
    unit = parsed[0]
    assert unit["unit_id"] == "6301828"
    assert unit["unit_number"] == "206"
    assert unit["unit_name"] == "206"
    assert unit["source_ids"] == {
        "essex_unit_id": "6301828",
        "essex_floorplan_id": "2224884",
        "essex_property_id": "492967",
    }
    assert unit["source_request_payload"] == {
        "property_id": "492967",
        "start_date": "2026-08-02",
        "end_date": "2026-10-01",
        "format": "spa",
    }
    assert unit["floor_plan_name"] == "Plan 1C"
    assert unit["bedrooms"] == "1"
    assert unit["bathrooms"] == "1"
    assert unit["sqft"] == "784"
    assert unit["market_rent_low"] == 2377
    assert unit["market_rent_high"] == 9129
    assert unit["availability_date"] == "2026-08-07"

    final = _format_v2_unit(
        unit,
        datetime(2026, 8, 2, 12, tzinfo=UTC),
        property_id="2282",
    )
    assert final["unit_id"] == "6301828"
    assert final["unit_name"] == "206"
    assert final["floor_plan_name"] == "Plan 1C"
    assert final["beds"] == 1
    assert final["baths"] == 1.0
    assert final["area"] == 784
    assert final["rent_low"] == 2377
    assert final["rent_high"] == 9129
    assert final["available_date"] == "2026-08-07"
    assert final["source_ids"]["essex_unit_id"] == "6301828"
    assert final["source_ids"]["essex_floorplan_id"] == "2224884"
    assert final["source_ids"]["essex_property_id"] == "492967"


@pytest.mark.parametrize(
    "configured_name",
    [
        "Avondale at Warner Center",
        "Brookside Oaks",
        "Carmel Creek",
        "Foster's Landing",
        "Mira Monte",
        "Summit Park Village",
        "The Palms at Laguna Niguel",
        "The Village at Toluca Lake I",
    ],
)
def test_all_eight_retained_canary_shells_classify_as_explicit_404(
    configured_name: str,
) -> None:
    evidence = _classify_essex_page(
        _fixture_text("canary_404_shell.html"),
        configured_name=configured_name,
        requested_url="https://www.essexapartmenthomes.com/configured-property",
        final_url="https://www.essexapartmenthomes.com/configured-property",
        status=200,
        via="retained_canary_fixture",
    )
    assert evidence.outcome == "SOURCE_404_SHELL"
    assert evidence.property_id == ""


def test_valid_page_selects_configured_property_not_earlier_sibling() -> None:
    evidence = _classify_essex_page(
        _fixture_text("belcarra_page.html"),
        configured_name="Belcarra",
        requested_url="https://www.essexapartmenthomes.com/apartments/bellevue/belcarra",
        final_url="https://www.essexapartmenthomes.com/apartments/bellevue/belcarra",
        status=200,
        via="retained_canary_fixture",
    )
    assert evidence.outcome == "SUCCESS"
    assert evidence.property_id == "510860"
    assert evidence.property_name == "Belcarra"


def test_legacy_page_with_one_property_id_remains_source_bound() -> None:
    evidence = _classify_essex_page(
        '<script>self.__next_f.push([1,"{\\"itemPath\\":\\"/apartments/hayward/city-view\\",'
        '\\"propertyId\\":\\"492967\\"}"])</script>',
        configured_name="City View",
        requested_url="https://www.essexapartmenthomes.com/apartments/hayward/city-view",
        final_url="https://www.essexapartmenthomes.com/apartments/hayward/city-view",
        status=200,
        via="legacy_fixture",
    )
    assert evidence.outcome == "SUCCESS"
    assert evidence.property_id == "492967"


@pytest.mark.asyncio
async def test_retained_404_shell_gets_one_fresh_page_api_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []
    page_url = "https://www.essexapartmenthomes.com/apartments/bellevue/belcarra"

    def fake_probe(url: str, **kwargs: object) -> _Response:
        calls.append((url, dict(kwargs)))
        if "/api/properties/" in url:
            return _Response(status=200, payload=_city_view_bulk(), url=url)
        return _Response(
            status=200,
            text=_valid_page("Belcarra", "510860", "/apartments/bellevue/belcarra"),
            url=page_url,
        )

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe)
    result = await EssexAdapter().extract(
        None,  # type: ignore[arg-type]
        _ctx(_fixture_text("canary_404_shell.html")),
    )

    assert len(result.units) == 1
    assert result.units[0]["unit_id"] == "6301828"
    assert result.units[0]["source_ids"]["essex_property_id"] == "510860"
    assert [item["via"] for item in result.api_responses] == [
        "essex_retained_page",
        "essex_fresh_configured_page",
        "essex_active_bulk",
    ]
    assert len(calls) == 2
    assert all(kwargs.get("unlocker") is False for _url, kwargs in calls)


@pytest.mark.asyncio
async def test_belcarra_retryable_bulk_failure_refreshes_page_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_probe(url: str, **_kwargs: object) -> _Response:
        calls.append(url)
        api_attempt = sum("/api/properties/" in value for value in calls)
        if "/api/properties/" in url and api_attempt == 1:
            return _Response(status=503, text="temporarily unavailable", url=url)
        if "/api/properties/" in url:
            return _Response(status=200, payload=_city_view_bulk(), url=url)
        return _Response(
            status=200,
            text=_fixture_text("belcarra_page.html"),
            url=url,
        )

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe)
    result = await EssexAdapter().extract(
        None,  # type: ignore[arg-type]
        _ctx(_fixture_text("belcarra_page.html")),
    )

    assert len(result.units) == 1
    assert calls[0].startswith(
        "https://www.essexapartmenthomes.com/api/properties/510860/availability"
    )
    assert calls[1].endswith("/apartments/bellevue/belcarra")
    assert calls[2].startswith(
        "https://www.essexapartmenthomes.com/api/properties/510860/availability"
    )
    outcomes = [item.get("essex_outcome") for item in result.api_responses]
    assert outcomes == ["SUCCESS", "BULK_HTTP_ERROR", "SUCCESS", "SUCCESS"]


@pytest.mark.parametrize(
    ("response_kind", "expected_outcome", "expected_api_calls"),
    [
        ("invalid_json", "BULK_JSON_ERROR", 2),
        ("invalid_shape", "BULK_SHAPE_REJECTED", 2),
        ("empty", "BULK_EMPTY", 1),
    ],
)
@pytest.mark.asyncio
async def test_belcarra_empty_exits_have_one_mutually_exclusive_cause(
    monkeypatch: pytest.MonkeyPatch,
    response_kind: str,
    expected_outcome: str,
    expected_api_calls: int,
) -> None:
    api_calls = 0

    def fake_probe(url: str, **_kwargs: object) -> _Response:
        nonlocal api_calls
        if "/api/properties/" not in url:
            return _Response(
                status=200,
                text=_fixture_text("belcarra_page.html"),
                url=url,
            )
        api_calls += 1
        if response_kind == "invalid_json":
            return _Response(status=200, text="not-json", url=url, json_error=ValueError)
        if response_kind == "invalid_shape":
            return _Response(status=200, payload={"message": "not a roster"}, url=url)
        return _Response(status=200, payload={"result": {"floorplans": []}}, url=url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe)
    result = await EssexAdapter().extract(
        None,  # type: ignore[arg-type]
        _ctx(_fixture_text("belcarra_page.html")),
    )

    assert result.units == []
    assert result.tier_used == f"TIER_1_API_ESSEX_{expected_outcome}"
    assert api_calls == expected_api_calls
    empty_causes = [error for error in result.errors if error.startswith("ESSEX_EMPTY_OUTCOME=")]
    assert len(empty_causes) == 1
    assert empty_causes[0].startswith(f"ESSEX_EMPTY_OUTCOME={expected_outcome} ")


@pytest.mark.asyncio
async def test_captured_sibling_bulk_is_rejected_before_fresh_exact_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx(_fixture_text("belcarra_page.html"))
    ctx._api_responses = [  # type: ignore[attr-defined]
        {
            "url": (
                "https://www.essexapartmenthomes.com/api/properties/510867/"
                "availability?format=spa"
            ),
            "status": 200,
            "body": _city_view_bulk(),
        }
    ]

    def fake_probe(url: str, **_kwargs: object) -> _Response:
        if "/api/properties/" in url:
            assert "/api/properties/510860/" in url
            return _Response(status=200, payload=_city_view_bulk(), url=url)
        return _Response(status=200, text=_fixture_text("belcarra_page.html"), url=url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe)
    result = await EssexAdapter().extract(None, ctx)  # type: ignore[arg-type]

    assert len(result.units) == 1
    assert result.units[0]["source_ids"]["essex_property_id"] == "510860"
    assert "CAPTURED_PROPERTY_MISMATCH" in {
        item.get("essex_outcome") for item in result.api_responses
    }
    assert result.winning_url and "/api/properties/510860/" in result.winning_url


def test_per_unit_fallback_rejects_page_response_property_mismatch() -> None:
    assert parse_essex_availability(
        _REAL,
        "https://essex/x",
        property_id="510860",
    ) == []


def test_per_unit_fallback_preserves_native_source_ids_when_bound() -> None:
    units = parse_essex_availability(
        _REAL,
        "https://essex/x",
        {"6302379": "G104"},
        property_id="492967",
        property_name="City View",
    )
    assert len(units) == 1
    assert units[0]["unit_id"] == "6302379"
    assert units[0]["unit_number"] == "G104"
    assert units[0]["source_ids"] == {
        "essex_unit_id": "6302379",
        "essex_floorplan_id": "2101784",
        "essex_property_id": "492967",
    }
