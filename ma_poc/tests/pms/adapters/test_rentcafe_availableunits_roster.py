"""RentCafe ``{origin}/availableunits`` whole-roster short-circuit (2026-07-25).

Markup below is cut verbatim from a live fetch of
``https://www.oaksofnorthgatesanantonio.com/availableunits`` (200, 205KB,
16 ``tr.unit-container`` rows) on 2026-07-25.

WHY THIS PATH EXISTS. The 2026-07-25 run left 341 properties on
``TIER_1_API_RENTCAFE_NO_RESPONSE_PLAN_LEVEL`` — floor-plan rows only, no
apartments. A 42-property live probe of that cohort found 39 recoverable and
ZERO true ceilings, and the dominant navigation step was this single URL:
``/availableunits`` server-renders the property's ENTIRE available roster in
one response.

Two properties make it worth a dedicated path rather than more drill anchors:

1. It is NOT DISCOVERABLE. The Oaks homepage exposes no ``href`` to it — the
   existing anchor-drill regex finds only ``/floorplans``. It has to be tried
   by convention.
2. It is CHEAPER AND MORE COMPLETE than the ``/floorplans`` → N-drill fan-out:
   one request instead of 1+N, and the whole roster instead of whatever subset
   the plan pages link.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.rentcafe_layout_tab import (
    RentCafeLayoutTabAdapter,
    parse_rentcafe_lt_applyga,
)
from ma_poc.pms.detector import detect_pms


def _row(unit: str, plan: str, beds: str, sqft: str, rent: str, avail: str, uid: str) -> str:
    """One ``tr.unit-container`` exactly as the live page emits it."""
    return f"""
<tr data-selenium-id="urow1" class="unit-container" id="unit-container-{uid}">
  <td class="td-card-name" data-selenium-id="Apt1">
    <p class="d-block d-lg-none td-label">Apartment:</p> #{unit}
  </td>
  <td class="td-card-sqft" data-selenium-id="Sqft1"><p class="d-block d-lg-none td-label">Sq. Ft.:</p> {sqft}</td>
  <td class="td-card-rent" data-selenium-id="Rent1">
    <p class="d-block d-lg-none td-label">Rent:</p>${rent}<span></span>
  </td>
  <td class="td-card-available" data-selenium-id="AvailDate1">
    <p class="d-block d-lg-none td-label">Date:</p>{avail}
  </td>
  <td class="td-card-footer" data-selenium-id="Action1">
    <a data-selenium-id="Select_1" type="button" class="btn btn-primary" id="{unit}"
       onclick="applyGAClick('{plan}', '{beds}', '{sqft}', '{rent}', '{rent}', '{unit}')"
       href=' https://oaksofnorthgatesanantonio.securecafe.com/onlineleasing/oaks-of-northgate/oleapplication.aspx?stepname=RentalOptions&amp;UnitID={uid}'>
      Apply Now <span class="sr-only">&nbsp;for apartment #{unit}</span>
    </a>
  </td>
