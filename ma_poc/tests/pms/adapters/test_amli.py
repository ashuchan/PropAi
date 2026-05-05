"""AMLI adapter tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import ma_poc.pms.adapters  # noqa: F401  # populates registry
from ma_poc.pms.adapters.amli import (
    AmliAdapter,
    _floorplan_arrays,
    _looks_like_amli_next_data,
    parse_amli_floor_plans,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import DetectedPMS, detect_pms

FIXTURES = Path(__file__).parent / "fixtures" / "amli"


# ── Synthetic _next/data fixtures ────────────────────────────────────────────


def _make_floorplan(
    name: str,
    property_uid: str,
    beds: int,
    sqft: int,
    units: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "floorplanName": name,
        "propertyUid": property_uid,
        "bedrooms": beds,
        "bathroomMax": 1,
        "sqftMin": sqft,
        "sqftMax": sqft,
        "priceMin": units[0]["rent"] if units else 0,
        "units": units,
    }


def _submarket_payload() -> dict[str, Any]:
    """Synthetic subregion JSON with two AMLI properties' floor plans."""
    return {
        "buildId": "synth_buildid_1",
        "pageProps": {
            "trpcState": {
                "json": {
                    "queries": [
                        # queries[0] — Prismic CMS, no floorplanName
                        {"state": {"data": [{"foo": "bar"}]}},
                        # queries[1] — also Prismic
                        {"state": {"data": {"randomKey": 1}}},
                        # queries[2] — actual floor plans (two properties)
                        {"state": {"data": [
                            _make_floorplan(
                                "A1",
                                "amli-target",
                                1,
                                720,
                                [
                                    {"unitNumber": "101", "floor": "1", "rent": 1850, "rpAvailableDate": "2026-06-01"},
                                    {"unitNumber": "207", "floor": "2", "rent": 1900, "rpAvailableDate": "2026-06-15"},
                                ],
                            ),
                            _make_floorplan(
                                "B2",
                                "amli-target",
                                2,
                                1100,
                                [{"unitNumber": "510", "floor": "5", "rent": 2700, "rpAvailableDate": "2026-07-01T00:00:00Z"}],
                            ),
                            # Different property — must be filtered out.
                            _make_floorplan(
                                "C3",
                                "amli-other-property",
                                3,
                                1500,
                                [{"unitNumber": "999", "floor": "9", "rent": 3500, "rpAvailableDate": "2026-08-01"}],
                            ),
                        ]}},
                    ],
                }
            }
        },
    }


def _amli_html(build_id: str = "synth_buildid_1") -> str:
    """Minimal AMLI homepage HTML carrying a __NEXT_DATA__ blob."""
    next_data = {
        "buildId": build_id,
        "pageProps": {
            "trpcState": {
                "json": {
                    # No floor-plan queries inline (the typical case).
                    "queries": [{"state": {"data": [{"prismic": "stuff"}]}}],
                }
            }
        },
    }
    return f"""<!doctype html>
<html><head><title>AMLI Target</title></head>
<body>
<script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script>
</body></html>"""


# ── Detector ────────────────────────────────────────────────────────────────


def test_detector_picks_amli_for_amli_com() -> None:
    for url in (
        "https://www.amli.com/apartments/austin/downtown-austin-apartments/amli-south-shore",
        "https://amli.com/apartments/chicago/west-loop-apartments/amli-west-loop",
        "https://www.amli.com/apartments/southeast-florida/dadeland-apartments/amli-joya",
    ):
        d = detect_pms(url)
        assert d.pms == "amli", f"{url} -> {d.pms}"
        assert d.confidence >= 0.95


def test_detector_amli_beats_entrata_and_sightmap_in_html() -> None:
    """AMLI HTML embeds entrata.com and sightmap.com vendor strings; the
    host fingerprint must win regardless."""
    html = "<html>" + "entrata.com sightmap.com" * 50 + "</html>"
    d = detect_pms("https://www.amli.com/apartments/x/y/z", page_html=html)
    assert d.pms == "amli"


# ── Parser ──────────────────────────────────────────────────────────────────


def test_floorplan_arrays_finds_floor_plan_query() -> None:
    payload = _submarket_payload()
    arrays = _floorplan_arrays(payload)
    assert len(arrays) == 1
    assert arrays[0][0]["floorplanName"] == "A1"


