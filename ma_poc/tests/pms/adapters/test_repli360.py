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
    _AREA_FROM_FP_CARD,
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


def test_parse_str_html_future_dated_unit_is_available() -> None:
    """2026-05-26 fix (#116): units with future Availability dates (e.g.
    '06-05-2026') were wrongly marked UNKNOWN because the prior check looked
    for 'available' in the display text. getUnitListByFloor ONLY returns
    units that are bookable — all rows must be AVAILABLE unconditionally.

    royceattrumbull.com probe: 20/29 units (69%) had future dates and were
    wrongly marked UNKNOWN before this fix.
    """
    str_html = """
    <div><table><tbody>
    <tr class="unitlisting 2 lease_term_wrap_2"
        data-count="0"
        data-available_date="2026-06-11"
        data-engrain="">
      <td>Building Number 6</td>
      <td><span class="mobile_rrac">Unit Number</span><b class="unitNumber">6311</b></td>
      <td class="unitTouring"></td>
      <td><span class="mobile_rrac">Deposit</span>$1,000</td>
      <td class="rrac_unit_price">
        <span class="unit_price_value unit-rrac-price">$2,365</span>
      </td>
      <td><span class="mobile_rrac">Availability</span>06-11-2026</td>
      <td></td>
    </tr>
    </tbody></table></div>
    """
    units = parse_repli360_str(str_html, "https://app.repli360.com/x")
    assert len(units) == 1
    u = units[0]
    assert u["unit_number"] == "6311"
    # Future-dated unit must be AVAILABLE (not UNKNOWN)
    assert u["availability_status"] == "AVAILABLE", (
        "getUnitListByFloor only returns bookable units; future-dated ones "
        "must be AVAILABLE, not UNKNOWN"
    )
    assert u.get("availability_date") == "2026-06-11" or u.get("available_date") == "2026-06-11"
    assert u["market_rent_low"] == 2365


def test_waitlist_sentinel_becomes_undated_plan_evidence_not_a_unit() -> None:
    from datetime import UTC, datetime

    from ma_poc.scripts.runners.jugnu import _format_v2_unit

    html = """
    <table><tr class="unitlisting 33759999 lease_term_wrap_33759999"
      data-apartmentid="33759999" data-available_date="2026-08-02">
      <td><span class="mobile_rrac">Unit Number</span>
          <b class="unitNumber">WAIT147S</b></td>
      <td><span class="mobile_rrac">Starting At</span>Call for Pricing</td>
      <td><span class="mobile_rrac">Availability</span>--</td>
      <td><a href="javascript:void(0)">Contact</a></td>
    </tr></table>
    """

    [row] = parse_repli360_str(html, "https://app.repli360.com/admin/getUnitListByFloor")

    assert row["unit_number"] == ""
    assert row["is_floor_plan_level"] is True
    assert row["availability_status"] == "WAITLIST"
    assert row["availability_date"] == ""
    assert "repli360_unit_id" not in row["source_ids"]

    output = _format_v2_unit(
        row,
        datetime(2026, 8, 2, 12, tzinfo=UTC),
        "river-oaks",
    )
    assert output["availability_status"] == "WAITLIST"
    assert output["available_date"] is None
    assert output["unit_id"] is None


def test_physical_row_preserves_native_repli_identity() -> None:
    html = """
    <table><tr class="unitlisting 33752567 lease_term_wrap_33752567"
      data-apartmentid="33752567" data-available_date="2026-09-15">
      <td><span class="mobile_rrac">Unit Number</span>
          <b class="unitNumber">1238</b></td>
      <td><span class="mobile_rrac">Starting At</span>
          <span class="unit_price_value">$1,925</span></td>
      <td><span class="mobile_rrac">Availability</span>09/15/2026</td>
      <td><a href="https://example.securecafe.com/apply?UnitID=33752567">Lease Now</a></td>
    </tr></table>
    """

    [row] = parse_repli360_str(html, "https://app.repli360.com/admin/getUnitListByFloor")

    assert row["unit_number"] == "1238"
    assert row["unit_name"] == "1238"
    assert row["source_ids"]["repli360_unit_id"] == "33752567"
    assert row["unit_id"] == "33752567"


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


