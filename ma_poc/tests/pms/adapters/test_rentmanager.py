"""Rent Manager adapter tests.

Covers DOM extraction (post-render and pre-render template-literal variants),
detector integration, and the empty/no-rows failure path. Sample HTML mirrors
the structure observed on KRC Apartments properties (norfolk-place /
virginia-flats) on 2026-05-06.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.rentmanager import (
    RentManagerAdapter,
    parse_rentmanager_html,
)
from ma_poc.pms.detector import detect_pms

_SAMPLE_HTML = """
<html>
<body>
<table id="availabilitytable">
  <thead><tr><th>Unit</th><th>Beds</th><th>Rent</th><th>Sq.Ft.</th><th>Available</th></tr></thead>
  <tbody>
    <tr class='unit_avail_container' data-date='3/1/2026'>
      <td class='unit-number'>12</td>
      <td class='unit-beds'>2</td>
      <td class='unit-rent'>$1,275.00</td>
      <td class='unit-sqft'>725</td>
      <td class='unit-date'>3/1/2026</td>
      <td class='unit-info'><a href='#'>View Details</a></td>
      <td class='unit-tour'><a href='#'>Tour</a></td>
      <td class='unit-action'><a href='/apply'>Apply</a></td>
    </tr>
    <tr class='unit_avail_container' data-date='now'>
      <td class='unit-number'>20</td>
      <td class='unit-beds'>1</td>
      <td class='unit-rent'>$1,060.00</td>
      <td class='unit-sqft'>600</td>
      <td class='unit-date'>Now</td>
      <td class='unit-info'><a href='#'>View Details</a></td>
      <td class='unit-tour'></td>
      <td class='unit-action'></td>
    </tr>
  </tbody>
</table>
<div id='3172-detail'>
  <p><strong>Beds:</strong> 2<br /><strong>Baths:</strong> 1.0<br /><strong>Sq.Ft.:</strong> 725<br /><strong>Rent:</strong> $1,275.00<br /></p>
</div>
<a href="https://rentmanager.com/websites">Rent Manager(R)</a>
</body>
</html>
"""


_TEMPLATE_LITERAL_HTML = """
<html>
<body>
<table id="availabilitytable"></table>
<script>
  jQuery('#availabilitytable').append(`
    <tr class='unit_avail_container' data-date='3/1/2026'>
      <td class='unit-number'>12</td>
      <td class='unit-beds'>2</td>
      <td class='unit-rent'>$1,275.00</td>
      <td class='unit-sqft'>725</td>
      <td class='unit-date'>${UnitDate}</td>
    </tr>
  `);
