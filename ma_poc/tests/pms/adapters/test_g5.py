"""G5 Marketing Cloud adapter (GraphQL).

Coverage:
* URN extraction from rendered HTML (image CDN paths carry the slug)
* Apartment parser handles the multi-price G5 response shape
* End-to-end mocked POST → 21-unit response

Source: 2026-05-13 live probe of kings-manor-apartments.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.g5 import (
    G5Adapter,
    find_g5_urn,
    parse_g5_apartments,
)
from ma_poc.pms.detector import detect_pms


@dataclass
class _StubFetchResult:
    body: bytes | str | None


class _DummyPage:
    pass


_KINGS_MANOR_HTML = """
<html><body>
<img src="https://g5-assets-cld-res.cloudinary.com/image/upload/x/v1/g5/g5-c-5befy22d4-morgan-properties/g5-cl-1jsdmzcxpf-king-s-manor-apartments/uploads/foo.png">
</body></html>
"""

_NO_G5_HTML = """
<html><body><p>nothing g5 here</p></body></html>
"""

# Real-shape excerpt from the 2026-05-13 King's Manor GraphQL response.
_REAL_G5_PAYLOAD = {
    "data": {
        "apartmentComplex": {
            "id": 257651016,
            "name": "King's Manor",
            "apartments": [
                {
                    "id": 1311734088,
                    "name": "KA2140A4",
                    "displayName": "KA2140A4",
                    "building": None,
                    "availabilityDate": "2026-01-26",
                    "sqftDisplay": "950",
                    "prices": [
                        {"value": "1365.0", "formattedPrice": "$1,365", "priceType": "min_rent"},
                        {"value": "1365.0", "formattedPrice": "$1,365", "priceType": "rate"},
                    ],
                    "floorplan": {
                        "id": 2078036642,
                        "name": "Two Bedroom 2 Bath- 950 sqft",
                        "beds": 2,
                        "baths": "2.0",
                        "sqft": 950,
                    },
                },
                {
                    "id": 1311734098,
                    "name": "QU2150A2",
                    "displayName": "QU2150A2",
                    "building": "Building B",
                    "availabilityDate": "2026-02-09",
                    "sqftDisplay": "950",
                    "prices": [
                        {"value": "1365.0", "formattedPrice": "$1,365", "priceType": "min_rent"}
                    ],
                    "floorplan": {
                        "id": 2078036642,
                        "name": "Two Bedroom 2 Bath- 950 sqft",
                        "beds": 2,
                        "baths": "2.0",
                        "sqft": 950,
                    },
                },
            ],
        }
    }
}


def test_find_g5_urn_extracts_full_slug() -> None:
    """The full ``g5-cl-<id>-<name>`` slug is preferred over the bare id."""
    urn = find_g5_urn(_KINGS_MANOR_HTML)
    assert urn == "g5-cl-1jsdmzcxpf-king-s-manor-apartments"


def test_find_g5_urn_returns_none_when_absent() -> None:
    assert find_g5_urn(_NO_G5_HTML) is None
    assert find_g5_urn("") is None


def test_find_g5_urn_picks_longest_when_both_forms_present() -> None:
    html = (
        '<a href="https://x/g5-cl-abc123">x</a>'
        '<a href="https://x/g5-cl-abc123-my-property-slug">y</a>'
    )
    assert find_g5_urn(html) == "g5-cl-abc123-my-property-slug"


def test_find_g5_urn_extracts_g5_clw_property_level_urn() -> None:
    """2026-05-20 fix: the COMPLEX-scoped URN form ``g5-clw-<id>-<slug>``
    is used by single-property sites like Villas Willow Glen / Fairways V.
    The old regex required literal ``g5-cl-`` and missed these. Verified
    live against ``villaswillowglen.com`` (g5-clw-guhjplm75w-villas-
    willow-glen-...) — the URN is accepted by the GraphQL
    ``apartmentComplex(locationUrn:$urn)`` API."""
    html = (
        '<script>var theme = "g5-clw-guhjplm75w-villas-willow-glen-'
        'ccd198fe3a9da82ff16f099a096c1d91";</script>'
    )
    urn = find_g5_urn(html)
    assert urn == (
        "g5-clw-guhjplm75w-villas-willow-glen-"
        "ccd198fe3a9da82ff16f099a096c1d91"
    )


def test_find_g5_urn_g5_clw_substring_gate() -> None:
    """The early-out check accepts ``g5-cl`` (no trailing dash) so the
    ``g5-clw-`` form passes the substring gate. Without that broadening,
    Villas Willow Glen returns None even though the URN is present."""
    html = '<img src="//cdn/g5-clw-guvkvtfn23-fairways-v-1217fab7c6b6151ad98d9565d8584f39/x.png">'
    urn = find_g5_urn(html)
    assert urn is not None
    assert urn.startswith("g5-clw-")
    assert "fairways-v" in urn


def test_find_g5_urn_picks_longest_clw_form() -> None:
    """When both bare ``g5-clw-<id>`` and full slug forms appear, prefer
    the longer (fully-qualified) form just like the ``g5-cl-`` case."""
    html = (
        '<a href="https://x/g5-clw-guhjplm75w">x</a>'
        '<a href="https://x/g5-clw-guhjplm75w-villas-willow-glen-ccd198fe3a9da82ff16f099a096c1d91">y</a>'
    )
    urn = find_g5_urn(html)
    assert urn == (
        "g5-clw-guhjplm75w-villas-willow-glen-"
        "ccd198fe3a9da82ff16f099a096c1d91"
    )


def test_g5_units_query_includes_floorplan_specials_subfields() -> None:
    """2026-05-20 schema-drift guard: ``floorplanSpecials`` is now an
    object (``FloorplanSpecials``) with ``id``/``name`` fields, NOT a
    scalar. Requesting it without subfields returns
    ``Field must have selections (field 'floorplanSpecials' returns
    FloorplanSpecials but has no selections)`` from the GraphQL server.
    This pin catches accidental schema-regression reverts."""
    from ma_poc.pms.adapters.g5 import _G5_UNITS_QUERY
    # Must specify subfields on floorplanSpecials.
    assert "floorplanSpecials{id name}" in _G5_UNITS_QUERY or (
        "floorplanSpecials { id name }" in _G5_UNITS_QUERY
    ), "floorplanSpecials must specify subfields"
    # Bare ``floorplanSpecials}`` (scalar form) must NOT appear.
    assert "floorplanSpecials}" not in _G5_UNITS_QUERY, (
        "floorplanSpecials must not be requested as scalar"
    )


def test_parse_g5_apartments_basic() -> None:
    units = parse_g5_apartments(_REAL_G5_PAYLOAD)
    assert len(units) == 2
    u0 = units[0]
    assert u0["unit_number"] == "KA2140A4"
    assert u0["market_rent_low"] == 1365
    assert u0["sqft"] == "950"
    assert u0["bedrooms"] == "2"
    assert u0["bathrooms"] == "2.0"
    assert u0["availability_date"] == "2026-01-26"
    assert u0["floor_plan_name"] == "Two Bedroom 2 Bath- 950 sqft"


def test_parse_g5_apartments_drops_implausible_rents() -> None:
    payload = {
        "data": {
            "apartmentComplex": {
                "apartments": [
                    {
                        "name": "X",
                        "prices": [{"value": "5.0", "priceType": "min_rent"}],
                        "floorplan": {"name": "F", "beds": 1, "baths": "1", "sqft": 500},
                    },
                    {
                        "name": "Y",
                        "prices": [{"value": "120000.0", "priceType": "min_rent"}],
                        "floorplan": {"name": "F", "beds": 1, "baths": "1", "sqft": 500},
                    },
                ]
            }
        }
    }
    assert parse_g5_apartments(payload) == []


def test_parse_g5_apartments_empty_payload_returns_empty() -> None:
    assert parse_g5_apartments({}) == []
    assert parse_g5_apartments({"data": None}) == []
    assert parse_g5_apartments({"data": {"apartmentComplex": None}}) == []


@pytest.mark.asyncio
async def test_extract_happy_path(mocker) -> None:
    mock_response = mocker.Mock()
    mock_response.status_code = 200
    mock_response.json = mocker.Mock(return_value=_REAL_G5_PAYLOAD)

    async def _mock_post(self, url, json=None, headers=None):  # noqa: ANN001
        assert url == "https://inventory.g5marketingcloud.com/graphql"
        assert json["variables"]["urn"] == "g5-cl-1jsdmzcxpf-king-s-manor-apartments"
        return mock_response

    mocker.patch("httpx.AsyncClient.post", _mock_post)

    detected = detect_pms(
        "https://www.morgan-properties.com/apartments/pa/harrisburg/kings-manor-apartments/",
        page_html=_KINGS_MANOR_HTML,
    )
    assert detected.pms == "g5"

    ctx = AdapterContext(
        base_url="https://www.morgan-properties.com/apartments/pa/harrisburg/kings-manor-apartments/",
        detected=detected,
        profile=None,
        expected_total_units=None,
        property_id="227350",
        fetch_result=_StubFetchResult(body=_KINGS_MANOR_HTML.encode("utf-8")),
    )
    result = await G5Adapter().extract(_DummyPage(), ctx)

    assert result.tier_used == "TIER_1_API_G5"
    assert len(result.units) == 2
    assert result.units[0]["availability_date"] == "2026-01-26"
    assert result.confidence >= 0.7


@pytest.mark.asyncio
async def test_extract_no_urn_returns_clean_failure(mocker) -> None:
    spy = mocker.patch("httpx.AsyncClient.post")

    ctx = AdapterContext(
        base_url="https://example.com/",
        detected=detect_pms("https://example.com/", page_html=_NO_G5_HTML),
        profile=None,
        expected_total_units=None,
        property_id="9999",
        fetch_result=_StubFetchResult(body=_NO_G5_HTML.encode("utf-8")),
    )
    result = await G5Adapter().extract(_DummyPage(), ctx)
    assert spy.call_count == 0
    assert result.tier_used == "TIER_1_API_G5_NO_URN"
    assert len(result.units) == 0


@pytest.mark.asyncio
async def test_extract_api_error_recorded(mocker) -> None:
    mock_response = mocker.Mock()
    mock_response.status_code = 500
    mock_response.json = mocker.Mock(side_effect=ValueError)

    async def _mock_post(self, url, json=None, headers=None):  # noqa: ANN001
        return mock_response

    mocker.patch("httpx.AsyncClient.post", _mock_post)

    ctx = AdapterContext(
        base_url="https://www.morgan-properties.com/apartments/pa/harrisburg/kings-manor-apartments/",
        detected=detect_pms(
            "https://www.morgan-properties.com/apartments/pa/harrisburg/kings-manor-apartments/",
            page_html=_KINGS_MANOR_HTML,
        ),
        profile=None,
        expected_total_units=None,
        property_id="227350",
        fetch_result=_StubFetchResult(body=_KINGS_MANOR_HTML.encode("utf-8")),
    )
    result = await G5Adapter().extract(_DummyPage(), ctx)
    assert result.tier_used.startswith("TIER_1_API_G5_")
    assert len(result.units) == 0


def test_detect_g5_from_inventory_host() -> None:
    """Direct script reference to the G5 GraphQL host routes to ``g5``."""
    html = (
        '<html><body><script src="https://inventory.g5marketingcloud.com/x.js"></script>'
        '</body></html>'
    )
    result = detect_pms("https://www.morgan-properties.com/", page_html=html)
    assert result.pms == "g5"
    assert result.recommended_strategy == "api_first"
