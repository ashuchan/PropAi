"""Wix floor-plans adapter (2026-05-21, HAR-validation greenfield).

Plan-card text captured live from
www.liveatarcos.com/phoenix-apartment-floor-plans — 3 Wix component
cards, each with the same templated text pattern.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters import wix_floor_plans as wix_module
from ma_poc.pms.adapters.base import VERIFIED_PLAN_ONLY_SURFACE_KEY, AdapterContext
from ma_poc.pms.adapters.wix_floor_plans import (
    WixFloorPlansAdapter,
    _bounded_card_texts,
    _discover_same_site_plan_links,
    _extract_labeled_3dplans_links,
    _extract_plan_codes,
    _HtmlSource,
    _MapCapture,
    _MapLink,
    _parse_3dplans_units,
    _parse_card_text,
    _split_plan_title_and_code,
    extract_wix_authored_plan_rows,
    parse_wix_floor_plans,
)
from ma_poc.pms.adapters.wix_nopms import WixNoPmsAdapter
from ma_poc.pms.detector import detect_pms
from ma_poc.scripts.runners.jugnu import _format_v2_floor_plan

# Live-captured card texts from liveatarcos.com (2026-05-21):
_STUDIO_CARD_TEXT = (
    "Studio Bedroom S1 Studio | 1 Bath | 424 Sq. Ft. $500 Starting at $999 Request More Info"
)
_ONE_BR_A1_TEXT = (
    "One Bedroom A1 1 Bed | 1 Bath | 534 Sq. Ft. $500 Starting at $1,199 Request More Info"
)
_ONE_BR_A2_TEXT = (
    "One Bedroom A2 1 Bed | 1 Bath | 560 Sq. Ft. $500 Starting at $1,199 Request More Info"
)


# ── _parse_card_text + _split_plan_title_and_code ──────────────────


def test_parse_studio_card() -> None:
    p = _parse_card_text(_STUDIO_CARD_TEXT)
    assert p is not None
    assert p["title"] == "Studio Bedroom"
    assert p["code"] == "S1"
    assert p["beds"] == 0  # studio → 0
    assert p["baths"] == "1"
    assert p["sqft"] == "424"
    assert p["rent"] == 999
    assert p["deposit"] == "500"


def test_parse_one_bedroom_card() -> None:
    p = _parse_card_text(_ONE_BR_A1_TEXT)
    assert p is not None
    assert p["title"] == "One Bedroom"
    assert p["code"] == "A1"
    assert p["beds"] == 1
    assert p["sqft"] == "534"
    assert p["rent"] == 1199


def test_parse_returns_none_on_unrelated_text() -> None:
    """A card without the specs+rent pattern should not match."""
    assert _parse_card_text("Welcome to our community! Contact us today.") is None
    assert _parse_card_text("Studio | 1 Bath | 424 Sq. Ft. with no rent") is None  # no "Starting at"


def test_split_plan_title_and_code_short_code() -> None:
    title, code = _split_plan_title_and_code("Studio Bedroom S1")
    assert title == "Studio Bedroom"
    assert code == "S1"


def test_split_plan_title_no_code() -> None:
    title, code = _split_plan_title_and_code("Studio Bedroom")
    # "Bedroom" is too long to be a plan code → all-title
    assert title == "Studio Bedroom"
    assert code == ""


# ── parse_wix_floor_plans ──


def test_parse_all_three_real_liveatarcos_cards() -> None:
    cards = [
        {"tag": "DIV", "text": _STUDIO_CARD_TEXT},
        {"tag": "DIV", "text": _ONE_BR_A1_TEXT},
        {"tag": "DIV", "text": _ONE_BR_A2_TEXT},
    ]
    rows = parse_wix_floor_plans(cards, "https://www.liveatarcos.com/")
    assert len(rows) == 3
    codes = sorted(r["floor_plan_name"] for r in rows)
    assert codes == ["A1", "A2", "S1"]
    studio = next(r for r in rows if r["floor_plan_name"] == "S1")
    assert studio["bedrooms"] == "0"
    assert studio["market_rent_low"] == 999
    assert studio["extraction_tier"] == "TIER_1_DOM_WIX_FLOOR_PLANS"
    assert studio["availability_status"] == "UNKNOWN"


def test_parse_dedupes_duplicate_card_renders() -> None:
    """Wix sometimes renders the same plan card twice in different
    containers (mobile/desktop variants); dedupe by (title, code, rent)."""
    cards = [
        {"tag": "DIV", "text": _ONE_BR_A1_TEXT},
        {"tag": "DIV", "text": _ONE_BR_A1_TEXT},  # exact duplicate
    ]
    rows = parse_wix_floor_plans(cards, "u")
    assert len(rows) == 1


def test_parse_skips_cards_without_starting_at() -> None:
    """Cards missing the 'Starting at $X' marker (e.g. "Contact for pricing")
    are skipped; downstream WixNoPmsAdapter handles plan-less Wix sites."""
    cards = [
        {"tag": "DIV", "text": "One Bedroom A1 1 Bed | 1 Bath | 534 Sq. Ft. Contact for pricing"},
    ]
    rows = parse_wix_floor_plans(cards, "u")
    assert rows == []


# ── adapter end-to-end ──


class _FakePage:
    def __init__(self, payload, url="https://www.liveatarcos.com/phoenix-apartment-floor-plans"):
        self._payload = payload
        self.url = url

    async def evaluate(self, _js):
        return self._payload


class _StaticPage:
    def __init__(self, html: str, url: str, context=None):
        self._html = html
        self.url = url
        self.context = context

    async def content(self):
        return self._html


@pytest.mark.asyncio
async def test_adapter_extracts_three_plans() -> None:
    payload = {
        "ok": True,
        "cards": [
            {"tag": "DIV", "text": _STUDIO_CARD_TEXT},
            {"tag": "DIV", "text": _ONE_BR_A1_TEXT},
            {"tag": "DIV", "text": _ONE_BR_A2_TEXT},
        ],
    }
    ctx = AdapterContext(
        base_url="https://www.liveatarcos.com/",
        detected=detect_pms("https://www.liveatarcos.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await WixFloorPlansAdapter().extract(_FakePage(payload), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_WIX_FLOOR_PLANS"
    assert len(result.units) == 3
    assert result.confidence > 0.6


@pytest.mark.asyncio
async def test_adapter_bails_when_no_cards() -> None:
    payload = {"ok": False}
    ctx = AdapterContext(
        base_url="https://x.test/",
        detected=detect_pms("https://x.test/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await WixFloorPlansAdapter().extract(_FakePage(payload), ctx)  # type: ignore[arg-type]
    assert result.confidence == 0.0


# ── 2026-08-02 complete Wix cohort regressions ─────────────────────


_WESTERVILLE_HTML = """
<html><body>
  <section><h2>View Floor Plans</h2>
    <div><span>1 Bedroom Garden</span><div>Contact for Availability Bed: 1 Bathroom: 1 Rent Starting at: $965</div></div>
    <div><span>2 Bedroom Garden</span><div>Contact for Availability Bed: 2 Bathroom: 1 Rent Starting at: $1,075</div></div>
    <div><span>2 Bedroom Townhome</span><div>Contact for Availability Bed: 2 Bathroom: 2 with W/D Hookup Rent Starting at: $1,390</div></div>
  </section>
