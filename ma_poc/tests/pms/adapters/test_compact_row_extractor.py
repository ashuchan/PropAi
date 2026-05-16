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