def test_parse_amli_floor_plans_filters_by_property_slug() -> None:
    payload = _submarket_payload()
    floor_plans = _floorplan_arrays(payload)[0]
    units = parse_amli_floor_plans(floor_plans, "amli-target", "https://www.amli.com/_next/data/x.json")
    # 2 + 1 = 3 units for amli-target; the C3 entry for the other property is filtered out.
    assert len(units) == 3
    fp_names = {u["floor_plan_name"] for u in units}
    assert fp_names == {"A1", "B2"}
    unit_numbers = {u["unit_number"] for u in units}
    assert unit_numbers == {"101", "207", "510"}
    # Rent + availability roundtripped
    rents = sorted(int(u["market_rent_low"]) for u in units if u.get("market_rent_low"))
    assert rents == [1850, 1900, 2700]
    # ISO timestamp is trimmed to date
    target_unit = next(u for u in units if u["unit_number"] == "510")
    assert target_unit["availability_date"] == "2026-07-01"


def test_parse_amli_floor_plans_no_filter_returns_all() -> None:
    payload = _submarket_payload()
    floor_plans = _floorplan_arrays(payload)[0]
    units = parse_amli_floor_plans(floor_plans, None, "https://x")
    # All 4 units across all properties when no filter.
    assert len(units) == 4


def test_parse_amli_floor_plans_substring_match_for_renamed_slug() -> None:
    payload = _submarket_payload()
    floor_plans = _floorplan_arrays(payload)[0]
    # Old slug "target" should still find amli-target via substring fallback.
    units = parse_amli_floor_plans(floor_plans, "target", "https://x")
    assert len(units) == 3


# ── Body shape check ────────────────────────────────────────────────────────


def test_looks_like_amli_next_data_positive() -> None:
    assert _looks_like_amli_next_data(_submarket_payload())


def test_looks_like_amli_next_data_rejects_unrelated_body() -> None:
    assert not _looks_like_amli_next_data({"foo": "bar"})
    assert not _looks_like_amli_next_data({"pageProps": {"trpcState": {}}})
    assert not _looks_like_amli_next_data(None)
    assert not _looks_like_amli_next_data([])


# ── Adapter end-to-end (with mocked Playwright page) ────────────────────────


class _StubResponse:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.ok = 200 <= status < 300
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


class _StubRequest:
    def __init__(self, response: _StubResponse) -> None:
        self._response = response
        self.last_url: str | None = None

    async def get(self, url: str, timeout: int = 0) -> _StubResponse:  # noqa: ARG002
        self.last_url = url
        return self._response


class _StubContext:
    def __init__(self, request: _StubRequest) -> None:
        self.request = request


class _StubPage:
    def __init__(self, request: _StubRequest) -> None:
        self.context = _StubContext(request)


class _StubFetchResult:
    def __init__(self, body: bytes) -> None:
        self.body = body


def _make_ctx(html: str, base_url: str) -> AdapterContext:
    return AdapterContext(
        base_url=base_url,
        detected=DetectedPMS(
            pms="amli",
            confidence=0.95,
            evidence=["test"],
            recommended_strategy="api_first",
        ),
        profile=None,
        expected_total_units=None,
        property_id="61552",
        fetch_result=_StubFetchResult(html.encode("utf-8")),
    )


@pytest.mark.asyncio
async def test_amli_adapter_fetches_submarket_json_and_extracts_units() -> None:
    """Happy path: inline blob carries no plans, adapter falls back to
    the submarket _next/data fetch and pulls 3 filtered units."""
    request = _StubRequest(_StubResponse(200, _submarket_payload()))
    page = _StubPage(request)
    ctx = _make_ctx(
        _amli_html(),
        "https://www.amli.com/apartments/austin/downtown-austin/amli-target",
    )
    result = await AmliAdapter().extract(page, ctx)

    assert result.tier_used == "TIER_1_API_AMLI_NEXT_DATA"
    assert len(result.units) == 3
    assert result.winning_url and "amli-target" not in result.winning_url
    assert request.last_url is not None
    assert "/_next/data/synth_buildid_1/en/apartments/austin/downtown-austin.json" in request.last_url


@pytest.mark.asyncio
async def test_amli_adapter_uses_inline_when_floor_plans_present() -> None:
    """If the inline __NEXT_DATA__ blob already carries floor plans for this
    property, we don't bother with the submarket fetch."""
    inline_with_plans = {
        "buildId": "synth_buildid_1",
        "pageProps": {"trpcState": {"json": {"queries": [
            {"state": {"data": [
                _make_floorplan("A1", "amli-target", 1, 700, [
                    {"unitNumber": "12", "floor": "1", "rent": 1750, "rpAvailableDate": "2026-06-01"}
                ])
            ]}},
        ]}}},
    }
    html = (
        f'<html><body><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(inline_with_plans)}</script></body></html>"
    )
    request = _StubRequest(_StubResponse(500, {}))  # would fail if called
    page = _StubPage(request)
    ctx = _make_ctx(
        html,
        "https://www.amli.com/apartments/austin/downtown-austin/amli-target",
    )
    result = await AmliAdapter().extract(page, ctx)

    assert result.tier_used == "TIER_1_API_AMLI_INLINE"
    assert len(result.units) == 1
    assert request.last_url is None  # never made a fetch call


