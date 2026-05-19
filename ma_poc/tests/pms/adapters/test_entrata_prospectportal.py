"""Entrata ProspectPortal check_availability — parser + detector wiring.

Acceptance (2026-05-18, real DevTools-captured view_unit_spaces body
from springriver.prospectportal.com, floorplan A1 / fp 712595):
- one unit-level row per <a class="unit-button" data-*>; data-* attrs
  authoritative (data-unit, data-rent, data-bedroom/-bathroom,
  data-unitavailabilitydate); unit# from sibling .unit-col.unit.
- detector routes .prospectportal.com HTML -> pms="entrata".
"""
from __future__ import annotations

import ma_poc.pms.adapters  # noqa: F401  # populate adapter registry
from ma_poc.pms.adapters.entrata import (
    _pp_iso,
    parse_prospectportal_unit_spaces,
)
from ma_poc.pms.detector import detect_pms

# Faithful trim of the real capture (2 of 3 units; structure verbatim).
_PP_HTML = """
<h6 class="availability-fp-name">A1</h6>
<ul class="fp-stats-list">
 <li class="fp-stats-item modal-sq-feet"><span class="stat-title">Sq Ft</span>
   <span class="stat-value">642</span></li>
</ul>
<div class="check-availability-table">
 <div class="unit-row js-unit-row"><div class="unit-row-wrapper">
   <div class="unit-col select">
     <a id="unit_space_4516893" class="unit-button radio-default" href="#"
        rel="4516893" data-floorplan="712595" data-unit="4516893"
        data-bedroom="1" data-bathroom="1" data-rent="1291.00"
        data-deposit="0" data-min-advertised-base-rent="1291"
        data-unitavailabilitydate="2026/05/17"><label>Select</label></a>
   </div>
   <div class="unit-col unit"><span class="unit-col-title">Unit</span>
     <span class="unit-col-text">1306</span></div>
   <div class="unit-col rent"><span class="unit-col-text fee-transparency-text">
     $1,291<span class="rent-frequency">/month</span></span></div>
   <div class="unit-col sqft"><span class="unit-col-text">642</span></div>
   <div id="available_4516893" class="unit-col availability">
     <span class="unit-col-text">Now</span></div>
 </div></div>
 <div class="unit-row js-unit-row"><div class="unit-row-wrapper">
   <div class="unit-col select">
     <a id="unit_space_4516909" class="unit-button radio-default" href="#"
        rel="4516909" data-floorplan="712595" data-unit="4516909"
        data-bedroom="1" data-bathroom="1" data-rent="1291.00"
        data-deposit="0" data-min-advertised-base-rent="1291"
        data-unitavailabilitydate="2026/05/17"><label>Select</label></a>
   </div>
   <div class="unit-col unit"><span class="unit-col-text">1406</span></div>
   <div class="unit-col sqft"><span class="unit-col-text">642</span></div>
 </div></div>
</div>
"""


def test_pp_iso() -> None:
    assert _pp_iso("2026/05/17") == "2026-05-17"
    assert _pp_iso("2026-5-7") == "2026-05-07"
    assert _pp_iso("") == ""
    assert _pp_iso("nope") == ""


def test_parse_prospectportal_unit_spaces() -> None:
    units = parse_prospectportal_unit_spaces(_PP_HTML, "https://x.prospectportal.com/?q")
    assert len(units) == 2
    u0 = units[0]
    assert u0["unit_number"] == "1306"
    assert u0["market_rent_low"] == 1291
    assert u0["market_rent_high"] == 1291
    assert u0["floor_plan_name"] == "A1"
    assert u0["sqft"] == "642"
    assert u0["availability_date"] == "2026-05-17"
    assert u0["availability_status"] == "AVAILABLE"
    assert u0["extraction_tier"] == "TIER_1_DOM_ENTRATA_PROSPECTPORTAL"
    assert units[1]["unit_number"] == "1406"
    assert units[1]["market_rent_low"] == 1291


def test_parse_empty_and_non_pp() -> None:
    assert parse_prospectportal_unit_spaces("", "x") == []
    assert parse_prospectportal_unit_spaces("<div>no units here</div>", "x") == []


def test_detector_routes_prospectportal_to_entrata() -> None:
    html = (
        '<html><body><a href="https://springriver.prospectportal.com/'
        'Apartments/module/application_authentication/">Apply</a></body></html>'
    )
    d = detect_pms("https://www.springriverapartments.com/", page_html=html)
    assert d.pms == "entrata", d.pms
