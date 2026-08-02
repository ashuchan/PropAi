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

import json
from types import SimpleNamespace

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.rentcafe import (
    RentCafeAdapter,
    _try_rentcafe_vanity_availableunits,
    parse_rentcafe_inline_available_units,
)
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


INLINE_AVAILABLE_UNITS_ROWS = [
    {
        "AvailableDate": "2026-08-28T00:00:00",
        "Baths": 1.0,
        "Beds": 1.0,
        "DepositCol": "$0.00",
        "FloorPlanID": 4332154,
        "FloorPlanName": "One Bedroom, One Bath",
        "IpmDisplayMinRent": 1625,
        "IpmDisplayMaxRent": 1625,
        "SquareFeet": 650,
        "UnitCode": "IB203-A",
        "UnitId": 32505942,
        "bRestrictPublish": False,
    },
    {
        "AvailableDate": "2026-09-01T00:00:00",
        "Baths": 1.0,
        "Beds": 1.0,
        "DepositCol": "$0.00",
        "FloorPlanID": 4332154,
        "FloorPlanName": "One Bedroom, One Bath",
        "IpmDisplayMinRent": 1520,
        "IpmDisplayMaxRent": 1520,
        "SquareFeet": 650,
        "UnitCode": "IB114-B",
        "UnitId": 32505929,
        "bRestrictPublish": False,
    },
]
INLINE_AVAILABLE_UNITS_HTML = (
    "<html><script>var available_units = "
    + json.dumps(INLINE_AVAILABLE_UNITS_ROWS)
    + ";</script></html>"
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


def test_parses_strict_inline_available_units_roster() -> None:
    units = parse_rentcafe_inline_available_units(
        INLINE_AVAILABLE_UNITS_HTML,
        "https://www.indigobremerton.com/floorplans",
    )
    assert [unit["unit_number"] for unit in units] == ["IB203-A", "IB114-B"]
    assert units[0]["market_rent_low"] == 1625
    assert units[0]["availability_date"] == "2026-08-28"
    assert units[0]["source_ids"] == {
        "securecafe_apartment_id": "32505942",
        "securecafe_floorplan_id": "4332154",
    }


def test_inline_roster_rejects_plan_rows_and_ambiguous_identity() -> None:
    plan_only = [
        {
            "FloorPlanID": 4332154,
            "FloorPlanName": "One Bedroom",
            "IpmDisplayMinRent": 1625,
        }
    ]
    assert parse_rentcafe_inline_available_units(
        f"<script>var available_units = {json.dumps(plan_only)};</script>",
        "https://example.test/floorplans",
    ) == []

    duplicate = [dict(row) for row in INLINE_AVAILABLE_UNITS_ROWS]
    duplicate[1]["UnitId"] = duplicate[0]["UnitId"]
    assert parse_rentcafe_inline_available_units(
        f"<script>var available_units = {json.dumps(duplicate)};</script>",
        "https://example.test/floorplans",
    ) == []


@pytest.mark.asyncio
async def test_inline_roster_wins_without_any_network_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_probe(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("inline roster must not make a network request")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", unexpected_probe)
    ctx = _ctx()
    ctx.fetch_result = SimpleNamespace(body=INLINE_AVAILABLE_UNITS_HTML)

    result = await RentCafeAdapter().extract(_FakePage(), ctx)  # type: ignore[arg-type]

    assert len(result.units) == 2
    assert result.tier_used == "TIER_1_DOM_RENTCAFE_INLINE_AVAILABLE_UNITS"
    assert result.winning_url == ctx.base_url


# ── The adapter unions the shortcut with exact plan drills ──────────────────


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
    def __init__(self, status: int, text: str, url: str = "") -> None:
        self.status_code = status
        self.text = text
        self.url = url


@pytest.mark.asyncio
async def test_availableunits_remains_a_fallback_when_no_drills_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shortcut remains useful when the exact plan surface is absent."""
    seen: list[str] = []

    def fake_probe_get(url: str, **_kw: object) -> _Resp:
        seen.append(url)
        if url.endswith("/availableunits"):
            return _Resp(200, AVAILABLEUNITS_HTML)
        if url.endswith("/floorplans"):
            return _Resp(404, "")
        raise AssertionError(f"unexpected route: {url}")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)

    adapter = RentCafeLayoutTabAdapter()
    result = await adapter.extract(_FakePage(), _ctx())  # type: ignore[arg-type]

    assert seen == [
        "https://www.oaksofnorthgatesanantonio.com/availableunits",
        "https://www.oaksofnorthgatesanantonio.com/floorplans",
    ]
    assert len(result.units) == 3
    assert result.confidence > 0.7
    assert result.winning_url.endswith("/availableunits")


@pytest.mark.asyncio
async def test_exact_plan_drills_expand_and_correct_the_shortcut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Northview/Broadway/Franklin/Jasper regression in a bounded fixture."""
    shortcut = (
        "<html><body><table>"
        + _row("219H", "Studio", "Studio", "687", "1,800.00", "Available", "1")
        + "</table></body></html>"
    )
    listing = """
    <html><body>
      <div class="page-content-floorplans floorplans-layout-tab">
        <a href="/floorplans/1br%2f1ba">1BR/1BA</a>
        <a href="/floorplans/2br%2f2ba">2BR/2BA</a>
      </div>
    </body></html>
    """
    drills = {
        "/floorplans/1br%2f1ba": (
            "<html><body>1 Bed 1 Bath 687 Sq. Ft."
            + _row("219H", "1BR/1BA", "1 Bed(s)", "687", "1,800.00", "9/1/2026", "1")
            + "</body></html>"
        ),
        "/floorplans/2br%2f2ba": (
            "<html><body>2 Beds 2 Baths 1025 Sq. Ft."
            + _row("402", "2BR/2BA", "2 Bed(s)", "1025", "2,400.00", "10/1/2026", "2")
            + "</body></html>"
        ),
    }

    def fake_probe_get(url: str, **_kw: object) -> _Resp:
        if url.endswith("/availableunits"):
            return _Resp(200, shortcut)
        if url.endswith("/floorplans"):
            return _Resp(200, listing)
        for path, body in drills.items():
            if url.endswith(path):
                return _Resp(200, body)
        return _Resp(404, "")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)

    result = await RentCafeLayoutTabAdapter().extract(  # type: ignore[arg-type]
        _FakePage(), _ctx()
    )

    assert {row["unit_number"] for row in result.units} == {"219H", "402"}
    by_unit = {row["unit_number"]: row for row in result.units}
    assert by_unit["219H"]["floor_plan_name"] == "1BR/1BA"
    assert by_unit["219H"]["bedrooms"] == "1"
    assert by_unit["219H"]["bathrooms"] == "1"
    assert by_unit["402"]["bathrooms"] == "2"
    assert result.winning_url.endswith("/floorplans/1br%2f1ba")
    assert {
        row["source_url"] for row in result.unit_source_provenance
    } == {
        "https://www.oaksofnorthgatesanantonio.com/floorplans/1br%2f1ba",
        "https://www.oaksofnorthgatesanantonio.com/floorplans/2br%2f2ba",
    }
    assert sum(
        row["unit_count"] for row in result.unit_source_provenance
    ) == 2


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


# ── General RentCafe fast-flag handoff ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_fast_flag_recovers_roster_before_broad_rentcafe_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same-origin roster is useful beyond the exact layout-tab theme.

    Its production contract is one direct request with every escalation path
    disabled, followed by an immediate strict-unit return.
    """
    monkeypatch.setenv("ENABLE_RENTCAFE_AVAILUNITS_FAST", "true")
    seen: list[tuple[str, dict[str, object]]] = []

    def fake_probe_get(url: str, **kw: object) -> _Resp:
        seen.append((url, kw))
        if url.endswith("/availableunits"):
            return _Resp(200, AVAILABLEUNITS_HTML, url)
        raise AssertionError(f"broad fallback ran after roster win: {url}")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)

    result = await RentCafeAdapter().extract(_FakePage(), _ctx())  # type: ignore[arg-type]

    assert len(result.units) == 3
    assert result.tier_used == "TIER_1_DOM_RENTCAFE_AVAILABLEUNITS_ROSTER"
    assert [url for url, _ in seen] == [
        "https://www.oaksofnorthgatesanantonio.com/availableunits"
    ]
    kwargs = seen[0][1]
    assert kwargs.get("unlocker") is False
    assert kwargs.get("proxies") == {}
    assert kwargs.get("retries") == 1


@pytest.mark.asyncio
async def test_fast_roster_declines_cross_host_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A portfolio/sibling redirect must not become this property's units."""

    def fake_probe_get(url: str, **_kw: object) -> _Resp:
        return _Resp(
            200,
            AVAILABLEUNITS_HTML,
            "https://sibling-property.example/availableunits",
        )

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)
    recovered = await _try_rentcafe_vanity_availableunits(
        _ctx(),
        result=AdapterResult(),
    )
    assert recovered is None


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_status", [403, 429])
async def test_fast_roster_uses_hyperbrowser_only_for_transient_block(
    monkeypatch: pytest.MonkeyPatch,
    blocked_status: int,
) -> None:
    """A clean HB raw fetch may recover the exact blocked same-origin route.

    The distinct tier is required so canary yield and cost can be measured
    without conflating the direct and residential-browser lanes.
    """
    monkeypatch.setenv("FETCH_BACKEND", "hyperbrowser")
    hb_calls: list[tuple[str, str, bool]] = []

    def fake_probe_get(url: str, **_kw: object) -> _Resp:
        return _Resp(blocked_status, "", url)

    async def fake_hb_raw_get(
        url: str,
        property_id: str,
        *,
        same_origin_only: bool = False,
    ) -> tuple[int, str]:
        hb_calls.append((url, property_id, same_origin_only))
        return 200, AVAILABLEUNITS_HTML

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)
    monkeypatch.setattr(
        "ma_poc.fetch.hyperbrowser_backend.hb_raw_get",
        fake_hb_raw_get,
    )

    recovered = await _try_rentcafe_vanity_availableunits(_ctx(), AdapterResult())

    assert recovered is not None
    assert len(recovered.units) == 3
    assert recovered.tier_used.endswith("_HYPERBROWSER")
    assert hb_calls == [
        (
            "https://www.oaksofnorthgatesanantonio.com/availableunits",
            "P_OAKS",
            True,
        )
    ]


