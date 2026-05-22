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
            pms="funnel",
            confidence=0.95,
            evidence=["test"],
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
    responses = [
        {
            "url": "https://nestiolistings.com/api/v2/listings/residential/rentals/?key=x",
            "body": body,
        }
    ]
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
    responses = [
        {
            "url": "https://nestiolistings.com/api/v2/listings/residential/rentals/?key=y",
            "body": body,
        }
    ]
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
    rentcafe_body = [
        {
            "floorplanName": "A1",
            "floorplanId": "1",
            "minimumRent": "1500",
            "maximumRent": "1600",
            "api": "rentcafe",
        }
    ]
    adapter = FunnelAdapter()
    ctx = _make_ctx([{"url": "https://other.example/x", "body": rentcafe_body}])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_FUNNEL_SHAPE_REJECTED"


@pytest.mark.asyncio
async def test_funnel_tier_re_stamped_on_empty_list() -> None:
    adapter = FunnelAdapter()
    ctx = _make_ctx(
        [
            {
                "url": "https://nestiolistings.com/api/v2/listings/residential/rentals/?key=x",
                "body": {"results": []},
            }
        ]
    )
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
        "data": [
            {
                "floorplanName": "A1",
                "floorplanId": "1",
                "minimumRent": "1500",
                "maximumRent": "1600",
            }
        ]
    }
    sightmap_body = {
        "data": {
            "floor_plans": [{"id": 1, "name": "A", "bedroom_count": 1, "filter_label": "1BR"}],
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
        body, "https://nestiolistings.com/api/v2/listings/residential/rentals/?key=x"
    )
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
    assert _is_funnel_response_url("https://nestiolistings.com/api/v2/listings/residential/rentals/?key=x")
    assert _is_funnel_response_url("https://nestiostaging.com/api/v2/listings/residential/rentals/?key=y")
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


# ---------------------------------------------------------------------------
# 2026-05-22 — v2/listings/all endpoint (Dermot / 220 East 72nd canonical)
# ---------------------------------------------------------------------------
# A different Nestio endpoint, served from the same host. Snake-case field
# names (price / date_available / layout / unit_number / square_footage) and
# an ``items`` envelope wrapper. Until 2026-05-22 the adapter shape-rejected
# this body and the property fell through to an LLM_API call. PID 262799
# burned $0.0065 to extract data the adapter can now read deterministically.


@pytest.mark.asyncio
async def test_funnel_extract_v2_listings_all_emits_two_units() -> None:
    body = _load_fixture("synthetic_listings_all.json")
    responses = [
        {
            "url": "https://nestiolistings.com/api/v2/listings/all?key=x&property=3152",
            "body": body,
        }
    ]
    adapter = FunnelAdapter()
    ctx = _make_ctx(
        responses,
        base_url="https://nestiolistings.com/api/v2/listings/all",
    )
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_FUNNEL"
    # Studio + 2-bed = 2 units, mirroring PID 262799 live capture.
    assert len(result.units) == 2


def test_funnel_v2_listings_all_body_shape_accepted() -> None:
    body = _load_fixture("synthetic_listings_all.json")
    assert _is_funnel_response_body(body) is True


def test_funnel_v2_listings_all_url_accepted() -> None:
    # The /api/ marker matches both the documented residential rentals
    # endpoint and the v2 listings/all variant.
    assert _is_funnel_response_url(
        "https://nestiolistings.com/api/v2/listings/all?key=x&property=3152"
    )


def test_funnel_v2_listings_all_uses_layout_as_floor_plan_name() -> None:
    body = _load_fixture("synthetic_listings_all.json")
    units = parse_funnel_listings(body, "https://nestiolistings.com/api/v2/listings/all")
    names = sorted(u["floor_plan_name"] for u in units)
    assert names == ["2 Bedroom", "Studio"]


def test_funnel_v2_listings_all_uses_unit_number_directly() -> None:
    body = _load_fixture("synthetic_listings_all.json")
    units = parse_funnel_listings(body, "https://x")
    unit_nums = sorted(u["unit_number"] for u in units)
    # Live capture unit_number values: "9E1-1" (Studio), "7G" (2 Bedroom).
    assert unit_nums == ["7G", "9E1-1"]


def test_funnel_v2_listings_all_picks_price_when_no_min_max_pair() -> None:
    body = _load_fixture("synthetic_listings_all.json")
    units = parse_funnel_listings(body, "https://x")
    # Studio fixture: price="5395.00" → rent_low == rent_high == 5395.
    studio = next(u for u in units if u["floor_plan_name"] == "Studio")
    assert studio["market_rent_low"] == 5395
    assert studio["market_rent_high"] == 5395


def test_funnel_v2_listings_all_preserves_half_bathrooms() -> None:
    body = _load_fixture("synthetic_listings_all.json")
    units = parse_funnel_listings(body, "https://x")
    two_br = next(u for u in units if u["floor_plan_name"] == "2 Bedroom")
    # bathrooms=1.5 must survive — the documented adapter coerced via
    # int(float(...)) and shipped "1", silently losing the half-bath.
    assert two_br["bathrooms"] == "1.5"


def test_funnel_v2_listings_all_picks_building_name_from_nested_dict() -> None:
    body = _load_fixture("synthetic_listings_all.json")
    units = parse_funnel_listings(body, "https://x")
    # ``building`` is a nested dict; we extract .name rather than stringify
    # the whole dict.
    for u in units:
        assert u["building"] == "220 East 72nd Street"


def test_funnel_v2_listings_all_status_mapping() -> None:
    body = _load_fixture("synthetic_listings_all.json")
    units = parse_funnel_listings(body, "https://x")
    # Live capture status == "Available" on both rows → AVAILABLE.
    for u in units:
        assert u["availability_status"] == "AVAILABLE"


def test_funnel_v2_listings_all_threads_lease_term() -> None:
    body = _load_fixture("synthetic_listings_all.json")
    units = parse_funnel_listings(body, "https://x")
    # min_lease_term == 12 on both rows.
    for u in units:
        assert u["lease_term"] == "12"


def test_funnel_v2_listings_all_concession_when_incentives_empty() -> None:
    body = _load_fixture("synthetic_listings_all.json")
    units = parse_funnel_listings(body, "https://x")
    # Both rows in the live capture have incentives="" — no concession.
    for u in units:
        assert u.get("concession") in (None, "")


def test_funnel_v2_listings_all_concession_string_picked_up() -> None:
    body = {
        "items": [
            {
                "unit_number": "1A",
                "layout": "1 Bedroom",
                "bedrooms": 1,
                "bathrooms": 1.0,
                "price": "3000.00",
                "square_footage": 700,
                "date_available": "2026-06-01",
                "status": "Available",
                "min_lease_term": 12,
                "incentives": "1 month free on 18-month lease",
            }
        ]
    }
    units = parse_funnel_listings(body, "https://nestiolistings.com/api/")
    assert units[0]["concession"] == "1 month free on 18-month lease"


def test_funnel_v2_listings_all_concession_list_picks_first_text() -> None:
    body = {
        "items": [
            {
                "unit_number": "1A",
                "layout": "1 Bedroom",
                "bedrooms": 1,
                "bathrooms": 1.0,
                "price": "3000.00",
                "square_footage": 700,
                "date_available": "2026-06-01",
                "status": "Available",
                "min_lease_term": 12,
                "incentives": [
                    {"description": "1 month free", "expires_at": "2026-12-31"},
                    {"description": "fallback only"},
                ],
            }
        ]
    }
    units = parse_funnel_listings(body, "https://nestiolistings.com/api/")
    assert units[0]["concession"] == "1 month free"


def test_funnel_v2_listings_all_status_off_market_maps_to_unavailable() -> None:
    body = {
        "items": [
            {
                "unit_number": "9Z",
                "layout": "Studio",
                "bedrooms": 0,
                "bathrooms": 1.0,
                "price": "5000.00",
                "square_footage": 500,
                "date_available": "2026-06-01",
                "status": "Off-Market",
                "min_lease_term": 12,
            }
        ]
    }
    units = parse_funnel_listings(body, "https://nestiolistings.com/api/")
    assert units[0]["availability_status"] == "UNAVAILABLE"


def test_funnel_v2_listings_all_iso_timestamp_trimmed() -> None:
    body = {
        "items": [
            {
                "unit_number": "1A",
                "layout": "1 Bedroom",
                "bedrooms": 1,
                "bathrooms": 1.0,
                "price": "3000.00",
                "square_footage": 700,
                # Some v2 payloads ship an ISO timestamp instead of a bare
                # date string; trim to the date portion.
                "date_available": "2026-06-01T00:00:00+00:00",
                "status": "Available",
                "min_lease_term": 12,
            }
        ]
    }
    units = parse_funnel_listings(body, "https://nestiolistings.com/api/")
    assert units[0]["availability_date"] == "2026-06-01"
