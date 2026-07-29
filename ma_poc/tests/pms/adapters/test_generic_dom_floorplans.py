"""Generic SSR floor-plan DOM fallback (2026-05-19).

Catches the long-tail custom-CMS sites where plan-level data is rendered
in repeated containers one labelled nav-hop deep. Conservative against
false positives: ≥2 of {bed/bath, sqft, $} per card, container text <800
chars, ≥2 admitted cards total, sqft-or-rent required.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters._generic_dom_floorplans import (
    parse_generic_floorplan_cards,
    recover_generic_floorplans,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import detect_pms

# Real-shape cards (mckinley-style "The Birch $1,179-$1,349" + similar)
_GOOD_CARDS = [
    {"name": "The Birch", "text": "The Birch 1 Bed 1 Bath 850 sq ft $1,179 - $1,349 Available Now", "klass": "fp-card"},
    {"name": "Schooner Cove", "text": "Schooner Cove 2 Bed 1 Bath 950 sq ft $1,746 Available Jun 1, 2026", "klass": "fp-card"},
    {"name": "Studio Loft", "text": "Studio Loft Studio 1 Bath 500 sq ft $895 - $950 2 Units Available", "klass": "fp-card"},
]

# False-positive guards
_NAV_LIKE_CARDS = [
    # No $, no sqft — just bd/ba mention → reject
    {"name": "Browse", "text": "Browse our 1 bed and 2 bed apartments", "klass": "nav-card"},
    # $ but no bed/bath/sqft → reject
    {"name": "Deposit", "text": "Security deposit $500 per unit", "klass": "info-card"},
]


class _FakePage:
    def __init__(self, scan: object, url: str = "https://www.example.com/floorplans/") -> None:
        self._scan = scan
        self.url = url

    async def evaluate(self, _js: str, *_a: object) -> object:
        return self._scan


def _ctx() -> AdapterContext:
    return AdapterContext(
        base_url="https://www.example.com/",
        detected=detect_pms("https://www.example.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )


def test_parse_good_cards() -> None:
    units = parse_generic_floorplan_cards(_GOOD_CARDS, "u")
    assert len(units) == 3
    birch = units[0]
    assert birch["floor_plan_name"] == "The Birch"
    assert birch["bedrooms"] == "1"
    assert birch["bathrooms"] == "1"
    assert birch["sqft"] == "850"
    assert birch["market_rent_low"] == 1179
    assert birch["market_rent_high"] == 1349
    assert birch["availability_status"] == "AVAILABLE"
    assert birch["extraction_tier"] == "TIER_3_DOM_GENERIC"

    # studio
    s = units[2]
    assert s["bedrooms"] == "0"
    assert s["bed_label"]
    assert s["market_rent_low"] == 895
    assert s["market_rent_high"] == 950
    assert s["available_units"] == "2"


def test_parse_filters_low_signal_cards() -> None:
    """Cards with neither sqft nor rent should be dropped."""
    units = parse_generic_floorplan_cards(_NAV_LIKE_CARDS, "u")
    assert units == []


def test_parse_filters_below_money_threshold() -> None:
    """$ below 3 digits ($1, $50) is not rent — rejected by regex."""
    cards = [{"name": "Plan", "text": "Plan 1 Bed 1 Bath 700 sq ft only $1 application fee", "klass": "c"}]
    units = parse_generic_floorplan_cards(cards, "u")
    # No real $ rent → no rent_low/high; sqft is present so it IS admitted
    # (sqft alone keeps it; final guard requires sqft OR rent).
    assert len(units) == 1
    assert units[0]["market_rent_low"] is None
    assert units[0]["sqft"] == "700"


def test_parse_waitlist_status() -> None:
    cards = [{"name": "A", "text": "Plan A 1 Bed 1 Bath 600 sq ft $1,200 Join the Waitlist", "klass": "c"}]
    units = parse_generic_floorplan_cards(cards, "u")
    assert units[0]["availability_status"] == "UNAVAILABLE"


def test_parse_call_for_pricing_no_rent() -> None:
    cards = [{"name": "B", "text": "Plan B 1 Bed 1 Bath 700 sq ft Call for pricing", "klass": "c"}]
    units = parse_generic_floorplan_cards(cards, "u")
    # No $, has bd/bath/sqft. Call-for-pricing → UNAVAILABLE.
    assert units[0]["availability_status"] == "UNAVAILABLE"
    assert units[0]["market_rent_low"] is None


@pytest.mark.asyncio
async def test_recover_returns_units_when_scan_yields_cards() -> None:
    scan = {"cards": _GOOD_CARDS, "winningPath": "/floorplans/", "count": 3}
    page = _FakePage(scan)
    units, path = await recover_generic_floorplans(page, _ctx())  # type: ignore[arg-type]
    assert len(units) == 3
    assert path == "/floorplans/"


@pytest.mark.asyncio
async def test_recover_rejects_single_card() -> None:
    """Single card → reject (need ≥2 for confidence)."""
    scan = {"cards": _GOOD_CARDS[:1], "winningPath": "/floorplans/", "count": 1}
    page = _FakePage(scan)
    units, _ = await recover_generic_floorplans(page, _ctx())  # type: ignore[arg-type]
    assert units == []


@pytest.mark.asyncio
async def test_recover_empty_scan() -> None:
    page = _FakePage({"cards": [], "winningPath": "", "count": 0})
    units, _ = await recover_generic_floorplans(page, _ctx())  # type: ignore[arg-type]
    assert units == []


@pytest.mark.asyncio
async def test_recover_pageless_stub() -> None:
    class _Bare:
        url = "https://x.com/"

    units, _ = await recover_generic_floorplans(_Bare(), _ctx())  # type: ignore[arg-type]
    assert units == []


# ─────────────────────────────────────────────────────────────────────
# 2026-05-20 brand-CMS URL discovery: `/apartments/{state}/{city}/floor-plans`
# The TIER_3_DOM + TIER_MERGED ALL_fail probes found ~16-18% of failed
# properties use a multi-property brand template (Lincoln, McKinley,
# HG Living, MG Properties) where `/floorplans` returns 404 but the real
# floor-plans page lives at `/apartments/{state}/{city}/floor-plans`.
# The JS discovery scans landing-page hrefs for this pattern.
# ─────────────────────────────────────────────────────────────────────


def test_js_has_brand_cms_url_pattern() -> None:
    """The JS source contains the brand-CMS regex + the scan + probe loop.

    Structural test only — we can't execute the JS in unit tests; this
    pins the source-shape contract so accidental removal during refactor
    fails CI loudly.
    """
    from ma_poc.pms.adapters._generic_dom_floorplans import _GENERIC_DOM_JS

    js = _GENERIC_DOM_JS
    # Pattern matches `/apartments/{state-slug}/{city-slug}/(floor-plans|floorplans)`
    assert "/apartments/" in js
    assert "floor-plans|floorplans" in js
    # Scan source: anchors on the landing page
    assert "a[href]" in js
    # The scan dedupes via Set
    assert "new Set()" in js
    # The fall-through ordering: brand-CMS scan is LAST (after standard
    # subpaths) so common /floorplans paths still win first
    assert js.index("SUBPATHS") < js.index("brand-CMS")


def test_brand_cms_pattern_matches_real_examples() -> None:
    """Regression: the regex used in the JS source must match the real-world
    brand-CMS URLs observed during the 2026-05-20 TIER_3_DOM probe."""
    import re

    # Mirror the JS regex — supports both 3-segment (state/city/tail) and
    # 4-segment (state/city/property-slug/tail) brand-CMS URL shapes.
    py_re = re.compile(
        r"^(\/apartments\/[a-z-]+\/[a-z0-9-]+(?:\/[a-z0-9-]+)?\/(?:floor-plans|floorplans))(?:[?#].*)?$",
        re.IGNORECASE,
    )
    # Live-verified URLs from the TIER_3_DOM probe
    matches = [
        "/apartments/tx/san-antonio/floor-plans",       # Fairways 5
        "/apartments/ca/los-angeles/floor-plans",       # Museum Terrace
        "/apartments/ca/san-jose/floor-plans",          # Villas Willow Glen
        "/apartments/tx/lubbock/floor-plans",           # Renaissance at Northpark
        "/apartments/wa/burien/alcove-at-seahurst/floor-plans",  # HG Living
        "/apartments/michigan/ypsilanti/roundtree/floorplans",   # McKinley (no dash)
        "/apartments/tx/san-antonio/floor-plans?utm=x",  # with query string
        "/apartments/tx/san-antonio/floor-plans#section",  # with fragment
    ]
    for url in matches:
        assert py_re.match(url), f"expected brand-CMS regex to match {url!r}"


def test_brand_cms_pattern_rejects_non_brand_urls() -> None:
    """The regex must NOT match URLs that aren't the brand-CMS shape —
    avoid false-positive probes that waste fetches and could surface
    irrelevant scan results."""
    import re

    # Mirror the JS regex — supports both 3-segment (state/city/tail) and
    # 4-segment (state/city/property-slug/tail) brand-CMS URL shapes.
    py_re = re.compile(
        r"^(\/apartments\/[a-z-]+\/[a-z0-9-]+(?:\/[a-z0-9-]+)?\/(?:floor-plans|floorplans))(?:[?#].*)?$",
        re.IGNORECASE,
    )
    rejects = [
        "/floorplans",                              # plain root
        "/apartments",                              # too shallow
        "/apartments/tx",                           # missing city
        "/apartments/tx/san-antonio",               # missing /floor-plans
        "/apartments/tx/san-antonio/amenities",     # wrong tail
        "/apartments/tx/san-antonio/floor-plans/the-birch",  # nested deeper
        "https://example.com/apartments/tx/san-antonio/floor-plans",  # absolute (not relative)
    ]
    for url in rejects:
        assert not py_re.match(url), f"expected regex to REJECT {url!r}"


@pytest.mark.asyncio
async def test_recover_accepts_brand_cms_winning_path() -> None:
    """Full recovery flow: scan result reports a brand-CMS winning path
    with valid cards → admit. Same code path as standard /floorplans win;
    the brand path just looks different in the source_api_url provenance.
    """
    scan = {
        "cards": _GOOD_CARDS,
        "winningPath": "/apartments/tx/san-antonio/floor-plans",
        "count": 3,
    }
    page = _FakePage(scan, url="https://www.fairways5.com/")
    units, path = await recover_generic_floorplans(page, _ctx())  # type: ignore[arg-type]
    assert len(units) == 3
    assert path == "/apartments/tx/san-antonio/floor-plans"
    # Provenance is stamped on each unit dict via the recover wrapper
    assert all(
        "fairways5.com/apartments/tx/san-antonio/floor-plans" in (u.get("source_api_url") or "")
        for u in units
    )



# ─────────────────────────────────────────────────────────────────────
# 2026-05-24 static-HTML fallback — fires when page.evaluate isn't
# available (e.g. after today's fetcher GET-path auto-escalation has
# delivered a curl_cffi body without spinning up Playwright).
# Closes ~15-20 of the TIER_3_DOM P1 cohort where prod's static-HTML
# scan worked but canary never got DOM tooling.
# ─────────────────────────────────────────────────────────────────────

from unittest.mock import MagicMock, patch
from ma_poc.pms.adapters._generic_dom_floorplans import (
    _recover_generic_floorplans_static,
    _scan_static_html_for_cards,
)


def _ctx_with_body(body: str, final_url: str = "https://example.com/"):
    import dataclasses
    @dataclasses.dataclass
    class _FR:
        body: bytes | None
        final_url: str
    ctx = MagicMock()
    ctx.fetch_result = _FR(body=body.encode() if isinstance(body, str) else body, final_url=final_url)
    ctx.base_url = final_url
    return ctx


# ── static scanner: positive cases ────────────────────────────────────

def test_static_scanner_finds_repeated_plan_cards() -> None:
    """Three div.fp-card siblings with bd/ba/sqft/rent → 3 cards."""
    html = """<html><body>
        <div class="fp-card"><h3>The Birch</h3><p>1 Bed 1 Bath 850 sqft \,179</p></div>
        <div class="fp-card"><h3>Schooner Cove</h3><p>2 Bed 1 Bath 950 sqft \,746</p></div>
        <div class="fp-card"><h3>Studio Loft</h3><p>Studio 1 Bath 500 sqft \</p></div>
    </body></html>"""
    cards = _scan_static_html_for_cards(html)
    assert len(cards) == 3
    assert cards[0]["name"] == "The Birch"
    assert "1 Bed" in cards[0]["text"]


def test_static_scanner_filters_below_threshold_signals() -> None:
    """A container with only \\$ but no bd/ba/sqft → rejected (<2 signals)."""
    html = """<html><body>
        <div class="fp-card"> deposit only</div>
        <div class="fp-card">Another fee notice </div>
    </body></html>"""
    cards = _scan_static_html_for_cards(html)
    # No card has ≥2 signals → empty
    assert cards == []


def test_static_scanner_picks_best_class_when_multiple_match() -> None:
    """Two plan-like classes coexist; the one with more qualifying
    containers wins."""
    html = """<html><body>
        <div class="nav-item">1 Bed apartments coming soon </div>
        <div class="plan-card"><h3>A1</h3>1 Bed 1 Bath 700 sqft ,200</div>
        <div class="plan-card"><h3>B1</h3>2 Bed 2 Bath 950 sqft ,800</div>
        <div class="plan-card"><h3>C1</h3>3 Bed 2 Bath 1200 sqft ,400</div>
    </body></html>"""
    cards = _scan_static_html_for_cards(html)
    # plan-card class wins (3 cards) over nav-item (1 below-threshold)
    assert len(cards) == 3
    names = {c["name"] for c in cards}
    assert names == {"A1", "B1", "C1"}


def test_static_scanner_drops_overlong_containers() -> None:
    """A container whose text exceeds 800 chars is treated as boilerplate
    (footer / blog / description block) — must NOT pollute the card list."""
    long_text = "description " * 100  # >800 chars
    html = f"""<html><body>
        <div class="fp-card"><h3>A1</h3>1 Bed 1 Bath 700 sqft ,200 {long_text}</div>
        <div class="fp-card"><h3>B1</h3>2 Bed 2 Bath 950 sqft ,800</div>
        <div class="fp-card"><h3>C1</h3>3 Bed 2 Bath 1200 sqft ,400</div>
    </body></html>"""
    cards = _scan_static_html_for_cards(html)
    # A1 is overlong (filtered), B1 + C1 remain
    assert len(cards) == 2
    assert {c["name"] for c in cards} == {"B1", "C1"}


def test_static_scanner_empty_html_returns_empty() -> None:
    assert _scan_static_html_for_cards("") == []
    assert _scan_static_html_for_cards("<html></html>") == []


def test_static_scanner_caps_class_size_at_50() -> None:
    """A class with 51+ matching elements is treated as a navigation
    template (every nav-link wrapped in .fp-card) — reject."""
    cells = "".join(
        f"""<div class="fp-card"><h3>P{i}</h3>1 Bed 1 Bath 700 sqft ,200</div>"""
        for i in range(55)
    )
    html = f"<html><body>{cells}</body></html>"
    cards = _scan_static_html_for_cards(html)
    assert cards == []  # 55 > 50 cap


def test_static_scanner_requires_plan_class_word() -> None:
    """A container without one of the plan/floorplan/fp-/unit/listing/
    card/model/tile/item words in its class attribute is ignored."""
    html = """<html><body>
        <div class="section"><h3>A1</h3>1 Bed 1 Bath 700 sqft ,200</div>
        <div class="section"><h3>B1</h3>2 Bed 2 Bath 950 sqft ,800</div>
    </body></html>"""
    cards = _scan_static_html_for_cards(html)
    assert cards == []


# ── recover_generic_floorplans_static integration ────────────────────

@pytest.mark.asyncio
async def test_static_recover_no_body_returns_empty() -> None:
    ctx = _ctx_with_body("")
    units, _ = await _recover_generic_floorplans_static(ctx)
    assert units == []


@pytest.mark.asyncio
async def test_static_recover_homepage_cards_succeed() -> None:
    """Homepage body itself has plan cards → no subpage probe needed."""
    html = """<html><body>
        <div class="fp-card"><h3>The Birch</h3>1 Bed 1 Bath 850 sqft ,179 - ,349 Available Now</div>
        <div class="fp-card"><h3>Schooner</h3>2 Bed 1 Bath 950 sqft ,746 Available Jun 1</div>
    </body></html>"""
    ctx = _ctx_with_body(html)
    units, _ = await _recover_generic_floorplans_static(ctx)
    assert len(units) == 2
    assert any(u["floor_plan_name"] == "The Birch" for u in units)


@pytest.mark.asyncio
async def test_static_recover_probes_subpaths_when_homepage_empty() -> None:
    """Homepage has no plan grid → probe /floorplans, /floor-plans etc.
    First subpath returning ≥2 cards wins."""
    homepage = "<html><body><nav>no plans here</nav></body></html>"
    fp_html = """<html><body>
        <div class="fp-card"><h3>A1</h3>1 Bed 1 Bath 700 sqft ,200</div>
        <div class="fp-card"><h3>B1</h3>2 Bed 2 Bath 950 sqft ,800</div>
    </body></html>"""

    def fake_probe(url, **kw):
        r = MagicMock()
        if url.endswith("/floor-plans") or url.endswith("/floorplans"):
            r.status_code = 200; r.text = fp_html
        else:
            r.status_code = 404; r.text = ""
        return r

    ctx = _ctx_with_body(homepage)
    with patch("ma_poc.pms.adapters._generic_dom_floorplans.probe_get" if False else "ma_poc.pms.adapters._probe.probe_get", side_effect=fake_probe):
        units, path = await _recover_generic_floorplans_static(ctx)
    assert len(units) == 2
    assert path in ("/floorplans", "/floor-plans", "/floorplans/", "/floor-plans/")


@pytest.mark.asyncio
async def test_static_recover_returns_empty_when_no_subpath_yields_cards() -> None:
    homepage = "<html><body>nothing</body></html>"
    def fake_probe(url, **kw):
        r = MagicMock(); r.status_code = 404; r.text = ""
        return r
    ctx = _ctx_with_body(homepage)
    with patch("ma_poc.pms.adapters._probe.probe_get", side_effect=fake_probe):
        units, _ = await _recover_generic_floorplans_static(ctx)
    assert units == []


# ── full recover_generic_floorplans falls back to static when no page.evaluate ──

@pytest.mark.asyncio
async def test_recover_full_falls_back_to_static_when_no_page_evaluate() -> None:
    """When the page has no evaluate (curl-mode fetcher), the JS scan
    is skipped and the static-HTML fallback runs."""
    homepage = """<html><body>
        <div class="fp-card"><h3>X</h3>1 Bed 1 Bath 700 sqft ,200</div>
        <div class="fp-card"><h3>Y</h3>2 Bed 2 Bath 950 sqft ,800</div>
    </body></html>"""

    class _NoEvalPage:
        url = "https://example.com/"
        # No evaluate attr at all

    ctx = _ctx_with_body(homepage)
    units, _ = await recover_generic_floorplans(_NoEvalPage(), ctx)
    assert len(units) == 2


# ─────────────────────────────────────────────────────────────────────
# 2026-07-29 SCOPE GUARD — reject cards describing a DIFFERENT property.
#
# Incident: canary run-2026-07-27-full-0d54ca7, canonical_id 71534.
# Riverwalk on the Falls shipped 33 units, SUCCESS_PLAN_LEVEL, every
# floor_plan_name a sibling Wimmer community. Two bugs combined:
#   (a) the sub-path walk fetched `location.origin + "/apartments"`, which
#       on a management-company site is the company-wide directory, and
#   (b) nothing checked that admitted cards belong to this property.
#
# Live-reproduced against wimmercommunities.com: /apartments returns cards
# of class `property-card-info` whose anchors point at
# /apartments/wi/{city}/{other-property}.
#
# The guard's shape is measured, not assumed. A naive "any link leaving the
# property's path subtree is off-scope" rule rejects 118/757 card sets in
# that run's saved HTML, because a deep landing URL does not imply a
# multi-property site. The tests below pin both the rejections AND the four
# legitimate shapes that must survive.
# ─────────────────────────────────────────────────────────────────────

from ma_poc.pms.adapters._generic_dom_floorplans import (  # noqa: E402
    card_scope_verdict,
    classify_href_scope,
    filter_cards_by_scope,
)

# Riverwalk on the Falls — the property under scrape.
_RIVERWALK = (
    "https://www.wimmercommunities.com/apartments/menomonee-falls/"
    "riverwalk-on-the-falls/"
)
# The company-wide directory the origin-rooted probe landed on.
_WIMMER_DIRECTORY = "https://www.wimmercommunities.com/apartments"

# Cards as the live directory grid actually renders them (class
# `property-card-info`; anchors captured from the live page 2026-07-29).
_DIRECTORY_CARDS = [
    {
        "name": "Poplar Creek Town Center",
        "text": "Poplar Creek Town Center 2 Bed 2 Bath 1,239 sq ft Starting at $1,390",
        "klass": "col-sm-8 col-md-12 col-lg-8 property-card-info",
        "hrefs": [
            "https://www.wimmercommunities.com/apartments/wi/brookfield/poplar-creek-town-center",
            "tel:+1 262-264-8420",
        ],
    },
    {
        "name": "College Square Apartments",
        "text": "College Square Apartments 2 Bed 1 Bath 1,766 sq ft Starting at $1,575",
        "klass": "col-sm-8 col-md-12 col-lg-8 property-card-info",
        "hrefs": [
            "https://www.wimmercommunities.com/apartments/wi/greendale/college-square",
            "tel:+1 414-662-9731",
        ],
    },
    {
        "name": "South 111th Street Studios",
        "text": "South 111th Street Studios Studio 1 Bath 1,276 sq ft Starting at $1,499",
        "klass": "col-sm-8 col-md-12 col-lg-8 property-card-info",
        "hrefs": [
            "https://www.wimmercommunities.com/apartments/wi/west-allis/south-111th-street-studios"
        ],
    },
]


def test_scope_guard_rejects_wimmer_community_directory() -> None:
    """The 71534 regression: every card is a sibling community → emit none.

    Wrong data attributed to the right property is worse than no data —
    the run counted these 33 rows as SUCCESS_PLAN_LEVEL.
    """
    units = parse_generic_floorplan_cards(
        _DIRECTORY_CARDS,
        _WIMMER_DIRECTORY,
        property_url=_RIVERWALK,
        doc_url=_WIMMER_DIRECTORY,
    )
    assert units == []


def test_scope_guard_off_by_default_preserves_legacy_callers() -> None:
    """Without a property_url the guard is inert — same cards are admitted."""
    units = parse_generic_floorplan_cards(_DIRECTORY_CARDS, _WIMMER_DIRECTORY)
    assert len(units) == 3


# ── classify_href_scope: the four verdict classes ────────────────────

def test_scope_descendant_link_is_in() -> None:
    """A plan-detail page under the property's own path."""
    assert (
        classify_href_scope(_RIVERWALK, _RIVERWALK + "floorplans/the-birch")
        == "IN"
    )


