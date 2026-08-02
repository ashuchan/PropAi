"""Evidence-gated tests for published unit-area recovery.

Acceptance criteria mirrored from ``pms.area_enrichment``:

* exact displayed unit labels require at least three corroborated joins;
* opaque API ids require a complete, unique rent/bed/bath bijection;
* a published plan range is retained as a range, never midpoint-imputed;
* every admitted field retains URL, response hash and record locator;
* an ambiguous or partial alternate roster leaves the input unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.area_enrichment import (
    enrich_missing_unit_areas,
    parse_published_plan_areas,
    parse_published_unit_area_roster,
)
from ma_poc.pms.detector import DetectedPMS
from ma_poc.pms.source_provenance import response_sha256
from ma_poc.scripts.runners.jugnu import (
    _emit_v2_units_for_property,
    _format_v2_unit,
)


def _ctx(html: str, url: str = "https://example.test/floorplans") -> AdapterContext:
    """Build a fetch-backed adapter context without browser or network I/O."""

    return AdapterContext(
        base_url=url,
        detected=DetectedPMS(pms="rentcafe", confidence=0.9),
        profile=None,
        expected_total_units=None,
        property_id="49248",
        fetch_result=SimpleNamespace(body=html.encode(), final_url=url),
        property_name="Evidence Apartments",
    )


def _unit(unit_number: str, rent: int, beds: int, baths: int) -> dict[str, object]:
    """Return one intentionally area-free adapter row."""

    return {
        "unit_number": unit_number,
        "market_rent_low": rent,
        "market_rent_high": rent,
        "bedrooms": str(beds),
        "bathrooms": str(baths),
    }


def test_windsor_roster_exact_labels_attach_auditable_area() -> None:
    """Three exact physical labels admit scalar area with source evidence."""

    html = """
    <div data-spaces-unit="0321" data-spaces-unit-id="u-321"
         data-spaces-sort-area="775" data-spaces-sort-price="3125"
         data-spaces-sort-bed="1" data-spaces-sort-bath="1"></div>
    <div data-spaces-unit="0402" data-spaces-unit-id="u-402"
         data-spaces-sort-area="910" data-spaces-sort-price="3440"
         data-spaces-sort-bed="1" data-spaces-sort-bath="1"></div>
    <div data-spaces-unit="PH01" data-spaces-unit-id="u-ph01"
         data-spaces-sort-area="1550" data-spaces-sort-price="8995"
         data-spaces-sort-bed="2" data-spaces-sort-bath="2"></div>
    """
    result = AdapterResult(
        units=[
            _unit("321", 3125, 1, 1),
            _unit("0402", 3440, 1, 1),
            _unit("PH01", 8995, 2, 2),
        ],
        tier_used="TIER_1_API_RENTCAFE_SECURECAFE_UNIT_LEVEL",
    )

    diagnostic = enrich_missing_unit_areas(_ctx(html), result)

    assert diagnostic["matched_units"] == 3
    assert diagnostic["exact_units"] == 3
    assert diagnostic["range_units"] == 0
    assert diagnostic["patched_units"] == 3
    assert [row["sqft"] for row in result.units] == ["775", "910", "1550"]
    assert result.units[0]["area_provenance"] == ("published_unit_unique_leading_zero_alias")
    assert result.units[1]["area_provenance"] == "published_unit_exact_display_unit"
    assert result.units[0]["source_response_sha256"] == response_sha256(html)
    assert result.units[0]["source_record_locator"] == "data-spaces-unit-id:u-321"
    assert len(result.html_responses) == 1
    assert result.html_responses[0]["body"] == html


def test_verified_duplicate_leading_zero_rows_share_exact_area_then_collapse() -> None:
    """Waterline's 321/0321 duplicate receives one source fact on both rows."""

    html = """
    <div data-spaces-unit="0321" data-spaces-unit-id="u-321"
         data-spaces-sort-area="775" data-spaces-sort-price="13875"
         data-spaces-sort-bed="2" data-spaces-sort-bath="2"></div>
    <div data-spaces-unit="0402" data-spaces-unit-id="u-402"
         data-spaces-sort-area="910" data-spaces-sort-price="3440"
         data-spaces-sort-bed="1" data-spaces-sort-bath="1"></div>
    <div data-spaces-unit="PH01" data-spaces-unit-id="u-ph01"
         data-spaces-sort-area="1550" data-spaces-sort-price="8995"
         data-spaces-sort-bed="2" data-spaces-sort-bath="2"></div>
    """
    duplicate = {
        **_unit("321", 13875, 2, 2),
        "market_rent_high": 13875,
        "floor_plan_name": "Waterline 2: B2a",
        "availability_status": "AVAILABLE",
        "available_date": "2026-08-02",
    }
    result = AdapterResult(
        units=[
            duplicate,
            {**duplicate, "unit_number": "0321"},
            _unit("0402", 3440, 1, 1),
            _unit("PH01", 8995, 2, 2),
        ],
        tier_used="TIER_1_API_RENTCAFE_SECURECAFE_UNIT_LEVEL",
    )

    diagnostic = enrich_missing_unit_areas(_ctx(html), result)

    assert diagnostic["exact_units"] == 4
    assert [result.units[0]["sqft"], result.units[1]["sqft"]] == ["775", "775"]
    assert result.units[0]["area_provenance"] == ("published_unit_verified_duplicate_leading_zero_alias")
    captured = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    emitted = _emit_v2_units_for_property(
        [_format_v2_unit(unit, captured, "261530") for unit in result.units]
    )
    assert len(emitted) == 3
    waterline = next(unit for unit in emitted if unit["unit_id"] == "0321")
    assert waterline["area"] == 775
    assert waterline["unit_id_aliases"] == ["321", "0321"]


