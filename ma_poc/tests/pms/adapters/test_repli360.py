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
    merge_repli360_plan_meta,
    parse_repli360_plan_meta,
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


def test_find_script_url_public_path_variant() -> None:
    """2026-05-22 bucket-B grind: the live embed-script URL carries a
    ``/public/`` path segment — app.repli360.com/public/admin/rrac-website-
    script/<token>. The regex must match this current variant; without it
    the adapter exited REPLI360_NO_FLOORPLANS on every repli360 property
    (verified live on marquisonevans.com — site_id 1649, 9 floorplans)."""
    tok = "eyJpdiI6Iml4T1Ric2JlT3hBNmhQb3loVXFKQVE9PSJ9"
    html = (
        "<html><body>"
        f'<script src="https://app.repli360.com/public/admin/'
        f'rrac-website-script/{tok}"></script></body></html>'
    )
    url = find_repli360_script_url(html)
    assert url == (
        f"https://app.repli360.com/public/admin/rrac-website-script/{tok}"
    )


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


# ─── 2026-05-22 PLAN_LEVEL→unit plan-meta merge (Repli360 PARTIAL fix) ─

# Captured from the marquisonevans.com (site_id 1649) template-render
# response: per-plan card layout is <h2>plan-name</h2> immediately
# followed by "<beds-word/digit> Bedroom[s] | N Bath[s] | <span>SQFT</
# span> sq.ft." and then the per-plan View Details anchor whose onclick
# carries the floorPlanID. The plan-meta parser keys on the onclick and
# walks BACKWARDS to pick up the nearest plan card.
_TPL_RENDER_HTML = """
<div class="plan-list">
  <div class="card">
    <h2>1A</h2>
    <p>One Bedroom | 1 Bath | <span>670</span> sq.ft. | <span>14</span> Units Available</p>
    <a class="btn" onclick="getUnitListByFloor(this,'4832490', 2, 1649, '');">View Details</a>
  </div>
  <div class="card">
    <h2>2B</h2>
    <p>Two Bedrooms | 2 Baths | <span>1100</span> sq.ft. | <span>3</span> Units Available</p>
    <a class="btn" onclick="getUnitListByFloor(this,'4832491', 2, 1649, '');">View Details</a>
  </div>
  <div class="card">
    <h2>S1</h2>
    <p>Studio | 1 Bath | <span>520</span> sq.ft. | <span>1</span> Unit Available</p>
    <a class="btn" onclick="getUnitListByFloor(this,'4832492', 2, 1649, '');">View Details</a>
  </div>
  <div class="card">
    <h2>3PH</h2>
    <p>3 Bedrooms | 2.5 Baths | <span>1600</span> sq.ft.</p>
    <a class="btn" onclick="getUnitListByFloor(this,'4832493', 2, 1649, '');">View Details</a>
  </div>
</div>
"""


def test_parse_plan_meta_extracts_name_beds_baths_sqft() -> None:
    """The four plan-card patterns we see in marquisonevans/royce/etc.:
    "One Bedroom | 1 Bath" (word beds), "Two Bedrooms | 2 Baths" (word
    plural), "Studio | 1 Bath" (no Bedroom word), "3 Bedrooms | 2.5
    Baths" (digit beds + fractional baths)."""
    meta = parse_repli360_plan_meta(_TPL_RENDER_HTML)
    assert set(meta.keys()) == {"4832490", "4832491", "4832492", "4832493"}
    assert meta["4832490"] == {
        "floor_plan_name": "1A",
        "bedrooms": "1",
        "bathrooms": "1",
        "sqft": "670",
    }
    assert meta["4832491"] == {
        "floor_plan_name": "2B",
        "bedrooms": "2",
        "bathrooms": "2",
        "sqft": "1100",
    }
    # Studio → bedrooms "0", no "Bedroom" word in source.
    assert meta["4832492"] == {
        "floor_plan_name": "S1",
        "bedrooms": "0",
        "bathrooms": "1",
        "sqft": "520",
    }
    # Digit-prefix beds and fractional baths.
    assert meta["4832493"] == {
        "floor_plan_name": "3PH",
        "bedrooms": "3",
        "bathrooms": "2.5",
        "sqft": "1600",
    }


def test_parse_plan_meta_empty_and_no_onclick() -> None:
    assert parse_repli360_plan_meta("") == {}
    assert parse_repli360_plan_meta("<html><body>no onclick here</body></html>") == {}


def test_parse_plan_meta_dedup_repeated_onclick() -> None:
    """A floorPlanID can appear in multiple tabs (All Available, Filter
    by Beds, etc.); first card wins so we don't accidentally pick up a
    later, less-specific occurrence."""
    html = """
    <div><h2>1A</h2>
      <p>One Bedroom | 1 Bath | <span>670</span> sq.ft.</p>
      <a onclick="getUnitListByFloor(this,'4832490',2,1649)">x</a>
    </div>
    <div><h2>1A-duplicate</h2>
      <p>Two Bedrooms | 2 Baths | <span>9999</span> sq.ft.</p>
      <a onclick="getUnitListByFloor(this,'4832490',2,1649)">x</a>
    </div>
    """
    meta = parse_repli360_plan_meta(html)
    assert meta == {
        "4832490": {
            "floor_plan_name": "1A",
            "bedrooms": "1",
            "bathrooms": "1",
            "sqft": "670",
        }
    }


