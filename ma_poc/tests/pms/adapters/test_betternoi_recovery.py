from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ma_poc.pms.adapters import _betternoi_recovery as recovery

_CLIENT_MAGNOLIA = "4638ef92-b0ef-43e1-9ce7-8a39ef365999"
_CLIENT_KRC = "a1945274-2232-4de6-94f8-83cda4c243ae"
_CLIENT_CHESTER = "3b522898-667b-4c3d-8581-f1db308840a1"

_FP_MAGNOLIA = "1b197315-d463-4639-8d6a-880c59b611c2"
_FP_KRC = "192e78a5-8c35-4ca0-a28c-42ef64273011"
_FP_CHESTER = "0901bb51-aca9-4da2-9564-d9ea35162de0"


def _html(client_uuid: str, floorplan_uuids: list[str]) -> str:
    buttons = "".join(
        f"""
        <a class="apply-button btn individual-plan-button btn-block"
           data-property="{client_uuid}"
           data-fpcode="{floorplan_uuid}"
           data-fpname="A1">View Availability</a>
        """
        for floorplan_uuid in floorplan_uuids
    )
    return f"""
        <html><body>{buttons}</body>
        <script>
          var url = "https://ares.betternoi.com/api/pub/v1/client/building/unit?client_uuid="
                    + $(this).attr('data-property')
                    + "&floorplan_uuid=" + $(this).attr('data-fpcode')
                    + "&is_available=true";
        </script></html>
    """


def _ctx(
    html: str,
    *,
    city: str = "Norcross",
    state: str = "GA",
    zip_code: str = "30093",
    address: str = "4200 Jimmy Carter Blvd",
    base_url: str = "https://www.krcreserveapts.com/",
) -> SimpleNamespace:
    return SimpleNamespace(
        fetch_result=SimpleNamespace(body=html.encode(), final_url=base_url),
        base_url=base_url,
        city=city,
        state=state,
        zip_code=zip_code,
        address=address,
        property_id="18187",
    )


def _item(
    *,
    client_uuid: str = _CLIENT_KRC,
    floorplan_uuid: str = _FP_KRC,
    unit_uuid: str = "24f8e6cd-92d9-40fc-853f-b0c4f69ee911",
    unit_number: str = "122",
    unit_identifier: str = "01-122",
    city: str = "NORCROSS",
    state: str = "GA",
    zip_code: str = "30093",
    rent: str = "1049.00",
    sqft: str = "747.00",
    available_date: str = "2026-08-01",
) -> dict[str, Any]:
    return {
        "uuid": unit_uuid,
        "client_uuid": client_uuid,
        "floor_plan": {
            "uuid": floorplan_uuid,
            "name": "1 Bed 1 Bath",
            "description": "Classic",
            "code": "hr-11m",
        },
        "id": 94445,
        "unit_number": unit_number,
        "unit_identifier": unit_identifier,
        "min_square_feet": sqft,
        "max_square_feet": sqft,
        "min_rent": rent,
        "max_rent": rent,
        "bathroom_count": "1.00",
        "bedroom_count": "1.00",
        "adjusted_available_date": available_date,
        "building_address": " HUNTERS CLUB LN ",
        "building_number": "1",
        "building_city": city,
        "building_postal_code": zip_code,
        "building_state": state,
        "unit_to_skip": False,
        "availability_status": "available",
    }


@pytest.mark.parametrize(
    ("client_uuid", "floorplan_uuids"),
    (
        (
            _CLIENT_MAGNOLIA,
            [
                "ae1e3301-7d38-422f-b8fc-4c2e5e400816",
                "4e59b11c-647a-4b02-a8a7-0d28f323bde5",
                _FP_MAGNOLIA,
            ],
        ),
        (
            _CLIENT_KRC,
            [
                _FP_KRC,
                "6ca29819-c60d-44e1-ace3-df83f85e253b",
                "f7cb2493-f3dd-43fe-9dda-fd8ba27c0edb",
            ],
        ),
        (
            _CLIENT_CHESTER,
            [
                _FP_CHESTER,
                "ac27c22c-6af4-420a-9241-e56d4310365f",
                "e0075039-f67a-4cc6-804c-ceac22db8716",
            ],
        ),
    ),
    ids=("magnolia-994", "krc-18187", "chester-live-control"),
)
def test_find_targets_for_three_live_betternoi_shapes(
    client_uuid: str,
    floorplan_uuids: list[str],
) -> None:
    html = _html(client_uuid, floorplan_uuids)

    assert recovery.find_betternoi_targets(html) == [
        (client_uuid, floorplan_uuid) for floorplan_uuid in floorplan_uuids
    ]


