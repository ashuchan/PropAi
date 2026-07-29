"""F11: AppFolio /listings-direct path with SSR DOM fallback.

Validates H18 (live SSR pages on captcha-blocked tenants like becovic,
pillarrei, blackrealtymanagement, plentyofplaces extract correctly via
the js-listing-* class names) plus the offboarded-tenant detection path.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from ma_poc.pms.adapters.appfolio import (
    AppFolioAdapter,
    _extract_unit_from_address,
    parse_appfolio_detail_page,
    parse_appfolio_listings_ssr,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import detect_pms
from ma_poc.pms.resolver import normalize_appfolio_url


class _DummyPage:
    """Stand-in for the playwright Page argument; adapter only reads
    fetch_result.body in F11's SSR fallback, not anything from the page."""


@dataclass
class _StubFetchResult:
    body: bytes | str | None


_RICHELSON_LISTING_FRAGMENT = """
<html><body>
<article class="listing-item result js-listing-item" data-listing-id="233">
  <div class="js-listing-blurb-rent">$1,335</div>
  <div class="js-listing-blurb-bed-bath">3 bd / 2 ba</div>
  <div class="js-listing-square-feet">Square Feet: 1,342</div>
  <div class="js-listing-available">5/22/26</div>
  <div class="js-listing-address"><span>123 Main St</span></div>
</article>
<article class="listing-item result js-listing-item" data-listing-id="265">
  <div class="js-listing-blurb-rent">$1,500</div>
  <div class="js-listing-blurb-bed-bath">Studio / 1 ba</div>
  <div class="js-listing-square-feet">Square Feet: 540</div>
  <div class="js-listing-available">6/15/26</div>
  <div class="js-listing-address"><span>456 Oak Ave</span></div>
</article>
</body></html>
"""

_OFFBOARDED_PAGE = """
<html><body>
<script>window.location.replace("https://www.appfolio.com/page-not-found-sub");</script>
</body></html>
"""

# Verified 2026-05-05 against pablogroup.appfolio.com — the actual offboarded
# tenant response body that AppFolio serves after the 302 redirect.
_OFFBOARDED_PAGE_TITLE_VARIANT = """
<!DOCTYPE html><html><head><title>AppFolio - Page Not Found</title></head>
<body><h1>Page Not Found</h1></body></html>
"""


# ---- normalize_appfolio_url unit tests -----------------------------------


@pytest.mark.parametrize("inp,expected", [
    # Bare tenant root → /listings
    ("https://richelsonmanagement.appfolio.com/", "https://richelsonmanagement.appfolio.com/listings"),
    ("https://becovic.appfolio.com", "https://becovic.appfolio.com/listings"),
    ("https://becovic.appfolio.com/some/random/path", "https://becovic.appfolio.com/listings"),
    # Already on /listings — pass through unchanged
    ("https://becovic.appfolio.com/listings", "https://becovic.appfolio.com/listings"),
    ("https://becovic.appfolio.com/listings/", "https://becovic.appfolio.com/listings/"),  # trailing slash
    ("https://becovic.appfolio.com/listings/233", "https://becovic.appfolio.com/listings/233"),
    ("https://becovic.appfolio.com/listings?q=1", "https://becovic.appfolio.com/listings?q=1"),
    # Static marketing site — never touched
    ("https://www.appfolio.com/property-manager", "https://www.appfolio.com/property-manager"),
    ("https://www.appfolio.com/", "https://www.appfolio.com/"),
    # Non-AppFolio host — never touched
    ("https://marketstationapartmentsnc.com/", "https://marketstationapartmentsnc.com/"),
    ("https://marketstationapartmentsnc.com/apartments/floorplans/", "https://marketstationapartmentsnc.com/apartments/floorplans/"),
    # Mixed-case host — normalized via .lower() comparison
    ("https://BECOVIC.AppFolio.COM/", "https://BECOVIC.AppFolio.COM/listings"),
    # Empty / malformed inputs — passed through
    ("", ""),
    # Bare apex appfolio.com (no subdomain) — pass through (not a tenant)
    ("https://appfolio.com/", "https://appfolio.com/"),
])
def test_normalize_appfolio_url_table_driven(inp: str, expected: str) -> None:
    assert normalize_appfolio_url(inp) == expected


