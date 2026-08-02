"""Edifice CMS adapter — parser + detector wiring tests.

Acceptance (canary 1ef1060 regression #10, user-flagged):
- Detector routes Edifice CMS HTML marker → pms="edificecms" with
  confidence 0.92 (above ResMan's 0.90).
- Property UUID recoverable from raw HTML (no browser).
- ``/floorplans?action=get_floorplans&property_id=`` envelope → per-plan
  iteration; ``/units?action=getunits&u=`` per-plan roster → one
  unit-level row per ``UnitID``.
- Plans with ``UnitsAvailable == 0`` emit one plan-level summary row.
- ``"999.00"``, ``999``, ``"$1,340"`` all → int.
- ``Availability.MadeReadyDate`` (MM/DD/YYYY) → ISO ``YYYY-MM-DD``.
- Live-captured HTML from cobblestonephx.com routes correctly + the
  full adapter pipeline (mocked HTTP) emits 9+ unit-level rows.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import ma_poc.pms.adapters  # noqa: F401  # populate adapter registry
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.edificecms import (
    EdificeCmsAdapter,
    _avail_date,
    _avail_status,
    _rent_to_int,
    find_edificecms_property_id,
    find_edificecms_property_ids,
    parse_edificecms_plan_summary,
    parse_edificecms_units,
    select_edificecms_catalogue,
)
from ma_poc.pms.adapters.registry import get_adapter
from ma_poc.pms.detector import _detect_html_markers

FIXTURES = Path(__file__).parent / "fixtures" / "edificecms"
LIVE_HTML = (FIXTURES / "cobblestonephx_floorplans.html").read_text(encoding="utf-8")
FLOORPLANS_API = json.loads((FIXTURES / "floorplans_api.json").read_text(encoding="utf-8"))
UNITS_API = json.loads((FIXTURES / "units_api.json").read_text(encoding="utf-8"))
COBBLESTONE_UUID = "b63cc3f8-3edf-4ec9-be58-8bf4a77455bf"


def test_rent_to_int_handles_strs_ints_and_money() -> None:
    """Unit MarketRent comes as ``"999.00"``, floorplan as ``999``."""
    assert _rent_to_int("999.00") == 999
    assert _rent_to_int(1340) == 1340
    assert _rent_to_int("$1,340") == 1340
    assert _rent_to_int("$1,340.50") == 1340
    assert _rent_to_int("") is None
    assert _rent_to_int(None) is None
    assert _rent_to_int(0) is None
    assert _rent_to_int(True) is None  # bool guard


def test_property_id_from_inline_js() -> None:
    """The inline getFloorPlan() block carries the canonical UUID."""
    html = (
        '<script>function getFloorPlan(){$.ajax({url:"...",'
        'data:{action:"get_floorplans",property_id:"B63CC3F8-3EDF-4EC9-BE58-8BF4A77455BF"}})}</script>'
    )
    assert find_edificecms_property_id(html) == COBBLESTONE_UUID


def test_property_id_fallback_to_resman_apply_link() -> None:
    """When inline JS is stripped, fall back to ResMan apply link UUID."""
    html = (
        '<a href="https://lpp.myresman.com/Portal/Applicants/Availability'
        '?a=2071&p=b63cc3f8-3edf-4ec9-be58-8bf4a77455bf&moveInDate=4/30/2026">Apply</a>'
    )
    assert find_edificecms_property_id(html) == COBBLESTONE_UUID


def test_property_ids_decode_html_entities_and_preserve_candidate_order() -> None:
    first = "318beef3-c0ee-4d07-a9c7-a9624bb13238"
    second = "e7494880-99cb-4613-9de6-06812af8bbdd"
    html = (
        f'<script>const x={{property_id:"{first}"}}</script>'
        '<a href="https://lpp.myresman.com/Portal/Applicants/Availability'
        f'?a=2071&amp;p={second}&amp;moveInDate=8/1/2026">Apply</a>'
    )
    assert find_edificecms_property_ids(html) == [first, second]


def test_property_id_returns_none_when_absent() -> None:
    assert find_edificecms_property_id("") is None
    assert find_edificecms_property_id("<html>nothing</html>") is None
    # A UUID-shaped string that isn't tagged as property_id and isn't on
    # a ResMan link should NOT match — it could be a session ID.
    assert (
        find_edificecms_property_id(
            "session=11111111-2222-3333-4444-555555555555"
        )
        is None
    )


def test_property_id_recoverable_from_live_html() -> None:
    assert find_edificecms_property_id(LIVE_HTML) == COBBLESTONE_UUID


def test_avail_date_normalises_made_ready_date() -> None:
    """MM/DD/YYYY → YYYY-MM-DD; prefer MadeReadyDate over VacateDate."""
    u = {"Availability": {"VacateDate": "05/01/2026", "MadeReadyDate": "05/07/2026"}}
    assert _avail_date(u) == "2026-05-07"
    # Fallback to VacateDate when MadeReadyDate is missing.
    u2 = {"Availability": {"VacateDate": "03/24/2026"}}
    assert _avail_date(u2) == "2026-03-24"
    # AvailDate prose like "Move in Today!" returns empty.
    u3 = {"AvailDate": "Move in Today!"}
    assert _avail_date(u3) == ""
    # Single-digit month/day pad correctly.
    u4 = {"Availability": {"MadeReadyDate": "3/7/2026"}}
    assert _avail_date(u4) == "2026-03-07"


def test_avail_status_from_unit_flags() -> None:
    assert (
        _avail_status({"UnitLeasedStatus": "available", "UnitOccupancyStatus": "vacant"})
        == "AVAILABLE"
    )
    assert (
        _avail_status({"UnitLeasedStatus": "leased", "UnitOccupancyStatus": "occupied"})
        == "UNAVAILABLE"
    )
    # Vacant but not yet leased → still AVAILABLE per Edifice's API
    # (only available units show in /units/{uuid}/{plan}).
    assert _avail_status({"UnitOccupancyStatus": "vacant"}) == "AVAILABLE"
    # Missing both fields → conservative AVAILABLE (API would have
    # filtered it out otherwise).
    assert _avail_status({}) == "AVAILABLE"


def test_future_on_notice_row_in_positive_roster_is_available() -> None:
    row = {
        "UnitLeasedStatus": "on_notice",
        "UnitOccupancyStatus": "occupied",
        "Availability": {"MadeReadyDate": "09/08/2026"},
    }
    assert _avail_status(row, capture_date="2026-08-02") == "AVAILABLE"


def test_historical_on_notice_and_true_leased_rows_remain_unavailable() -> None:
    historical = {
        "UnitLeasedStatus": "on_notice",
        "UnitOccupancyStatus": "occupied",
        "Availability": {"MadeReadyDate": "07/01/2026"},
    }
    leased = {
        "UnitLeasedStatus": "leased",
        "UnitOccupancyStatus": "occupied",
        "Availability": {"MadeReadyDate": "09/08/2026"},
    }
    assert _avail_status(historical, capture_date="2026-08-02") == "UNAVAILABLE"
    assert _avail_status(leased, capture_date="2026-08-02") == "UNAVAILABLE"


def test_parse_units_preserves_future_on_notice_date_and_state() -> None:
    plan = {
        "Id": "A2",
        "Name": "A2",
        "Bedroom": 1,
        "Bathroom": 1,
        "SquareFeet": 700,
    }
    rows = parse_edificecms_units(
        plan,
        [
            {
                "UnitID": "4204",
                "MarketRent": "1650.00",
                "UnitOccupancyStatus": "occupied",
                "UnitLeasedStatus": "on_notice",
                "Availability": {"MadeReadyDate": "09/08/2026"},
            }
        ],
        "https://edificecms.com/units",
        capture_date="2026-08-02",
    )
    assert rows[0]["availability_status"] == "AVAILABLE"
    assert rows[0]["availability_date"] == "2026-09-08"


def test_catalogue_selector_prefers_aggregate_over_strict_subset() -> None:
    aggregate = {
        "property_id": "aggregate",
        "response": {"data": []},
        "identity": object(),
        "plan_ids": {"1x1", "2x2", "3x2"},
    }
    subset = {
        "property_id": "subset",
        "response": {"data": []},
        "identity": object(),
        "plan_ids": {"2x2", "3x2"},
    }
    selected, relation = select_edificecms_catalogue([aggregate, subset])
    assert selected is aggregate
    assert relation == "aggregate_over_strict_subset"


def test_catalogue_selector_rejects_noncontained_sibling_sets() -> None:
    first = {"property_id": "phase-i", "plan_ids": {"S1", "S2"}}
    sibling = {"property_id": "phase-ii", "plan_ids": {"A1", "B1"}}
    selected, relation = select_edificecms_catalogue([first, sibling])
    assert selected is None
    assert relation == "ambiguous_noncontained"


def test_parse_units_emits_unit_level_rows() -> None:
    """Each ``UnitID`` in the per-plan list → one unit-level row."""
    plan = FLOORPLANS_API["data"][0]  # A1-1X1-615
    units_list = UNITS_API["units"]["A1-1X1-615"]
    rows = parse_edificecms_units(
        plan, units_list, "https://edificecms.com/.../units?u=A1-1X1-615"
    )
    # Live fixture: 9 distinct UnitIDs for this floorplan.
    assert len(rows) == 9
    # Every row has the plan name spliced on.
    assert {r["floor_plan_name"] for r in rows} == {"1X1"}
    # UnitID becomes the canonical unit_number.
    assert {"1053", "2004", "2010", "2058", "2059", "2068", "2083", "2085", "2087"}.issubset(
        {r["unit_number"] for r in rows}
    )
    # Per-unit MarketRent ("999.00") parses to int.
    u1053 = next(r for r in rows if r["unit_number"] == "1053")
    assert u1053["market_rent_low"] == 999
    assert u1053["bedrooms"] == "1"
    assert u1053["bathrooms"] == "1"
    assert u1053["sqft"] == "615"
    assert u1053["building"] == "1"
    assert u1053["floor"] == "1"
    assert u1053["deposit"] == "300"
    assert u1053["availability_date"] == "2026-05-07"
    assert u1053["availability_status"] == "AVAILABLE"
    assert u1053["extraction_tier"] == "TIER_1_API_EDIFICECMS"
    # source_ids carries the Edifice-native unit ID + plan ID.
    assert u1053["source_ids"]["edifice_unit_id"] == "1053"
    assert u1053["source_ids"]["edifice_plan_id"] == "A1-1X1-615"


def test_parse_units_skips_rows_missing_unit_id() -> None:
    """Defensive: a unit dict with no UnitID is dropped (not demoted to
    inferred_)."""
    plan = {"Id": "A1", "Name": "1X1", "Bedroom": 1, "Bathroom": "1", "SquareFeet": 615}
    units = [{"Id": "A1", "MarketRent": "999.00"}]  # no UnitID
    assert parse_edificecms_units(plan, units, "u") == []


def test_parse_plan_summary_emits_unavailable_for_zero_availability() -> None:
    """``UnitsAvailable == 0`` + rent present → UNAVAILABLE plan-level row."""
    plan = FLOORPLANS_API["data"][1]  # A1PR-1X1-615 (avail=0)
    assert plan["UnitsAvailable"] == "0"
    row = parse_edificecms_plan_summary(plan, "u")
    assert row is not None
    assert row["availability_status"] == "UNAVAILABLE"
    assert row["floor_plan_name"] == "1X1 Partial Reno"
    assert row["bedrooms"] == "1"
    assert row["market_rent_low"] == 1150
    assert row["market_rent_high"] == 1495
    assert row["available_units"] == "0"
    # No unit_number — plan-level rows are unit-less.
    assert row["unit_number"] == ""
    assert row["extraction_tier"] == "TIER_1_API_EDIFICECMS"


def test_detector_routes_edificecms_html_marker() -> None:
    """Static HTML carrying ``edificecms.com`` → pms="edificecms" @ 0.92."""
    html = (
        '<html><head><link href="https://assets.edificecms.com/uploads/p/x.css"></head>'
        '<body><script>var BUILDER_LIVE="https://beta.edificecms.com/builder/";</script>'
        '<div class="eWidget fp-html-rent-sqft">$907 | 561sf</div></body></html>'
    ).lower()
    res = _detect_html_markers(html)
    assert res is not None
    assert res[0] == "edificecms"
    assert res[1] >= 0.92


def test_detector_prefers_edifice_over_resman_when_co_resident() -> None:
    """Edifice + ResMan apply link co-resident → Edifice wins (0.92 > 0.90)."""
    # Live HTML carries BOTH edificecms.com markers and a myresman.com
    # apply link — Edifice must outrank ResMan.
    res = _detect_html_markers(LIVE_HTML.lower())
    assert res is not None, "detector returned None on live HTML"
    assert res[0] == "edificecms", f"expected edificecms, got {res[0]}"


def test_adapter_registered() -> None:
    a = get_adapter("edificecms")
    assert isinstance(a, EdificeCmsAdapter)
    assert a.pms_name == "edificecms"
    assert a.matches_response_body(LIVE_HTML) is True
    assert a.matches_response_body("nothing here") is False
    assert a.matches_response_body(LIVE_HTML.encode("utf-8")) is True


def test_adapter_static_fingerprints() -> None:
    """Fingerprints exposed match the detector-side _HTML_FINGERPRINTS."""
    a = EdificeCmsAdapter()
    fps = a.static_fingerprints()
    assert "edificecms.com" in fps
    assert "/edi-assets/" in fps
    # static_fingerprints returns a copy (not the internal list).
    fps.append("mutation")
    assert "mutation" not in a.static_fingerprints()


# Full pipeline test: mock the two HTTP calls and assert the adapter
# emits the expected unit-level rows when given the live HTML body.
class _FakeFetchResult:
    def __init__(self, body: str) -> None:
        self.body = body
        self.final_url = "https://www.cobblestonephx.com/floorplans.php"


def _make_ctx(html: str) -> AdapterContext:
    from ma_poc.pms.detector import DetectedPMS as _DPMS

    return AdapterContext(
        base_url="https://www.cobblestonephx.com/",
        detected=_DPMS(pms="edificecms", confidence=0.92, evidence=["test"]),
        profile=None,
        expected_total_units=None,
        property_id="cobblestone_phx_test",
        fetch_result=_FakeFetchResult(html),
    )


def test_adapter_extract_emits_unit_level_rows_end_to_end() -> None:
    """Full adapter dispatch on live HTML + mocked APIs.

    Mocks both ``_fetch_json`` calls (floorplans + units) so the test
    doesn't hit the network. Asserts the adapter produces at least 9
    admitted unit-level rows (the 9 A1-1X1-615 units) plus plan-level
    summaries for zero-availability plans.
    """
    fp_calls: list[dict[str, str]] = []

    async def mock_fetch_json(url: str, params: dict[str, str]) -> dict[str, Any]:
        fp_calls.append({"url": url, **params})
        if "floorplans" in url:
            return FLOORPLANS_API
        if "units" in url and params.get("u") == "A1-1X1-615":
            return UNITS_API
        # Other plans with UnitsAvailable>0 — return empty so they fall
        # through to plan-level summary.
        return {"status": True, "units": {params.get("u", ""): []}, "data": []}

    adapter = EdificeCmsAdapter()
    ctx = _make_ctx(LIVE_HTML)
    with patch("ma_poc.pms.adapters.edificecms._fetch_json", side_effect=mock_fetch_json):
        result = asyncio.run(adapter.extract(page=None, ctx=ctx))  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_API_EDIFICECMS", (
        f"unexpected tier: {result.tier_used}; errors: {result.errors}"
    )
    # At minimum the 9 A1-1X1-615 units survive post_process.
    assert len(result.units) >= 9, f"got {len(result.units)} unit-level rows"
    # And plan-level summaries for the zero-availability floorplans.
    assert len(result.plan_summaries) > 0
    # The floorplans API was called once with the right property_id.
    fp_call = next(c for c in fp_calls if "floorplans" in c["url"])
    assert fp_call["property_id"] == COBBLESTONE_UUID


def test_adapter_extract_no_property_id_fails_cleanly() -> None:
    """HTML with Edifice marker but no UUID → NO_PROPERTY_ID error."""
    adapter = EdificeCmsAdapter()
    ctx = _make_ctx(
        "<html><body>"
        '<script>var BUILDER_LIVE="https://beta.edificecms.com/builder/";</script>'
        "no uuid here</body></html>"
    )
    result = asyncio.run(adapter.extract(page=None, ctx=ctx))  # type: ignore[arg-type]
    assert result.tier_used.endswith("_NO_PROPERTY_ID")
    assert result.units == []


def test_adapter_extract_misroute_falls_through() -> None:
    """HTML with no Edifice marker → NO_FINGERPRINT, no API calls fired."""
    adapter = EdificeCmsAdapter()
    ctx = _make_ctx("<html><body>not edifice</body></html>")
    fired = False

    async def boom(*_a: Any, **_kw: Any) -> dict[str, Any]:
        nonlocal fired
        fired = True
        return {}

    with patch("ma_poc.pms.adapters.edificecms._fetch_json", side_effect=boom):
        result = asyncio.run(adapter.extract(page=None, ctx=ctx))  # type: ignore[arg-type]
    assert result.tier_used.endswith("_NO_FINGERPRINT")
    assert fired is False, "API call fired despite missing fingerprint"


def test_adapter_verifies_each_uuid_and_selects_matching_phase() -> None:
    wrong = "318beef3-c0ee-4d07-a9c7-a9624bb13238"
    correct = "e7494880-99cb-4613-9de6-06812af8bbdd"
    html = (
        '<script>var BUILDER_LIVE="https://beta.edificecms.com/builder/";'
        f'const a={{property_id:"{wrong}"}};'
        f'const b={{property_id:"{correct}"}};</script>'
    )
    ctx = _make_ctx(html)
    ctx.property_name = "Turtle Dove I"
    ctx.address = "3516 Matilda St"
    calls: list[str] = []

    async def mock_fetch_json(url: str, params: dict[str, str]) -> dict[str, Any]:
        candidate = params.get("property_id", "")
        if "floorplans" in url:
            calls.append(candidate)
            response = dict(FLOORPLANS_API)
            response["property"] = "Turtle Dove 2" if candidate == wrong else "Turtle Dove 1"
            return response
        return {"status": True, "units": {params.get("u", ""): []}}

    with patch("ma_poc.pms.adapters.edificecms._fetch_json", side_effect=mock_fetch_json):
        result = asyncio.run(EdificeCmsAdapter().extract(page=None, ctx=ctx))  # type: ignore[arg-type]

    assert calls[:2] == [wrong, correct]
    assert result.tier_used == "TIER_1_API_EDIFICECMS"
    assert correct in (result.winning_url or "")
    assert any("PROPERTY_IDENTITY_REJECTED" in error for error in result.errors)
    assert result.unit_source_provenance
    assert result.unit_source_provenance[0]["identity"]["status"] == "MATCH"


def test_adapter_selects_newport_aggregate_instead_of_subset() -> None:
    aggregate_uuid = "a33c14d8-a587-4273-afa9-65cd7919c5d9"
    subset_uuid = "21d5cb08-2e9d-46fc-b369-70c914588ed1"
    html = (
        '<script>var BUILDER_LIVE="https://beta.edificecms.com/builder/";'
        f'const a={{property_id:"{aggregate_uuid}"}};'
        f'const b={{property_id:"{subset_uuid}"}};</script>'
    )
    ctx = _make_ctx(html)
    ctx.property_name = "Newport Village"
    calls: list[str] = []

    def plan(plan_id: str) -> dict[str, Any]:
        return {
            "Id": plan_id,
            "Name": plan_id,
            "Bedroom": 2,
            "Bathroom": 2,
            "SquareFeet": 1000,
            "MarketRent": 1500,
            "UnitsAvailable": "0",
        }

    async def mock_fetch_json(url: str, params: dict[str, str]) -> dict[str, Any]:
        candidate = params.get("property_id", "")
        if "floorplans" in url:
            calls.append(candidate)
            plan_ids = ["1x1G-nv", "2x2S-nv", "2x2-npv", "2x2G-nv", "3x2-npv"]
            if candidate == subset_uuid:
                plan_ids = ["2x2-npv", "3x2-npv"]
            return {
                "status": True,
                "property": "Newport Village",
                "data": [plan(plan_id) for plan_id in plan_ids],
            }
        raise AssertionError("zero-availability plans must not call units endpoint")

    with patch("ma_poc.pms.adapters.edificecms._fetch_json", side_effect=mock_fetch_json):
        result = asyncio.run(EdificeCmsAdapter().extract(page=None, ctx=ctx))  # type: ignore[arg-type]

    assert calls == [aggregate_uuid, subset_uuid]
    assert result.tier_used == "TIER_1_API_EDIFICECMS"
    assert aggregate_uuid in (result.winning_url or "")
    assert len(result.plan_summaries) == 5
    assert any(
        "relation=aggregate_over_strict_subset" in error for error in result.errors
    )
