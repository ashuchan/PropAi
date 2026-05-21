"""Phase 6.2 — extract unit records from ``<table>`` blocks with a
recognisable floor-plan header row.

2026-05-21: ~8 properties in the HAR ``actionable_html_extractor``
bucket ship unit data as a plain ``<table>``. Before 6.2 these
all fell through to Tier-4 LLM despite having clean, deterministic
column-mapped data. The extractor scores by header keywords and
emits one unit per data row, applying ≥2-row + beds+(rent|sqft)
floors to keep amenities / fees / nav tables out.
"""

from __future__ import annotations

from ma_poc.pms.adapters._html_extract import extract_units_from_html_tables

# ─────────────────────────────────────────────────────────────────────
# Happy path — canonical floor-plan table
# ─────────────────────────────────────────────────────────────────────


_CANONICAL_TABLE = """
<html><body>
<h2>Available Floor Plans</h2>
<table class="floorplans">
  <thead>
    <tr>
      <th>Plan</th>
      <th>Beds</th>
      <th>Baths</th>
      <th>Sqft</th>
      <th>Rent</th>
      <th>Available</th>
    </tr>
  </thead>
  <tbody>
    <tr><td>The Oak</td><td>1</td><td>1</td><td>720</td><td>$1,450</td><td>Now</td></tr>
    <tr><td>The Maple</td><td>2</td><td>2</td><td>980</td><td>$1,850</td><td>6/1/2026</td></tr>
    <tr><td>The Pine</td><td>3</td><td>2</td><td>1240</td><td>$2,250</td><td>7/15/2026</td></tr>
  </tbody>
</table>
</body></html>
"""


def test_extracts_canonical_table() -> None:
    units = extract_units_from_html_tables(_CANONICAL_TABLE, "https://x.test/")
    assert len(units) == 3, f"expected 3 units; got {len(units)}: {units}"
    oak = units[0]
    assert oak["floor_plan_name"] == "The Oak"
    assert oak["bedrooms"] == "1"
    assert oak["bathrooms"] == "1"
    assert oak["sqft"] == "720"
    assert oak["market_rent_low"] == 1450
    assert oak["market_rent_high"] == 1450
    assert oak["rent_range"] == "$1,450"
    assert oak["availability_status"] == "Now"


def test_records_source_url() -> None:
    units = extract_units_from_html_tables(_CANONICAL_TABLE, "https://x.test/fp")
    assert units, "no units"
    assert units[0]["source_api_url"] == "https://x.test/fp"
    assert units[0]["source"] == "html_table"


# ─────────────────────────────────────────────────────────────────────
# Header-keyword variants
# ─────────────────────────────────────────────────────────────────────


def test_handles_variant_header_labels() -> None:
    """``Square Feet`` / ``Monthly Rent`` / ``Move-in Date`` etc. —
    canonical mapping is keyword-based, not exact-match."""
    html = """
    <table>
      <thead>
        <tr>
          <th>Floor Plan</th>
          <th>Bedrooms</th>
          <th>Bathrooms</th>
          <th>Square Feet</th>
          <th>Monthly Rent</th>
          <th>Move-in Date</th>
        </tr>
      </thead>
      <tbody>
        <tr><td>Aspen</td><td>1</td><td>1</td><td>700</td><td>$1,400</td><td>6/1</td></tr>
        <tr><td>Birch</td><td>2</td><td>2</td><td>1000</td><td>$1,800</td><td>7/1</td></tr>
      </tbody>
    </table>
    """
    units = extract_units_from_html_tables(html, "")
    assert len(units) == 2, units
    assert units[0]["floor_plan_name"] == "Aspen"
    assert units[1]["availability_date"] == "7/1"


def test_handles_studio_in_beds_column() -> None:
    """``Studio`` in the beds column normalises to 0 — common rental
    convention."""
    html = """
    <table>
      <thead><tr><th>Plan</th><th>Beds</th><th>Sqft</th><th>Rent</th></tr></thead>
      <tbody>
        <tr><td>S1</td><td>Studio</td><td>500</td><td>$1,200</td></tr>
        <tr><td>S2</td><td>Studio</td><td>520</td><td>$1,250</td></tr>
      </tbody>
    </table>
    """
    units = extract_units_from_html_tables(html, "")
    assert len(units) == 2
    assert units[0]["bedrooms"] == "0"


