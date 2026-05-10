"""Pin per-field selector replay in ``extract_with_hints``.

Background — yesterday's audit identified that ``extract_with_hints``
only honoured ``hints.container``; the per-field selectors (rent /
sqft / beds / baths / avail_date / floor_plan_name / availability_status
/ amenities / concession) were persisted by ``profile_updater`` but
ignored by the replay extractor, which fell back to regex on the
container's full text. Adding amenities + concession slots to the
model is necessary but not sufficient until the replay path uses them.

These tests pin the new per-field replay contract:
  - When a per-field selector is set AND matches, it overrides the
    regex value.
  - When the selector is absent or doesn't match, the regex fallback
    fills the field (back-compat with the existing
    ``test_dom_hints_wiring`` contract).
  - amenities + concession (newly added slots) populate the per-unit
    dict so schema_v2 and the new units.amenities column see them.
"""

from __future__ import annotations

from ma_poc.models.scrape_profile import FieldSelectorMap
from ma_poc.pms.adapters._html_extract import extract_with_hints


_HTML_RICH = """
<html><body>
<div class="unit-card">
  <span class="unit-no">405</span>
  <span class="plan-name">The Aspen</span>
  <span class="price">$1,825/mo</span>
  <span class="size">820 sqft</span>
  <span class="beds">2 bed</span>
  <span class="baths">2 bath</span>
  <span class="avail">2026-06-15</span>
  <span class="status">AVAILABLE</span>
  <ul class="amen"><li>pool</li><li>gym</li><li>dog park</li></ul>
  <span class="conc">1 month free</span>
</div>
<div class="unit-card">
  <span class="unit-no">410</span>
  <span class="plan-name">The Birch</span>
  <span class="price">$2,200/mo</span>
  <span class="size">1100 sqft</span>
  <span class="beds">3 bed</span>
  <span class="baths">2 bath</span>
  <span class="avail">2026-07-01</span>
  <span class="status">AVAILABLE</span>
  <ul class="amen"><li>balcony</li><li>in-unit washer/dryer</li></ul>
</div>
</body></html>
"""


def test_full_field_selectors_drive_per_field_extraction() -> None:
    hints = FieldSelectorMap(
        container=".unit-card",
        unit_id=".unit-no",
        floor_plan_name=".plan-name",
        rent=".price",
        sqft=".size",
        bedrooms=".beds",
        bathrooms=".baths",
        availability_date=".avail",
        availability_status=".status",
        amenities=".amen li",
        concession=".conc",
    )
    units = extract_with_hints(_HTML_RICH, "https://x.com", hints)
    assert len(units) == 2

    u1, u2 = units
    assert u1["unit_number"] == "405"
    assert u1["floor_plan_name"] == "The Aspen"
    assert u1["market_rent_low"] == 1825
    assert u1["market_rent_high"] == 1825
    assert u1["sqft"] == "820"
    assert u1["bedrooms"] == "2"
    assert u1["bathrooms"] == "2"
    assert u1["availability_date"] == "2026-06-15"
    assert u1["availability_status"] == "AVAILABLE"
    # Amenities — multi-element selector returns each <li> independently.
    assert u1["amenities"] == ["pool", "gym", "dog park"]
    # Concession — provenance auto-stamped to the V2 enum.
    assert u1["concession_text"] == "1 month free"
    assert u1["concession_source"] == "strikethrough_dom"

    assert u2["unit_number"] == "410"
    assert u2["floor_plan_name"] == "The Birch"
    assert u2["amenities"] == ["balcony", "in-unit washer/dryer"]
    # No concession on second unit — the field stays absent rather than
    # carrying the previous unit's value.
    assert "concession_text" not in u2 or u2.get("concession_text") in (None, "")


def test_container_only_hint_still_works_back_compat() -> None:
    """The original test_dom_hints_wiring contract — when only
    ``container`` is set, regex fallback fills every field. New per-field
    slots default to None and don't break extraction."""
    hints = FieldSelectorMap(container=".unit-card")
    units = extract_with_hints(_HTML_RICH, "https://x.com", hints)
    assert len(units) == 2
    # Regex picked up rents from the visible text:
    rents = sorted(u["market_rent_low"] for u in units)
    assert rents == [1825, 2200]


def test_amenities_comma_separated_string_splits() -> None:
    """When the amenities selector matches a single element whose text
    is a comma-separated list, it splits into individual items rather
    than emitting a single concatenated string."""
    html = """
    <html><body>
    <div class="card">
      <span class="rent">$1,500</span>
      <span class="size">700 sqft</span>
      <span class="beds">1 bed</span>
      <span class="amen">pool, gym, dog park, balcony</span>
    </div>
    </body></html>
    """
    hints = FieldSelectorMap(
        container=".card",
        rent=".rent",
        sqft=".size",
        bedrooms=".beds",
        amenities=".amen",
    )
    units = extract_with_hints(html, "https://x.com", hints)
    assert len(units) == 1
    assert units[0]["amenities"] == ["pool", "gym", "dog park", "balcony"]


def test_partial_selector_misses_fall_back_to_regex() -> None:
    """When a per-field selector is set but doesn't match a node, the
    field falls back to the regex value — a partially-broken hint set
    degrades gracefully rather than wiping out otherwise-valid units."""
    html = """
    <html><body>
    <div class="card">
      <span class="rent">$1,500</span>
      820 sqft, 1 bed, 1 bath
    </div>
    </body></html>
    """
    hints = FieldSelectorMap(
        container=".card",
        rent=".rent",
        sqft=".size-not-present",  # Won't match — regex must fill the gap.
    )
    units = extract_with_hints(html, "https://x.com", hints)
    assert len(units) == 1
    assert units[0]["market_rent_low"] == 1500
    # Regex fallback picked up sqft from the container text.
    assert units[0]["sqft"] == "820"
