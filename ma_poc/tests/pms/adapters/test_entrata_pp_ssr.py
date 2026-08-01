"""Entrata Prospect Portal SSR-grid parser tests (2026-05-24).

Pins the ``parse_entrata_prospectportal_html`` fallback that lifts the
``TIER_1_API_ENTRATA_EMPTY`` cluster (128 props in focused-3886351
canary). Two SSR templates exist in production:

  - Template A — ``.fp-card`` with ``dynamic-text-before/after``
    siblings (older PP layout). Live sample: thebellemeade.com.
  - Template B — ``li.fp-group-item`` with title-labelled ``.fp-col``
    columns (newer PP layout). Live sample: triomke.com.

Both fixtures are unmodified live captures from 2026-05-24 (curl_cffi
chrome120 against the canonical ``/{city}/{slug}/conventional/`` URL).
"""
from __future__ import annotations

from pathlib import Path

from ma_poc.pms.adapters.entrata import (
    _pp_beds_baths,
    _pp_money_low_high,
    parse_entrata_prospectportal_html,
)

FIXTURES = Path(__file__).parent / "fixtures" / "entrata"


def test_pp_money_low_high_em_dash_returns_none() -> None:
    """The em-dash placeholder operators use to hide rent must not
    parse to 0 — that would trip the validity gate as a real $0 row.
    Live observation: ~80% of triomke plans show ``—`` for rent."""
    assert _pp_money_low_high("—") == (None, None)
    assert _pp_money_low_high("--") == (None, None)
    assert _pp_money_low_high("") == (None, None)


def test_pp_money_low_high_single_value() -> None:
    assert _pp_money_low_high("From $1,298 per month") == (1298, 1298)


def test_pp_money_low_high_range() -> None:
    assert _pp_money_low_high("$1,200 - $1,450 / month") == (1200, 1450)


def test_pp_beds_baths_studio() -> None:
    beds, baths = _pp_beds_baths("Studio / 1 ba")
    assert beds == 0
    assert baths == "1"


def test_pp_beds_baths_one_bd_one_ba() -> None:
    beds, baths = _pp_beds_baths("1 bd / 1 ba")
    assert beds == 1
    assert baths == "1"


def test_pp_beds_baths_two_bed_two_half_bath() -> None:
    beds, baths = _pp_beds_baths("2 Bed / 2.5 Bath")
    assert beds == 2
    assert baths == "2.5"


def test_pp_ssr_fp_card_template_bellemeade() -> None:
    """Template A — live thebellemeade.com fixture. 4 floor plans, all
    publish rent + sqft (strict-pass-ready)."""
    html = (FIXTURES / "prospectportal_fp_card_bellemeade.html").read_text()
    units = parse_entrata_prospectportal_html(
        html, "https://www.thebellemeade.com/houston/the-belle-meade-at-river-oaks/conventional/"
    )
    assert len(units) == 4
    names = {u["floor_plan_name"] for u in units}
    assert names == {"The Chilton", "The Willowick", "The Del Monte", "The Inwood"}

    # Every plan in this fixture has both rent and sqft (strict pass)
    for u in units:
        assert u["market_rent_low"], f"missing rent on {u['floor_plan_name']}"
        assert u["sqft"], f"missing sqft on {u['floor_plan_name']}"
        assert u["extraction_tier"] == "TIER_1_DOM_ENTRATA_PP_FPCARD"

    chilton = next(u for u in units if u["floor_plan_name"] == "The Chilton")
    assert chilton["bedrooms"] == "1"
    assert chilton["bathrooms"] == "1"
    assert chilton["sqft"] == "1006"
    assert int(chilton["market_rent_low"]) == 2664
    assert chilton["available_units"] == "1"


