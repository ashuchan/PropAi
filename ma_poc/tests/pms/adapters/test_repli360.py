"""Repli360 (rrac) adapter — parser + detector wiring tests.

Acceptance (2026-05-17, Chrome-MCP + curl verified on royce):
- JS-rendered ``getUnitListByFloor(this,'<fp>',<tt>,<sid>)`` onclick
  attrs → (site_id, [(floorPlanID, template_type), ...]).
- getUnitListByFloor ``str`` HTML ``<tr class="unitlisting ...">`` →
  one unit-level row: real unit number, building, rent from
  ``span.unit_price_value``, ISO ``data-available_date``.
- Detector routes the repli360/rrac HTML marker → pms="repli360".
- Adapter registered and degrades gracefully when the page was not
  rendered (onclick attrs absent).
"""
from __future__ import annotations

import ma_poc.pms.adapters  # noqa: F401  # populate adapter registry
from ma_poc.pms.adapters.registry import get_adapter
from ma_poc.pms.adapters.repli360 import (
    Repli360Adapter,
    _movein_today,
    find_repli360_floorplans,
    parse_repli360_str,
)
from ma_poc.pms.detector import _detect_html_markers

# Exact markup captured live from royceattrumbull.com getUnitListByFloor.
_STR_HTML = """
<div class="rrac_listAvailableUnit">
<table class="table" id="fp_table1">
<tr><th>Building Number</th><th>Unit Number</th><th class="unitTouring">Tour</th>
<th>Deposits Starting At</th><th>Starting At</th><th>Availability</th>
<th>Lease Now</th></tr>
<tr class="unitlisting 4114 lease_term_wrap_4114" data-count="0"
 data-available_date="2026-05-17">
<td><span class="mobile_rrac">Building Number</span>4</td>
<td><span class="mobile_rrac">Unit Number</span><b class="unitNumber">4114</b></td>
<td class="unitTouring"><span class="mobile_rrac">Tour</span></td>
<td><span class="mobile_rrac">Deposit</span>$1,000</td>
<td class="rrac_unit_price"><span class="mobile_rrac">Starting At</span>
<span class="unit_price_value unit-rrac-price">$2,335</span></td>
<td><span class="mobile_rrac">Availability</span>Available Now</td>
<td><a href="https://idolben.mriprospectconnect.com/x?BuildingID=4&ApartmentID=4114">
Lease Now</a></td></tr>
<tr class="unitlisting 6203 lease_term_wrap_6203" data-count="1"
 data-available_date="2026-06-01">
<td><span class="mobile_rrac">Building Number</span>6</td>
<td><span class="mobile_rrac">Unit Number</span><b class="unitNumber">6203</b></td>
<td class="unitTouring"></td>
<td><span class="mobile_rrac">Deposit</span>$1,000</td>
<td class="rrac_unit_price"><span class="unit_price_value">$2,410</span></td>
<td><span class="mobile_rrac">Availability</span>Jun 1</td>
<td></td></tr>
</table></div>
"""

_ONCLICK_HTML = """
<div id="all_available_tab" class="rrac-tab-container active">
<a class="btn" onclick="getUnitListByFloor(this,'A1AL' , 2 , 1619,``);">
View Details</a>
<a class="btn" onclick="getUnitListByFloor(this,'B2CL', 2, 1619, '');">
View Details</a>
<a class="btn" onclick="getUnitListByFloor(this,'A1AL',2,1619);">View Details</a>
</div>
"""


def test_find_floorplans_parses_onclick() -> None:
    site_id, fps = find_repli360_floorplans(_ONCLICK_HTML)
    assert site_id == "1619"
    # dedup A1AL; preserve order; capture template_type
    assert fps == [("A1AL", "2"), ("B2CL", "2")]


def test_find_floorplans_empty_when_not_rendered() -> None:
    # Static HTML (no JS-injected onclick) → empty, caller degrades.
    site_id, fps = find_repli360_floorplans(
        '<html><a href="https://repli360.com">logo</a></html>'
    )
    assert site_id == ""
    assert fps == []


def test_parse_str_html_unit_level() -> None:
    units = parse_repli360_str(_STR_HTML, "https://app.repli360.com/x")
    assert len(units) == 2
    u0 = units[0]
    assert u0["unit_number"] == "4114"
    assert u0["building"] == "4"
    assert u0["market_rent_low"] == 2335
    assert u0["market_rent_high"] == 2335
    assert u0["availability_date"] == "2026-05-17"
    assert u0["availability_status"] == "AVAILABLE"
    assert units[1]["unit_number"] == "6203"
    assert units[1]["market_rent_low"] == 2410


def test_parse_str_html_empty_and_malformed() -> None:
    assert parse_repli360_str("", "x") == []
    assert parse_repli360_str("<html><body>no units</body></html>", "x") == []


def test_movein_today_format() -> None:
    # "%-d %b %Y" e.g. "17 May 2026" — no zero-pad, portable build.
    s = _movein_today()
    parts = s.split()
    assert len(parts) == 3
    assert parts[0].isdigit() and not parts[0].startswith("0")
    assert len(parts[1]) == 3  # month abbrev
    assert parts[2].isdigit() and len(parts[2]) == 4


def test_detector_routes_repli360_marker() -> None:
    for marker in (
        '<script src="https://app.repli360.com/widget.js"></script>',
        '<a onclick="getUnitListByFloor(this,\'A1AL\',2,1619)">x</a>',
        '<div class="rrac_listAvailableUnit"></div>',
    ):
        res = _detect_html_markers(marker)
        assert res is not None and res[0] == "repli360", marker


def test_adapter_registered() -> None:
    a = get_adapter("repli360")
    assert isinstance(a, Repli360Adapter)
    assert a.pms_name == "repli360"
    assert "app.repli360.com" in a.static_fingerprints()


def test_matches_response_body() -> None:
    a = Repli360Adapter()
    assert a.matches_response_body("...getUnitListByFloor(this,...")
    assert a.matches_response_body("x app.repli360.com x")
    assert not a.matches_response_body("unrelated html")
    assert not a.matches_response_body({"not": "a string"})