def test_scope_sibling_property_link_is_out() -> None:
    """Another community on the same management-company host."""
    assert (
        classify_href_scope(
            _RIVERWALK,
            "/apartments/wi/brookfield/poplar-creek-town-center",
            _WIMMER_DIRECTORY,
        )
        == "OUT"
    )


def test_scope_ancestor_link_is_neutral() -> None:
    """A breadcrumb / section index is not evidence of another property."""
    assert classify_href_scope(_RIVERWALK, "/apartments", _WIMMER_DIRECTORY) == "NEUTRAL"


def test_scope_cross_host_link_is_neutral() -> None:
    """Application portals and map links legitimately leave the host."""
    assert (
        classify_href_scope(_RIVERWALK, "https://portal.rentcafe.com/apply")
        == "NEUTRAL"
    )


def test_scope_non_navigational_hrefs_are_neutral() -> None:
    for href in ("tel:+14146629731", "mailto:a@b.com", "#top", "javascript:void(0)", ""):
        assert classify_href_scope(_RIVERWALK, href) == "NEUTRAL"


def test_scope_property_at_site_root_admits_whole_host() -> None:
    """Single-property site: origin-rooted probing is correct, everything
    same-host is in scope."""
    assert (
        classify_href_scope("https://www.thebirchapts.com/", "/floorplans/a1")
        == "IN"
    )