def test_parse_plan_meta_onclick_with_missing_meta_yields_empty_dict() -> None:
    """An onclick whose preceding window has none of the expected meta
    markup should still appear in the map (with an empty value) — the
    adapter's .get(fpid, {}) pattern then merges nothing for that plan
    rather than skipping the floorplan entirely."""
    html = '<a onclick="getUnitListByFloor(this,\'X1\',2,1649)">x</a>'
    meta = parse_repli360_plan_meta(html)
    assert meta == {"X1": {}}


def test_parse_plan_meta_picks_nearest_plan_card() -> None:
    """When two plan cards exist and the second onclick must pick up the
    second card (not the first). Verifies the backward-window join is
    correctly bounded to the nearest preceding card."""
    html = """
    <h2>1A</h2><p>One Bedroom | 1 Bath | <span>670</span> sq.ft.</p>
    <a onclick="getUnitListByFloor(this,'FP1',2,1649)">x</a>
    <h2>2B</h2><p>Two Bedrooms | 2 Baths | <span>1100</span> sq.ft.</p>
    <a onclick="getUnitListByFloor(this,'FP2',2,1649)">x</a>
    """
    meta = parse_repli360_plan_meta(html)
    assert meta["FP1"]["floor_plan_name"] == "1A"
    assert meta["FP1"]["sqft"] == "670"
    assert meta["FP2"]["floor_plan_name"] == "2B"
    assert meta["FP2"]["sqft"] == "1100"


def test_merge_plan_meta_fills_missing_only() -> None:
    """The merge MUST fill empty plan-level fields and MUST NOT overwrite
    per-unit values that came back from getUnitListByFloor (rent and
    unit_number are the only per-unit-authoritative fields today, but
    other adapters in the future may populate beds/baths per unit; the
    helper has to be defensive)."""
    units = [
        {
            "unit_number": "4114",
            "rent_range": "$2335",
            "sqft": "",
            "bedrooms": "",
            "bathrooms": "",
            "floor_plan_name": "",
        },
        {
            "unit_number": "4115",
            "rent_range": "$2400",
            "sqft": "999",  # already set — must not be overwritten
            "bedrooms": "1",
            "bathrooms": "1",
            "floor_plan_name": "OverrideMe",
        },
    ]
    meta = {
        "floor_plan_name": "1A",
        "sqft": "670",
        "bedrooms": "1",
        "bathrooms": "1",
    }
    merge_repli360_plan_meta(units, meta)
    assert units[0]["sqft"] == "670"
    assert units[0]["bedrooms"] == "1"
    assert units[0]["floor_plan_name"] == "1A"
    # Per-unit value preserved on unit #2.
    assert units[1]["sqft"] == "999"
    assert units[1]["floor_plan_name"] == "OverrideMe"


def test_merge_plan_meta_no_op_when_meta_empty() -> None:
    units = [{"unit_number": "4114", "sqft": ""}]
    merge_repli360_plan_meta(units, {})
    assert units == [{"unit_number": "4114", "sqft": ""}]


def test_end_to_end_plan_meta_lifts_units_to_rent_plus_sqft() -> None:
    """The success-bar test: this mirrors the per-floorplan loop in
    ``Repli360Adapter.extract`` — parse template-render plan_meta once,
    parse each floorplan's getUnitListByFloor str into units, merge
    meta in. The resulting units must have BOTH rent (from str HTML)
    AND sqft (from plan meta) — that combination is the Surgex success
    criterion that lifts repli360 out of PARTIAL."""
    # 1. Plan-level metadata from /admin/template-render.
    tpl_html = (
        "<h2>1A</h2><p>One Bedroom | 1 Bath | <span>670</span> sq.ft.</p>"
        "<a onclick=\"getUnitListByFloor(this,'4832490',2,1649)\">x</a>"
    )
    plan_meta = parse_repli360_plan_meta(tpl_html)
    assert "4832490" in plan_meta

    # 2. Per-unit rows from a getUnitListByFloor response (the existing
    # _STR_HTML fixture — 2 units, both have rent, NEITHER has sqft).
    units = parse_repli360_str(_STR_HTML, "https://app.repli360.com/x")
    assert all(not u.get("sqft") for u in units)
    assert all(u.get("market_rent_low") for u in units)

    # 3. Apply the meta merge as the adapter does.
    merge_repli360_plan_meta(units, plan_meta["4832490"])

    # 4. Verify the success bar: ≥1 unit with rent AND sqft AND beds.
    rent_and_sqft = [u for u in units if u.get("market_rent_low") and u.get("sqft")]
    assert len(rent_and_sqft) == 2, (
        "Every unit must gain sqft from the plan meta merge"
    )
    assert rent_and_sqft[0]["sqft"] == "670"
    assert rent_and_sqft[0]["bedrooms"] == "1"
    assert rent_and_sqft[0]["bathrooms"] == "1"
    assert rent_and_sqft[0]["floor_plan_name"] == "1A"


def test_parse_plan_meta_word_to_digit_map_complete() -> None:
    """Every word in the One..Six map should resolve correctly. Anything
    above Six (rare for residential) falls through to the literal."""
    cases = [
        ("One Bedroom", "1"),
        ("Two Bedrooms", "2"),
        ("Three Bedrooms", "3"),
        ("Four Bedrooms", "4"),
        ("Five Bedrooms", "5"),
        ("Six Bedrooms", "6"),
    ]
    for phrase, expected in cases:
        html = (
            f"<h2>X</h2><p>{phrase} | 1 Bath | <span>500</span> sq.ft.</p>"
            f"<a onclick=\"getUnitListByFloor(this,'FP',2,1)\">x</a>"
        )
        meta = parse_repli360_plan_meta(html)
        assert meta["FP"]["bedrooms"] == expected, phrase
