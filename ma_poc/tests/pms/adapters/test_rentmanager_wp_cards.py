"""RentManager WordPress-plugin availability cards (prod 2026-07-12 cohort).

The KRC/WP RentManager theme renders each unit as
``<a class="individual-item" data-bed data-rent data-date>`` inside
``.rm-ua-container`` — not the ``tr.unit_avail_container`` table the
iLoveLeasing parser targets. So finelivingapts-class properties parsed to 0
units and fell to the LLM tier. Fixture markup is captured verbatim from
finelivingapts.com/aquila-park-availability/ (19 real SSR cards live).
"""

from __future__ import annotations

from ma_poc.pms.adapters.rentmanager import parse_rentmanager_wp_cards

# Two cards in the exact live shape (attrs + h2/span + .availableDate).
_WP_CARDS = """
<div class="rm-ua-container">
  <a class="individual-item" data-bed="0" data-date="2026/08" data-rent="1030.00"
     href="/aquila-floor-plan-detail/?propid=244&amp;uid=7326">
    <div class="availableDate">Available Date: <br/>8/1/2026</div>
    <div class="detail-content">
      <h2>The Pearl <span>8150 W 30 1/2 St, #308</span></h2>
      <div class="unit-specs">Beds 0 Bath 1.0 Rent $1,030.00</div>
    </div>
  </a>
  <a class="individual-item" data-bed="2" data-date="2026/06" data-rent="1445.00"
     href="/aquila-floor-plan-detail/?propid=244&amp;uid=7393">
    <div class="availableDate">Available Date: <br/>6/1/2026</div>
    <div class="detail-content">
      <h2>The Emerald <span>8150 W 30 1/2 St, #209</span></h2>
      <div class="unit-specs">Beds 2 Bath 1.5 Rent $1,445.00</div>
    </div>
  </a>
</div>
"""


def test_wp_cards_parse_full_unit_records() -> None:
    units = parse_rentmanager_wp_cards(_WP_CARDS, "https://x.com/availability")
    assert len(units) == 2
    a, b = units
    assert a["unit_number"] == "308"
    assert a["market_rent_low"] == 1030
    assert a["bedrooms"] == "0"
    assert a["bathrooms"] == "1.0"
    assert a["floor_plan_name"] == "The Pearl"
    assert a["available_date"] == "2026-08-01"
    assert a["source_ids"]["rentmanager_uid"] == "7326"
    assert a["extraction_tier"] == "TIER_1_DOM_RENTMANAGER_WP_CARDS"
    assert b["unit_number"] == "209"
    assert b["market_rent_low"] == 1445
    assert b["bedrooms"] == "2"
    assert b["floor_plan_name"] == "The Emerald"


def test_data_date_fallback_when_no_availabledate_div() -> None:
    """data-date=YYYY/MM → first-of-month when the .availableDate div is absent."""
    html = (
        '<a class="individual-item" data-bed="1" data-date="2027/03" '
        'data-rent="1600" href="/d/?uid=99"><h2>A1 <span>#5</span></h2></a>'
    )
    units = parse_rentmanager_wp_cards(html, "x")
    assert len(units) == 1
    assert units[0]["available_date"] == "2027-03-01"
    assert units[0]["unit_number"] == "5"


def test_absent_marker_returns_empty() -> None:
    assert parse_rentmanager_wp_cards("<html>no cards</html>", "x") == []


def test_card_without_unit_id_is_skipped() -> None:
    """No #unit in span AND no uid in href → unidentifiable, skip."""
    html = '<a class="individual-item" data-rent="1200"><h2>Studio</h2></a>'
    assert parse_rentmanager_wp_cards(html, "x") == []


def test_malformed_html_does_not_raise() -> None:
    bad = '<a class="individual-item" data-rent="1200"><h2>broken'
    # must not raise; returns whatever it can (0 units — no unit id)
    assert parse_rentmanager_wp_cards(bad, "x") == []
