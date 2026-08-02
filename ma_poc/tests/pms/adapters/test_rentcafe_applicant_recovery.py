from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.rentcafe import (
    RentCafeAdapter,
    _stamp_sc_applicant_vanity_shell_provenance,
    _try_captured_securecafe_applicant,
    _try_rentcafe_securecafe_probe,
    parse_securecafe_applicant_floorplans,
)
from ma_poc.pms.detector import detect_pms


def _payload(
    *,
    property_name: str = "7400 Roosevelt Apartments",
    property_id: str = "477466",
    units: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "status": True,
        "floorPlanList": [
            {
                "floorPlan": {
                    "PropertyName": property_name,
                    "PropertyID": property_id,
                    "FloorPlanID": "81001",
                    "FloorPlanName": "One Bedroom",
                    "Beds": 1,
                    "Baths": 1,
                    "MinimumRent": 1650,
                    "MaximumRent": 1900,
                    "MinimumArea": 500,
                    "MaximumArea": 700,
                    "MinimumDeposit": 300,
                },
                "UnitAvailability": units
                if units is not None
                else [
                    {
                        "id": 99101,
                        "unitcode": "B004",
                        "DisplayMinRent": 1700,
                        "DisplayMaxRent": 1800,
                        "Area": 500,
                        "Deposit": 350,
                        "AvailableDate": "2026-08-15",
                        "Status": "Vacant Unrented Ready",
                    }
                ],
            }
        ],
    }


def _ctx(
    *,
    property_name: str = "7400 Roosevelt",
    property_id: str = "5719",
    body: str = "",
    base_url: str = "https://www.example.test/",
    address: str = "",
    city: str = "",
    state: str = "",
    zip_code: str = "",
) -> AdapterContext:
    context = AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id=property_id,
        fetch_result=SimpleNamespace(body=body.encode(), final_url=base_url),
        property_name=property_name,
        address=address,
        city=city,
        state=state,
        zip_code=zip_code,
    )
    context._api_responses = []  # type: ignore[attr-defined]
    return context


def _applicant_shell() -> str:
    return """
    <html><head><title>Applicant Portal | RentCafe</title></head>
    <body><script src="/applicant/js/index-abc123.js"></script></body></html>
    """


def _theme_payload(
    *,
    property_name: str,
    property_id: str,
    section_name: str,
    website: str,
    address: str,
    city: str,
    state: str,
    zip_code: str,
) -> dict[str, Any]:
    return {
        "propertyId": int(property_id),
        "propertyname": property_name,
        "sectionname": section_name,
        "propertyWebsiteUrl": website,
        "propertyaddress": address,
        "propertycity": city,
        "propertystate": state,
        "propertyzip": zip_code,
    }


class _Response:
    def __init__(self, status_code: int, payload: Any, url: str = "") -> None:
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else json.dumps(payload)
        self.url = url


class _Page:
    pass


def test_parser_uses_exact_unit_values_and_native_source_ids() -> None:
    source_url = (
        "https://roosevelt.securecafeapplicant.com/onlineleasing/api/"
        "floorplan/getfloorplanandavailableunits?propertyId=477466"
    )

    rows = parse_securecafe_applicant_floorplans(_payload(), source_url)

    assert len(rows) == 1
    row = rows[0]
    assert row["unit_number"] == "B004"
    assert row["sqft"] == "500"
    assert row["market_rent_low"] == 1700
    assert row["market_rent_high"] == 1800
    assert row["deposit"] == "$350"
    assert row["source_ids"] == {
        "securecafe_apartment_id": "99101",
        "securecafe_floorplan_id": "81001",
    }
    assert row["source_api_url"] == source_url
    assert row["source_property_id"] == "477466"
    assert row["source_property_name"] == "7400 Roosevelt Apartments"