@pytest.mark.asyncio
async def test_amli_adapter_handles_missing_build_id() -> None:
    """No buildId in __NEXT_DATA__ → adapter records error, doesn't crash."""
    html = '<html><body><script id="__NEXT_DATA__" type="application/json">{"foo":"bar"}</script></body></html>'
    request = _StubRequest(_StubResponse(200, {}))
    page = _StubPage(request)
    ctx = _make_ctx(
        html,
        "https://www.amli.com/apartments/austin/downtown-austin/amli-target",
    )
    result = await AmliAdapter().extract(page, ctx)
    assert result.units == []
    assert any("buildId" in e for e in result.errors)
    assert request.last_url is None  # short-circuited before fetch


@pytest.mark.asyncio
async def test_amli_adapter_rejects_non_amli_url() -> None:
    """Non-property URLs do not crash and produce a structured error."""
    request = _StubRequest(_StubResponse(200, _submarket_payload()))
    page = _StubPage(request)
    ctx = _make_ctx(_amli_html(), "https://www.amli.com/about")
    result = await AmliAdapter().extract(page, ctx)
    assert result.units == []
    assert any("path" in e for e in result.errors)


@pytest.mark.asyncio
async def test_amli_adapter_handles_submarket_fetch_failure() -> None:
    """5xx from _next/data does not crash; error is recorded."""
    request = _StubRequest(_StubResponse(503, {}))
    page = _StubPage(request)
    ctx = _make_ctx(
        _amli_html(),
        "https://www.amli.com/apartments/austin/downtown-austin/amli-target",
    )
    result = await AmliAdapter().extract(page, ctx)
    assert result.units == []
    assert any("503" in e for e in result.errors)


# ── Realistic-shape fixture (matches observed prod _next/data schema) ────────


def test_parse_amli_floor_plans_against_realistic_fixture() -> None:
    """Asserts the parser handles the actual production shape — including
    fields we don't currently extract (entrataPropertyId, priceMinWithFees,
    bathroomMin, propertySlug). If AMLI's _next/data schema drifts this
    fixture is the canary.
    """
    payload = json.loads((FIXTURES / "submarket_realistic.json").read_text(encoding="utf-8"))
    floor_plans_arrays = _floorplan_arrays(payload)
    # The fixture has one floor-plan-shaped queries[*] entry.
    assert len(floor_plans_arrays) == 1, "expected exactly one floor-plan query"
    floor_plans = floor_plans_arrays[0]

    units = parse_amli_floor_plans(floor_plans, "amli-south-shore", "https://x")
    # 2 + 1 + 0 = 3 units for amli-south-shore (S1 has no units, AD1 is a
    # different property and must not leak through).
    assert len(units) == 3
    fp_names = {u["floor_plan_name"] for u in units}
    assert fp_names == {"A1a", "B2"}
    # AD-101 (other property) must not be present.
    assert "AD-101" not in {u["unit_number"] for u in units}
    # Floor numbers come through as strings.
    assert {u["floor"] for u in units} == {"15", "8", "22"}
    # Rent floats are coerced to int dollars.
    rents = sorted(int(u["market_rent_low"]) for u in units if u.get("market_rent_low"))
    assert rents == [2750, 2920, 4100]
    # ISO timestamp on unit 0814 must be trimmed to date.
    by_unit = {u["unit_number"]: u for u in units}
    assert by_unit["0814"]["availability_date"] == "2026-07-02"
    # bathroomMax is preferred over bathroomMin when both present.
    assert by_unit["1515"]["bathrooms"] == "1"
    assert by_unit["2207"]["bathrooms"] == "2"


def test_parse_amli_floor_plans_realistic_other_property_filter() -> None:
    """Switching the slug filter to amli-downtown extracts only that property."""
    payload = json.loads((FIXTURES / "submarket_realistic.json").read_text(encoding="utf-8"))
    floor_plans = _floorplan_arrays(payload)[0]
    units = parse_amli_floor_plans(floor_plans, "amli-downtown", "https://x")
    assert len(units) == 1
    assert units[0]["unit_number"] == "AD-101"
    assert units[0]["floor_plan_name"] == "AD1"
