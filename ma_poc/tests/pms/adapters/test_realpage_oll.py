"""RealPage OLL (Online Leasing) adapter tests.

Covers the Category-D browser-intercept path: the stateful
``leasing.realpage.com/...appstate/v1/...OLL.SearchFloorPlan`` PUT
``Workflow`` response, plus the legacy shared ``/floorplans`` envelope
and detector routing. The Workflow fixture reconstructs the 4 verified
lochraven units (1805C AB / 1719F AB / 8309AT LO / 1715A AB) from the
committed contract at
investigations/2026-05-17-canary-iterate/artifacts/analysis/
categoryD_realpage_OLL_api.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.realpage_oll import (
    RealPageOllAdapter,
    dotnet_date_to_iso,
    parse_realpage_oll_workflow,
)
from ma_poc.pms.detector import detect_pms

FIXTURES = Path(__file__).parent / "fixtures" / "realpage_oll"


def _load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _workflow_body() -> dict:
    return _load_fixture("oll_workflow_lochraven.json")[0]["body"]


def _make_ctx(api_responses: list[dict]) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://lochravenapts.com/content/apply#k=44781",
        detected=detect_pms("https://lochravenapts.com/"),
        profile=None,
        expected_total_units=None,
        property_id="lochraven",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    pass


# ── Parser: happy path ────────────────────────────────────────────────────


def test_parse_oll_workflow_happy_path() -> None:
    units = parse_realpage_oll_workflow(_workflow_body(), "https://leasing.realpage.com/x")
    assert len(units) == 4
    by_no = {u["unit_number"]: u for u in units}
    assert set(by_no) == {"1805C AB", "1719F AB", "8309AT LO", "1715A AB"}

    u = by_no["1805C AB"]
    assert u["market_rent_low"] == 1255
    assert u["market_rent_high"] == 1515
    assert u["sqft"] == "700"
    assert u["availability_date"] == "2026-05-21"
    assert u["floor_plan_name"] == "The Oak (1x1)"
    assert u["bedrooms"] == "1"
    assert u["bathrooms"] == "1"
    assert u["extraction_tier"] == "TIER_1_API_REALPAGE_OLL"

    u2 = by_no["8309AT LO"]
    assert u2["market_rent_low"] == 1715
    assert u2["market_rent_high"] == 1815
    assert u2["availability_date"] == "2026-05-25"
    assert u2["floor_plan_name"] == "The Maple (2x2)"
    assert u2["bedrooms"] == "2"


def test_parse_oll_workflow_rent_within_sanity_range() -> None:
    units = parse_realpage_oll_workflow(_workflow_body(), "u")
    for u in units:
        assert u["market_rent_low"] is None or 200 <= u["market_rent_low"] <= 50000
        assert u["market_rent_high"] is None or 200 <= u["market_rent_high"] <= 50000


# ── Parser: no-Units floorplan fallback ───────────────────────────────────


def test_parse_oll_workflow_no_units_fallback() -> None:
    body = {
        "Workflow": {
            "ActivityGroups": [
                {
                    "GroupActivities": [
                        {
                            "__type": "RP.ApartmentSelectionLeaseMgmtActivity, RP",
                            "Floorplan": {
                                "Id": "999",
                                "Name": "Waitlist Studio",
                                "Bedrooms": 0,
                                "Bathrooms": "1",
                                "MinSquareFeet": 480,
                                "MinPriceRange": 1100,
                                "MaxPriceRange": 1200,
                                "AvailableUnits": 0,
                            },
                            "Units": [],
                        }
                    ]
                }
            ]
        }
    }
    units = parse_realpage_oll_workflow(body, "u")
    assert len(units) == 1
    assert units[0]["unit_number"] == ""
    assert units[0]["source_ids"]["floorplan_id"] == "999"
    assert units[0]["floor_plan_name"] == "Waitlist Studio"
    assert units[0]["bed_label"] == "Studio"
    assert "$1,100" in units[0]["rent_range"]


# ── Parser: malformed / empty bodies ──────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"Workflow": None},
        {"Workflow": {}},
        {"Workflow": {"ActivityGroups": None}},
        {"Workflow": {"ActivityGroups": []}},
        {"Workflow": {"ActivityGroups": [{"GroupActivities": None}]}},
        "not-a-dict",
        None,
    ],
)
def test_parse_oll_workflow_malformed_returns_empty(body: object) -> None:
    assert parse_realpage_oll_workflow(body, "u") == []  # type: ignore[arg-type]


def test_parse_oll_workflow_ignores_non_apartment_activities() -> None:
    body = {
        "Workflow": {"ActivityGroups": [{"GroupActivities": [{"__type": "RP.MenuActivity, RP", "Id": "m"}]}]}
    }
    assert parse_realpage_oll_workflow(body, "u") == []


# ── .NET date conversion ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/Date(1779339600000-0500)/", "2026-05-21"),
        ("/Date(1779339600000+0000)/", "2026-05-21"),
        ("/Date(1779339600000)/", "2026-05-21"),
        ("1779339600000", "2026-05-21"),
        ("", ""),
        (None, ""),
        ("/Date(not-a-number)/", ""),
        ("garbage", ""),
    ],
)
def test_dotnet_date_to_iso(raw: object, expected: str) -> None:
    assert dotnet_date_to_iso(raw) == expected


# ── Adapter wiring ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_extracts_oll_workflow() -> None:
    responses = _load_fixture("oll_workflow_lochraven.json")
    adapter = RealPageOllAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) == 4
    assert result.confidence >= 0.7
    assert "leasing.realpage.com" in (result.winning_url or "")
    assert all(u["extraction_tier"] == "TIER_1_API_REALPAGE_OLL" for u in result.units)


@pytest.mark.asyncio
async def test_adapter_still_handles_legacy_floorplans() -> None:
    """The pre-existing api.ws.realpage.com /floorplans path must keep working."""
    responses = [
        {
            "url": "https://api.ws.realpage.com/v2/property/7824595/floorplans",
            "body": {
                "status": 200,
                "response": {
                    "floorplans": [
                        {
                            "id": "1",
                            "name": "A1",
                            "bedRooms": "1",
                            "bathRooms": "1",
                            "minimumSquareFeet": "650",
                            "maximumSquareFeet": "650",
                            "minimumMarketRent": 1400.0,
                            "maximumMarketRent": 1600.0,
                        }
                    ]
                },
            },
        }
    ]
    adapter = RealPageOllAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1
    assert result.units[0]["extraction_tier"] == "TIER_1_API_REALPAGE_OLL"
    assert "$1,400" in result.units[0]["rent_range"]


@pytest.mark.asyncio
async def test_adapter_handles_current_response_units_envelope_and_date() -> None:
    responses = [
        {
            "url": "https://api.ws.realpage.com/v2/property/8648527/units",
            "body": {
                "response": {
                    "units": [
                        {
                            "id": 14185870,
                            "unitNumber": "128",
                            "rent": 1325,
                            "squareFeet": 730,
                            "internalAvailableDate": "2026-09-22 00:00 -0500",
                        }
                    ]
                }
            },
        }
    ]
    result = await RealPageOllAdapter().extract(  # type: ignore[arg-type]
        _DummyPage(), _make_ctx(responses)
    )

    assert len(result.units) == 1
    assert result.units[0]["unit_number"] == "128"
    assert result.units[0]["availability_date"] == "2026-09-22"
    assert result.units[0]["extraction_tier"] == "TIER_1_API_REALPAGE_OLL"


@pytest.mark.asyncio
async def test_floorplan_checkpoint_directly_enriches_public_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A captured /floorplans response must not hide the public unit roster."""
    responses = [
        {
            "url": "https://api.ws.realpage.com/v2/property/8648527/floorplans",
            "body": {
                "response": {
                    "floorplans": [
                        {
                            "id": "FP-A1",
                            "name": "A1",
                            "bedRooms": 1,
                            "minimumSquareFeet": 650,
                            "minimumMarketRent": 1400,
                        }
                    ]
                }
            },
        }
    ]
    ctx = _make_ctx(responses)
    ctx.fetch_result = SimpleNamespace(
        body=(
            b'<script>var propertyId = "8648527"; '
            b'var config = {apiKey: "public-browser-key"};</script>'
        ),
        final_url="https://www.plumtreeapt.com/",
    )
    calls: list[tuple[str, dict[str, object]]] = []

    class _Response:
        status_code = 200
        text = json.dumps(
            {
                "response": {
                    "units": [
                        {
                            "unitNumber": "128",
                            "rent": 1325,
                            "squareFeet": 730,
                            "internalAvailableDate": "2026-09-22 00:00 -0500",
                        }
                    ]
                }
            }
        )

    def _probe(url: str, **kwargs: object) -> _Response:
        calls.append((url, kwargs))
        return _Response()

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _probe)

    result = await RealPageOllAdapter().extract(None, ctx)  # type: ignore[arg-type]

    assert [unit["unit_number"] for unit in result.units] == ["128"]
    assert result.units[0]["availability_date"] == "2026-09-22"
    assert result.winning_url and result.winning_url.endswith(
        "/8648527/units?available=true&honordisplayorder=true"
    )
    assert len(calls) == 1
    assert calls[0][1]["unlocker"] is False
    headers = calls[0][1]["headers"]
    assert isinstance(headers, dict)
    assert headers["Origin"] == "https://www.plumtreeapt.com"
    assert headers["Referer"] == "https://www.plumtreeapt.com/"