# ---- parse_appfolio_listings_ssr (regex on production-shape HTML) -------


def test_h18_ssr_parser_extracts_richelson_shape() -> None:
    """Verified against the live richelsonmanagement.appfolio.com page on
    2026-05-05 (8 cards, all with rent + bed/bath + sqft + avail)."""
    units = parse_appfolio_listings_ssr(
        _RICHELSON_LISTING_FRAGMENT,
        "https://richelsonmanagement.appfolio.com/listings",
    )
    assert len(units) == 2
    u1 = units[0]
    assert u1["unit_number"] == "233"
    assert "$1,335" in u1["rent_range"]
    assert u1["bedrooms"] == "3"
    assert u1["bathrooms"] == "2.0"
    assert u1["sqft"] == "1342"
    assert u1["extraction_tier"] == "TIER_1_DOM_APPFOLIO_SSR"
    u2 = units[1]
    assert u2["unit_number"] == "265"
    # Studio is parsed as bedrooms=0
    assert u2["bedrooms"] == "0"


def test_ssr_parser_returns_empty_when_no_listing_cards() -> None:
    """Pages without data-listing-id markers yield no units (no false
    positives from incidentally-named classes)."""
    units = parse_appfolio_listings_ssr(
        "<html><body><div class='js-listing-blurb-rent'>$0</div></body></html>",
        "https://example.com",
    )
    assert units == []


# ─────────────────────────────────────────────────────────────────────
# 2026-05-24 — audit xlsx (2026-05-23) flagged 9 AppFolio properties
# with "didn't find this unit". Root cause: SSR adapter stored
# unit_number = listing_id (AppFolio internal). The real unit number
# lives in the address suffix (e.g. '#810', 'Apt 429', '- V024,').
# These tests pin the address-suffix extractor + verify the SSR parser
# prefers the parsed suffix over the listing_id.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("addr, expected", [
    # Pattern 1 — hash-prefixed (Carlton / Becovic — most common)
    ("1422 Som Center Road #810, Mayfield Heights, OH 44124", "810"),
    ("1414 Som Center Road #503, Mayfield Heights, OH 44124", "503"),
    ("6012 N Kenmore Ave #2D, Chicago, IL 60660", "2D"),
    ("1552.5 W Juneway Ter. #3H, Chicago, IL 60626", "3H"),
    ("1556 W Juneway Ter. #1O, Chicago, IL 60626", "1O"),
    ("5840 Mckahan Ct #5840, Columbus, OH 43232", "5840"),
    # Pattern 2 — Apt/Apartment (kelseymanagement → Brantley Pines)
    ("2620 Wild Pines Ln, Apt 429, Naples, FL 34112", "429"),
    ("9305 Takomah Trail, Apt 213, Tampa, FL 33617", "213"),
    ("4803 Mandy Avenue, Apt 8, Tampa, FL 33617", "8"),
    # Pattern 3 — Suite / Unit / Ste
    ("123 Main St, Suite 12, Anytown, CA 90001", "12"),
    ("123 Main St, Unit 5B, Anytown, CA 90001", "5B"),
    ("123 Main St, Ste 200, Anytown, CA 90001", "200"),
    # Pattern 4 — dash-separated suffix before comma (bargeprops + becovic)
    ("3623 McCann Road - 2043, Longview, TX 75602", "2043"),
    ("1625 W Howard - 305, Chicago, IL 60626", "305"),
    ("3050 E. Fountain Blvd - 3050-302, Colorado Springs, CO 80910",
     "3050-302"),
    ("Parker Apartments - V024, 5105 Bullard Road, Tyler, TX 75703", "V024"),
    # Pattern 5 — trailing numeric before first comma (no dash, no hash)
    ("301 W. Hawkins Parkway 1116, Longview, TX", "1116"),
    ("1810 Marlandwood Road 9208, Temple, TX 76502", "9208"),
    # Pattern 6 — inter-comma alphanumeric token
    # (americancapitalrealty/Citadel + Riverside North)
    ("4121 San Antonio St, 614, Odessa, TX 79765", "614"),
    ("1349 Redmond Circle, G1-47, Rome, GA 30165", "G1-47"),
    # No unit (single-family / townhouse) — empty
    ("355 Monument Road, Jacksonville, FL 32225", ""),
    ("7789 Club Ridge Rd, Westerville, OH 43081", ""),
    ("301 Nat Turner Blvd, Newport News, VA 23606", ""),
    ("852 Park Road, Westerville, OH 43081", ""),
    # Defensive — empty / None-like
    ("", ""),
])
def test_extract_unit_from_address_matrix(addr: str, expected: str) -> None:
    """End-to-end fixture set — every shape from the 9 AppFolio audit
    failures + the 5 live tenants probed on 2026-05-24."""
    assert _extract_unit_from_address(addr) == expected


