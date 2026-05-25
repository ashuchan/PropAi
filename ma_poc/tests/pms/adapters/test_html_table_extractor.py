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


# ─────────────────────────────────────────────────────────────────────
# Spearhead Oak-I sqft-leak residue (chip #106 follow-up, 2026-05-25)
# ─────────────────────────────────────────────────────────────────────
#
# spearheadproperties.com (Oak-I, Eden Roc) renders its unit roster with
# header columns "Unit Type | Price | Beds | Baths | Unit Size |
# Availability Date". Before this commit, the substring fallback in
# ``_TABLE_HEADER_VOCAB`` matched the bare "unit" key against BOTH
# "Unit Type" and "Unit Size" headers, so both columns mapped to
# ``unit_number``. With cell-level last-write-wins, the unit_number
# slot ended up carrying the sqft text ("623 sq ft") and the real sqft
# column was never populated. Chip #106 in ``_parsing.py`` cleaned the
# leaked text inside ``make_unit_dict``, but ``extract_units_from_html_tables``
# builds unit dicts inline without calling ``make_unit_dict`` — so the
# leak survived for the table-extracted path on 4 of 6 user-flagged
# units. The fix adds explicit vocab entries (``unit type`` →
# ``floor_plan_name``, ``unit size`` → ``sqft``) so exact match wins
# before the substring fallback, and adds a defensive ``clean_unit_number``
# pass inside the table extractor as a safety net for future
# substring-collision shapes.


_SPEARHEAD_OAK_I_TABLE = """
<table class="table  table-striped table-multi-properties spearhead-multi-unit-galleries">
  <thead>
    <tr>
      <th>Unit Type</th>
      <th>Price</th>
      <th>Beds</th>
      <th>Baths</th>
      <th>Unit Size</th>
      <th>Availability Date</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td class="title blue"><p>1 Bedroom Unit - A</p></td>
      <td>$1,400 / mo</td><td>1</td><td>1</td><td>623 sq ft</td><td> 8/21/2026</td>
    </tr>
    <tr>
      <td class="title blue"><p>1 Bedroom Unit - B</p></td>
      <td>$1,600 / mo</td><td>1</td><td>1</td><td>727 sq ft</td><td> 05/5/2026</td>
    </tr>
    <tr>
      <td class="title blue"><p>2 Bedroom Unit</p></td>
      <td>$1,800 / mo</td><td>2</td><td>1</td><td>975 sq ft</td>
      <td> No Upcoming Vacancies - Contact us to be added to our email list!</td>
    </tr>
    <tr>
      <td class="title blue"><p>2 Bedroom Unit (Remodeled)</p></td>
      <td>$1,900 / mo</td><td>2</td><td>1</td><td>975 sq ft</td>
      <td> No Upcoming Vacancies - Contact us to be added to our email list!</td>
    </tr>
    <tr>
      <td class="title blue"><p>1 Bedroom Unit - A (Remodeled)</p></td>
      <td>$1,475 / mo</td><td>1</td><td>1</td><td>623 sq ft</td>
      <td> No Upcoming Vacancies - Contact us to be added to our email list!</td>
    </tr>
    <tr>
      <td class="title blue"><p>1 Bedroom Unit - B (Remodeled)</p></td>
      <td>$1,650 / mo</td><td>1</td><td>1</td><td>727 sq ft</td>
      <td> No Upcoming Vacancies - Contact us to be added to our email list!</td>
    </tr>
  </tbody>
</table>
"""


def test_spearhead_oak_i_all_six_units_recovered() -> None:
    """Regression guard: pin all 6 Oak-I units extract cleanly.

    Before the chip #106 follow-up (2026-05-25), only 2 of 6 units made
    it through with clean fields; the other 4 had ``unit_number =
    "623 sq ft"`` (or 727 / 975) and ``sqft = ""`` because the substring
    fallback misrouted the "Unit Size" header to unit_number.
    """
    units = extract_units_from_html_tables(
        _SPEARHEAD_OAK_I_TABLE,
        "https://www.spearheadproperties.com/property/oak-i/",
    )
    assert len(units) == 6, f"expected 6 units; got {len(units)}"

    # No unit should have sqft text leaked into unit_number — the residue
    # the user flagged.
    for u in units:
        assert "sq ft" not in (u.get("unit_number") or ""), (
            f"sqft leaked into unit_number: {u!r}"
        )

    # Each row should have a parsed integer sqft (not "", not -1).
    for u in units:
        assert u["sqft"] in {"623", "727", "975"}, (
            f"sqft not recovered: {u!r}"
        )

    # The "Unit Type" cell carries the floor-plan name, not a unit id.
    plans = [u["floor_plan_name"] for u in units]
    assert plans == [
        "1 Bedroom Unit - A",
        "1 Bedroom Unit - B",
        "2 Bedroom Unit",
        "2 Bedroom Unit (Remodeled)",
        "1 Bedroom Unit - A (Remodeled)",
        "1 Bedroom Unit - B (Remodeled)",
    ], f"floor_plan_name mismatch: {plans}"

    # Rent + beds + baths still recovered.
    rents = [u["market_rent_low"] for u in units]
    assert rents == [1400, 1600, 1800, 1900, 1475, 1650], f"rents={rents}"
    assert [u["bedrooms"] for u in units] == ["1", "1", "2", "2", "1", "1"]
    assert [u["bathrooms"] for u in units] == ["1", "1", "1", "1", "1", "1"]