def test_pp_ssr_fp_group_item_template_triomke() -> None:
    """Template B — live triomke.com fixture. 10 floor plans; only 2
    publish rent (Bruce $1,298 + Barclay $1,502), the rest show ``—``
    for rent. Sqft is extracted for all 10 — we want every row that
    has at least one numeric dimension, since the cross-tier merger
    can fill missing rent from JSON-LD or downstream tiers."""
    html = (FIXTURES / "prospectportal_fp_group_triomke.html").read_text()
    units = parse_entrata_prospectportal_html(
        html, "https://www.triomke.com/milwaukee/trio/conventional/"
    )
    assert len(units) == 10
    names = [u["floor_plan_name"] for u in units]
    assert "Bruce" in names
    assert "Pierce" in names
    assert "Barclay" in names

    # All rows have sqft, even em-dash-rent rows
    for u in units:
        assert u["sqft"], f"missing sqft on {u['floor_plan_name']}"
        assert u["extraction_tier"] == "TIER_1_DOM_ENTRATA_PP_FPGROUP"

    # The 2 rows that publish real rent
    bruce = next(u for u in units if u["floor_plan_name"] == "Bruce")
    assert bruce["bedrooms"] == "0"  # Studio
    assert bruce["sqft"] == "540"
    assert int(bruce["market_rent_low"]) == 1298

    barclay = next(u for u in units if u["floor_plan_name"] == "Barclay")
    assert int(barclay["market_rent_low"]) == 1502
    assert barclay["sqft"] == "778"

    # Em-dash rent rows still emit sqft + availability_date
    pierce = next(u for u in units if u["floor_plan_name"] == "Pierce")
    assert not pierce["market_rent_low"]  # rent was "—"
    assert pierce["sqft"] == "633"
    # availability date from the action col
    assert "2026" in pierce["availability_date"]


def test_pp_ssr_strict_pass_units_count() -> None:
    """Strict-pass = ≥1 unit with both rent + sqft. Both fixtures
    must produce ≥1 strict-pass row."""
    for fixture, expected_strict in (
        ("prospectportal_fp_card_bellemeade.html", 4),
        ("prospectportal_fp_group_triomke.html", 2),
    ):
        html = (FIXTURES / fixture).read_text()
        units = parse_entrata_prospectportal_html(html, "https://x/y/z/conventional/")
        strict = sum(
            1 for u in units if u.get("market_rent_low") and u.get("sqft")
        )
        assert strict == expected_strict, (
            f"{fixture}: expected {expected_strict} strict-pass rows, got {strict}"
        )


def test_pp_ssr_empty_html_returns_empty() -> None:
    assert parse_entrata_prospectportal_html("", "u") == []


def test_pp_ssr_unrelated_html_returns_empty() -> None:
    """HTML with no .fp-card and no .fp-group-item must return [] — not
    fall through to a misleading partial match."""
    html = "<html><body><div class='unrelated'>nothing here</div></body></html>"
    assert parse_entrata_prospectportal_html(html, "u") == []


def test_pp_ssr_skips_rows_with_no_dimension() -> None:
    """A fp-card with no sqft and no rent (label-only) must be dropped
    by the parser's pre-validity guard so we don't ship a name-only
    plan row that then fails post_process."""
    html = """
    <html><body>
      <div class="fp-card">
        <div class="fp-title">Empty Plan</div>
        <div class="dynamic-text-before">1 Bed / 1 Bath</div>
        <span class="fee-transparency-text">—</span>
      </div>
    </body></html>
    """
    units = parse_entrata_prospectportal_html(html, "u")
    assert units == []


def test_pp_ssr_unit_item_template_c_greenwoods() -> None:
    """Template C — live greenwoodsapts.com HAR (2026-05-24). 3 floor
    plans rendered as ``.unit-item`` with packed ``.unit-bed-bath``
    text ("1 Bed, 1 Bath, 620 SqFt") and ``.unit-price``."""
    html = (FIXTURES / "prospectportal_unit_item_greenwoods.html").read_text()
    units = parse_entrata_prospectportal_html(
        html, "https://www.greenwoodsapts.com/"
    )
    # 3 distinct floor plans (1 / 2 / 3 bedroom)
    assert len(units) == 3, f"expected 3 plans, got {len(units)}: {[u.get('floor_plan_name') for u in units]}"

    names = {u["floor_plan_name"] for u in units}
    assert names == {"1 Bedroom", "2 Bedroom", "3 Bedroom"}

    # All tagged with the Template-C extraction tier
    for u in units:
        assert u["extraction_tier"] == "TIER_1_DOM_ENTRATA_PP_UNITITEM"

    one_bed = next(u for u in units if u["floor_plan_name"] == "1 Bedroom")
    assert one_bed["bedrooms"] == "1"
    assert one_bed["bathrooms"] == "1"
    assert one_bed["sqft"] == "620"
    assert int(one_bed["market_rent_low"]) == 2050

    two_bed = next(u for u in units if u["floor_plan_name"] == "2 Bedroom")
    assert two_bed["sqft"] == "896"
    assert int(two_bed["market_rent_low"]) == 2552

    # 3 Bedroom row has sqft + name but no published rent — should still
    # be admitted (sqft alone clears the validity guard for downstream
    # cross-tier rent merge)
    three_bed = next(u for u in units if u["floor_plan_name"] == "3 Bedroom")
    assert three_bed["sqft"] == "1077"