@pytest.mark.asyncio
async def test_floorplan_checkpoint_is_preserved_when_unit_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        {
            "url": "https://api.ws.realpage.com/v2/property/8648527/floorplans",
            "body": {
                "response": {
                    "floorplans": [
                        {
                            "id": "FP-A1",
                            "name": "A1",
                            "bedRooms": 1,
                            "minimumSquareFeet": 650,
                            "minimumMarketRent": 1400,
                        }
                    ]
                }
            },
        }
    ]
    ctx = _make_ctx(responses)
    ctx.fetch_result = SimpleNamespace(
        body=b'propertyId = "8648527"; apiKey: "public-browser-key"',
        final_url="https://www.plumtreeapt.com/",
    )

    class _Blocked:
        status_code = 403
        text = "blocked"

    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda *_args, **_kwargs: _Blocked(),
    )

    result = await RealPageOllAdapter().extract(None, ctx)  # type: ignore[arg-type]

    assert len(result.units) == 1
    assert result.units[0]["floor_plan_name"] == "A1"
    assert result.winning_url and result.winning_url.endswith("/floorplans")


@pytest.mark.asyncio
async def test_floorplan_checkpoint_rejects_property_id_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        {
            "url": "https://api.ws.realpage.com/v2/property/8648527/floorplans",
            "body": {
                "response": {
                    "floorplans": [
                        {
                            "id": "FP-A1",
                            "name": "A1",
                            "bedRooms": 1,
                            "minimumSquareFeet": 650,
                            "minimumMarketRent": 1400,
                        }
                    ]
                }
            },
        }
    ]
    ctx = _make_ctx(responses)
    ctx.fetch_result = SimpleNamespace(
        body=b'propertyId = "9999999"; apiKey: "other-property-key"',
        final_url="https://operator.example/property-a/",
    )
    called = False

    def _must_not_probe(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("cross-property credentials must not be used")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _must_not_probe)

    result = await RealPageOllAdapter().extract(None, ctx)  # type: ignore[arg-type]

    assert called is False
    assert len(result.units) == 1
    assert result.units[0]["floor_plan_name"] == "A1"