def test_ssr_parser_prefers_address_suffix_over_listing_id() -> None:
    """The 2026-05-23 audit's signature case: address contains '#810';
    the listing_id is 760. The fixed parser must surface unit_number =
    '810' (what the website displays), not 760 (internal id). The
    listing_id is preserved in source_ids for provenance."""
    html = """
    <article class="listing-item result js-listing-item" data-listing-id="760">
      <div class="js-listing-blurb-rent">$1,939</div>
      <div class="js-listing-blurb-bed-bath">3 bd / 2 ba</div>
      <div class="js-listing-square-feet">Square Feet: 904</div>
      <div class="js-listing-available">5/30/26</div>
      <div class="js-listing-address">
        <span>1422 Som Center Road #810, Mayfield Heights, OH 44124</span>
      </div>
    </article>
    """
    units = parse_appfolio_listings_ssr(html, "https://carltonequities.appfolio.com/listings")
    assert len(units) == 1
    u = units[0]
    assert u["unit_number"] == "810", (
        f"expected '810' (from #810 in address), got {u['unit_number']!r} "
        f"— the AppFolio listing_id leak is back."
    )
    # 2026-07-28: the address belongs in unit_name, NOT floor_plan_name.
    # AppFolio SSR cards publish no plan name at all (verified live), so
    # floor_plan_name stays empty rather than carrying an address.
    assert "1422 Som Center Road" in u["unit_name"]
    assert u["floor_plan_name"] == ""
    # listing_id preserved in source_ids for downstream provenance.
    # (Make_unit_dict returns it as 'source_ids', a serialized dict, OR
    # it appears in the appfolio_listing_id field on the unit dict —
    # the exact representation depends on make_unit_dict's contract.)
    # Smoke check: 760 must appear SOMEWHERE in the row so we can
    # cross-reference back to AppFolio later if needed.
    assert "760" in str(u), "listing_id 760 should be preserved in the unit dict"


def test_ssr_parser_handles_address_span_without_inner_tag() -> None:
    """kelseymanagement (Brantley Pines I) shape: the address text sits
    DIRECTLY inside the js-listing-address span, with no inner <span>.
    The prior regex required an inner tag — this is the bug that made
    Brantley Pines' address capture fail entirely (which used to surface
    as the 'AppFolio listing 193' placeholder in floor_plan_name)."""
    html = """
    <article class="listing-item js-listing-item" data-listing-id="193">
      <div class="js-listing-blurb-rent">$1,625</div>
      <div class="js-listing-blurb-bed-bath">2 bd / 2 ba</div>
      <div class="js-listing-square-feet">Square Feet: 616</div>
      <div class="js-listing-available">6/1/26</div>
      <span class="u-pad-rm js-listing-address">2620 Wild Pines Ln, Apt 429, Naples, FL 34112</span>
    </article>
    """
    units = parse_appfolio_listings_ssr(html, "https://kelseymanagement.appfolio.com/listings")
    assert len(units) == 1
    u = units[0]
    assert u["unit_number"] == "429", (
        f"address-suffix should extract 'Apt 429' → '429'; "
        f"got {u['unit_number']!r} (likely the listing_id 193 leaked)"
    )
    # 2026-07-28: address capture is still the thing under test — it just
    # lands in unit_name now. An empty unit_name would mean the inner-tag
    # regression is back.
    assert "2620 Wild Pines Ln" in u["unit_name"], (
        "address capture failed — the inner-tag regex regression is back"
    )
    assert u["floor_plan_name"] == "", (
        "AppFolio SSR publishes no plan name; floor_plan_name must stay "
        "empty rather than carry an address or a listing_id placeholder"
    )


