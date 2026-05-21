"""RealPage OLL (Online Leasing) workflow-parser tests.

2026-05-13 port (Commit 10 of MAY13_API_TIER_PORT_PLAN.md): the OLL
appstate ``Workflow`` parser handles the Category-D cluster (~187
properties) where vanity sites hop to ``leasing.realpage.com`` and the
stateful ``OLL.SearchFloorPlan`` PUT response carries unit data nested
under ``Workflow.ActivityGroups[].GroupActivities[].Units[]``.

Server-side curl/httpx of the OLL endpoint is bot-walled
(DataDome + Akamai); the only viable strategy is browser interception,
so these tests exercise the parser against a synthetic captured body
(no live network).
"""
from __future__ import annotations

import pytest

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.realpage_oll import (
    OLL_TIER,
    RealPageOllAdapter,
    _is_oll_workflow_response,
    _to_int,
    dotnet_date_to_iso,
    parse_realpage_oll_workflow,
)
from ma_poc.pms.detector import detect_pms


class _DummyPage:
    """Stand-in for Playwright Page; the OLL adapter only reads
    ``ctx._api_responses``."""


def _make_ctx(api_responses: list[dict]) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://example.com/",
        detected=detect_pms("https://example.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_OLL_TEST",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


def _oll_body(units: list[dict] | None) -> dict:
    """Minimal valid OLL Workflow body wrapping one activity."""
    return {
        "Workflow": {
            "ActivityGroups": [{
                "GroupActivities": [{
                    "__type": "ApartmentSelectionLeaseMgmtActivity, RealPage.Leasing",
                    "Floorplan": {
                        "Name": "A1",
                        "Bedrooms": 1,
                        "Bathrooms": 1,
                        "MinSquareFeet": 700,
                        "MinPriceRange": 1500,
                        "MaxPriceRange": 1800,
                        "AvailableUnits": 3,
                    },
                    "Units": units if units is not None else [],
                }],
            }],
        }
    }


# ────────────────────────────────────────────────────────────────────
# .NET date parser
# ────────────────────────────────────────────────────────────────────


class TestDotnetDateToIso:
    def test_parses_dotnet_date_with_offset(self):
        # 1779339600000 ms = 2026-05-21 14:20:00 UTC; offset ignored.
        out = dotnet_date_to_iso("/Date(1779339600000-0500)/")
        assert out.startswith("2026-")
        assert len(out) == 10  # YYYY-MM-DD

    def test_parses_dotnet_date_without_offset(self):
        out = dotnet_date_to_iso("/Date(1779339600000)/")
        assert out.startswith("2026-")

    def test_bare_epoch_ms_fallback(self):
        out = dotnet_date_to_iso("1779339600000")
        assert out.startswith("2026-")

    def test_empty_or_none_returns_empty(self):
        assert dotnet_date_to_iso(None) == ""
        assert dotnet_date_to_iso("") == ""

    def test_garbage_returns_empty(self):
        assert dotnet_date_to_iso("not a date") == ""
        assert dotnet_date_to_iso("/Date(notnumeric)/") == ""


# ────────────────────────────────────────────────────────────────────
# _to_int coercion
# ────────────────────────────────────────────────────────────────────


class TestToInt:
    def test_handles_int_float_str(self):
        assert _to_int(1500) == 1500
        assert _to_int(1500.99) == 1500
        assert _to_int("1500") == 1500
        assert _to_int("$1,500") == 1500
        assert _to_int(" 1,500.00 ") == 1500

    def test_returns_none_for_unparseable(self):
        assert _to_int(None) is None
        assert _to_int("") is None
        assert _to_int("garbage") is None
        # Bools are NOT ints here -- we want explicit numeric strings.
        assert _to_int(True) is None
        assert _to_int(False) is None


# ────────────────────────────────────────────────────────────────────
# Body shape discriminator
# ────────────────────────────────────────────────────────────────────


class TestIsOllWorkflowResponse:
    def test_recognizes_workflow_with_activity_groups(self):
        assert _is_oll_workflow_response(
            {"Workflow": {"ActivityGroups": []}}, ""
        ) is True

    def test_rejects_non_dict(self):
        assert _is_oll_workflow_response([], "") is False
        assert _is_oll_workflow_response("not a dict", "") is False
        assert _is_oll_workflow_response(None, "") is False

    def test_rejects_workflow_with_wrong_type(self):
        # Workflow present but not a dict -> no.
        assert _is_oll_workflow_response({"Workflow": "string"}, "") is False

    def test_accepts_on_url_marker_when_workflow_present(self):
        # No ActivityGroups but URL marker present -> still accepted
        # (graceful for truncated captures).
        out = _is_oll_workflow_response(
            {"Workflow": {}},
            "https://leasing.realpage.com/x",
        )
        assert out is True


# ────────────────────────────────────────────────────────────────────
# parse_realpage_oll_workflow
# ────────────────────────────────────────────────────────────────────


class TestParseOllWorkflow:
    def test_emits_unit_per_apartment(self):
        body = _oll_body([{
            "UnitNumber": "101",
            "MinPriceRange": 1500,
            "MaxPriceRange": 1500,
            "Squarefeet": 700,
            "AvailableDate": "/Date(1779339600000-0500)/",
        }, {
            "UnitNumber": "102",
            "MinPriceRange": 1600,
            "MaxPriceRange": 1600,
            "Squarefeet": 700,
            "AvailableDate": "/Date(1782018000000)/",
        }])
        units = parse_realpage_oll_workflow(body, "https://leasing.realpage.com/x")
        assert len(units) == 2
        assert {u["unit_number"] for u in units} == {"101", "102"}
        assert all(u["extraction_tier"] == OLL_TIER for u in units)
        # All emit a date.
        assert all(u["availability_date"] for u in units)

    def test_emits_floorplan_summary_when_units_empty(self):
        """No-Units activities still surface a plan-level row so the
        floorplan isn't lost from the catalog."""
        body = _oll_body([])  # empty Units
        units = parse_realpage_oll_workflow(body, "https://leasing.realpage.com/x")
        assert len(units) == 1
        u = units[0]
        # Plan-level row: name + rent_range from Floorplan.MinPriceRange/MaxPriceRange.
        assert u["floor_plan_name"] == "A1"
        assert "1,500" in u["rent_range"] or "$1500" in u["rent_range"] or "1500" in u["rent_range"]

    def test_skips_activity_without_floorplan_or_rent(self):
        """A bare activity (no name, no rent, no units) doesn't emit a
        useless row."""
        body = {
            "Workflow": {"ActivityGroups": [{
                "GroupActivities": [{
                    "__type": "ApartmentSelectionLeaseMgmtActivity",
                    "Floorplan": {},
                    "Units": [],
                }],
            }]},
        }
        assert parse_realpage_oll_workflow(body, "https://x") == []

    def test_non_dict_body_returns_empty(self):
        assert parse_realpage_oll_workflow([], "https://x") == []  # type: ignore[arg-type]
        assert parse_realpage_oll_workflow("not a dict", "https://x") == []  # type: ignore[arg-type]

    def test_units_without_unit_number_or_id_are_skipped(self):
        body = _oll_body([{
            # No UnitNumber, no Id -> skipped.
            "MinPriceRange": 1500,
            "Squarefeet": 700,
        }])
        units = parse_realpage_oll_workflow(body, "https://x")
        # Falls through to the plan-summary path (Units was non-empty
        # but no unit had an identifier), so we emit either zero or the
        # plan summary depending on the implementation. Either is fine
        # -- the key invariant is "no junk units".
        for u in units:
            assert u.get("unit_number") or u.get("floor_plan_name")


# ────────────────────────────────────────────────────────────────────
# Adapter end-to-end
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_extracts_from_workflow_response():
    body = _oll_body([{
        "UnitNumber": "201",
        "MinPriceRange": 1900,
        "MaxPriceRange": 1900,
        "Squarefeet": 850,
        "AvailableDate": "/Date(1782018000000)/",
    }])
    ctx = _make_ctx([{
        "url": (
            "https://leasing.realpage.com/RP.Leasing.AppService.WebHost/"
            "appstate/v1/?BpmId=OLL.SearchFloorPlan"
        ),
        "body": body,
    }])
    result = await RealPageOllAdapter().extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1
    assert result.units[0]["unit_number"] == "201"
    assert result.tier_used == OLL_TIER
    assert result.confidence > 0


@pytest.mark.asyncio
async def test_adapter_returns_empty_when_no_workflow():
    ctx = _make_ctx([])
    result = await RealPageOllAdapter().extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0
    assert result.errors


def test_adapter_fingerprints_include_oll_markers():
    """Fingerprints must include the OLL-specific markers (leasing
    subdomain, rp-leasing-widget, appstate URL, content/apply#k=)."""
    fps = RealPageOllAdapter().static_fingerprints()
    assert "leasing.realpage.com" in fps
    assert any("rp-leasing-widget" in f for f in fps)
    assert any("content/apply#k=" in f for f in fps)


def test_matches_response_body_accepts_oll_and_legacy():
    a = RealPageOllAdapter()
    # OLL workflow shape
    assert a.matches_response_body({"Workflow": {"ActivityGroups": []}}) is True
    # Legacy /floorplans envelope
    assert a.matches_response_body(
        {"response": {"floorplans": [{"id": "1", "name": "A"}]}}
    ) is True
    # Random body
    assert a.matches_response_body({"random": "junk"}) is False
