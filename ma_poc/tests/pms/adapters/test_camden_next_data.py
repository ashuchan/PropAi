"""Regression guards for the retired Camden representative cross-product."""

from __future__ import annotations

import json

import pytest

from ma_poc.pms.adapters._camden import (
    detect_camden_next_data,
    is_camden_host,
    parse_camden_next_data,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.camdenliving.com/apartments/houston-tx/camden-vanderbilt", True),
        ("https://camdenliving.com/", True),
        ("https://camdenliving.com.evil.com/", False),
        ("https://www.gables.com/aster", False),
        ("", False),
        (None, False),
    ],
)
def test_is_camden_host(url: str | None, expected: bool) -> None:
    assert is_camden_host(url) is expected


def _preview_html() -> str:
    payload = {
        "props": {
            "pageProps": {
                "suggestedFloorPlans": [
                    {
                        "name": "B.2",
                        "monthlyRent": 1469,
                        "totalMonthlyRent": 1644,
                        "realPageUnitId": 25,
                        "availableUnitIds": ["4611", "4613", "4632"],
                        "unitNumber": "4611",
                    }
                ]
            }
        }
    }
    return (
        '<html><script id="__NEXT_DATA__">'
        + json.dumps(payload)
        + "</script>"
        + "x" * 1200
        + "</html>"
    )


def test_detector_still_identifies_historical_preview_shape() -> None:
    assert detect_camden_next_data(_preview_html()) is True


def test_preview_never_cross_products_representative_values() -> None:
    # The preview names three public labels but gives exact rent/date/native id
    # for only the representative.  Emitting three rows would fabricate two.
    assert parse_camden_next_data(_preview_html(), "https://camdenliving.com/x") == []


def test_dict_preview_is_also_disabled() -> None:
    payload = {"props": {"pageProps": {"suggestedFloorPlans": [{"name": "A1"}]}}}
    assert detect_camden_next_data(payload) is True
    assert parse_camden_next_data(payload) == []