def test_ssr_parser_falls_back_to_listing_id_when_address_has_no_unit() -> None:
    """Single-family rentals (e.g. '355 Monument Road, Jacksonville, FL')
    legitimately have no unit suffix. Rather than drop the unit_number
    entirely (which would break row identity), fall back to the
    listing_id — this preserves the prior behaviour for that cohort."""
    html = """
    <article class="listing-item js-listing-item" data-listing-id="9328">
      <div class="js-listing-blurb-rent">$2,200</div>
      <div class="js-listing-blurb-bed-bath">3 bd / 2 ba</div>
      <div class="js-listing-square-feet">Square Feet: 1,500</div>
      <span class="js-listing-address">101 Little Bay Avenue, Yorktown, VA 23693</span>
    </article>
    """
    units = parse_appfolio_listings_ssr(html, "https://artcraft.appfolio.com/listings")
    assert len(units) == 1
    # No # / Apt / dash — fall back to listing_id (the only stable id we have)
    assert units[0]["unit_number"] == "9328"


# ---- AppFolioAdapter end-to-end with SSR fallback ------------------------


def _make_ctx(
    base_url: str,
    api_responses: list[dict] | None = None,
    body: bytes | str | None = None,
) -> AdapterContext:
    ctx = AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
        fetch_result=_StubFetchResult(body=body) if body is not None else None,
    )
    ctx._api_responses = api_responses or []  # type: ignore[attr-defined]
    return ctx


@pytest.mark.asyncio
async def test_h18_ssr_fallback_when_api_responses_empty() -> None:
    """API tier returns 0 units → adapter falls back to SSR DOM parse and
    extracts ≥1 unit from the listings HTML body."""
    ctx = _make_ctx(
        "https://richelsonmanagement.appfolio.com/listings",
        api_responses=[],
        body=_RICHELSON_LISTING_FRAGMENT.encode("utf-8"),
    )
    adapter = AppFolioAdapter()
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 2
    assert result.tier_used == "TIER_1_DOM_APPFOLIO_SSR"
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_offboarded_tenant_detected_via_url_marker() -> None:
    """Pablogroup-shape tenant: page redirects to page-not-found-sub →
    adapter records the signal and returns 0 units cleanly."""
    ctx = _make_ctx(
        "https://pablogroup.appfolio.com/listings",
        body=_OFFBOARDED_PAGE.encode("utf-8"),
    )
    adapter = AppFolioAdapter()
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.tier_used == "TIER_1_APPFOLIO_TENANT_OFFBOARDED"


@pytest.mark.asyncio
async def test_offboarded_tenant_detected_via_title_marker() -> None:
    """Bug-hunt regression: even when the page-not-found-sub URL string
    isn't literally in the body, the AppFolio-served title is the same
    on every offboarded tenant. The detection must catch both signals."""
    ctx = _make_ctx(
        "https://pablogroup.appfolio.com/listings",
        body=_OFFBOARDED_PAGE_TITLE_VARIANT.encode("utf-8"),
    )
    adapter = AppFolioAdapter()
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.tier_used == "TIER_1_APPFOLIO_TENANT_OFFBOARDED"


@pytest.mark.asyncio
async def test_api_path_wins_when_data_present() -> None:
    """When the API tier produces units, the SSR fallback does not run."""
    api_resp = {
        "url": "https://example.appfolio.com/api/v1/listings/",
        "body": {
            "objects": [
                {
                    "name": "1BR",
                    "bedrooms": 1,
                    "bathrooms": 1,
                    "price": 1500,
                    "sq_ft": 750,
                    "unit_number": "U101",
                }
            ]
        },
    }
    # SSR body intentionally has 0 listing markers so we'd notice if it ran
    ctx = _make_ctx(
        "https://example.appfolio.com/listings",
        api_responses=[api_resp],
        body=b"<html>no cards here</html>",
    )
    adapter = AppFolioAdapter()
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1
    assert result.tier_used == "TIER_1_API_APPFOLIO"