</script>
<a href="https://rentmanager.com/websites">RM</a>
</body>
</html>
"""


def test_parse_rentmanager_html_extracts_two_units() -> None:
    """Standard happy path — both rows yield unit dicts with rent."""
    units = parse_rentmanager_html(_SAMPLE_HTML)
    assert len(units) == 2
    u1, u2 = sorted(units, key=lambda u: u["unit_number"])
    assert u1["unit_number"] == "12"
    assert u1["bedrooms"] == "2"
    assert u1["bathrooms"] == "1.0"
    assert u1["sqft"] == "725"
    assert u1["market_rent_low"] == 1275
    assert u1["market_rent_high"] == 1275
    assert u1["availability_date"] == "3/1/2026"
    assert u1["availability_status"] == "AVAILABLE"
    assert u2["unit_number"] == "20"
    assert u2["market_rent_low"] == 1060
    # "Now" gets normalized to empty availability_date but status stays AVAILABLE.
    assert u2["availability_date"] == ""
    assert u2["availability_status"] == "AVAILABLE"


def test_parse_rentmanager_html_skips_template_literal_rows() -> None:
    """Pre-render template still has `${UnitDate}` — quality gate must skip."""
    units = parse_rentmanager_html(_TEMPLATE_LITERAL_HTML)
    # The unit date contains an unrendered ``${...}`` placeholder. Other cells
    # are clean so the unit_number / rent are real, but the date check is
    # informational; we DO want to keep this unit because rent and unit_number
    # are real.
    # Actually our gate skips rows with ``${`` in unit_number OR rent_text.
    # ``${UnitDate}`` is in date_text only — should NOT cause skip. So this
    # row is kept.
    assert len(units) == 1
    assert units[0]["unit_number"] == "12"
    assert units[0]["market_rent_low"] == 1275


def test_parse_rentmanager_html_returns_empty_on_no_rows() -> None:
    """Page with no `unit_avail_container` rows returns empty."""
    assert parse_rentmanager_html("<html><body>nothing here</body></html>") == []
    assert parse_rentmanager_html("") == []


def test_parse_rentmanager_html_skips_unrendered_placeholder_unit_number() -> None:
    """Unit number with `${...}` is an unrendered template — must skip."""
    template = """
    <tr class='unit_avail_container' data-date='now'>
      <td class='unit-number'>${UnitNumber}</td>
      <td class='unit-beds'>1</td>
      <td class='unit-rent'>$0.00</td>
      <td class='unit-sqft'></td>
      <td class='unit-date'>Now</td>
    </tr>
    """
    assert parse_rentmanager_html(template) == []


def test_parse_rentmanager_html_skips_zero_rent_rows() -> None:
    """Row with no parseable rent (header row, placeholder) gets dropped."""
    bogus = """
    <tr class='unit_avail_container' data-date='now'>
      <td class='unit-number'>1</td>
      <td class='unit-beds'>1</td>
      <td class='unit-rent'>Call for pricing</td>
      <td class='unit-sqft'>500</td>
      <td class='unit-date'>Now</td>
    </tr>
    """
    assert parse_rentmanager_html(bogus) == []


def test_detector_recognizes_rentmanager_marker() -> None:
    """Detector returns pms='rentmanager' when HTML has the footer link."""
    detection = detect_pms(
        "https://krcapartments.com/property-detail/norfolk-place/",
        page_html='<html><a href="https://rentmanager.com/websites">RM</a></html>',
    )
    assert detection.pms == "rentmanager"
    assert detection.confidence >= 0.85
    assert detection.recommended_strategy == "dom_first"


def test_detector_recognizes_rentmanager_via_unit_avail_container() -> None:
    """Detector also fires on the secondary `unit_avail_container` signal."""
    detection = detect_pms(
        "https://example.com/property/foo/",
        page_html=(
            "<html><tr class='unit_avail_container'><td>x</td></tr></html>"
        ),
    )
    assert detection.pms == "rentmanager"


@pytest.mark.asyncio
async def test_adapter_extract_uses_fetch_result_body() -> None:
    """Adapter parses HTML from ctx.fetch_result.body when page=None."""

    class _StubFetchResult:
        body = _SAMPLE_HTML.encode("utf-8")

    ctx = AdapterContext(
        base_url="https://krcapartments.com/property-detail/norfolk-place/",
        detected=detect_pms(
            "https://krcapartments.com/",
            page_html=_SAMPLE_HTML,
        ),
        profile=None,
        expected_total_units=None,
        property_id="TEST",
        fetch_result=_StubFetchResult(),
    )
    adapter = RentManagerAdapter()
    result = await adapter.extract(None, ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) == 2
    assert result.tier_used == "TIER_3_DOM_RENTMANAGER"
    assert result.confidence > 0.6


@pytest.mark.asyncio
async def test_adapter_extract_returns_no_rows_tier_when_empty() -> None:
    """Empty body produces TIER_3_DOM_RENTMANAGER_NO_HTML or NO_ROWS."""

    class _EmptyFetchResult:
        body = None

    ctx = AdapterContext(
        base_url="https://krcapartments.com/",
        detected=detect_pms("https://krcapartments.com/"),
        profile=None,
        expected_total_units=None,
        property_id="TEST",
        fetch_result=_EmptyFetchResult(),
    )
    adapter = RentManagerAdapter()
    result = await adapter.extract(None, ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.tier_used.startswith("TIER_3_DOM_RENTMANAGER_NO")
    assert any("RENTMANAGER_NO" in e for e in result.errors)