# ─── 2026-07-28 per-unit "Unit SQFT" column (area=-1 recovery) ─────────
#
# Reference run 2026-07-27 (run-2026-07-27-full-0d54ca7): 5 repli360
# properties shipped 37 rows with area=-1 while the SAME payload carried
# the area on the unit's own row:
#   14117 Marquis at Great Hills  13 rows (e.g. 0219 @ $1,731)
#   27950 Marquis Parkside         8 rows (e.g. 0320 @ $1,334)
#   62953 Marq on Burnet           7 rows (e.g.  142 @ $1,679)
#   39494 Hamburg Farms            6 rows (e.g. 1207 @ $1,457)
#   58452 Enclave at Brookside     3 rows (e.g. 1-143 @ $1,485)
#
# Cause: sqft came ONLY from the plan card, and the plan card writes a
# RANGE for multi-size plans — "<span>932 - 1084</span> sq.ft." — which
# _SQFT_META_RE (a span of pure digits) does not match. Every unit of
# such a plan got no sqft. The availability table's own "Unit SQFT"
# column had the exact per-unit answer all along.

# Captured verbatim from mqgreathills.com getUnitListByFloor,
# floorPlanID 4464995 (plan "1F", whose card reads "932 - 1084 sq.ft.").
# The two units have DIFFERENT real areas — which is exactly why
# collapsing the plan range to one number would have been wrong.
_STR_HTML_UNIT_SQFT = """
<div class="rrac_listAvailableUnit"><table class="table" id="fp_table1">
<tr><th>Unit Number</th><th>Unit SQFT</th><th class="rrac_amenties">Unit Amenities</th>
<th class="bedRename">Starting At</th><th>Availability</th><th>Lease Now</th></tr>
<tr class="unitlisting 33752741 lease_term_wrap_33752741" data-count="0"
    data-available_date="2026-08-05">
  <td><span class="unitNumberlbl mobile_rrac">Unit Number</span>
      <b class="unitNumber">0219</b></td>
  <td><span class="mobile_rrac">Unit SQFT</span><b class="">932 SQFT</b></td>
  <td class="rrac_amenties"><span class="mobile_rrac">Unit Amenities</span>
      <a class="entrata_unitwise_amenity_info">See Amenities</a></td>
  <td class="rrac_unit_price lease-price"><span class="mobile_rrac">Starting At</span>
      <span class="unit_price_value unit-rrac-price">$1,731</span></td>
  <td><span class="mobile_rrac">Availability</span>08/05/2026</td>
  <td><a href="https://mqgreathills.securecafe.com/x">Lease Now</a></td>
</tr>
<tr class="unitlisting 33752740 lease_term_wrap_33752740" data-count="7"
    data-available_date="2026-10-05">
  <td><span class="unitNumberlbl mobile_rrac">Unit Number</span>
      <b class="unitNumber">0218</b></td>
  <td><span class="mobile_rrac">Unit SQFT</span><b class="">1084 SQFT</b></td>
  <td class="rrac_amenties"><span class="mobile_rrac">Unit Amenities</span>
      <a class="entrata_unitwise_amenity_info">See Amenities</a></td>
  <td class="rrac_unit_price lease-price"><span class="mobile_rrac">Starting At</span>
      <span class="unit_price_value unit-rrac-price">$2,280</span></td>
  <td><span class="mobile_rrac">Availability</span>10/05/2026</td>
  <td><a href="https://mqgreathills.securecafe.com/y">Lease Now</a></td>
</tr>
</table></div>
"""

