"""Tests for HTML-based JSON-LD + embedded-JSON extractors (step 4)."""

from __future__ import annotations

import json

import pytest

from ma_poc.pms.adapters._html_extract import (
    extract_embedded_blobs_from_html,
    extract_jsonld_from_html,
    extract_units_from_dom,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.generic import GenericAdapter
from ma_poc.pms.detector import detect_pms

# ── JSON-LD ──────────────────────────────────────────────────────────────────


def test_jsonld_apartment_with_offers() -> None:
    html = """<html><head>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Apartment",
      "name": "The Parker 1x1",
      "numberOfRooms": 1,
      "floorSize": {"@type": "QuantitativeValue", "value": 650},
      "offers": {"@type": "Offer", "lowPrice": 1800, "highPrice": 2000}
    }
    </script></head><body></body></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    assert len(units) == 1
    u = units[0]
    assert u["floor_plan_name"] == "The Parker 1x1"
    assert u["rent_range"] == "$1,800 - $2,000"
    assert u["sqft"] == "650"
    assert u["extraction_tier"] == "TIER_2_JSONLD"


def test_jsonld_marketing_apartment_recovers_labelled_concrete_unit_number() -> None:
    """A marketing detail page's ``#7953C`` is a real unit, not its FTK plan code."""
    html = """<html><script type="application/ld+json">
    {"@type":"Apartment","name":"Sedona #7953C","unitCode":"FTK",
     "numberOfBedrooms":1,"offers":{"@type":"Offer","price":779}}
    </script></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/Marketing/FloorPlans/Units/id")
    assert len(units) == 1
    assert units[0]["unit_number"] == "7953C"
    assert units[0]["market_rent_low"] == 779


def test_dom_data_unit_identity_pair_recovers_concrete_entrata_marketing_unit() -> None:
    """A labelled Entrata marketing row is a unit even without a ``.unit-card`` class."""
    html = """
    <div class="unit-body" data-unit-id="4112268" data-unit-number="0612"
         data-rent="1938.00" data-bedrooms="2" data-bathrooms="1" data-area="879 SquareFeet">
      <div class="unit-body-content">
        <h3>Unit 0612</h3><h4>Available Now</h4>
        <p>2 bedrooms, 1 bathroom</p><p>From $1,938.00 / month</p>
      </div>
    </div>
    """
    units, mode = extract_units_from_dom(html, "https://www.thevillagedallas.com/properties/the-village-lakes/")
    assert mode == "default"
    assert len(units) == 1
    assert units[0]["unit_number"] == "0612"
    assert units[0]["unit_id"] == "4112268"
    assert units[0]["market_rent_low"] == 1938


def test_securecafe_applicant_react_rows_keep_real_unit_identity_not_plan_name() -> None:
    """The Applicant card has a plan range plus distinct C310/C404 unit prices."""
    html = """
    <li><div><h2>2 Bedroom</h2><p>2 Bed / 2 Bath / 1052 Sqft</p><p>$1,893.00 - $1,918.00</p>
      <div><div>#C310</div><div>From $1,918.00</div><button aria-label="View unit C310 details">View</button></div>
      <div><div>#C404</div><div>From $1,893.00</div><button aria-label="View unit C404 details">View</button></div>
    </div></li>
    """
    units, mode = extract_units_from_dom(
        html,
        "https://bromleyhouse.securecafeapplicant.com/onlineleasing/content3/access/bromley-house/floorplans/2039041",
    )
    assert mode == "default"
    assert [(unit["unit_number"], unit["market_rent_low"]) for unit in units] == [
        ("C310", 1918),
        ("C404", 1893),
    ]
    assert {unit["floor_plan_name"] for unit in units} == {"2 Bedroom"}


def test_appfolio_public_listing_iframe_recovers_unit_from_address_middle_segment() -> None:
    """AppFolio cards place 0188BE in the address rather than a unit field."""
    html = """
    <div class="listing-item result js-listing-item" id="listing_2397">
      <dl>
        <div><dt>RENT</dt><dd>$800</dd></div>
        <div><dt>Square Feet</dt><dd>575</dd></div>
        <div><dt>Bed / Bath</dt><dd>1 bd / 1 ba</dd></div>
      </dl>
      <span class="js-listing-address">1919 Burton Dr, 0188BE, Austin, TX 78741</span>
      <p>AVAILABLE NOW</p>
    </div>
    """
    units, mode = extract_units_from_dom(
        html,
        "https://gordonandbilyeupm.appfolio.com/listings?filters%5Bproperty_list%5D=emerson",
    )
    assert mode == "default"
    assert len(units) == 1
    assert units[0]["unit_number"] == "0188BE"
    assert units[0]["market_rent_low"] == 800
    assert units[0]["sqft"] == "575"
    assert units[0]["source_ids"] == {"appfolio_listing_id": "2397"}


def test_jsonld_skips_property_shell_with_no_offers() -> None:
    """ApartmentComplex with only name+address is not a unit — must be skipped."""
    html = """<html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"ApartmentComplex",
     "name":"Some Property","telephone":"555-1234"}
    </script></head></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    assert units == []


