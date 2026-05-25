"""OneSite workflowstartup probe + parser tests (2026-05-24).

Pins the HAR-driven OneSite fallback that lifts TIER_1_API_ONESITE_
NO_RESPONSE (45 props in focused-3886351 canary). Pre-fix the adapter
returned empty when the homepage didn't fire any RealPage XHRs;
post-fix it discovers SiteId via three paths and calls
``workflowstartup/v1/{SITE_ID}/English`` directly via curl_cffi,
parses ``Workflow.ActivityGroups[*].GroupActivities[*].Floorplans[]``.

Live fixture from 2026-05-24 HAR capture of
www.thepointatabington.com (SiteId 4777974, 3 floorplans).
"""
from __future__ import annotations

import json
from pathlib import Path

from ma_poc.pms.adapters.onesite import (
    _collapse_huge_rent_range,
    _extract_onesite_site_ids,
    _generate_xyz_token,
    _onesite_workflowstartup_url,
    _xyz_md5_upper,
    parse_onesite_workflowstartup,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ----- _extract_onesite_site_ids ---------------------------------------


def test_extract_siteid_from_widget_loader() -> None:
    """Canonical path: leasing subdomain HTML loads
    ``property.onesite.realpage.com/ollr/widgetLoader.js?siteId=N``."""
    body = (
        '<html><body><script src="https://property.onesite.realpage.com/ollr/'
        'widgetLoader.js?siteId=4646505&amp;ContainerId=OllrDiv"></script>'
        '</body></html>'
    )
    ids = _extract_onesite_site_ids(body, "https://x.onlineleasing.realpage.com/")
    assert ids == ["4646505"]


def test_extract_siteid_dedupes_repeated_references() -> None:
    body = (
        '<script src=".../widgetLoader.js?siteId=12345"></script>'
        '<script src=".../widgetLoader.js?siteId=12345"></script>'
    )
    ids = _extract_onesite_site_ids(body, "")
    assert ids == ["12345"]


def test_extract_siteid_returns_empty_for_unrelated_body() -> None:
    body = "<html><body>Welcome to our apartments!</body></html>"
    assert _extract_onesite_site_ids(body, "") == []


def test_extract_siteid_returns_empty_for_blank_body() -> None:
    assert _extract_onesite_site_ids("", "") == []


# ----- xyz auth token generator (reverse-engineered from OLL JS bundle) -----


def test_xyz_md5_upper_matches_har_for_siteid_4646505() -> None:
    """Pins the rpCalculate (MD5) implementation against the verified
    HAR value. SiteId 4646505 → BAC7950C65FF98CFE97623E891524170."""
    assert _xyz_md5_upper("4646505") == "BAC7950C65FF98CFE97623E891524170"


def test_xyz_md5_upper_is_standard_md5_uppercase_hex() -> None:
    """The rpCalculate fn in the OLL bundle uses the same constants
    (1732584193, 4023233417, 2562383102, 271733878) as standard MD5
    init values. Confirm our port matches stdlib MD5."""
    import hashlib
    sample = "hello world"
    assert (
        _xyz_md5_upper(sample)
        == hashlib.md5(sample.encode()).hexdigest().upper()
    )


def test_xyz_token_structure_matches_har_template() -> None:
    """The xyz token decodes (base64) to:
       charGen(1) + md5(siteId).upper() + charGen(3) + md5(UA).upper()
       + charGen(5) + base64(timestamp_ms) + charGen(7)
    Pin the structure so any future bundle update breaks this test."""
    import base64
    token = _generate_xyz_token("4646505", "test-ua", ts_ms=1779444064132)
    decoded = base64.b64decode(token).decode("latin-1")
    # decoded[0] = charGen(1) — 1 alphanumeric
    assert len(decoded) > 0 and decoded[0].isalnum()
    # decoded[1:33] = md5(siteId) — 32 hex chars uppercase
    md5_chunk = decoded[1:33]
    assert len(md5_chunk) == 32
    assert md5_chunk == "BAC7950C65FF98CFE97623E891524170"
    # decoded[33:36] = charGen(3)
    assert all(c.isalnum() for c in decoded[33:36])
    # decoded[36:68] = md5(UA)
    assert len(decoded[36:68]) == 32
    import hashlib
    assert decoded[36:68] == hashlib.md5(b"test-ua").hexdigest().upper()
    # decoded[68:73] = charGen(5)
    assert all(c.isalnum() for c in decoded[68:73])
    # Then base64(timestamp) — should decode to our known ts
    # The token tail is "<base64-ts>" + charGen(7), so find the
    # base64 padding to delimit
    tail = decoded[73:]
    # Take up to the first 20-char base64 sequence (16 chars + ==)
    import re
    m = re.search(r"([A-Za-z0-9+/]+={1,2})", tail)
    assert m, f"expected base64 timestamp in tail: {tail!r}"
    ts_b64 = m.group(1)
    decoded_ts = base64.b64decode(ts_b64).decode()
    assert decoded_ts == "1779444064132"


def test_xyz_token_is_different_on_each_call_due_to_random_chars() -> None:
    """Same siteId but different random chunks → different token."""
    a = _generate_xyz_token("12345", "ua")
    b = _generate_xyz_token("12345", "ua")
    assert a != b


def test_xyz_token_is_base64_encoded() -> None:
    """Final output is base64 — must be decodeable."""
    import base64
    token = _generate_xyz_token("12345", "ua")
    # Will raise on bad padding/chars
    decoded = base64.b64decode(token)
    assert len(decoded) > 0


def test_xyz_token_full_example_reproduces_known_har_md5_chunks() -> None:
    """Cross-check against the live HAR's xyz token for
    www.thepointatabington.com (SiteId 4777974)."""
    import base64

    # SiteId 4777974 → its MD5
    import hashlib
    expected_md5 = hashlib.md5(b"4777974").hexdigest().upper()
    # Our token-generator should embed this exact MD5
    token = _generate_xyz_token(
        "4777974",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    )
    decoded = base64.b64decode(token).decode("latin-1")
    # SiteId MD5 is at position [1:33]
    assert decoded[1:33] == expected_md5


# ----- _onesite_workflowstartup_url ------------------------------------


def test_workflowstartup_url_carries_siteid_and_fresh_uuid() -> None:
    """URL template should embed SiteId and a fresh ClientSessionID."""
    u1 = _onesite_workflowstartup_url("4646505")
    u2 = _onesite_workflowstartup_url("4646505")
    assert "/workflowstartup/v1/4646505/English" in u1
    assert "BpmId=OLL.WorkflowStartUp" in u1
    assert "BpmSequence=0" in u1
    assert "LogSequence=3" in u1
    # Fresh UUID each call (avoid replay-cache poisoning)
    assert "ClientSessionID=" in u1
    assert u1 != u2  # different UUIDs


# ----- parse_onesite_workflowstartup -----------------------------------


def test_parse_workflowstartup_extracts_from_live_fixture() -> None:
    """Live HAR from www.thepointatabington.com — SiteId 4777974, 3
    floorplans (Whitman, Pembroke, plus one more)."""
    body = json.loads(
        (FIXTURES / "onesite_workflowstartup_thepointatabington.json").read_text()
    )
    units = parse_onesite_workflowstartup(body, "u")
    assert len(units) >= 2, f"expected ≥2 plans, got {len(units)}"

    names = {u["floor_plan_name"] for u in units}
    assert "Whitman" in names

    whitman = next(u for u in units if u["floor_plan_name"] == "Whitman")
    assert whitman["bedrooms"] == "2"
    assert whitman["bathrooms"] == "2"
    assert whitman["sqft"] == "1210"
    assert int(whitman["market_rent_low"]) == 2903
    assert int(whitman["market_rent_high"]) == 4240
    assert whitman["extraction_tier"] == "TIER_1_API_ONESITE_WORKFLOW"
    # AvailableUnits surfaces as available_units string
    assert whitman.get("available_units") == "1"


def test_parse_workflowstartup_strict_pass_units() -> None:
    """Every emitted unit from the live fixture should be strict-pass
    (rent + sqft both present)."""
    body = json.loads(
        (FIXTURES / "onesite_workflowstartup_thepointatabington.json").read_text()
    )
    units = parse_onesite_workflowstartup(body, "u")
    strict = sum(
        1 for u in units
        if u.get("market_rent_low") and u.get("sqft")
    )
    assert strict >= 2, f"expected ≥2 strict-pass, got {strict}/{len(units)}"


def test_parse_workflowstartup_skips_zero_rent_zero_sqft_rows() -> None:
    """Income-restricted/call-for-pricing rows have rent=0 AND sqft=0
    — skip them so they don't trip the validity gate as bare names."""
    body = {
        "Workflow": {
            "ActivityGroups": [{
                "GroupActivities": [{
                    "__type": "FloorplanSearchLeaseMgmtActivity",
                    "Floorplans": [
                        {
                            "Id": "1",
                            "Name": "Real Plan",
                            "Bedrooms": 1,
                            "Bathrooms": 1,
                            "Squarefeet": 700,
                            "MinPriceRange": 1500,
                            "MaxPriceRange": 1500,
                            "AvailableUnits": 2,
                        },
                        {
                            "Id": "2",
                            "Name": "Empty Plan",
                            "Bedrooms": 2,
                            "Bathrooms": 2,
                            "Squarefeet": 0,
                            "MinPriceRange": 0,
                            "MaxPriceRange": 0,
                            "AvailableUnits": 0,
                        },
                    ],
                }]
            }]
        }
    }
    units = parse_onesite_workflowstartup(body, "u")
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "Real Plan"


def test_parse_workflowstartup_dedupes_repeated_floorplan_ids() -> None:
    """The endpoint can repeat a floorplan across multiple Activity
    groups — dedupe on Id to avoid double-counting."""
    body = {
        "Workflow": {
            "ActivityGroups": [
                {
                    "GroupActivities": [{
                        "Floorplans": [{
                            "Id": "X", "Name": "A", "Bedrooms": 1, "Bathrooms": 1,
                            "Squarefeet": 700, "MinPriceRange": 1500, "MaxPriceRange": 1500,
                        }],
                    }]
                },
                {
                    "GroupActivities": [{
                        "Floorplans": [{
                            "Id": "X", "Name": "A", "Bedrooms": 1, "Bathrooms": 1,
                            "Squarefeet": 700, "MinPriceRange": 1500, "MaxPriceRange": 1500,
                        }],
                    }]
                },
            ]
        }
    }
    units = parse_onesite_workflowstartup(body, "u")
    assert len(units) == 1


def test_parse_workflowstartup_handles_missing_workflow_envelope() -> None:
    """Defensive parse — missing/null Workflow envelope returns []."""
    assert parse_onesite_workflowstartup({}, "u") == []
    assert parse_onesite_workflowstartup({"Workflow": None}, "u") == []
    assert parse_onesite_workflowstartup({"Workflow": {"ActivityGroups": None}}, "u") == []


def test_parse_workflowstartup_falls_back_min_squarefeet() -> None:
    """Some Floorplan entries use ``MinSquareFeet`` instead of
    ``Squarefeet`` — parser must check both."""
    body = {
        "Workflow": {"ActivityGroups": [{"GroupActivities": [{"Floorplans": [
            {
                "Id": "Y", "Name": "B",
                "Bedrooms": 1, "Bathrooms": 1.5,
                "MinSquareFeet": 850,  # not "Squarefeet"
                "MinPriceRange": 1800, "MaxPriceRange": 1800,
            }
        ]}]}]}
    }
    units = parse_onesite_workflowstartup(body, "u")
    assert len(units) == 1
    assert units[0]["sqft"] == "850"
    assert units[0]["bathrooms"] == "1.5"


# ----- residue fixes (deep-probe 2026-05-25) ---------------------------
# Three patterns surfaced on 195 TIER_1_API_ONESITE_WORKFLOW props:
#   • huge-rent-range (Bridgewater 1630-9004 leak)
#   • sqft cascade gap (Max* keys missed → -1)
#   • placeholder rows leaking when AvailableUnits=0 + rent=0


def test_collapse_huge_rent_range_spread_above_5k_clamps_to_low() -> None:
    """Bridgewater 'Cumberland': $1630-$9004 spread is short-term-lease leak.
    Collapse to (1630, 1630, True)."""
    lo, hi, clamped = _collapse_huge_rent_range(1630, 9004)
    assert (lo, hi, clamped) == (1630, 1630, True)


def test_collapse_huge_rent_range_ratio_above_3_clamps_to_low() -> None:
    """Even with a smaller absolute spread, a >3x ratio is implausible
    for the same floorplan within a single property — clamp."""
    lo, hi, clamped = _collapse_huge_rent_range(800, 2700)  # 3.375x, spread 1900
    assert clamped is True
    assert (lo, hi) == (800, 800)


def test_collapse_huge_rent_range_normal_spread_unchanged() -> None:
    """Reserve at City Place 'A1' $1169-$1283: 9.8% spread, legitimate
    in-property variation. Leave it alone."""
    lo, hi, clamped = _collapse_huge_rent_range(1169, 1283)
    assert (lo, hi, clamped) == (1169, 1283, False)


def test_collapse_huge_rent_range_at_threshold_5000_inclusive() -> None:
    """Boundary: exactly $5000 spread (with ratio under 3.0) is allowed;
    just-over is clamped. Use 4000→9000 (ratio 2.25) so the spread —
    not the ratio — is the only check that fires."""
    lo_eq, hi_eq, c_eq = _collapse_huge_rent_range(4000, 9000)  # spread = 5000, ratio 2.25
    assert (lo_eq, hi_eq, c_eq) == (4000, 9000, False)
    lo_gt, hi_gt, c_gt = _collapse_huge_rent_range(4000, 9001)  # spread = 5001
    assert (lo_gt, hi_gt, c_gt) == (4000, 4000, True)


def test_collapse_huge_rent_range_swaps_inverted_inputs() -> None:
    """If the high field happened to be < the low field, swap before
    checking spread/ratio so the clamp logic is well-defined."""
    lo, hi, clamped = _collapse_huge_rent_range(9004, 1630)
    assert (lo, hi, clamped) == (1630, 1630, True)


def test_collapse_huge_rent_range_handles_none_and_zero() -> None:
    """``None`` rents pass through; zero rents pass through unchanged."""
    assert _collapse_huge_rent_range(None, 1500) == (None, 1500, False)
    assert _collapse_huge_rent_range(1500, None) == (1500, None, False)
    assert _collapse_huge_rent_range(0, 1500) == (0, 1500, False)


def test_parse_workflowstartup_clamps_huge_rent_range_from_bridgewater_live() -> None:
    """Live HAR from www.thebridgewaterapartments.com (pid=245526, SiteId
    3590166, 2026-05-25 probe). 3 of 6 plans have $1630-$8687+ ranges —
    short-term-lease leak in OneSite OLL. Post-fix: rent_high clamped
    to rent_low for those plans, normal-spread plans untouched."""
    body = json.loads(
        (FIXTURES / "onesite_workflowstartup_bridgewater.json").read_text()
    )
    units = parse_onesite_workflowstartup(body, "u")
    by_name = {u["floor_plan_name"]: u for u in units}

    # Cumberland: $1630-$9004 → clamp to (1630, 1630)
    cumberland = by_name["The Cumberland"]
    assert int(cumberland["market_rent_low"]) == 1630
    assert int(cumberland["market_rent_high"]) == 1630
    # Holton: $1630-$8687 → clamp
    holton = by_name["The Holton"]
    assert int(holton["market_rent_low"]) == 1630
    assert int(holton["market_rent_high"]) == 1630
    # Medora: $1680-$8914 → clamp
    medora = by_name["The Medora"]
    assert int(medora["market_rent_low"]) == 1680
    assert int(medora["market_rent_high"]) == 1680
    # Beeson: $1509-$3625 → 2.4x ratio, spread 2116 → UNTOUCHED (normal)
    beeson = by_name["The Beeson"]
    assert int(beeson["market_rent_low"]) == 1509
    assert int(beeson["market_rent_high"]) == 3625


def test_parse_workflowstartup_emits_six_plans_from_bridgewater_live() -> None:
    """No plan should be dropped by the placeholder guard on Bridgewater:
    all 6 plans have either rent>0 or sqft>0 (or both)."""
    body = json.loads(
        (FIXTURES / "onesite_workflowstartup_bridgewater.json").read_text()
    )
    units = parse_onesite_workflowstartup(body, "u")
    assert len(units) == 6


def test_parse_workflowstartup_widens_sqft_cascade_to_max_keys() -> None:
    """Some floorplans publish only the upper bound. Cascade must accept
    ``MaxSquareFeet`` / ``MaxUnitSquareFeet`` when the lower bounds are 0,
    so the row doesn't fall through to sqft=-1 downstream."""
    body = {
        "Workflow": {"ActivityGroups": [{"GroupActivities": [{"Floorplans": [
            {
                "Id": "max-only-1", "Name": "Plan MaxSqft",
                "Bedrooms": 1, "Bathrooms": 1,
                "Squarefeet": 0, "MinSquareFeet": 0,
                "MaxSquareFeet": 925,  # upper bound only
                "MinPriceRange": 1400, "MaxPriceRange": 1400,
            },
            {
                "Id": "max-unit-only-2", "Name": "Plan MaxUnitSqft",
                "Bedrooms": 2, "Bathrooms": 2,
                "Squarefeet": 0, "MinSquareFeet": 0,
                "MaxSquareFeet": 0, "MinUnitSquareFeet": 0,
                "MaxUnitSquareFeet": 1180,  # deepest fallback
                "MinPriceRange": 1900, "MaxPriceRange": 1900,
            },
        ]}]}]}
    }
    units = parse_onesite_workflowstartup(body, "u")
    assert len(units) == 2
    by_name = {u["floor_plan_name"]: u for u in units}
    assert by_name["Plan MaxSqft"]["sqft"] == "925"
    assert by_name["Plan MaxUnitSqft"]["sqft"] == "1180"


def test_parse_workflowstartup_skips_placeholder_when_no_avail_and_no_rent() -> None:
    """Plans operators carry in the catalog with AvailableUnits=0 AND
    MinPriceRange=MaxPriceRange=0 are pure placeholders. Pre-fix they
    leaked into the workflow-tier zero-rent residue (e.g. Reserve at
    City Place observed 28 zero-rent rows). Drop them outright."""
    body = {
        "Workflow": {"ActivityGroups": [{"GroupActivities": [{"Floorplans": [
            {
                "Id": "real-1", "Name": "Real Plan",
                "Bedrooms": 1, "Bathrooms": 1,
                "Squarefeet": 700, "MinPriceRange": 1500, "MaxPriceRange": 1500,
                "AvailableUnits": 2,
            },
            {
                "Id": "placeholder-1", "Name": "Phantom Plan",
                "Bedrooms": 2, "Bathrooms": 2,
                "Squarefeet": 1100,  # has sqft but...
                "MinPriceRange": 0, "MaxPriceRange": 0,  # no rent
                "AvailableUnits": 0,                       # not available
            },
        ]}]}]}
    }
    units = parse_onesite_workflowstartup(body, "u")
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "Real Plan"


def test_parse_workflowstartup_keeps_zero_avail_with_priced_plan() -> None:
    """Conservative guard: when a plan has rent published but is not
    currently available (avail=0, rent>0), keep it — the price floor is
    real signal even without immediate inventory. Reserve at City Place
    has plans like A2A ($1258-$1476, avail=0) we want to keep."""
    body = {
        "Workflow": {"ActivityGroups": [{"GroupActivities": [{"Floorplans": [
            {
                "Id": "priced-no-avail", "Name": "A2A",
                "Bedrooms": 1, "Bathrooms": 1,
                "Squarefeet": 854, "MinPriceRange": 1258, "MaxPriceRange": 1476,
                "AvailableUnits": 0,
            }
        ]}]}]}
    }
    units = parse_onesite_workflowstartup(body, "u")
    assert len(units) == 1
    assert int(units[0]["market_rent_low"]) == 1258
    assert int(units[0]["market_rent_high"]) == 1476


def test_parse_workflowstartup_reserve_at_city_place_live_no_huge_range() -> None:
    """Reserve at City Place (pid=217930) had no huge-range plans in its
    live workflow body. Confirm the new clamp leaves its 14 plan rents
    untouched — guard against over-zealous clamping."""
    # Inline mini-fixture mirroring Reserve at City Place's structure
    body = {
        "Workflow": {"ActivityGroups": [{"GroupActivities": [{
            "__type": "FloorplanSearchLeaseMgmtActivity",
            "Floorplans": [
                {"Id": str(i), "Name": f"X{i}", "Bedrooms": 1, "Bathrooms": 1,
                 "Squarefeet": 700 + i, "MinPriceRange": 1169 + 50*i,
                 "MaxPriceRange": 1169 + 50*i + (100 if i % 2 else 0),
                 "AvailableUnits": (i % 3)}
                for i in range(14)
            ],
        }]}]}
    }
    units = parse_onesite_workflowstartup(body, "u")
    assert len(units) == 14
    # No clamp should fire — all spreads <= 100
    for u in units:
        assert int(u["market_rent_high"]) - int(u["market_rent_low"]) <= 100
