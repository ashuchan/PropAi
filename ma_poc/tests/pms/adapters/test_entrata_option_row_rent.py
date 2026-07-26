"""Entrata ProspectPortal ``.option-row`` rent extraction (2026-07-25).

Markup cut verbatim from a live fetch of the Grand Oaks 2X2A plan-detail
page (grandoakscommunity.com, HTTP 200, 186KB, static — no JS, no XHR).

THE BUG. The rent cell was selected with ONE comma-separated selector:

    row.select_one(".detail.second .unit-rent, "
                   ".detail.second .fee-transparency-text, "
                   ".detail.second")

``select_one`` with a comma list returns the first match in DOCUMENT ORDER,
not in selector order. ProspectPortal renders the Building cell as a bare
``.detail.second`` BEFORE the rent cell, so the broad third alternative won
every time: rent_text came back "Building D", the money parser returned
(None, None), and every unit shipped with a NULL RENT while still carrying a
real unit_number.

That failure is quiet in the worst way — the property still looks like a
successful unit-level extraction (real apartment numbers, sqft, dates,
backend ids) and only the rent, the one field the whole pipeline exists to
collect, is missing.

Live cross-check: an independent browser probe of the same page on
2026-07-25 read D103 $1,939 / F302 $2,049 / G202 $1,939 at 961 sq.ft.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from ma_poc.pms.adapters.entrata import _parse_pp_option_rows


def _unit_row(unit: str, building: str, rent: str, uid: str, avail: str) -> str:
    """One ``.option-row`` in ProspectPortal's real shape.

    The ordering matters and is the point of the test: ``.detail.second`` for
    Building comes BEFORE ``.detail.second.unit-rent-cell`` for Rent.
    """
    return f"""
<div class="option-row">
  <div class="detail first"><span class="mobile-text">Unit</span> {unit} </div>
  <div class="detail second"><span class="mobile-text">Building </span> {building} </div>
  <div class="detail second unit-rent-cell">
    <div class="fee-transparency-wrapper">
      <span class="stat-value unit-rent fee-transparency-text">
        <span class="mobile-text">Rent</span>
        <span class="small-text">Starting from</span>
        ${rent}
        <span class="rent-frequency">/month</span>
      </span>
      <button class="calculate-btn"
        data-url="https://x.test/?module=check_availability&amp;action=view_rent_calculator&amp;unit_space[id]={uid}&amp;property_floorplan[id]=564677"></button>
    </div>
    <span class="lease-term-wrapper"><span class="lease-term-name">13mo lease</span></span>
  </div>
  <div class="detail block"><span class="mobile-text">Sq.ft.</span> 961 </div>
  <div class="detail block"><span class="mobile-text">Available</span> {avail} </div>
  <div class="detail action">
    <button class="btn outline js-show-details" data-floorplan="564677"
            data-unit="{uid}" data-date="07/25/2026">Details</button>
  </div>