</body></html>
"""


def _bellagio_html(*, map_label: str = "View Map of Available Units") -> str:
    categories = "".join(
        f'<div class="wixui-repeater__item">{text}</div>'
        for text in (
            "Studio $1,499 - $1,699 Reserve Now!",
            "1 Bedroom &amp; 1 Bath $1,599 - $1,999 Reserve Now!",
            "2 Bedroom &amp; 2 Bath $1,899 - $2,399 Reserve Now!",
            "3 Bedroom &amp; 3 Bath $2,799 - $3,199 Reserve Now!",
            "2 Bedroom Penthouse $1,999 - $3,199 Reserve Now!",
            "1 Bedroom Penthouse $1,699 - 2,399 Reserve Now!",
        )
    )
    codes = "".join(
        f'<div data-testid="gallery-item-item"><div aria-label="X{i:02d}"></div></div>'
        for i in range(1, 26)
    )
    return f"""
    <html><body>{categories}{codes}
      <a aria-label="{map_label}"
         href="https://apps.3dplans.com/InteractivePropertyMap/PropertyMap?Embed=True&amp;Id=640c40c9-6c72-4c16-b6d3-4a996fcff013">{map_label}</a>
    </body></html>
    """


def _bellagio_current_pricing_html() -> str:
    return """
    <html><body>
      <div class="wixui-repeater__item">Studio $1,799 - $1,899 Reserve Now!</div>
      <div class="wixui-repeater__item">1 Bedroom &amp; 1 Bath $1,899 - $2,299 Reserve Now!</div>
      <div class="wixui-repeater__item">2 Bedroom &amp; 2 Bath $2,399 - $2,599 Reserve Now!</div>
      <div class="wixui-repeater__item">3 Bedroom &amp; 3 Bath $3,299 - $3,499 Reserve Now!</div>
      <div class="wixui-repeater__item">2 Bedroom Penthouse $2,899 - $3,699 Reserve Now!</div>
      <div class="wixui-repeater__item">1 Bedroom Penthouse $2,399 - $2,599 Reserve Now!</div>
    </body></html>
    """


def test_westerville_labeled_cards_are_exact_unknown_plans() -> None:
    cards = _bounded_card_texts(_WESTERVILLE_HTML)
    rows = parse_wix_floor_plans(
        cards,
        "https://www.capcityapartments.com/westerville-park",
        verified_plan_only=True,
    )
    assert len(rows) == 3
    assert [row["floor_plan_name"] for row in rows] == [
        "1 Bedroom Garden",
        "2 Bedroom Garden",
        "2 Bedroom Townhome",
    ]
    assert [(row["bedrooms"], row["bathrooms"]) for row in rows] == [
        ("1", "1"),
        ("2", "1"),
        ("2", "2"),
    ]
    assert [row["market_rent_low"] for row in rows] == [965, 1075, 1390]
    assert all(row["sqft"] == "" for row in rows)
    assert all(row["availability_status"] == "UNKNOWN" for row in rows)
    assert all(row["availability_date"] == "" for row in rows)
    assert all(row[VERIFIED_PLAN_ONLY_SURFACE_KEY] for row in rows)


@pytest.mark.asyncio
async def test_westerville_source_to_adapter_is_three_plans_not_units() -> None:
    ctx = AdapterContext(
        base_url="https://www.capcityapartments.com/westerville-park",
        detected=detect_pms("https://www.capcityapartments.com/westerville-park"),
        profile=None,
        expected_total_units=None,
        property_id="47909",
        property_name="Westerville Park Apartment Homes",
        address="4565 Northland Square Dr E",
        city="Columbus",
        state="OH",
        zip_code="43231",
    )
    result = await WixFloorPlansAdapter().extract(
        _StaticPage(_WESTERVILLE_HTML, ctx.base_url), ctx  # type: ignore[arg-type]
    )
    assert result.units == result.plan_summaries
    assert len(result.plan_summaries) == 3
    assert all(not row.get("unit_number") for row in result.plan_summaries)
    assert all(not row.get("availability_date") for row in result.plan_summaries)

    from ma_poc.reporting.publish_ceiling import PublishCeiling, assess_publish_ceiling
    from ma_poc.scripts.runners.jugnu import _publish_ceiling_plan_inputs

    plans, verified = _publish_ceiling_plan_inputs(
        {"plan_summaries": result.plan_summaries}
    )
    ceiling = assess_publish_ceiling(
        units=[],
        plan_summaries=plans,
        html_signals={"rent_signal_count": 3, "spa_confidence": 0.0},
        tier_trace=[{"outcome": "ran_empty"}],
        page_html=_WESTERVILLE_HTML,
        verified_plan_only_surface=verified,
    )
    assert ceiling.verdict is PublishCeiling.CONFIRMED_PLAN_ONLY
    assert ceiling.gold_eligible is True


def test_bellagio_categories_codes_and_labeled_map_are_bounded() -> None:
    html = _bellagio_html()
    rows = parse_wix_floor_plans(
        _bounded_card_texts(html), "https://www.bellagio1384.com/bellagio-pricing"
    )
    assert len(rows) == 6
    assert [(row["market_rent_low"], row["market_rent_high"]) for row in rows] == [
        (1499, 1699),
        (1599, 1999),
        (1899, 2399),
        (2799, 3199),
        (1999, 3199),
        (1699, 2399),
    ]
    assert _extract_plan_codes(html) == {f"X{i:02d}" for i in range(1, 26)}
    links = _extract_labeled_3dplans_links(
        html, "https://www.bellagio1384.com/bellagio-pricing"
    )
    assert [link.guid for link in links] == ["640c40c9-6c72-4c16-b6d3-4a996fcff013"]
    # The exact same external URL without operator-authored availability text
    # is not an admissible route.
    assert not _extract_labeled_3dplans_links(
        _bellagio_html(map_label="Open map"),
        "https://www.bellagio1384.com/bellagio-pricing",
    )


def _map_captures(*, observed_name: str = "Bellagio", observed_address: str = "1384 Empire Blvd"):
    current_property = {
        "Id": "6",
        "Name": observed_name,
        "Address": observed_address,
        "City": "Rochester",
        "Zip": "14609",
        "EmailLink": "https://www.bellagio1384.com/",
        "PropertyGUID": "640c40c9-6c72-4c16-b6d3-4a996fcff013",
    }
    map_request = {
        "screenData": {"variables": {"CurrentProperty": current_property}}
    }
    unit_request = {"screenData": {"variables": {"PropertyId": "6"}}}
    unit_body = {
        "data": {
            "PropertyUnits": {
                "List": [
                    {
                        "Id": "989",
                        "UnitNumber": "509 - Mountain Scenic View",
                        "FloorPlanID": "39",
                        "LocationId": "2105",
                        "BedRoomCount": 1,
                        "BathCount": "1.0",
                        "AreaSqFt": 892,
                        "FloorPlanName": "X09",
                        "ActualPrice": "2089.00000000",
                        "ActualAvailDate": "2026-08-14",
                    }
                ]
            },
            "TotalCount": 1,
        }
    }
    return [
        _MapCapture(
            "https://apps.3dplans.com/InteractivePropertyMap/screenservices/InteractivePropertyMap/Blocks/Map/DataActionGetUnits2",
            200,
            {"data": {"PlayCanvasUnitsList": {"List": []}}},
            map_request,
        ),
        _MapCapture(
            "https://apps.3dplans.com/InteractivePropertyMap/screenservices/InteractivePropertyMap/Blocks/Units/DataActionGetUnits",
            200,
            unit_body,
            unit_request,
        ),
    ]


def _bellagio_context() -> AdapterContext:
    return AdapterContext(
        base_url="https://www.bellagio1384.com/",
        detected=detect_pms("https://www.bellagio1384.com/"),
        profile=None,
        expected_total_units=None,
        property_id="262964",
        property_name="The Bellagio",
        address="1384 Empire Blvd",
        city="Rochester",
        state="NY",
        zip_code="14609",
    )


def _bellagio_map_link() -> _MapLink:
    return _MapLink(
        "https://apps.3dplans.com/InteractivePropertyMap/PropertyMap?Embed=True&Id=640c40c9-6c72-4c16-b6d3-4a996fcff013",
        "640c40c9-6c72-4c16-b6d3-4a996fcff013",
        "View Map of Available Units",
        "https://www.bellagio1384.com/bellagio-pricing",
    )


def test_3dplans_unit_requires_property_and_catalogue_binding() -> None:
    rows, producing, identity, error = _parse_3dplans_units(
        captures=_map_captures(),
        link=_bellagio_map_link(),
        plan_codes={f"X{i:02d}" for i in range(1, 26)},
        ctx=_bellagio_context(),
    )
    assert error == ""
    assert len(rows) == len(producing) == 1
    assert identity and identity["status"] == "MATCH"
    row = rows[0]
    assert row["unit_number"] == "509 - Mountain Scenic View"
    assert row["floor_plan_name"] == "X09"
    assert (row["bedrooms"], row["bathrooms"], row["sqft"]) == ("1", "1.0", "892")
    assert row["market_rent_low"] == row["market_rent_high"] == 2089
    assert row["availability_date"] == "2026-08-14"
    assert row["source_ids"] == {
        "three_d_plans_unit_id": "989",
        "three_d_plans_floor_plan_id": "39",
        "three_d_plans_property_guid": "640c40c9-6c72-4c16-b6d3-4a996fcff013",
        "three_d_plans_property_id": "6",
        "three_d_plans_location_id": "2105",
    }

    rows, _, identity, error = _parse_3dplans_units(
        captures=_map_captures(observed_name="Brookside", observed_address="1 Other St"),
        link=_bellagio_map_link(),
        plan_codes={"X09"},
        ctx=_bellagio_context(),
    )
    assert rows == []
    assert identity and identity["status"] == "MISMATCH"
    assert "property identity MISMATCH" in error

    rows, _, _, error = _parse_3dplans_units(
        captures=_map_captures(),
        link=_bellagio_map_link(),
        plan_codes={"X08"},
        ctx=_bellagio_context(),
    )
    assert rows == []
    assert "absent from Wix catalogue" in error


class _FakeRequest:
    def __init__(self, body):
        self.post_data_json = body


class _FakeResponse:
    def __init__(self, capture: _MapCapture):
        self.url = capture.url
        self.status = capture.status
        self.request = _FakeRequest(capture.request_body)
        self._body = capture.body

    async def json(self):
        return self._body


class _FakeMapPage:
    def __init__(self, captures):
        self._captures = captures
        self._callback = None
        self.closed = False

    def on(self, event, callback):
        assert event == "response"
        self._callback = callback

    async def goto(self, _url, **_kwargs):
        for capture in self._captures:
            self._callback(_FakeResponse(capture))

    async def wait_for_timeout(self, _timeout):
        await asyncio.sleep(0)

    async def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, map_page):
        self.map_page = map_page

    async def new_page(self):
        return self.map_page


@pytest.mark.asyncio
async def test_bellagio_source_to_final_preserves_six_plans_and_unit(monkeypatch) -> None:
    root_html = """
    <html><body>
      <a href="/apartments">PRICING</a>
      <a href="/bellagio-pricing">FLOOR PLANS</a>
    </body></html>
    """
    plan_html = _bellagio_html()
    monkeypatch.setattr(
        wix_module,
        "_fetch_public_html",
        lambda url: _HtmlSource(
            url,
            _bellagio_current_pricing_html() if url.endswith("/apartments") else plan_html,
            200,
        ),
    )
    map_page = _FakeMapPage(_map_captures())
    ctx = _bellagio_context()
    page = _StaticPage(root_html, ctx.base_url, _FakeContext(map_page))
    result = await WixFloorPlansAdapter().extract(page, ctx)  # type: ignore[arg-type]

    physical = [row for row in result.units if row.get("unit_number")]
    assert len(physical) == 1
    assert physical[0]["source_ids"]["three_d_plans_unit_id"] == "989"
    assert physical[0]["availability_date"] == "2026-08-14"
    assert len(result.plan_summaries) == 6
    one_bed = next(
        row for row in result.plan_summaries if row["floor_plan_name"] == "1 Bedroom & 1 Bath"
    )
    assert (one_bed["market_rent_low"], one_bed["market_rent_high"]) == (1899, 2299)
    assert one_bed["wix_source_role"] == "PRICING"
    assert all(VERIFIED_PLAN_ONLY_SURFACE_KEY not in row for row in result.plan_summaries)
    assert result.tier_used == "TIER_1_API_WIX_3DPLANS"
    assert result.unit_source_provenance[0]["provider"] == "3dplans"
    assert result.unit_source_provenance[0]["unit_count"] == 1
    assert result.unit_source_provenance[0]["identity"]["status"] == "MATCH"
    assert result.api_responses[0]["body"] == "<3dplans-unit-roster>"
    assert map_page.closed is True


def _warmup_html(collections: dict[str, dict[str, dict[str, object]]]) -> str:
    payload = {
        "appsWarmupData": {
            "dataBinding": {
                "dataStore": {"recordsByCollectionId": collections}
            }
        }
    }
    return (
        '<html><body><script type="application/json" id="wix-warmup-data">'
        + json.dumps(payload)
        + "</script></body></html>"
    )


def test_complete_four_site_wix_plan_catalogues_preserve_27_records() -> None:
    arcos_records = {
        record_id: {
            "_id": record_id,
            "unitName": code,
            "title": title,
            "price": price,
            "deposit": "$500",
            "sqFt": f"{sqft} Sq. Ft.",
            "beds": beds,
            "bath": "1 Bath",
        }
        for record_id, code, title, price, sqft, beds in (
            ("58a5a464-d47e-47c8-aa15-63ca90e477fd", "A1", "One Bedroom", "$1,199", 534, "1 Bed"),
            ("e5f21a8d-95aa-431d-bd04-a1e816748e5a", "A2", "One Bedroom", "$1,199", 560, "1 Bed"),
            ("27ba1d17-a47e-4e32-82ca-11aee8ebe424", "S1", "Studio Bedroom", "$999", 424, "Studio"),
        )
    }
    arcos = extract_wix_authored_plan_rows(
        _warmup_html({"Import133": arcos_records}),
        "https://www.liveatarcos.com/phoenix-apartment-floor-plans",
        verified_plan_only=True,
    )
    assert [(row["floor_plan_name"], row["bedrooms"], row["sqft"]) for row in arcos] == [
        ("A1", "1", "534"),
        ("A2", "1", "560"),
        ("S1", "0", "424"),
    ]
    assert [row["deposit"] for row in arcos] == ["500", "500", "500"]

    constellation_specs = (
        ("Electra", "electra", 1, 1, 654, 1300),
        ("Vega", "vega", 1, 1, 815, 1449),
        ("Vega With Garage", "vegagarage", 1, 1, 815, 2110),
        ("Hudson", "hudson", 2, 1, 918, 1485),
        ("Loadstar", "loadstar", 2, 2, 1040, 1549),
        ("Neptune", "neptune", 2, 2, 1100, 1650),
        ("Galaxy", "galaxy", 2, 2, 1255, 1750),
        ("Hercules", "hercules", 3, 2, 1349, 2302),
        ("Hercules With Garage", "herculesgarage", 3, 2, 1349, 2357),
    )
    constellation_html = "<html><body>" + "".join(
        f'<div id="comp-{slug}"><a href="/{slug}">Floor Plan : {name}</a> '
        f"{beds} Bed | {baths} Bath | {sqft:,} Sq. Ft. ${rent:,}</div>"
        for name, slug, beds, baths, sqft, rent in constellation_specs
    ) + "</body></html>"
    constellation = extract_wix_authored_plan_rows(
        constellation_html,
        "https://www.constellationranchtx.com/floorplans",
        verified_plan_only=True,
    )
    assert [row["floor_plan_name"] for row in constellation] == [
        spec[0] for spec in constellation_specs
    ]
    assert [row["source_ids"]["wix_plan_record_id"] for row in constellation] == [
        f"route:/{spec[1]}" for spec in constellation_specs
    ]

    stoney_specs = (
        ("1 Bedroom - 1 Bath", "Phase IV", 944, 1200),
        ("1 Bedroom - 1 Bath", "Phase I - III", 768, 1210),
        ("1 Bedroom - 1 Bath", "Phase V", 900, 1250),
        ("2 Bedroom - 1 Bath", "Phase IV", 1128, 1275),
        ("2 Bedroom - 1 Bath", "Phase I - III", 988, 1310),
        ("2 Bedroom - 1 Bath", "Phase V", 1100, 1290),
        ("2 Bedroom w/Den - 2 Bath", "Phase I - III", 1536, 1760),
        ("2 Bedroom - 1.5 Bath Townhouse", "Phase IV", 1044, 1425),
        ("2 Bedroom - 1.5 Bath Townhouse", "Phase I - III", 1004, 1420),
    )
    stoney_records = {
        f"00000000-0000-4000-8000-{index:012d}": {
            "_id": f"00000000-0000-4000-8000-{index:012d}",
            "title": title,
            "Strng_sTxt0": phase,
            "i98ch45k": f"${rent:,}",
            "wxRchTxt_sTxt4": f"<p>This floor plan features {sqft:,}-square-foot of living space.</p>",
        }
        for index, (title, phase, sqft, rent) in enumerate(stoney_specs, 1)
    }
    stoney = extract_wix_authored_plan_rows(
        _warmup_html({"one_bed": dict(list(stoney_records.items())[:3]), "two_bed": dict(list(stoney_records.items())[3:])}),
        "https://www.stoneycreekapts.com/floorplans",
        verified_plan_only=True,
    )
    assert [(row["sqft"], row["market_rent_low"]) for row in stoney] == [
        (str(spec[2]), spec[3]) for spec in stoney_specs
    ]
    assert len({row["floor_plan_name"] for row in stoney}) == 9

    gentry_specs = (
        ("2 BEDROOM STYLE 1", 1075, 1185, 1742, 2490),
        ("1 BEDROOM CONVERTIBLE 1", 600, 912, 1252, 2090),
        ("1 BEDROOM RIVERFRONT", 750, 1047, 1442, 2290),
        ("2 BEDROOM STYLE 2", 1050, 1185, 1742, 2490),
        ("1 BEDROOM CONVERTIBLE 2", 600, 912, 1252, 2090),
        ("STUDIO", 500, 810, 1140, 1990),
    )
    gentry_html = "<html><body>" + "".join(
        f'<div id="comp-gentry-{index}">{name} ({sqft} SQ. FT.) '
        f"Unfurnished ${low} - ${high} Furnished ${furnished}</div>"
        for index, (name, sqft, low, high, furnished) in enumerate(gentry_specs, 1)
    ) + "</body></html>"
    gentry = extract_wix_authored_plan_rows(
        gentry_html,
        "https://www.gentryslanding.com/apartments",
        verified_plan_only=True,
    )
    assert [(row["floor_plan_name"], row["market_rent_low"], row["market_rent_high"]) for row in gentry] == [
        (spec[0], spec[2], spec[3]) for spec in gentry_specs
    ]
    assert [row["furnished_rent_low"] for row in gentry] == [spec[4] for spec in gentry_specs]

    all_rows = arcos + constellation + stoney + gentry
    assert len(all_rows) == 27
    assert len({row["source_ids"]["wix_plan_record_id"] for row in all_rows}) == 27
    assert all(row["availability_status"] == "UNKNOWN" for row in all_rows)
    assert all(row["availability_date"] == "" for row in all_rows)
    assert all(row[VERIFIED_PLAN_ONLY_SURFACE_KEY] for row in all_rows)
    final = [
        _format_v2_floor_plan(row, datetime(2026, 8, 2, 12, 0), "P_WIX")
        for row in all_rows
    ]
    assert all(row["available_date"] is None for row in final)
    assert all(row["availability_date_provenance"] == "missing" for row in final)
    assert len({row["floor_plan_id"] for row in final}) == 27
    assert all(row["source_ids"]["wix_plan_record_id"] for row in final)


def test_three_recoverable_wix_plan_shapes_and_negative_controls() -> None:
    indian_html = """
    <html><body><h2>Current Rates</h2>
    <div>$1000 Monthly Rent $1200 Security Deposit</div>
    <div>$1300-1500 Monthly Rent $1500 Security Deposit</div>
    <h2>Layout of Apartments</h2>
    <div>1 Bedroom Apartment 535 square feet</div>
    <div>2 Bedroom Apartment 2 Bedroom Apartment 740 square feet</div>
    </body></html>
    """
    indian = extract_wix_authored_plan_rows(
        indian_html, "https://www.indianvillageapts.com/", verified_plan_only=True
    )
    assert [(row["floor_plan_name"], row["sqft"], row["market_rent_low"], row["market_rent_high"], row["deposit"]) for row in indian] == [
        ("1 Bedroom Apartment", "535", 1000, 1000, "1200"),
        ("2 Bedroom Apartment", "740", 1300, 1500, "1500"),
    ]

    westgate_root = """
    <a href="/onebedroom">ONE BEDROOM</a>
    <a href="/twobedroom">TWO BEDROOM</a>
    """
    links = _discover_same_site_plan_links(
        westgate_root, "https://www.westgate-village-townhouses.com/"
    )
    assert [link.url for link in links] == [
        "https://www.westgate-village-townhouses.com/onebedroom",
        "https://www.westgate-village-townhouses.com/twobedroom",
    ]
    one = extract_wix_authored_plan_rows(
        "<html><head><title>ONE BEDROOM | westgate-village</title></head><body>"
        "Floor Plans Bed: 1 Bath: 1 SQ.FT.: 680 Rent: $1150-1200 Deposit: Varies "
        "Carport rental $35 Garage rental $60 Cleaning fee $300</body></html>",
        links[0].url,
        verified_plan_only=True,
    )
    two = extract_wix_authored_plan_rows(
        "<html><head><title>TWO BEDROOM | westgate-village</title></head><body>"
        "Floor Plans Bed: 2 Bath: 1.5 SQ.FT.: 1,297 Rent: $1600-1850 Deposit: Varies "
        "Washer & Dryer Rental: $75 Application processing fee $35</body></html>",
        links[1].url,
        verified_plan_only=True,
    )
    assert [(row["bedrooms"], row["bathrooms"], row["sqft"], row["market_rent_low"], row["market_rent_high"]) for row in one + two] == [
        ("1", "1", "680", 1150, 1200),
        ("2", "1.5", "1297", 1600, 1850),
    ]

    allen_html = """
    <html><body><h1>Allen Ranch Estates</h1><h2>3 Bedroom Townhouse</h2>
    <div id="comp-allen">Now Available! Screening Requirements: 2 Year Rental History
    Details: $1400.00 Per Month 3 Bedroom / 2.5 Bathroom 1,360 Sq Ft NO PETS
    Lease Terms: Month to Month $800.00 Refundable Deposit</div></body></html>
    """
    allen = extract_wix_authored_plan_rows(
        allen_html, "https://www.ishranch.com/allen-ranch", verified_plan_only=True
    )
    assert len(allen) == 1
    assert (allen[0]["availability_status"], allen[0]["availability_date"]) == (
        "AVAILABLE",
        "Now",
    )
    assert (allen[0]["market_rent_low"], allen[0]["deposit"]) == (1400, "800")
    allen_final = _format_v2_floor_plan(
        allen[0], datetime(2026, 8, 2, 12, 0), "282696"
    )
    assert allen_final["available_date"] == "2026-08-02"
    assert allen_final["availability_date_provenance"] == "available_now"

    assert len(indian + one + two + allen) == 5
    for negative in (
        "<html><body>1, 2 and 3 bedroom apartments. Contact leasing.</body></html>",
        "<html><body>AVAILABILITY <iframe></iframe> Parking $250 Garage $400</body></html>",
        "<html><body>Apply today <form><input name='email'></form></body></html>",
    ):
        assert extract_wix_authored_plan_rows(
            negative, "https://negative.example/", verified_plan_only=True
        ) == []


@pytest.mark.asyncio
async def test_wix_nopms_prefers_authored_cms_records_over_empty_universal_chain(
    monkeypatch,
) -> None:
    from ma_poc.pms.adapters import _universal_recovery

    html = _warmup_html(
        {
            "plans": {
                "record-a1": {
                    "_id": "record-a1",
                    "unitName": "A1",
                    "title": "One Bedroom",
                    "price": "$1,199",
                    "deposit": "$500",
                    "sqFt": "534 Sq. Ft.",
                    "beds": "1 Bed",
                    "bath": "1 Bath",
                }
            }
        }
    )

    async def empty_chain(_page, _ctx):
        return [], "", ""

    monkeypatch.setattr(_universal_recovery, "recover_universal_embed", empty_chain)
    ctx = AdapterContext(
        base_url="https://www.liveatarcos.com/",
        detected=detect_pms("https://www.liveatarcos.com/"),
        profile=None,
        expected_total_units=None,
        property_id="23963",
    )
    ctx.fetch_result = SimpleNamespace(
        body=html,
        final_url="https://www.liveatarcos.com/phoenix-apartment-floor-plans",
    )
    result = await WixNoPmsAdapter().extract(
        _StaticPage(html, ctx.fetch_result.final_url), ctx  # type: ignore[arg-type]
    )
    assert len(result.plan_summaries) == 1
    assert result.plan_summaries[0]["floor_plan_name"] == "A1"
    assert result.plan_summaries[0]["source_ids"] == {
        "wix_plan_record_id": "record-a1"
    }


# ── detector tests ──


def test_detector_routes_wix_with_starting_at_to_wix_floor_plans() -> None:
    """Wix host marker + 'Starting at $X' text → wix_floor_plans (not
    the plan-less wix_nopms fallback)."""
    from ma_poc.pms.detector import _iter_html_markers
    html = """
    <html><body>
      <script src="https://static.parastorage.com/services/santa-resolver.bundle.min.js"></script>
      <div>Studio Bedroom S1 Studio | 1 Bath | 424 Sq. Ft. $500 Starting at $999</div>
    </body></html>
    """
    markers = list(_iter_html_markers(html.lower()))
    assert any(m[0] == "wix_floor_plans" for m in markers), markers
    # And wix_nopms must NOT fire when wix_floor_plans does.
    assert not [m for m in markers if m[0] == "wix_nopms"]


def test_detector_routes_wix_without_starting_at_to_wix_nopms() -> None:
    """A Wix site that just embeds Wix runtime but has no plan-card
    text pattern should keep routing to the existing wix_nopms
    fallback (no regression)."""
    from ma_poc.pms.detector import _iter_html_markers
    html = """
    <html><body>
      <script src="https://static.parastorage.com/services/santa-resolver.bundle.min.js"></script>
      <p>Welcome to our community. Contact us for pricing.</p>
    </body></html>
    """
    markers = list(_iter_html_markers(html.lower()))
    assert any(m[0] == "wix_nopms" for m in markers)
    assert not [m for m in markers if m[0] == "wix_floor_plans"]


def test_adapter_registered() -> None:
    a = get_adapter("wix_floor_plans")
    assert isinstance(a, WixFloorPlansAdapter)


def test_strategy_is_dom_first() -> None:
    from ma_poc.pms.detector import _STRATEGY_BY_PMS
    assert _STRATEGY_BY_PMS["wix_floor_plans"] == "dom_first"