# Captured verbatim from the Enclave at Brookside (site_id 2538) payload.
# SAME endpoint, DIFFERENT column order — Amenities/Special/Deposit sit
# in other positions and the Deposit cell is a bare "$300". A positional
# read would have grabbed the deposit as an area.
_STR_HTML_UNIT_SQFT_REORDERED = """
<div class="rrac_listAvailableUnit"><table class="table" id="fp_table1">
<tr><th>Unit Number</th><th>Unit SQFT</th><th class="rrac_amenties">Amenities</th>
<th class="special_th">Special</th><th class="bedRename">Starting At</th>
<th>Deposit</th><th>Availability</th><th>Lease Now</th></tr>
<tr class="unitlisting 15164042 lease_term_wrap_15164042" data-count="0"
    data-available_date="2026-07-03">
  <td><span class="unitNumberlbl mobile_rrac">Unit Number</span>
      <b class="unitNumber">1-143</b></td>
  <td><span class="mobile_rrac">Unit SQFT</span><b class="">724 SQFT</b></td>
  <td class="rrac_amenties"><span class="mobile_rrac">Amenities</span>
      <a class="entrata_unitwise_amenity_info">See Amenities</a></td>
  <td class="special_td"><span class="mobile_rrac">Special</span>-</td>
  <td class="rrac_unit_price lease-price"><span class="mobile_rrac">Starting At</span>
      <span class="unit_price_value unit-rrac-price">$1,485</span></td>
  <td><span class="mobile_rrac">Deposit</span>$300</td>
  <td><span class="mobile_rrac">Availability</span>Available Now</td>
  <td><a href="https://enclavebrooksideapts.securecafe.com/x">Lease Now</a></td>
</tr>
</table></div>
"""


def test_unit_sqft_read_from_the_units_own_row() -> None:
    """THE 37-row regression. Plan 1F's card says "932 - 1084 sq.ft." so
    the plan-meta path yields nothing; each unit row carries its own
    exact area. 0219=932 and 0218=1084 on the SAME plan — proof that any
    single plan-level number would be wrong for one of them."""
    units = parse_repli360_str(_STR_HTML_UNIT_SQFT, "https://app.repli360.com/x")
    assert len(units) == 2
    by_num = {u["unit_number"]: u for u in units}
    assert by_num["0219"]["sqft"] == "932"
    assert by_num["0218"]["sqft"] == "1084"
    # Rent/date/status must be untouched by the sqft read.
    assert by_num["0219"]["market_rent_low"] == 1731
    assert by_num["0218"]["market_rent_low"] == 2280
    assert by_num["0219"]["availability_date"] == "2026-08-05"
    assert by_num["0218"]["availability_status"] == "AVAILABLE"
    # Read off the unit's own row ⇒ NOT plan-derived ⇒ no provenance flag.
    assert not by_num["0219"].get("data_quality_flag")


def test_unit_sqft_survives_reordered_columns_and_ignores_deposit() -> None:
    """Column order is not stable across repli360 templates. The cell is
    found by its label, so a "$300" Deposit cell sitting where another
    template puts Availability can never be read as an area."""
    units = parse_repli360_str(
        _STR_HTML_UNIT_SQFT_REORDERED, "https://app.repli360.com/x"
    )
    assert len(units) == 1
    u = units[0]
    assert u["unit_number"] == "1-143"
    assert u["sqft"] == "724"  # not 300, not 1485
    assert u["market_rent_low"] == 1485


def test_unit_sqft_header_index_fallback_when_cell_labels_absent() -> None:
    """If a template drops the per-cell ``span.mobile_rrac`` labels, the
    column is located from that table's own <th> header row — never from
    an assumed index."""
    html = """
    <table class="table">
    <tr><th>Unit Number</th><th>Deposit</th><th>Unit SQFT</th><th>Starting At</th></tr>
    <tr class="unitlisting 7" data-available_date="2026-09-01">
      <td><b class="unitNumber">312</b></td>
      <td>$500</td>
      <td>845 SQFT</td>
      <td><span class="unit_price_value">$1,900</span></td>
    </tr></table>
    """
    units = parse_repli360_str(html, "https://app.repli360.com/x")
    assert len(units) == 1
    assert units[0]["sqft"] == "845"  # not 500
    assert units[0]["market_rent_low"] == 1900


