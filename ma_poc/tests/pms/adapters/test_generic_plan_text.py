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


class _FakeProbeResponse:
    """Minimal curl_cffi-response shim (``.status_code`` / ``.text``)."""

    __slots__ = ("status_code", "text", "content", "headers", "url")

    def __init__(self, url: str, status_code: int = 404, text: str = "") -> None:
        self.url = url
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _stub_probe_get(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the ``_probe`` seam so no subpage probe leaves the box.

    ``GenericPlanTextAdapter.extract`` fires ``probe_get`` on two
    optional side-quests: the ``/floorplans/`` … ``/apartments/``
    subpage sweep when the body yields no rows, and
    ``_enrich_sqft_from_floorplans`` when fewer than half the rows carry
    sqft. Neither is what these tests assert on — the extraction subject
    is the live-captured ``_*_BODY`` text below, fed straight to the
    parser. A 404 makes both side-quests deterministic no-ops.
    """

    def _fake_probe_get(url: str, **_kw: object) -> _FakeProbeResponse:
        return _FakeProbeResponse(url)

    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get", _fake_probe_get
    )


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
    # #91 BUG2 (2026-07-30): plan-text rows carry NO availability signal, so
    # they must be UNKNOWN, never a false AVAILABLE — this body literally says
    # "NO APARTMENTS AVAILABLE AT THIS TIME". A false AVAILABLE also fabricates
    # an available_date via the schema_v2 scrape-date fallback (Henry/Frontera).
    assert all(r["availability_status"] == "UNKNOWN" for r in rows), [
        r["availability_status"] for r in rows
    ]


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


def test_elementor_starting_at_before_bed_bath_1045_on_the_park() -> None:
    """2026-05-25 deep-probe cluster A — 1045 on the Park (Princeton Mgmt
    Elementor-themed WP site, user-flagged 2026-05-25). Rent text
    appears IMMEDIATELY BEFORE the bed/bath line::

        Starting at $2,127 1 Bed | 1 Bath

    Pre-fix the forward-only lookahead skipped these (no $ in the 100
    chars after Bath) so emit was 0 rows. The backwards-lookup fix
    catches them.
    """
    body = (
        "Residences starting at $2,127\n"
        "1 and 2 Bedroom Luxury Apartments\n"
        "Starting at $2,865 2 Bed | 2 Bath\n"
        "Starting at $2,509 2 Bed | 2 Bath\n"
        "Starting at $2,127 1 Bed | 1 Bath\n"
    )
    rows = parse_generic_plan_text(body, "http://www.1045onthepark.com/")
    # Each plan line yields one row; the two "2 Bed | 2 Bath" lines
    # carry distinct rents ($2,865 + $2,509) so they're NOT dedup'd.
    assert len(rows) >= 3, f"expected >=3 rows, got {len(rows)}: {rows}"
    # 1-Bed row must be present (skipped entirely pre-fix)
    has_one_bed = any(r.get("bedrooms") == "1" for r in rows)
    assert has_one_bed, f"no 1-bed row: {[r.get('bedrooms') for r in rows]}"
    # 2-Bed row must be present
    has_two_bed = any(r.get("bedrooms") == "2" for r in rows)
    assert has_two_bed, f"no 2-bed row: {[r.get('bedrooms') for r in rows]}"
    # Three distinct rent levels must appear across all rows
    rents = {int(r["market_rent_low"]) for r in rows if r.get("market_rent_low")}
    assert 2127 in rents, f"missing $2,127 1-Bed rent: {rents}"
    assert 2509 in rents, f"missing $2,509 2-Bed rent: {rents}"
    assert 2865 in rents, f"missing $2,865 2-Bed rent: {rents}"


def test_backwards_lookup_does_not_cross_prior_plan_boundary() -> None:
    """Defensive: when previous plan's rent sits BEFORE the current
    bed/bath token, the boundary check must stop the backwards search
    from pulling it. Without the boundary guard the 1-Bed row would
    inherit the previous 2-Bed's $3,500 rent."""
    body = (
        "Starting at $3,500 2 Bed | 2 Bath\n"
        "Some amenity blurb in between, no dollar sign here.\n"
        "1 Bed | 1 Bath"  # no rent before OR after — must skip
    )
    rows = parse_generic_plan_text(body, "u")
    one_bed_rows = [r for r in rows if r.get("bedrooms") == "1"]
    # Either the 1-bed row is skipped entirely (no rent found, correct),
    # OR it has rent that is NOT the $3,500 from the prior plan.
    for r in one_bed_rows:
        rent = r.get("market_rent_low")
        assert rent != 3500, f"1-bed row leaked prior plan's rent: {r}"


def test_backwards_lookup_below_rent_floor_rejected() -> None:
    """Defensive: a small $-amount (e.g. $150 application fee) just
    before the bed/bath token must NOT be picked up as rent. Backwards
    lookup must honor the RENT_FLOOR threshold (400)."""
    body = "Application fee $150 1 Bed | 1 Bath"
    rows = parse_generic_plan_text(body, "u")
    # 1-bed row should NOT emit because no real rent is present.
    one_bed_rent_rows = [
        r for r in rows
        if r.get("bedrooms") == "1" and r.get("market_rent_low")
    ]
    assert one_bed_rent_rows == [], (
        f"backwards lookup picked up fee as rent: {one_bed_rent_rows}"
    )


def test_boundary_regex_recognizes_bare_bed_token() -> None:
    """Regression: pre-fix the _NEXT_PLAN_BOUNDARY_RE only matched
    ``bedroom|bdrm|bd|br`` (missing bare ``bed``), so forward-window
    rent for the SECOND ``X Bed | X Bath`` plan would leak back to the
    first. The boundary regex must now recognise bare ``bed`` too."""
    from ma_poc.pms.adapters.generic_plan_text import _NEXT_PLAN_BOUNDARY_RE
    assert _NEXT_PLAN_BOUNDARY_RE.search("2 Bed | 2 Bath") is not None
    assert _NEXT_PLAN_BOUNDARY_RE.search("studio") is not None
    assert _NEXT_PLAN_BOUNDARY_RE.search("1 Bedroom") is not None


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


# ─── bed-only range pattern (Sunflower aggregator, 2026-05-23) ───────


def test_parse_bed_only_range_pleasantview_sunflower() -> None:
    """Pleasant View Gardens (Sunflower aggregator) — bed-range + $ in
    adjacent divs collapse to: ``1, 2 beds $1959 - $2409``. The old
    bed+bath regex missed this; the new bed-only-range pattern catches
    it. Emits one row per bed in the range (1-bed + 2-bed = 2 rows)."""
    body = (
        "Pleasant View Gardens 1, 2 beds $1959 - $2409 "
        "Sandy Pond 2, 3 beds $2189 - $2599"
    )
    units = parse_generic_plan_text(body, "https://example.com")
    # Expect 1-bed, 2-bed (from Pleasant View) + 2-bed, 3-bed (from
    # Sandy Pond), then dedup by signature. The dedup key is (beds,
    # bath, sqft, low, high) so 2-bed @1959-2409 vs 2-bed @2189-2599
    # are distinct.
    assert len(units) >= 3
    by_beds = {(u["bedrooms"], u["market_rent_low"]) for u in units}
    assert ("1", 1959) in by_beds
    assert ("2", 1959) in by_beds  # 1, 2 beds → both at same rent
    assert ("3", 2189) in by_beds


def test_parse_bed_only_does_not_emit_without_rent() -> None:
    """A bed-only range with no nearby $ rent must NOT emit (defends
    against amenity-text matches)."""
    body = "Welcome to Whispering Pines. We offer 1, 2 beds in a serene setting."
    assert parse_generic_plan_text(body, "https://example.com") == []


def test_parse_bed_only_bails_when_bed_bath_pass_already_succeeded() -> None:
    """If primary bed+bath pass already produced ≥2 rows, the bed-only
    pass should NOT run (avoid double-counting)."""
    body = (
        "Floor Plan A: 1 Bedroom / 1 Bathroom $1500. "
        "Floor Plan B: 2 Bedroom / 2 Bathroom $2000. "
        "Also we have 1, 2 beds available $1500 - $2000"
    )
    units = parse_generic_plan_text(body, "https://example.com")
    # Primary pass picks up A + B. Second pass should NOT run because
    # len(out) >= 2 after the first pass.
    bed_baths = {u["bathrooms"] for u in units}
    assert "1" in bed_baths or "1.0" in bed_baths or "" not in bed_baths
    # All units should have bath captured (came from primary pass).
    assert all(u["bathrooms"] for u in units)


def test_parse_bed_only_range_implausible_rejected() -> None:
    """Bed range >6 implausible — reject."""
    body = "1, 99 beds available $1500 - $2000 here"
    assert parse_generic_plan_text(body, "https://example.com") == []


# ─── short-bed-token + single-bed-with-rent (2026-05-23 cohort grind) ────


def test_parse_bed_short_token_alders_grove() -> None:
    """Alders Grove: "2 Bed, 1.5 Bath Units 945 sq ... $1,700 month" —
    the short "Bed"/"Bath" tokens + comma separator. Both added in the
    second-pass DOM_GENERIC fix."""
    body = (
        "Resident Specials! 2 Bed, 1.5 Bath Units 945 sq w/ Garage "
        "$1,700 month $1,700 deposit. 1 Bed, 1 Bath Units 700 sq "
        "$1,300 month $1,300 deposit."
    )
    units = parse_generic_plan_text(body, "https://example.com")
    assert len(units) >= 2
    rents = {u["market_rent_low"] for u in units}
    assert 1700 in rents
    assert 1300 in rents


def test_parse_single_bed_with_rent_towne_crest() -> None:
    """Towne Crest: '1 Bedrooms starting at just $1500/month & 2
    Bedrooms starting at only $1748/month' — single bed token, no
    bath, with /month suffix gate."""
    body = (
        "May Special! 1 Bedrooms starting at just $1500/month & "
        "2 Bedrooms starting at only $1748/month! Some restrictions apply."
    )
    units = parse_generic_plan_text(body, "https://example.com")
    assert len(units) >= 2
    rents = {u["market_rent_low"] for u in units}
    assert 1500 in rents
    assert 1748 in rents


def test_parse_single_bed_requires_per_month_gate() -> None:
    """Bare "1 Bedroom $500" with no /month suffix must NOT match — the
    /month gate filters out deposit and amenity blurbs."""
    body = "We have a 1 Bedroom amenity for $500 deposit pet refund."
    assert parse_generic_plan_text(body, "https://example.com") == []


def test_parse_realpage_unit_blobs_aven() -> None:
    """Aven (pid=3148): SSR'd RealPage JSON blobs in homepage HTML.
    Same shape as the OLL XHR — should yield UNIT-LEVEL records."""
    body = (
        'window.unitData = ['
        '{"ApartmentNumber":"3204-11","RealPageUnitID":40,'
        '"MinPrice":1174.0,"MaxPrice":1730.0,"DisplayPrice":"$1,174",'
        '"SqFt":745,"FloorPlanType":"1Bedroom"},'
        '{"ApartmentNumber":"3205-12","RealPageUnitID":41,'
        '"MinPrice":1290.0,"MaxPrice":1830.0,"DisplayPrice":"$1,290",'
        '"SqFt":820,"FloorPlanType":"2Bedroom"}'
        '];'
    )
    units = parse_generic_plan_text(body, "https://example.com")
    assert len(units) == 2
    u1 = next(u for u in units if u["unit_number"] == "3204-11")
    assert u1["market_rent_low"] == 1174
    assert u1["market_rent_high"] == 1730
    assert u1["sqft"] == "745"
    assert u1["bedrooms"] == "1"
    assert u1["extraction_tier"] == "TIER_1_DOM_GENERIC_PLAN_TEXT_REALPAGE_BLOB"


def test_parse_realpage_blobs_take_priority_over_text_pass() -> None:
    """When RealPage blobs are present, they win — text-pass shouldn't
    also run (avoid double-counting)."""
    body = (
        '{"ApartmentNumber":"A1","MinPrice":1500.0,"SqFt":700,"FloorPlanType":"1Bedroom"} '
        '{"ApartmentNumber":"A2","MinPrice":1700.0,"SqFt":800,"FloorPlanType":"1Bedroom"} '
        # Also has text-pass-matchable plan lines that would have produced rows
        '1 Bedroom / 1 Bath $9999 should not appear'
    )
    units = parse_generic_plan_text(body, "https://example.com")
    # All units come from RealPage blobs — none from text pass.
    assert all(u["unit_number"] in ("A1", "A2") for u in units)
    assert all(u["extraction_tier"].endswith("REALPAGE_BLOB") for u in units)


def test_parse_realpage_blob_rejects_below_rent_floor() -> None:
    body = (
        '{"ApartmentNumber":"X1","MinPrice":300.0,"SqFt":500,"FloorPlanType":"1Bedroom"} '
        '{"ApartmentNumber":"X2","MinPrice":350.0,"SqFt":550,"FloorPlanType":"1Bedroom"}'
    )
    assert parse_generic_plan_text(body, "https://example.com") == []


# ─── 2026-05-23 grind phase 2 — additional patterns + entity unescape ────


def test_parse_word_number_bed_bath() -> None:
    """Country Club style: "One Bedroom/One Bath" — word numbers."""
    body = (
        "Plan A One Bedroom/One Bath 880 $1,250. "
        "Plan B Two Bedroom/Two Bath 1100 $1,800."
    )
    units = parse_generic_plan_text(body, "https://example.com")
    assert len(units) >= 2
    rents = {u["market_rent_low"] for u in units}
    assert 1250 in rents and 1800 in rents


def test_parse_bed_rent_range_windpoint() -> None:
    """Windpoint Apartments: explicit bed + $LOW - $HIGH range, no /month."""
    body = (
        "Unit Type Rent Range "
        "1 Bedroom $965.00 - $1,190.00 "
        "2 Bedroom $1,175.00 - $1,250.00"
    )
    units = parse_generic_plan_text(body, "https://example.com")
    assert len(units) >= 2
    by_beds = {(u["bedrooms"], u["market_rent_low"], u["market_rent_high"]) for u in units}
    assert ("1", 965, 1190) in by_beds
    assert ("2", 1175, 1250) in by_beds


def test_parse_bed_plan_letter_aster_village() -> None:
    """Aster Village: "1 Bedroom A $1,350" — bed + plan-letter + rent."""
    body = (
        "Floor Plan Unit Photos 1 Bedroom A $1,350 Available 8/7 "
        "1 Bedroom A1 $1,395 Available Now. 1 Bedroom B $1,165 Available 9/1"
    )
    units = parse_generic_plan_text(body, "https://example.com")
    assert len(units) >= 2
    rents = {u["market_rent_low"] for u in units}
    assert 1350 in rents
    assert 1395 in rents


def test_parse_jsonld_price_range_pedcor() -> None:
    """Pedcor: LocalBusiness JSON-LD with priceRange field. Single
    property-level row with data_gaps for sqft+beds."""
    body = (
        '{"@type":"LocalBusiness","name":"Kings Mill","priceRange":"$1,355 - $1,810"}'
    )
    units = parse_generic_plan_text(body, "https://example.com")
    assert len(units) == 1
    assert units[0]["market_rent_low"] == 1355
    assert units[0]["market_rent_high"] == 1810
    assert "sqft" in units[0]["data_gaps"]
    assert "beds" in units[0]["data_gaps"]
    assert units[0]["data_quality_flag"] == "PLAN_RANGE_ONLY"
    assert units[0]["extraction_tier"].endswith("JSONLD_PRICERANGE")


def test_parse_from_price_fountainview() -> None:
    """Fountainview Townhomes: "Fountainview Townhomes from $1400" —
    property-name-style from-$ marker, no bed token preceding."""
    body = (
        "About Fountainview Townhomes from $1400.00, Hagerstown, MD."
    )
    units = parse_generic_plan_text(body, "https://example.com")
    assert len(units) == 1
    assert units[0]["market_rent_low"] == 1400
    assert units[0]["extraction_tier"].endswith("FROM_PRICE")


def test_parse_from_price_rejects_amenity_blurb() -> None:
    """'1 bedroom from $500' must NOT trigger from-$ fallback —
    "bedroom" preceding the from-$ is the amenity-blurb signature."""
    body = "We offer a 1 bedroom from $500 amenity refund this month."
    assert parse_generic_plan_text(body, "https://example.com") == []


def test_unescape_js_entities_recovers_wix_blob() -> None:
    """Crawford Square (Wix): plan/rent inline as JSON-escaped HTML.
    The pre-pass unescape recovers the bed/rent so the parser sees it."""
    body = (
        '\\u003ep\\u003eTwo Bedroom 2 Beds 1 Bath 820-to862 Sq. Ft.'
        '\\u003c/p\\u003e\\u003cp\\u003e\\u003cstrong\\u003e$1,608to-$1,637 / mo'
        '\\u003c/strong\\u003e\\u003c/p\\u003e '
        '\\u003ep\\u003e2 Bedroom Townhome 2 Beds 1.5 Baths 1,006 Sq. Ft.'
        '\\u003c/p\\u003e\\u003cp\\u003e\\u003cstrong\\u003e$1,965 / mo'
        '\\u003c/strong\\u003e\\u003c/p\\u003e'
    )
    units = parse_generic_plan_text(body, "https://example.com")
    # At minimum the unescape allowed the parser to see the bed/bath
    # tokens — should produce ≥1 row from the bed-only or bed+bath
    # patterns.
    assert len(units) >= 1


# ─── 2026-05-23 grind phase 3 ── unit-street, primary-suites, table-row ─


def test_parse_unit_street_tor_view() -> None:
    """Tor View Village: '20H Kensington Circle-$2600.00' — unit_id
    + street + literal hyphen + rent. UNIT_STREET suffix accepted by
    single-row guard."""
    body = "Available Units 20H Kensington Circle-$2600.00 Some text"
    units = parse_generic_plan_text(body, "https://example.com")
    assert len(units) == 1
    assert units[0]["unit_number"] == "20H"
    assert units[0]["market_rent_low"] == 2600
    assert units[0]["extraction_tier"].endswith("UNIT_STREET")


def test_parse_primary_suites_conklin() -> None:
    """Conklin Townhomes: '2 primary suites 1,355 sq. ft. living space
    $2,050/month' — non-standard 'primary suites' as bedroom alias.
    Includes sqft in signature so multiple matches (Type I + Type II)
    both emit."""
    body = (
        "Type I Floorplan 2 primary suites 1,355 sq. ft. living space $2,050/month "
        "Type II Floorplan 2 primary suites 1,497 sq. ft. living space $2,050/month"
    )
    units = parse_generic_plan_text(body, "https://example.com")
    assert len(units) == 2
    sqfts = {u["sqft"] for u in units}
    assert "1355" in sqfts
    assert "1497" in sqfts
    assert all(u["bedrooms"] == "2" for u in units)


def test_parse_murphy_table_row() -> None:
    """Murphy Varnish availability table: '101 2 Bed 2 1,170 $2,450'
    — UNIT-LEVEL data with unit_num + bed + bath + sqft + rent."""
    body = (
        "UNIT TYPE BATH SQUARE FEET PRICE STATUS "
        "101 2 Bed 2 1,170 $2,450 Not Available "
        "102 Studio 1 708 $2,100 Available "
        "103 1 Bed 1 780 $1,900 Not Available"
    )
    units = parse_generic_plan_text(body, "https://example.com")
    assert len(units) == 3
    by_unit = {u["unit_number"]: u for u in units}
    assert by_unit["101"]["market_rent_low"] == 2450
    assert by_unit["101"]["sqft"] == "1170"
    assert by_unit["101"]["bedrooms"] == "2"
    assert by_unit["102"]["bedrooms"] == "0"  # Studio
    assert by_unit["102"]["sqft"] == "708"
    assert by_unit["103"]["market_rent_low"] == 1900


def test_parse_harvard_rent_range_bed_range() -> None:
    """Harvard Apartments: '$1,530 - $2,000 / 1 3 Bedroom(s)' — rent
    range + bed-count range. Emits one row per bed in the range."""
    body = "Property header $1,530 - $2,000 / 1 3 Bedroom(s) , 1 2 Bath(s) Photos"
    units = parse_generic_plan_text(body, "https://example.com")
    # Beds 1, 2, 3 — three rows
    assert len(units) == 3
    by_beds = {u["bedrooms"] for u in units}
    assert by_beds == {"1", "2", "3"}
    assert all(u["market_rent_low"] == 1530 for u in units)
    assert all(u["market_rent_high"] == 2000 for u in units)


def test_parse_labeled_monthly_rent_annaberg() -> None:
    """Annaberg: 'MONTHLY RENT: $950.00 ... TOTAL MONTHLY DUE: $1,060.00'.
    The labeled Monthly Rent / Total Monthly Due pattern is captured by
    the expanded _LABELED_PRICE_RE."""
    body = "MONTHLY RENT: $950.00 RESIDENT BENEFITS PACKAGE: $40.00 TOTAL MONTHLY DUE: $1,060.00"
    units = parse_generic_plan_text(body, "https://example.com")
    assert len(units) == 1
    # First match wins (MONTHLY RENT: $950).
    assert units[0]["market_rent_low"] == 950
    assert units[0]["extraction_tier"].endswith("LABELED_PRICE")
