"""RentCafe-WordPress plan-card enrichment tests (2026-05-23).

Pins the parser + merger that lifts the ironstate.com portfolio
(5 properties, 104 units) out of SUCCESS_PLAN_LEVEL by joining
the homepage's WordPress floorplan cards to SecureCafe drill units
by FloorPlanID. See module docstring on
``ma_poc/pms/adapters/_rentcafe_wp_floorplan_cards.py`` for the full
data-shape research log.
"""
from __future__ import annotations

from ma_poc.pms.adapters._rentcafe_wp_floorplan_cards import (
    has_wp_floorplan_cards,
    merge_wp_cards_into_securecafe,
    parse_wp_floorplan_cards,
)

# ─── card markup captured live from ironstate.com/property/the-gotham ─

_GOTHAM_HTML = """
<section class="floorplans">
  <article class="floorplans-box active" data-price="3150" data-beds="0"
           data-baths="1" data-movein="05/14/2026">
    <a href="https://ironstate.com/property/the-gotham/floorplan/5059833"
       class="box">
      <figure class="img-box">
        <img src="/dmslivecafe/GOT-StudioC.jpg" alt="" loading="lazy">
      </figure>
      <div class="txt-box">
        <ul class="collection"><li>Available Now</li></ul>
        <h3><span>Studio</span></h3>
        <ul><li>$3,150</li><li>544 sq. ft.</li></ul>
        <ul><li>1 Available</li></ul>
      </div>
    </a>
  </article>
  <article class="floorplans-box" data-price="3390" data-beds="1"
           data-baths="1" data-movein="06/01/2026">
    <a href="https://ironstate.com/property/the-gotham/floorplan/5059848"
       class="box">
      <h3><span>1 Bed - 1 Bath</span></h3>
      <ul><li>$3,390</li><li>680 sq. ft.</li></ul>
    </a>
  </article>
  <article class="floorplans-box" data-price="4980" data-beds="2"
           data-baths="2" data-movein="07/01/2026">
    <a href="https://ironstate.com/property/the-gotham/floorplan/5059889"
       class="box">
      <h3><span>2 Bed - 2 Bath</span></h3>
      <ul><li>$4,980</li><li>1,060 sq. ft.</li></ul>
    </a>
  </article>
</section>
"""


# ─── detection ───────────────────────────────────────────────────────


def test_has_wp_floorplan_cards_positive() -> None:
    assert has_wp_floorplan_cards(_GOTHAM_HTML) is True


def test_has_wp_floorplan_cards_negative() -> None:
    assert has_wp_floorplan_cards("") is False
    assert has_wp_floorplan_cards("<html>no marker here</html>") is False
    # The marker must be the literal "floorplans-box" — adjacent words
    # don't match.
    assert has_wp_floorplan_cards("<div class='floor-plans'>x</div>") is False


# ─── parse_wp_floorplan_cards ────────────────────────────────────────


def test_parse_wp_floorplan_cards_extracts_all_fields() -> None:
    plans = parse_wp_floorplan_cards(_GOTHAM_HTML)
    assert len(plans) == 3
    # Cards come out in document order.
    studio = plans[0]
    assert studio["fp_id"] == "5059833"
    assert studio["rent"] == "3150"
    assert studio["beds"] == "0"
    assert studio["baths"] == "1"
    assert studio["sqft"] == "544"
    assert studio["name"] == "Studio"

    one_bed = plans[1]
    assert one_bed["fp_id"] == "5059848"
    assert one_bed["sqft"] == "680"
    assert one_bed["name"] == "1 Bed - 1 Bath"

    two_bed = plans[2]
    # Comma-separated sqft must be normalised.
    assert two_bed["sqft"] == "1060"


def test_parse_wp_floorplan_cards_drops_cards_without_fpid() -> None:
    """Without a /floorplan/<id> href the card cannot be joined to SC
    units. Drop it rather than emit unkeyable noise."""
    html = """
    <article class="floorplans-box" data-price="2000" data-beds="1"
             data-baths="1">
      <a href="https://example.com/contact" class="box">
        <h3><span>Phantom</span></h3>
        <ul><li>$2,000</li><li>500 sq. ft.</li></ul>
      </a>
    </article>
    """
    assert parse_wp_floorplan_cards(html) == []


def test_parse_wp_floorplan_cards_filters_implausible_sqft() -> None:
    """A card whose sqft string parses to < 100 (typo / spurious match)
    drops the sqft field but keeps the other data — partial enrichment
    is still useful for floor_plan_name back-fill."""
    html = """
    <article class="floorplans-box" data-price="2000" data-beds="1"
             data-baths="1">
      <a href="https://x.com/floorplan/42" class="box">
        <h3><span>Tiny</span></h3>
        <ul><li>$2,000</li><li>99 sq. ft.</li></ul>
      </a>
    </article>
    """
    plans = parse_wp_floorplan_cards(html)
    assert len(plans) == 1
    assert plans[0]["fp_id"] == "42"
    assert plans[0]["sqft"] == ""
    assert plans[0]["name"] == "Tiny"


def test_parse_wp_floorplan_cards_empty_when_no_marker() -> None:
    assert parse_wp_floorplan_cards("") == []
    assert parse_wp_floorplan_cards("<html>no floorplans-box here</html>") == []


# ─── merge_wp_cards_into_securecafe ──────────────────────────────────