def test_unit_sqft_absent_rather_than_guessed() -> None:
    """Fail closed. A row with no sqft column, or an unparseable cell,
    leaves sqft empty (area=-1 downstream) instead of picking up money,
    a placeholder dash, or half of a range."""
    rows = {
        "no such column": """
            <table><tr><th>Unit Number</th><th>Starting At</th></tr>
            <tr class="unitlisting 1" data-available_date="2026-09-01">
              <td><b class="unitNumber">100</b></td>
              <td><span class="mobile_rrac">Starting At</span>
                  <span class="unit_price_value">$1,485</span></td>
            </tr></table>""",
        "placeholder dash": """
            <table><tr><th>Unit Number</th><th>Unit SQFT</th></tr>
            <tr class="unitlisting 1" data-available_date="2026-09-01">
              <td><b class="unitNumber">100</b></td>
              <td><span class="mobile_rrac">Unit SQFT</span>-</td>
            </tr></table>""",
        "range in the unit cell": """
            <table><tr><th>Unit Number</th><th>Unit SQFT</th></tr>
            <tr class="unitlisting 1" data-available_date="2026-09-01">
              <td><b class="unitNumber">100</b></td>
              <td><span class="mobile_rrac">Unit SQFT</span>932 - 1084 SQFT</td>
            </tr></table>""",
        "money in the sqft cell": """
            <table><tr><th>Unit Number</th><th>Unit SQFT</th></tr>
            <tr class="unitlisting 1" data-available_date="2026-09-01">
              <td><b class="unitNumber">100</b></td>
              <td><span class="mobile_rrac">Unit SQFT</span>$1,485</td>
            </tr></table>""",
    }
    for label, html in rows.items():
        units = parse_repli360_str(html, "https://app.repli360.com/x")
        assert len(units) == 1, label
        assert units[0]["sqft"] == "", (
            f"{label}: guessed an area instead of leaving it absent"
        )


def test_plan_card_sqft_is_stamped_with_its_provenance() -> None:
    """When the area comes from the plan card rather than the unit's own
    row, it is recorded via the EXISTING data_quality_flag machinery. A
    unit that already carries its own area is neither overwritten nor
    stamped."""
    units = [
        {"unit_number": "4114", "sqft": "", "bedrooms": "", "bathrooms": "",
         "floor_plan_name": "", "data_quality_flag": ""},
        {"unit_number": "4115", "sqft": "932", "bedrooms": "", "bathrooms": "",
         "floor_plan_name": "", "data_quality_flag": ""},
    ]
    merge_repli360_plan_meta(
        units,
        {"floor_plan_name": "1A", "sqft": "670", "bedrooms": "1", "bathrooms": "1"},
    )
    assert units[0]["sqft"] == "670"
    assert _AREA_FROM_FP_CARD in units[0]["data_quality_flag"]
    # Unit-level area wins and stays unflagged.
    assert units[1]["sqft"] == "932"
    assert _AREA_FROM_FP_CARD not in units[1]["data_quality_flag"]
    # Plan-genuine fields (beds/baths/name) do not trigger the area flag.
    assert units[1]["bedrooms"] == "1"


def test_area_provenance_flag_cannot_demote_a_real_unit() -> None:
    """The provenance token must not contain "PLAN".

    ``extraction/post_process.py`` and ``core/schema_v2.py`` read any
    data_quality_flag containing "PLAN" / "PLAN_LEVEL" as evidence that
    the row is a floor-plan placeholder. A well-meaning rename to e.g.
    "AREA_FROM_PLAN_CARD" would silently strip unit-anchored repli360
    rows of their identity."""
    upper = _AREA_FROM_FP_CARD.upper()
    assert "PLAN" not in upper
    assert "PRESENCE" not in upper
    assert "UNVERIFIED" not in upper


def test_plan_card_range_sqft_is_not_collapsed_to_a_number() -> None:
    """Documents the underlying cause and pins the honest behaviour: a
    plan card carrying a RANGE yields no plan-level sqft. We do not pick
    the min (or max, or mean) and present it as a unit's area — the unit
    row is the only place a real per-unit area exists."""
    html = (
        "<h2>1F</h2><p>One Bedroom | 1 Bath | <span>932 - 1084</span> sq.ft. "
        "| <span>8</span> Units Available</p>"
        "<a onclick=\"getUnitListByFloor(this,'4464995',2,1608)\">x</a>"
    )
    meta = parse_repli360_plan_meta(html)
    assert meta["4464995"].get("sqft") is None
    assert meta["4464995"]["floor_plan_name"] == "1F"
    assert meta["4464995"]["bedrooms"] == "1"


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


