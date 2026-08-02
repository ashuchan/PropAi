"""On-Site.com (``on-site.com``) leasing-portal adapter tests (2026-07-18).

Pins the timeout-grind Surface C recovery: the ``on-site.com/apply/property/
{id}`` → ``/web/online_app3?property_id={id}`` React shell embeds a full
UNIT-LEVEL roster as a props island (unquoted-key JS literal). Adapter fetches
it statically (``probe_get``, no render) and parses per-unit records.

Fixtures are REAL online_app3 shells captured 2026-07-18:
  pullman_606821  — pullmansantarosa.com, 11 available units
  tustinview_214988 — allenproperties.net / tustin-view, 4 available units

Routing is flag-gated (``ENABLE_ONSITE_APPLY_ADAPTER``); the detector tests
toggle the env var (the flag helper reads env each call, no reload needed).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.onsite_apply import (
    OnSiteApplyAdapter,
    _active_unit_identifiers,
    _extract_balanced_array,
    _extract_balanced_object,
    _is_non_unit_application_option,
    _iter_top_level_objects,
    _normalize_onsite_bathrooms,
    _onsite_property_metadata,
    _plan_names_by_style,
    extract_onsite_property_id,
    parse_onsite_online_app3,
)
from ma_poc.pms.detector import detect_pms
from ma_poc.scripts.runners.jugnu import _format_v2_unit

FIXTURES = Path(__file__).parent / "fixtures" / "onsite"


def _shell(name: str) -> str:
    return (FIXTURES / f"online_app3_{name}.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_onsite_online_app3 — real fixtures
# ---------------------------------------------------------------------------


def test_parse_pullman_yields_11_unit_level_records() -> None:
    units = parse_onsite_online_app3(_shell("pullman_606821"), source_url="u")
    assert len(units) == 11
    # every unit carries a real number + rent + sqft (Tier-1 unit-level)
    assert all(u["unit_number"] for u in units)
    assert all(u["market_rent_low"] for u in units)
    assert all(u["sqft"] for u in units)
    first = next(u for u in units if u["unit_number"] == "301B")
    assert first["market_rent_low"] == 2504
    assert first["sqft"] == "760"
    assert first["floor_plan_name"] == "11L : 1 Bed, 1 Bath"
    assert first["availability_date"] == "03/05/2026"
    assert first["extraction_tier"] == "TIER_1_API_ONSITE_APPLY"


def test_parse_tustinview_yields_4_records_with_varying_rent() -> None:
    units = parse_onsite_online_app3(_shell("tustinview_214988"), source_url="u")
    assert len(units) == 4
    # per-unit rent is genuine (not a single plan starting_price)
    rents = sorted(u["market_rent_low"] for u in units)
    assert rents == [2030, 2095, 2190, 2635]


def test_parse_emits_stable_source_ids() -> None:
    units = parse_onsite_online_app3(_shell("pullman_606821"), source_url="u")
    first = next(u for u in units if u["unit_number"] == "301B")
    assert first["source_ids"]["onsite_unit_id"] == 5232557
    assert first["source_ids"]["onsite_style_id"] == "633761"
    assert first["source_ids"]["onsite_property_id"] == "606821"
    assert first["source_ids"]["onsite_unit_property_id"] == "606821"
    assert first["unit_id"] == "5232557"


def test_parse_retains_display_label_property_boundary_and_provenance() -> None:
    units = parse_onsite_online_app3(
        _shell("pullman_606821"),
        source_url="SOURCE",
        expected_property_id="606821",
    )
    first = next(u for u in units if u["unit_number"] == "301B")
    assert first["unit_name"] == "725-301B"
    assert first["bathrooms"] == "1"
    assert first["source_bathrooms_label"] == "1 bath"
    assert first["address"] == "725 Wilson Street"
    assert first["source_property_id"] == "606821"
    assert first["source_property_name"] == "Pullman Modern Urban Apartments"
    assert first["source_property_address"] == ("725  Wilson Street, Santa Rosa, CA 95401")
    assert first["source_property_provenance"] == ("onsite_online_app3_property_object")
    assert first["source_request_payload"] == {
        "property_id": "606821",
        "unit_id": "0",
    }


def test_parse_empty_body_returns_empty() -> None:
    assert parse_onsite_online_app3("", source_url="u") == []


def test_parse_island_absent_returns_empty() -> None:
    body = "<html><body>Welcome — no availability data here.</body></html>"
    assert parse_onsite_online_app3(body, source_url="u") == []


def test_parse_source_url_threaded_onto_units() -> None:
    units = parse_onsite_online_app3(_shell("pullman_606821"), source_url="SRC")
    assert all(u["source_api_url"] == "SRC" for u in units)


def test_active_unit_identifiers_preserves_display_number_prefix() -> None:
    """The public roster may list display rather than bare apartment numbers."""
    assert _active_unit_identifiers('unit_list:["725-301B"]') == {"725-301B"}


def test_parse_drops_bootstrap_units_outside_active_unit_list() -> None:
    """Only units exposed by the public On-Site unit step may be published."""
    body = """
    unit_availability:{floorplans:[{
      name:"1 Bed",abbreviation:"1 Bed",style_id:99,units:[
        {apartment_num:"PPT060",display_unit_number:"PPT060",rent:1595,
         sq_feet:700,num_bedrooms:1,bathrooms:"1 bath",id:1,style_id:99,
         date_available:"07/30/2026",street_address:"1005 S Gilbert St"},
        {apartment_num:"PPT010",display_unit_number:"PPT010",rent:1680,
         sq_feet:700,num_bedrooms:1,bathrooms:"1 bath",id:2,style_id:99,
         date_available:"08/30/2026",street_address:"1005 S Gilbert St"}
      ]}],unit_list:["PPT060"]}
    """

    units = parse_onsite_online_app3(body, source_url="u")

    assert [unit["unit_number"] for unit in units] == ["PPT060"]


def test_parse_empty_active_unit_list_publishes_no_units() -> None:
    """An explicit empty public roster must not fall back to raw unit objects."""
    body = """
    unit_availability:{floorplans:[{
      name:"1 Bed",abbreviation:"1 Bed",style_id:99,units:[
        {apartment_num:"PPT060",display_unit_number:"PPT060",rent:1595,
         sq_feet:700,num_bedrooms:1,bathrooms:"1 bath",id:1,style_id:99}
      ]}],unit_list:[]}
    """

    assert parse_onsite_online_app3(body, source_url="u") == []


def _current_shape_shell(
    *,
    shell_property_id: str = "717420",
    unit_property_id: str = "717421",
    plan_style_id: str = "99",
    unit_style_id: str = "99",
    baths: str = "1 1/2 bath",
) -> str:
    return f'''
    property:{{city:"Denver",property_id:{shell_property_id},
      property_name:"Bound Community",state:"CO",street_addr:"100 Main St",
      zip_code:"80202"}},
    unit_availability:{{floorplans:[{{
      name:"Nested Plan",abbreviation:"NP",starting_term:{{
        best_price:{{rent:1800,term:12}},other_terms:[{{rent:1900}}]}},
      style_id:{plan_style_id},units:[{{amenities:[],apartment_num:"A-1",
        display_unit_number:"Tower-A-1",rent:1800,sq_feet:750,
        num_bedrooms:1,bathrooms:"{baths}",date_available:"09/15/2026",
        id:7001,property_id:{unit_property_id},style_id:{unit_style_id},
        street_address:"100 Main St"}}]
    }}],unit_list:["Tower-A-1"]}}
    '''


def test_current_nested_floorplan_shape_joins_exact_top_level_name() -> None:
    body = _current_shape_shell()
    island = _extract_balanced_object(body, "unit_availability:{")

    assert _plan_names_by_style(island) == {"99": "Nested Plan"}
    units = parse_onsite_online_app3(
        body,
        source_url="SOURCE",
        expected_property_id="717420",
    )

    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "Nested Plan"
    assert units[0]["bathrooms"] == "1.5"


def test_child_unit_property_is_retained_not_rejected_as_sibling() -> None:
    """Some public rosters explicitly aggregate a child On-Site property."""
    units = parse_onsite_online_app3(
        _current_shape_shell(),
        source_url="SOURCE",
        expected_property_id="717420",
    )

    assert len(units) == 1
    assert units[0]["source_ids"]["onsite_property_id"] == "717420"
    assert units[0]["source_ids"]["onsite_unit_property_id"] == "717421"


def test_unmapped_style_remains_unproven() -> None:
    units = parse_onsite_online_app3(
        _current_shape_shell(unit_style_id="100"),
        source_url="SOURCE",
        expected_property_id="717420",
    )

    assert len(units) == 1
    assert units[0]["floor_plan_name"] == ""


def test_parser_rejects_wrong_or_missing_shell_property_boundary() -> None:
    body = _current_shape_shell(shell_property_id="717420")
    assert (
        parse_onsite_online_app3(
            body,
            source_url="SOURCE",
            expected_property_id="999999",
        )
        == []
    )
    assert (
        parse_onsite_online_app3(
            body.replace("property:{", "not_property:{", 1),
            source_url="SOURCE",
            expected_property_id="717420",
        )
        == []
    )


@pytest.mark.parametrize(
    "label,expected",
    [
        ("1 bath", "1"),
        ("2 baths", "2"),
        ("1 1/2 bath", "1.5"),
        ("2 1/2 baths", "2.5"),
        ("0 bath", ""),
        ("11 baths", ""),
        ("1 1/3 bath", ""),
        ("bath", ""),
    ],
)
def test_bath_label_normalization_is_bounded(label: str, expected: str) -> None:
    assert _normalize_onsite_bathrooms(label) == expected


def test_current_shape_survives_source_to_final_format() -> None:
    source = parse_onsite_online_app3(
        _current_shape_shell(),
        source_url="SOURCE",
        expected_property_id="717420",
    )[0]

    final = _format_v2_unit(
        source,
        datetime(2026, 8, 2, 12, tzinfo=UTC),
        property_id="291337",
    )

    assert final["unit_id"] == "7001"
    assert final["unit_name"] == "Tower-A-1"
    assert final["floor_plan_name"] == "Nested Plan"
    assert final["baths"] == 1.5
    assert final["rent_low"] == 1800
    assert final["area"] == 750
    assert final["available_date"] == "2026-09-15"
    assert final["source_ids"]["onsite_unit_id"] == 7001


def test_explicit_generic_plan_name_survives_source_to_final_format() -> None:
    source = parse_onsite_online_app3(
        _current_shape_shell().replace("Nested Plan", "1 Bed 1 Bath"),
        source_url="SOURCE",
        expected_property_id="717420",
    )[0]

    final = _format_v2_unit(
        source,
        datetime(2026, 8, 2, 12, tzinfo=UTC),
        property_id="291337",
    )

    assert source["_floor_plan_name_provenance"] == "onsite.floorplans[].name"
    assert final["floor_plan_name"] == "1 Bed 1 Bath"


def test_proven_roommate_add_on_application_option_is_not_a_unit() -> None:
    body = _current_shape_shell(
        plan_style_id="614819",
        unit_style_id="614819",
        baths="0 bath",
    )
    body = (
        body.replace("Nested Plan", "Roommate Add On")
        .replace("A-1", "Roommate Add O")
        .replace("Tower-Roommate Add O", "Roommate Add O")
        .replace("num_bedrooms:1", "num_bedrooms:0")
        .replace("sq_feet:750,", "sq_feet:null,")
    )

    assert (
        parse_onsite_online_app3(
            body,
            source_url="SOURCE",
            expected_property_id="717420",
        )
        == []
    )


def test_roommate_text_alone_cannot_suppress_a_physical_unit() -> None:
    assert not _is_non_unit_application_option(
        apartment_number="Roommate Add O",
        display_number="Roommate Add O",
        plan_name="Roommate Add On",
        bedrooms=1,
        bathrooms_label="1 bath",
        sqft=700,
    )


# ---------------------------------------------------------------------------
# _extract_balanced_object
# ---------------------------------------------------------------------------


def test_balanced_object_slices_nested_braces() -> None:
    text = "x=1;unit_availability:{a:{b:1},c:2};tail"
    assert _extract_balanced_object(text, "unit_availability:{") == "{a:{b:1},c:2}"


def test_balanced_object_ignores_brace_inside_string() -> None:
    text = 'unit_availability:{name:"a}b",n:1}END'
    assert _extract_balanced_object(text, "unit_availability:{") == '{name:"a}b",n:1}'


def test_balanced_object_absent_key_returns_empty() -> None:
    assert _extract_balanced_object("no key here", "unit_availability:{") == ""


def test_balanced_object_unbalanced_returns_empty() -> None:
    assert _extract_balanced_object("unit_availability:{a:{b:1}", "unit_availability:{") == ""


def test_balanced_floorplan_array_keeps_nested_arrays_and_objects() -> None:
    text = 'x;floorplans:[{a:[{b:1}],name:"x]y"},{c:2}];tail'
    array = _extract_balanced_array(text, "floorplans:[")
    assert array == '[{a:[{b:1}],name:"x]y"},{c:2}]'
    assert _iter_top_level_objects(array) == [
        '{a:[{b:1}],name:"x]y"}',
        "{c:2}",
    ]


def test_property_metadata_reads_exact_shell_boundary() -> None:
    assert _onsite_property_metadata(_shell("pullman_606821")) == {
        "property_id": "606821",
        "property_name": "Pullman Modern Urban Apartments",
        "property_address": "725  Wilson Street, Santa Rosa, CA 95401",
    }


# ---------------------------------------------------------------------------
# extract_onsite_property_id — three observed link shapes + escaping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,expected",
    [
        ('<a href="https://www.on-site.com/apply/property/606821">Apply</a>', "606821"),
        ('href="https://on-site.com/web/online_app3?property_id=40114&unit_id=0"', "40114"),
        ('src="https://www.on-site.com/web/online_app3/214988"', "214988"),
        (r"href=\"https:\/\/www.on-site.com\/apply\/property\/999123\"", "999123"),
        ("<a href='https://example.com/floorplans'>Plans</a>", None),
    ],
)
def test_extract_property_id(body: str, expected: str | None) -> None:
    assert extract_onsite_property_id(body) == expected


# ---------------------------------------------------------------------------
# detector routing (flag-gated)
# ---------------------------------------------------------------------------

_APPLY_HTML = (
    '<html><body><a href="https://www.on-site.com/apply/property/606821">Apply Now</a></body></html>'
)


def test_detector_routes_onsite_apply_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ONSITE_APPLY_ADAPTER", "true")
    r = detect_pms("https://pullmansantarosa.com/", page_html=_APPLY_HTML)
    assert r.pms == "onsite_apply"
    assert r.confidence >= 0.90


def test_detector_does_not_route_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ONSITE_APPLY_ADAPTER", "false")
    r = detect_pms("https://pullmansantarosa.com/", page_html=_APPLY_HTML)
    assert r.pms != "onsite_apply"


def test_detector_routes_from_onsite_host_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ONSITE_APPLY_ADAPTER", "true")
    r = detect_pms("https://www.on-site.com/apply/property/606821")
    assert r.pms == "onsite_apply"
    assert r.confidence >= 0.90


def test_detector_onsite_apply_beats_coresident_knock(monkeypatch: pytest.MonkeyPatch) -> None:
    """On-Site carries the real unit roster; when its apply link is present it
    must win over a co-resident Knock chat widget (0.91 > 0.90)."""
    monkeypatch.setenv("ENABLE_ONSITE_APPLY_ADAPTER", "true")
    html = (
        "<html><head>"
        '<script src="https://doorway.knck.io/latest/doorway.min.js"></script>'
        "</head><body>"
        '<a href="https://www.on-site.com/apply/property/40114">Apply</a>'
        "</body></html>"
    )
    r = detect_pms("https://sienavilla.com/", page_html=html)
    assert r.pms == "onsite_apply"


def test_detector_ignores_bare_onsite_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare on-site.com reference (no apply/online_app3 path) is NOT a
    leasing-portal signal even with the flag on."""
    monkeypatch.setenv("ENABLE_ONSITE_APPLY_ADAPTER", "true")
    html = '<html><body><img src="https://cdn.on-site.com/logo.png"></body></html>'
    r = detect_pms("https://example.com/", page_html=html)
    assert r.pms != "onsite_apply"