</tr>"""


AVAILABLEUNITS_HTML = (
    "<html><body><table><tbody>"
    + _row("01012", "A1", "1 Bed(s)", "660", "749.00", "11/6/2026", "18028262")
    + _row("18062", "A2- VL/LI", "1 Bed(s)", "710", "764.00", "Available", "18028523")
    + _row("16021", "B1", "2 Bed(s)", "838", "1,009.00", "Available", "18028481")
    + "</tbody></table></body></html>"
)


# ── The parser reads the live roster shape ──────────────────────────────────


def test_parses_the_live_availableunits_roster() -> None:
    units = parse_rentcafe_lt_applyga(AVAILABLEUNITS_HTML, "https://x.test/availableunits")
    assert len(units) == 3
    by_num = {u["unit_number"]: u for u in units}
    assert set(by_num) == {"01012", "18062", "16021"}

    a1 = by_num["01012"]
    assert a1["floor_plan_name"] == "A1"
    assert a1["bedrooms"] == "1"
    assert a1["sqft"] == "660"
    # Rent lives on the INTERNAL keys at this layer; the v2 formatter maps
    # market_rent_low/high -> rent_low/high downstream.
    assert a1["market_rent_low"] == 749
    assert a1["market_rent_high"] == 749


def test_comma_formatted_rent_survives() -> None:
    """`1,009.00` must not truncate to 1 — the roster's larger units all use
    thousands separators."""
    units = parse_rentcafe_lt_applyga(AVAILABLEUNITS_HTML, "https://x.test/availableunits")
    b1 = next(u for u in units if u["unit_number"] == "16021")
    assert b1["market_rent_low"] == 1009
    assert b1["bedrooms"] == "2"


def test_every_row_is_unit_level_not_plan_level() -> None:
    """The whole point: real apartment numbers, not plan names. A plan-level
    row would have an empty unit_number and would keep the property in the
    RENTCAFE_NO_RESPONSE_PLAN_LEVEL bucket."""
    units = parse_rentcafe_lt_applyga(AVAILABLEUNITS_HTML, "https://x.test/availableunits")
    assert all(str(u.get("unit_number") or "").strip() for u in units)


def test_non_roster_page_yields_nothing() -> None:
    """A plain marketing page must not produce phantom rows — otherwise the
    short-circuit would swallow properties whose /availableunits 200s with a
    soft-404 body."""
    assert parse_rentcafe_lt_applyga("<html><body><h1>Floor Plans</h1></body></html>", "u") == []


# ── The adapter tries the URL, and prefers it over the drill fan-out ─────────


class _FakePage:
    """The jugnu fetch-only stub page.

    Deliberately has NO ``evaluate`` — that absence is exactly what routes
    ``extract()`` into ``_extract_code_only``, which is the path under test.
    Giving it an ``evaluate`` sends the adapter down the DOM-JS drill-walker
    instead and the roster probe never runs.
    """

    url = "https://www.oaksofnorthgatesanantonio.com/"


def _ctx() -> AdapterContext:
    return AdapterContext(
        base_url="https://www.oaksofnorthgatesanantonio.com/",
        detected=detect_pms("https://www.oaksofnorthgatesanantonio.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_OAKS",
    )


class _Resp:
    def __init__(self, status: int, text: str) -> None:
        self.status_code = status
        self.text = text


@pytest.mark.asyncio
async def test_availableunits_short_circuits_the_drill_fanout(monkeypatch: pytest.MonkeyPatch) -> None:
    """One request, whole roster, and NO per-plan drills afterwards.

    Guards the cost property as well as the data property: if this regressed
    into "fetch the roster AND still fan out", it would multiply request volume
    across 341 properties without adding a single unit.
    """
    seen: list[str] = []

    def fake_probe_get(url: str, **_kw: object) -> _Resp:
        seen.append(url)
        if url.endswith("/availableunits"):
            return _Resp(200, AVAILABLEUNITS_HTML)
        raise AssertionError(f"drill fan-out should not have run; fetched {url}")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)

    adapter = RentCafeLayoutTabAdapter()
    result = await adapter.extract(_FakePage(), _ctx())  # type: ignore[arg-type]

    assert seen == ["https://www.oaksofnorthgatesanantonio.com/availableunits"]
    assert len(result.units) == 3
    assert result.confidence > 0.7
    assert result.winning_url.endswith("/availableunits")


@pytest.mark.asyncio
async def test_falls_through_to_drills_when_roster_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 (or an empty body) must not strand the property — the existing
    /floorplans drill walk still has to run."""
    seen: list[str] = []

    def fake_probe_get(url: str, **_kw: object) -> _Resp:
        seen.append(url)
        if url.endswith("/availableunits"):
            return _Resp(404, "")
        if url.endswith("/floorplans"):
            return _Resp(200, AVAILABLEUNITS_HTML)  # roster inline on the listing
        return _Resp(404, "")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)

    result = await RentCafeLayoutTabAdapter().extract(_FakePage(), _ctx())  # type: ignore[arg-type]

    assert seen[0].endswith("/availableunits"), "roster must be tried FIRST"
    assert any(u.endswith("/floorplans") for u in seen), "must still fall through"
    assert len(result.units) == 3


@pytest.mark.asyncio
async def test_probe_exception_is_never_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Network failure on the roster probe degrades to the drill path."""
    def fake_probe_get(url: str, **_kw: object) -> _Resp:
        if url.endswith("/availableunits"):
            raise OSError("connection reset")
        return _Resp(200, AVAILABLEUNITS_HTML)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)

    result = await RentCafeLayoutTabAdapter().extract(_FakePage(), _ctx())  # type: ignore[arg-type]
    assert len(result.units) == 3