def _make_sc_unit(
    fp_id: str = "5059833", sqft: str = "", plan_name: str = "Studio"
) -> dict:
    return {
        "unit_number": "101", "floor_plan_name": plan_name,
        "bedrooms": "0", "bathrooms": "1.0", "sqft": sqft,
        "market_rent_low": 3150, "market_rent_high": 3150,
        "source_ids": {"securecafe_floorplan_id": fp_id},
    }


def test_merge_wp_cards_fills_sqft_by_fpid_exact_match() -> None:
    plans = parse_wp_floorplan_cards(_GOTHAM_HTML)
    units = [
        _make_sc_unit(fp_id="5059833"),
        _make_sc_unit(fp_id="5059848", plan_name="1 Bed - 1 Bath"),
        _make_sc_unit(fp_id="5059889", plan_name="2 Bed - 2 Bath"),
    ]
    n = merge_wp_cards_into_securecafe(units, plans)
    assert n == 3
    assert units[0]["sqft"] == "544"
    assert units[1]["sqft"] == "680"
    assert units[2]["sqft"] == "1060"


def test_merge_wp_cards_no_fuzzy_fallback_for_unknown_fpid() -> None:
    """STRICT join: an SC unit with an unknown FloorPlanID must NOT
    inherit sqft from some other plan via name/bed-bath match. Wrong
    fills here would silently corrupt prod data — better to leave
    sqft blank."""
    plans = parse_wp_floorplan_cards(_GOTHAM_HTML)
    # Same beds/baths/name as plan[0] but a *different* fp_id.
    units = [_make_sc_unit(fp_id="9999999", plan_name="Studio")]
    n = merge_wp_cards_into_securecafe(units, plans)
    assert n == 0
    assert units[0]["sqft"] == ""


def test_merge_wp_cards_skips_unit_with_no_fpid_capture() -> None:
    """A unit whose ``source_ids`` lacks ``securecafe_floorplan_id``
    (older AvailUnitRow markup, parser path that didn't run the
    FloorPlanID capture) cannot be joined — leave it unenriched."""
    plans = parse_wp_floorplan_cards(_GOTHAM_HTML)
    units = [{"unit_number": "101", "sqft": "", "source_ids": {}}]
    n = merge_wp_cards_into_securecafe(units, plans)
    assert n == 0


def test_merge_wp_cards_preserves_existing_unit_values() -> None:
    plans = parse_wp_floorplan_cards(_GOTHAM_HTML)
    units = [_make_sc_unit(fp_id="5059833", sqft="999")]
    n = merge_wp_cards_into_securecafe(units, plans)
    assert n == 0  # nothing filled
    assert units[0]["sqft"] == "999"  # original wins


def test_merge_wp_cards_overwrites_zero_sqft() -> None:
    """Like the apts247 path: sqft == '0' is the lancasterridge pattern.
    Treat it as missing and overwrite."""
    plans = parse_wp_floorplan_cards(_GOTHAM_HTML)
    units = [_make_sc_unit(fp_id="5059833", sqft="0")]
    n = merge_wp_cards_into_securecafe(units, plans)
    assert n == 1
    assert units[0]["sqft"] == "544"


def test_merge_wp_cards_fills_floor_plan_name_when_blank() -> None:
    """sqft missing on the plan card AND the unit's floor_plan_name is
    blank → fill the name as a fallback (better than empty)."""
    custom_html = """
    <article class="floorplans-box" data-price="2000" data-beds="1"
             data-baths="1">
      <a href="https://x.com/floorplan/42" class="box">
        <h3><span>The Magnolia</span></h3>
        <ul><li>$2,000</li></ul>
      </a>
    </article>
    """
    plans = parse_wp_floorplan_cards(custom_html)
    assert plans[0]["sqft"] == ""  # no sq. ft. in markup
    units = [{"unit_number": "1", "sqft": "", "floor_plan_name": "",
              "source_ids": {"securecafe_floorplan_id": "42"}}]
    n = merge_wp_cards_into_securecafe(units, plans)
    assert n == 1
    assert units[0]["floor_plan_name"] == "The Magnolia"


def test_merge_wp_cards_noop_when_no_units_or_no_plans() -> None:
    plans = parse_wp_floorplan_cards(_GOTHAM_HTML)
    assert merge_wp_cards_into_securecafe([], plans) == 0
    assert merge_wp_cards_into_securecafe([_make_sc_unit()], []) == 0


# ─── end-to-end SUCCESS-bar smoke test ───────────────────────────────


def test_end_to_end_ironstate_cards_lift_units_to_rent_plus_sqft() -> None:
    """Mirrors the production flow: SC drill produces units carrying
    rent + unit_number + source_ids but blank sqft → WP-cards parser
    extracts plan map from the same homepage HTML → merge fills sqft
    → units clear the Surgex ≥1-unit-with-rent+sqft bar."""
    # Simulate SC drill output (rent + unit_number, NO sqft):
    sc_units = [
        _make_sc_unit(fp_id="5059833"),
        _make_sc_unit(fp_id="5059889", plan_name="2 Bed - 2 Bath"),
    ]
    assert all(u["sqft"] == "" for u in sc_units)
    # WP-card extraction from homepage:
    plans = parse_wp_floorplan_cards(_GOTHAM_HTML)
    merge_wp_cards_into_securecafe(sc_units, plans)
    # Verify the SUCCESS bar.
    rent_and_sqft = [
        u for u in sc_units if u.get("market_rent_low") and u.get("sqft")
    ]
    assert len(rent_and_sqft) == 2
    assert rent_and_sqft[0]["sqft"] == "544"
    assert rent_and_sqft[1]["sqft"] == "1060"