# ── the four legitimate shapes that must NOT be rejected ─────────────
# Each is a real property from run-2026-07-27-full-0d54ca7 whose plans live
# outside its landing-URL subtree. A naive subtree rule rejects all four.

def test_scope_plan_marker_link_survives_off_subtree() -> None:
    """bridgelaneapartments.com: landing /Home.aspx, plans under /phoenix/…"""
    assert (
        classify_href_scope(
            "http://www.bridgelaneapartments.com/Home.aspx",
            "/phoenix/bridge-lane-apartment-homes/floorplans/studio-1154964/",
        )
        == "NEUTRAL"
    )


def test_scope_listing_detail_link_survives_off_subtree() -> None:
    """dlhproperties.net: AppFolio unit pages at /listings/detail/{uuid}."""
    assert (
        classify_href_scope(
            "https://www.dlhproperties.net/property/cottages-on-tazewell/",
            "/listings/detail/503f0308-ab54-4abe-90f7-3e2e77e2e290",
        )
        == "NEUTRAL"
    )


def test_scope_asset_download_link_survives() -> None:
    """gardencommunities.com gives every plan card its own brochure guid —
    a distinct URL per card that is not another property."""
    assert (
        classify_href_scope(
            "https://www.gardencommunities.com/Properties/NJ/Bergen/The-Dayton.aspx",
            "/CMSPages/GetFile.aspx?guid=e701a10c-e54c-49c5-9bce-801b728f7232",
        )
        == "NEUTRAL"
    )


