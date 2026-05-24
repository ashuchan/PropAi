"""SecureCafe + apts247 plan-meta enrichment tests (2026-05-22).

Background — the SUCCESS-bar fix:
  6 of 50 SecureCafe-PLAN_LEVEL properties in full-2c2a0af canary are
  apts247-backed Yardi marketing sites. SecureCafe's ``availableunits.
  aspx`` per-unit response carries rent + unit_number but NO sqft for
  these properties (the operator hasn't populated the Sq.Ft cell, or
  the cell is absent in the row markup entirely). Scraper.py's
  ``no_area`` retry trigger then fires → all retries fail → result
  stamps SUCCESS_PLAN_LEVEL despite real unit-level data.

  Fix: when the SecureCafe drill returns units missing sqft AND the
  property's homepage embeds the apts247 ``window.api_key`` token,
  fetch ``/api/v3/floorplans/all/?api_key=KEY`` (returns ``sq_ft +
  bed + bath + name + feed_id`` per plan) and merge sqft into units by
  joining on FloorPlanID (captured from the SecureCafe rentaloptions
  onclick) → apts247 ``feed_id``, falling back to plan-name fuzzy
  match when ``feed_id`` is blank on the apts247 side.

Live-probed 2026-05-22:
  - longwoodsouthernhills.com (1938): apts247 → 4 plans, 700-1100 sq_ft
  - waterfordvillagetn.com (41134): apts247 → 5 plans, 900-1200 sq_ft
"""
from __future__ import annotations

from ma_poc.pms.adapters.base import AdapterResult
from ma_poc.pms.adapters.rentcafe import (
    _SC_FPID_RE,
    _flag_securecafe_units_operator_sqft_gap,
    extract_sqft_from_sc_plan_name,
    find_apts247_api_key,
    merge_apts247_into_securecafe,
    parse_securecafe_availableunits,
)

# ─── api_key extraction ──────────────────────────────────────────────


def test_find_apts247_api_key_extracts_hex_token() -> None:
    """The apts247 marketing-site template embeds a 40-char hex api_key
    in an inline <script>. Live: longwoodsouthernhills.com →
    a9844e2be6499502feb1773f652426a07a3a0e3d."""
    html = (
        "<html><body>"
        '<script>window.api_key = "a9844e2be6499502feb1773f652426a07a3a0e3d";</script>'
        "</body></html>"
    )
    assert find_apts247_api_key(html) == "a9844e2be6499502feb1773f652426a07a3a0e3d"


def test_find_apts247_api_key_single_quotes() -> None:
    """The single-quoted variant of the same pattern — both seen in the
    wild across the 6-property apts247 cohort."""
    html = "<script>window.api_key = '67a19f3d13ee3b149656c1151bc8d63913efd8b7';</script>"
    assert find_apts247_api_key(html) == "67a19f3d13ee3b149656c1151bc8d63913efd8b7"


def test_find_apts247_api_key_absent() -> None:
    assert find_apts247_api_key("") == ""
    assert find_apts247_api_key("<html>no apts247 marker here</html>") == ""
    # Non-hex token must not match (must be 20-80 hex chars only).
    assert find_apts247_api_key('window.api_key = "not-a-hex-token"') == ""


# ─── SecureCafe row → FloorPlanID capture ────────────────────────────


def test_sc_fpid_regex_extracts_from_rentaloptions_onclick() -> None:
    """The Apply Now button's onclick is ``SetTermsUrl('rentaloptions.
    aspx?UnitID=<u>&FloorPlanID=<fp>&...')`` — captured exactly from
    longwoodsouthernhills.com unitrow_10901344."""
    row = (
        "<input onclick=\"SetTermsUrl('rentaloptions.aspx?UnitID=10901344"
        "&FloorPlanID=2295220&myOlePropertyid=603298&MoveInDate=5/28/2026')\">"
    )
    m = _SC_FPID_RE.search(row)
    assert m is not None
    assert m.group(1) == "2295220"


def test_sc_fpid_regex_absent_when_no_onclick() -> None:
    assert _SC_FPID_RE.search("") is None
    assert _SC_FPID_RE.search("<td>no apply button here</td>") is None