def test_pp_ssr_unit_item_nbsp_sqft_separator() -> None:
    """Real-world bug from the greenwoods HAR: the SqFt token is
    separated from the digit by an UNTERMINATED HTML entity
    (``620\\n&nbspSqFt`` — no ``;``), so BS4 leaves ``&nbsp`` as raw
    text. Parser must still find the 620."""
    html = """
    <html><body>
      <li class="unit-item">
        <span class="unit-title">A1</span>
        <div class="unit-bed-bath">1 Bed,
1 Bath,
620
&nbspSqFt</div>
        <div class="unit-price">From $1,500 per month</div>
        <div class="unit-floor-plan">2 Available</div>
      </li>
    </body></html>
    """
    units = parse_entrata_prospectportal_html(html, "u")
    assert len(units) == 1
    assert units[0]["sqft"] == "620"
    assert units[0]["bedrooms"] == "1"
    assert int(units[0]["market_rent_low"]) == 1500
    assert units[0]["available_units"] == "2"


def test_pp_ssr_unit_item_dedupes_carousel_repeats() -> None:
    """Template C often repeats the same .unit-item once per carousel
    image. Dedupe on (name, bedbath) so we don't emit phantom plans."""
    html = """
    <html><body>
      <li class="unit-item">
        <span class="unit-title">A1</span>
        <div class="unit-bed-bath">1 Bed, 1 Bath, 700 SqFt</div>
        <div class="unit-price">$1,500</div>
      </li>
      <li class="unit-item">
        <span class="unit-title">A1</span>
        <div class="unit-bed-bath">1 Bed, 1 Bath, 700 SqFt</div>
        <div class="unit-price">$1,500</div>
      </li>
      <li class="unit-item">
        <span class="unit-title">A1</span>
        <div class="unit-bed-bath">1 Bed, 1 Bath, 700 SqFt</div>
        <div class="unit-price">$1,500</div>
      </li>
    </body></html>
    """
    units = parse_entrata_prospectportal_html(html, "u")
    assert len(units) == 1, (
        f"expected dedupe to 1 plan, got {len(units)}"
    )


def test_pp_ssr_unit_item_only_one_left_phrase() -> None:
    """The 'Only One Left!' availability phrase should parse to count=1."""
    html = """
    <html><body>
      <li class="unit-item">
        <span class="unit-title">B1</span>
        <div class="unit-bed-bath">2 Bed, 2 Bath, 950 SqFt</div>
        <div class="unit-price">$2,200</div>
        <div class="unit-floor-plan">Only One Left</div>
      </li>
    </body></html>
    """
    units = parse_entrata_prospectportal_html(html, "u")
    assert len(units) == 1
    assert units[0]["available_units"] == "1"


def test_pp_ssr_fp_group_item_waitlist_status() -> None:
    """Waitlist marker in the action column flips availability_status."""
    html = """
    <html><body>
      <ul class="fp-group-list">
        <li class="fp-group-item">
          <span class="fp-name">A1</span>
          <div class="fp-col bed-bath">
            <span class="fp-col-title">Beds / Baths</span>
            <span class="fp-col-text">1 bd / 1 ba</span></div>
          <div class="fp-col sq-feet">
            <span class="fp-col-title">Sq. Ft</span>
            <span class="fp-col-text">700</span></div>
          <div class="fp-col rent">
            <span class="fp-col-title">Rent</span>
            <div class="fp-col-text fee-transparency-wrapper">
              <span class="fee-transparency-text">$1,500 per month</span></div></div>
          <div class="fp-col action">Waitlist Open</div>
        </li>
      </ul>
    </body></html>
    """
    units = parse_entrata_prospectportal_html(html, "u")
    assert len(units) == 1
    assert units[0]["availability_status"] == "UNAVAILABLE"
    assert int(units[0]["market_rent_low"]) == 1500
    assert units[0]["sqft"] == "700"