def test_parser_rejects_placeholder_units_but_preserves_waitlist_plan() -> None:
    placeholders = [
        {"id": 1, "unitcode": "WAIT11", "DisplayMinRent": 1400, "Area": 700},
        {"id": 2, "unitcode": "MODEL2", "DisplayMinRent": 1500, "Area": 700},
        {"id": 3, "unitcode": "OFFICE3", "DisplayMinRent": 1600, "Area": 700},
        {
            "id": 4,
            "unitcode": "A104",
            "DisplayMinRent": 1700,
            "Area": 700,
            "Status": "Waitlist",
        },
    ]

    rows = parse_securecafe_applicant_floorplans(
        _payload(units=placeholders),
        "https://example.securecafeapplicant.com/api",
    )

    assert len(rows) == 1
    assert rows[0]["unit_number"] == ""
    assert rows[0]["is_floor_plan_level"] is True
    assert rows[0]["availability_status"] == "WAITLIST"


def test_parser_preserves_inquiry_waitlist_and_current_unit_date_semantics() -> None:
    """Zander-shaped regression through the production formatter boundary."""
    from ma_poc.scripts.runners.jugnu import _format_v2_unit

    payload = {
        "status": True,
        "floorPlanList": [
            {
                "floorPlan": {
                    "PropertyName": "Zander Place",
                    "PropertyID": "450410",
                    "FloorPlanID": "F-NP",
                    "FloorPlanName": "F-NP (Surface Lot Parking Only)",
                    "Beds": 0,
                    "Baths": 1,
                    "MinimumRent": 1500,
                    "AvailableUnits": 0,
                    "IsFullyOccupied": True,
                    "FloorPlanAvailable": False,
                },
                "UnitAvailability": [],
            },
            {
                "floorPlan": {
                    "PropertyName": "Zander Place",
                    "PropertyID": "450410",
                    "FloorPlanID": "C",
                    "FloorPlanName": "C",
                    "Beds": 1,
                    "Baths": 1,
                    "MinimumRent": 1600,
                    "AvailableUnits": 0,
                    "IsFullyOccupied": True,
                },
                "UnitAvailability": [
                    {
                        "id": 147,
                        "unitcode": "WAIT147S",
                        "Status": "Waitlist",
                        "DisplayMinRent": 1600,
                    }
                ],
            },
            {
                "floorPlan": {
                    "PropertyName": "Zander Place",
                    "PropertyID": "450410",
                    "FloorPlanID": "B",
                    "FloorPlanName": "B",
                    "Beds": 2,
                    "Baths": 1,
                    "MinimumRent": 1745,
                    "AvailableUnits": 1,
                },
                "UnitAvailability": [
                    {
                        "id": 20202,
                        "unitcode": "202",
                        "Status": "Vacant Unrented Ready",
                        "DisplayMinRent": 1745,
                        "AvailableDate": "04/01/2026",
                        "UnitAvailableStartDate": "2026-08-02T00:00:00",
                    }
                ],
            },
        ],
    }

    rows = parse_securecafe_applicant_floorplans(
        payload,
        "https://zanderplace.securecafeapplicant.com/onlineleasing/api/floorplan/"
        "getfloorplanandavailableunits?propertyId=450410",
    )
    by_plan = {row["floor_plan_name"]: row for row in rows}

    assert len(rows) == 3
    assert by_plan["F-NP (Surface Lot Parking Only)"]["availability_status"] == "UNAVAILABLE"
    assert by_plan["F-NP (Surface Lot Parking Only)"]["available_units"] == "0"
    assert by_plan["C"]["availability_status"] == "WAITLIST"
    assert by_plan["C"]["unit_number"] == ""
    assert by_plan["B"]["availability_date"] == "2026-08-02T00:00:00"

    capture = datetime(2026, 8, 1, 18, tzinfo=UTC)
    inquiry = _format_v2_unit(by_plan["F-NP (Surface Lot Parking Only)"], capture, "239094")
    waitlist = _format_v2_unit(by_plan["C"], capture, "239094")
    physical = _format_v2_unit(by_plan["B"], capture, "239094")

    assert inquiry["availability_status"] == "UNAVAILABLE"
    assert inquiry["available_date"] is None
    assert waitlist["availability_status"] == "WAITLIST"
    assert waitlist["available_date"] is None
    assert physical["available_date"] == "2026-08-02"
    assert physical["availability_date_provenance"] == "explicit_future"