@pytest.mark.asyncio
async def test_adapter_empty_on_no_data() -> None:
    adapter = RealPageOllAdapter()
    ctx = _make_ctx([{"url": "https://x.com/foo", "body": {"unrelated": True}}])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0
    assert result.errors


def test_adapter_static_fingerprints_nonempty() -> None:
    fps = RealPageOllAdapter().static_fingerprints()
    assert fps
    assert "leasing.realpage.com" in fps


def test_adapter_matches_response_body() -> None:
    adapter = RealPageOllAdapter()
    assert adapter.matches_response_body(_workflow_body()) is True
    assert adapter.matches_response_body({"response": {"floorplans": []}}) is True
    assert adapter.matches_response_body({"unrelated": 1}) is False
    assert adapter.matches_response_body("nope") is False


# ── Detector routing ──────────────────────────────────────────────────────


def test_detector_routes_oll_wizard_html_to_realpage_oll() -> None:
    html = (
        "<html><body>"
        '<div id="rp-leasing-widget" data-site="4000138"></div>'
        '<a href="https://lochravenapts.com/content/apply#k=44781">Apply Now</a>'
        "<script src='https://leasing.realpage.com/widget.js'></script>"
        "</body></html>"
    )
    r = detect_pms("https://lochravenapts.com/", page_html=html)
    assert r.pms == "realpage_oll"


def test_detector_onesite_subdomain_not_regressed() -> None:
    r = detect_pms("https://8756399.onlineleasing.realpage.com/#k=44781")
    assert r.pms == "onesite"


def test_detector_onesite_html_marker_not_regressed() -> None:
    html = '<a href="https://1234567.onlineleasing.realpage.com/">Apply</a>'
    r = detect_pms("https://example.com/", page_html=html)
    assert r.pms == "onesite"