# ─── 2026-07-28 `building` read the label, not the first column ────────
#
# ``building`` was taken from the FIRST <td> of every row, with the
# literal string "Building Number" stripped off it:
#
#     building = tds[0].get_text(strip=True).replace("Building Number", "")
#
# That holds on royceattrumbull (the 2026-05-17 reference property),
# whose first column really is "Building Number". On the CURRENT repli360
# template the first column is "Unit Number" and there is no Building
# column at all, so the strip matched nothing and the label text shipped
# concatenated with the unit number.
#
# Reference run 2026-07-27 (run-2026-07-27-full-0d54ca7) — every unit of
# every affected property, not a sample:
#   14117 Marquis at Great Hills  30/30 rows  building="Unit Number0313"
#   27950 Marquis Parkside                    building="Unit Number1025"
#   62953 Marq on Burnet                      building="Unit Number208"
#   39494 Hamburg Farms                       building="Unit Number3412"
#   58452 Enclave at Brookside                building="Unit Number1-143"
#
# Across the 10 captured repli360 payloads: 212 of 229 rows. The other 17
# are royce's, which has a real Building column and was always correct —
# so the fix must BLANK the 212 without touching the 17.

# Captured verbatim from mqgreathills.com getUnitListByFloor (site_id
# 1608). Note the header row: Unit Number, Unit SQFT, … — no Building
# column exists on this template at all.
_STR_HTML_NO_BUILDING_COLUMN = """
<div class="rrac_listAvailableUnit"><table class="table" id="fp_table1">
<tr><th>Unit Number</th><th>Unit SQFT</th><th class="rrac_amenties">Unit Amenities</th>
<th class="bedRename">Starting At</th><th>Availability</th><th>Lease Now</th></tr>
<tr class="unitlisting 33752741 lease_term_wrap_33752741" data-count="0"
    data-available_date="2026-08-05">
  <td><span class="unitNumberlbl mobile_rrac">Unit Number</span>
      <b class="unitNumber">0219</b></td>
  <td><span class="mobile_rrac">Unit SQFT</span><b class="">932 SQFT</b></td>
  <td class="rrac_amenties"><span class="mobile_rrac">Unit Amenities</span>
      <a class="entrata_unitwise_amenity_info">See Amenities</a></td>
  <td class="rrac_unit_price lease-price"><span class="mobile_rrac">Starting At</span>
      <span class="unit_price_value unit-rrac-price">$1,731</span></td>
  <td><span class="mobile_rrac">Availability</span>08/05/2026</td>
  <td><a href="https://mqgreathills.securecafe.com/x">Lease Now</a></td>
</tr>
</table></div>
"""


def test_building_is_empty_when_the_template_has_no_building_column() -> None:
    """The current repli360 template leads with "Unit Number".

    Pre-fix this shipped ``building="Unit Number0219"`` — the column
    label glued to the unit number — on every unit of every property on
    this template. There is no Building column here, so the honest
    answer is empty: absent, not invented.
    """
    units = parse_repli360_str(
        _STR_HTML_NO_BUILDING_COLUMN, "https://app.repli360.com/x"
    )
    assert len(units) == 1
    u = units[0]
    assert u["unit_number"] == "0219"
    assert u["building"] == "", (
        f"building={u['building']!r} — the 'Unit Number' column label was "
        f"read as a building"
    )
    # The rest of the row is unaffected.
    assert u["sqft"] == "932"
    assert u["market_rent_low"] == 1731


def test_building_never_echoes_a_column_label_or_the_unit_number() -> None:
    """Guards the exact shape of the bug rather than one literal value.

    Any first column whose label is not a building label must yield an
    empty building — never the label text, and never the unit number
    (a unit number in the building field silently corrupts the
    ``unit_number|building`` dedup key in ``Repli360Adapter.extract``).
    """
    for label in ("Unit Number", "Apartment Number", "Unit", "Apt #"):
        html = f"""
        <table class="table">
        <tr><th>{label}</th><th>Starting At</th></tr>
        <tr class="unitlisting 1" data-available_date="2026-09-01">
          <td><span class="mobile_rrac">{label}</span>
              <b class="unitNumber">0219</b></td>
          <td><span class="mobile_rrac">Starting At</span>
              <span class="unit_price_value">$1,731</span></td>
        </tr></table>
        """
        units = parse_repli360_str(html, "https://app.repli360.com/x")
        assert len(units) == 1, label
        got = units[0]["building"]
        assert got == "", f"{label}: building={got!r}"
        assert "0219" not in got, f"{label}: unit number leaked into building"