def test_parse_securecafe_attaches_floorplan_id_to_source_ids() -> None:
    """``parse_securecafe_availableunits`` now stamps
    ``source_ids['securecafe_floorplan_id']`` on each unit — the join
    key for the apts247 enrichment downstream. Without this, the
    apts247 merge would have to fall back to name-only matching."""
    html = """
    <h1>… Floor Plan: 1 BED 1 BATH - 1 Bedroom, 1 Bathroom</h1>
    <tr class='AvailUnitRow' data-selenium-id='urow1' id='unitrow_10901344'
        scope='row' FloorPlateID='0'>
      <th data-label='Apartment'>#119</th>
      <td data-label='Rent'>$1,125-$1,719</td>
      <td data-label='Action'>
        <input type='button' class='UnitSelect btn'
          onclick="SetTermsUrl('rentaloptions.aspx?UnitID=10901344&FloorPlanID=2295220&myOlePropertyid=603298&MoveInDate=5/28/2026')">
      </td>
    </tr>
    """
    units = parse_securecafe_availableunits(html, "https://x.securecafe.com/x/availableunits.aspx")
    assert len(units) == 1
    u = units[0]
    assert u["unit_number"] == "119"
    assert u["market_rent_low"] == 1125
    assert u["market_rent_high"] == 1719
    # The FloorPlanID is on source_ids — schema-blessed for stable
    # PMS-native identifiers.
    assert u["source_ids"]["securecafe_floorplan_id"] == "2295220"
    # And sqft is empty because the row lacked the Sq.Ft cell — exactly
    # the case the apts247 enrichment is built to fix.
    assert u["sqft"] == ""


# ─── apts247 plan-list shape (live-captured 2026-05-22) ──────────────


# Captured live from longwoodsouthernhills.com
# ``/api/v3/floorplans/all/?api_key=a9844e2be6499502feb1773f652426a07a3a0e3d``
# (Yardi/apts247 marketing platform). Only the fields used by the merge
# function are kept; the live response carries ~50 keys per plan.
_APTS247_PLANS = [
    {
        "id": 17337, "feed_id": "2295220", "name": "1 Bed 1 Bath",
        "bed": 1, "bath": 1.0, "sq_ft": "700",
    },
    {
        "id": 17339, "feed_id": "2295221", "name": "2 Bed 1 Bath",
        "bed": 2, "bath": 1.0, "sq_ft": "1080",
    },
    {
        "id": 17341, "feed_id": "2295223", "name": "2 Bed 1.5 Bath Townhouse",
        "bed": 2, "bath": 1.5, "sq_ft": "1100",
    },
    {
        "id": 17342, "feed_id": "2295222", "name": "2 Bed 2 Bath",
        "bed": 2, "bath": 2.0, "sq_ft": "1080",
    },
]


def _make_unit(
    fp_id: str = "",
    plan_name: str = "1 Bed 1 Bath",
    beds: str = "1",
    baths: str = "1.0",
    sqft: str = "",
) -> dict:
    return {
        "unit_number": "119", "floor_plan_name": plan_name,
        "bedrooms": beds, "bathrooms": baths, "sqft": sqft,
        "market_rent_low": 1125, "market_rent_high": 1719,
        "source_ids": ({"securecafe_floorplan_id": fp_id} if fp_id else {}),
    }


# ─── merge: feed_id join (preferred) ─────────────────────────────────


def test_merge_apts247_fills_sqft_via_feed_id_join() -> None:
    units = [
        _make_unit(fp_id="2295220", plan_name="1 BED 1 BATH"),
        _make_unit(fp_id="2295222", plan_name="2 BED 2 BATH",
                   beds="2", baths="2.0"),
    ]
    n = merge_apts247_into_securecafe(units, _APTS247_PLANS)
    assert n == 2
    assert units[0]["sqft"] == "700"
    assert units[1]["sqft"] == "1080"


def test_merge_apts247_preserves_existing_unit_values() -> None:
    """Per-unit values (when present) WIN — meta only fills gaps. If
    SecureCafe somehow had non-zero sqft, the apts247 value must NOT
    overwrite it."""
    units = [_make_unit(fp_id="2295220", sqft="722")]
    n = merge_apts247_into_securecafe(units, _APTS247_PLANS)
    assert n == 0  # nothing filled
    assert units[0]["sqft"] == "722"  # SecureCafe value preserved


def test_merge_apts247_overwrites_zero_sqft() -> None:
    """``sqft == '0'`` is the lancasterridgeapts pattern — the cell
    exists but the operator hasn't populated it. Treat as missing and
    overwrite with the apts247 value when one is available."""
    units = [_make_unit(fp_id="2295220", sqft="0")]
    n = merge_apts247_into_securecafe(units, _APTS247_PLANS)
    assert n == 1
    assert units[0]["sqft"] == "700"


# ─── merge: name fuzzy join (fallback) ───────────────────────────────


