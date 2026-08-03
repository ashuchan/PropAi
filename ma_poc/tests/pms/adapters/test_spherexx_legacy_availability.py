from __future__ import annotations

from types import SimpleNamespace

import pytest

from ma_poc.pms.adapters._pms_portal_hop import recover_pms_portal
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.spherexx import parse_spherexx_legacy_availability
from ma_poc.pms.detector import detect_pms

_ADKAST_URL = "https://clients.spherexx.com/adkast_availability_2/availability/mgoelqnd/rose/BE9B77"
_ADKAST_HTML = """
<p class="nTableTitle">One Bedroom</p>
<table class="nTable availability">
  <tr><th>Unit</th><th>Bedroom</th><th>Bathroom</th><th>Price</th></tr>
  <tr>
    <td data-label="Unit"><a href="#unit480051"
      data-tlabel="Irondale at Wharton Apartments  - 305">305</a></td>
    <td data-label="Bedroom">1 BR</td>
    <td data-label="Bathroom">1 BA</td>
    <td data-label="Rent">$2,325</td>
    <td data-label="Availability">11/8/2026</td>
  </tr>
  <tr>
    <td data-label="Unit"><a href="#unit480062"
      data-tlabel="Irondale at Wharton Apartments  - 401">401</a></td>
    <td data-label="Bedroom">1 BR</td>
    <td data-label="Bathroom">1 BA</td>
    <td data-label="Rent">$2,375</td>
    <td data-label="Availability">Immediate</td>
  </tr>
</table>
"""

_KAMSON_URL = "https://clients.spherexx.com/kamson_availability/availability.asp?id=noegpbnd"
_KAMSON_HTML = """
<h2>Two Bedroom</h2>
<table class="treatedTable sort">
  <tr data-unitid="529700" data-floorplanid="64619">
    <td data-label="Unit">1A</td>
    <td data-label="Building">030</td>
    <td data-label="Bedroom">2 BR</td>
    <td data-label="Bathroom">1.5 BA</td>
    <td data-label="Rent">$2,950</td>
    <td data-label="LeaseTerm">8 Months</td>
    <td data-label="Availability">9/7/2026</td>
  </tr>
</table>
"""


def _ctx(body: str) -> AdapterContext:
    base_url = "http://irondaleatwharton.com/"
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url, page_html=body),
        profile=None,
        expected_total_units=None,
        property_id="281149",
        fetch_result=SimpleNamespace(body=body, final_url=base_url),
        property_name="Irondale at Wharton",
        address="2 North Main Street",
        city="Wharton",
        state="NJ",
        zip_code="07885",
    )


def test_parse_adkast_href_native_ids_and_property_label() -> None:
    rows = parse_spherexx_legacy_availability(_ADKAST_HTML, _ADKAST_URL)

    assert [row["unit_number"] for row in rows] == ["305", "401"]
    assert [row["market_rent_low"] for row in rows] == [2325, 2375]
    assert rows[0]["floor_plan_name"] == "One Bedroom"
    assert rows[0]["source_ids"] == {"spherexx_unit_id": "480051"}
    assert rows[0]["source_property_name"] == ("Irondale at Wharton Apartments")
    assert rows[1]["availability_date"] == "Immediate"
    assert rows[0]["extraction_tier"] == ("TIER_1_DOM_SPHEREXX_LEGACY_AVAILABILITY")


def test_parse_kamson_row_and_floorplan_native_ids() -> None:
    rows = parse_spherexx_legacy_availability(_KAMSON_HTML, _KAMSON_URL)

    assert len(rows) == 1
    assert rows[0]["unit_number"] == "1A"
    assert rows[0]["building"] == "030"
    assert rows[0]["bedrooms"] == "2"
    assert rows[0]["bathrooms"] == "1.5"
    assert rows[0]["lease_term"] == "8 Months"
    assert rows[0]["source_ids"] == {
        "spherexx_unit_id": "529700",
        "spherexx_floorplan_id": "64619",
    }


def test_legacy_parser_rejects_plan_or_unpriced_rows() -> None:
    html = """
    <div>Availability</div>
    <table class="availability">
      <tr data-floorplanid="PLAN-1">
        <td data-label="Unit">A1</td><td data-label="Rent">$2,000</td>
      </tr>
      <tr data-unitid="UNIT-2">
        <td data-label="Unit">202</td><td data-label="Rent">Call for rent</td>
      </tr>
    </table>
    """
    assert parse_spherexx_legacy_availability(html, _ADKAST_URL) == []


@pytest.mark.asyncio
async def test_page_none_follows_only_published_inventory_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = '<html><a href="/availability/">Availability</a></html>'
    inventory_url = "http://irondaleatwharton.com/availability/"
    inventory_html = f'<iframe src="{_ADKAST_URL}"></iframe>'
    calls: list[str] = []

    async def _probe(url: str) -> tuple[int, str]:
        calls.append(url)
        bodies = {
            inventory_url: inventory_html,
            _ADKAST_URL: _ADKAST_HTML,
        }
        return (200 if url in bodies else 404, bodies.get(url, ""))

    monkeypatch.setattr(
        "ma_poc.pms.adapters._pms_portal_hop._direct_public_html",
        _probe,
    )
    rows = await recover_pms_portal(None, _ctx(root))  # type: ignore[arg-type]

    assert [row["unit_number"] for row in rows] == ["305", "401"]
    assert calls == [inventory_url, _ADKAST_URL]
    assert rows[0]["source_portal_url"] == inventory_url
    assert rows[0]["source_property_provenance"] == ("published_spherexx_availability_iframe")


@pytest.mark.asyncio
async def test_page_none_rejects_cross_host_inventory_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _unexpected(url: str) -> tuple[int, str]:
        calls.append(url)
        return 200, _ADKAST_HTML

    monkeypatch.setattr(
        "ma_poc.pms.adapters._pms_portal_hop._direct_public_html",
        _unexpected,
    )
    rows = await recover_pms_portal(
        None,
        _ctx('<a href="https://sibling.example/availability/">Units</a>'),
    )  # type: ignore[arg-type]

    assert rows == []
    assert calls == []
