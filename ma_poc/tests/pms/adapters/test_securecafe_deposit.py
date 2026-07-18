"""SecureCafe deposit extraction (2026-07-16).

Many securecafe ``availableunits.aspx`` AvailUnitRow layouts carry a
``<td data-label='Deposit'>$200</td>`` cell (verified live on
parksrichardson.securecafe.com). The parser dropped it despite ``deposit``
being plumbed through make_unit_dict + the v2 output — a RealPage priority
field. Other layouts (rosenyc) have no Deposit column; those stay empty.
"""

from __future__ import annotations

from ma_poc.pms.adapters.rentcafe import parse_securecafe_availableunits

_URL = "https://x.securecafe.com/onlineleasing/x/availableunits.aspx"


def test_securecafe_extracts_deposit():
    # Real cell markup captured live: <td class="text-center"
    # data-selenium-id="Deposit1" data-label="Deposit">$200</td>
    html = """
    <h1>… Floor Plan: 1 BED 1 BATH - 1 Bedroom, 1 Bathroom</h1>
    <tr class='AvailUnitRow' id='unitrow_1'>
      <th data-label='Apartment'>#2115</th>
      <td data-label='Sq.Ft.'>525</td>
      <td data-label='Rent'>$1,500</td>
      <td class='text-center' data-selenium-id='Deposit1' data-label='Deposit'>$200</td>
      <td data-label='Action'></td>
    </tr>
    """
    units = parse_securecafe_availableunits(html, _URL)
    assert len(units) == 1
    assert units[0]["unit_number"] == "2115"
    assert units[0]["market_rent_low"] == 1500
    assert units[0]["deposit"] == "$200"


def test_securecafe_deposit_with_comma():
    html = """
    <h1>… Floor Plan: 2 BED 2 BATH - 2 Bedrooms, 2 Bathrooms</h1>
    <tr class='AvailUnitRow' id='unitrow_1'>
      <th data-label='Apartment'>#8CS</th>
      <td data-label='Rent'>$8,400</td>
      <td data-label='Deposit'>$1,000</td>
    </tr>
    """
    units = parse_securecafe_availableunits(html, _URL)
    assert units[0]["deposit"] == "$1,000"


def test_securecafe_no_deposit_column_is_empty():
    # rosenyc-style layout: Apartment | Sq.Ft. | Rent | Date Available | Action
    # (no Deposit column) → deposit stays empty, not an error.
    html = """
    <h1>… Floor Plan: 1 BED 1 BATH - 1 Bedroom, 1 Bathroom</h1>
    <tr class='AvailUnitRow' id='unitrow_1'>
      <th data-label='Apartment'>#6ES</th>
      <td data-label='Sq.Ft.'>629</td>
      <td data-label='Rent'>$6,100</td>
      <td data-label='Date Available'>7/30/2026</td>
      <td data-label='Action'></td>
    </tr>
    """
    units = parse_securecafe_availableunits(html, _URL)
    assert len(units) == 1
    assert units[0]["deposit"] == ""
