"""Phase 3 — RentCafe adapter tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.rentcafe import (
    RentCafeAdapter,
    _classify_rentcafe_failure,
    _is_rentcafe_response,
    _unwrap_rentcafe_list,
    parse_rentcafe_floorplans,
)
from ma_poc.pms.detector import detect_pms

FIXTURES = Path(__file__).parent / "fixtures" / "rentcafe"


def _load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _make_ctx(api_responses: list[dict]) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://www.rentcafe.com/apartments/test/",
        detected=detect_pms("https://www.rentcafe.com/apartments/test/"),
        profile=None,
        expected_total_units=None,
        property_id="35593",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    pass


@pytest.mark.asyncio
async def test_rentcafe_extract_happy_path() -> None:
    """Real RentCafe payload produces units with correct fields."""
    responses = _load_fixture("35593.json")
    adapter = RentCafeAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) >= 5
    first = result.units[0]
    assert first["floor_plan_name"]
    assert first["rent_range"]
    assert "RENTCAFE" in first["extraction_tier"]


@pytest.mark.asyncio
async def test_rentcafe_extract_from_stored_fixture() -> None:
    for fixture_path in FIXTURES.glob("*.json"):
        responses = json.loads(fixture_path.read_text(encoding="utf-8"))
        adapter = RentCafeAdapter()
        ctx = _make_ctx(responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert isinstance(result, AdapterResult)
        assert len(result.units) > 0


@pytest.mark.asyncio
async def test_rentcafe_extract_returns_empty_on_no_data() -> None:
    responses = [{"url": "https://example.com/api", "body": {"some": "data"}}]
    adapter = RentCafeAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


def test_is_rentcafe_response_positive() -> None:
    body = [{"floorplanName": "A1", "api": "rentcafe", "minimumRent": "1000.00"}]
    assert _is_rentcafe_response(body)


def test_is_rentcafe_response_negative() -> None:
    assert not _is_rentcafe_response({"some": "dict"})
    assert not _is_rentcafe_response([])
    assert not _is_rentcafe_response(None)
    assert not _is_rentcafe_response([{"random": "keys"}])


@pytest.mark.asyncio
async def test_rentcafe_extracts_from_dict_wrapped_payload() -> None:
    """Yardi-style ``{"data": [...]}`` wrappers must extract the same units."""
    item = {
        "floorplanName": "A1",
        "api": "rentcafe",
        "beds": "1",
        "baths": "1",
        "floorplanId": "42",
        "minimumRent": "1000.00",
        "maximumRent": "1200.00",
        "availableUnitsCount": "2",
    }
    for wrapper in ("data", "results", "floorplans", "Result"):
        responses = [{
            "url": "https://example.rentcafe.com/api/floorplans",
            "body": {wrapper: [item]},
        }]
        adapter = RentCafeAdapter()
        ctx = _make_ctx(responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert len(result.units) == 1, f"wrapper={wrapper!r} produced no units"
        assert result.units[0]["floor_plan_name"] == "A1"


def test_parse_rentcafe_min_max_price() -> None:
    """Prefer numeric min_price/max_price over string minimumRent/maximumRent."""
    items = [{"floorplanName": "X", "beds": "1", "baths": "1", "minimumSQFT": "700",
              "maximumSQFT": "700", "minimumRent": "1349.00", "maximumRent": "2211.00",
              "min_price": 1349, "max_price": 1349, "floorplanId": "123",
              "availableUnitsCount": "1", "availableDate": "2026-05-01"}]
    units = parse_rentcafe_floorplans(items, "test")
    assert len(units) == 1
    assert units[0]["rent_range"] == "$1,349"
    assert units[0]["availability_status"] == "AVAILABLE"


def test_static_fingerprints_nonempty() -> None:
    assert RentCafeAdapter().static_fingerprints()


def test_tier_used_label_is_pms_specific() -> None:
    items = [{"floorplanName": "A", "beds": "1", "baths": "1", "minimumRent": "1000.00",
              "maximumRent": "1000.00", "floorplanId": "1", "availableUnitsCount": "1"}]
    units = parse_rentcafe_floorplans(items, "test")
    assert "RENTCAFE" in units[0]["extraction_tier"]


def test_rent_within_sanity_range() -> None:
    responses = _load_fixture("35593.json")
    import re
    for resp in responses:
        body = resp.get("body")
        if isinstance(body, list):
            units = parse_rentcafe_floorplans(body, "test")
            for u in units:
                if u["rent_range"]:
                    nums = re.findall(r"\d[\d,]*", u["rent_range"])
                    for n in nums:
                        val = int(n.replace(",", ""))
                        assert 200 <= val <= 50000


# --- 2026-04-19 fix tests (RC_T01 – RC_T10) ---------------------------------


def test_rc_t01_lowercase_root_list_regression_guard() -> None:
    body = [{
        "floorplanName": "Studio", "floorplanId": "F1", "api": "rentcafe",
        "beds": "0", "baths": "1", "minimumRent": "1200.00",
        "maximumRent": "1400.00", "availableUnitsCount": "1",
    }]
    assert _is_rentcafe_response(body) is True
    units = parse_rentcafe_floorplans(body, "test")
    assert len(units) >= 1
    assert units[0]["floor_plan_name"] == "Studio"
    assert units[0]["rent_range"]


def test_rc_t02_pascalcase_root_list_is_fingerprinted() -> None:
    body = [{
        "FloorplanName": "1BR", "FloorplanId": "FP1", "MinimumRent": "2100.00",
        "MaximumRent": "2400.00", "AvailableUnitsCount": 2, "Beds": 1, "Baths": 1,
        "MinimumSQFT": "700", "MaximumSQFT": "750",
    }]
    assert _is_rentcafe_response(body) is True


def test_rc_t03_pascalcase_items_parse_to_correct_fields() -> None:
    import re
    body = [{
        "FloorplanName": "1BR", "FloorplanId": "FP1", "MinimumRent": "2100.00",
        "MaximumRent": "2400.00", "AvailableUnitsCount": 2, "Beds": 1, "Baths": 1,
        "MinimumSQFT": "700", "MaximumSQFT": "750",
    }]
    units = parse_rentcafe_floorplans(body, "test")
    assert len(units) == 1
    u = units[0]
    assert u["floor_plan_name"] == "1BR"
    assert u["bedrooms"] == "1"
    assert u["unit_number"] == "FP1"
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", u["rent_range"])]
    assert nums and nums[0] > 0


@pytest.mark.asyncio
async def test_rc_t04_data_wrapper_pascalcase_items() -> None:
    body = {"data": [{
        "FloorplanName": "Aspen", "FloorplanId": "A1", "Beds": 1,
        "MinimumRent": "2195.00", "MaximumRent": "2395.00",
        "AvailableUnitsCount": 3, "Baths": 1, "MinimumSQFT": "685", "MaximumSQFT": "695",
    }]}
    assert _is_rentcafe_response(body) is True
    responses = [{"url": "https://windsor.example.com/api", "body": body}]
    adapter = RentCafeAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1
    assert result.units[0]["floor_plan_name"] == "Aspen"


@pytest.mark.asyncio
async def test_rc_t05_result_wrapper_lowercase_items() -> None:
    body = {"Result": [{
        "floorplanName": "Studio", "floorplanId": "S1",
        "minimumRent": "1500.00", "availableUnitsCount": 1,
        "availabilityURL": "https://securecafe.com/...",
    }]}
    assert _is_rentcafe_response(body) is True
    responses = [{"url": "https://example.com/api", "body": body}]
    adapter = RentCafeAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1


def test_rc_t06_floorplans_new_wrapper_key() -> None:
    body = {"Floorplans": [{
        "floorplanName": "2BR", "api": "rentcafe",
        "minimumRent": "2500.00", "availableUnitsCount": 1,
    }]}
    assert _is_rentcafe_response(body) is True


def test_rc_t07_two_level_response_result_unwrap() -> None:
    body = {"response": {"result": [{
        "floorplanName": "1BR", "api": "rentcafe",
        "minimumRent": "1800.00", "availableUnitsCount": 2,
    }]}}
    items = _unwrap_rentcafe_list(body)
    assert items is not None
    assert len(items) == 1
    assert _is_rentcafe_response(body) is True


def test_rc_t08_all_unavailable_floorplans_still_extracted() -> None:
    body = [{
        "floorplanName": "3BR", "floorplanId": "FP3",
        "minimumRent": "3000.00", "maximumRent": "3200.00",
        "availableUnitsCount": 0, "beds": 3, "baths": 2,
    }]
    units = parse_rentcafe_floorplans(body, "test")
    assert len(units) == 1
    assert units[0]["availability_status"] == "UNAVAILABLE"
    assert units[0]["rent_range"]


def test_rc_t09_non_rentcafe_body_rejected() -> None:
    body = {"amenities": [{"id": 1, "name": "Pool"}], "events": []}
    assert _is_rentcafe_response(body) is False


def test_rc_t10_empty_list_body_rejected() -> None:
    assert _is_rentcafe_response([]) is False
    assert _unwrap_rentcafe_list([]) is None


# --- 2026-04-20 fix tests (structured failure tier codes) -------------------
#
# These cover Change 1 from claude_adapter_fixes.md: every RentCafe failure
# path must re-stamp ``tier_used`` with a sub-code so the report can split
# misrouted properties (Windsor on Funnel) from genuine zero-data sites.


@pytest.mark.asyncio
async def test_rentcafe_tier_re_stamped_on_no_response() -> None:
    """Empty network log → ``TIER_1_API_RENTCAFE_NO_RESPONSE``."""
    adapter = RentCafeAdapter()
    ctx = _make_ctx([])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_RENTCAFE_NO_RESPONSE"
    assert result.confidence == 0.0
    assert any(e.startswith("RENTCAFE_NO_RESPONSE") for e in result.errors)


@pytest.mark.asyncio
async def test_rentcafe_tier_re_stamped_on_shape_reject() -> None:
    """Responses captured but none shape-match → ``..._SHAPE_REJECTED``."""
    adapter = RentCafeAdapter()
    ctx = _make_ctx([{"url": "https://example.com/x", "body": {"random": "payload"}}])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_RENTCAFE_SHAPE_REJECTED"
    assert any(e.startswith("RENTCAFE_SHAPE_REJECTED") for e in result.errors)


@pytest.mark.asyncio
async def test_rentcafe_tier_re_stamped_on_empty_list() -> None:
    """Yardi wrapper with empty list → ``..._SHAPE_REJECTED``."""
    body = {"data": []}
    adapter = RentCafeAdapter()
    ctx = _make_ctx([{"url": "https://example.com/x", "body": body}])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_RENTCAFE_SHAPE_REJECTED"


@pytest.mark.asyncio
async def test_rentcafe_tier_re_stamped_on_parse_zero() -> None:
    """Shape-matched envelope with un-parseable items → ``..._PARSE_ZERO``."""
    fake_responses = [{
        "url": "https://example.com/x",
        "body": [{"floorplanName": "X", "api": "rentcafe"}],
    }]
    tier_code, msg = _classify_rentcafe_failure(fake_responses)
    assert tier_code == "TIER_1_API_RENTCAFE_PARSE_ZERO"
    assert msg.startswith("RENTCAFE_PARSE_ZERO")


@pytest.mark.asyncio
async def test_rentcafe_success_tier_unchanged() -> None:
    """Real-shaped success → ``tier_used`` stays at ``TIER_1_API_RENTCAFE``."""
    body = [{
        "floorplanName": "1BR", "floorplanId": "FP1", "api": "rentcafe",
        "minimumRent": "1500.00", "maximumRent": "1700.00",
        "availableUnitsCount": "2", "beds": "1", "baths": "1",
    }]
    adapter = RentCafeAdapter()
    ctx = _make_ctx([{"url": "https://example.com/api", "body": body}])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_RENTCAFE"
    assert len(result.units) == 1


def test_rentcafe_api_value_case_insensitive() -> None:
    """``"Api": "RentCafe"`` PascalCase value is recognised."""
    body = [{
        "Api": "RentCafe", "FloorplanName": "A1", "FloorplanId": "1",
        "MinimumRent": "1500", "MaximumRent": "1600",
    }]
    assert _is_rentcafe_response(body) is True


@pytest.mark.asyncio
async def test_rentcafe_errors_list_has_machine_readable_prefix() -> None:
    """All failure-path errors start with an upper-snake-case code prefix."""
    import re
    code_re = re.compile(r"^RENTCAFE_[A-Z_]+:")
    for responses in [
        [],
        [{"url": "x", "body": {"random": "payload"}}],
    ]:
        adapter = RentCafeAdapter()
        ctx = _make_ctx(responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert result.errors, f"expected at least one error for input={responses!r}"
        assert code_re.match(result.errors[0]), (
            f"error did not start with machine-readable code prefix: {result.errors[0]!r}"
        )


def test_rentcafe_matches_response_body_protocol() -> None:
    """``matches_response_body`` is implemented and reuses the predicate."""
    adapter = RentCafeAdapter()
    assert adapter.matches_response_body(
        [{"floorplanName": "A1", "api": "rentcafe", "minimumRent": "1000"}]
    ) is True
    assert adapter.matches_response_body({"random": "not-rentcafe"}) is False