def test_find_targets_deduplicates_responsive_cards() -> None:
    html = _html(_CLIENT_MAGNOLIA, [_FP_MAGNOLIA, _FP_MAGNOLIA, _FP_MAGNOLIA])

    assert recovery.find_betternoi_targets(html) == [(_CLIENT_MAGNOLIA, _FP_MAGNOLIA)]


@pytest.mark.parametrize(
    "html",
    (
        # Bayside Villas live negative: the shared script exists but the plans
        # are explicitly contact-only and carry no property/plan UUID pair.
        """
        <a href="/contact/">Contact for Availability</a>
        <script>
        var url = "https://ares.betternoi.com/api/pub/v1/client/building/unit?client_uuid=";
        </script>
        """,
        f"""
        <script>
        var url = "https://ares.betternoi.com/api/pub/v1/client/building/unit?client_uuid=";
        </script>
        <a data-property="{_CLIENT_KRC}">View Availability</a>
        """,
        _html(_CLIENT_KRC, [_FP_KRC]) + _html(_CLIENT_MAGNOLIA, [_FP_MAGNOLIA]),
        f'<a data-property="{_CLIENT_KRC}" data-fpcode="{_FP_KRC}">View</a>',
        f"""
        <a href="https://ares.betternoi.com/screening/application/create/?key={_CLIENT_KRC}">
          Apply
        </a>
        <script>
        var url = "https://ares.betternoi.com/api/pub/v1/client/building/unit?client_uuid=";
        </script>
        """,
        """
        <a data-property="12345" data-fpcode="67890">View Availability</a>
        <script>
        var url = "https://ares.betternoi.com/api/pub/v1/client/building/unit?client_ids=12345";
        </script>
        """,
    ),
    ids=(
        "contact-only",
        "unpaired",
        "multiple-clients",
        "no-api-marker",
        "application-key-is-not-client-uuid",
        "scalar-client-ids-portfolio-trap",
    ),
)
def test_find_targets_rejects_negative_or_ambiguous_shapes(html: str) -> None:
    assert recovery.find_betternoi_targets(html) == []


def test_parse_payload_preserves_canonical_fields_and_explicit_date() -> None:
    source_url = recovery._target_url(_CLIENT_KRC, _FP_KRC)
    units = recovery.parse_betternoi_payload(
        {"count": 1, "next": None, "previous": None, "results": [_item()]},
        ctx=_ctx(_html(_CLIENT_KRC, [_FP_KRC])),
        client_uuid=_CLIENT_KRC,
        floorplan_uuid=_FP_KRC,
        source_url=source_url,
    )

    assert len(units) == 1
    unit = units[0]
    assert unit["unit_number"] == "01-122"
    assert unit["unit_name"] == "01-122"
    assert unit["building"] == "1"
    assert unit["floor_plan_name"] == "1 Bed 1 Bath"
    assert (
        unit["_floor_plan_name_provenance"]
        == "betternoi.floor_plan.name"
    )
    assert unit["bedrooms"] == "1"
    assert unit["bathrooms"] == "1"
    assert unit["sqft"] == "747"
    assert unit["market_rent_low"] == 1049
    assert unit["market_rent_high"] == 1049
    assert unit["availability_date"] == "2026-08-01"
    assert unit["available_date"] == "2026-08-01"
    assert unit["extraction_tier"] == "TIER_1_API_BETTERNOI"
    assert unit["source_api_url"] == source_url
    assert unit["source_ids"] == {
        "betternoi_unit_uuid": "24f8e6cd-92d9-40fc-853f-b0c4f69ee911",
        "betternoi_floorplan_uuid": _FP_KRC,
        "betternoi_client_uuid": _CLIENT_KRC,
    }


def test_jugnu_preserves_only_explicit_betternoi_bed_bath_plan_name() -> None:
    """The API's real plan name survives without weakening generic hygiene."""
    from ma_poc.scripts.runners.jugnu import _format_v2_unit

    source_url = recovery._target_url(_CLIENT_KRC, _FP_KRC)
    [unit] = recovery.parse_betternoi_payload(
        {"count": 1, "next": None, "previous": None, "results": [_item()]},
        ctx=_ctx(_html(_CLIENT_KRC, [_FP_KRC])),
        client_uuid=_CLIENT_KRC,
        floorplan_uuid=_FP_KRC,
        source_url=source_url,
    )

    formatted = _format_v2_unit(
        unit,
        datetime(2026, 8, 1, tzinfo=UTC),
        "18187",
    )
    unmarked = _format_v2_unit(
        {**unit, "_floor_plan_name_provenance": None},
        datetime(2026, 8, 1, tzinfo=UTC),
        "18187",
    )
    forged = _format_v2_unit(
        {**unit, "_floor_plan_name_provenance": "other.explicit.name"},
        datetime(2026, 8, 1, tzinfo=UTC),
        "18187",
    )

    assert formatted["floor_plan_name"] == "1 Bed 1 Bath"
    assert unmarked["floor_plan_name"] is None
    assert forged["floor_plan_name"] is None
    assert formatted["unit_id"] == "01-122"
    assert formatted["rent_low"] == 1049
    assert formatted["area"] == 747
    assert formatted["available_date"] == "2026-08-01"