def test_partial_two_label_roster_is_not_admitted() -> None:
    """One or two coincidental labels cannot patch a changing roster."""

    html = """
    <div data-unit="101" data-area="701"></div>
    <div data-unit="102" data-area="702"></div>
    """
    result = AdapterResult(
        units=[_unit("101", 1401, 1, 1), _unit("102", 1402, 1, 1)],
        tier_used="TIER_1_API_RENTCAFE_SECURECAFE_UNIT_LEVEL",
    )

    diagnostic = enrich_missing_unit_areas(_ctx(html), result)

    assert diagnostic["matched_units"] == 0
    assert all("sqft" not in row for row in result.units)
    assert len(result.html_responses) == 1
    assert result.html_responses[0]["identity"]["status"] == "NOT_ADMITTED"
    assert result.html_responses[0]["identity"]["admission_reason"] == ("evidence_gate_not_met")


def test_opaque_ids_use_only_complete_unique_fingerprint_bijection() -> None:
    """A complete authored table can join opaque ids without replacing them."""

    landing = '<iframe src="https://availability.example.test/roster/"></iframe>'
    roster = """
    <table><thead><tr>
      <th>Unit</th><th>Bedroom</th><th>Bathroom</th><th>Sq. Ft.</th>
      <th>Rent</th><th>Avail.</th>
    </tr></thead><tbody>
      <tr><td>14A</td><td>1</td><td>1</td><td>740</td><td>$3,101</td><td>Now</td></tr>
      <tr><td>20B</td><td>2</td><td>2</td><td>1,125</td><td>$5,202</td><td>9/1/2026</td></tr>
      <tr><td>31C</td><td>3</td><td>2</td><td>1,410</td><td>$7,303</td><td>10/1/2026</td></tr>
    </tbody></table>
    """

    class Response:
        status_code = 200
        text = roster
        url = "https://availability.example.test/roster/"

    result = AdapterResult(
        units=[
            _unit("opaque-a", 3101, 1, 1),
            _unit("opaque-b", 5202, 2, 2),
            _unit("opaque-c", 7303, 3, 2),
        ],
        tier_used="TIER_1_API_ONSITE_APPLY_UNIT_LEVEL",
    )

    diagnostic = enrich_missing_unit_areas(
        _ctx(landing, "https://70pine.example.test/availability/"),
        result,
        fetch=lambda *_args, **_kwargs: Response(),
    )

    assert diagnostic["join_method"] == "complete_rent_bed_bath_bijection"
    assert [row["unit_number"] for row in result.units] == [
        "opaque-a",
        "opaque-b",
        "opaque-c",
    ]
    assert [row["unit_name"] for row in result.units] == ["14A", "20B", "31C"]
    assert [row["sqft"] for row in result.units] == ["740", "1125", "1410"]