</div>"""


PLAN_PAGE = (
    '<html><body><div class="fp-details-container"><h1>2X2A</h1>'
    "<p>2 Bed / 2 Bath</p></div>"
    '<div class="sub-section fp-units-table" id="available-units">'
    '<div class="option-row title"><div class="detail">Unit</div>'
    '<div class="detail">Building</div><div class="detail">Rent</div></div>'
    + _unit_row("D103", "D", "1,939", "4176948", "Available Now")
    + _unit_row("F302", "F", "2,049", "4176974", "Available Now")
    + _unit_row("G202", "G", "1,939", "4176979", "Jul 31, 2026")
    + "</div></body></html>"
)


def _parse() -> list[dict]:
    soup = BeautifulSoup(PLAN_PAGE, "lxml")
    return _parse_pp_option_rows(soup, "https://x.test/plan", "2X2A", "564677")


# ── The regression itself ───────────────────────────────────────────────────


def test_every_unit_carries_its_rent() -> None:
    """THE regression. Before the fix all three parsed with rent None."""
    rows = _parse()
    assert len(rows) == 3
    rents = {r["unit_number"]: r["market_rent_low"] for r in rows}
    assert rents == {"D103": 1939, "F302": 2049, "G202": 1939}
    assert all(r["market_rent_high"] is not None for r in rows)


def test_building_cell_is_not_mistaken_for_rent() -> None:
    """The precise failure mode: the Building cell precedes the rent cell in
    document order and used to win the comma-selector."""
    for r in _parse():
        assert r["market_rent_low"] is not None, (
            f"unit {r['unit_number']} lost its rent — the broad .detail.second "
            "selector matched the Building cell again"
        )


def test_rent_differs_per_unit_not_collapsed_to_the_plan() -> None:
    """F302 is $110 more than its two 2X2A siblings. A plan-level fallback
    would flatten all three to one number and hide real rent dispersion."""
    rows = {r["unit_number"]: r["market_rent_low"] for r in _parse()}
    assert rows["F302"] != rows["D103"]


# ── The rest of the row must not regress ────────────────────────────────────


def test_unit_number_building_sqft_and_backend_id_survive() -> None:
    rows = {r["unit_number"]: r for r in _parse()}
    assert set(rows) == {"D103", "F302", "G202"}
    d103 = rows["D103"]
    assert d103["sqft"] == "961"
    assert d103["floor_plan_name"] == "2X2A"
    # A real per-unit backend id — this is what keeps the row off a synthetic
    # inferred_* identity downstream.
    assert d103["source_ids"]["entrata_uid"] == "4176948"


def test_mobile_label_prefix_is_stripped_from_unit_number() -> None:
    """PP injects a "Unit" mobile label inside the same cell."""
    assert all(not r["unit_number"].lower().startswith("unit") for r in _parse())


def test_header_row_is_not_emitted_as_a_unit() -> None:
    """``.option-row.title`` carries the column headings, not an apartment."""
    assert "Building" not in {r["unit_number"] for r in _parse()}


def test_template_with_rent_directly_in_detail_second_still_parses() -> None:
    """Fallback path: some PP templates put the rent in a bare
    ``.detail.second`` with no ``.unit-rent`` wrapper. Trying selectors in
    priority order must not break those."""
    html = (
        '<html><body><div class="fp-details-container"><h1>A1</h1></div>'
        '<div class="fp-units-table">'
        '<div class="option-row">'
        '<div class="detail first">101</div>'
        '<div class="detail second">$1,500</div>'
        '<div class="detail block">700</div>'
        "</div></div></body></html>"
    )
    rows = _parse_pp_option_rows(BeautifulSoup(html, "lxml"), "u", "A1", "1")
    assert len(rows) == 1
    assert rows[0]["market_rent_low"] == 1500


# ── Reachability: the drill gate must hand this template to the parser ──────


def test_drill_gate_admits_the_option_row_template() -> None:
    """The parser above was correct but UNREACHABLE from the per-plan drill.

    The drill gated on ``"unit-card" in plan_html`` — template U1 only — and
    ``continue``d past every U2 page. parse_entrata_pp_unit_cards already
    falls back to _parse_pp_option_rows for exactly this shape, so the branch
    existed and was simply never given the page.

    Live counts for grandoakscommunity.com plan 2X2A:
        unit-card      0     <- old gate rejected the page on this alone
        option-row     4
        fp-units-table 1
    """
    from ma_poc.pms.adapters.entrata import _pp_plan_page_has_units

    assert _pp_plan_page_has_units(PLAN_PAGE) is True
    assert "unit-card" not in PLAN_PAGE, (
        "fixture must reproduce the U2 shape — no .unit-card anywhere"
    )


def test_drill_gate_still_admits_the_unit_card_template() -> None:
    from ma_poc.pms.adapters.entrata import _pp_plan_page_has_units

    assert _pp_plan_page_has_units('<div class="unit-card">…</div>') is True


def test_drill_gate_rejects_pages_with_no_roster() -> None:
    """A plan page with no unit markup must still be skipped — admitting it
    would spend a parse on every marketing page in the fan-out."""
    from ma_poc.pms.adapters.entrata import _pp_plan_page_has_units

    assert _pp_plan_page_has_units("") is False
    assert _pp_plan_page_has_units("<html><h1>Floor Plans</h1></html>") is False


def test_gate_and_parser_agree_end_to_end() -> None:
    """Whatever the gate admits, the drill's own entry point must parse —
    the two drifting apart is what made the U2 branch dead code."""
    from ma_poc.pms.adapters.entrata import (
        _pp_plan_page_has_units,
        parse_entrata_pp_unit_cards,
    )

    assert _pp_plan_page_has_units(PLAN_PAGE)
    rows = parse_entrata_pp_unit_cards(PLAN_PAGE, "https://x.test/plan")
    assert len(rows) == 3
    assert all(r["market_rent_low"] for r in rows), "gate admitted it; rents must parse"


def test_gate_does_not_match_jonah_digital_unit_cards() -> None:
    """`unit-card` as a bare SUBSTRING also matches Jonah Digital's
    ``jd-fp-unit-card``, which would hand a foreign vendor's page to the
    Entrata parser for a guaranteed-empty parse.

    Live: livethewatts.com/floorplans/ returns 235KB containing 46
    ``jd-fp-unit-card`` markers and ZERO real Entrata elements
    (soup.select('.unit-card') == 0). The property is Jonah Digital, not
    Entrata.
    """
    from ma_poc.pms.adapters.entrata import _pp_plan_page_has_units

    jonah = (
        '<div data-jd-fp-selector="map-embed-unit-list" class="jd-fp-unit-card-container">'
        '<div class="jd-fp-unit-card jd-fp-unit-card--preload">…</div></div>'
    )
    assert _pp_plan_page_has_units(jonah) is False
    # …while a genuine Entrata card still admits.
    assert _pp_plan_page_has_units('<div class="unit-card container-shape">…</div>') is True