def test_vanity_applicant_shell_stamps_only_exact_subdomain_match() -> None:
    matching = _ctx(
        property_name="Canvas",
        body=_applicant_shell(),
        base_url="https://www.canvas-apts.com/",
    )
    rows = [{"unit_number": "101", "market_rent_low": 1900}]

    assert _stamp_sc_applicant_vanity_shell_provenance(rows, matching, "canvas-apts")
    assert rows[0]["source_property_provenance"] == "vanity_applicant_shell"
    assert rows[0]["source_portal_url"] == "https://www.canvas-apts.com/"

    mismatching_rows = [{"unit_number": "101", "market_rent_low": 1900}]
    assert not _stamp_sc_applicant_vanity_shell_provenance(
        mismatching_rows,
        matching,
        "sibling-apartments",
    )
    assert "source_property_provenance" not in mismatching_rows[0]


def test_vanity_applicant_provenance_rejects_title_only_or_nested_host() -> None:
    title_only = _ctx(
        body="<title>Applicant Portal | RentCafe</title>",
        base_url="https://www.canvas-apts.com/",
    )
    nested_host = _ctx(
        body=_applicant_shell(),
        base_url="https://leasing.canvas-apts.com/",
    )

    assert not _stamp_sc_applicant_vanity_shell_provenance(
        [{"unit_number": "101"}],
        title_only,
        "canvas-apts",
    )
    assert not _stamp_sc_applicant_vanity_shell_provenance(
        [{"unit_number": "101"}],
        nested_host,
        "canvas-apts",
    )


def test_captured_inventory_fails_closed_on_property_mismatch() -> None:
    url = (
        "https://roosevelt.securecafeapplicant.com/onlineleasing/api/floorplan/"
        "getfloorplanandavailableunits?propertyId=477466"
    )
    context = _ctx()
    result = AdapterResult()

    recovered = _try_captured_securecafe_applicant(
        [{"url": url, "status": 200, "body": _payload(property_name="Sibling Place")}],
        context,
        result,
    )

    assert recovered is None
    assert result.units == []


@pytest.mark.asyncio
async def test_adapter_promotes_exact_captured_applicant_inventory() -> None:
    url = (
        "https://roosevelt.securecafeapplicant.com/onlineleasing/api/floorplan/"
        "getfloorplanandavailableunits?propertyId=477466&RequestBeforeLogin=true"
    )
    context = _ctx()
    context._api_responses = [  # type: ignore[attr-defined]
        {"url": url, "status": 200, "body": json.dumps(_payload())}
    ]

    result = await RentCafeAdapter().extract(_Page(), context)  # type: ignore[arg-type]

    assert result.tier_used.endswith("APPLICANT_FLOORPLANS_V2_CAPTURED")
    assert [row["unit_number"] for row in result.units] == ["B004"]