def test_scope_cms_taxonomy_link_survives() -> None:
    """WordPress property themes link plan cards to archive pages."""
    for href in ("/status/for-rent/", "/label/spearhead/", "/contact"):
        assert (
            classify_href_scope("https://www.jfmanagementgroup.com/summerset/", href)
            == "NEUTRAL"
        ), href


def test_scope_link_naming_this_property_survives() -> None:
    """avaloncommunities.com: the landing URL of record is a SUB-page
    (…/avalon-alderwood/map-directions), so the property's real root is one
    level up and its own unit links look off-subtree."""
    assert (
        classify_href_scope(
            "http://www.avaloncommunities.com/washington/lynnwood-apartments/"
            "avalon-alderwood/map-directions",
            "/washington/lynnwood-apartments/avalon-alderwood/apartment/"
            "WA025-WA025-00A-A414/",
        )
        == "NEUTRAL"
    )


# ── query-string-identified properties ───────────────────────────────
# pmsi.biz / rookwoodproperties.com / apartmentsniagara.com identify the
# property purely by query param, so path comparison alone cannot help.

def test_scope_same_path_different_id_param_is_out() -> None:
    assert (
        classify_href_scope("https://pmsi.biz/details/?pid=30", "/details/?pid=31")
        == "OUT"
    )


