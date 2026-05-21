"""GenericPlanTextAdapter (2026-05-21, HAR-validation greenfield).

Last-resort plan-level text extractor for bespoke / custom-CMS sites
that don't route to any other adapter. Body texts captured live from:
  - colonialcourtapts.com (Drupal)
  - stargatewest.com (custom WP)
  - countryvillageapthomes.com (slick carousel)
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.generic_plan_text import (
    GenericPlanTextAdapter,
    parse_generic_plan_text,
)
from ma_poc.pms.detector import detect_pms


# Live-captured body text excerpts (real DOM ).
_COLONIAL_BODY = (
    "Skip to main content Reach Us: (248) 646-1188 Office Hours: Mon-Fri "
    "9:00am-5:00pm Search HOME FLOOR PLANS APPLICATION RESIDENT SERVICES "
    "Floor Plans - Any - 2 Bedroom / 1 Bath Apartment 2 Bedroom / 1 Bath "
    "Townhome 2 Bedroom / 1.5 Bath Townhome 3 Bedroom / 1.5 Bath Townhome "
    "2 Bedroom / 1 Bath Apartment ** NO APARTMENTS AVAILABLE AT THIS TIME ** "
    "$1495 *All Prices Subject to Change and Availability "
    "2 Bedroom / 1 Bath Townhome $1450 - $1525 *All Prices Subject to Change "
    "2 Bedroom / 1.5 Bath Townhome $1945 *All Prices Subject to Change "
    "3 Bedroom / 1.5 Bath Townhome $2100 - $2200 *All Prices Subject to Change"
)

_STARGATE_BODY = (
    "LUXURY APARTMENTS IN TUCSON, ARIZONA (520) 623-5336 RENTAL APPLICATION "
    "Floor Plans and Rental Rates RENTAL RATES Rates subject to change "
    "1 Bedroom / 1 Bathroom From $1275 "
    "2 Bedroom / 2 Bathroom From $1400 "
    "2 Bedroom / 1.5 Bathroom From $1425 "
    "3 Bedroom / 2 Bathroom From $1655 "
    "Complete a Rental Application"
)

_COUNTRYVILLAGE_BODY = (
    "Floor Plans Photos Map FAQ Application (903) 891-1166 "
    "1 Bedroom 1 Bath 750 Sq Ft Starting at $852 Deposit $300 "
    "2 Bedroom 1 Bath 850 Sq Ft Starting at $978 Deposit $300 "
    "2 Bedroom 2 Bath 950 Sq Ft Starting at $982 Deposit $300 "
    "3 Bedroom 2 Bath 1150 Sq Ft Starting at $1100 Deposit $400"
)

# Edge: a marketing blurb with a single "1 bedroom ... $500" amenity line
# (NOT a real plan listing). Must NOT match (anti-noise ≥2 guard).
_AMENITY_NOISE = (
    "Welcome to our community! Cozy 1 bedroom homes available from $500 "
    "deposit. Pet rent: $35 per cat. Contact us for tour."
)


# ── parse_generic_plan_text tests ──


def test_parse_colonial_court() -> None:
    rows = parse_generic_plan_text(_COLONIAL_BODY, "https://www.colonialcourtapts.com/floor-plans")
    # Drupal renders the nav menu (4 plans repeated) + the body (4 plans
    # with rents). The deduper collapses identical signatures to 4.
    assert len(rows) == 4
    names = sorted(r["floor_plan_name"] for r in rows)
    assert all("Bedroom" in n or "Studio" in n for n in names)
    # Cheapest plan is $1450 (1 Bath Townhome range)
    rents = sorted(r["market_rent_low"] for r in rows)
    assert rents[0] == 1450


def test_parse_stargatewest() -> None:
    rows = parse_generic_plan_text(_STARGATE_BODY, "https://stargatewest.com/x/")
    assert len(rows) == 4
    rents = sorted(r["market_rent_low"] for r in rows)
    assert rents == [1275, 1400, 1425, 1655]
    bedrooms = sorted(int(r["bedrooms"]) for r in rows)
    assert bedrooms == [1, 2, 2, 3]


def test_parse_countryvillage() -> None:
    """Body has deposits ($300, $400) interleaved with rents. The
    parser's _RENT_FLOOR=$400 lets the cheapest deposit ($300) be
    rejected as too-low for rent. Only real rents ($852, $978, $982,
    $1100) qualify."""
    rows = parse_generic_plan_text(_COUNTRYVILLAGE_BODY, "u")
    assert len(rows) == 4
    rents = sorted(r["market_rent_low"] for r in rows)
    assert rents == [852, 978, 982, 1100]
    sqfts = sorted(r["sqft"] for r in rows)
    assert sqfts == ["1150", "750", "850", "950"]


def test_parse_amenity_noise_does_NOT_match() -> None:
    """Single 'X bedroom from $X' amenity blurb must NOT yield rows —
    the ≥2-row threshold kills this kind of noise."""
    rows = parse_generic_plan_text(_AMENITY_NOISE, "u")
    assert rows == []


def test_parse_rejects_deposit_only_rows() -> None:
    """A row where the only $-amount is a $300 deposit (below the
    _RENT_FLOOR of $400) must NOT emit. The ≥2 distinct rows guard
    also fires here."""
    body = (
        "1 Bedroom 1 Bath $300 deposit only "
        "2 Bedroom 1 Bath $300 deposit only"
    )
    rows = parse_generic_plan_text(body, "u")
    assert rows == []  # both rows skipped due to no real rent


def test_parse_studio_marker() -> None:
    body = (
        "Studio Bedroom 1 Bath 350 Sq Ft From $1100 "
        "1 Bedroom 1 Bath 500 Sq Ft From $1300"
    )
    rows = parse_generic_plan_text(body, "u")
    assert len(rows) == 2
    studio = next(r for r in rows if r["bedrooms"] == "0")
    assert studio["market_rent_low"] == 1100


def test_parse_dedupes_repeated_plan_rows() -> None:
    """Drupal/WP often render the nav menu items + the body content
    (same plan list twice). Same signature must dedupe."""
    body = (
        "2 Bedroom 1 Bath $1200 "
        "2 Bedroom 1 Bath $1200 "  # exact duplicate
        "3 Bedroom 2 Bath $1500"
    )
    rows = parse_generic_plan_text(body, "u")
    assert len(rows) == 2


# ── adapter end-to-end ──


class _FakePage:
    def __init__(self, body, url="https://stargatewest.com/x/"):
        self._body = body
        self.url = url

    async def evaluate(self, _js):
        return {"ok": True, "bodyText": self._body}


@pytest.mark.asyncio
async def test_adapter_extracts_stargatewest() -> None:
    ctx = AdapterContext(
        base_url="https://stargatewest.com/",
        detected=detect_pms("https://stargatewest.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await GenericPlanTextAdapter().extract(_FakePage(_STARGATE_BODY), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_GENERIC_PLAN_TEXT"
    assert len(result.units) == 4
    # Confidence capped at 0.70 — every real PMS adapter outranks this.
    assert result.confidence <= 0.70


@pytest.mark.asyncio
async def test_adapter_bails_on_empty_body() -> None:
    ctx = AdapterContext(
        base_url="https://x.test/",
        detected=detect_pms("https://x.test/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await GenericPlanTextAdapter().extract(_FakePage(""), ctx)  # type: ignore[arg-type]
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_adapter_bails_on_amenity_noise_single_match() -> None:
    """Single 'X bedroom from $X' blurb → adapter must NOT confidence."""
    ctx = AdapterContext(
        base_url="https://x.test/",
        detected=detect_pms("https://x.test/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )
    result = await GenericPlanTextAdapter().extract(_FakePage(_AMENITY_NOISE), ctx)  # type: ignore[arg-type]
    assert result.confidence == 0.0
    assert "<2 distinct plan rows" in " ".join(result.errors)


# ── detector ──


def test_detector_yields_generic_plan_text_at_low_confidence() -> None:
    """The signal fires on any HTML with bedroom + $, but at 0.55 — so
    a real PMS detector (0.85+) always wins. Verify the confidence."""
    from ma_poc.pms.detector import _iter_html_markers
    html = "<body>2 Bedroom 1 Bath $1500</body>"
    markers = list(_iter_html_markers(html.lower()))
    gpt = [m for m in markers if m[0] == "generic_plan_text"]
    assert len(gpt) == 1
    assert gpt[0][1] == 0.55  # confidence


def test_detector_does_not_yield_without_dollar() -> None:
    """No $-prefix in HTML → no generic_plan_text marker."""
    from ma_poc.pms.detector import _iter_html_markers
    html = "<body>2 Bedroom apartments coming soon</body>"
    markers = list(_iter_html_markers(html.lower()))
    assert not [m for m in markers if m[0] == "generic_plan_text"]


def test_adapter_registered() -> None:
    a = get_adapter("generic_plan_text")
    assert isinstance(a, GenericPlanTextAdapter)


def test_strategy_is_dom_first() -> None:
    from ma_poc.pms.detector import _STRATEGY_BY_PMS
    assert _STRATEGY_BY_PMS["generic_plan_text"] == "dom_first"
