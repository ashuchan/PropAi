"""Phase 3 — Entrata adapter tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.entrata import (
    EntrataAdapter,
    _ENTRATA_AVAIL_DATE_ALIASES,
    _iso_date,
    _pp_iso,
    find_entrata_fp_detail_links,
    parse_entrata_available_units,
    parse_entrata_floorplans,
    parse_entrata_widget_envelope,
    parse_prospectportal_unit_spaces,
)
from ma_poc.pms.detector import detect_pms

FIXTURES = Path(__file__).parent / "fixtures" / "entrata"


def _load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _make_ctx(api_responses: list[dict]) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://www.hackneyhouseapartments.com/",
        detected=detect_pms("https://www.hackneyhouseapartments.com/"),
        profile=None,
        expected_total_units=None,
        property_id="257356",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    """Minimal mock for Playwright Page."""

    pass


@pytest.mark.asyncio
async def test_entrata_extract_happy_path() -> None:
    """Real Entrata payload (257356) produces units with rent and floor plan name."""
    responses = _load_fixture("257356.json")
    adapter = EntrataAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) >= 10
    first = result.units[0]
    assert first["floor_plan_name"]
    assert first["rent_range"]
    assert "ENTRATA" in first["extraction_tier"]


@pytest.mark.asyncio
async def test_entrata_extract_from_stored_fixture() -> None:
    """All stored fixtures load and produce units."""
    for fixture_path in FIXTURES.glob("*.json"):
        responses = json.loads(fixture_path.read_text(encoding="utf-8"))
        adapter = EntrataAdapter()
        ctx = _make_ctx(responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert isinstance(result, AdapterResult)
        # 252511 has no floorplan data (only availability widget + ppConfig)
        if "257356" in fixture_path.name:
            assert len(result.units) > 0


@pytest.mark.asyncio
async def test_entrata_extract_returns_empty_list_on_no_data() -> None:
    """Noise-only responses produce empty units, not an exception."""
    responses = [
        {"url": "https://example.com/Apartments/module/widgets/", "body": {"widget_name": "directions"}}
    ]
    adapter = EntrataAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


def test_parse_entrata_floorplans_basic() -> None:
    """Parse a minimal Entrata floorplan list."""
    items = [
        {
            "id": 100,
            "floorplan-name": "A1",
            "no_of_bedroom": 1,
            "no_of_bathroom": 1,
            "square_footage": 750,
            "min_rent": "$1,500",
            "max_rent": "$1,800",
        },
    ]
    units = parse_entrata_floorplans(items, "https://test.com/widgets/")
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "A1"
    assert units[0]["bedrooms"] == "1"
    assert "$1,500" in units[0]["rent_range"]


def test_parse_entrata_widget_envelope() -> None:
    """Parse from widget_data.content.floor_plans envelope."""
    body = {
        "widget_name": "floor_plans",
        "widget_data": {
            "content": {
                "floor_plans": {
                    "floor_plans": [
                        {
                            "id": 1,
                            "floorplan-name": "B2",
                            "no_of_bedroom": 2,
                            "no_of_bathroom": 2,
                            "square_footage": 1000,
                            "min_rent": "$2,000",
                            "max_rent": "$2,500",
                        },
                    ]
                }
            }
        },
    }
    units = parse_entrata_widget_envelope(body, "https://test.com/widgets/")
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "B2"


def test_static_fingerprints_nonempty() -> None:
    adapter = EntrataAdapter()
    fps = adapter.static_fingerprints()
    assert len(fps) >= 1
    assert "entrata.com" in fps


def test_tier_used_label_is_pms_specific() -> None:
    items = [
        {
            "id": 1,
            "floorplan-name": "X",
            "no_of_bedroom": 1,
            "no_of_bathroom": 1,
            "square_footage": 500,
            "min_rent": "$1,000",
            "max_rent": "$1,000",
        }
    ]
    units = parse_entrata_floorplans(items, "test")
    assert "ENTRATA" in units[0]["extraction_tier"]


def test_rent_within_sanity_range() -> None:
    """All emitted rents from real fixture are in sanity range."""
    responses = _load_fixture("257356.json")
    for resp in responses:
        body = resp.get("body")
        if isinstance(body, list) and body and isinstance(body[0], dict):
            units = parse_entrata_floorplans(body, "test")
            for u in units:
                if u["rent_range"]:
                    # Extract numeric rent from range
                    import re

                    nums = re.findall(r"\d[\d,]*", u["rent_range"])
                    for n in nums:
                        val = int(n.replace(",", ""))
                        assert 200 <= val <= 50000, f"Rent {val} out of range"


# ── Bug 9 (2026-05-09) — direct-endpoint probe ──────────────────────────────


class _ProbingPage:
    """Stub Page that returns a scripted payload from the first probe URL it
    sees. Used to exercise EntrataAdapter._probe_known_endpoints without a
    real browser session."""

    def __init__(self, url: str, payload_by_path: dict[str, object] | None) -> None:
        self.url = url
        self._payloads = payload_by_path or {}
        self.calls: list[str] = []

    async def evaluate(self, _script: str, target_url: str) -> object | None:
        self.calls.append(target_url)
        # Find a path key whose suffix matches the requested URL.
        for path, payload in self._payloads.items():
            if target_url.endswith(path):
                return payload
        return None


@pytest.mark.asyncio
async def test_bug9_probe_returns_units_when_floorplans_endpoint_responds() -> None:
    """Bug 9: direct probe of /Apartments/module/floor_plans/ wins when the
    captured-API path returned nothing."""
    page = _ProbingPage(
        url="https://www.livethearch.com/",
        payload_by_path={
            "/Apartments/module/floor_plans/": [
                {
                    "id": 1,
                    "floorplan-name": "1BR-A",
                    "no_of_bedroom": 1,
                    "no_of_bathroom": 1,
                    "square_footage": "650",
                    "min_rent": "$1,500",
                    "max_rent": "$1,500",
                },
                {
                    "id": 2,
                    "floorplan-name": "2BR-B",
                    "no_of_bedroom": 2,
                    "no_of_bathroom": 2,
                    "square_footage": "950",
                    "min_rent": "$2,100",
                    "max_rent": "$2,300",
                },
            ]
        },
    )
    ctx = AdapterContext(
        base_url="https://www.livethearch.com/",
        detected=detect_pms("https://www.livethearch.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_BUG9",
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    result = await EntrataAdapter().extract(page, ctx)  # type: ignore[arg-type]
    assert len(result.units) == 2
    assert result.tier_used == "TIER_1_API_ENTRATA_PROBE"
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_bug9_probe_skips_paths_until_data_returned() -> None:
    """Bug 9: probes try each path until one returns useful data, then stop."""
    page = _ProbingPage(
        url="https://www.livethearch.com/",
        # First two probe paths return None; floor_plans is later in the
        # catalogue but we map only that one to a payload.
        payload_by_path={
            "/api/floorplans": [
                {
                    "id": 7,
                    "floorplan-name": "Studio",
                    "no_of_bedroom": 0,
                    "no_of_bathroom": 1,
                    "square_footage": "450",
                    "min_rent": "$1,100",
                    "max_rent": "$1,100",
                }
            ]
        },
    )
    ctx = AdapterContext(
        base_url="https://www.livethearch.com/",
        detected=detect_pms("https://www.livethearch.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_BUG9_B",
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    result = await EntrataAdapter().extract(page, ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1
    # The probe loop tried each catalogue path in order until /api/floorplans hit.
    assert any(call.endswith("/api/floorplans") for call in page.calls)


@pytest.mark.asyncio
async def test_bug9_probe_handles_evaluate_exception() -> None:
    """Bug 9: a probe that raises must not abort the whole extract — the
    adapter falls through to the empty-result path cleanly."""

    class _RaisingPage:
        url = "https://www.livethearch.com/"

        async def evaluate(self, _script: str, _url: str) -> None:
            raise RuntimeError("boom")

    ctx = AdapterContext(
        base_url="https://www.livethearch.com/",
        detected=detect_pms("https://www.livethearch.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_BUG9_C",
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    result = await EntrataAdapter().extract(_RaisingPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_bug9_probe_skipped_when_captured_api_already_has_units() -> None:
    """Bug 9: when the captured-API path already produced units, the probe
    must NOT run (return early). Otherwise we'd double-fire the LLM cost
    on every successful Entrata property."""
    captured = [
        {
            "url": "https://www.livethearch.com/Apartments/module/widgets/",
            "body": [
                {
                    "id": 99,
                    "floorplan-name": "From Capture",
                    "no_of_bedroom": 1,
                    "no_of_bathroom": 1,
                    "square_footage": "700",
                    "min_rent": "$1,800",
                    "max_rent": "$1,800",
                }
            ],
        }
    ]
    page = _ProbingPage(
        url="https://www.livethearch.com/",
        payload_by_path={
            "/Apartments/module/floor_plans/": [
                {
                    "id": 100,
                    "floorplan-name": "FROM PROBE",
                    "no_of_bedroom": 2,
                    "no_of_bathroom": 2,
                    "square_footage": "1100",
                    "min_rent": "$2,500",
                    "max_rent": "$2,500",
                }
            ]
        },
    )
    ctx = AdapterContext(
        base_url="https://www.livethearch.com/",
        detected=detect_pms("https://www.livethearch.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_BUG9_D",
    )
    ctx._api_responses = captured  # type: ignore[attr-defined]
    result = await EntrataAdapter().extract(page, ctx)  # type: ignore[arg-type]
    # Captured units win; probe never ran.
    assert len(result.units) == 1
    assert result.units[0]["floor_plan_name"] == "From Capture"
    assert page.calls == []
    assert result.tier_used == "TIER_1_API_ENTRATA"


# ────────────────────────────────────────────────────────────────────
# 2026-05-13 port (Commit 5 of MAY13_API_TIER_PORT_PLAN.md):
# - 7-alias available_date lookup
# - parse_entrata_available_units (WordPress + ProspectPortal apply)
# - parse_prospectportal_unit_spaces (view_unit_spaces HTML fragment)
# - find_entrata_fp_detail_links, _iso_date, _pp_iso
# ────────────────────────────────────────────────────────────────────


class TestEntrataAvailDateAliases:
    """parse_entrata_floorplans must read move-in date from any of 7
    producer aliases. Pre-port fleet was 0% available_date capture on
    TIER_1_API_ENTRATA (memory note 2026-05-19)."""

    def test_alias_move_in_date(self):
        out = parse_entrata_floorplans(
            [{"floorplan-name": "A1", "no_of_bedroom": 1, "no_of_bathroom": 1,
              "min_rent": "$1,500", "max_rent": "$1,500", "id": 1,
              "move_in_date": "2026-06-01"}],
            "https://e.com/api",
        )
        assert out[0]["availability_date"] == "2026-06-01"
        # Dual emission from make_unit_dict (Commit 4) makes both keys present.
        assert out[0]["available_date"] == "2026-06-01"

    def test_alias_min_move_in_date(self):
        out = parse_entrata_floorplans(
            [{"floorplan-name": "A1", "no_of_bedroom": 1, "no_of_bathroom": 1,
              "min_rent": "$1,500", "max_rent": "$1,500", "id": 1,
              "min_move_in_date": "2026-06-02"}],
            "https://e.com/api",
        )
        assert out[0]["availability_date"] == "2026-06-02"

    def test_alias_available_on(self):
        out = parse_entrata_floorplans(
            [{"floorplan-name": "A1", "no_of_bedroom": 1, "no_of_bathroom": 1,
              "min_rent": "$1,500", "max_rent": "$1,500", "id": 1,
              "available_on": "2026-06-03"}],
            "https://e.com/api",
        )
        assert out[0]["availability_date"] == "2026-06-03"

    def test_no_date_alias_yields_empty(self):
        out = parse_entrata_floorplans(
            [{"floorplan-name": "A1", "no_of_bedroom": 1, "no_of_bathroom": 1,
              "min_rent": "$1,500", "max_rent": "$1,500", "id": 1}],
            "https://e.com/api",
        )
        assert out[0]["availability_date"] == ""

    def test_alias_priority_first_non_empty_wins(self):
        # Earlier alias in the tuple wins over later ones when both present.
        out = parse_entrata_floorplans(
            [{"floorplan-name": "A1", "no_of_bedroom": 1, "no_of_bathroom": 1,
              "min_rent": "$1,500", "max_rent": "$1,500", "id": 1,
              "available_date": "2026-06-04",  # first in tuple
              "move_in_date": "2026-12-01"}],  # later -- ignored
            "https://e.com/api",
        )
        assert out[0]["availability_date"] == "2026-06-04"

    def test_all_7_aliases_documented_in_tuple(self):
        """Regression guard: anyone shrinking the alias list will hit
        this. The 7-alias set is the one verified against producer
        captures (memory project_run_2026_05_19)."""
        expected = {
            "available_date", "availableDate", "availability_date",
            "move_in_date", "min_move_in_date", "date_available",
            "available_on", "first_available_date",
        }
        assert set(_ENTRATA_AVAIL_DATE_ALIASES) >= expected


class TestEntrataAvailableUnitsWP:
    """parse_entrata_available_units extracts unit-level data from the
    WordPress-mounted Entrata pattern where the page server-renders an
    HTML-entity-encoded JSON blob inside the page."""

    def test_parses_canonical_blob(self):
        html = (
            'header text ... "available_units":'
            '[{"id":"5001","name":"206","available_on":"6/15/2026",'
            '"price":"$1,725","deposit":"$200","apply_url":"/apply"}]'
            ' ... footer'
        )
        out = parse_entrata_available_units(
            html, "https://e.com/floorplan/1br-1ba-pennsylvania/"
        )
        assert len(out) == 1
        u = out[0]
        assert u["unit_number"] == "206"
        assert u["market_rent_low"] == 1725
        assert u["market_rent_high"] == 1725
        assert u["availability_date"] == "2026-06-15"
        assert u["bedrooms"] == "1"
        assert u["bathrooms"] == "1"
        assert u["extraction_tier"] == "TIER_1_DOM_ENTRATA_WP"

    def test_html_entity_encoded_blob_decodes(self):
        # Producer-emitted form: &quot;-escaped JSON inside HTML.
        html = (
            '&quot;available_units&quot;:'
            '[{&quot;id&quot;:&quot;1&quot;,&quot;name&quot;:&quot;A1&quot;,'
            '&quot;available_on&quot;:&quot;7/1/2026&quot;,'
            '&quot;price&quot;:&quot;$1,800&quot;}]'
        )
        out = parse_entrata_available_units(html, "https://e.com/floorplan/2br-2ba-east/")
        assert len(out) == 1
        assert out[0]["availability_date"] == "2026-07-01"

    def test_no_available_units_returns_empty(self):
        assert parse_entrata_available_units("<html>no data</html>", "https://e.com/x") == []
        assert parse_entrata_available_units("", "https://e.com/x") == []

    def test_dedupes_within_blob(self):
        html = (
            '"available_units":'
            '[{"id":"1","name":"101","available_on":"6/1/2026","price":"$1,500"},'
            '{"id":"1","name":"101","available_on":"6/1/2026","price":"$1,500"}]'
        )
        out = parse_entrata_available_units(html, "https://e.com/floorplan/1br-1ba/")
        assert len(out) == 1

    def test_malformed_json_does_not_raise(self):
        # Truncated payload -- _bracket_json returns None and we move on.
        html = '"available_units":[{"id":"1","name":"101"'
        assert parse_entrata_available_units(html, "https://e.com/x") == []


class TestFindEntrataFpDetailLinks:
    def test_extracts_absolute_links(self):
        html = (
            '<a href="/floorplan/1br-1ba-pa">x</a>'
            '<a href="/floorplan/2br-2ba-pa/">y</a>'
            '<a href="/unrelated/page">z</a>'
        )
        out = find_entrata_fp_detail_links(html, "https://e.com")
        assert out == [
            "https://e.com/floorplan/1br-1ba-pa/",
            "https://e.com/floorplan/2br-2ba-pa/",
        ]

    def test_dedupes(self):
        html = (
            '<a href="/floorplan/x/">a</a>'
            '<a href="/floorplan/x/">b</a>'
        )
        out = find_entrata_fp_detail_links(html, "https://e.com")
        assert out == ["https://e.com/floorplan/x/"]

    def test_empty_inputs_return_empty(self):
        assert find_entrata_fp_detail_links("", "https://e.com") == []
        assert find_entrata_fp_detail_links("<html></html>", "") == []


class TestIsoDateHelpers:
    def test_iso_date_mmddyyyy(self):
        assert _iso_date("5/22/2026") == "2026-05-22"
        assert _iso_date("12/3/2026") == "2026-12-03"

    def test_iso_date_passthrough_for_unrecognized(self):
        assert _iso_date("") == ""
        assert _iso_date("June 1, 2026") == ""
        assert _iso_date(None) == ""

    def test_pp_iso_slash_form(self):
        assert _pp_iso("2026/05/17") == "2026-05-17"

    def test_pp_iso_hyphen_form(self):
        assert _pp_iso("2026-5-17") == "2026-05-17"

    def test_pp_iso_empty(self):
        assert _pp_iso("") == ""
        assert _pp_iso(None) == ""


class TestProspectPortalUnitSpaces:
    """parse_prospectportal_unit_spaces parses the
    ?module=check_availability&action=view_unit_spaces HTML fragment.
    Live-verified 2026-05-18 on springriver.prospectportal.com (3 units)."""

    def test_parses_unit_button_row(self):
        html = """
        <h6 class="availability-fp-name">The Adams</h6>
        <li class="fp-stats-item modal-sq-feet"><span class="stat-value">642</span></li>
        <div class="unit-row-wrapper">
          <a class="unit-button"
             data-unit="UNIT-1306"
             data-rent="1291"
             data-bedroom="1"
             data-bathroom="1"
             data-unitavailabilitydate="2026-05-17">Apply</a>
          <div class="unit-col unit"><span class="unit-col-text">1306</span></div>
        </div>
        """
        out = parse_prospectportal_unit_spaces(html, "https://x.prospectportal.com/")
        assert len(out) == 1
        u = out[0]
        assert u["unit_number"] == "1306"
        assert u["market_rent_low"] == 1291
        assert u["availability_date"] == "2026-05-17"
        assert u["bedrooms"] == "1"
        assert u["bathrooms"] == "1"
        assert u["floor_plan_name"] == "The Adams"
        assert u["extraction_tier"] == "TIER_1_DOM_ENTRATA_PROSPECTPORTAL"

    def test_no_unit_button_returns_empty(self):
        assert parse_prospectportal_unit_spaces(
            "<html><p>no portal here</p></html>", "https://x.prospectportal.com/"
        ) == []
        assert parse_prospectportal_unit_spaces("", "https://x.prospectportal.com/") == []

    def test_dedupes_by_unit_number(self):
        html = """
        <div class="unit-row">
          <a class="unit-button" data-unit="U1" data-rent="1000"
             data-bedroom="1" data-bathroom="1"></a>
          <div class="unit-col unit"><span class="unit-col-text">101</span></div>
        </div>
        <div class="unit-row">
          <a class="unit-button" data-unit="U2" data-rent="1100"
             data-bedroom="1" data-bathroom="1"></a>
          <div class="unit-col unit"><span class="unit-col-text">101</span></div>
        </div>
        """
        out = parse_prospectportal_unit_spaces(html, "https://x.prospectportal.com/")
        # Two rows with same unit_number=101 -> deduped to 1.
        assert len(out) == 1


# ── Sitemap URL selection — post-canary fix 2026-05-24 ──────────────────────


class TestSitemapUrlSelection:
    """End-to-end behaviour tests for ``_probe_sitemap_conventional`` URL
    selection. Modera-class sitemaps (PIDs 257570, 262539) shipped two
    bugs that caused the canary to pick a SPA wrapper over the canonical
    inventory page:

    1. Per-floorplan-detail URLs ending in ``/conventional/`` slipped
       past the terminal-keyword guard.
    2. Sort-by-length picked ``/floor-plans`` (29 chars, SPA shell) over
       the proper ``/{area}/{property}/conventional/`` URL (76 chars).

    These tests reproduce the modera sitemap shape and pin the fix.
    """

    @pytest.mark.asyncio
    async def test_picks_conventional_over_floor_plans_wrapper(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Modera-style sitemap with both the canonical
        ``/{city}/{slug}/conventional/`` AND a short ``/floor-plans`` SPA
        wrapper. The fix must pick the canonical URL."""
        from dataclasses import dataclass, field as _field
        from ma_poc.pms.adapters import entrata as _e

        sitemap = """<?xml version="1.0"?><urlset>
  <url><loc>https://example.com/area/property/conventional/</loc></url>
  <url><loc>https://example.com/floor-plans</loc></url>
</urlset>"""
        fetched_urls: list[str] = []

        async def _fake_fetch(url: str, *, ctx=None, stage=None) -> str:
            fetched_urls.append(url)
            if url.endswith("/sitemap.xml"):
                return sitemap
            return ""

        monkeypatch.setattr(_e, "_entrata_static_fetch", _fake_fetch)

        @dataclass
        class _Ctx:
            base_url: str = "https://example.com/"
            property_id: str = "test-1"
            fetch_result: object = None
            _api_responses: list = _field(default_factory=list)

        adapter = EntrataAdapter()
        await adapter._probe_sitemap_conventional(None, _Ctx())  # type: ignore[arg-type]

        assert fetched_urls[0].endswith("/sitemap.xml")
        assert len(fetched_urls) >= 2, (
            "Adapter should have followed sitemap to fetch the canonical URL"
        )
        assert "/conventional" in fetched_urls[1], (
            f"Expected the canonical /conventional/ URL to be picked, got "
            f"{fetched_urls[1]!r}. Sort-by-length leak: SPA wrapper won."
        )
        assert "/floor-plans" not in fetched_urls[1], (
            f"SPA wrapper /floor-plans was selected over canonical "
            f"inventory page: {fetched_urls[1]!r}"
        )

    @pytest.mark.asyncio
    async def test_rejects_per_floorplan_detail_urls(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The Modera-style sitemap also carries ~40 per-fp detail URLs
        of shape ``.../floorplans/{slug}-{id}/fp_name/occupancy_type/
        conventional/``. Each ends in ``/conventional/`` so the terminal
        guard alone admits them. The ``/fp_name/`` + ``/occupancy_type/``
        interior-segment filter must reject them."""
        from dataclasses import dataclass, field as _field
        from ma_poc.pms.adapters import entrata as _e

        sitemap = """<?xml version="1.0"?><urlset>
  <url><loc>https://example.com/area/property/conventional/</loc></url>
  <url><loc>https://example.com/area/property/floorplans/a01-1234/fp_name/occupancy_type/conventional/</loc></url>
  <url><loc>https://example.com/area/property/floorplans/b14-5678/fp_name/occupancy_type/conventional/</loc></url>
  <url><loc>https://example.com/area/property/floorplans/c02-9999/fp_name/occupancy_type/conventional/</loc></url>
</urlset>"""
        fetched_urls: list[str] = []

        async def _fake_fetch(url: str, *, ctx=None, stage=None) -> str:
            fetched_urls.append(url)
            if url.endswith("/sitemap.xml"):
                return sitemap
            return ""

        monkeypatch.setattr(_e, "_entrata_static_fetch", _fake_fetch)

        @dataclass
        class _Ctx:
            base_url: str = "https://example.com/"
            property_id: str = "test-2"
            fetch_result: object = None
            _api_responses: list = _field(default_factory=list)

        adapter = EntrataAdapter()
        await adapter._probe_sitemap_conventional(None, _Ctx())  # type: ignore[arg-type]

        assert len(fetched_urls) >= 2
        chosen = fetched_urls[1]
        assert "/fp_name/" not in chosen, (
            f"Per-fp detail URL was selected: {chosen!r}"
        )
        assert "/occupancy_type/" not in chosen, (
            f"Per-fp detail URL was selected: {chosen!r}"
        )
        assert chosen.endswith("/conventional/"), (
            f"Expected the bare canonical URL, got {chosen!r}"
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_floor_plans_when_no_conventional(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the sitemap has NO ``/conventional/`` URL (non-Entrata-CMS
        sites widened in by R5), the bare ``/floor-plans`` index is the
        best signal we have. Verify the fallback still picks it."""
        from dataclasses import dataclass, field as _field
        from ma_poc.pms.adapters import entrata as _e

        sitemap = """<?xml version="1.0"?><urlset>
  <url><loc>https://example.com/floor-plans/</loc></url>
  <url><loc>https://example.com/about/</loc></url>
</urlset>"""
        fetched_urls: list[str] = []

        async def _fake_fetch(url: str, *, ctx=None, stage=None) -> str:
            fetched_urls.append(url)
            if url.endswith("/sitemap.xml"):
                return sitemap
            return ""

        monkeypatch.setattr(_e, "_entrata_static_fetch", _fake_fetch)

        @dataclass
        class _Ctx:
            base_url: str = "https://example.com/"
            property_id: str = "test-3"
            fetch_result: object = None
            _api_responses: list = _field(default_factory=list)

        adapter = EntrataAdapter()
        await adapter._probe_sitemap_conventional(None, _Ctx())  # type: ignore[arg-type]

        assert len(fetched_urls) >= 2
        assert "/floor-plans" in fetched_urls[1], (
            f"Fallback to /floor-plans broken: {fetched_urls[1]!r}"
        )