def test_plan_family_range_is_retained_without_midpoint() -> None:
    """Multiple published one-bed plans produce an honest family range."""

    html = """
    <div class="kad_caption_inner">1 Bed / 1 Bath - 675 sq ft</div>
    <div class="kad_caption_inner">1 Bed / 1 Bath - 705 sq ft</div>
    <div class="kad_caption_inner">1 Bed / 1 Bath - 850 sq ft</div>
    """
    plans = parse_published_plan_areas(html, "https://example.test/floorplans")
    assert {(plan.area_low, plan.area_high) for plan in plans} == {
        (675, 675),
        (705, 705),
        (850, 850),
    }
    result = AdapterResult(
        units=[_unit("A101", 1900, 1, 1)],
        tier_used="TIER_3_DOM_GENERIC_UNIT_LEVEL",
    )

    diagnostic = enrich_missing_unit_areas(_ctx(html), result)

    row = result.units[0]
    assert diagnostic["range_units"] == 1
    assert diagnostic["exact_units"] == 0
    assert diagnostic["patched_units"] == 1
    assert row["area_low"] == 675
    assert row["area_high"] == 850
    assert row["area_range"] == "675-850"
    assert row["area_provenance"] == "published_plan_family_range_no_midpoint"
    assert "sqft" not in row


def test_parser_rejects_conflicting_area_for_same_unit() -> None:
    """Contradictory authored scalars never become a source fact."""

    html = """
      <div data-unit="101" data-area="700"></div>
      <div data-unit="101" data-area="900"></div>
      <div data-unit="102" data-area="800"></div>
    """
    records = parse_published_unit_area_roster(html, "https://example.test")
    assert [(record.unit_number, record.area_low) for record in records] == [("102", 800)]


def test_identity_gated_apts247_catalogue_retains_exact_and_family_range() -> None:
    """A second provider may enrich area only after exact property binding."""

    landing = '<a href="/floorplans/">Floor Plans</a>'
    marker = '<script src="https://static2.apts247.info/js/toolbox247.js"></script>'
    community = {
        "objects": [
            {
                "api_key": "a" * 40,
                "name": "Evidence Apartments",
                "address": "1 Main Street",
                "city": "Wharton",
                "state": "TX",
                "zip_code": "77488",
            }
        ]
    }
    plans = {
        "objects": [
            {
                "id": 10,
                "name": "Palm",
                "bed": 2,
                "bath": 1,
                "sq_ft": "905",
                "community": community["objects"][0],
            },
            {
                "id": 11,
                "name": "Elm",
                "bed": 2,
                "bath": 1,
                "sq_ft": "916",
                "community": community["objects"][0],
            },
            {
                "id": 12,
                "name": "Ash",
                "bed": 1,
                "bath": 1,
                "sq_ft": "670",
                "community": community["objects"][0],
            },
        ]
    }

    class Response:
        def __init__(self, url: str, body: str) -> None:
            self.url = url
            self.status_code = 200
            self.text = body
            self.content = body.encode()

    calls: list[str] = []

    def fetch(url: str, **_kwargs: object) -> Response:
        calls.append(url)
        if url.endswith("/floorplans/"):
            return Response(url, marker)
        if "/community_info/" in url:
            return Response(url, json.dumps(community))
        if "/api/v1/floorplans/" in url:
            return Response(url, json.dumps(plans))
        raise AssertionError(url)

    context = AdapterContext(
        base_url="https://evidence.example/",
        detected=DetectedPMS(pms="onsite_apply", confidence=0.9),
        profile=None,
        expected_total_units=None,
        property_id="14956",
        fetch_result=SimpleNamespace(body=landing.encode(), final_url="https://evidence.example/"),
        property_name="Evidence Apartments",
        address="1 Main St",
        city="Wharton",
        state="TX",
        zip_code="77488",
    )
    result = AdapterResult(
        units=[
            {**_unit("604", 799, 2, 1), "floor_plan_name": "2X1S"},
            {**_unit("712", 960, 2, 1), "floor_plan_name": "2X1S"},
            {**_unit("801", 970, 2, 1), "floor_plan_name": "Palm"},
        ],
        tier_used="TIER_1_API_ONSITE_APPLY_UNIT_LEVEL",
    )

    diagnostic = enrich_missing_unit_areas(context, result, fetch=fetch)

    assert len(calls) == 3
    assert diagnostic["apts247_enrichment"]["patched_units"] == 3
    assert diagnostic["apts247_enrichment"]["exact_units"] == 1
    assert diagnostic["apts247_enrichment"]["range_units"] == 2
    assert diagnostic["exact_units"] == 1
    assert diagnostic["range_units"] == 2
    assert diagnostic["patched_units"] == 3
    assert [row.get("sqft") for row in result.units] == [None, None, "905"]
    assert [row["area_range"] for row in result.units] == ["905-916", "905-916", "905"]
    assert result.units[0]["area_provenance"] == ("published_plan_family_range_no_midpoint")
    assert result.units[2]["area_provenance"] == "published_plan_name_exact"
    floorplan_body = json.dumps(plans)
    assert result.units[0]["source_response_sha256"] == response_sha256(floorplan_body)
    assert len(result.api_responses) == 2
    assert "api_key=%3Credacted%3E" in result.unit_source_provenance[0]["source_url"]


