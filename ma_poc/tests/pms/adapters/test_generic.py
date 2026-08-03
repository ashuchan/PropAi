"""Phase 3 — Generic adapter tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.pms.adapters._html_extract import extract_units_from_dom
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.generic import GenericAdapter, _find_unit_list, parse_generic_api
from ma_poc.pms.detector import detect_pms

FIXTURES = Path(__file__).parent / "fixtures" / "generic"


def _load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _make_ctx(api_responses: list[dict], pms: str = "unknown") -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://example.com/",
        detected=detect_pms("https://example.com/"),
        profile=None,
        expected_total_units=None,
        property_id="TEST",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    pass


@pytest.mark.asyncio
async def test_generic_extract_happy_path() -> None:
    responses = _load_fixture("synthetic_units.json")
    adapter = GenericAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) == 2
    assert result.units[0]["floor_plan_name"] == "1BR"
    assert result.units[0]["rent_range"]


@pytest.mark.asyncio
async def test_generic_extract_from_stored_fixture() -> None:
    for fixture_path in FIXTURES.glob("*.json"):
        responses = json.loads(fixture_path.read_text(encoding="utf-8"))
        adapter = GenericAdapter()
        ctx = _make_ctx(responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert isinstance(result, AdapterResult)
        assert len(result.units) >= 1


@pytest.mark.asyncio
async def test_generic_extract_returns_empty_on_no_data() -> None:
    responses = [{"url": "https://example.com/api", "body": {"config": True}}]
    adapter = GenericAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_generic_skips_llm_for_detected_pms_with_units() -> None:
    """F12: when pms != 'unknown' AND the upstream adapter produced units
    (adapter_unit_count > 0), the generic-fallback LLM tier is skipped.

    Before F12 the gate fired purely on detected.pms; now it requires the
    upstream adapter to have actually succeeded. This exercises the
    "still skip" half of the post-F12 truth table — adapter_unit_count=5
    means rentcafe found units, so spending generic-LLM budget is wasteful.
    """
    responses = [{"url": "https://example.com/api", "body": {"config": True}}]
    ctx = AdapterContext(
        base_url="https://www.rentcafe.com/test/",
        detected=detect_pms("https://www.rentcafe.com/test/"),
        profile=None,
        expected_total_units=None,
        property_id="TEST",
    )
    ctx._api_responses = responses  # type: ignore[attr-defined]
    ctx.adapter_unit_count = 5  # F12: upstream had units → gate stays shut
    adapter = GenericAdapter()
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert any("LLM/Vision skipped" in e for e in result.errors)


@pytest.mark.asyncio
async def test_generic_does_not_skip_llm_when_upstream_returned_zero_units() -> None:
    """F12 (the reverse half): when pms != 'unknown' but adapter_unit_count
    is 0, the LLM gate stays open so the LLM can attempt rescue. Before
    F12 the LLM was skipped here, producing the FAILED_NO_DATA cohort
    (~100 properties/run on AppFolio + Entrata).
    """
    responses = [{"url": "https://example.com/api", "body": {"config": True}}]
    ctx = AdapterContext(
        base_url="https://www.rentcafe.com/test/",
        detected=detect_pms("https://www.rentcafe.com/test/"),
        profile=None,
        expected_total_units=None,
        property_id="TEST",
    )
    ctx._api_responses = responses  # type: ignore[attr-defined]
    # adapter_unit_count defaults to 0 — gate stays open
    adapter = GenericAdapter()
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert not any(
        "LLM/Vision skipped for non-unknown PMS" in e for e in result.errors
    ), f"F12 gate should be OPEN when adapter_unit_count=0; got errors: {result.errors}"


def test_find_unit_list_direct_list() -> None:
    body = [{"name": "A", "minRent": 1000}]
    assert _find_unit_list(body) == body


def test_find_unit_list_nested() -> None:
    body = {"data": {"results": [{"name": "A", "minRent": 1000}]}}
    items = _find_unit_list(body)
    assert len(items) == 1


def test_find_unit_list_empty() -> None:
    assert _find_unit_list({"config": True}) == []
    assert _find_unit_list(None) == []
    assert _find_unit_list("string") == []


def test_parse_generic_api_dedup() -> None:
    """Duplicate items are deduplicated by unit_number."""
    items = [
        {"unitNumber": "101", "name": "A1", "bedrooms": 1, "minRent": 1500},
        {"unitNumber": "101", "name": "A1", "bedrooms": 1, "minRent": 1500},  # dupe
    ]
    units = parse_generic_api(items, "test")
    assert len(units) == 1


def test_parse_generic_api_rent_sanity() -> None:
    """Rents outside $200-$50,000 are filtered."""
    items = [
        {"name": "Valid", "bedrooms": 1, "minRent": 1500},
        {"name": "TooLow", "bedrooms": 1, "minRent": 14},  # rent=14 is garbage
    ]
    units = parse_generic_api(items, "test")
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "Valid"


@pytest.mark.parametrize(
    ("url", "item", "plan_id"),
    [
        (
            "https://example.com/api/v1/floorplans/?api_key=public",
            {"id": 48986, "name": "1 Bed 1 Bath", "bed": 1, "bath": 1, "sq_ft": 700, "rent": "From $900"},
            "48986",
        ),
        (
            "https://example.com/Apartments/module/widgets/",
            {"id": 1148242, "floorPlanName": "E1", "bedrooms": 0, "minRent": 937},
            "1148242",
        ),
        (
            "https://my.gounion.com/api/v1/properties/public_floor_plans/?is_all_unit=true",
            {"unitId": 504073, "name": "A1", "bedrooms": 1, "minRent": 1499, "available": 2},
            "504073",
        ),
    ],
)
def test_parse_generic_api_demotes_three_public_plan_catalogue_shapes(
    url: str,
    item: dict,
    plan_id: str,
) -> None:
    """A plan PK named id/unitId is provenance, never apartment identity."""
    rows = parse_generic_api([item], url)
    assert len(rows) == 1
    assert rows[0]["unit_number"] == ""
    assert rows[0]["source_ids"] == {"api_floorplan_id": plan_id}
    assert rows[0]["extraction_tier"] == "TIER_1_API_GENERIC_PLAN_LEVEL"


def test_parse_generic_api_keeps_explicit_unit_number_on_floorplan_channel() -> None:
    """A nested real unit row remains eligible under a /floorplans response."""
    rows = parse_generic_api(
        [
            {
                "id": 991630,
                "unitNumber": "172",
                "floorPlanName": "Peanut-R",
                "bedrooms": 1,
                "sqft": 693,
                "rent": "$915",
            }
        ],
        "https://example.com/api/v1/floorplans/",
    )
    assert len(rows) == 1
    assert rows[0]["unit_number"] == "172"
    assert rows[0]["extraction_tier"] == "TIER_1_API"


def test_parse_generic_api_keeps_bare_id_off_plan_channel() -> None:
    rows = parse_generic_api(
        [{"id": "21110", "name": "A1", "bedrooms": 1, "rent": 1200}],
        "https://example.com/api/inventory",
    )
    assert len(rows) == 1
    assert rows[0]["unit_number"] == "21110"


@pytest.mark.asyncio
async def test_generic_extract_preserves_plan_catalogue_as_plan_summaries() -> None:
    responses = [
        {
            "url": "https://example.com/api/v1/floorplans/?api_key=public",
            "body": {
                "meta": {"total_count": 2},
                "objects": [
                    {"id": 1, "name": "A1", "bed": 1, "bath": 1, "sq_ft": 700, "rent": "$900", "units": []},
                    {"id": 2, "name": "B1", "bed": 2, "bath": 2, "sq_ft": 950, "rent": "$1200", "units": []},
                ],
            },
        }
    ]
    result = await GenericAdapter().extract(
        _DummyPage(),
        _make_ctx(responses),
    )  # type: ignore[arg-type]
    assert result.units == []
    assert len(result.plan_summaries) == 2
    assert {row["source_ids"]["api_floorplan_id"] for row in result.plan_summaries} == {"1", "2"}


@pytest.mark.asyncio
async def test_generic_extract_does_not_repromote_nested_entrata_widget_plans() -> None:
    """Entrata widget plan IDs must stay plan-scoped in generic fallback.

    This mirrors PID 39378's captured ``/Apartments/module/widgets/`` shape:
    the PMS-native adapter had already retained the rows as plans, but the
    generic broad parser recursively rediscovered them and emitted eight fake
    apartment numbers from the bare plan ``id`` values.
    """
    responses = [
        {
            "url": "https://www.andanteapts.biz/Apartments/module/widgets/",
            "body": {
                "widget_name": "floor_plans",
                "widget_data": {
                    "content": {
                        "floor_plans": {
                            "floor_plans": [
                                {
                                    "id": 525217,
                                    "floorplan-name": "Pisa",
                                    "no_of_bedroom": 1,
                                    "square_footage": 689,
                                    "min_rent": 1405,
                                },
                                {
                                    "id": 525219,
                                    "floorplan-name": "Milan",
                                    "no_of_bedroom": 1,
                                    "square_footage": 742,
                                    "min_rent": 1355,
                                },
                            ]
                        }
                    }
                },
            },
        }
    ]

    result = await GenericAdapter().extract(
        _DummyPage(),
        _make_ctx(responses),
    )  # type: ignore[arg-type]

    assert result.units == []
    assert len(result.plan_summaries) == 2
    assert {
        row["source_ids"]["api_floorplan_id"]
        for row in result.plan_summaries
    } == {"525217", "525219"}


@pytest.mark.asyncio
async def test_generic_extract_does_not_repromote_empty_knock_layouts() -> None:
    """Knock layouts are plans when its sibling unit roster is empty."""
    responses = [
        {
            "url": (
                "https://doorway-api.knockrentals.com/"
                "v1/property/2005294/units"
            ),
            "body": {
                "units_data": {
                    "layouts": [
                        {
                            "id": "4c5a03c5-1ad2-412e-b01e-152a9d5213a8",
                            "name": "3x2f (Camellia)",
                            "bedrooms": 3,
                            "bathrooms": 2,
                            "area": 1650,
                        },
                        {
                            "id": "6173acd2-4bb6-4aaf-b3cb-e51bbebcb5c2",
                            "name": "2x2c (Bermuda)",
                            "bedrooms": 2,
                            "bathrooms": 2,
                            "area": 1348,
                        },
                    ],
                    "units": [],
                }
            },
        }
    ]

    result = await GenericAdapter().extract(
        _DummyPage(),
        _make_ctx(responses),
    )  # type: ignore[arg-type]

    assert result.units == []
    assert len(result.plan_summaries) == 2
    assert all(not row.get("unit_number") for row in result.plan_summaries)
    assert {
        row["source_ids"]["api_floorplan_id"]
        for row in result.plan_summaries
    } == {
        "4c5a03c5-1ad2-412e-b01e-152a9d5213a8",
        "6173acd2-4bb6-4aaf-b3cb-e51bbebcb5c2",
    }


@pytest.mark.asyncio
async def test_generic_extract_does_not_repromote_empty_sightmap_plans() -> None:
    """SightMap floor-plan ids stay plan-scoped when units[] is empty."""
    responses = [
        {
            "url": "https://sightmap.com/app/api/v1/key/sightmaps/88624",
            "body": {
                "data": {
                    "floor_plans": [
                        {
                            "id": "547190",
                            "name": "Bainbridge Non Reno",
                            "bedroom_count": 2,
                            "bathroom_count": 2,
                        },
                        {
                            "id": "547191",
                            "name": "Bainbridge Reno",
                            "bedroom_count": 2,
                            "bathroom_count": 2,
                        },
                    ],
                    "units": [],
                }
            },
        }
    ]

    result = await GenericAdapter().extract(
        _DummyPage(),
        _make_ctx(responses),
    )  # type: ignore[arg-type]

    assert result.units == []
    assert len(result.plan_summaries) == 2
    assert {
        row["source_ids"]["api_floorplan_id"]
        for row in result.plan_summaries
    } == {"547190", "547191"}


def test_static_fingerprints_empty() -> None:
    """Generic adapter has no fingerprints (it's the catch-all)."""
    assert GenericAdapter().static_fingerprints() == []


def test_parse_generic_nested_envelope() -> None:
    responses = _load_fixture("nested_envelope.json")
    body = responses[0]["body"]
    items = _find_unit_list(body)
    assert len(items) == 2
    units = parse_generic_api(items, "test")
    assert len(units) == 2
    studio = [u for u in units if u["floor_plan_name"] == "Studio"][0]
    assert studio["bed_label"] == "Studio"


def test_dom_does_not_promote_accessibility_prose_unit_it() -> None:
    html = """
    <div class="fp-card">
      Clicking this button will favorite the floor plan or unit it is associated with.
      1 Bed 1 Bath Rent: $750 - $850 620 sq ft
    </div>
    """

    rows, _tier = extract_units_from_dom(html, "https://example.test/")

    assert len(rows) == 1
    assert rows[0]["unit_number"] == ""
