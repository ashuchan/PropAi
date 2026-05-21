"""RentCafe modern-theme adapter (2026-05-21, HAR-validation greenfield).

DOM contract captured live from:
  - www.thefrankestate.com (9 plans, 11 unit-containers)
  - www.somaresidences.com/Floor-Plans.aspx (6 plans, 9 unit-containers)

Both sites publish RentCafe-managed inventory inline (no SecureCafe
portal hop) using the same DOM relationship:
  .floorplan-block#floorplan_{ID}[data-rent, data-floorplan-name, ...]
  .par-units#par_{ID} > .unit-container × N
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.rentcafe_unit_roster import (
    RentCafeUnitRosterAdapter,
    _parse_unit_text,
    parse_rentcafe_unit_roster,
)
from ma_poc.pms.detector import detect_pms


# ── live-captured unit-container text samples ──────────────────────
# thefrankestate sample unit (verified live 2026-05-21):
_FRANK_UNIT_TEXT = (
    "Unit #08-7 750 sqft Available: NOW Lease Term: 12 "
    "See Unit Amenities Starting At: $1,694 Lease Now "
    "Stainless Steel Appliances Hardwood-Style Vinyl Flooring"
)
# somaresidences sample unit (verified live 2026-05-21):
_SOMA_UNIT_TEXT = (
    "Unit #333 275 sqft Available: 06/13/2026 Lease Term: 13 "
    "See Unit Amenities Starting At: $2,724 Lease Now "
    "Disposal Microwave Refrigerator Bamboo Courtyard View"
)


# ── _parse_unit_text tests ─────────────────────────────────────────


def test_parse_unit_text_frankestate_now_availability() -> None:
    """thefrankestate sample: Unit #08-7 / 750 sqft / Available NOW /
    12-mo lease / $1,694."""
    parsed = _parse_unit_text(_FRANK_UNIT_TEXT)
    assert parsed["unit_number"] == "08-7"
    assert parsed["sqft"] == "750"
    assert parsed["availability_status"] == "AVAILABLE"
    assert parsed["availability_date"] == ""  # "NOW" → empty date
    assert parsed["lease_term"] == "12"
    assert parsed["rent"] == "1694"


def test_parse_unit_text_somaresidences_iso_date() -> None:
    """somaresidences sample: Unit #333 / 275 sqft / 06/13/2026 / 13-mo
    lease / $2,724."""
    parsed = _parse_unit_text(_SOMA_UNIT_TEXT)
    assert parsed["unit_number"] == "333"
    assert parsed["sqft"] == "275"
    assert parsed["availability_status"] == "AVAILABLE"
    assert parsed["availability_date"] == "06/13/2026"
    assert parsed["lease_term"] == "13"
    assert parsed["rent"] == "2724"


def test_parse_unit_text_robust_to_extra_whitespace() -> None:
    """The unit text walker uses regex finds; tolerate verbose
    inner text with extra whitespace and amenity list dumps."""
    text = "\n  Unit  #A1   \n950 sqft   Available:  August 15, 2026\n  Lease Term: 24  $3,150 "
    parsed = _parse_unit_text(text)
    assert parsed["unit_number"] == "A1"
    assert parsed["sqft"] == "950"
    assert "August 15, 2026" in parsed["availability_date"] or parsed["availability_date"].startswith("August 15")
    assert parsed["lease_term"] == "24"
    assert parsed["rent"] == "3150"


# ── parse_rentcafe_unit_roster tests ───────────────────────────────


def test_parse_full_payload_unit_level_rows() -> None:
    """Full end-to-end: pass a payload mirroring what the DOM JS returns,
    confirm unit-level rows with plan metadata joined."""
    plans = [
        {
            "id": "8635342",
            "data": {
                "floorplanName": "1x1 Stella",
                "bed": "1",
                "bath": "1",
                "sqft": "750",
                "rent": "1701.0",
                "numunits": "10",
                "building": "N/A",
            },
            "units": [
                {"id": "unit-13394691", "text": _FRANK_UNIT_TEXT},
                {"id": "unit-13394692",
                 "text": "Unit #08-9 750 sqft Available: 07/01/2026 Lease Term: 12 Starting At: $1,725 Lease Now"},
            ],
        },
    ]
    rows = parse_rentcafe_unit_roster(plans, "u")
    assert len(rows) == 2
    r0 = rows[0]
    assert r0["floor_plan_name"] == "1x1 Stella"
    assert r0["unit_number"] == "08-7"
    assert r0["bedrooms"] == "1"
    assert r0["bathrooms"] == "1"
    assert r0["sqft"] == "750"
    assert r0["market_rent_low"] == 1694
    assert r0["availability_status"] == "AVAILABLE"
    assert r0["availability_date"] == ""
    assert r0["building"] == ""  # "N/A" → empty
    assert r0["extraction_tier"] == "TIER_1_DOM_RENTCAFE_UR"

    r1 = rows[1]
    assert r1["unit_number"] == "08-9"
    assert r1["market_rent_low"] == 1725
    assert r1["availability_date"] == "07/01/2026"


def test_parse_handles_studio_bed_marker() -> None:
    """RentCafe ``data-bed="S"`` means studio → bedrooms="0"."""
    plans = [
        {
            "id": "1636818",
            "data": {
                "floorplanName": "Studio",
                "bed": "S",  # studio
                "bath": "1",
                "sqft": "275",
                "rent": "2273.0",
                "numunits": "5",
                "building": "1",
            },
            "units": [
                {"id": "u1", "text": _SOMA_UNIT_TEXT},
            ],
        },
    ]
    rows = parse_rentcafe_unit_roster(plans, "u")
    assert len(rows) == 1
    assert rows[0]["bedrooms"] == "0"  # "S" maps to studio = 0 beds
    assert rows[0]["building"] == "1"  # non-N/A building preserved


def test_parse_plan_with_no_units_emits_plan_level_row() -> None:
    """Plan without an associated .par-units roster (or empty roster)
    still surfaces a plan-level fallback row using the plan-card data."""
    plans = [
        {
            "id": "999",
            "data": {
                "floorplanName": "2x2 Phantom",
                "bed": "2",
                "bath": "2",
                "sqft": "1100",
                "rent": "2199.0",
                "numunits": "0",
                "building": "",
            },
            "units": [],
        },
    ]
    rows = parse_rentcafe_unit_roster(plans, "u")
    assert len(rows) == 1
    assert rows[0]["unit_number"] == ""  # plan-level
    assert rows[0]["floor_plan_name"] == "2x2 Phantom"
    assert rows[0]["market_rent_low"] == 2199
    assert rows[0]["bedrooms"] == "2"


def test_parse_skips_unit_with_no_id_and_no_rent_anywhere() -> None:
    """Unit-container with no Unit# AND no rent (even from the plan card
    fallback) is skipped — would emit a meaningless empty row otherwise."""
    plans = [
        {
            "id": "x",
            "data": {"floorplanName": "Empty", "bed": "1", "bath": "1", "sqft": "500", "rent": ""},  # NO plan rent
            "units": [
                {"id": "u-junk", "text": "Lease Term: 12 See Unit Amenities Lease Now"},  # no Unit# no $
            ],
        },
    ]
    rows = parse_rentcafe_unit_roster(plans, "u")
    # Junk unit was skipped because no rent can be recovered from anywhere.
    assert rows == []


def test_parse_falls_back_to_plan_rent_when_unit_text_incomplete() -> None:
    """When a unit-container's text lacks an explicit $rent but the plan
    card carries ``data-rent``, the row falls back to plan rent rather
    than dropping the unit. This keeps incomplete-but-real unit rows in
    the output."""
    plans = [
        {
            "id": "x",
            "data": {"floorplanName": "1x1", "bed": "1", "bath": "1", "sqft": "500", "rent": "1500.0"},
            "units": [
                # Has Unit# but no $rent in the text — should still emit a row.
                {"id": "u-partial", "text": "Unit #A1 500 sqft Available: NOW Lease Term: 12"},
            ],
        },
    ]
    rows = parse_rentcafe_unit_roster(plans, "u")
    assert len(rows) == 1
    assert rows[0]["unit_number"] == "A1"
    assert rows[0]["market_rent_low"] == 1500  # fell back to plan card data-rent


# ── adapter end-to-end + registry ───────────────────────────────────


class _FakePage:
    def __init__(self, payload, url="https://www.thefrankestate.com/"):
        self._payload = payload
        self.url = url

    async def evaluate(self, _js):
        return self._payload


@pytest.mark.asyncio
async def test_adapter_returns_units_on_real_payload() -> None:
    payload = {
        "ok": True,
        "plans": [
            {
                "id": "8635342",
                "data": {
                    "floorplanName": "1x1 Stella",
                    "bed": "1", "bath": "1", "sqft": "750",
                    "rent": "1701.0", "numunits": "10", "building": "N/A",
                },
                "units": [{"id": "u1", "text": _FRANK_UNIT_TEXT}],
            },
        ],
    }
    ctx = AdapterContext(
        base_url="https://www.thefrankestate.com/",
        detected=detect_pms("https://www.thefrankestate.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await RentCafeUnitRosterAdapter().extract(_FakePage(payload), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_RENTCAFE_UR"
    assert len(result.units) == 1
    assert result.units[0]["unit_number"] == "08-7"
    assert result.units[0]["market_rent_low"] == 1694
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_adapter_bails_when_no_par_units_on_page() -> None:
    """Adapter must NOT fire on a page that has .floorplan-block but no
    .par-units (e.g. Market Apartments Template A) — the DOM JS guard
    returns ok:false to prevent cross-routing."""
    payload = {"ok": False, "reason": "no .par-units roster present"}
    ctx = AdapterContext(
        base_url="https://x.test/",
        detected=detect_pms("https://x.test/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await RentCafeUnitRosterAdapter().extract(_FakePage(payload), ctx)  # type: ignore[arg-type]
    assert result.confidence == 0.0
    assert "no .par-units" in " ".join(result.errors)


@pytest.mark.asyncio
async def test_adapter_bails_when_zero_plans() -> None:
    payload = {"ok": True, "plans": []}
    ctx = AdapterContext(
        base_url="https://x.test/",
        detected=detect_pms("https://x.test/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await RentCafeUnitRosterAdapter().extract(_FakePage(payload), ctx)  # type: ignore[arg-type]
    assert result.confidence == 0.0
    assert "zero plans" in " ".join(result.errors)


def test_detector_routes_when_all_three_selectors_present() -> None:
    """Detector must yield rentcafe_unit_roster when .floorplan-block,
    .par-units, AND .unit-container are all in the HTML body."""
    from ma_poc.pms.detector import _iter_html_markers
    html = """
    <html><body>
      <div class="floorplan-block" id="floorplan_123" data-rent="1500" data-bed="1">x</div>
      <div class="par-units" id="par_123">
        <div class="unit-container">Unit #1 500 sqft Available: NOW Starting At: $1,500</div>
      </div>
    </body></html>
    """
    markers = list(_iter_html_markers(html.lower()))
    assert any(m[0] == "rentcafe_unit_roster" for m in markers), markers


def test_detector_does_NOT_route_when_par_units_missing() -> None:
    """If .floorplan-block is present but .par-units is missing (e.g.
    Market Apartments Template A), this adapter must NOT route. The MA
    adapter handles that case via its own .floorplan-unit-single guard."""
    from ma_poc.pms.detector import _iter_html_markers
    html = """
    <html><body>
      <div class="floorplan-block" data-bedrooms="1">x</div>
      <div class="floorplan-unit-single" data-when="2026-05-21">y</div>
    </body></html>
    """
    markers = list(_iter_html_markers(html.lower()))
    ur_markers = [m for m in markers if m[0] == "rentcafe_unit_roster"]
    assert not ur_markers, (
        f"rentcafe_unit_roster must NOT fire when .par-units is missing; got {ur_markers}"
    )


def test_adapter_registered() -> None:
    adapter = get_adapter("rentcafe_unit_roster")
    assert isinstance(adapter, RentCafeUnitRosterAdapter)


def test_strategy_is_dom_first() -> None:
    from ma_poc.pms.detector import _STRATEGY_BY_PMS
    assert _STRATEGY_BY_PMS["rentcafe_unit_roster"] == "dom_first"