@pytest.mark.parametrize(
    "changed",
    (
        {"client_uuid": _CLIENT_MAGNOLIA},
        {"floor_plan": {"uuid": _FP_MAGNOLIA, "name": "1 Bed 1 Bath"}},
        {"building_city": "Gaffney", "building_state": "SC", "building_postal_code": "29340"},
        {"availability_status": "unavailable"},
        {"unit_to_skip": True},
        {"unit_identifier": "Left", "unit_number": ""},
        {"min_rent": "0", "max_rent": "0"},
        {"min_square_feet": "0", "max_square_feet": "0"},
        {"uuid": "not-a-uuid"},
    ),
    ids=(
        "wrong-client",
        "wrong-floorplan",
        "wrong-property",
        "unavailable",
        "skip-flag",
        "junk-unit",
        "zero-rent",
        "zero-area",
        "bad-unit-uuid",
    ),
)
def test_parse_payload_fails_closed_on_identity_or_quality_mismatch(
    changed: dict[str, Any],
) -> None:
    item = _item()
    item.update(changed)

    assert (
        recovery.parse_betternoi_payload(
            {"results": [item]},
            ctx=_ctx(_html(_CLIENT_KRC, [_FP_KRC])),
            client_uuid=_CLIENT_KRC,
            floorplan_uuid=_FP_KRC,
            source_url=recovery._target_url(_CLIENT_KRC, _FP_KRC),
        )
        == []
    )


def test_one_foreign_row_rejects_the_whole_payload() -> None:
    valid = _item()
    foreign = _item(
        unit_uuid="901ba3b2-e50d-4639-b9c5-bc1db00b5d5d",
        unit_number="F29",
        unit_identifier="F29",
        client_uuid=_CLIENT_MAGNOLIA,
        floorplan_uuid=_FP_MAGNOLIA,
        city="Gaffney",
        state="SC",
        zip_code="29340",
    )

    assert (
        recovery.parse_betternoi_payload(
            {"count": 2, "next": None, "results": [valid, foreign]},
            ctx=_ctx(_html(_CLIENT_KRC, [_FP_KRC])),
            client_uuid=_CLIENT_KRC,
            floorplan_uuid=_FP_KRC,
            source_url=recovery._target_url(_CLIENT_KRC, _FP_KRC),
        )
        == []
    )


def test_numbered_foreign_street_rejects_same_city_state_zip() -> None:
    item = _item()
    item["building_address"] = "999 Jimmy Carter Blvd"

    assert not recovery.property_scope_matches(
        _ctx(_html(_CLIENT_KRC, [_FP_KRC])),
        item,
    )


def test_target_url_never_uses_portfolio_wide_client_ids() -> None:
    url = recovery._target_url(_CLIENT_KRC, _FP_KRC)
    query = parse_qs(urlsplit(url).query)

    assert "client_ids" not in query
    assert "client_ids[]" not in query
    assert query == {
        "client_uuid": [_CLIENT_KRC],
        "floorplan_uuid": [_FP_KRC],
        "is_available": ["true"],
    }


def test_merge_drops_only_conflicting_identity() -> None:
    ctx = _ctx(_html(_CLIENT_KRC, [_FP_KRC]))
    source_url = recovery._target_url(_CLIENT_KRC, _FP_KRC)
    first, sibling = recovery.parse_betternoi_payload(
        {
            "results": [
                _item(),
                _item(
                    unit_uuid="901ba3b2-e50d-4639-b9c5-bc1db00b5d5d",
                    unit_number="821",
                    unit_identifier="08-821",
                ),
            ]
        },
        ctx=ctx,
        client_uuid=_CLIENT_KRC,
        floorplan_uuid=_FP_KRC,
        source_url=source_url,
    )
    conflict = dict(first)
    conflict["market_rent_low"] = 9999

    assert recovery._merge_strict_rows([first, first, sibling, conflict]) == [sibling]