def test_parses_rent_range_with_dash() -> None:
    """``$1,450 - $1,850`` should produce low + high + formatted range."""
    html = """
    <table>
      <thead><tr><th>Plan</th><th>Beds</th><th>Rent</th></tr></thead>
      <tbody>
        <tr><td>A</td><td>1</td><td>$1,450 - $1,850</td></tr>
        <tr><td>B</td><td>2</td><td>$1,950 - $2,400</td></tr>
      </tbody>
    </table>
    """
    units = extract_units_from_html_tables(html, "")
    assert len(units) == 2
    assert units[0]["market_rent_low"] == 1450
    assert units[0]["market_rent_high"] == 1850
    assert units[0]["rent_range"] == "$1,450 - $1,850"


def test_handles_call_for_pricing() -> None:
    """``Call for Pricing`` cells leave rent fields blank but the row
    still emits IF sqft is also present (sqft is the alternate
    qualifying signal). Without rent OR sqft, the row drops."""
    html = """
    <table>
      <thead><tr><th>Plan</th><th>Beds</th><th>Sqft</th><th>Rent</th></tr></thead>
      <tbody>
        <tr><td>A</td><td>1</td><td>720</td><td>Call for Pricing</td></tr>
        <tr><td>B</td><td>2</td><td>980</td><td>$1,850</td></tr>
      </tbody>
    </table>
    """
    units = extract_units_from_html_tables(html, "")
    # Row A qualifies via sqft alone; rent stays blank
    assert len(units) == 2, units
    assert units[0]["market_rent_low"] is None
    assert units[0]["sqft"] == "720"
    assert units[0]["availability_status"] == "call for pricing"


# ─────────────────────────────────────────────────────────────────────
# Negative filters — keep noise out
# ─────────────────────────────────────────────────────────────────────


def test_rejects_amenities_table() -> None:
    """An amenities catalogue table must not emit units even if it
    happens to have header keywords that look unit-shaped."""
    html = """
    <table>
      <thead><tr><th>Feature</th><th>Beds</th><th>Available</th></tr></thead>
      <tbody>
        <tr><td>Granite Counters</td><td>1</td><td>Yes</td></tr>
        <tr><td>Stainless Appliances</td><td>2</td><td>Yes</td></tr>
      </tbody>
    </table>
    """
    units = extract_units_from_html_tables(html, "")
    assert units == [], f"amenities table emitted units: {units}"


def test_rejects_fees_table() -> None:
    """A fees/deposit table has dollar signs and integer counts but
    no real plan/beds vocab — must be rejected."""
    html = """
    <table>
      <thead><tr><th>Fee</th><th>Amount</th><th>Refundable</th></tr></thead>
      <tbody>
        <tr><td>Application Fee</td><td>$50</td><td>No</td></tr>
        <tr><td>Pet Deposit</td><td>$250</td><td>Yes</td></tr>
      </tbody>
    </table>
    """
    units = extract_units_from_html_tables(html, "")
    assert units == [], f"fees table emitted units: {units}"


def test_rejects_table_missing_beds_header() -> None:
    """Beds is the mandatory header — without it we can't say it's a
    unit table."""
    html = """
    <table>
      <thead><tr><th>Plan</th><th>Sqft</th><th>Rent</th></tr></thead>
      <tbody>
        <tr><td>A</td><td>720</td><td>$1,450</td></tr>
        <tr><td>B</td><td>980</td><td>$1,850</td></tr>
      </tbody>
    </table>
    """
    units = extract_units_from_html_tables(html, "")
    assert units == [], f"missing-beds table emitted units: {units}"


def test_rejects_single_row_table() -> None:
    """A single-row table is probably a footnote / pricing teaser —
    require ≥2 qualifying rows."""
    html = """
    <table>
      <thead><tr><th>Plan</th><th>Beds</th><th>Sqft</th><th>Rent</th></tr></thead>
      <tbody>
        <tr><td>A</td><td>1</td><td>720</td><td>$1,450</td></tr>
      </tbody>
    </table>
    """
    units = extract_units_from_html_tables(html, "")
    assert units == [], f"single-row table emitted: {units}"


def test_handles_table_with_no_thead() -> None:
    """Hand-rolled CMSes often put headers in <td> of the first <tr>,
    no <thead> wrapper."""
    html = """
    <table>
      <tr><td>Plan</td><td>Beds</td><td>Sqft</td><td>Rent</td></tr>
      <tr><td>A</td><td>1</td><td>720</td><td>$1,450</td></tr>
      <tr><td>B</td><td>2</td><td>980</td><td>$1,850</td></tr>
    </table>
    """
    units = extract_units_from_html_tables(html, "")
    assert len(units) == 2, f"no-thead table missed; got {units}"


def test_empty_html_returns_empty() -> None:
    assert extract_units_from_html_tables("", "") == []
    assert extract_units_from_html_tables(
        "<html><body><p>Welcome.</p></body></html>", ""
    ) == []