def test_unit_type_header_maps_to_floor_plan_name() -> None:
    """New vocab alias: ``unit type`` header → floor_plan_name.

    Direct exact-match path (not substring fallback). Picks 4 of the 6
    Spearhead unit shapes from the residue ("Unit Type" header alone is
    enough to exercise the new mapping).
    """
    html = """
    <table>
      <thead><tr>
        <th>Unit Type</th><th>Price</th><th>Beds</th><th>Baths</th><th>Sqft</th>
      </tr></thead>
      <tbody>
        <tr><td>Studio A</td><td>$1,200</td><td>0</td><td>1</td><td>450</td></tr>
        <tr><td>1 Bedroom B</td><td>$1,500</td><td>1</td><td>1</td><td>650</td></tr>
        <tr><td>2 Bedroom C</td><td>$1,900</td><td>2</td><td>2</td><td>950</td></tr>
        <tr><td>3 Bedroom D</td><td>$2,400</td><td>3</td><td>2</td><td>1300</td></tr>
      </tbody>
    </table>
    """
    units = extract_units_from_html_tables(html, "")
    assert len(units) == 4
    assert [u["floor_plan_name"] for u in units] == [
        "Studio A", "1 Bedroom B", "2 Bedroom C", "3 Bedroom D",
    ]
    # And unit_number stays empty (no "Unit #" column in this table).
    assert all(u["unit_number"] == "" for u in units)


def test_unit_size_header_maps_to_sqft() -> None:
    """New vocab alias: ``unit size`` header → sqft.

    Before the fix, the substring fallback matched "unit" first and
    routed the column to unit_number, leaking text like "850 sq ft"
    into the unit identifier. Exact match now wins.
    """
    html = """
    <table>
      <thead><tr>
        <th>Plan</th><th>Beds</th><th>Baths</th><th>Unit Size</th><th>Rent</th>
      </tr></thead>
      <tbody>
        <tr><td>Aspen</td><td>1</td><td>1</td><td>850 sq ft</td><td>$1,500</td></tr>
        <tr><td>Birch</td><td>2</td><td>2</td><td>1,100 sq ft</td><td>$1,900</td></tr>
      </tbody>
    </table>
    """
    units = extract_units_from_html_tables(html, "")
    assert len(units) == 2
    assert [u["sqft"] for u in units] == ["850", "1100"]
    # And the unit_number stays empty — "850 sq ft" never leaked into it.
    assert all(u["unit_number"] == "" for u in units)


def test_defensive_cleanup_when_unit_number_still_leaks() -> None:
    """Belt-and-braces: if a future CMS uses a different header phrasing
    that the vocab doesn't catch and sqft text still leaks into the
    unit_number slot, the in-extractor ``clean_unit_number`` pass strips
    it AND recovers sqft into the empty sqft column.

    Construction: use two columns that both substring-match ``unit``
    via a phrasing the vocab doesn't have an exact entry for ("Unit
    Code" + "Unit Footprint"). The bare "unit" fallback maps both to
    unit_number, the size cell wins the last-write race — and the
    defensive cleanup recovers it.
    """
    html = """
    <table>
      <thead><tr>
        <th>Unit Code</th><th>Beds</th><th>Baths</th><th>Unit Footprint</th><th>Rent</th>
      </tr></thead>
      <tbody>
        <tr><td>A-101</td><td>1</td><td>1</td><td>700 sq ft</td><td>$1,400</td></tr>
        <tr><td>A-102</td><td>2</td><td>2</td><td>950 sq ft</td><td>$1,800</td></tr>
      </tbody>
    </table>
    """
    units = extract_units_from_html_tables(html, "")
    assert len(units) == 2, f"got {len(units)} units"
    # The sqft text was stripped from unit_number, and the real sqft
    # slot was backfilled from the leaked text.
    for u in units:
        assert "sq ft" not in (u.get("unit_number") or "")
    assert [u["sqft"] for u in units] == ["700", "950"]


def test_non_spearhead_tables_not_regressed() -> None:
    """The new vocab entries are additive — verify the canonical
    floor-plan table from the existing happy path still extracts the
    same way (floor_plan_name from "Plan" header, sqft from "Sqft"
    header).
    """
    units = extract_units_from_html_tables(_CANONICAL_TABLE, "")
    assert len(units) == 3
    assert units[0]["floor_plan_name"] == "The Oak"
    assert units[0]["sqft"] == "720"
    assert units[0]["market_rent_low"] == 1450


def test_chip_106_clean_unit_number_still_works_standalone() -> None:
    """The original chip #106 fix lives in ``_parsing.clean_unit_number``
    and runs from ``make_unit_dict``. This test is a smoke check that
    the helper still cleans the exact Spearhead shapes — the chip's
    contract is preserved, this commit only extends its reach to the
    table-extractor path.
    """
    from ma_poc.pms.adapters._parsing import clean_unit_number

    # Pure sqft text → empty (better than shipping a fake id).
    # All three shapes from Oak-I residue, plus the "sqft" / "square feet"
    # / "ft2" variants chip #106 already covered.
    assert clean_unit_number("623 sq ft") == ""
    assert clean_unit_number("727 sq ft") == ""
    assert clean_unit_number("975 sq ft") == ""
    assert clean_unit_number("1,200 sqft") == ""
    assert clean_unit_number("950 ft²") == ""
    # A bare id with no sqft text passes through untouched.
    assert clean_unit_number("A-101") == "A-101"
