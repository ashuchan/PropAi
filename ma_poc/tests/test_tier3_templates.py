"""Tests for extraction/tier3_templates.py + each PMS template — 9+ tests."""
from __future__ import annotations

from extraction import tier3_templates
from models.extraction_result import ExtractionStatus
from scraper.browser import BrowserSession
from templates import appfolio, entrata, rentcafe


def _session(html: str, pms: str | None = None) -> BrowserSession:
    s = BrowserSession(property_id="P1", url="https://example.com/", pms_platform=pms)
    s.html = html
    return s


def test_rentcafe_list_view(rentcafe_html: str) -> None:
    records = rentcafe.extract(rentcafe_html, "P1")
    assert len(records) == 3
    by_num = {r.unit_number: r for r in records}
    assert by_num["101"].asking_rent == 3250.0
    assert by_num["101"].sqft == 750


def test_rentcafe_floorplan_grouped() -> None:
    html = """<html><body>
    <div class="floorplanContainer">
      <div class="floorplanName">1/1</div>
      <div class="pricingWrapper"><div class="unitNumber">A1</div><div class="rent">$1,500</div><div class="sqft">600 sf</div><div class="availabilityDate">Available</div></div>
      <div class="pricingWrapper"><div class="unitNumber">A2</div><div class="rent">$1,550</div><div class="sqft">605 sf</div><div class="availabilityDate">Available</div></div>
    </div></body></html>"""
    records = rentcafe.extract(html, "P1")
    assert len(records) == 2


def test_rentcafe_all_selectors_fail_returns_empty() -> None:
    records = rentcafe.extract("<html><body><p>nothing</p></body></html>", "P1")
    assert records == []


def test_entrata_standard(entrata_html: str) -> None:
    records = entrata.extract(entrata_html, "P1")
    assert len(records) == 3
    assert any(r.unit_number == "A301" for r in records)
    assert any(r.asking_rent == 3650.0 for r in records)


def test_entrata_lazy_loaded_rows() -> None:
    """Even after lazy load, the rows present in the DOM are extracted."""
    html = """<html><body>
    <div class="entrata-unit-row"><span class="unit-number">L1</span><span class="unit-price">$1000</span><span class="unit-availability">Available</span></div>
    </body></html>"""
    records = entrata.extract(html, "P1")
    assert len(records) == 1


def test_entrata_fail_returns_empty() -> None:
    assert entrata.extract("<html></html>", "P1") == []


def test_appfolio_standard(appfolio_html: str) -> None:
    records = appfolio.extract(appfolio_html, "P1")
    assert len(records) == 3
    assert any(r.unit_number == "3A" for r in records)


def test_appfolio_paginated_rows() -> None:
    html = """<html><body>
    <div class="js-listing-card" data-unit-number="P1"><div class="price">$2k</div><div class="status">Available</div></div>
    <div class="js-listing-card" data-unit-number="P2"><div class="price">$2.1k</div><div class="status">Leased</div></div>
    </body></html>"""
    records = appfolio.extract(html, "X")
    assert len(records) == 2


def test_appfolio_fail_returns_empty() -> None:
    assert appfolio.extract("<html></html>", "P1") == []


async def test_dispatcher_picks_correct_template(rentcafe_html: str) -> None:
    result = await tier3_templates.extract(_session(rentcafe_html, pms="rentcafe"))
    assert result.status == ExtractionStatus.SUCCESS
    assert result.raw_fields["platform"] == "rentcafe"
