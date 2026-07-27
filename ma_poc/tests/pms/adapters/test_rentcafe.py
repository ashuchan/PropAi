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


# ── probe-seam stub (2026-07-26) ────────────────────────────────────────────
# Every failure-path test in this file drives the RentCafe adapter past its
# captured-API stage into the recovery fallbacks (``_try_rentcafe_securecafe_probe``
# homepage re-fetch at rentcafe.py:1420, the anchor walk, the vanity
# ``/floorplans`` sqft probe). Those fetch through the *sync*
# ``ma_poc.pms.adapters._probe.probe_get`` curl_cffi seam, which no
# httpx/adapter-level patch intercepts — so the tests were hitting the live
# internet (www.rentcafe.com / testprop.rentcafe.com).
#
# The stub answers "nothing here", which is exactly the state these tests
# describe: no recovery data, so the adapter falls through to the structured
# failure classification each test asserts on.


class _NoDataProbeResponse:
    """curl_cffi-response stand-in carrying no usable body.

    Exposes the full attribute surface adapter call sites read off a probe
    response: ``status_code`` / ``text`` / ``content`` / ``headers`` / ``url``.
    """

    def __init__(self, url: str, status_code: int = 404, text: str = "") -> None:
        self.url = url
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _stub_probe_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the ``_probe`` fetch seam at an offline 404/empty-body stub.

    Deterministic and network-free: each recovery fetch returns "no data".
    Tests whose subject IS a successful recovery must override this with
    their own stub.
    """

    def _fake_probe_get(url: str = "", **_kw: object) -> _NoDataProbeResponse:
        return _NoDataProbeResponse(url)

    def _fake_probe_post(
        url: str = "", data: object = None, **_kw: object
    ) -> _NoDataProbeResponse:
        return _NoDataProbeResponse(url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _fake_probe_get)
    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_post", _fake_probe_post)


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


def test_floorplan_id_is_provenance_not_a_unit_identity() -> None:
    """A plan-price payload must stay plan-level when it lacks an apartment number."""
    units = parse_rentcafe_floorplans(
        [
            {
                "floorplanName": "A1",
                "floorplanId": "shared-plan-71",
                "beds": 1,
                "baths": 1,
                "minimumRent": "1200",
                "maximumRent": "1200",
            }
        ],
        "https://example.test/api/floorplans",
    )
    assert units[0]["unit_number"] == ""
    assert units[0]["source_ids"] == {"rentcafe_floorplan_id": "shared-plan-71"}


def test_rentcafe_real_apartment_number_remains_an_identity() -> None:
    """A concrete ApartmentName wins while the plan id remains provenance."""
    units = parse_rentcafe_floorplans(
        [
            {
                "floorplanName": "A1",
                "floorplanId": "shared-plan-71",
                "apartmentName": "B-203",
                "minimumRent": "1200",
            }
        ],
        "https://example.test/api/floorplans",
    )
    assert units[0]["unit_number"] == "B-203"
    assert units[0]["source_ids"] == {"rentcafe_floorplan_id": "shared-plan-71"}


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
    assert u["unit_number"] == ""
    assert u["source_ids"] == {"rentcafe_floorplan_id": "FP1"}
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

    def test_floorplanid_without_apartment_number_stays_plan_level(self):
        """A RentCafe floorplan id is provenance, not a shared fake unit id."""
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
        assert units[0]["unit_number"] == ""
        assert units[0]["source_ids"] == {"rentcafe_floorplan_id": "FP1"}
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


# ─────────────────────────────────────────────────────────────────────
# 2026-05-20 cluster #5 RentCafe sub-cluster — broaden _SECURECAFE_URL_RE
# to also match /residentservices/<slug> paths.
#
# Many RentCafe marketing sites only link to the resident-services portal
# (current-resident login). The same slug is mounted under both
# /onlineleasing/<slug> and /residentservices/<slug> on the SecureCafe
# tenant. The adapter's _find_securecafe_base always synthesizes the
# /onlineleasing/<slug> URL from the captured slug, so capturing it from
# either path works.
#
# Live-verified 2026-05-20 on cityridgedc.com + thedylanchicago.com:
#   Marketing page exposes only /residentservices/<slug>/userlogin.aspx
#   /onlineleasing/<slug>/availableunits.aspx returns 59 + 2 AvailUnitRow
#   rows respectively via curl_cffi chrome120 impersonation.
# ─────────────────────────────────────────────────────────────────────


def test_securecafe_regex_matches_onlineleasing_url() -> None:
    """Regression — the legacy /onlineleasing/<slug> form must keep matching."""
    from ma_poc.pms.adapters.rentcafe import _SECURECAFE_URL_RE
    m = _SECURECAFE_URL_RE.search(
        "https://the24.securecafe.com/onlineleasing/the24/guestlogin.aspx"
    )
    assert m is not None
    assert m.group("sub") == "the24"
    assert m.group("slug") == "the24"


def test_securecafe_regex_matches_residentservices_url() -> None:
    """2026-05-20 cluster #5 fix — /residentservices/<slug> must now match
    with the same group names. The slug from the residentservices path
    is identical to the slug under /onlineleasing/ on the same tenant."""
    from ma_poc.pms.adapters.rentcafe import _SECURECAFE_URL_RE
    m = _SECURECAFE_URL_RE.search(
        "https://cityridgedc.securecafe.com/residentservices/city-ridge-clo/userlogin.aspx"
    )
    assert m is not None
    assert m.group("sub") == "cityridgedc"
    assert m.group("slug") == "city-ridge-clo"


def test_securecafe_regex_residentservices_dylan_pattern() -> None:
    """Real-bytes URL from thedylanchicago.com 2026-05-20 live probe."""
    from ma_poc.pms.adapters.rentcafe import _SECURECAFE_URL_RE
    m = _SECURECAFE_URL_RE.search(
        "https://thedylanchicago.securecafe.com/residentservices/160-n-morgan/userlogin.aspx"
    )
    assert m is not None
    assert m.group("sub") == "thedylanchicago"
    assert m.group("slug") == "160-n-morgan"


def test_securecafe_find_base_returns_onlineleasing_url_from_residentservices() -> None:
    """The end-to-end contract — _find_securecafe_base reads a
    /residentservices/<slug> URL from the page HTML and returns the
    canonical /onlineleasing/<slug> base for downstream availableunits
    probing. This is the cluster #5 routing fix in action."""
    from ma_poc.pms.adapters.base import AdapterContext
    from ma_poc.pms.adapters.rentcafe import _find_securecafe_base
    html = (
        '<html><body>'
        '<a href="https://cityridgedc.securecafe.com/residentservices/'
        'city-ridge-clo/userlogin.aspx">Resident Login</a>'
        '</body></html>'
    )
    # Minimal context — _find_securecafe_base only reads ctx._api_responses
    # + fetch_result.final_url + base_url as fallback sources.
    ctx = AdapterContext(
        base_url="https://cityridgedc.com/",
        detected=None,  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id="P-test",
    )
    base = _find_securecafe_base(html, ctx)
    assert base == (
        "https://cityridgedc.securecafe.com/onlineleasing/city-ridge-clo"
    ), (
        f"adapter must synthesize the canonical /onlineleasing/<slug> URL "
        f"from a /residentservices/<slug> source; got {base!r}"
    )