def test_scope_same_path_same_id_param_is_in() -> None:
    assert (
        classify_href_scope("https://pmsi.biz/details/?pid=30", "/details/?pid=30")
        == "IN"
    )


# ── card- and set-level behaviour ────────────────────────────────────

def test_card_with_any_in_scope_anchor_is_kept() -> None:
    """A card that links to this property anywhere survives even if it also
    carries an unrelated link."""
    card = {
        "name": "#146",
        "text": "#146 2 Bed 2 Bath 1101 sq ft $1,800",
        "hrefs": [
            "/apartments/cortland-at-raven/available-apartments/1146/pricing/",
            "/apartments/cortland-biltmore/",
        ],
    }
    assert card_scope_verdict(card, "https://cortland.com/apartments/cortland-at-raven/") == "IN"


def test_cards_without_anchors_are_never_rejected() -> None:
    """The guard needs positive evidence; a bare card set is untouched."""
    kept = filter_cards_by_scope(_GOOD_CARDS, _RIVERWALK, _RIVERWALK)
    assert len(kept) == len(_GOOD_CARDS)


def test_scope_guard_rejects_set_when_fewer_than_two_survive() -> None:
    """Re-applies the module's ≥2-admitted-cards invariant after filtering.
    One surviving card is the property's own 'starting at' summary row
    inside a sibling directory — plan-level noise, not a plan grid."""
    cards = [
        {"name": "Riverwalk on the Falls", "text": "Riverwalk 1 Bed 1 Bath 800 sq ft $1,200",
         "hrefs": [_RIVERWALK]},
        {"name": "Falcon Glen", "text": "Falcon Glen 2 Bed 2 Bath 1,100 sq ft $1,500",
         "hrefs": ["/apartments/wi/menomonee-falls/falcon-glen"]},
        {"name": "The Orchard", "text": "The Orchard 1 Bed 1 Bath 900 sq ft $1,300",
         "hrefs": ["/apartments/wi/waukesha/the-orchard"]},
    ]
    assert filter_cards_by_scope(cards, _RIVERWALK, _WIMMER_DIRECTORY) == []