def test_apts247_identity_mismatch_is_archived_but_never_admitted() -> None:
    """A sibling community is diagnostic evidence, not an area source."""

    marker = '<script src="https://static2.apts247.info/widget.js"></script>'
    payload = {
        "objects": [
            {
                "api_key": "b" * 40,
                "name": "Sibling Apartments",
                "address": "99 Other Road",
            }
        ]
    }

    class Response:
        status_code = 200
        text = json.dumps(payload)
        content = text.encode()

    calls: list[str] = []

    def fetch(url: str, **_kwargs: object) -> Response:
        calls.append(url)
        return Response()

    context = AdapterContext(
        base_url="https://evidence.example/",
        detected=DetectedPMS(pms="onsite_apply", confidence=0.9),
        profile=None,
        expected_total_units=None,
        property_id="14956",
        fetch_result=SimpleNamespace(body=marker.encode(), final_url="https://evidence.example/"),
        property_name="Evidence Apartments",
        address="1 Main St",
    )
    result = AdapterResult(
        units=[_unit("604", 799, 2, 1)],
        tier_used="TIER_1_API_ONSITE_APPLY_UNIT_LEVEL",
    )

    diagnostic = enrich_missing_unit_areas(context, result, fetch=fetch)

    assert len(calls) == 1
    assert diagnostic["apts247_enrichment"]["reason"] == ("community_identity_not_match")
    assert "sqft" not in result.units[0]
    assert len(result.api_responses) == 1
    assert result.api_responses[0]["identity"]["status"] == "MISMATCH"


def test_invalid_apts247_response_is_archived_for_offline_diagnosis() -> None:
    """Rejected API bytes survive even when they cannot produce a field."""

    marker = '<script src="https://static2.apts247.info/widget.js"></script>'

    class Response:
        status_code = 200
        text = "not-json"
        content = bytearray(text.encode())
        headers = {"content-type": "text/plain"}

    context = AdapterContext(
        base_url="https://evidence.example/",
        detected=DetectedPMS(pms="onsite_apply", confidence=0.9),
        profile=None,
        expected_total_units=None,
        property_id="14956",
        fetch_result=SimpleNamespace(
            body=marker.encode(),
            final_url="https://evidence.example/",
        ),
        property_name="Evidence Apartments",
        address="1 Main St",
    )
    result = AdapterResult(
        units=[_unit("604", 799, 2, 1)],
        tier_used="TIER_1_API_ONSITE_APPLY_UNIT_LEVEL",
    )

    diagnostic = enrich_missing_unit_areas(
        context,
        result,
        fetch=lambda *_args, **_kwargs: Response(),
    )

    assert diagnostic["apts247_enrichment"]["reason"] == ("community_info_invalid_json")
    assert result.api_responses[0]["body"] == "not-json"
    assert result.api_responses[0]["content_type"] == "text/plain"
    assert result.api_responses[0]["identity"]["admission_reason"] == ("community_info_invalid_json")


def test_exact_unit_roster_overrides_coarser_plan_fallback() -> None:
    """Apartment evidence wins when an earlier page exposed one plan scalar."""

    landing = """
    <div class="kad_caption_inner">1 Bed / 1 Bath - 700 sq ft</div>
    <iframe src="https://evidence.example/availability"></iframe>
    """
    roster = """
    <div data-unit="101" data-area="701"></div>
    <div data-unit="102" data-area="702"></div>
    <div data-unit="103" data-area="703"></div>
    """

    class Response:
        status_code = 200
        text = roster
        content = roster.encode()
        url = "https://evidence.example/availability"

    result = AdapterResult(
        units=[
            _unit("101", 1401, 1, 1),
            _unit("102", 1402, 1, 1),
            _unit("103", 1403, 1, 1),
        ],
        tier_used="TIER_1_API_RENTCAFE_SECURECAFE_UNIT_LEVEL",
    )

    diagnostic = enrich_missing_unit_areas(
        _ctx(landing),
        result,
        fetch=lambda *_args, **_kwargs: Response(),
    )

    assert [unit["sqft"] for unit in result.units] == ["701", "702", "703"]
    assert all(unit["area_provenance"] == "published_unit_exact_display_unit" for unit in result.units)
    assert diagnostic["exact_units"] == 3
    assert diagnostic["range_units"] == 0
    assert diagnostic["patched_units"] == 3