@pytest.mark.asyncio
async def test_no_api_no_html_returns_clean_failure() -> None:
    """When neither API nor SSR HTML is available, adapter reports the
    miss with confidence=0 — never raises."""
    ctx = _make_ctx("https://example.appfolio.com/listings", api_responses=[], body=None)
    adapter = AppFolioAdapter()
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0
    assert result.errors  # non-empty diagnostic


# ── Bug 7 (2026-05-09) — /listings/detail/<uuid> parser ──────────────────────


_DETAIL_PAGE = """
<html><head><title>Gage Crossing</title></head><body>
<main>
  <h1>Gage Crossing — 2BR Modern</h1>
  <div class="rent-block">$2,150 / month</div>
  <div class="specs">2 bd / 2 ba</div>
  <div class="sqft">1,150 sqft</div>
  <p>Available August 1, 2026</p>
</main>
</body></html>
"""


def test_bug7_detail_parser_extracts_full_specs() -> None:
    """Bug 7: /listings/detail/<uuid> page yields one unit with rent, beds,
    baths, sqft, and floor_plan_name from the H1."""
    units = parse_appfolio_detail_page(
        _DETAIL_PAGE,
        "https://ardentpm.appfolio.com/listings/detail/cbc67d94-deadbeef",
    )
    assert len(units) == 1
    u = units[0]
    assert u["floor_plan_name"] == "Gage Crossing — 2BR Modern"
    assert u["bedrooms"] == "2"
    assert u["bathrooms"] == "2"
    assert u["sqft"] == "1150"
    assert u["market_rent_low"] == 2150
    assert u["market_rent_high"] == 2150
    assert "$2,150" in u["rent_range"]
    assert u["extraction_tier"] == "TIER_1_DOM_APPFOLIO_DETAIL"


def test_bug7_detail_parser_returns_empty_without_rent() -> None:
    """Bug 7: a page with no $XXX rent token (auth interstitial / sign-in
    redirect) yields no units — guard against extracting from non-listing
    pages that happen to have /listings/detail/ in the URL."""
    units = parse_appfolio_detail_page(
        "<html><body><main><h1>Sign In</h1><p>Please log in.</p></main></body></html>",
        "https://example.appfolio.com/listings/detail/abc",
    )
    assert units == []


def test_bug7_detail_parser_falls_back_to_full_html_when_no_main() -> None:
    """Bug 7: pages without a <main> element still parse — the regex falls
    back to the entire HTML body."""
    html = "<html><body><h1>Plan A</h1><div>$1,500/mo · 1 bed · 1 bath · 700 sqft</div></body></html>"
    units = parse_appfolio_detail_page(
        html, "https://example.appfolio.com/listings/detail/x"
    )
    assert len(units) == 1
    assert units[0]["bedrooms"] == "1"
    assert units[0]["market_rent_low"] == 1500


@pytest.mark.asyncio
async def test_bug7_adapter_routes_detail_url_to_detail_parser() -> None:
    """Bug 7: AppFolioAdapter.extract() runs the detail parser when the
    fetch_result.final_url contains ``/listings/detail/`` and the SSR
    listing-id path returned nothing."""

    @dataclass
    class _DetailFetchResult:
        body: bytes | str | None
        final_url: str

    ctx = AdapterContext(
        base_url="https://ardentpm.appfolio.com/listings/detail/cbc67d94",
        detected=detect_pms("https://ardentpm.appfolio.com/listings/detail/cbc67d94"),
        profile=None,
        expected_total_units=None,
        property_id="P_BUG7",
        fetch_result=_DetailFetchResult(
            body=_DETAIL_PAGE.encode("utf-8"),
            final_url="https://ardentpm.appfolio.com/listings/detail/cbc67d94",
        ),
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    result = await AppFolioAdapter().extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1
    assert result.tier_used == "TIER_1_DOM_APPFOLIO_DETAIL"
    assert result.confidence == 0.85
