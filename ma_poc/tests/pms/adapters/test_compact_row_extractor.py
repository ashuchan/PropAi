"""Tests for compact-row field extraction + G5 widget SightMap hint.

Covers two extraction paths previously blocked by the generic ``_container_yields_unit``
≥2-structural-signal gate or by missing inline-JS PMS init patterns:

  * Per-plan availability rows whose visible text is too short to pass the
    generic gate ("Unit 138 $1,115 Sq.ft. 1,025 Available Now"). The
    specialised extractors (``_extract_rentcafe_option_row``,
    ``_extract_brook_availapts_card``) read field values from child
    selectors and inherit beds/baths/plan_name from a page-level context
    computed once per page.
  * G5 Marketing Cloud floor-plans-plus-config exposes a ``sightmapID``
    key; the inline-JS init scanner extracts it and synthesises the
    canonical ``sightmap.com/embed/{id}`` URL so the existing SightMap
    adapter can recover unit data without a vendor-specific handler.
"""
from __future__ import annotations

import pytest


# ── Compact-row extractors (RentCafe option-row, Brook #availApts card) ───

class TestCompactRowFieldExtractor:
    """Per-plan availability pages whose rows are too short for the
    ≥2-structural-signal `_container_yields_unit` gate are extracted via
    specialised selector-specific functions that read fields from child
    elements and inherit beds/baths/plan_name from the page header."""

    def test_page_ctx_extracts_beds_baths_fp_name(self) -> None:
        from bs4 import BeautifulSoup
        from ma_poc.pms.adapters._html_extract import _extract_page_ctx
        html = """
        <html><head>
          <title>2x2 RENO - The Izzy</title>
          <meta property="og:title" content="2x2 RENO Floor Plan | The Izzy">
        </head><body>
          <h1>2x2 RENO Floor Plan</h1>
          <h2>2 Bedrooms / 2 Bath / 1,025 sqft</h2>
        </body></html>
        """
        soup = BeautifulSoup(html, "lxml")
        ctx = _extract_page_ctx(soup)
        assert ctx["beds"] == "2"
        assert ctx["baths"] == "2"
        # Plan name extracted from one of h1/h2/title/og:title
        assert "2x2" in ctx["fp_name"].lower() or "reno" in ctx["fp_name"].lower()

    def test_page_ctx_studio(self) -> None:
        from bs4 import BeautifulSoup
        from ma_poc.pms.adapters._html_extract import _extract_page_ctx
        html = "<html><head><title>Studio - The Hub</title></head></html>"
        soup = BeautifulSoup(html, "lxml")
        ctx = _extract_page_ctx(soup)
        assert ctx["is_studio"] is True
        assert ctx["beds"] == "0"

    def test_rentcafe_option_row_extracts_full_unit(self) -> None:
        """Izzy 2x2_reno-shaped option-row carries unit#, rent, sqft, avail,
        plus a data-unit attribute on the See Details button."""
        from bs4 import BeautifulSoup
        from ma_poc.pms.adapters._html_extract import _extract_rentcafe_option_row
        html = """
        <div class="option-row">
          <div class="detail first"><span class="mobile-text">Unit</span> 138</div>
          <div class="detail second" id="rent">
            <span class="stat-value unit-rent">$1,115</span>
          </div>
          <div class="detail block" id="sq-feet">Sq.ft. 1,025</div>
          <div class="detail block">Available Now</div>
          <button data-unit="5704854" data-target="#x">See Details</button>
        </div>
        """
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one("div.option-row")
        ctx = {"beds": "2", "baths": "2", "fp_name": "2x2 RENO", "is_studio": False}
        unit = _extract_rentcafe_option_row(node, ctx, "https://example.com/x")
        assert unit is not None
        assert unit["unit_id"] == "5704854"
        assert unit["unit_number"] == "138"
        assert unit["market_rent_low"] == 1115
        assert unit["sqft"] == "1025"
        assert unit["bedrooms"] == "2"
        assert unit["floor_plan_name"] == "2x2 RENO"
        assert unit["availability_date"] == "Now"

    def test_rentcafe_option_row_skips_empty_rows(self) -> None:
        """A nominal `.option-row` selector hit that's actually a wrapper
        (e.g. column header row) with no unit data returns None."""
        from bs4 import BeautifulSoup
        from ma_poc.pms.adapters._html_extract import _extract_rentcafe_option_row
        html = '<div class="option-row"><span>Header text</span></div>'
        node = BeautifulSoup(html, "lxml").select_one("div.option-row")
        ctx = {"beds": "", "baths": "", "fp_name": "", "is_studio": False}
        unit = _extract_rentcafe_option_row(node, ctx, "https://example.com/x")
        assert unit is None

    def test_brook_availapts_card_extracts_unit(self) -> None:
        """Brook `#availApts .card` carries 'Apartment: # NNNNNN' + 'Starting at: $NNN'
        + an Apply Now link with UnitID=N as href param."""
        from bs4 import BeautifulSoup
        from ma_poc.pms.adapters._html_extract import _extract_brook_availapts_card
        html = """
        <div class="card">
          <div class="card-body text-center">
            <h3 class="h5 card-title">Apartment: <span># 517001</span></h3>
            <p class="card-subtitle mb-2 text-muted">Available Now</p>
            <p class="card-subtitle mb-2 text-muted"><span>Starting at:</span> $1,745.00</p>
            <a href="https://securecafe.com/x?FloorPlanID=2283832&amp;UnitID=10501058">Apply Now</a>
          </div>
        </div>
        """
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one("div.card")
        ctx = {"beds": "1", "baths": "1", "fp_name": "1 Bed / 1 Bath", "is_studio": False}
        unit = _extract_brook_availapts_card(node, ctx, "https://thebrookatcolumbia.com/x")
        assert unit is not None
        assert unit["unit_number"] == "517001"
        assert unit["unit_id"] == "10501058"  # from UnitID=N in href
        assert unit["market_rent_low"] == 1745
        assert unit["bedrooms"] == "1"
        assert unit["floor_plan_name"] == "1 Bed / 1 Bath"

    def test_end_to_end_izzy_per_plan_extracts_all_rows(self) -> None:
        """End-to-end: a per-plan HTML with N option-row blocks emits N
        unit records — not blocked by `_container_yields_unit` ≥2-signal gate."""
        from ma_poc.pms.adapters._html_extract import extract_units_from_dom
        # Build a synthetic 3-row option-row page that previously would
        # have emitted 0 (rows too short for the ≥2-signal gate).
        rows = "".join(
            f'''
            <div class="option-row">
              <div class="detail first"><span class="mobile-text">Unit</span> {100 + i}</div>
              <div class="detail second"><span class="unit-rent">$1,{i:03d}</span></div>
              <div class="detail block" id="sq-feet">Sq.ft. 1,025</div>
              <div class="detail block">Available Now</div>
              <button data-unit="570{i:04d}">See Details</button>
            </div>
            '''
            for i in range(3)
        )
        html = f"""
        <html><head><title>2x2 Floor Plan - 2 Bed 2 Bath</title></head>
        <body>
          <h1>2x2 Floor Plan</h1>
          {rows}
        </body></html>
        """
        units, mode = extract_units_from_dom(html, "https://example.com/x")
        assert len(units) == 3
        # All have real unit_ids from data-unit attrs
        uids = sorted(u["unit_id"] for u in units)
        assert uids == ["5700000", "5700001", "5700002"]
        # All inherit beds=2 from the page header
        assert all(u["bedrooms"] == "2" for u in units)