@pytest.mark.asyncio
async def test_fast_roster_never_uses_hyperbrowser_when_backend_is_not_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FETCH_BACKEND", "brightdata")

    def fake_probe_get(url: str, **_kw: object) -> _Resp:
        return _Resp(403, "", url)

    async def unexpected_hb(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("Hyperbrowser must remain behind its explicit backend switch")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)
    monkeypatch.setattr("ma_poc.fetch.hyperbrowser_backend.hb_raw_get", unexpected_hb)

    assert await _try_rentcafe_vanity_availableunits(_ctx(), AdapterResult()) is None


@pytest.mark.asyncio
async def test_fast_roster_attempt_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_probe_get(url: str, **_kw: object) -> _Resp:
        nonlocal calls
        calls += 1
        return _Resp(404, "", url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)
    ctx = _ctx()
    assert await _try_rentcafe_vanity_availableunits(
        ctx, AdapterResult()
    ) is None
    assert await _try_rentcafe_vanity_availableunits(
        ctx, AdapterResult()
    ) is None
    assert calls == 1


# ── SecureCafe portal fallback (404/403 on the vanity route) ────────────────

_SC_PORTAL_HTML = (
    # Cut verbatim from a live fetch of
    # modaatthehill.securecafe.com/onlineleasing/moda-at-the-hill/availableunits.aspx
    # (HTTP 200, 111KB, 20 AvailUnitRow rows). Trimmed to the first floor-plan
    # header + its first row, which still parses to exactly 1 unit.
    "<html><body><table><caption>"
    'Floor Plan: The Campbell - 2 Bedrooms, 1 Bathroom</caption><thead><tr><th class=\'table-header tcolumn text-left\' data-label=\'Apartment\' scope=\'col\'>Apartment</th><th class=\'table-header tcolumn text-center\' data-label=\'Sq. Ft.\' scope=\'col\'><span>Sq.Ft.</span></th><th class=\'table-header tcolumn text-center\' data-label=\'Rent\' scope=\'col\'>Rent</th><th class=\'table-header tcolumn text-center\' data-label=\'Availability\' scope=\'col\'>Date Available</th><th class=\'table-header tcolumn text-center\' data-label=\'Action\' scope=\'col\'>Action</th></tr></thead><tbody><tr class=\'AvailUnitRow\'  data-selenium-id=\'urow1\' id=\'unitrow_32163017\' scope=\'row\' FloorPlateID=\'0\' ><th class=\'text-left\' data-selenium-id=\'Apt1\' id=\'32163017\' data-label=\'Apartment\'>#311</th><td class=\'text-center\' data-selenium-id=\'SqFt1\' data-label=Sq.Ft.>855</td><td  data-selenium-id=\'Rent1\'  style="" class=\'text-center\' data-label=\'Rent\'>$2,670-$7,055</td><td data-selenium-id=\'AvailDate1\' data-label=\'Date Available\'><span class=\'text-success\'>Available</span></td><td class=\'text-center\' \' data-selenium-id=\'Action1\' data-label=\'Action\'><input type="button" data-selenium-id=\'btnUnitSelect1\' class="UnitSelect btn btn-primary" id="311" value = "Select" aria-describedby="32163017" onclick=SetTermsUrl(\'rentaloptions.aspx?UnitID=32163017&FloorPlanID=4285110&myOlePropertyid=1471013&MoveInDate=7/26/2026\')></td></tr>'
    "</table></body></html>"
)