def test_securecafe_regex_does_not_match_unrelated_paths() -> None:
    """Sanity — /onlineleasing/ and /residentservices/ are the ONLY two
    accepted path segments. Other paths under securecafe.com should NOT
    match (avoid synthesizing bogus /onlineleasing/<slug> URLs)."""
    from ma_poc.pms.adapters.rentcafe import _SECURECAFE_URL_RE
    for url in (
        "https://foo.securecafe.com/login",
        "https://foo.securecafe.com/payments/auth",
        "https://foo.securecafe.com/marketing/slug",
    ):
        assert _SECURECAFE_URL_RE.search(url) is None, (
            f"path-form not in {{onlineleasing, residentservices}} must NOT match; "
            f"url={url!r}"
        )


# ─────────────────────────────────────────────────────────────────────
# 2026-05-21 grind600 finding — extend coverage to securecafenet.com
# (the Yardi resident-services alt domain). 100/600 = 16.7% of the
# random-sample worklist routed via ``<sub>.securecafenet.com/
# residentservices/<slug>/userlogin``. The legacy regex only matched
# securecafe.com so those drilled-down to TIER_3_DOM / LLM fallback.
# The Yardi tenant exposes the same onlineleasing/<slug>/availableunits
# .aspx endpoint regardless of which Yardi sub-product (securecafe.com
# vs. securecafenet.com) the marketing site links to.
# ─────────────────────────────────────────────────────────────────────


def test_securecafe_regex_matches_securecafenet_residentservices() -> None:
    """grind600 #1 finding — ``<sub>.securecafenet.com/residentservices/
    <slug>/userlogin`` must extract sub + slug for downstream probing.
    """
    from ma_poc.pms.adapters.rentcafe import _SECURECAFE_URL_RE
    m = _SECURECAFE_URL_RE.search(
        "https://livebrez.securecafenet.com/residentservices/atlantic-crossing0/userlogin"
    )
    assert m is not None
    assert m.group("sub") == "livebrez"
    assert m.group("dom") == "securecafenet"
    assert m.group("slug") == "atlantic-crossing0"


def test_securecafe_regex_matches_securecafenet_onlineleasing() -> None:
    """Some securecafenet sites also expose /onlineleasing/<slug> directly."""
    from ma_poc.pms.adapters.rentcafe import _SECURECAFE_URL_RE
    m = _SECURECAFE_URL_RE.search(
        "https://thecolumnsatbearcreek.securecafenet.com/onlineleasing/the-columns-at-bear-creek/guestlogin"
    )
    assert m is not None
    assert m.group("dom") == "securecafenet"
    assert m.group("slug") == "the-columns-at-bear-creek"