def test_pp_ssr_anchor_grid_details_marlowe_fee_transparency() -> None:
    """Template D1: Marlowe's wrapper-less ``.grid-details`` row."""
    html = """
    <html><body>
      <div class="grid-details">
        <h4 class="fp-name show-available">
          <a class="fp-name-link" href="/floorplans/a1-790000/">A1</a>
        </h4>
        <span class="available-units">10 Available</span>
        <div class="details-col bed-bath">
          <span class="title">Bed / Bath</span>
          <span class="value">1 bd / 1 ba</span>
        </div>
        <div class="details-col rent">
          <span class="title">Base Rent</span>
          <div class="value fee-transparency-wrapper">
            <span class="fee-transparency-text">
              Total Monthly Leasing Price Starting from $1,570.06
              <span class="pp-base-rent pp-base-rent-line">
                <span class="pp-base-rent-label">Base Rent:</span>
                <span class="pp-base-rent-amount">$1,455+/month</span>
              </span>
            </span>
          </div>
          <span class="lease-term-name">15mo lease</span>
        </div>
        <div class="details-col deposit"><span class="value">$300</span></div>
        <div class="details-col sq-feet"><span class="value">715</span></div>
      </div>
    </body></html>
    """

    units = parse_entrata_prospectportal_html(html, "https://marlowe.example/")

    assert len(units) == 1
    unit = units[0]
    assert unit["floor_plan_name"] == "A1"
    assert unit["bedrooms"] == "1"
    assert unit["bathrooms"] == "1"
    assert unit["sqft"] == "715"
    assert unit["market_rent_low"] == 1455
    assert unit["market_rent_high"] == 1455
    assert unit["available_units"] == "10"
    assert unit["lease_term"] == "15mo lease"
    assert unit["extraction_tier"] == "TIER_1_DOM_ENTRATA_PP_ANCHOR_GRID"


def test_pp_ssr_anchor_grid_details_avana_studio() -> None:
    """A studio row with no explicit bath still keeps its rent + area."""
    html = """
    <div class="grid-details">
      <h4><a class="fp-name-link" href="/floorplans/studio/">Studio (550 SF)</a></h4>
      <span class="available-units">5 Available</span>
      <div class="details-col bed-bath"><span class="value">Studio</span></div>
      <div class="details-col rent">
        <div class="value fee-transparency-wrapper">
          Total Monthly Leasing Price $990
          <span class="pp-base-rent-amount">$929+/month</span>
        </div>
      </div>
      <div class="details-col sq-feet"><span class="value">550</span></div>
    </div>
    """

    units = parse_entrata_prospectportal_html(html, "https://avana.example/")

    assert len(units) == 1
    assert units[0]["bedrooms"] == "0"
    assert units[0]["sqft"] == "550"
    assert units[0]["market_rent_low"] == 929
    assert units[0]["available_units"] == "5"


def test_pp_ssr_anchor_fp_row_white_furniture() -> None:
    """Template D2: table-like ``li.fp-row`` with a dated availability."""
    html = """
    <ul>
      <li class="fp-row col-7">
        <div class="fp-col name">
          <span class="fp-col-text">
            <a class="fp-name-link" href="/floorplans/s1-1126662/">S1</a>
          </span>
          <div class="mobile-availability">
            <button class="availability-link">Available Aug 05, 2026</button>
          </div>
        </div>
        <div class="fp-col bed-bath"><span class="fp-col-text">Studio</span></div>
        <div class="fp-col rent">
          <div class="fp-col-text fee-transparency-wrapper">
            <span class="fee-transparency-text">From $1,245 per month</span>
          </div>
        </div>
        <div class="fp-col sq-feet"><span class="fp-col-text">616</span></div>
      </li>
    </ul>
    """

    units = parse_entrata_prospectportal_html(
        html, "https://whitefurniture.example/"
    )

    assert len(units) == 1
    unit = units[0]
    assert unit["floor_plan_name"] == "S1"
    assert unit["bedrooms"] == "0"
    assert unit["sqft"] == "616"
    assert unit["market_rent_low"] == 1245
    assert unit["availability_date"] == "Aug 05, 2026"


def test_pp_ssr_anchor_grid_requires_price_or_area() -> None:
    """A navigation-style plan anchor with only beds is not inventory."""
    html = """
    <div class="grid-details">
      <a class="fp-name-link" href="/floorplans/a1/">A1</a>
      <div class="details-col bed-bath"><span class="value">1 bd / 1 ba</span></div>
    </div>
    """
    assert parse_entrata_prospectportal_html(html, "u") == []