# ── F2 + F3 (2026-05-20): RentCafe vanity `.fp-container` data-attr cards ─

class TestRentCafeFpContainerDataAttrs:
    """RentCafe vanity-site plan cards (1105townbrookhaven, wymberlycrossing,
    sussexwestlife, …) carry the canonical name/beds/sqft/price values as
    `data-floorplan-*` attributes on descendant Apply / Guided-Tour buttons.

    Verified live against 1105townbrookhaven-apts.com/floorplans 2026-05-20:
    19 `.fp-container` cards, 38 `data-floorplan-price` attrs total (each
    plan in two buttons), all four `data-floorplan-*` attrs present.
    """

    def test_extracts_price_range_lo_hi(self) -> None:
        """Standard live shape: data-floorplan-price="1660-2199"."""
        from bs4 import BeautifulSoup
        from ma_poc.pms.adapters._html_extract import _extract_rentcafe_data_attrs
        html = """
        <div class="fp-container" id="fp-container-6114511">
          <h3 class="card-title">A1</h3>
          <a class="btn btn-primary track-apply"
             data-floorplan-name="A1"
             data-floorplan-size="1"
             data-floorplan-sqft="682"
             data-floorplan-price="1660-2199">Availability</a>
        </div>
        """
        node = BeautifulSoup(html, "lxml").select_one(".fp-container")
        ctx = {"beds": "", "baths": "", "fp_name": "", "is_studio": False}
        unit = _extract_rentcafe_data_attrs(node, ctx, "https://example.com/floorplans")
        assert unit is not None
        assert unit["floor_plan_name"] == "A1"
        assert unit["bedrooms"] == "1"
        assert unit["sqft"] == "682"
        assert unit["market_rent_low"] == 1660
        assert unit["market_rent_high"] == 2199
        assert unit["rent_range"] == "$1,660 - $2,199"

    def test_extracts_price_with_space_before_dash(self) -> None:
        """Live RentCafe sometimes emits "1660 -2199" with a leading space
        on HI — observed in PID 60578 forensic. Regex must accept this."""
        from bs4 import BeautifulSoup
        from ma_poc.pms.adapters._html_extract import _extract_rentcafe_data_attrs
        html = """
        <div class="fp-container">
          <a data-floorplan-name="B1" data-floorplan-size="2"
             data-floorplan-sqft="1276" data-floorplan-price="2207 -3046">Apply</a>
        </div>
        """
        node = BeautifulSoup(html, "lxml").select_one(".fp-container")
        unit = _extract_rentcafe_data_attrs(
            node, {"beds": "", "baths": "", "fp_name": "", "is_studio": False},
            "https://example.com/floorplans",
        )
        assert unit is not None
        assert unit["market_rent_low"] == 2207
        assert unit["market_rent_high"] == 3046

    def test_extracts_single_price_lo_only(self) -> None:
        """When data-floorplan-price is a single number ('1660'), use it
        for both rent_low and rent_high."""
        from bs4 import BeautifulSoup
        from ma_poc.pms.adapters._html_extract import _extract_rentcafe_data_attrs
        html = """
        <div class="fp-container">
          <a data-floorplan-name="A2" data-floorplan-size="1"
             data-floorplan-sqft="693" data-floorplan-price="1566">Apply</a>
        </div>
        """
        node = BeautifulSoup(html, "lxml").select_one(".fp-container")
        unit = _extract_rentcafe_data_attrs(
            node, {"beds": "", "baths": "", "fp_name": "", "is_studio": False},
            "https://example.com/floorplans",
        )
        assert unit is not None
        assert unit["market_rent_low"] == 1566
        assert unit["market_rent_high"] == 1566

    def test_price_zero_sentinel_yields_unit_without_rent(self) -> None:
        """RentCafe uses "0" as a placeholder for "Contact Us" / no public
        rent. Don't ship 0 as a rent number — fall back to sqft-only row."""
        from bs4 import BeautifulSoup
        from ma_poc.pms.adapters._html_extract import _extract_rentcafe_data_attrs
        html = """
        <div class="fp-container">
          <a data-floorplan-name="A3" data-floorplan-size="1"
             data-floorplan-sqft="800" data-floorplan-price="0">Apply</a>
        </div>
        """
        node = BeautifulSoup(html, "lxml").select_one(".fp-container")
        unit = _extract_rentcafe_data_attrs(
            node, {"beds": "", "baths": "", "fp_name": "", "is_studio": False},
            "https://example.com/floorplans",
        )
        # Sqft present → row still emits, but with no rent set.
        assert unit is not None
        assert unit["floor_plan_name"] == "A3"
        assert unit.get("market_rent_low") is None
        assert unit["sqft"] == "800"

    def test_missing_floorplan_name_returns_none(self) -> None:
        """No data-floorplan-name = not a RentCafe plan card."""
        from bs4 import BeautifulSoup
        from ma_poc.pms.adapters._html_extract import _extract_rentcafe_data_attrs
        html = '<div class="fp-container"><a>just some content</a></div>'
        node = BeautifulSoup(html, "lxml").select_one(".fp-container")
        unit = _extract_rentcafe_data_attrs(
            node, {"beds": "", "baths": "", "fp_name": "", "is_studio": False},
            "https://example.com/floorplans",
        )
        assert unit is None

    def test_studio_size_zero_renders_studio_bed_label(self) -> None:
        from bs4 import BeautifulSoup
        from ma_poc.pms.adapters._html_extract import _extract_rentcafe_data_attrs
        html = """
        <div class="fp-container">
          <a data-floorplan-name="Studio-A" data-floorplan-size="0"
             data-floorplan-sqft="540" data-floorplan-price="1450-1550">Apply</a>
        </div>
        """
        node = BeautifulSoup(html, "lxml").select_one(".fp-container")
        unit = _extract_rentcafe_data_attrs(
            node, {"beds": "", "baths": "", "fp_name": "", "is_studio": False},
            "https://example.com/floorplans",
        )
        assert unit is not None
        assert unit["bedrooms"] == "0"
        assert unit["bed_label"] == "Studio"

    def test_end_to_end_extract_units_from_dom_picks_fp_container(self) -> None:
        """Full cascade: extract_units_from_dom should route .fp-container
        cards through _extract_rentcafe_data_attrs and emit one row per
        card with rent / beds / sqft populated."""
        from ma_poc.pms.adapters._html_extract import extract_units_from_dom
        # Synthetic 3-plan page modelling the live 1105townbrookhaven shape
        # — each card has both an Availability and a Guided Tour button
        # carrying the same data-floorplan-* attrs (mirrors live).
        cards = []
        for i, (name, beds, sqft, lo, hi) in enumerate([
            ("A1", "1", "682", 1660, 2199),
            ("A2", "1", "693", 1566, 2095),
            ("B1", "2", "1276", 2207, 3046),
        ]):
            cards.append(f"""
            <div class="fp-container" id="fp-container-{i+1}">
              <h3 class="card-title">{name}</h3>
              <a class="btn btn-primary track-apply"
                 data-floorplan-name="{name}" data-floorplan-size="{beds}"
                 data-floorplan-sqft="{sqft}" data-floorplan-price="{lo}-{hi}">Availability</a>
              <a class="btn btn-outline-dark"
                 data-floorplan-name="{name}" data-floorplan-size="{beds}"
                 data-floorplan-sqft="{sqft}" data-floorplan-price="{lo}-{hi}">Guided Tour</a>
            </div>
            """)
        html = f"<html><body><h1>Floor Plans</h1>{''.join(cards)}</body></html>"
        units, mode = extract_units_from_dom(html, "https://example.com/floorplans")
        assert mode == "default"
        assert len(units) == 3, f"expected 3 cards, got {len(units)}: {[u['floor_plan_name'] for u in units]}"
        # Verify A1 specifically
        a1 = next(u for u in units if u["floor_plan_name"] == "A1")
        assert a1["bedrooms"] == "1"
        assert a1["sqft"] == "682"
        assert a1["market_rent_low"] == 1660
        assert a1["market_rent_high"] == 2199