def test_merge_apts247_falls_back_to_name_when_feed_id_missing() -> None:
    """waterfordvillagetn.com case: apts247 returns plans with
    feed_id="" so feed-id join fails — fall back to normalized name
    match (SecureCafe header "2Bed 1.5 Bath" vs apts247 "2 Bed 1.5
    Bath" must match)."""
    plans_no_feedid = [
        {"feed_id": "", "name": "2 Bed 1.5 Bath", "bed": 2, "bath": 1.5,
         "sq_ft": "1040"},
        {"feed_id": "", "name": "3 Bed 2 Bath", "bed": 3, "bath": 2.0,
         "sq_ft": "1200"},
    ]
    units = [
        # Note: SecureCafe header text has no space — "2Bed 1.5 Bath"
        _make_unit(plan_name="2Bed 1.5 Bath", beds="2", baths="1.5"),
    ]
    n = merge_apts247_into_securecafe(units, plans_no_feedid)
    assert n == 1
    assert units[0]["sqft"] == "1040"


def test_merge_apts247_bedbath_bucket_join_unambiguous_only() -> None:
    """Last-resort: when both feed_id AND name fail, match by
    (bed, bath) — but ONLY when exactly one apts247 plan has that
    bed/bath signature. Ambiguous matches (e.g. 3 plans all 2/1.5)
    must be skipped — mis-filling sqft across plans is worse than
    leaving it blank."""
    # Two 2/1.5 plans → ambiguous → no fill.
    ambiguous_plans = [
        {"feed_id": "x1", "name": "Plan-A", "bed": 2, "bath": 1.5, "sq_ft": "999"},
        {"feed_id": "x2", "name": "Plan-B", "bed": 2, "bath": 1.5, "sq_ft": "1111"},
    ]
    units = [_make_unit(plan_name="unknown-name", beds="2", baths="1.5")]
    n = merge_apts247_into_securecafe(units, ambiguous_plans)
    assert n == 0
    assert units[0]["sqft"] == ""

    # Single 3/2.0 plan → unambiguous → fill.
    unambiguous_plans = ambiguous_plans + [
        {"feed_id": "x3", "name": "Plan-C", "bed": 3, "bath": 2.0, "sq_ft": "1500"},
    ]
    units2 = [_make_unit(plan_name="unknown-name", beds="3", baths="2.0")]
    n2 = merge_apts247_into_securecafe(units2, unambiguous_plans)
    assert n2 == 1
    assert units2[0]["sqft"] == "1500"


# ─── merge: defensive no-ops ─────────────────────────────────────────


def test_merge_apts247_noop_when_no_units() -> None:
    assert merge_apts247_into_securecafe([], _APTS247_PLANS) == 0


def test_merge_apts247_noop_when_no_plans() -> None:
    units = [_make_unit(fp_id="2295220")]
    assert merge_apts247_into_securecafe(units, []) == 0
    assert units[0]["sqft"] == ""


def test_merge_apts247_skips_unit_when_no_match() -> None:
    """A unit with an unknown FloorPlanID and unrecognized plan name
    must NOT silently inherit a random plan's sqft."""
    units = [
        _make_unit(fp_id="9999999", plan_name="Penthouse Suite",
                   beds="4", baths="3.5"),
    ]
    n = merge_apts247_into_securecafe(units, _APTS247_PLANS)
    assert n == 0
    assert units[0]["sqft"] == ""


# ─── sqft-from-plan-name (gravity255 pattern, 29 units) ──────────────


def test_extract_sqft_from_plan_name_recognises_bedsxbaths_sqft() -> None:
    """The gravity255 operator encodes sqft in the SC plan name itself:
    ``Floor Plan: 1x1 534 - 1 Bedroom, 1 Bathroom``. Pattern is
    <beds>x<baths> <sqft-3-to-5-digits>."""
    assert extract_sqft_from_sc_plan_name("1x1 534") == "534"
    assert extract_sqft_from_sc_plan_name("2x2 988") == "988"
    assert extract_sqft_from_sc_plan_name("3x2 1297") == "1297"
    # With fractional baths:
    assert extract_sqft_from_sc_plan_name("2x1.5 1040") == "1040"
    # And with surrounding text (some operators add suffixes):
    assert extract_sqft_from_sc_plan_name("1x1 534 PHII") == "534"


def test_extract_sqft_from_plan_name_no_false_positives() -> None:
    """The pattern must NOT match arbitrary numbers — only when prefixed
    by the bedsxbaths signature. Plan names like "Adhara" or "2 Bed 1
    Bath" don't carry sqft inline."""
    assert extract_sqft_from_sc_plan_name("") == ""
    assert extract_sqft_from_sc_plan_name("Adhara") == ""
    assert extract_sqft_from_sc_plan_name("1 BED 1 BATH") == ""
    assert extract_sqft_from_sc_plan_name("2 Bed 1.5 Bath Townhouse") == ""
    # Bare number, no bedsxbaths prefix — must not match.
    assert extract_sqft_from_sc_plan_name("Plan 534") == ""


# ─── operator-sqft-gap flag (byelon-style fix, 2026-05-23) ───────────