# ---------------------------------------------------------------------------
# OnSiteApplyAdapter.extract
# ---------------------------------------------------------------------------


class _DummyPage:
    pass


class _FR:
    def __init__(self, body: str) -> None:
        self.body = body


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 200


def _ctx(body: str, base_url: str = "https://pullmansantarosa.com/") -> AdapterContext:
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P1",
        fetch_result=_FR(body),
    )


@pytest.mark.asyncio
async def test_adapter_extracts_units_from_portal_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell = _shell("pullman_606821")
    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda url, timeout=None, unlocker=None: _Resp(shell),
    )
    adapter = OnSiteApplyAdapter()
    ctx = _ctx('<a href="https://www.on-site.com/apply/property/606821">Apply</a>')
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert result.tier_used == "TIER_1_API_ONSITE_APPLY"
    assert len(result.units) == 11
    assert result.confidence >= 0.7
    assert result.winning_url.endswith("property_id=606821&unit_id=0")
    assert result.api_responses[0]["requested_property_id"] == "606821"
    assert result.api_responses[0]["returned_property_id"] == "606821"


@pytest.mark.asyncio
async def test_adapter_no_id_when_no_portal_link() -> None:
    adapter = OnSiteApplyAdapter()
    ctx = _ctx("<html><body>no on-site link here</body></html>")
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_ONSITE_APPLY_NO_ID"
    assert result.units == []