@pytest.mark.asyncio
async def test_securecafe_probe_uses_applicant_v2_after_legacy_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = "https://roosevelt.securecafe.com/onlineleasing/7400-roosevelt"
    context = _ctx(body=f'<a href="{base}">Apply</a>')
    calls: list[str] = []

    def fake_probe_get(url: str, **_kwargs: Any) -> _Response:
        calls.append(url)
        if url.endswith("/availableunits.aspx"):
            return _Response(403, "blocked", url)
        if "getcustomcolorsfilename" in url:
            return _Response(
                200,
                {"propertyId": 477466, "propertyname": "7400 Roosevelt Apartments"},
                url,
            )
        if "getfloorplanandavailableunits" in url:
            return _Response(200, _payload(), url)
        raise AssertionError(f"unexpected route: {url}")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)
    result = AdapterResult()

    rows = await _try_rentcafe_securecafe_probe(
        context,
        result,
        fast_direct_only=True,
    )

    assert [row["unit_number"] for row in rows] == ["B004"]
    assert result.tier_used.endswith("APPLICANT_FLOORPLANS_V2_DIRECT")
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_published_theme_scopes_blank_canonical_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = "https://cooperslandingapts.securecafe.com/onlineleasing/coopers-landing-apartments"
    body = f"""
    <title>Coopers Landing Apartments | Apartments in Kalamazoo, MI</title>
    <a href="{base}/floorplans.aspx">Floor Plans</a>
    <a href="https://www.CoopersLandingApts.com">Property website</a>
    <address>5001 Coopers Landing Dr., Kalamazoo, MI 49004</address>
    """
    context = _ctx(
        property_name="",
        property_id="218786",
        body=body,
        base_url="https://www.landcoapartments.com/coopers-landing-apartments/",
    )

    def fake_probe_get(url: str, **_kwargs: Any) -> _Response:
        if url.endswith("/availableunits.aspx"):
            return _Response(403, "", url)
        if "getcustomcolorsfilename" in url:
            return _Response(
                200,
                _theme_payload(
                    property_name="Coopers Landing Apartments",
                    property_id="480033",
                    section_name="Welcome to Cooper's Landing Apartments",
                    website="www.cooperslandingapts.com",
                    address="5001 Coopers Landing Dr.",
                    city="Kalamazoo",
                    state="MI",
                    zip_code="49004",
                ),
                url,
            )
        if "getfloorplanandavailableunits" in url:
            return _Response(
                200,
                _payload(
                    property_name="Coopers Landing Apartments",
                    property_id="480033",
                ),
                url,
            )
        raise AssertionError(url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)
    result = AdapterResult()

    rows = await _try_rentcafe_securecafe_probe(context, result)

    assert [row["unit_number"] for row in rows] == ["B004"]
    assert {row["source_property_provenance"] for row in rows} == {"published_applicant_theme"}
    assert rows[0]["source_portal_url"] == base
    assert rows[0]["property_address"].startswith("5001 Coopers Landing Dr.")


@pytest.mark.asyncio
async def test_theme_section_name_can_exactly_scope_canonical_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = "https://villamilano.securecafe.com/onlineleasing/villa-milano0"
    context = _ctx(
        property_name="Villa Milano",
        property_id="61979",
        body=f'<a href="{base}/guestlogin.aspx">Apply</a>',
        base_url="https://www.villamilano.us/",
    )

    def fake_probe_get(url: str, **_kwargs: Any) -> _Response:
        if url.endswith("/availableunits.aspx"):
            return _Response(403, "", url)
        if "getcustomcolorsfilename" in url:
            return _Response(
                200,
                _theme_payload(
                    property_name="Villa Milano Apartments and Villas",
                    property_id="1161613",
                    section_name="Villa Milano",
                    website="www.villamilano.us",
                    address="13740 Howe Lane",
                    city="Leawood",
                    state="KS",
                    zip_code="66224",
                ),
                url,
            )
        if "getfloorplanandavailableunits" in url:
            return _Response(
                200,
                _payload(
                    property_name="Villa Milano Apartments and Villas",
                    property_id="1161613",
                ),
                url,
            )
        raise AssertionError(url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)
    result = AdapterResult()

    rows = await _try_rentcafe_securecafe_probe(context, result)

    assert [row["unit_number"] for row in rows] == ["B004"]
    assert rows[0]["source_property_section_name"] == "Villa Milano"


