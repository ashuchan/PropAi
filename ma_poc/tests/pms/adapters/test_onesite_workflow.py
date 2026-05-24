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
    _extract_onesite_site_ids,
    _onesite_workflowstartup_url,
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