# ── G5 Marketing Cloud widget config sightmapID extraction ────────────────

class TestG5WidgetSightmapHint:
    """G5 Marketing Cloud floor-plans-plus widget exposes `sightmapID` in
    a JSON config script. Extracting it server-side and queueing the
    canonical sightmap.com/embed/{id} URL routes the property through
    the existing Sightmap adapter."""

    def test_g5_sightmap_id_extracted(self) -> None:
        """Hickory Mill / Morgan Properties shape."""
        from ma_poc.pms.scraper import _scan_inline_js_pms_init
        html = """
        <script id="floor-plans-plus-config" type="application/json">
        {
          "widgetId": "floor-plans-plus-35591661",
          "locationUrn": "g5-cl-1q7sxy2zz3-morgan-properties-...",
          "inventoryHost": "https://inventory.g5marketingcloud.com",
          "sightmapID": "ryzvg6mywln"
        }
        </script>
        """
        hits = _scan_inline_js_pms_init(html)
        sightmap_urls = [u for u, p in hits if p == "sightmap"]
        assert "https://sightmap.com/embed/ryzvg6mywln" in sightmap_urls

    def test_g5_sightmap_case_insensitive(self) -> None:
        """`sightmapID` / `sightmapid` / `sightmap_id` variants all match."""
        from ma_poc.pms.scraper import _scan_inline_js_pms_init
        # _scan_inline_js_pms_init requires html length >= 100; pad with neutral.
        # This test confirms the regex matches case-insensitively.
        html = (
            "<html><body><script id='floor-plans-plus-config' "
            "type='application/json'>"
            '{"sightmapid": "abc123def4", "irrelevant": "padding text to exceed 100 chars"}'
            "</script></body></html>"
        )
        hits = _scan_inline_js_pms_init(html)
        urls = [u for u, _p in hits]
        assert "https://sightmap.com/embed/abc123def4" in urls
