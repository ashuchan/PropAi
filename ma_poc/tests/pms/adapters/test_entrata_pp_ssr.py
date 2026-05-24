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
