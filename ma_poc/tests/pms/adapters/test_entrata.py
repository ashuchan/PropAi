"""Phase 3 — Entrata adapter tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.entrata import (
    EntrataAdapter,
    parse_entrata_floorplans,
    parse_entrata_widget_envelope,
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
