"""Phase 6.3 — extract unit records from ``data-*`` attribute cards.

2026-05-21: a small cluster of HARs in actionable_html_extractor ship
unit data inside ``data-*`` attributes on per-card ``<div>``s. The DOM
cascade misses these because the visible text doesn't always contain
the numeric data — the rendered DOM uses the attributes for client-side
hydration. Phase 6.3 reads the attributes directly.

Floors (false-positive protection):
  • Per-card: ≥3 vocab-matching data-* attributes AND beds + (rent OR sqft).
  • Per-page: ≥2 sibling cards share the same parent.
"""

from __future__ import annotations

from ma_poc.pms.adapters._html_extract import extract_units_from_data_attr_cards

# ─────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────


_BASIC_CARDS = """
<html><body>
<section class="floorplans">
  <div class="card"
       data-unit="A-101" data-beds="1" data-baths="1"
       data-sqft="720" data-rent="1450" data-available="2026-06-01">
    Aspen 1BR
  </div>
  <div class="card"
       data-unit="A-102" data-beds="2" data-baths="2"
       data-sqft="980" data-rent="1850" data-available="2026-06-15">
    Birch 2BR
  </div>
  <div class="card"
       data-unit="A-103" data-beds="3" data-baths="2"
       data-sqft="1240" data-rent="2250" data-available="2026-07-01">
    Cedar 3BR
  </div>
</section>
</body></html>
"""


def test_extracts_basic_card_set() -> None:
    units = extract_units_from_data_attr_cards(_BASIC_CARDS, "https://x.test/")
    assert len(units) == 3, f"expected 3 cards; got {len(units)}: {units}"
    aspen = units[0]
    assert aspen["unit_number"] == "A-101"
    assert aspen["bedrooms"] == "1"
    assert aspen["bathrooms"] == "1"
    assert aspen["sqft"] == "720"
    assert aspen["market_rent_low"] == 1450
    assert aspen["market_rent_high"] == 1450
    assert aspen["rent_range"] == "$1,450"
    assert aspen["availability_date"] == "2026-06-01"
    assert aspen["source"] == "html_data_attr"
    assert aspen["source_api_url"] == "https://x.test/"


def test_handles_dashed_attribute_names() -> None:
    """``data-sq-ft`` / ``data-monthly-rent`` / ``data-move-in-date``
    — Phase 6.3 normalises these to the same canonical fields."""
    html = """
    <div class="grid">
      <article data-unit="1A" data-bed="1" data-bath="1"
               data-sq-ft="710" data-monthly-rent="1400"
               data-move-in-date="6/1/2026"></article>
      <article data-unit="2A" data-bed="2" data-bath="2"
               data-sq-ft="990" data-monthly-rent="1800"
               data-move-in-date="7/1/2026"></article>
    </div>
    """
    units = extract_units_from_data_attr_cards(html, "")
    assert len(units) == 2, units
    assert units[0]["sqft"] == "710"
    assert units[0]["market_rent_low"] == 1400
    assert units[0]["availability_date"] == "6/1/2026"


def test_parses_rent_range_attribute() -> None:
    """``data-rent="1450-1850"`` should produce a range."""
    html = """
    <div class="cards">
      <div data-unit="A" data-beds="1" data-baths="1" data-rent="1450-1850"></div>
      <div data-unit="B" data-beds="2" data-baths="2" data-rent="1850-2400"></div>
    </div>
    """
    units = extract_units_from_data_attr_cards(html, "")
    assert len(units) == 2
    assert units[0]["market_rent_low"] == 1450
    assert units[0]["market_rent_high"] == 1850
    assert units[0]["rent_range"] == "$1,450 - $1,850"


# ─────────────────────────────────────────────────────────────────────
# Floors — false-positive protection
# ─────────────────────────────────────────────────────────────────────


def test_rejects_single_card_no_siblings() -> None:
    """One ``data-*``-rich element with no siblings carrying matching
    vocab is almost certainly a "more info" link / form, not a floor
    plan."""
    html = """
    <div class="lone">
      <div data-unit="A" data-beds="1" data-baths="1"
           data-sqft="720" data-rent="1450"></div>
    </div>
    """
    units = extract_units_from_data_attr_cards(html, "")
    assert units == [], f"single-card emitted: {units}"


def test_rejects_card_below_vocab_floor() -> None:
    """Two cards but each has only 2 vocab attributes — below the
    ≥3-attribute card-level floor. Must be rejected."""
    html = """
    <div class="cards">
      <div data-beds="1" data-rent="1450" data-track-id="x"></div>
      <div data-beds="2" data-rent="1850" data-track-id="y"></div>
    </div>
    """
    units = extract_units_from_data_attr_cards(html, "")
    assert units == [], f"low-vocab cards emitted: {units}"


def test_rejects_card_missing_beds() -> None:
    """Per-card filter: beds is required."""
    html = """
    <div class="cards">
      <div data-unit="A" data-sqft="720" data-rent="1450" data-floor="1"></div>
      <div data-unit="B" data-sqft="980" data-rent="1850" data-floor="2"></div>
    </div>
    """
    units = extract_units_from_data_attr_cards(html, "")
    assert units == [], f"no-beds cards emitted: {units}"


def test_rejects_card_missing_rent_and_sqft() -> None:
    """Per-card filter: rent OR sqft is required."""
    html = """
    <div class="cards">
      <div data-unit="A" data-beds="1" data-baths="1" data-floor="1"></div>
      <div data-unit="B" data-beds="2" data-baths="2" data-floor="2"></div>
    </div>
    """
    units = extract_units_from_data_attr_cards(html, "")
    assert units == [], units


def test_does_not_merge_cards_across_unrelated_parents() -> None:
    """Two cards under different parents must be treated as singletons
    (and each rejected by the sibling-floor). This guards against a
    page with one floor-plan div + one unrelated stats widget div both
    happening to carry similar data-* names."""
    html = """
    <html><body>
      <section class="floorplans">
        <div data-unit="A" data-beds="1" data-baths="1" data-sqft="720"></div>
      </section>
      <section class="stats">
        <div data-unit="B" data-beds="2" data-baths="2" data-sqft="980"></div>
      </section>
    </body></html>
    """
    units = extract_units_from_data_attr_cards(html, "")
    assert units == [], f"cross-parent cards merged: {units}"


def test_handles_studio_in_data_beds() -> None:
    """``data-beds="studio"`` should normalise to 0."""
    html = """
    <div class="cards">
      <div data-unit="S1" data-beds="studio" data-sqft="500" data-rent="1200"></div>
      <div data-unit="S2" data-beds="studio" data-sqft="520" data-rent="1250"></div>
    </div>
    """
    units = extract_units_from_data_attr_cards(html, "")
    assert len(units) == 2
    assert units[0]["bedrooms"] == "0"
