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
    find_repli360_script_url,
    parse_repli360_str,
)
from ma_poc.pms.detector import _detect_html_markers


def test_find_script_url_from_static_html() -> None:
    # The embed-script URL is in STATIC HTML (render-independent entry).
    tok = "eyJpdiI6Inp2M25HZlJLTVduVHE4a1cxQjFVYXc9PSJ9"
    html = (
        '<html><body><a href="https://repli360.com">logo</a>'
        f'<script src="https://app.repli360.com/admin/rrac-website-script/{tok}">'
        "</script></body></html>"
    )
    url = find_repli360_script_url(html)
    assert url == f"https://app.repli360.com/admin/rrac-website-script/{tok}"


def test_find_script_url_absent() -> None:
    assert find_repli360_script_url("<html>no repli embed here</html>") == ""
    # A bare logo link is NOT the embed script (the false-positive trap).
    assert find_repli360_script_url('<a href="https://repli360.com">x</a>') == ""


def test_template_render_html_parses_via_onclick_reuse() -> None:
    # fetch_repli360_floorplans feeds the template-render HTML through
    # find_repli360_floorplans; verify that reuse parses the bootstrap
    # widget's onclick attrs (same shape the live API returns).
    tpl = (
        "<div>"
        "<a onclick=\"getUnitListByFloor(this,'A1AL' , 2 , 1619,``);\">x</a>"
        "<a onclick=\"getUnitListByFloor(this,'B2CL', 2, 1619, '');\">y</a>"
        "</div>"
    )
    sid, fps = find_repli360_floorplans(tpl)
    assert sid == "1619"
    assert fps == [("A1AL", "2"), ("B2CL", "2")]

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


# ─── 2026-05-21 HAR-validation regression ────────────────────────────


def test_detector_repli360_beats_co_resident_funnel_chat_widget() -> None:
    """HAR-validation finding: thebelmontbyreside.com and liveattrailpoint.com
    have both Repli360 markers (app.repli360.com / getUnitListByFloor /
    rrac-website-script) AND a co-resident Funnel/Nestio chat widget.
    Both detector signals fire at 0.90 originally; tie was broken by
    first-yielded → Funnel won → FunnelAdapter has no extraction path
    for these sites → fall to LLM.

    Fix: Repli360 yields at 0.92 when ``app.repli360.com`` host marker
    is present (the chat widget alone wouldn't add this host). Higher
    confidence outranks Funnel and routes to RepliAdapter where
    extraction works.

    This test pins the precedence: a page with BOTH markers must route
    to repli360, not funnel.
    """
    html = """
    <html><body>
      <!-- Funnel/Nestio chat widget co-resident -->
      <script src="https://funnelleasing.com/chat-widget.js"></script>
      <div data-nestio-component="chat">x</div>
      <!-- Repli360 actual extraction surface -->
      <script src="https://app.repli360.com/public/admin/rrac-website-script/abc"></script>
      <a onclick="getUnitListByFloor(this,'A1','2','1619')">View Details</a>
    </body></html>
    """
    res = _detect_html_markers(html.lower())
    assert res is not None and res[0] == "repli360", (
        f"Expected repli360 to win over co-resident funnel chat widget; got {res}"
    )


def test_detector_repli360_strong_marker_alone_still_routes() -> None:
    """Sanity: ``app.repli360.com`` alone (without co-resident markers)
    still routes to repli360 (just at 0.92 now)."""
    res = _detect_html_markers(
        '<script src="https://app.repli360.com/widget.js"></script>'
    )
    assert res is not None and res[0] == "repli360"


def test_detector_repli360_weak_marker_only_still_routes() -> None:
    """Sanity: ``getUnitListByFloor`` JS call alone (no app.repli360.com
    host) still routes to repli360 at the original 0.90 confidence."""
    res = _detect_html_markers(
        '<a onclick="getUnitListByFloor(this,\'A1\',2,1619)">x</a>'
    )
    assert res is not None and res[0] == "repli360"