def test_flag_operator_sqft_gap_marks_units_missing_sqft() -> None:
    """After all 3 enrichment paths return empty, any remaining unit
    without sqft gets data_gaps=['sqft'] + data_quality_flag set."""
    units = [
        {"unit_number": "101", "sqft": "", "market_rent_low": 1200},
        {"unit_number": "102", "sqft": "0", "market_rent_low": 1250},
        # Already has sqft — must not be flagged.
        {"unit_number": "201", "sqft": "950", "market_rent_low": 1500},
    ]
    result = AdapterResult(tier_used="TIER_1_API_RENTCAFE_SECURECAFE")
    n = _flag_securecafe_units_operator_sqft_gap(units, result)
    assert n == 2
    assert units[0]["data_gaps"] == ["sqft"]
    assert units[0]["data_quality_flag"] == "SQFT_NOT_PUBLISHED"
    assert units[1]["data_gaps"] == ["sqft"]  # zero sqft also gets flagged
    assert units[2].get("data_gaps", []) == []  # real sqft preserved unflagged
    assert units[2].get("data_quality_flag", "") == ""


def test_flag_operator_sqft_gap_appends_to_existing_gaps_list() -> None:
    """If a previous adapter already documented a different gap (e.g.
    bedrooms), we must APPEND sqft, not overwrite the list."""
    units = [{
        "unit_number": "101", "sqft": "", "market_rent_low": 1200,
        "data_gaps": ["bedrooms"],
    }]
    result = AdapterResult(tier_used="TIER_1_API_RENTCAFE_SECURECAFE")
    _flag_securecafe_units_operator_sqft_gap(units, result)
    assert units[0]["data_gaps"] == ["bedrooms", "sqft"]


def test_flag_operator_sqft_gap_preserves_existing_quality_flag() -> None:
    """If an upstream layer already set a stronger flag (e.g.
    'CARRIED_FORWARD'), don't clobber it."""
    units = [{
        "unit_number": "101", "sqft": "", "market_rent_low": 1200,
        "data_quality_flag": "CARRIED_FORWARD",
    }]
    result = AdapterResult(tier_used="TIER_1_API_RENTCAFE_SECURECAFE")
    _flag_securecafe_units_operator_sqft_gap(units, result)
    assert units[0]["data_quality_flag"] == "CARRIED_FORWARD"
    # But the gap list still updates so consumers can see sqft is missing.
    assert "sqft" in units[0]["data_gaps"]


def test_flag_operator_sqft_gap_idempotent() -> None:
    """Calling twice must not add 'sqft' twice to data_gaps."""
    units = [{"unit_number": "101", "sqft": "", "market_rent_low": 1200}]
    result = AdapterResult(tier_used="TIER_1_API_RENTCAFE_SECURECAFE")
    _flag_securecafe_units_operator_sqft_gap(units, result)
    _flag_securecafe_units_operator_sqft_gap(units, result)
    assert units[0]["data_gaps"] == ["sqft"]


# ─── end-to-end SUCCESS-bar smoke test ───────────────────────────────


def test_end_to_end_securecafe_plus_apts247_yields_rent_plus_sqft() -> None:
    """Mirrors the production flow:
      1. parse_securecafe_availableunits(html) → unit with rent +
         unit_number + source_ids.securecafe_floorplan_id, sqft=""
      2. merge_apts247_into_securecafe(units, apts247_plans) → sqft
         filled from the matching plan
    Asserts the unit clears the Surgex ≥1-unit-with-rent+sqft bar."""
    sc_html = """
    <h1>… Floor Plan: 1 BED 1 BATH - 1 Bedroom, 1 Bathroom</h1>
    <tr class='AvailUnitRow' id='unitrow_10901344'>
      <th data-label='Apartment'>#119</th>
      <td data-label='Rent'>$1,125-$1,719</td>
      <td><input onclick="SetTermsUrl('rentaloptions.aspx?UnitID=10901344&FloorPlanID=2295220&myOlePropertyid=603298&MoveInDate=5/28/2026')"></td>
    </tr>
    """
    units = parse_securecafe_availableunits(
        sc_html, "https://x.securecafe.com/x/availableunits.aspx"
    )
    assert len(units) == 1
    assert units[0]["market_rent_low"] == 1125
    assert units[0]["sqft"] == ""  # missing before enrichment

    n = merge_apts247_into_securecafe(units, _APTS247_PLANS)
    assert n == 1
    # SUCCESS bar: ≥1 unit with rent AND sqft.
    rent_and_sqft = [
        u for u in units if u.get("market_rent_low") and u.get("sqft")
    ]
    assert len(rent_and_sqft) == 1
    assert rent_and_sqft[0]["sqft"] == "700"
