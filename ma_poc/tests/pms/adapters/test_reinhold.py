"""Reinhold Residential adapter tests (rr-unit-block SSR table parser).

Live fixtures (captured 2026-05-25 via curl_cffi chrome120):
  - ma_poc/tests/fixtures/reinhold/chocolateworks_availability.html
    (7 units, 1 rr-unit-block, 1 toggle — chocolateworks-living.com)
  - ma_poc/tests/fixtures/reinhold/shadyside_availability.html
    (18 units, 2 rr-unit-blocks, 2 toggles — shadyside-living.com)

Coverage targets the column-position contract (Type | Floor Plan |
Rent | Available | Apply), the Available Now → today normalisation,
the apply-link → source_ids mining, and the detector routing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ma_poc.pms.adapters.reinhold import (
    REINHOLD_TIER,
    ReinholdAdapter,
    parse_reinhold_units,
)
from ma_poc.pms.detector import detect_pms

# Anchor on this file, not the process CWD — ``pytest tests/pms`` from inside
# ma_poc/ must resolve fixtures the same way a repo-root run does.
_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "reinhold"
_CHOCO = _FIX / "chocolateworks_availability.html"
_SHADY = _FIX / "shadyside_availability.html"


def _load(p: Path) -> str:
    return p.read_text(encoding="utf-8")


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
    """Stub the ``_probe`` seam for every test in this module.

    ``_fetch_availability_html`` tries ``{base}/availability/`` through
    ``probe_get`` before it will look at ``ctx.fetch_result.body``. The
    adapter test below is specifically about the body-only path ("no
    Playwright… mirrors the L1-only call path used in prod"), so the
    probe must come back empty-handed. A 404 does that deterministically
    — the live chocolateworks-living.com request the test used to make
    was never the subject, and the fixture it asserts against
    (``ma_poc/tests/fixtures/reinhold/chocolateworks_availability.html``)
    is already loaded into ``ctx.fetch_result.body``.
    """

    def _fake_probe_get(url: str, **_kw: object) -> _FakeProbeResponse:
        return _FakeProbeResponse(url)

    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get", _fake_probe_get
    )


# ── Parser tests (10) ──────────────────────────────────────────────────────


def test_parses_all_7_chocolateworks_units() -> None:
    units = parse_reinhold_units(_load(_CHOCO), "https://x.test/")
    assert len(units) == 7, f"expected 7; got {len(units)}"


def test_parses_all_18_shadyside_units() -> None:
    units = parse_reinhold_units(_load(_SHADY), "https://x.test/")
    assert len(units) == 18, f"expected 18; got {len(units)}"


def test_units_have_unit_number_rent_date() -> None:
    """Every parsed row must carry a unit number, rent, and date — these
    are the three fields prod consolidation gates on."""
    units = parse_reinhold_units(_load(_CHOCO), "https://x.test/")
    assert units, "no units extracted"
    for u in units:
        assert u["unit_number"], f"missing unit_number: {u!r}"
        assert u["market_rent_low"], f"missing rent: {u!r}"
        assert u["availability_date"], f"missing date: {u!r}"
        # YYYY-MM-DD ISO format
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", u["availability_date"])


def test_known_unit_449_0203_full_extraction() -> None:
    """First chocolateworks unit — verify every field."""
    units = parse_reinhold_units(_load(_CHOCO), "https://cw.test/availability/")
    u = next((x for x in units if x["unit_number"] == "449-0203"), None)
    assert u is not None, "unit 449-0203 not found"
    assert u["bedrooms"] == "1"
    assert u["bathrooms"] == "1"
    assert u["market_rent_low"] == 1975
    assert u["market_rent_high"] == 1975
    assert u["availability_date"] == "2026-06-06"
    assert u["floor_plan_name"] == "1 Bed 1 Bath"
    assert u["availability_status"] == "AVAILABLE"
    assert u["extraction_tier"] == REINHOLD_TIER
    assert u["source_api_url"] == "https://cw.test/availability/"
    # Apply link supplies BOTH apartment_id + floor_plan_id
    assert u["source_ids"]["apartment_id"] == "4172232"
    assert u["source_ids"]["floor_plan_id"] == "1360590"


def test_loft_variant_distinct_floor_plan() -> None:
    """The toggle groups 1BR + 1BR-Loft together, but per-unit floor_plan_name
    must carry the Loft suffix so downstream consolidation distinguishes them."""
    units = parse_reinhold_units(_load(_CHOCO), "https://x.test/")
    lofts = [u for u in units if u["floor_plan_name"] == "1 Bed 1 Bath - Loft"]
    standards = [u for u in units if u["floor_plan_name"] == "1 Bed 1 Bath"]
    assert len(lofts) == 4
    assert len(standards) == 3
    # Lofts get a different floor_plan_id in the apply link
    loft_fpids = {u["source_ids"]["floor_plan_id"] for u in lofts}
    std_fpids = {u["source_ids"]["floor_plan_id"] for u in standards}
    assert loft_fpids.isdisjoint(std_fpids)


def test_available_now_normalises_to_today() -> None:
    """``Available Now`` in cell[3] → today's date in ISO format
    (shadyside fixture has 2 such rows)."""
    units = parse_reinhold_units(_load(_SHADY), "https://x.test/")
    today = datetime.now(tz=UTC).date().isoformat()
    today_units = [u for u in units if u["availability_date"] == today]
    assert len(today_units) >= 2, (
        f"expected at least 2 'Available Now' units → today ({today}); "
        f"got {len(today_units)}"
    )


def test_returns_empty_when_no_rr_unit_block() -> None:
    """Bail-out short-circuit: no class marker → empty list, no exception."""
    assert parse_reinhold_units("", "https://x.test/") == []
    assert parse_reinhold_units("<html><body>nothing</body></html>", "https://x.test/") == []
    # marker substring missing → not Reinhold
    assert (
        parse_reinhold_units(
            "<html><div class='floor-plan-card'>no rr</div></html>",
            "https://x.test/",
        )
        == []
    )


def test_handles_studio_bedroom() -> None:
    """A synthetic Studio row should set bedrooms=0 + bed_label=Studio."""
    html = """
    <html><body>
    <div class="et_pb_module et_pb_toggle rr-pricing-toggle">
      <h5 class="et_pb_toggle_title" aria-label="Studio 450 SQ. FT. | FROM $1200.00">Studio</h5>
      <div class="rr-unit-block">
        <div class="rr-unit-header">
          <div class="rr-price-column">Type</div>
          <div class="rr-price-column">Floor Plan</div>
          <div class="rr-price-column">Rent</div>
          <div class="rr-price-column">Available</div>
        </div>
        <div class="rr-unit">
          <div class="rr-price-column">Studio 1 Bath</div>
          <div class="rr-price-column">100-100</div>
          <div class="rr-price-column">$1200.00</div>
          <div class="rr-price-column">07/01/2026</div>
          <div class="rr-price-column"><a href="x">APPLY</a></div>
        </div>
      </div>
    </div>
    </body></html>
    """
    units = parse_reinhold_units(html, "https://x.test/")
    assert len(units) == 1
    assert units[0]["bedrooms"] == "0"
    assert units[0]["bed_label"] == "Studio"
    assert units[0]["bathrooms"] == "1"
    assert units[0]["market_rent_low"] == 1200


def test_call_for_pricing_toggle_emits_no_units() -> None:
    """When a toggle has no rr-unit-block inside (just a phone CTA),
    no rows are emitted — these aren't actual units."""
    # The shadyside fixture has only available-unit blocks; the chocolateworks
    # one has a 2-bedroom 'Call for Pricing' toggle with no rr-unit-block —
    # so the 7 units we extract are all real, none synthesised.
    units = parse_reinhold_units(_load(_CHOCO), "https://x.test/")
    # If 'Call for Pricing' bled into units, we'd have an N-bed row with
    # no rent. Assert no zero-rent rows.
    assert all(u["market_rent_low"] for u in units)
    # And no 2-bedroom rows (the 2BR toggle is the Call-for-Pricing one)
    assert not any(u["bedrooms"] == "2" for u in units)


def test_malformed_row_under_4_cells_skipped_not_raised() -> None:
    """A defensive row with <4 rr-price-columns is silently skipped
    (don't crash the run on one bad row)."""
    html = """
    <html><body>
    <div class="rr-pricing-toggle">
      <h5 class="et_pb_toggle_title" aria-label="1 Bedroom 500 SQ. FT.">1BR</h5>
      <div class="rr-unit-block">
        <div class="rr-unit">
          <div class="rr-price-column">1 Bed 1 Bath</div>
          <div class="rr-price-column">200-101</div>
        </div>
        <div class="rr-unit">
          <div class="rr-price-column">1 Bed 1 Bath</div>
          <div class="rr-price-column">200-102</div>
          <div class="rr-price-column">$1500.00</div>
          <div class="rr-price-column">08/01/2026</div>
        </div>
      </div>
    </div>
    </body></html>
    """
    units = parse_reinhold_units(html, "https://x.test/")
    # Only the well-formed row survives
    assert len(units) == 1
    assert units[0]["unit_number"] == "200-102"


# ── Detector tests (2) ─────────────────────────────────────────────────────


def test_detector_routes_chocolateworks_to_reinhold() -> None:
    """The HTML marker (rr-unit-block + rr-pricing-toggle pair) must beat
    the generic securecafe marker so we route to the right adapter."""
    detected = detect_pms(
        "https://chocolateworks-living.com/availability/",
        page_html=_load(_CHOCO),
    )
    assert detected.pms == "reinhold", (
        f"expected reinhold; got {detected.pms} (evidence={detected.evidence})"
    )
    assert detected.confidence >= 0.9


def test_detector_does_not_misroute_plain_securecafe_site() -> None:
    """A site whose HTML only has ``.securecafe.com`` (no rr-unit-block /
    rr-pricing-toggle) must NOT route to Reinhold — Reinhold detection
    must require BOTH class markers."""
    html = '''<html><body><a href="https://foo.securecafe.com/onlineleasing/x">apply</a></body></html>'''
    detected = detect_pms("https://example.com/", page_html=html)
    assert detected.pms != "reinhold"


# ── Adapter integration test (1) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_uses_fetch_result_body_when_no_page() -> None:
    """The adapter must work from ``ctx.fetch_result.body`` alone — no
    Playwright. Mirrors the L1-only call path used in prod."""
    from types import SimpleNamespace

    from ma_poc.pms.adapters.base import AdapterContext
    from ma_poc.pms.detector import DetectedPMS

    ctx = AdapterContext(
        base_url="https://chocolateworks-living.com",
        detected=DetectedPMS(pms="reinhold", confidence=0.92, evidence=[]),
        profile=None,
        expected_total_units=None,
        property_id="cw_test",
        fetch_result=SimpleNamespace(body=_load(_CHOCO).encode("utf-8")),
    )
    adapter = ReinholdAdapter()
    # No live page — pass None. The probe_get call to /availability/ may
    # fail offline; that's fine, the fetch_result.body path takes over.
    result = await adapter.extract(None, ctx)  # type: ignore[arg-type]
    assert result.tier_used == REINHOLD_TIER
    assert len(result.units) >= 1, (
        f"adapter produced no units (errors={result.errors})"
    )
    assert result.confidence >= 0.7