@pytest.mark.asyncio
async def test_theme_housing_type_suffix_does_not_hide_exact_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Apartments & Townhouses`` is a housing type, not a name conflict."""
    base = "https://meetinghouseapartments.securecafe.com/onlineleasing/meetinghouse-park-apartments"
    context = _ctx(
        property_name="Meetinghouse",
        property_id="42371",
        body=f'<a href="{base}/guestlogin.aspx">Apply</a>',
        base_url="https://meetinghouseapartments.com/",
    )

    def fake_probe_get(url: str, **_kwargs: Any) -> _Response:
        if url.endswith("/availableunits.aspx"):
            return _Response(403, "", url)
        if "getcustomcolorsfilename" in url:
            return _Response(
                200,
                _theme_payload(
                    property_name="Meetinghouse Apartments & Townhouses",
                    property_id="185073",
                    section_name="Welcome to Meetinghouse Apartments & Townhouses",
                    website="www.meetinghouseapartments.com",
                    address="3131 Meetinghouse Rd.",
                    city="Boothwyn",
                    state="PA",
                    zip_code="19061",
                ),
                url,
            )
        if "getfloorplanandavailableunits" in url:
            return _Response(
                200,
                _payload(
                    property_name="Meetinghouse Apartments & Townhouses",
                    property_id="185073",
                ),
                url,
            )
        raise AssertionError(url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)

    rows = await _try_rentcafe_securecafe_probe(context, AdapterResult())

    assert [row["unit_number"] for row in rows] == ["B004"]
    assert rows[0]["source_property_provenance"] == "published_applicant_theme"
    assert rows[0]["property_address"].startswith("3131 Meetinghouse Rd.")


@pytest.mark.asyncio
async def test_refetched_same_name_sibling_with_conflicting_theme_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Name equality alone cannot cross a same-name portfolio boundary."""
    base = "https://liveatcentennialapartments.securecafe.com/onlineleasing/centennial-apartments"
    context = _ctx(
        property_name="Centennial",
        property_id="238508",
        body="<html><title>Centennial Apartments in Springfield</title></html>",
        base_url="https://www.liveatcentennialapartments.com/",
        address="506 W Centennial Blvd",
        city="Springfield",
        state="OR",
        zip_code="97477",
    )
    api_called = False

    def fake_probe_get(url: str, **_kwargs: Any) -> _Response:
        nonlocal api_called
        if url == "https://www.liveatcentennialapartments.com":
            return _Response(200, f'<a href="{base}/guestlogin.aspx">Apply</a>', url)
        if url.endswith("/availableunits.aspx"):
            return _Response(403, "", url)
        if "getcustomcolorsfilename" in url:
            return _Response(
                200,
                _theme_payload(
                    property_name="Centennial Apartments",
                    property_id="490638",
                    section_name="Welcome to Centennial Apartments",
                    website="www.lloydmanagement.com/centennial-apartments/",
                    address="120 N Spring Street",
                    city="Luverne",
                    state="MN",
                    zip_code="56156",
                ),
                url,
            )
        if "getfloorplanandavailableunits" in url:
            api_called = True
            return _Response(
                200,
                _payload(
                    property_name="Centennial Apartments",
                    property_id="490638",
                ),
                url,
            )
        raise AssertionError(url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)

    assert await _try_rentcafe_securecafe_probe(context, AdapterResult()) == []
    assert api_called is False


