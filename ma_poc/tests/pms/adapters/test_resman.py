"""ResMan Implicity adapter (2026-05-19, greenfield).

Row data captured live from regaliabellaterra.com's ResMan Implicity iframe
(implicity.myresman.com/Portal/Applicants/Availability?a=1450&p=...&MoveInDate=
05/30/2026) — a real mislabelled-"Knock" zero-unit failure that the
&MoveInDate= query un-gates into 15 unit-level rows.
"""

from __future__ import annotations

import datetime

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.resman import (
    ResManAdapter,
    _move_in_date,
    parse_resman_units,
)
from ma_poc.pms.detector import detect_pms

_RESMAN_ROWS = [
    {
        # Real shape: rentText is the *Other Charges* table (fees < 100);
        # the actual rent + Total Rent live in the full row text.
        "unit": "21008",
        "sqft": "728",
        "term": "12 Months",
        "rentText": "Other Charges Description Amount Valet Trash 25.00 "
        "Pest Control Fees 10.00 Renter's Insurance - Liability Only 15.00",
        "availText": "Apply Move in before 8/5/2026 Select new Move-in date "
        "for pricing Available on 7/15/2026 Select new Move-in date for pricing",
        "text": "21008 Summary Sq Ft 728 Bedrooms 1 Bathrooms 1.00 Building 2 "
        "Floor 1 Pets permitted Yes Deposit 0.00 View Pet Policy Other Charges "
        "Description Amount Valet Trash 25.00 Pest Control Fees 10.00 Renter's "
        "Insurance - Liability Only 15.00 Lease Terms Lease term Rent 12 Months "
        "1,299.00 Total Rent Rent + Other Charges 1,349.00 Apply Move in before "
        "8/5/2026 Available on 7/15/2026",
    },
    {
        "unit": "31004",
        "sqft": "820",
        "term": "12 Months",
        "rentText": "Other Charges Valet Trash 25.00",
        "availText": "Available on 5/28/2026 Select new Move-in date for pricing",
        "text": "31004 Summary Sq Ft 820 Bedrooms 2 Bathrooms 2.00 Building 3 "
        "Floor 2 Pets permitted Yes Deposit 0.00 Lease Terms Lease term Rent "
        "14 Months 1,410.00 12 Months 1,455.00 Total Rent 1,460.00",
    },
]


class _FakePage:
    def __init__(self, rows: object, url: str = "https://www.regaliabellaterra.com/floorplans/") -> None:
        self._rows = rows
        self.url = url

    async def evaluate(self, _js: str, *_a: object) -> object:
        return self._rows


def _ctx(base_url: str = "https://www.regaliabellaterra.com/") -> AdapterContext:
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )


def test_parse_resman_units_fields() -> None:
    units = parse_resman_units(_RESMAN_ROWS, "https://implicity.myresman.com/x")
    assert len(units) == 2

    u1 = units[0]
    assert u1["unit_number"] == "21008"
    assert u1["bedrooms"] == "1"
    assert u1["bathrooms"] == "1.00"
    assert u1["sqft"] == "728"
    assert u1["building"] == "2"
    assert u1["floor"] == "1"
    assert u1["market_rent_low"] == 1299
    assert u1["availability_date"] == "7/15/2026"
    assert u1["lease_term"] == "12 Months"
    assert u1["availability_status"] == "AVAILABLE"
    assert u1["extraction_tier"] == "TIER_1_DOM_RESMAN_IMPLICITY"

    # Multiple lease-term prices → take the lowest plausible.
    u2 = units[1]
    assert u2["unit_number"] == "31004"
    assert u2["bedrooms"] == "2"
    assert u2["market_rent_low"] == 1410
    assert u2["availability_date"] == "5/28/2026"


def test_parse_resman_skips_empty_rows() -> None:
    assert parse_resman_units([{}, {"unit": "", "sqft": "", "text": ""}], "u") == []


def test_move_in_date_format_and_offset() -> None:
    today = datetime.date(2026, 5, 19)
    out = _move_in_date(today)
    assert out == "6/18/2026"  # +30 days, M/D/YYYY (no zero-pad)


@pytest.mark.asyncio
async def test_resman_adapter_extract() -> None:
    result = await ResManAdapter().extract(_FakePage(_RESMAN_ROWS), _ctx())  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_RESMAN_IMPLICITY"
    assert len(result.units) == 2
    assert result.confidence > 0.7
    assert result.units[0]["unit_number"] == "21008"


@pytest.mark.asyncio
async def test_resman_adapter_no_rows() -> None:
    result = await ResManAdapter().extract(_FakePage([]), _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0
    assert result.errors


@pytest.mark.asyncio
async def test_resman_adapter_pageless_stub() -> None:
    class _Bare:
        url = "https://x.com/"

    result = await ResManAdapter().extract(_Bare(), _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


def test_detector_routes_resman_iframe_marker() -> None:
    html = (
        '<html><body><iframe src="https://implicity.myresman.com/Portal/'
        'Applicants/Availability?a=1450&p=57495da9-baae-4ba3-98c0-e62612db16c3">'
        "</iframe></body></html>"
    )
    det = detect_pms("https://www.regaliabellaterra.com/floorplans/", page_html=html)
    assert det.pms == "resman"
    assert det.recommended_strategy == "dom_first"


def test_resman_adapter_registered() -> None:
    adapter = get_adapter("resman")
    assert isinstance(adapter, ResManAdapter)
    assert adapter.pms_name == "resman"
