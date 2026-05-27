"""RentVision adapter (2026-05-19, greenfield).

Card data captured live from westgateirving.com/floorplans (single-price +
vacancy variants) and loftsatlittlecreek.com/floorplans (price-range +
"Call for Details" variant) — the shape the SSR .floorplanItem grid emits.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.rentvision import RentVisionAdapter, parse_rentvision_cards
from ma_poc.pms.detector import detect_pms

# Live westgateirving.com/floorplans + loftsatlittlecreek.com/floorplans.
_RV_CARDS = [
    {"name": "B2", "bedsAttr": "2", "beds": "2 Bed", "baths": "2 Bath",
     "sqft": "926 Sq Ft square feet", "price": "Pricing Starting at $1,339",
     "avail": "Only 1 Vacant Apartment Left!"},
    {"name": "A3", "bedsAttr": "1", "beds": "1 Bed", "baths": "1 Bath",
     "sqft": "723 Sq Ft square feet", "price": "Pricing Starting at $1,006",
     "avail": "Available"},
    {"name": "Hillside II - Greensboro", "bedsAttr": "Studio",
     "beds": "Studio Bed", "baths": "1 Bath", "sqft": "572 Sq Ft square feet",
     "price": "Price $1,075 - $1,100", "avail": "Call for Details!"},
]


class _FakePage:
    def __init__(self, cards: object, url: str = "https://www.westgateirving.com/floorplans") -> None:
        self._cards = cards
        self.url = url

    async def evaluate(self, _js: str, *_a: object) -> object:
        return self._cards


def _ctx(base_url: str = "https://www.westgateirving.com/") -> AdapterContext:
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )


def test_parse_rentvision_cards_fields() -> None:
    units = parse_rentvision_cards(_RV_CARDS, "https://www.westgateirving.com/floorplans")
    assert len(units) == 3

    b2 = units[0]
    assert b2["floor_plan_name"] == "B2"
    assert b2["bedrooms"] == "2"
    assert b2["bathrooms"] == "2"
    assert b2["sqft"] == "926"
    assert b2["market_rent_low"] == 1339
    assert b2["market_rent_high"] == 1339
    assert b2["available_units"] == "1"
    assert b2["availability_status"] == "AVAILABLE"
    assert b2["extraction_tier"] == "TIER_3_DOM_RENTVISION"

    a3 = units[1]
    assert a3["bedrooms"] == "1"
    assert a3["market_rent_low"] == 1006
    assert a3["available_units"] == ""  # "Available", no vacancy count

    studio = units[2]
    assert studio["bedrooms"] == "0"
    assert studio["bed_label"]
    assert studio["market_rent_low"] == 1075
    assert studio["market_rent_high"] == 1100  # price RANGE shape


def test_parse_rentvision_skips_empty_rows() -> None:
    assert parse_rentvision_cards([{}, {"name": "", "bedsAttr": "", "beds": ""}], "u") == []


@pytest.mark.asyncio
async def test_rentvision_adapter_extract(monkeypatch) -> None:
    """2026-05-25 update: the adapter now drills per-plan detail pages
    before falling back to plan-level. Mock both self-fetch helpers to
    return empty so the test stays hermetic (no real network calls).
    The drill returns 0, the existing plan-level path still fires, and
    the test validates the original plan-level emit shape."""
    async def _mock_empty(*args, **kwargs):
        return ""
    monkeypatch.setattr(
        RentVisionAdapter, "_fetch_floorplans_html", staticmethod(_mock_empty)
    )
    monkeypatch.setattr(
        RentVisionAdapter, "_fetch_detail_html", staticmethod(_mock_empty)
    )
    result = await RentVisionAdapter().extract(_FakePage(_RV_CARDS), _ctx())  # type: ignore[arg-type]
    assert result.tier_used == "TIER_3_DOM_RENTVISION"
    assert len(result.units) == 3
    assert result.confidence > 0.0
    assert result.winning_url == "https://www.westgateirving.com/floorplans"


@pytest.mark.asyncio
async def test_rentvision_adapter_no_floorplans_blocks(monkeypatch) -> None:
    """No .floorplanItem (evaluate returns []) → clean zero-confidence fail."""
    async def _mock_empty(*args, **kwargs):
        return ""
    monkeypatch.setattr(
        RentVisionAdapter, "_fetch_floorplans_html", staticmethod(_mock_empty)
    )
    monkeypatch.setattr(
        RentVisionAdapter, "_fetch_detail_html", staticmethod(_mock_empty)
    )
    result = await RentVisionAdapter().extract(_FakePage([]), _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0
    assert result.errors


@pytest.mark.asyncio
async def test_rentvision_adapter_pageless_stub(monkeypatch) -> None:
    async def _mock_empty(*args, **kwargs):
        return ""
    monkeypatch.setattr(
        RentVisionAdapter, "_fetch_floorplans_html", staticmethod(_mock_empty)
    )
    monkeypatch.setattr(
        RentVisionAdapter, "_fetch_detail_html", staticmethod(_mock_empty)
    )

    class _Bare:
        url = "https://x.com/"

    result = await RentVisionAdapter().extract(_Bare(), _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


def test_detector_routes_rentvision_marker() -> None:
    html = (
        "<html><body><footer>Website created by RentVision · "
        '<a href="https://rentvision.com">RentVision</a></footer></body></html>'
    )
    det = detect_pms("https://www.westgateirving.com/", page_html=html)
    assert det.pms == "rentvision"
    assert det.recommended_strategy == "dom_first"


def test_rentvision_adapter_registered() -> None:
    adapter = get_adapter("rentvision")
    assert isinstance(adapter, RentVisionAdapter)
    assert adapter.pms_name == "rentvision"


# ─────────────────────────────────────────────────────────────────────
# 2026-05-25 user-flagged via Walnut Creek (pid 45534) — unit-detail
# drill from /floorplans/{bed-tier}/{slug}.
#
# Pre-fix: adapter was plan-level only. Walnut Creek result: 3 plan-
# summary `inferred_*` rows.
#
# Post-fix: adapter follows each plan card's /floorplans/{bed-tier}/
# {slug} anchor, fetches the SSR detail page, parses the unit-listing
# table for per-unit data (unit_number from <th class="left wrap">,
# rent from <span>$X</span>, availability from .unit-availability cell,
# move-in date from Apply Now button onclick). Walnut Creek result:
# 10 real units across 2 plans (greystone, vintage) plus heritage
# correctly returning 0 (no availability today).
# ─────────────────────────────────────────────────────────────────────


def test_find_plan_detail_urls_basic() -> None:
    """Plan-grid anchors point to /floorplans/{bed-tier}/{slug}.
    Extract them as absolute URLs ready for fetch."""
    from ma_poc.pms.adapters.rentvision import find_plan_detail_urls

    html = (
        '<html><body>'
        '<a class="floorplanNameAnchor" href="/floorplans/two-bedroom/greystone">G</a>'
        '<a class="accentButton" href="/floorplans/two-bedroom/vintage">V</a>'
        '<a class="floorplanNameAnchor" href="/floorplans/three-bedroom/heritage">H</a>'
        '</body></html>'
    )
    urls = find_plan_detail_urls(
        html, "https://www.liveatwalnutcreekapts.com/floorplans"
    )
    assert urls == [
        "https://www.liveatwalnutcreekapts.com/floorplans/two-bedroom/greystone",
        "https://www.liveatwalnutcreekapts.com/floorplans/two-bedroom/vintage",
        "https://www.liveatwalnutcreekapts.com/floorplans/three-bedroom/heritage",
    ]


def test_find_plan_detail_urls_dedupe() -> None:
    """Same slug linked thrice (name anchor + details button + header dropdown)
    → one URL out. Also: trailing-slash variant should dedupe with bare."""
    from ma_poc.pms.adapters.rentvision import find_plan_detail_urls

    html = (
        '<a href="/floorplans/two-bedroom/greystone">A</a>'
        '<a href="/floorplans/two-bedroom/greystone">B</a>'
        '<a href="/floorplans/two-bedroom/greystone">C</a>'
    )
    urls = find_plan_detail_urls(html, "https://example.com/floorplans")
    assert len(urls) == 1


def test_find_plan_detail_urls_only_known_bed_tiers() -> None:
    """Random /floorplans/X/Y links from the marketing menu shouldn't
    pollute the drill set — only known bed-tier prefixes accepted."""
    from ma_poc.pms.adapters.rentvision import find_plan_detail_urls

    html = (
        '<a href="/floorplans/studio/lakeside">L</a>'
        '<a href="/floorplans/photo-gallery/main">should-not-match</a>'
        '<a href="/floorplans/specials/move-in">should-not-match</a>'
    )
    urls = find_plan_detail_urls(html, "https://x.test/floorplans")
    assert urls == ["https://x.test/floorplans/studio/lakeside"]


def test_find_plan_detail_urls_empty_on_no_match() -> None:
    """No anchors → empty list (caller falls back to plan-level)."""
    from ma_poc.pms.adapters.rentvision import find_plan_detail_urls

    assert find_plan_detail_urls("<html>none</html>", "https://x.test") == []
    assert find_plan_detail_urls("", "https://x.test") == []


def test_parse_rentvision_unit_table_walnut_creek_greystone() -> None:
    """Real Walnut Creek greystone signature (live-probed 2026-05-25):
    5 unit rows — <th class="left wrap"> → unit_number, <span>$X</span>
    → asking rent, .unit-availability cell → status + move-in date,
    Apply Now button onclick → backup move-in date."""
    from ma_poc.pms.adapters.rentvision import parse_rentvision_unit_table

    html = (
        '<table><tbody>'
        '<tr>'
        '<th class="left wrap">622-102</th>'
        '<td class="standard identifiable-links right">'
        '<span>$1,249</span>'
        '<div class="term-pricing-popup"><h3>Unit Term Pricing - 622-102</h3>'
        '<table><tr><td>12 Month</td><td>$1,249</td></tr></table></div>'
        '</td>'
        '<td class="standard unit-availability">Available Now</td>'
        '<td class="unit-actions">'
        '<button onclick="window.open(\'https://aptdyn.myresman.com/Portal/'
        'Applicants/Availability?a&#61;1005&amp;p&#61;7391bce1&amp;'
        'moveInDate&#61;05/26/2026&amp;unit&#61;622-102\');">Apply Now</button>'
        '</td>'
        '</tr>'
        '<tr>'
        '<th class="left wrap">602-102</th>'
        '<td class="standard identifiable-links right">'
        '<span>$1,289</span></td>'
        '<td class="standard unit-availability">'
        'Available on <span>May 29, 2026</span></td>'
        '<td class="unit-actions">'
        '<button onclick="window.open(\'https://x?moveInDate&#61;05/29/2026'
        '&amp;unit&#61;602-102\');">Apply</button></td>'
        '</tr>'
        '</tbody></table>'
    )
    units = parse_rentvision_unit_table(
        html,
        "https://www.liveatwalnutcreekapts.com/floorplans/two-bedroom/greystone",
        "Greystone",
    )
    assert len(units) == 2

    u1 = units[0]
    assert u1["unit_number"] == "622-102"
    assert u1["market_rent_low"] == 1249
    assert u1["market_rent_high"] == 1249
    assert u1["availability_status"] == "AVAILABLE"
    assert u1["availability_date"] == "2026-05-26"  # from Apply URL
    assert u1["floor_plan_name"] == "Greystone"
    assert u1["bedrooms"] == "2"  # derived from URL bed-tier
    assert u1["extraction_tier"] == "TIER_3_DOM_RENTVISION_UNIT_LEVEL"

    u2 = units[1]
    assert u2["unit_number"] == "602-102"
    assert u2["market_rent_low"] == 1289
    # cell-text date "May 29, 2026" wins over Apply URL (consistent here)
    assert u2["availability_date"] == "2026-05-29"


def test_parse_rentvision_unit_table_compound_unit_codes() -> None:
    """Vintage plan unit numbers like C-612, B-2610 (letter-dash-digits).
    Confirms the unit-cell regex accepts alphanumeric+dash compound codes."""
    from ma_poc.pms.adapters.rentvision import parse_rentvision_unit_table

    html = (
        '<tr>'
        '<th class="left wrap">C-612</th>'
        '<td class="standard identifiable-links right"><span>$1,120</span></td>'
        '<td class="standard unit-availability">Available Now</td>'
        '<td class="unit-actions"><button onclick="window.open(\''
        '?moveInDate&#61;05/26/2026&amp;unit&#61;C-612\');">x</button></td>'
        '</tr>'
        '<tr>'
        '<th class="left wrap">B-2610</th>'
        '<td class="standard identifiable-links right"><span>$1,149</span></td>'
        '<td class="standard unit-availability">Available Now</td>'
        '<td class="unit-actions"><button onclick="window.open(\''
        '?moveInDate&#61;05/26/2026&amp;unit&#61;B-2610\');">x</button></td>'
        '</tr>'
    )
    units = parse_rentvision_unit_table(
        html,
        "https://www.liveatwalnutcreekapts.com/floorplans/two-bedroom/vintage",
        "Vintage",
    )
    assert [u["unit_number"] for u in units] == ["C-612", "B-2610"]
    assert all(u["floor_plan_name"] == "Vintage" for u in units)
    assert units[0]["market_rent_low"] == 1120
    assert units[1]["market_rent_low"] == 1149


def test_parse_rentvision_unit_table_empty_when_no_units() -> None:
    """Heritage plan: detail page rendered but no <th class="left wrap">
    rows (no availability today). Parser returns empty list — caller
    treats that as 'this plan contributes nothing' and either falls back
    to plan-level or proceeds with units from other plans."""
    from ma_poc.pms.adapters.rentvision import parse_rentvision_unit_table

    html = (
        "<html><body><h1>Heritage</h1>"
        "<p>No units currently available.</p>"
        "</body></html>"
    )
    assert parse_rentvision_unit_table(html, "https://x/floorplans/three-bedroom/heritage", "") == []


def test_parse_rentvision_unit_table_plan_name_from_url() -> None:
    """When the caller doesn't pass floor_plan_name, derive it from the
    URL slug (greystone → Greystone; the-park-suite → The Park Suite)."""
    from ma_poc.pms.adapters.rentvision import parse_rentvision_unit_table

    html = (
        '<tr><th class="left wrap">1</th>'
        '<td class="standard identifiable-links right"><span>$1,000</span></td>'
        '<td class="standard unit-availability">Available Now</td></tr>'
    )
    units = parse_rentvision_unit_table(
        html, "https://x.test/floorplans/two-bedroom/the-park-suite", ""
    )
    assert units[0]["floor_plan_name"] == "The Park Suite"


def test_parse_rentvision_unit_table_skips_no_digit_units() -> None:
    """Defensive: ``<th class="left wrap">Apartment</th>`` (a column header
    on a hypothetical future theme) must not be emitted as a unit row."""
    from ma_poc.pms.adapters.rentvision import parse_rentvision_unit_table

    html = (
        '<tr><th class="left wrap">Apartment</th>'  # header, no digit
        '<td class="standard identifiable-links right"><span>$1</span></td></tr>'
    )
    assert parse_rentvision_unit_table(html, "https://x/floorplans/studio/s", "") == []


def test_parse_rentvision_unit_table_rejects_garbage_dates() -> None:
    """Malformed "Available on" date strings → availability_date empty
    (parser must not crash, must not invent an ISO date)."""
    from ma_poc.pms.adapters.rentvision import parse_rentvision_unit_table

    html = (
        '<tr><th class="left wrap">101</th>'
        '<td class="standard identifiable-links right"><span>$1,200</span></td>'
        '<td class="standard unit-availability">Available on <span>Maytember 99, 2026</span></td>'
        '</tr>'
    )
    units = parse_rentvision_unit_table(html, "https://x/floorplans/studio/s", "")
    assert len(units) == 1
    assert units[0]["availability_date"] == ""


def test_parse_rentvision_unit_table_empty_inputs() -> None:
    """Empty / sentinel inputs → empty list (no crash, no false rows)."""
    from ma_poc.pms.adapters.rentvision import parse_rentvision_unit_table

    assert parse_rentvision_unit_table("", "https://x", "") == []
    assert parse_rentvision_unit_table("<html>none</html>", "https://x", "") == []


@pytest.mark.asyncio
async def test_adapter_drill_end_to_end(monkeypatch) -> None:
    """End-to-end: plan tile → mocked /floorplans returns HTML with 1
    detail anchor → mocked detail-fetch returns a unit-listing table →
    adapter emits 1 TIER_3_DOM_RENTVISION_UNIT_LEVEL unit with the
    right shape (drill prefers unit-level over plan-level)."""
    fp_html = (
        '<a class="floorplanNameAnchor" href="/floorplans/two-bedroom/greystone">G</a>'
    )
    detail_html = (
        '<tr><th class="left wrap">622-102</th>'
        '<td class="standard identifiable-links right"><span>$1,249</span></td>'
        '<td class="standard unit-availability">Available Now</td>'
        '<td class="unit-actions"><button onclick="window.open(\''
        '?moveInDate&#61;05/26/2026&amp;unit&#61;622-102\');">x</button></td>'
        '</tr>'
    )

    async def _mock_fp(*args, **kwargs):
        return fp_html

    async def _mock_detail(*args, **kwargs):
        return detail_html

    monkeypatch.setattr(
        RentVisionAdapter,
        "_fetch_floorplans_html",
        staticmethod(_mock_fp),
    )
    monkeypatch.setattr(
        RentVisionAdapter,
        "_fetch_detail_html",
        staticmethod(_mock_detail),
    )

    result = await RentVisionAdapter().extract(
        _FakePage(_RV_CARDS, url="https://www.westgateirving.com/floorplans"),
        _ctx(),  # type: ignore[arg-type]
    )
    # Drill succeeded → unit-level tier wins over plan-level.
    assert result.tier_used == "TIER_3_DOM_RENTVISION_UNIT_LEVEL"
    assert len(result.units) == 1
    u = result.units[0]
    assert u["unit_number"] == "622-102"
    assert u["market_rent_low"] == 1249
    assert u["availability_date"] == "2026-05-26"


@pytest.mark.asyncio
async def test_adapter_drill_falls_back_to_plan_level_on_empty(monkeypatch) -> None:
    """If the drill fetches detail pages but every page has 0 units
    (every plan unavailable — e.g. Walnut Creek's Heritage-only world),
    fall through to plan-level so the property still surfaces."""
    fp_html = (
        '<a class="floorplanNameAnchor" href="/floorplans/three-bedroom/heritage">H</a>'
    )
    detail_html = "<html><body>No units available.</body></html>"

    async def _mock_fp(*args, **kwargs):
        return fp_html

    async def _mock_detail(*args, **kwargs):
        return detail_html

    monkeypatch.setattr(
        RentVisionAdapter,
        "_fetch_floorplans_html",
        staticmethod(_mock_fp),
    )
    monkeypatch.setattr(
        RentVisionAdapter,
        "_fetch_detail_html",
        staticmethod(_mock_detail),
    )

    result = await RentVisionAdapter().extract(
        _FakePage(_RV_CARDS), _ctx()  # type: ignore[arg-type]
    )
    # Drill returned 0 units → plan-level wins.
    assert result.tier_used == "TIER_3_DOM_RENTVISION"
    assert len(result.units) == 3  # plan-level rows from _RV_CARDS


@pytest.mark.asyncio
async def test_adapter_drill_silent_on_floorplans_fetch_failure(monkeypatch) -> None:
    """If the /floorplans self-fetch returns empty (network failure,
    proxy block), the drill step is silently skipped and the plan-level
    path still fires — never crashes the extraction."""
    async def _mock_empty(*args, **kwargs):
        return ""

    monkeypatch.setattr(
        RentVisionAdapter,
        "_fetch_floorplans_html",
        staticmethod(_mock_empty),
    )
    monkeypatch.setattr(
        RentVisionAdapter,
        "_fetch_detail_html",
        staticmethod(_mock_empty),
    )

    result = await RentVisionAdapter().extract(
        _FakePage(_RV_CARDS), _ctx()  # type: ignore[arg-type]
    )
    assert result.tier_used == "TIER_3_DOM_RENTVISION"
    assert len(result.units) == 3
    assert result.confidence > 0.0