@pytest.mark.asyncio
async def test_securecafe_portal_recovers_a_404_vanity_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """When {origin}/availableunits 404s, the same roster is usually mounted at
    {sub}.securecafe.com/onlineleasing/{slug}/availableunits.aspx.

    Live-measured 2026-07-25 on the plan-level cohort: the vanity route covers
    70% of the 571 SecureCafe properties; this fallback recovered 8 of the 12
    sampled failures (Moda at the Hill 20 units, Mihir Taylor 33, Alicante 26,
    Grove Parkview 17, The Vue 12, Arlington West 11, Marina Key 7, Oak Creek
    6). Together: 36 of 40 — 90% of the block.
    """
    seen: list[str] = []
    listing = (
        '<html><body><a href="https://demo.securecafe.com/onlineleasing/demo-props/'
        'oleapplication.aspx">Apply Now</a></body></html>'
    )

    def fake_probe_get(url: str, **_kw: object) -> _Resp:
        seen.append(url)
        if url.endswith("/availableunits"):
            return _Resp(404, "")
        if url.endswith("/floorplans"):
            return _Resp(200, listing)
        if "securecafe.com" in url and url.endswith("availableunits.aspx"):
            return _Resp(200, _SC_PORTAL_HTML)
        return _Resp(404, "")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)

    result = await RentCafeLayoutTabAdapter().extract(_FakePage(), _ctx())  # type: ignore[arg-type]

    assert any("securecafe.com" in u for u in seen), "portal was never tried"
    assert len(result.units) == 1
    u = result.units[0]
    # Real values from the Moda at the Hill portal page.
    assert u["unit_number"] == "311"
    assert u["floor_plan_name"] == "The Campbell"
    assert u["sqft"] == "855"
    # Internal keys at this layer; the v2 formatter maps them to rent_low/high.
    assert u["market_rent_low"] == 2670
    assert u["market_rent_high"] == 7055
    assert u["extraction_tier"] == "TIER_1_API_RENTCAFE_SECURECAFE"
    assert result.winning_url.endswith("availableunits.aspx")


@pytest.mark.asyncio
async def test_portal_is_not_tried_when_the_vanity_route_worked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cost guard: the vanity route already covers ~70% of this cohort. Firing
    the portal anyway would add a wasted request on ~400 properties."""
    seen: list[str] = []

    def fake_probe_get(url: str, **_kw: object) -> _Resp:
        seen.append(url)
        if url.endswith("/availableunits"):
            return _Resp(200, AVAILABLEUNITS_HTML)
        return _Resp(404, "")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", fake_probe_get)
    result = await RentCafeLayoutTabAdapter().extract(_FakePage(), _ctx())  # type: ignore[arg-type]

    assert len(result.units) == 3
    assert not any("securecafe.com" in u for u in seen)