def test_scope_guard_keeps_majority_when_minority_off_scope() -> None:
    """A single stray sibling link must not discard a real plan grid."""
    cards = [
        {"name": "A1", "text": "A1 1 Bed 1 Bath 700 sq ft $1,200",
         "hrefs": [_RIVERWALK + "floorplans/a1"]},
        {"name": "B1", "text": "B1 2 Bed 2 Bath 950 sq ft $1,800",
         "hrefs": [_RIVERWALK + "floorplans/b1"]},
        {"name": "Falcon Glen", "text": "Falcon Glen 2 Bed 2 Bath 1,100 sq ft $1,500",
         "hrefs": ["/apartments/wi/menomonee-falls/falcon-glen"]},
    ]
    kept = filter_cards_by_scope(cards, _RIVERWALK, _RIVERWALK)
    assert [c["name"] for c in kept] == ["A1", "B1"]


@pytest.mark.asyncio
async def test_recover_rejects_directory_grid_end_to_end() -> None:
    """Full Track-A flow: the JS reports the directory grid, recovery emits
    nothing rather than 33 wrongly-attributed rows."""
    scan = {
        "cards": _DIRECTORY_CARDS,
        "winningPath": "/apartments",
        "docUrl": _WIMMER_DIRECTORY,
        "count": len(_DIRECTORY_CARDS),
    }
    page = _FakePage(scan, url=_RIVERWALK)
    units, _ = await recover_generic_floorplans(page, _ctx())  # type: ignore[arg-type]
    assert units == []