@pytest.mark.asyncio
async def test_adapter_no_data_when_shell_has_no_island(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda url, timeout=None, unlocker=None: _Resp("<html>empty shell</html>"),
    )
    adapter = OnSiteApplyAdapter()
    ctx = _ctx('<a href="https://www.on-site.com/apply/property/606821">Apply</a>')
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_ONSITE_APPLY_PROPERTY_MISMATCH"
    assert result.units == []


@pytest.mark.asyncio
async def test_adapter_rejects_mismatched_shell_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda url, timeout=None, unlocker=None: _Resp(_current_shape_shell(shell_property_id="999999")),
    )
    adapter = OnSiteApplyAdapter()
    ctx = _ctx('<a href="https://www.on-site.com/apply/property/717420">Apply</a>')

    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_API_ONSITE_APPLY_PROPERTY_MISMATCH"
    assert result.units == []
    assert "requested=717420, returned=999999" in result.errors[0]


@pytest.mark.asyncio
async def test_adapter_no_data_on_probe_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(url, timeout=None, unlocker=None):  # noqa: ANN001
        raise RuntimeError("network down")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _boom)
    adapter = OnSiteApplyAdapter()
    ctx = _ctx('<a href="https://www.on-site.com/apply/property/606821">Apply</a>')
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_ONSITE_APPLY_NO_DATA"
    assert result.units == []
