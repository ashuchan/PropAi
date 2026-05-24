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
        responses = [
            {
                "url": "https://example.rentcafe.com/api/floorplans",
                "body": {wrapper: [item]},
            }
        ]
        adapter = RentCafeAdapter()
        ctx = _make_ctx(responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert len(result.units) == 1, f"wrapper={wrapper!r} produced no units"
        assert result.units[0]["floor_plan_name"] == "A1"


def test_parse_rentcafe_min_max_price() -> None:
    """Prefer numeric min_price/max_price over string minimumRent/maximumRent."""
    items = [
        {
            "floorplanName": "X",
            "beds": "1",
            "baths": "1",
            "minimumSQFT": "700",
            "maximumSQFT": "700",
            "minimumRent": "1349.00",
            "maximumRent": "2211.00",
            "min_price": 1349,
            "max_price": 1349,
            "floorplanId": "123",
            "availableUnitsCount": "1",
            "availableDate": "2026-05-01",
        }
    ]
    units = parse_rentcafe_floorplans(items, "test")
    assert len(units) == 1
    assert units[0]["rent_range"] == "$1,349"
    assert units[0]["availability_status"] == "AVAILABLE"


def test_static_fingerprints_nonempty() -> None:
    assert RentCafeAdapter().static_fingerprints()


def test_tier_used_label_is_pms_specific() -> None:
    items = [
        {
            "floorplanName": "A",
            "beds": "1",
            "baths": "1",
            "minimumRent": "1000.00",
            "maximumRent": "1000.00",
            "floorplanId": "1",
            "availableUnitsCount": "1",
        }
    ]
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
    body = [
        {
            "floorplanName": "Studio",
            "floorplanId": "F1",
            "api": "rentcafe",
            "beds": "0",
            "baths": "1",
            "minimumRent": "1200.00",
            "maximumRent": "1400.00",
            "availableUnitsCount": "1",
        }
    ]
    assert _is_rentcafe_response(body) is True
    units = parse_rentcafe_floorplans(body, "test")
    assert len(units) >= 1
    assert units[0]["floor_plan_name"] == "Studio"
    assert units[0]["rent_range"]


def test_rc_t02_pascalcase_root_list_is_fingerprinted() -> None:
    body = [
        {
            "FloorplanName": "1BR",
            "FloorplanId": "FP1",
            "MinimumRent": "2100.00",
            "MaximumRent": "2400.00",
            "AvailableUnitsCount": 2,
            "Beds": 1,
            "Baths": 1,
            "MinimumSQFT": "700",
            "MaximumSQFT": "750",
        }
    ]
    assert _is_rentcafe_response(body) is True


def test_rc_t03_pascalcase_items_parse_to_correct_fields() -> None:
    import re

    body = [
        {
            "FloorplanName": "1BR",
            "FloorplanId": "FP1",
            "MinimumRent": "2100.00",
            "MaximumRent": "2400.00",
            "AvailableUnitsCount": 2,
            "Beds": 1,
            "Baths": 1,
            "MinimumSQFT": "700",
            "MaximumSQFT": "750",
        }
    ]
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
    body = {
        "data": [
            {
                "FloorplanName": "Aspen",
                "FloorplanId": "A1",
                "Beds": 1,
                "MinimumRent": "2195.00",
                "MaximumRent": "2395.00",
                "AvailableUnitsCount": 3,
                "Baths": 1,
                "MinimumSQFT": "685",
                "MaximumSQFT": "695",
            }
        ]
    }
    assert _is_rentcafe_response(body) is True
    responses = [{"url": "https://windsor.example.com/api", "body": body}]
    adapter = RentCafeAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1
    assert result.units[0]["floor_plan_name"] == "Aspen"


@pytest.mark.asyncio
async def test_rc_t05_result_wrapper_lowercase_items() -> None:
    body = {
        "Result": [
            {
                "floorplanName": "Studio",
                "floorplanId": "S1",
                "minimumRent": "1500.00",
                "availableUnitsCount": 1,
                "availabilityURL": "https://securecafe.com/...",
            }
        ]
    }
    assert _is_rentcafe_response(body) is True
    responses = [{"url": "https://example.com/api", "body": body}]
    adapter = RentCafeAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1


def test_rc_t06_floorplans_new_wrapper_key() -> None:
    body = {
        "Floorplans": [
            {
                "floorplanName": "2BR",
                "api": "rentcafe",
                "minimumRent": "2500.00",
                "availableUnitsCount": 1,
            }
        ]
    }
    assert _is_rentcafe_response(body) is True


def test_rc_t07_two_level_response_result_unwrap() -> None:
    body = {
        "response": {
            "result": [
                {
                    "floorplanName": "1BR",
                    "api": "rentcafe",
                    "minimumRent": "1800.00",
                    "availableUnitsCount": 2,
                }
            ]
        }
    }
    items = _unwrap_rentcafe_list(body)
    assert items is not None
    assert len(items) == 1
    assert _is_rentcafe_response(body) is True


def test_rc_t08_all_unavailable_floorplans_still_extracted() -> None:
    body = [
        {
            "floorplanName": "3BR",
            "floorplanId": "FP3",
            "minimumRent": "3000.00",
            "maximumRent": "3200.00",
            "availableUnitsCount": 0,
            "beds": 3,
            "baths": 2,
        }
    ]
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
    fake_responses = [
        {
            "url": "https://example.com/x",
            "body": [{"floorplanName": "X", "api": "rentcafe"}],
        }
    ]
    tier_code, msg = _classify_rentcafe_failure(fake_responses)
    assert tier_code == "TIER_1_API_RENTCAFE_PARSE_ZERO"
    assert msg.startswith("RENTCAFE_PARSE_ZERO")


@pytest.mark.asyncio
async def test_rentcafe_success_tier_unchanged() -> None:
    """Real-shaped success → ``tier_used`` stays at ``TIER_1_API_RENTCAFE``."""
    body = [
        {
            "floorplanName": "1BR",
            "floorplanId": "FP1",
            "api": "rentcafe",
            "minimumRent": "1500.00",
            "maximumRent": "1700.00",
            "availableUnitsCount": "2",
            "beds": "1",
            "baths": "1",
        }
    ]
    adapter = RentCafeAdapter()
    ctx = _make_ctx([{"url": "https://example.com/api", "body": body}])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_RENTCAFE"
    assert len(result.units) == 1


def test_rentcafe_api_value_case_insensitive() -> None:
    """``"Api": "RentCafe"`` PascalCase value is recognised."""
    body = [
        {
            "Api": "RentCafe",
            "FloorplanName": "A1",
            "FloorplanId": "1",
            "MinimumRent": "1500",
            "MaximumRent": "1600",
        }
    ]
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
    assert (
        adapter.matches_response_body([{"floorplanName": "A1", "api": "rentcafe", "minimumRent": "1000"}])
        is True
    )
    assert adapter.matches_response_body({"random": "not-rentcafe"}) is False


# ── 2026-05-12 field-map fix: MAA Worthington shape ─────────────────────────


class TestMAAWorthingtonFieldMap:
    """Regression tests for the unit-level field-map fix.

    The MAA Worthington payload (real shape from run 2026-05-11/shard_0/11992.md)
    uses ``apartmentName`` for the per-unit identifier and a plain ``sqft``
    field for the area. Pre-fix, the parser:
      * dropped ``sqft`` because only ``minimumsqft``/``maximumsqft`` were read
      * used ``floorplanId`` (a shared floorplan-level id) as ``unit_number``
        causing every unit on the same plan to collide.

    The fix reads both unit-level fields first, falling back to the legacy
    plan-level fields. Existing Windsor/Bexley tests (no apartmentName,
    no plain sqft) continue to use the legacy paths.
    """

    def test_unit_level_sqft_preferred_over_min_max(self):
        """``sqft=1019`` wins over ``MinimumSqft=900, MaximumSqft=1100``."""
        items = [
            {
                "floorplanName": "Traditional 2x2 1019 SF",
                "apartmentName": "217",
                "beds": 2,
                "baths": 2,
                "sqft": 1019,                # ← unit-level (MAA shape)
                "minimumSqft": 900,
                "maximumSqft": 1100,
                "minimumRent": 2680,
                "maximumRent": 5165,
            }
        ]
        units = parse_rentcafe_floorplans(items, "test")
        assert len(units) == 1
        assert units[0]["sqft"] == "1019"

    def test_unit_level_sqft_only(self):
        """MAA case: only ``sqft`` is set; no min/max range. Read directly."""
        items = [
            {
                "floorplanName": "Traditional 2x2 1019 SF",
                "apartmentName": "217",
                "beds": 2, "baths": 2,
                "sqft": 1019,
                "minimumRent": 2680,
                "maximumRent": 5165,
            }
        ]
        units = parse_rentcafe_floorplans(items, "test")
        assert units[0]["sqft"] == "1019"

    def test_apartmentname_preferred_over_floorplanid(self):
        """``apartmentName="217"`` wins over ``floorplanId="2321569"``."""
        items = [
            {
                "floorplanName": "Traditional 2x2 1019 SF",
                "floorplanId": "2321569",     # legacy fallback id
                "apartmentName": "217",       # ← real unit number
                "beds": 2, "baths": 2, "sqft": 1019,
                "minimumRent": 2680,
            }
        ]
        units = parse_rentcafe_floorplans(items, "test")
        assert units[0]["unit_number"] == "217"

    def test_unitnumber_alias_preferred_over_floorplanid(self):
        """If the payload uses ``unitNumber`` (snake_case lowercased) instead
        of ``apartmentName``, that's also preferred over ``floorplanId``."""
        items = [
            {
                "floorplanName": "1BR Standard",
                "floorplanId": "FP1",
                "unitNumber": "101",
                "beds": 1, "baths": 1, "sqft": 750, "minimumRent": 1500,
            }
        ]
        units = parse_rentcafe_floorplans(items, "test")
        assert units[0]["unit_number"] == "101"

    def test_legacy_floorplanid_fallback_still_works(self):
        """Windsor/Bexley payloads (no apartmentName, no plain sqft) keep
        their existing behaviour: ``floorplanId`` becomes ``unit_number``,
        and ``minimumSqft``/``maximumSqft`` form the sqft range string."""
        items = [
            {
                "FloorplanName": "1BR",
                "FloorplanId": "FP1",
                "Beds": 1, "Baths": 1,
                "MinimumSqft": 700,
                "MaximumSqft": 750,
                "MinimumRent": "2100",
                "MaximumRent": "2400",
            }
        ]
        units = parse_rentcafe_floorplans(items, "test")
        assert units[0]["unit_number"] == "FP1"
        assert units[0]["sqft"] == "700-750"

    @pytest.mark.asyncio
    async def test_full_extract_admits_maa_units(self):
        """End-to-end: 3 MAA-shape items → 3 admitted units after post_process."""
        body = [
            {
                "floorplanName": "Traditional 2x2 1019 SF",
                "floorplanId": "2321569",
                "apartmentName": "217",
                "beds": 2, "baths": 2, "sqft": 1019,
                "minimumRent": 2680, "maximumRent": 5165,
                "api": "rentcafe",
            },
            {
                "floorplanName": "Traditional 2x2 1019 SF",
                "floorplanId": "2321569",
                "apartmentName": "318",
                "beds": 2, "baths": 2, "sqft": 1019,
                "minimumRent": 2700, "maximumRent": 5180,
                "api": "rentcafe",
            },
            {
                "floorplanName": "Traditional 1x1 770 SF",
                "floorplanId": "2604570",
                "apartmentName": "302",
                "beds": 1, "baths": 1, "sqft": 720,
                "minimumRent": 1940, "maximumRent": 3270,
                "api": "rentcafe",
            },
        ]
        adapter = RentCafeAdapter()
        ctx = _make_ctx([{"url": "https://www.maac.com/api/properties/X/units/available/", "body": body}])
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert result.tier_used == "TIER_1_API_RENTCAFE"
        assert len(result.units) == 3
        # Each unit retains its apartmentName-derived unit_number
        unit_numbers = {u.get("unit_number") for u in result.units}
        assert unit_numbers == {"217", "318", "302"}

    @pytest.mark.asyncio
    async def test_full_extract_rejects_dimless_rows(self):
        """An adversarial payload with valid RentCafe shape but no dimensions
        on any item — post_process drops them all and adapter falls through
        to the failure path, recording RENTCAFE_VALIDITY_REJECTED."""
        body = [
            {
                "api": "rentcafe",
                "floorplanName": "Some Plan",
                "minimumRent": "1500",
                # no beds, no baths, no sqft, no min/max sqft — fails is_valid_unit
            },
        ]
        adapter = RentCafeAdapter()
        ctx = _make_ctx([{"url": "https://example.com/api", "body": body}])
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        # No admitted units
        assert len(result.units) == 0
        # Error trail names the validity-rejection
        assert any(
            "RENTCAFE_VALIDITY_REJECTED" in e for e in result.errors
        ), f"expected validity-rejection error, got: {result.errors!r}"

    def test_three_maa_units_on_same_plan_get_distinct_unit_numbers(self):
        """Pre-fix: three apartments sharing floorplanId 2321569 all got
        unit_number=2321569 → dedup collapsed them. Post-fix: each carries
        its real apartmentName, all distinct."""
        items = [
            {
                "floorplanName": "Traditional 2x2 1019 SF",
                "floorplanId": "2321569",
                "apartmentName": "217",
                "beds": 2, "baths": 2, "sqft": 1019, "minimumRent": 2680,
            },
            {
                "floorplanName": "Traditional 2x2 1019 SF",
                "floorplanId": "2321569",
                "apartmentName": "318",
                "beds": 2, "baths": 2, "sqft": 1019, "minimumRent": 2700,
            },
            {
                "floorplanName": "Traditional 2x2 1019 SF",
                "floorplanId": "2321569",
                "apartmentName": "419",
                "beds": 2, "baths": 2, "sqft": 1019, "minimumRent": 2720,
            },
        ]
        units = parse_rentcafe_floorplans(items, "test")
        assert len(units) == 3
        unit_numbers = {u["unit_number"] for u in units}
        assert unit_numbers == {"217", "318", "419"}


# ────────────────────────────────────────────────────────────────────
# 2026-05-13 port (Commit 9 of MAY13_API_TIER_PORT_PLAN.md):
# WP middleware probe helpers + Nestin/hosted-table fixture modules.
# ────────────────────────────────────────────────────────────────────


class TestFindRentcafePropertyId:
    """Property-ID extraction from rendered RentCafe HTML."""

    def test_extracts_query_string_form(self):
        from ma_poc.pms.adapters.rentcafe import _find_rentcafe_property_id
        assert _find_rentcafe_property_id("...?propertyId=12345 in body...") == "12345"

    def test_extracts_bracket_array_query_form(self):
        from ma_poc.pms.adapters.rentcafe import _find_rentcafe_property_id
        assert _find_rentcafe_property_id("...?propertyId[]=67890 ...") == "67890"

    def test_extracts_data_attribute_form(self):
        from ma_poc.pms.adapters.rentcafe import _find_rentcafe_property_id
        assert _find_rentcafe_property_id('<div data-property-id="55555">') == "55555"

    def test_extracts_js_config_form(self):
        from ma_poc.pms.adapters.rentcafe import _find_rentcafe_property_id
        assert _find_rentcafe_property_id("propertyId: 99999,") == "99999"

    def test_empty_html_returns_none(self):
        from ma_poc.pms.adapters.rentcafe import _find_rentcafe_property_id
        assert _find_rentcafe_property_id("") is None
        assert _find_rentcafe_property_id("<html>no id</html>") is None


class TestOriginFromCtx:
    """``_origin_from_ctx`` extracts scheme://netloc from the most
    authoritative source available on the AdapterContext."""

    def test_prefers_fetch_result_final_url(self):
        from ma_poc.pms.adapters.rentcafe import _origin_from_ctx

        class FR:
            final_url = "https://example.com/some/path?x=1"

        class CTX:
            fetch_result = FR()
            base_url = "https://fallback.com/"

        assert _origin_from_ctx(CTX()) == "https://example.com"

    def test_falls_through_to_base_url(self):
        from ma_poc.pms.adapters.rentcafe import _origin_from_ctx

        class CTX:
            fetch_result = None
            base_url = "https://fallback.com/"

        assert _origin_from_ctx(CTX()) == "https://fallback.com"

    def test_returns_empty_when_no_url(self):
        from ma_poc.pms.adapters.rentcafe import _origin_from_ctx

        class CTX:
            fetch_result = None
            base_url = ""

        assert _origin_from_ctx(CTX()) == ""


def test_nestin_and_hosted_table_modules_import_cleanly():
    """Smoke: the new helper modules import without raising. Their
    full parser behaviour is exercised in dedicated fixtures once the
    orchestration is wired in Commit 11; this test guards against
    import-time errors."""
    from ma_poc.pms.adapters import _rentcafe_hosted_table, _rentcafe_nestin
    # Both modules expose at least one callable.
    assert hasattr(_rentcafe_nestin, "__name__")
    assert hasattr(_rentcafe_hosted_table, "__name__")


# ── 2026-05-22 — SecureCafe new-template parser fix ─────────────────────
#
# Live diagnostic against 5 RentCafe-detected production PIDs (72944,
# 24561, 6550, 40584, 67750) showed each securecafe ``availableunits.aspx``
# page had ≥4 AvailUnitRow rows but the old ``_SECURECAFE_FP_HDR_RE`` regex
# matched zero floor-plan headers — silently returning ``[]`` from
# ``parse_securecafe_availableunits`` and dropping the units. The newer
# SecureCafe template's caption format is
# ``Floor Plan: 2 Bed - 1 Bath - 2 Bedrooms, 1 Bathroom`` (the visual-name
# segment ``2 Bed - 1 Bath`` contains literal dashes), which the old
# character class ``[^<\-]`` excluded.


def test_securecafe_hdr_regex_matches_old_template() -> None:
    """Pre-2026-05-22 template (PID 1084 / theblackhawkapartments family)."""
    from ma_poc.pms.adapters.rentcafe import _SECURECAFE_FP_HDR_RE

    m = _SECURECAFE_FP_HDR_RE.search(
        "Floor Plan: A1 One Bedroom / One Bath - 1 Bedroom, 1.0 Bathroom"
    )
    assert m is not None
    assert m.group("name").strip() == "A1 One Bedroom / One Bath"
    assert m.group("bedtxt").strip() == "1 Bedroom"
    assert m.group("bathtxt") == "1.0"


def test_securecafe_hdr_regex_matches_new_template_with_dashed_name() -> None:
    """Post-2026-05-22 template (PID 6550 / scottsdalevintageapts family).

    The caption format is wrapped under ``<caption class="sr-only">…</caption>``
    so the regex sees the full text inline. The previous ``[^<\\-]`` character
    class blocked the dash inside ``2 Bed - 1 Bath``.
    """
    from ma_poc.pms.adapters.rentcafe import _SECURECAFE_FP_HDR_RE

    new_template_caption = (
        "Apartment Details and Selection for Floor Plan: "
        "2 Bed - 1 Bath - 2 Bedrooms, 1 Bathroom"
    )
    m = _SECURECAFE_FP_HDR_RE.search(new_template_caption)
    assert m is not None
    assert m.group("name").strip() == "2 Bed - 1 Bath"
    assert m.group("bedtxt").strip() == "2 Bedrooms"
    assert m.group("bathtxt") == "1"


def test_securecafe_hdr_regex_studio_still_matches() -> None:
    """Studio flag preserved across regex relaxation."""
    from ma_poc.pms.adapters.rentcafe import _SECURECAFE_FP_HDR_RE

    m = _SECURECAFE_FP_HDR_RE.search(
        "Floor Plan: Studio Loft - Studio, 1 Bathroom"
    )
    assert m is not None
    assert m.group("bedtxt").strip() == "Studio"


def test_capture_body_diagnostics_extracts_structural_signals() -> None:
    """The diagnostic helper mines the body for structural signals so a
    future regex bug is debuggable from events.jsonl alone.

    Acceptance: the same HTML fragment the SecureCafe parser failed on
    yields a non-empty signal payload that includes (a) the caption text
    (so the new ``Floor Plan: 2 Bed - 1 Bath`` form is visible), (b) the
    data-label inventory (so column changes are detectable), and (c) the
    first-row context window.
    """
    from ma_poc.pms.adapters.rentcafe import _capture_body_diagnostics

    body = (
        "<html><body>"
        "<h1>Floor Plans</h1>"
        "<table id='divFPH_2247999' class='availableUnits'>"
        "<caption class='sr-only'>Apartment Details and Selection for Floor Plan: "
        "2 Bed - 1 Bath - 2 Bedrooms, 1 Bathroom</caption>"
        "<thead><tr><th data-label='Apartment'>Apartment</th>"
        "<th data-label='Sq.Ft.'>Sq.Ft.</th>"
        "<th data-label='Rent'>Rent</th></tr></thead>"
        "<tbody>"
        "<tr class='AvailUnitRow' data-selenium-id='urow1'>"
        "<th data-label='Apartment'>#207</th>"
        "<td data-label='Sq.Ft.'>850</td>"
        "<td data-label='Rent'>$1,249</td>"
        "<td><a onclick=\"SetTermsUrl('rentaloptions.aspx?UnitID=1&FloorPlanID=2247999&myOlePropertyid=584155')\">Sel</a></td>"
        "</tr>"
        "</tbody></table>"
        "</body></html>"
    )
    signals = _capture_body_diagnostics(body)
    # Body length always reported
    assert signals["body_len"] == len(body)
    # Caption text recovered intact — this is the signal that would have
    # surfaced the new SecureCafe template format in the original bug.
    assert "caption_samples" in signals
    assert any("Floor Plan: 2 Bed - 1 Bath" in c for c in signals["caption_samples"])
    # Heading text recovered
    assert "heading_samples" in signals
    assert "Floor Plans" in signals["heading_samples"]
    # Table id (divFPH_<FloorPlanID>) recovered
    assert "table_ids" in signals
    assert "divFPH_2247999" in signals["table_ids"]
    # data-label inventory captured — the column-shape change between
    # old and new SecureCafe templates is detectable from this list.
    assert "data_label_inventory" in signals
    assert "Apartment" in signals["data_label_inventory"]
    assert "Rent" in signals["data_label_inventory"]
    # FloorPlanID seen via the onclick handler
    assert "floorplan_ids_seen" in signals
    assert "2247999" in signals["floorplan_ids_seen"]
    # First-row context — 350 char window BEFORE the AvailUnitRow start
    assert "first_row_ctx" in signals
    assert "<tbody>" in signals["first_row_ctx"]


def test_capture_body_diagnostics_handles_empty_body() -> None:
    """Empty / None bodies must not raise — diagnostics are best-effort."""
    from ma_poc.pms.adapters.rentcafe import _capture_body_diagnostics

    assert _capture_body_diagnostics("") == {"body_len": 0}
    assert _capture_body_diagnostics(None) == {"body_len": 0}  # type: ignore[arg-type]


def test_capture_body_diagnostics_caps_oversized_lists() -> None:
    """Each signal-kind list is capped so total payload stays under ~3KB
    even on a multi-megabyte SecureCafe page."""
    from ma_poc.pms.adapters.rentcafe import _capture_body_diagnostics

    # 100 captions, 100 headings, 100 table ids
    parts = []
    for i in range(100):
        parts.append(f"<caption>Floor Plan: P{i} - 1 Bedroom, 1 Bathroom</caption>")
        parts.append(f"<h2>Heading {i}</h2>")
        parts.append(f"<div id='divFPH_{1000 + i}'></div>")
    body = "".join(parts)
    signals = _capture_body_diagnostics(body, max_per_kind=5)
    assert len(signals["caption_samples"]) == 5
    assert len(signals["heading_samples"]) == 5
    assert len(signals["table_ids"]) == 5


def test_securecafe_parse_matches_units_under_new_template() -> None:
    """End-to-end: parse a minimal new-template snippet and assert units
    are extracted with floor-plan name + beds + baths populated.
    """
    from ma_poc.pms.adapters.rentcafe import parse_securecafe_availableunits

    # Minimal new-template fragment captured from live PID 6550 (one
    # caption + two AvailUnitRow rows). Live SC HTML uses single-quoted
    # attribute markup throughout — the existing row-matcher regex is
    # hard-anchored on ``class='AvailUnitRow'`` (single quotes), so the
    # fixture mirrors that. If/when we relax the row regex to accept
    # double-quoted markup, this fixture should be expanded with both
    # variants.
    new_template_html = (
        "<table id='divFPH_2247999' class='availableUnits'>"
        "<caption class='sr-only'>Apartment Details and Selection for Floor Plan: "
        "2 Bed - 1 Bath - 2 Bedrooms, 1 Bathroom</caption>"
        "<thead><tr><th data-label='Apartment'>Apartment</th></tr></thead>"
        "<tbody>"
        "<tr class='AvailUnitRow' data-selenium-id='urow1' id='unitrow_9609624'>"
        "<th data-label='Apartment'>#207</th>"
        "<td data-label='Sq.Ft.'>850</td>"
        "<td data-label='Rent'>$1,249</td>"
        "</tr>"
        "<tr class='AvailUnitRow' data-selenium-id='urow2' id='unitrow_9609647'>"
        "<th data-label='Apartment'>#230</th>"
        "<td data-label='Sq.Ft.'>850</td>"
        "<td data-label='Rent'>$1,279</td>"
        "</tr>"
        "</tbody></table>"
    )
    units = parse_securecafe_availableunits(new_template_html, "https://example/")
    assert len(units) == 2, f"got {len(units)} units; expected 2"
    # The unit-dict shape comes from ``make_unit_dict`` in _parsing.py — assert
    # only the fields the SecureCafe parser sets explicitly. Other fields may
    # be promoted/renamed (e.g. rent_low → market_rent_low) by the unit_dict
    # factory; this test guards the SC parser, not the post-process pipeline.
    fp_names = {u.get("floor_plan_name") for u in units}
    apt_nums = {u.get("unit_number") for u in units}
    assert fp_names == {"2 Bed - 1 Bath"}, fp_names
    assert apt_nums == {"207", "230"}, apt_nums
    # Bedrooms parsed from the "2 Bedrooms" suffix (not the short-form
    # "2 Bed" prefix).
    assert all(u.get("bedrooms") == "2" for u in units)
    assert all(u.get("bathrooms") in ("1", "1.0") for u in units)


# ─────────────────────────────────────────────────────────────────────
# 2026-05-24 — SecureCafe ``floorplans.aspx`` plan-summary parser.
# Companion to ``parse_securecafe_availableunits`` for the empty-
# inventory case. PID 52725 thenorthpointeapts canonical: the
# ``availableunits.aspx`` page rendered 200 OK with 0 real
# ``<tr class='AvailUnitRow'>`` rows (only 2 JS-block mentions). The
# sibling ``floorplans.aspx`` exposes plan-level data with rent + bed/
# bath + sqft but no per-apartment identity. Live HTML fixture captured
# from PID 253339 shannoncreekapt (which the production canary parsed
# correctly via ``availableunits`` after the PROBE_PROXY_URL wiring fix
# and serves as a known-good baseline for the floorplans template).
# ─────────────────────────────────────────────────────────────────────


def _sc_floorplans_row(
    selenium_idx: int,
    name: str,
    beds: str,
    baths: str,
    sqft: str,
    rent: str,
) -> str:
    """Build one ``tRow{N}_1`` row matching live SecureCafe markup.

    The Yardi template wraps every value in a sr-only span for screen-
    reader accessibility; the regex tolerates either presence or absence.
    """
    return (
        f"<tr data-selenium-id='tRow{selenium_idx}_1' scope='row'>"
        f"<td data-label='Floor Plan' data-selenium-id='FloorPlanName_{selenium_idx}'>"
        f"<span class='sr-only'>Floor Plan</span>{name}</td>"
        f"<td data-label='Beds' data-selenium-id='Bed_Bath_{selenium_idx}'>"
        f"<span class='sr-only'>Bed/Bath</span>{beds} / {baths} </td>"
        f"<td data-label= Sq.Ft. >{sqft}<span class='sr-only'>Square Foot</span></td>"
        f"<td data-label='Rent' data-selenium-id='Rent_{selenium_idx}'>"
        f"<span class='sr-only'> Rent</span>{rent}</td>"
        f"<td data-label='Availability'><button>Availability</button></td>"
        f"</tr>"
    )


def test_parse_securecafe_floorplans_happy_path_two_plans() -> None:
    """Two plan rows with rent ranges; assert all 4 fields parsed and
    ``unit_number=''`` so post_process routes to plan_summaries.
    """
    from ma_poc.pms.adapters.rentcafe import parse_securecafe_floorplans

    html = (
        "<div id='floorplanlist'>"
        "<table><thead class='floorplan-headings'><tr></tr></thead>"
        "<tbody class='floorplan-details'>"
        + _sc_floorplans_row(1, "A1", "1", "1", "650", "$1,135 -<span class='sr-only'>to</span> $1,978")
        + _sc_floorplans_row(2, "B1", "2", "2", "1055", "$1,614 -<span class='sr-only'>to</span> $2,892")
        + "</tbody></table></div>"
    )
    plans = parse_securecafe_floorplans(
        html, "https://example.securecafe.com/onlineleasing/x/floorplans.aspx"
    )
    assert len(plans) == 2, f"got {len(plans)} plans; expected 2"
    by_name = {p["floor_plan_name"]: p for p in plans}
    assert set(by_name) == {"A1", "B1"}
    assert by_name["A1"]["bedrooms"] == "1"
    assert by_name["A1"]["bathrooms"] == "1"
    assert by_name["A1"]["sqft"] == "650"
    assert by_name["A1"]["market_rent_low"] == 1135
    assert by_name["A1"]["market_rent_high"] == 1978
    assert by_name["B1"]["market_rent_low"] == 1614
    assert by_name["B1"]["market_rent_high"] == 2892
    # Plan-summary contract: empty unit_number routes via
    # ``extraction.post_process.classify`` into plan_summaries (playbook
    # §8.20 promotion rule does not fire because status is UNKNOWN).
    assert all(p["unit_number"] == "" for p in plans)
    assert all(p["availability_status"] == "UNKNOWN" for p in plans)
    assert all(p["extraction_tier"] == "TIER_1_API_RENTCAFE_SECURECAFE_PLANS" for p in plans)


def test_parse_securecafe_floorplans_single_rent_no_range() -> None:
    """Some tenants emit ``$1,135`` only (no range). Both rent_low and
    rent_high should be 1135 (mirrors the availableunits behaviour).
    """
    from ma_poc.pms.adapters.rentcafe import parse_securecafe_floorplans

    html = (
        "<tbody class='floorplan-details'>"
        + _sc_floorplans_row(1, "A1", "1", "1.5", "650", "$1,135")
        + "</tbody>"
    )
    plans = parse_securecafe_floorplans(html, "https://example/")
    assert len(plans) == 1
    assert plans[0]["market_rent_low"] == 1135
    assert plans[0]["market_rent_high"] == 1135
    # Half-bath parses as float-style "1.5".
    assert plans[0]["bathrooms"] == "1.5"


def test_parse_securecafe_floorplans_empty_inputs_safe() -> None:
    """No HTML, missing floorplan-details marker, or empty body all
    short-circuit to ``[]`` without raising.
    """
    from ma_poc.pms.adapters.rentcafe import parse_securecafe_floorplans

    assert parse_securecafe_floorplans("", "https://example/") == []
    assert parse_securecafe_floorplans(
        "<html><body>marketing page</body></html>", "https://example/"
    ) == []
    # Has the floorplan-details marker but no tRow_1 rows — also safe.
    assert (
        parse_securecafe_floorplans(
            "<tbody class='floorplan-details'></tbody>", "https://example/"
        )
        == []
    )


def test_parse_securecafe_floorplans_skips_tRow_2_3_4_subrows() -> None:
    """Only ``tRow{N}_1`` is the primary plan row. The ``_2`` (description),
    ``_3`` and ``_4`` (specials) sub-rows live alongside but contain no
    plan name — the parser must skip them silently, not double-count.
    """
    from ma_poc.pms.adapters.rentcafe import parse_securecafe_floorplans

    html = (
        "<tbody class='floorplan-details'>"
        + _sc_floorplans_row(1, "A1", "1", "1", "650", "$1,135")
        + "<tr valign='top' style='display:none' data-selenium-id='tRow1_2'>"
        "<td class='floorplan-desc' colspan='4'>plan description blob</td>"
        "</tr>"
        "<tr class='specials-holder' style='display:none' data-selenium-id='tRow1_3'>"
        "<td colspan='8' class='alert specials'>specials text</td>"
        "</tr>"
        "<tr class='specials-holder' style='display:none' data-selenium-id='tRow1_4'>"
        "<td colspan='8'>more specials</td>"
        "</tr>"
        + _sc_floorplans_row(2, "B1", "2", "2", "1055", "$1,614")
        + "</tbody>"
    )
    plans = parse_securecafe_floorplans(html, "https://example/")
    assert len(plans) == 2, f"got {len(plans)}; tRow_2/_3/_4 sub-rows must not double-count"
    assert {p["floor_plan_name"] for p in plans} == {"A1", "B1"}


def test_parse_securecafe_floorplans_live_fixture_pid_253339() -> None:
    """End-to-end against the live ``floorplans.aspx`` HTML captured
    2026-05-24 from PID 253339 shannoncreekapt — 6 plans (A1-A4, B1-B2).
    Pinned to catch any future regex regression that drops live data.
    """
    from ma_poc.pms.adapters.rentcafe import parse_securecafe_floorplans

    fixture = (
        Path(__file__).parent / "fixtures" / "rentcafe" / "securecafe_floorplans_253339.html"
    )
    if not fixture.exists():
        pytest.skip(f"fixture not present: {fixture}")
    html = fixture.read_text(encoding="utf-8")
    plans = parse_securecafe_floorplans(
        html,
        "https://shannoncreekapt.securecafe.com/onlineleasing/shannon-creek-apartments/floorplans.aspx",
    )
    assert len(plans) == 6
    names = {p["floor_plan_name"] for p in plans}
    assert names == {"A1", "A2", "A3", "A4", "B1", "B2"}
    a1 = next(p for p in plans if p["floor_plan_name"] == "A1")
    assert a1["bedrooms"] == "1"
    assert a1["bathrooms"] == "1"
    assert a1["sqft"] == "650"
    assert a1["market_rent_low"] == 1135
    assert a1["market_rent_high"] == 1978
    b2 = next(p for p in plans if p["floor_plan_name"] == "B2")
    assert b2["bedrooms"] == "2"
    assert b2["market_rent_high"] == 3224