@pytest.mark.asyncio
async def test_refetched_theme_with_exact_context_address_is_provenanced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh same-origin link may win when theme identity independently agrees."""
    base = "https://liveatcentennialapartments.securecafe.com/onlineleasing/centennial-apartments0"
    context = _ctx(
        property_name="Centennial",
        property_id="238508",
        body="<html><title>Centennial Apartments in Springfield</title></html>",
        base_url="https://www.liveatcentennialapartments.com/",
        address="506 W Centennial Blvd",
        city="Springfield",
        state="OR",
        zip_code="97477",
    )

    def fake_probe_get(url: str, **_kwargs: Any) -> _Response:
        if url == "https://www.liveatcentennialapartments.com":
            return _Response(200, f'<a href="{base}/guestlogin.aspx">Apply</a>', url)
        if url.endswith("/availableunits.aspx"):
            return _Response(403, "", url)
        if "getcustomcolorsfilename" in url:
            return _Response(
                200,
                _theme_payload(
                    property_name="Centennial Apartments",
                    property_id="1235407",
                    section_name="Centennial Apartments",
                    website="www.liveatcentennialapartments.com",
                    address="506 West Centennial Blvd",
                    city="Springfield",
                    state="OR",
                    zip_code="97477",
                ),
                url,
            )
        if "getfloorplanandavailableunits" in url:
            return _Response(
                200,
                _payload(
                    property_name="Centennial Apartments",
                    property_id="1235407",
                ),
                url,
            )
        raise AssertionError(url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)

    rows = await _try_rentcafe_securecafe_probe(context, AdapterResult())

    assert [row["unit_number"] for row in rows] == ["B004"]
    assert rows[0]["source_property_provenance"] == ("property_matched_applicant_theme")
    assert rows[0]["property_address"].startswith("506 West Centennial Blvd")


@pytest.mark.asyncio
async def test_blank_name_theme_scope_rejects_unpublished_sibling_website(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = "https://target.securecafe.com/onlineleasing/target-property"
    context = _ctx(
        property_name="",
        body=f'<a href="{base}/guestlogin.aspx">Apply</a><p>Target, IL 60000</p>',
        base_url="https://portfolio.example/target/",
    )

    def fake_probe_get(url: str, **_kwargs: Any) -> _Response:
        if url.endswith("/availableunits.aspx"):
            return _Response(403, "", url)
        if "getcustomcolorsfilename" in url:
            return _Response(
                200,
                _theme_payload(
                    property_name="Sibling Apartments",
                    property_id="987654",
                    section_name="Sibling Apartments",
                    website="www.sibling.example",
                    address="1 Sibling Way",
                    city="Elsewhere",
                    state="IL",
                    zip_code="69999",
                ),
                url,
            )
        raise AssertionError(url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)

    assert await _try_rentcafe_securecafe_probe(context, AdapterResult()) == []


@pytest.mark.asyncio
async def test_applicant_403_uses_one_capped_hyperbrowser_raw_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = "https://lindyproperty.securecafe.com/onlineleasing/450-green-apartments-0"
    context = _ctx(
        property_name="450 Green Apartments",
        property_id="231934",
        body=(f'<a href="{base}/oleapplication.aspx?stepname=floorplan&myOlePropertyId=477492">Apply</a>'),
    )
    payload = _payload(
        property_name="450 Green Apartments",
        property_id="477492",
        units=[
            {
                "id": 1001,
                "unitcode": "C104",
                "DisplayMinRent": 1540,
                "Area": 1150,
                "Status": "Notice Unrented",
            }
        ],
    )

    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda url, **_kwargs: _Response(403, "", url),
    )
    hb_calls: list[tuple[str, str]] = []

    async def fake_hb_raw_get(url: str, property_id: str) -> tuple[int, str]:
        hb_calls.append((url, property_id))
        return 200, json.dumps(payload)

    monkeypatch.setenv("FETCH_BACKEND", "hyperbrowser")
    monkeypatch.setattr(
        "ma_poc.fetch.hyperbrowser_backend.hb_raw_get",
        fake_hb_raw_get,
    )
    result = AdapterResult()

    rows = await _try_rentcafe_securecafe_probe(context, result)

    assert [row["unit_number"] for row in rows] == ["C104"]
    assert result.tier_used.endswith("APPLICANT_FLOORPLANS_V2_HYPERBROWSER")
    assert hb_calls == [(result.winning_url, "231934")]
    assert result.api_responses[-1]["via"] == ("securecafe_applicant_hyperbrowser")