def test_securecafe_find_base_rewrites_securecafenet_to_securecafe() -> None:
    """When the source URL is on securecafenet.com (resident portal), the
    synthesized base must rewrite to securecafe.com (leasing portal).

    Validated live 2026-05-21 against 4 properties (livebrez,
    10xwoodwaysquare, livingatmarbella, thereservevisalia): the .net
    host returns 404 on /onlineleasing/<slug>/availableunits.aspx; the
    .com host returns the AvailUnitRow inventory page (CF-challenged
    for plain httpx; 200 with curl_cffi chrome120 impersonation).

    A .net→.com rewrite is the correct synthesis. Reverting to a
    domain-preserving synthesis ships a 404 → silent zero-unit exit.
    """
    from ma_poc.pms.adapters.base import AdapterContext
    from ma_poc.pms.adapters.rentcafe import _find_securecafe_base
    html = (
        '<html><body>'
        '<a href="https://livebrez.securecafenet.com/residentservices/'
        'atlantic-crossing0/userlogin">Resident Login</a>'
        '</body></html>'
    )
    ctx = AdapterContext(
        base_url="https://www.livebrez.com/",
        detected=None,  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id="P-test",
    )
    base = _find_securecafe_base(html, ctx)
    assert base == (
        "https://livebrez.securecafe.com/onlineleasing/atlantic-crossing0"
    ), (
        f"securecafenet source must rewrite to securecafe.com (leasing "
        f"portal lives on .com only); got {base!r}"
    )


def test_securecafe_find_base_preserves_securecafe_com_domain() -> None:
    """Regression — original securecafe.com path must still produce a
    securecafe.com base (no domain drift)."""
    from ma_poc.pms.adapters.base import AdapterContext
    from ma_poc.pms.adapters.rentcafe import _find_securecafe_base
    html = (
        '<html><body>'
        '<a href="https://cityridgedc.securecafe.com/onlineleasing/'
        'city-ridge-clo/guestlogin.aspx">Online Leasing</a>'
        '</body></html>'
    )
    ctx = AdapterContext(
        base_url="https://cityridgedc.com/",
        detected=None,  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id="P-test",
    )
    base = _find_securecafe_base(html, ctx)
    assert base == (
        "https://cityridgedc.securecafe.com/onlineleasing/city-ridge-clo"
    )


def test_securecafe_regex_does_not_match_securecafenet_unrelated_paths() -> None:
    """Sanity — only onlineleasing + residentservices are accepted on the
    .net domain too (no accidental matches on /payments, /maintenance, etc).
    """
    from ma_poc.pms.adapters.rentcafe import _SECURECAFE_URL_RE
    for url in (
        "https://foo.securecafenet.com/payments/auth",
        "https://foo.securecafenet.com/maintenance/requests",
        "https://foo.securecafenet.com/marketing/slug",
    ):
        assert _SECURECAFE_URL_RE.search(url) is None, (
            f"securecafenet path-form not in {{onlineleasing, residentservices}} "
            f"must NOT match; url={url!r}"
        )


def test_detector_routes_securecafenet_html_to_rentcafe() -> None:
    """grind600 finding — a marketing site that only links to
    securecafenet.com (no rentcafe.com / securecafe.com link) must still
    route to the rentcafe adapter so the SecureCafe drill-down probe runs.
    """
    from ma_poc.pms.detector import detect_pms
    html = (
        '<html><body>'
        '<a href="https://livebrez.securecafenet.com/residentservices/'
        'atlantic-crossing0/userlogin">Resident Portal</a>'
        '</body></html>'
    )
    det = detect_pms("https://www.livebrez.com/", page_html=html)
    assert det.pms == "rentcafe", (
        f"securecafenet link must route to rentcafe adapter; got "
        f"pms={det.pms!r}, confidence={det.confidence!r}"
    )


def test_securecafe_applicant_floorplans_v2() -> None:
    """RentCafe applicant-portal FloorPlansV2 API → unit + plan-level rows."""
    from ma_poc.pms.adapters.rentcafe import parse_securecafe_applicant_floorplans

    payload = {"status": True, "floorPlanList": [
        {"floorPlan": {"FloorPlanName": "B2", "Beds": 2, "Baths": 2.0,
                       "MinimumRent": 1570, "MaximumRent": 1570, "MinimumArea": 975,
                       "MinimumDeposit": 300, "DoNotPublish": False},
         "UnitAvailability": []},
        {"floorPlan": {"FloorPlanName": "A1", "Beds": 1, "Baths": 1.0,
                       "MinimumRent": 1200, "MaximumRent": 1350, "MinimumArea": 700,
                       "DoNotPublish": False},
         "UnitAvailability": [
             {"unitcode": "A1-101", "DisplayMinRent": 1225, "AvailableDate": "2026-08-01"},
             {"unitcode": "A1-204", "startingRent": 1250}]},
        {"floorPlan": {"FloorPlanName": "Hidden", "DoNotPublish": True},
         "UnitAvailability": []},
    ]}
    units = parse_securecafe_applicant_floorplans(payload, "https://x.securecafeapplicant.com/")
    assert len(units) == 3
    assert any(u["unit_number"] == "A1-101" and str(u["market_rent_low"]) == "1225" for u in units)
    assert any(u["floor_plan_name"] == "B2" and not u["unit_number"] for u in units)
    assert not any(u["floor_plan_name"] == "Hidden" for u in units)
