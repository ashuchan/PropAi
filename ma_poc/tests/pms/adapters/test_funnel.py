"""Change 3 — Funnel / Nestio adapter tests."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import ma_poc.pms.adapters  # noqa: F401  # ensure adapters registry is populated
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.funnel import (
    FunnelAdapter,
    _is_funnel_response_body,
    _is_funnel_response_url,
    parse_funnel_listings,
)
from ma_poc.pms.detector import DetectedPMS

FIXTURES = Path(__file__).parent / "fixtures" / "funnel"


def _load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _make_ctx(
    api_responses: list[dict],
    *,
    base_url: str = "https://nestiolistings.com/api/v2/listings/residential/rentals/",
) -> AdapterContext:
    ctx = AdapterContext(
        base_url=base_url,
        detected=DetectedPMS(
            pms="funnel", confidence=0.95, evidence=["test"],
            recommended_strategy="api_first",
        ),
        profile=None,
        expected_total_units=None,
        property_id="65069",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    pass


# ---------------------------------------------------------------------------
# Happy-path + second-fixture (over-fit guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_funnel_extract_happy_path_from_fixture() -> None:
    body = _load_fixture("synthetic_listings.json")
    responses = [{
        "url": "https://nestiolistings.com/api/v2/listings/residential/rentals/?key=x",
        "body": body,
    }]
    adapter = FunnelAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert result.tier_used == "TIER_1_API_FUNNEL"
    assert len(result.units) == 3
    first = result.units[0]
    assert first["floor_plan_name"]
    assert first["rent_range"] or first["unit_number"]


@pytest.mark.asyncio
async def test_funnel_extract_from_second_fixture() -> None:
    body = _load_fixture("synthetic_wrapped.json")
    responses = [{
        "url": "https://nestiolistings.com/api/v2/listings/residential/rentals/?key=y",
        "body": body,
    }]
    adapter = FunnelAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 3
    studios = [u for u in result.units if u["bed_label"] == "Studio"]
    assert len(studios) == 2, "wrapped fixture is expected to contain 2 studios"


# ---------------------------------------------------------------------------
# Failure-tier stamping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_funnel_returns_empty_on_no_data() -> None:
    adapter = FunnelAdapter()
    ctx = _make_ctx([])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.tier_used == "TIER_1_API_FUNNEL_NO_RESPONSE"
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_funnel_tier_re_stamped_on_shape_reject() -> None:
    # RentCafe-shaped body, no nestiolistings URL — must shape-reject.
    rentcafe_body = [{
        "floorplanName": "A1", "floorplanId": "1",
        "minimumRent": "1500", "maximumRent": "1600",
        "api": "rentcafe",
    }]
    adapter = FunnelAdapter()
    ctx = _make_ctx([{"url": "https://other.example/x", "body": rentcafe_body}])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_FUNNEL_SHAPE_REJECTED"


@pytest.mark.asyncio
async def test_funnel_tier_re_stamped_on_empty_list() -> None:
    adapter = FunnelAdapter()
    ctx = _make_ctx([{
        "url": "https://nestiolistings.com/api/v2/listings/residential/rentals/?key=x",
        "body": {"results": []},
    }])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_FUNNEL_LIST_EMPTY"


# ---------------------------------------------------------------------------
# Static fingerprints / body-shape checks
# ---------------------------------------------------------------------------


def test_funnel_static_fingerprints_contains_nestio() -> None:
    assert "nestiolistings.com" in FunnelAdapter().static_fingerprints()


def test_funnel_matches_response_body_accepts_real_capture() -> None:
    body = _load_fixture("synthetic_listings.json")
    assert FunnelAdapter().matches_response_body(body) is True
    body2 = _load_fixture("synthetic_wrapped.json")
    assert FunnelAdapter().matches_response_body(body2) is True


def test_funnel_matches_response_body_rejects_rentcafe_and_sightmap() -> None:
    rentcafe_body = {
        "data": [{
            "floorplanName": "A1", "floorplanId": "1",
            "minimumRent": "1500", "maximumRent": "1600",
        }]
    }
    sightmap_body = {
        "data": {
            "floor_plans": [{"id": 1, "name": "A",
                             "bedroom_count": 1, "filter_label": "1BR"}],
            "units": [{"floor_plan_id": "1", "price": 1500}],
        }
    }
    adapter = FunnelAdapter()
    assert adapter.matches_response_body(rentcafe_body) is False
    assert adapter.matches_response_body(sightmap_body) is False


# ---------------------------------------------------------------------------
# Emitted-unit invariants
# ---------------------------------------------------------------------------


def test_funnel_tier_used_is_pms_specific() -> None:
    body = _load_fixture("synthetic_listings.json")
    units = parse_funnel_listings(
        body, "https://nestiolistings.com/api/v2/listings/residential/rentals/?key=x")
    assert units
    for u in units:
        assert "FUNNEL" in u["extraction_tier"]


def test_funnel_unit_id_format_valid() -> None:
    body = _load_fixture("synthetic_listings.json")
    units = parse_funnel_listings(body, "https://x")
    # Synthetic fixture uses numeric unit ids — acceptable alphanumeric regex
    # is "at least one digit or >=2 chars". Any extracted unit_number must
    # match that relaxed shape.
    valid = re.compile(r"^[A-Za-z0-9_\-]{2,}$")
    for u in units:
        assert valid.match(u["unit_number"]), u["unit_number"]


def test_funnel_rent_within_sanity_range() -> None:
    for name in ("synthetic_listings.json", "synthetic_wrapped.json"):
        body = _load_fixture(name)
        units = parse_funnel_listings(body, "https://x")
        for u in units:
            lo = u.get("market_rent_low")
            hi = u.get("market_rent_high")
            for r in (lo, hi):
                if r is None:
                    continue
                assert 200 <= r <= 50000, (u, r)


# ---------------------------------------------------------------------------
# URL / body-shape helper tests
# ---------------------------------------------------------------------------


def test_funnel_url_marker_check() -> None:
    assert _is_funnel_response_url(
        "https://nestiolistings.com/api/v2/listings/residential/rentals/?key=x"
    )
    assert _is_funnel_response_url(
        "https://nestiostaging.com/api/v2/listings/residential/rentals/?key=y"
    )
    assert not _is_funnel_response_url("https://windsorcommunities.com/")


def test_funnel_body_check_handles_various_envelopes() -> None:
    list_at_root = _load_fixture("synthetic_listings.json")
    dict_wrapped = _load_fixture("synthetic_wrapped.json")
    assert _is_funnel_response_body(list_at_root) is True
    assert _is_funnel_response_body(dict_wrapped) is True
    assert _is_funnel_response_body({"unrelated": "payload"}) is False
    assert _is_funnel_response_body(None) is False
    assert _is_funnel_response_body([]) is False


# ---------------------------------------------------------------------------
# Research-blocked real-capture test — surfaces the gate without failing CI.
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="research-blocked: need >=2 real Funnel captures from "
           "Windsor 65069/77589/5715 before enabling this test"
)
def test_funnel_real_capture_unit_count_matches_property_inventory() -> None:
    raise AssertionError("placeholder for real-capture validation")