# ── static scanner: anchors + property-subtree probing ───────────────

def test_static_scanner_captures_card_anchors() -> None:
    """hrefs must reach the guard from the BeautifulSoup track too."""
    html = """<html><body>
        <div class="property-card-info"><h3>College Square Apartments</h3>
          <a href="/apartments/wi/greendale/college-square">View</a>
          <p>2 Bed 1 Bath 1,766 sqft Starting at $1,575</p></div>
        <div class="property-card-info"><h3>Falcon Glen</h3>
          <a href="/apartments/wi/menomonee-falls/falcon-glen">View</a>
          <p>3 Bed 2 Bath 2,046 sqft Starting at $1,995</p></div>
    </body></html>"""
    cards = _scan_static_html_for_cards(html)
    assert len(cards) == 2
    assert cards[0]["hrefs"] == ["/apartments/wi/greendale/college-square"]


@pytest.mark.asyncio
async def test_static_recover_rejects_sibling_directory() -> None:
    """shaoolmgt.com shape: the landing body itself carries the directory."""
    html = """<html><body>
        <div class="property-card"><h3>Goldfinch Meadows</h3>
          <a href="/property/goldfinch-meadows/">View</a>
          <p>2 Bed 1 Bath 950 sqft $1,250</p></div>
        <div class="property-card"><h3>Pangborn Park Townhomes</h3>
          <a href="/property/pangborn-park-townhomes/">View</a>
          <p>3 Bed 2 Bath 1,200 sqft $1,600</p></div>
    </body></html>"""
    ctx = _ctx_with_body(
        html, final_url="https://www.shaoolmgt.com/property/fountainview-townhomes/"
    )
    units, _ = await _recover_generic_floorplans_static(ctx)
    assert units == []