def test_jsonld_offers_as_array_picks_min_max() -> None:
    html = """<html><script type="application/ld+json">
    {"@type":"Apartment","name":"A1","numberOfRooms":1,
     "offers":[{"@type":"Offer","price":1500},
               {"@type":"Offer","price":1600},
               {"@type":"Offer","price":1700}]}
    </script></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    assert len(units) == 1
    assert units[0]["rent_range"] == "$1,500 - $1,700"


def test_jsonld_malformed_block_silently_skipped() -> None:
    html = """<html>
    <script type="application/ld+json">not valid json {{</script>
    <script type="application/ld+json">
    {"@type":"Apartment","name":"B1","numberOfRooms":2,
     "offers":{"price":2100}}
    </script></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    # Bad block skipped, good block emitted.
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "B1"


def test_jsonld_empty_html_returns_empty() -> None:
    assert extract_jsonld_from_html("", "https://example.com/") == []
    assert extract_jsonld_from_html("<html></html>", "https://example.com/") == []


# ── Container-with-multi-Offer patterns (added 2026-05) ─────────────────────
# 368 of 689 still-failing properties in the May 2026 canary had JSON-LD blocks
# present but used a Place / LocalBusiness / Product / RealEstateListing /
# ApartmentComplex container with an offers[] array, which the original parser
# only matched as Apartment-with-offers and missed.


def test_jsonld_place_with_multi_offer_array() -> None:
    """schema.org/Place with offers[] of length >= 2 emits one unit per Offer."""
    html = """<html><script type="application/ld+json">
    {"@type":"Place","name":"The Park",
     "offers":[
       {"@type":"Offer","name":"1BR","price":1850,
        "itemOffered":{"@type":"Apartment","numberOfRooms":1,"floorSize":{"value":700}}},
       {"@type":"Offer","name":"2BR","price":2450,
        "itemOffered":{"@type":"Apartment","numberOfRooms":2,"floorSize":{"value":1000}}},
       {"@type":"Offer","name":"3BR","price":3200,
        "itemOffered":{"@type":"Apartment","numberOfRooms":3,"floorSize":{"value":1400}}}
    ]}</script></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    assert len(units) == 3
    assert {u["floor_plan_name"] for u in units} == {"1BR", "2BR", "3BR"}
    assert {u["rent_range"] for u in units} == {"$1,850", "$2,450", "$3,200"}


def test_jsonld_localbusiness_offers_with_low_high_price() -> None:
    html = """<html><script type="application/ld+json">
    {"@type":"LocalBusiness","name":"Apt Co",
     "offers":[
       {"@type":"Offer","lowPrice":1500,"highPrice":1700,"name":"Studio"},
       {"@type":"Offer","lowPrice":1800,"highPrice":2100,"name":"1BR"}
    ]}</script></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    assert len(units) == 2
    rents = {u["rent_range"] for u in units}
    assert "$1,500 - $1,700" in rents and "$1,800 - $2,100" in rents


def test_jsonld_product_offers_with_itemoffered() -> None:
    html = """<html><script type="application/ld+json">
    {"@type":"Product","name":"Property",
     "offers":[
       {"@type":"Offer","price":2200,"itemOffered":{"name":"Plan A","numberOfRooms":1}},
       {"@type":"Offer","price":2800,"itemOffered":{"name":"Plan B","numberOfRooms":2}}
    ]}</script></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    assert len(units) == 2


def test_jsonld_realestatelisting_makesoffer() -> None:
    """``makesOffer`` is the schema.org alternate key for RealEstateListing."""
    html = """<html><script type="application/ld+json">
    {"@type":"RealEstateListing","name":"Listing",
     "makesOffer":[
       {"@type":"Offer","price":1800,"name":"Unit 101"},
       {"@type":"Offer","price":1900,"name":"Unit 102"}
    ]}</script></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    assert len(units) == 2


def test_jsonld_apartment_complex_with_multi_offer_array() -> None:
    """ApartmentComplex with offers[] of length >= 2 — historically suppressed
    pass 2 by emitting a single fake aggregate unit. New code defers to pass 2.
    """
    html = """<html><script type="application/ld+json">
    {"@type":"ApartmentComplex","name":"Test",
     "offers":[
       {"@type":"Offer","price":1500,"name":"Studio"},
       {"@type":"Offer","price":1800,"name":"1BR"},
       {"@type":"Offer","price":2200,"name":"2BR"}
    ]}</script></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    assert len(units) == 3


def test_jsonld_container_offers_in_graph_wrapper() -> None:
    """``@graph`` wrapper used by sites built with Yoast / generic SEO tools."""
    html = """<html><script type="application/ld+json">
    {"@graph":[
      {"@type":"Place","name":"X",
       "offers":[
         {"@type":"Offer","price":1500,"name":"A"},
         {"@type":"Offer","price":1900,"name":"B"}
      ]}
    ]}</script></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    assert len(units) == 2


def test_jsonld_container_pass2_skipped_when_pass1_finds_units() -> None:
    """When BOTH Apartment AND Place-with-offers exist, the Apartment wins
    (pass 1) and pass 2 is suppressed — avoids double-counting the same units.
    """
    html = """<html><script type="application/ld+json">
    {"@type":"Place","name":"X",
     "offers":[
       {"@type":"Offer","price":1500,"name":"A"},
       {"@type":"Offer","price":1900,"name":"B"}
     ],
     "containsPlace":{"@type":"Apartment","name":"Apt 1",
                      "numberOfRooms":1,"offers":{"price":1700}}}
    </script></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "Apt 1"


def test_jsonld_container_single_offer_skipped() -> None:
    """One-Offer container is property-level pricing, not a unit list."""
    html = """<html><script type="application/ld+json">
    {"@type":"Place","name":"X",
     "offers":[{"@type":"Offer","price":1500,"name":"P"}]}
    </script></html>"""
    assert extract_jsonld_from_html(html, "https://example.com/") == []


def test_jsonld_container_identical_offers_skipped() -> None:
    """Distinguishing-fields guard: identical offers don't qualify as units."""
    html = """<html><script type="application/ld+json">
    {"@type":"Place","name":"X",
     "offers":[
       {"@type":"Offer","price":1500,"name":"P"},
       {"@type":"Offer","price":1500,"name":"P"},
       {"@type":"Offer","price":1500,"name":"P"}
    ]}</script></html>"""
    assert extract_jsonld_from_html(html, "https://example.com/") == []


# ── Embedded JSON / SSR globals ──────────────────────────────────────────────


def test_embedded_next_data_block() -> None:
    # Pad payload over the 200-char length threshold — production pages are
    # always many KB; the threshold filters noise-scale inline configs.
    payload = {
        "props": {
            "pageProps": {
                "floorPlans": [
                    {
                        "id": i,
                        "name": f"Plan{i}",
                        "beds": 1 + (i % 3),
                        "minRent": 1500 + 50 * i,
                        "maxRent": 1600 + 50 * i,
                        "sqft": 650 + 50 * i,
                        "building": "Main",
                        "floor": i // 4,
                    }
                    for i in range(6)
                ]
            }
        }
    }
    html = f"""<html><body>
    <script id="__NEXT_DATA__" type="application/json">
    {json.dumps(payload)}
    </script></body></html>"""
    blobs = extract_embedded_blobs_from_html(html)
    assert len(blobs) >= 1
    assert any("__NEXT_DATA__" in b["url"] or "json-block" in b["url"] for b in blobs)


def test_embedded_script_var_assignment() -> None:
    plans = [
        {
            "id": i,
            "name": f"A{i}",
            "bedrooms": 1 + (i % 3),
            "rent": 1500 + 100 * i,
            "sqft": 650 + 50 * i,
            "building": "Main",
            "availableDate": "2026-05-01",
        }
        for i in range(8)
    ]
    html = f"""<html><body>
    <script>
    var floorPlans = {json.dumps(plans)};
    console.log('ok');
    </script></body></html>"""
    blobs = extract_embedded_blobs_from_html(html)
    assert len(blobs) >= 1
    assert any("floorPlans" in b["url"] for b in blobs)


def test_embedded_gates_unit_keyword_presence() -> None:
    """Random inline script without unit keywords must not be picked up."""
    html = """<html><body>
    <script>var trackingConfig = {"gtm_id": "GTM-ABC", "user_id": 42};</script>
    </body></html>"""
    blobs = extract_embedded_blobs_from_html(html)
    assert blobs == []


def test_embedded_window_nextdata_inline() -> None:
    payload = {
        "buildId": "x",
        "props": {
            "pageProps": {
                "floorplans": [
                    {
                        "id": i,
                        "name": f"B{i}",
                        "beds": 1 + (i % 2),
                        "rent": 1400 + 100 * i,
                        "sqft": 700 + 40 * i,
                    }
                    for i in range(5)
                ]
            }
        },
    }
    html = f"""<html><body>
    <script>window.__NEXT_DATA__ = {json.dumps(payload)};</script>
    </body></html>"""
    blobs = extract_embedded_blobs_from_html(html)
    assert len(blobs) >= 1


# ── Generic adapter end-to-end (fetch_result.body only, no page) ─────────────


class _FetchResult:
    """Minimal stand-in for the Jugnu FetchResult (only .body is needed here)."""

    def __init__(self, body: bytes) -> None:
        self.body = body


@pytest.mark.asyncio
async def test_generic_adapter_recovers_units_from_embedded_json() -> None:
    """Raw HTML with inline floorPlans assignment — no API, no JSON-LD."""
    plans = [
        {
            "id": f"A{i}",
            "name": f"A{i}",
            "bedrooms": 1 + (i % 2),
            "rent": 1500 + 50 * i,
            "sqft": 650 + 25 * i,
            "availableDate": "2026-05-01",
            "building": "Main",
        }
        for i in range(6)
    ]
    html = f"""<html><body>
    <script>
    var floorPlans = {json.dumps(plans)};
    </script></body></html>"""
    fr = _FetchResult(html.encode("utf-8"))
    ctx = AdapterContext(
        base_url="https://example.com/",
        detected=detect_pms("https://example.com/"),
        profile=None,
        expected_total_units=None,
        property_id="test",
        fetch_result=fr,
    )
    ctx._api_responses = []  # type: ignore[attr-defined]

    result = await GenericAdapter().extract(None, ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) >= 1, f"Expected units from embedded JSON; errors={result.errors}"
    assert result.tier_used == "TIER_1_5_EMBEDDED"


# ── Bug 4 (2026-05-09) — JSON-LD Pass 3: standalone Offer arrays ─────────────


def test_bug4_pass3_standalone_offers_emit_units() -> None:
    """Bug 4: bare ``Offer`` nodes that are siblings of Brand/PostalAddress
    (gscapts.com / Jonah Systems / Knock CMS shape) get emitted as units.
    Pass 1 skips bare Offers and Pass 2 won't descend into Brand — Pass 3
    closes the gap."""
    html = """<html><head>
    <script type="application/ld+json">
    [
      {"@type": "PostalAddress", "addressLocality": "Largo"},
      {"@type": "Brand", "name": "Madison"},
      {"@type": "Offer", "name": "1BR Plan A", "price": "1450"},
      {"@type": "Offer", "name": "2BR Plan B", "price": "1850"},
      {"@type": "Offer", "name": "3BR Plan C", "price": "2350"}
    ]
    </script></head></html>"""
    units = extract_jsonld_from_html(html, "https://gscapts.com/")
    assert len(units) == 3
    names = {u.get("floor_plan_name") for u in units}
    assert names == {"1BR Plan A", "2BR Plan B", "3BR Plan C"}


def test_bug4_pass3_requires_distinguishing_dimensions() -> None:
    """Bug 4: a single Offer replicated 3× must NOT inflate to 3 units.
    The distinguishing-fields guard (≥2 distinct prices OR ≥2 distinct names)
    drops degenerate arrays."""
    html = """<html><head>
    <script type="application/ld+json">
    [
      {"@type": "Offer", "name": "Plan A", "price": "1450"},
      {"@type": "Offer", "name": "Plan A", "price": "1450"},
      {"@type": "Offer", "name": "Plan A", "price": "1450"}
    ]
    </script></head></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    assert units == []


def test_bug4_pass3_only_fires_when_passes_1_2_empty() -> None:
    """Bug 4: when Pass 1 (Apartment) already produced units, Pass 3 must
    NOT add duplicates from any incidental Offer siblings."""
    html = """<html><head>
    <script type="application/ld+json">
    [
      {"@context":"https://schema.org","@type":"Apartment",
       "name":"The Parker 1x1","numberOfRooms":1,
       "offers":{"@type":"Offer","lowPrice":1800,"highPrice":2000}},
      {"@type": "Offer", "name": "Stray", "price": "999"},
      {"@type": "Offer", "name": "Other", "price": "888"}
    ]
    </script></head></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    # Only the Apartment unit — Pass 3 short-circuits because Pass 1 emitted.
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "The Parker 1x1"


def test_bug4_pass3_handles_offers_nested_in_brand() -> None:
    """Bug 4: the Pass 3 walker must recurse into non-listed containers like
    ``Brand`` so Offers nested there are still discovered."""
    html = """<html><head>
    <script type="application/ld+json">
    {
      "@type": "Brand",
      "name": "Madison",
      "makesOffer": [
        {"@type": "Offer", "name": "1BR", "price": "1450"},
        {"@type": "Offer", "name": "2BR", "price": "1850"}
      ]
    }
    </script></head></html>"""
    units = extract_jsonld_from_html(html, "https://example.com/")
    # Pass 3 walks dict.values() so the "makesOffer" array is reached and the
    # bare Offers inside are emitted.
    assert len(units) >= 2


# ─────────────────────────────────────────────────────────────────────
# 2026-05-20 random-30 probe finding: ~17% of "no_units" cohort had real
# unit data on the homepage in custom-CMS table-row layouts. The DOM
# scanner had ~30 ``div.*`` / ``article.*`` container selectors but ZERO
# ``tr.*`` patterns — sites like Corsa Management's Greenwood Village
# (``<tr class="prisma-units-row">``) fell through to LLM rescue or 0
# extraction. Adding TR-based selectors is low-risk because the existing
# >200-node and rent + structural-signal gates filter false positives.
# ─────────────────────────────────────────────────────────────────────


def test_extract_units_from_dom_handles_prisma_units_row() -> None:
    """goprisma (Corsa Management) prisma-units-table → UNIT-LEVEL (#93).

    Verified live 2026-05-20 against Greenwood Village. The ``first_tr_units``
    row is a plan SUMMARY (one per floor plan, no apartment) and is excluded;
    ``unit_details`` rows carry ``data-_id`` (the goprisma unit PK) and
    ``data-unoitId`` (the unit label) and become the real units. The full
    5-unit roster is pinned against the saved fixture in test_prisma_units.py.
    """
    from ma_poc.pms.adapters._html_extract import extract_units_from_dom

    html = """<html><body>
    <table class="prisma-units-table">
      <tbody class="prisma-units-body">
        <tr class="prisma-units-row first_tr_units">
          <td>plan image</td>
          <td class="prisma-units-row-autoi">1BR -3RM</td>
          <td class="prisma-units-row-autoi">1BA</td>
          <td><div class="unit_space">560 sqft</div></td>
          <td class="prisma-units-data">$1,450 - 1,500</td>
        </tr>
        <tr class="prisma-units-row unit_details" data-_id="pk-001" data-unoitId="N207-1">
          <td>unit</td>
          <td class="prisma-units-row-autoi">1BR -3RM</td>
          <td class="prisma-units-row-autoi">1BA</td>
          <td><div class="unit_space">560 sqft</div></td>
          <td class="prisma-units-data">$1,450</td>
        </tr>
        <tr class="prisma-units-row unit_details" data-_id="pk-002" data-unoitId="L16-2">
          <td>unit</td>
          <td class="prisma-units-row-autoi">2BR -5RM</td>
          <td class="prisma-units-row-autoi">2BA</td>
          <td><div class="unit_space">880 sqft</div></td>
          <td class="prisma-units-data">$2,100</td>
        </tr>
      </tbody>
    </table>
    </body></html>"""
    units, hit_mode = extract_units_from_dom(html, "https://example.com/")
    # first_tr_units summary excluded; the two unit_details rows become units.
    assert len(units) == 2, f"expected 2 units, got {len(units)}"
    assert {u.get("unit_id") for u in units} == {"pk-001", "pk-002"}
    assert {u.get("unit_number") for u in units} == {"N207-1", "L16-2"}
    rents = sorted(u["market_rent_low"] for u in units if u.get("market_rent_low"))
    assert rents == [1450, 2100], f"unexpected rents: {rents}"


def test_extract_units_from_dom_handles_unit_row_class_suffix() -> None:
    """Generic ``tr[class*='unit-row']`` catches custom-CMS variants
    that name their rows ``greenwood-unit-row``, ``my-unit-row``, etc.
    Without the wildcard suffix selector, every new theme needs its
    own explicit entry."""
    from ma_poc.pms.adapters._html_extract import extract_units_from_dom

    html = """<html><body>
    <table>
      <tr class="custom-cms-unit-row">
        <td>1 Bed</td>
        <td>1 Bath</td>
        <td>700 sqft</td>
        <td>$1,895</td>
      </tr>
    </table>
    </body></html>"""
    units, hit_mode = extract_units_from_dom(html, "https://example.com/")
    assert len(units) == 1
    assert units[0]["market_rent_low"] == 1895


def test_dom_container_selectors_includes_tr_patterns() -> None:
    """Source-grep guard: the DOM cascade MUST include TR-row selectors.
    A future refactor that drops them silently would regress Greenwood-
    shape sites without firing any test failure on synthetic fixtures."""
    from ma_poc.pms.adapters._html_extract import _DOM_CONTAINER_SELECTORS

    assert any(s.startswith("tr.") or s.startswith("tr[") for s in _DOM_CONTAINER_SELECTORS), (
        "DOM cascade must include TR-row selectors for custom-CMS unit tables"
    )


# ── bucket-B grind (2026-05-22): container text-format parser fixes ──────────
# The reached-but-empty cohort failed because _container_yields_unit
# mis-parsed three common container text formats. Pin each fix.


class TestContainerTextFormatFixes:
    def _u(self, text):
        from ma_poc.pms.adapters._html_extract import _container_yields_unit

        return _container_yields_unit(text)

    def test_rent_pattern_no_truncate_4digit_no_comma(self) -> None:
        """$2087 must parse as 2087, not 208 (alternation-order bug)."""
        u = self._u("2 Bed 2 Bath 900 sqft $2087")
        assert u is not None
        assert u["market_rent_low"] == 2087

    def test_tilde_range_and_label_first_beds_sqft(self) -> None:
        """'Price Range $1891 ~ $2087 BR 2 ... SqFt 833' — beds must be 2
        (not the rent number), sqft 833, rent the real range."""
        u = self._u("Price Range $1891 ~ $2087 BR 2 Utly None SqFt 833 Avail 7/2/2026")
        assert u is not None
        assert u["bedrooms"] == "2"
        assert u["sqft"] == "833"
        assert u["market_rent_low"] == 1891
        assert u["market_rent_high"] == 2087

    def test_deposit_excluded_from_rent(self) -> None:
        """'Rent: $808 Deposit: $300' — rent is 808, deposit is not a range."""
        u = self._u("1 bed 1 bath 655 ft² Rent: $808 Deposit: $300")
        assert u is not None
        assert u["market_rent_low"] == 808
        assert u["market_rent_high"] == 808

    def test_deposit_before_sqft_label_not_read_as_sqft(self) -> None:
        """'Deposit: $200 Square Feet: 980' — sqft is 980, not the $200."""
        u = self._u("2 Bedroom / 2 Bath Price: $1290-$1295 Deposit: $200 Square Feet: 980")
        assert u is not None
        assert u["sqft"] == "980"
        assert u["market_rent_low"] == 1290

    def test_ft_superscript_sqft(self) -> None:
        """'655 ft²' must register as sqft 655."""
        u = self._u("1 bed 1 bath 655 ft² $900")
        assert u is not None
        assert u["sqft"] == "655"

    def test_existing_number_first_formats_still_work(self) -> None:
        """Regression guard: the canonical '1 BR / 1 BA – 611 sq ft – $605'
        number-first format must still parse exactly as before."""
        u = self._u("1 BR / 1 BA – 611 sq ft – $605")
        assert u is not None
        assert u["bedrooms"] == "1"
        assert u["bathrooms"] == "1"
        assert u["sqft"] == "611"
        assert u["market_rent_low"] == 605


class TestBucketBDomFixtures:
    """End-to-end: the two saved bucket-B fixtures extract clean units."""

    def _fixture(self, name):
        from pathlib import Path

        p = Path(__file__).parents[2] / "fixtures" / "bucketb" / name
        return p.read_text(encoding="utf-8")

    def test_apartment_info_block_redoak(self) -> None:
        from ma_poc.pms.adapters._html_extract import extract_units_from_dom

        units, _ = extract_units_from_dom(
            self._fixture("apartment_info_block_redoak.html"), "https://x.test/"
        )
        assert len(units) >= 5
        priced = [u for u in units if u.get("sqft") and u.get("market_rent_low")]
        assert len(priced) >= 5
        # no garbage: bedrooms must be a small int, never a rent number
        for u in units:
            if u.get("bedrooms"):
                assert int(float(u["bedrooms"])) <= 6, f"bad beds: {u['bedrooms']}"

    def test_floor_plan_creekview(self) -> None:
        from ma_poc.pms.adapters._html_extract import extract_units_from_dom

        units, _ = extract_units_from_dom(self._fixture("floor_plan_creekview.html"), "https://x.test/")
        assert len(units) >= 6
        priced = [u for u in units if u.get("sqft") and u.get("market_rent_low")]
        assert len(priced) >= 5