class _FakeClient:
    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_fetch_target_requires_complete_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    first_url = recovery._target_url(_CLIENT_KRC, _FP_KRC)
    second_url = f"{first_url}&page=2"

    async def fake_fetch(
        _client: object,
        url: str,
    ) -> tuple[int, dict[str, Any]]:
        calls.append(url)
        if url == first_url:
            return 200, {"count": 2, "next": second_url, "results": [{}]}
        return 200, {"count": 2, "next": None, "results": [{}]}

    monkeypatch.setattr(recovery, "_fetch_public_json", fake_fetch)

    observations, complete = await recovery._fetch_target(
        object(),
        _CLIENT_KRC,
        _FP_KRC,
    )

    assert complete is True
    assert calls == [first_url, second_url]
    assert len(observations) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        {
            "count": 2,
            "next": "https://ares.betternoi.com/api/pub/v1/client/building/unit/?client_ids=12345",
            "results": [{}],
        },
        {"count": 39_710, "next": None, "results": [{}]},
        {"count": 2, "next": None, "results": [{}]},
    ),
    ids=("foreign-next-query", "portfolio-sized-count", "truncated-without-next"),
)
async def test_fetch_target_rejects_incomplete_or_portfolio_payload(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    async def fake_fetch(
        _client: object,
        _url: str,
    ) -> tuple[int, dict[str, Any]]:
        return 200, payload

    monkeypatch.setattr(recovery, "_fetch_public_json", fake_fetch)

    _, complete = await recovery._fetch_target(object(), _CLIENT_KRC, _FP_KRC)

    assert complete is False


@pytest.mark.asyncio
async def test_recovery_is_direct_bounded_and_property_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_kwargs: dict[str, Any] = {}
    urls: list[str] = []
    second_fp = "6ca29819-c60d-44e1-ace3-df83f85e253b"

    def fake_client(**kwargs: Any) -> _FakeClient:
        client_kwargs.update(kwargs)
        return _FakeClient()

    async def fake_fetch(
        _client: object,
        url: str,
    ) -> tuple[int, dict[str, Any]]:
        urls.append(url)
        query = parse_qs(urlsplit(url).query)
        floorplan_uuid = query["floorplan_uuid"][0]
        if floorplan_uuid == _FP_KRC:
            item = _item(floorplan_uuid=floorplan_uuid)
        else:
            item = _item(
                floorplan_uuid=floorplan_uuid,
                unit_uuid="901ba3b2-e50d-4639-b9c5-bc1db00b5d5d",
                unit_number="821",
                unit_identifier="08-821",
            )
        return 200, {"count": 1, "next": None, "results": [item]}

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(recovery, "_fetch_public_json", fake_fetch)

    units = await recovery.recover_betternoi_units(_ctx(_html(_CLIENT_KRC, [_FP_KRC, second_fp, _FP_KRC])))

    assert {unit["unit_number"] for unit in units} == {"01-122", "08-821"}
    assert len(urls) == 2
    for url in urls:
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        assert parts.scheme == "https"
        assert parts.netloc == "ares.betternoi.com"
        assert parts.path == "/api/pub/v1/client/building/unit/"
        assert query["client_uuid"] == [_CLIENT_KRC]
        assert query["is_available"] == ["true"]
    assert client_kwargs["trust_env"] is False
    assert client_kwargs["follow_redirects"] is False
    assert "User-Agent" not in client_kwargs["headers"]
    assert client_kwargs["headers"]["Referer"] == "https://www.krcreserveapts.com/"
    assert client_kwargs["limits"].max_connections == recovery._FETCH_CONCURRENCY


@pytest.mark.asyncio
async def test_contact_only_negative_performs_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_client(**_kwargs: Any) -> object:
        raise AssertionError("HTTP client must not be constructed without UUID pairs")

    monkeypatch.setattr(httpx, "AsyncClient", fail_client)
    html = """
        <a href="/contact/">Contact for Availability</a>
        <script>
        var url = "https://ares.betternoi.com/api/pub/v1/client/building/unit?client_uuid=";
        </script>
    """

    assert await recovery.recover_betternoi_units(_ctx(html)) == []


@pytest.mark.asyncio
async def test_bounded_json_reader_rejects_oversized_response() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(recovery._MAX_RESPONSE_BYTES + 1)},
            content=b"{}",
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as client:
        status, payload = await recovery._fetch_public_json(
            client,
            recovery._target_url(_CLIENT_KRC, _FP_KRC),
        )

    assert status == 200
    assert payload is None