@pytest.mark.asyncio
async def test_static_recover_probes_property_subtree_before_origin() -> None:
    """Root cause of 71534: `origin + "/apartments"` is the company-wide
    directory when the property lives at a deep path. The property's own
    subtree must be probed first."""
    homepage = "<html><body><nav>no plans here</nav></body></html>"
    own_plans = """<html><body>
        <div class="fp-card"><h3>The Birch</h3>1 Bed 1 Bath 850 sqft $1,179</div>
        <div class="fp-card"><h3>Schooner</h3>2 Bed 1 Bath 950 sqft $1,746</div>
    </body></html>"""
    requested: list[str] = []

    def fake_probe(url, **kw):
        requested.append(url)
        r = MagicMock()
        if url.startswith(
            "https://www.wimmercommunities.com/apartments/menomonee-falls/"
            "riverwalk-on-the-falls/floorplans"
        ):
            r.status_code, r.text = 200, own_plans
        else:
            r.status_code, r.text = 404, ""
        return r

    ctx = _ctx_with_body(homepage, final_url=_RIVERWALK)
    with patch("ma_poc.pms.adapters._probe.probe_get", side_effect=fake_probe):
        units, _ = await _recover_generic_floorplans_static(ctx)

    assert len(units) == 2
    # The property's own subtree is probed before any origin-rooted path.
    first_origin_rooted = next(
        (i for i, u in enumerate(requested)
         if u == "https://www.wimmercommunities.com/apartments"),
        len(requested),
    )
    first_subtree = next(
        i for i, u in enumerate(requested)
        if u.startswith(_RIVERWALK.rstrip("/"))
    )
    assert first_subtree < first_origin_rooted


def test_js_probes_property_subtree_and_captures_hrefs() -> None:
    """Source-shape contract for the JS extractor — it cannot be executed in
    unit tests, so pin the pieces the Python guard depends on."""
    from ma_poc.pms.adapters._generic_dom_floorplans import _GENERIC_DOM_JS

    js = _GENERIC_DOM_JS
    # Card anchors are captured and absolutised for the scope guard.
    assert "hrefs" in js
    assert "new URL(raw, base)" in js
    # The document URL is returned so relative hrefs resolve correctly.
    assert "docUrl" in js
    # Property-subtree probing exists and precedes the origin-rooted walk.
    # Both walk SUBPATHS via probePath(); the subtree one prefixes propBase.
    assert "propBase" in js
    subtree_call = js.index("probePath(propBase + p)")
    origin_call = js.index("probePath(p)")
    assert subtree_call < origin_call, (
        "origin-rooted sub-path probing must come AFTER the property's own "
        "subtree — reversing them reintroduces the 71534 defect"
    )