def test_building_still_read_when_the_column_is_real() -> None:
    """royceattrumbull (site_id 1619) — the property the original code was
    written against — has a genuine "Building Number" column. Those 17
    captured rows were always correct and must stay correct: unit 4114 is
    in building 4, per the endpoint's own BuildingID=4 lease link.
    """
    units = parse_repli360_str(_STR_HTML, "https://app.repli360.com/x")
    assert [u["building"] for u in units] == ["4", "6"]
    assert [u["unit_number"] for u in units] == ["4114", "6203"]


def test_building_survives_reordered_columns() -> None:
    """The building cell is located by its LABEL, so it is found wherever
    the template puts it — not assumed to be first."""
    html = """
    <table class="table">
    <tr><th>Unit Number</th><th>Starting At</th><th>Building Number</th></tr>
    <tr class="unitlisting 1" data-available_date="2026-09-01">
      <td><span class="mobile_rrac">Unit Number</span>
          <b class="unitNumber">312</b></td>
      <td><span class="mobile_rrac">Starting At</span>
          <span class="unit_price_value">$1,900</span></td>
      <td><span class="mobile_rrac">Building Number</span>7</td>
    </tr></table>
    """
    units = parse_repli360_str(html, "https://app.repli360.com/x")
    assert len(units) == 1
    assert units[0]["building"] == "7"
    assert units[0]["unit_number"] == "312"


def test_building_header_index_fallback_when_cell_labels_absent() -> None:
    """Mirrors the sqft fallback: a template that drops the per-cell
    ``span.mobile_rrac`` labels still resolves the column from that
    table's own <th> header row."""
    html = """
    <table class="table">
    <tr><th>Bldg</th><th>Unit Number</th><th>Starting At</th></tr>
    <tr class="unitlisting 1" data-available_date="2026-09-01">
      <td>12</td>
      <td><b class="unitNumber">312</b></td>
      <td><span class="unit_price_value">$1,900</span></td>
    </tr></table>
    """
    units = parse_repli360_str(html, "https://app.repli360.com/x")
    assert len(units) == 1
    assert units[0]["building"] == "12"


def test_building_value_keeps_names_that_merely_start_like_the_label() -> None:
    """The redundant-label strip is word-bounded.

    A cell reached by the header-index path may repeat its label as
    literal text ("Building Number 6" → "6"), but a building genuinely
    NAMED "Bldgwood" must survive intact rather than arrive as "wood".
    """
    cases = {
        "Building Number 6": "6",
        "Bldg 12": "12",
        "Building A": "A",
        "Bldgwood": "Bldgwood",
        "Buildings West": "Buildings West",
        "Building Number": "",  # label with no value → absent
        "-": "",
    }
    for cell, expected in cases.items():
        html = f"""
        <table class="table">
        <tr><th>Building</th><th>Unit Number</th></tr>
        <tr class="unitlisting 1" data-available_date="2026-09-01">
          <td>{cell}</td>
          <td><b class="unitNumber">312</b></td>
        </tr></table>
        """
        units = parse_repli360_str(html, "https://app.repli360.com/x")
        assert len(units) == 1, cell
        assert units[0]["building"] == expected, (
            f"cell={cell!r} -> {units[0]['building']!r}, expected {expected!r}"
        )


def test_building_label_matching_is_whole_label_not_substring() -> None:
    """"Building Amenities" contains "Building" but is not a building
    column. Whole-label matching fails closed; a substring rule would
    ship "See Amenities" as a building name."""
    html = """
    <table class="table">
    <tr><th>Unit Number</th><th>Building Amenities</th></tr>
    <tr class="unitlisting 1" data-available_date="2026-09-01">
      <td><span class="mobile_rrac">Unit Number</span>
          <b class="unitNumber">312</b></td>
      <td><span class="mobile_rrac">Building Amenities</span>See Amenities</td>
    </tr></table>
    """
    units = parse_repli360_str(html, "https://app.repli360.com/x")
    assert len(units) == 1
    assert units[0]["building"] == ""
