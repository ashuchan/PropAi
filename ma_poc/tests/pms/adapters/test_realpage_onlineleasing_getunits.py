"""Strict public-roster recovery for numeric RealPage Online Leasing roots."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.onesite import OneSiteAdapter
from ma_poc.pms.adapters.realpage_oll import (
    ONLINELEASING_GETUNITS_TIER,
    RealPageOllAdapter,
    _direct_onlineleasing_get,
    _onlineleasing_getunits_url,
    onlineleasing_roots_from_ctx,
    parse_scoped_onlineleasing_getunits,
    recover_onlineleasing_getunits,
)
from ma_poc.pms.detector import detect_pms

_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "realpage_oll"
    / "onlineleasing_getunits_examples.json"
)
_EXAMPLES: dict[str, dict[str, Any]] = json.loads(_FIXTURE.read_text())


def _ctx(
    base_url: str,
    *,
    body: bytes | None = None,
    responses: list[dict[str, Any]] | None = None,
) -> AdapterContext:
    ctx = AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P1",
        fetch_result=SimpleNamespace(final_url=base_url, body=body),
    )
    ctx._api_responses = responses or []  # type: ignore[attr-defined]
    return ctx


@pytest.mark.parametrize(
    ("root_id", "unit_number", "rent"),
    [
        ("8095670", "1295", 2221),
        ("9146180", "0844", 1789),
        ("6189022", "121", 1350),
    ],
)
def test_three_live_cohort_payloads_are_strict_units(
    root_id: str,
    unit_number: str,
    rent: int,
) -> None:
    """Captured rows from three exact-cohort portals prove the shared shape."""
    rows = parse_scoped_onlineleasing_getunits(
        json.dumps(_EXAMPLES[root_id]),
        _onlineleasing_getunits_url(root_id),
        root_id,
    )

    assert len(rows) == 1
    assert rows[0]["unit_number"] == unit_number
    assert rows[0]["market_rent_low"] == rent
    assert rows[0]["extraction_tier"] == ONLINELEASING_GETUNITS_TIER
    assert unit_has_real_anchor(rows[0])


def test_root_discovery_handles_current_and_escaped_urls_and_is_bounded() -> None:
    ctx = _ctx(
        "https://8095670.onlineleasing.realpage.com/",
        body=(
            b'<a href="https:\\/\\/9146180.onlineleasing.realpage.com/">A</a>'
            b'<a href="https://6189022.onlineleasing.realpage.com/">B</a>'
            b'<a href="https://9203612.onlineleasing.realpage.com/">capped</a>'
            b'<a href="https://abc.onlineleasing.realpage.com/">invalid</a>'
        ),
    )

    assert onlineleasing_roots_from_ctx(ctx) == ["8095670", "9146180", "6189022"]


def test_parser_rejects_cross_property_payload() -> None:
    """The payload propertyId must equal the numeric portal host."""
    rows = parse_scoped_onlineleasing_getunits(
        json.dumps(_EXAMPLES["9146180"]),
        _onlineleasing_getunits_url("8095670"),
        "8095670",
    )
    assert rows == []


def test_parser_rejects_leased_rentless_and_floorplan_rows() -> None:
    payload = {
        "units": [
            {
                "propertyId": 8095670,
                "unitNumber": "LEASED-1",
                "leaseStatus": "LEASED",
                "rent": 1800,
                "squareFeet": 700,
                "numberOfBeds": 1,
            },
            {
                "propertyId": 8095670,
                "unitNumber": "NO-RENT",
                "leaseStatus": "AVAILABLE_READY",
                "rent": 0,
                "squareFeet": 700,
                "numberOfBeds": 1,
            },
            {
                "propertyId": 8095670,
                "floorplanId": 123,
                "leaseStatus": "AVAILABLE_READY",
                "rent": 1800,
                "squareFeet": 700,
                "numberOfBeds": 1,
            },
        ]
    }

    assert (
        parse_scoped_onlineleasing_getunits(
            json.dumps(payload),
            _onlineleasing_getunits_url("8095670"),
            "8095670",
        )
        == []
    )


class _FakeHttpResponse:
    def __init__(
        self,
        *,
        status: int,
        url: str,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status
        self.url = url
        self.headers = headers or {}
        self.encoding = "utf-8"
        self._body = body

    async def aiter_bytes(self):  # type: ignore[no-untyped-def]
        yield self._body


class _FakeStreamContext:
    def __init__(self, response: _FakeHttpResponse) -> None:
        self.response = response

    async def __aenter__(self) -> _FakeHttpResponse:
        return self.response

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeAsyncClient:
    def __init__(self, responses: list[_FakeHttpResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def stream(self, method: str, url: str) -> _FakeStreamContext:
        self.calls.append((method, url))
        return _FakeStreamContext(self.responses.pop(0))


@pytest.mark.asyncio
async def test_direct_http_disables_env_proxy_and_bounds_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    root_id = "8095670"
    start_url = _onlineleasing_getunits_url(root_id)
    redirected_url = f"https://{root_id}.onlineleasing.realpage.com/roster.json"
    body = json.dumps(_EXAMPLES[root_id]).encode()
    client = _FakeAsyncClient(
        [
            _FakeHttpResponse(
                status=302,
                url=start_url,
                headers={"location": "/roster.json"},
            ),
            _FakeHttpResponse(status=200, url=redirected_url, body=body),
        ]
    )
    options: dict[str, Any] = {}

    def client_factory(**kwargs: Any) -> _FakeAsyncClient:
        options.update(kwargs)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    status, response_body, final_url = await _direct_onlineleasing_get(
        start_url,
        root_id,
    )

    assert status == 200
    assert json.loads(response_body) == _EXAMPLES[root_id]
    assert final_url == redirected_url
    assert options["trust_env"] is False
    assert options["follow_redirects"] is False
    assert isinstance(options["timeout"], httpx.Timeout)
    assert client.calls == [("GET", start_url), ("GET", redirected_url)]


@pytest.mark.asyncio
async def test_direct_http_rejects_oversized_body_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    root_id = "8095670"
    url = _onlineleasing_getunits_url(root_id)
    client = _FakeAsyncClient(
        [
            _FakeHttpResponse(
                status=200,
                url=url,
                body=b"must-not-be-used",
                headers={"content-length": "2000001"},
            )
        ]
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)

    assert await _direct_onlineleasing_get(url, root_id) == (200, "", url)


@pytest.mark.asyncio
async def test_recovery_is_direct_bounded_and_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ma_poc.config.feature_flags as flags
    import ma_poc.pms.adapters.realpage_oll as oll_module

    calls: list[tuple[str, str]] = []

    async def fake_get(url: str, root_id: str) -> tuple[int, str, str]:
        calls.append((url, root_id))
        return (
            200,
            json.dumps(_EXAMPLES["8095670"]),
            url,
        )

    monkeypatch.setattr(flags, "enable_cws_getunits", lambda: True)
    monkeypatch.setattr(oll_module, "_direct_onlineleasing_get", fake_get)

    result = await recover_onlineleasing_getunits(
        _ctx("https://8095670.onlineleasing.realpage.com/")
    )

    assert result is not None
    assert result.tier_used == ONLINELEASING_GETUNITS_TIER
    assert len(result.units) == 1
    assert result.units[0]["unit_number"] == "1295"
    assert calls == [
        (
            _onlineleasing_getunits_url("8095670"),
            "8095670",
        )
    ]


@pytest.mark.asyncio
async def test_recovery_flag_off_never_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    import ma_poc.config.feature_flags as flags
    import ma_poc.pms.adapters.realpage_oll as oll_module

    monkeypatch.setattr(flags, "enable_cws_getunits", lambda: False)

    async def unexpected_get(*args: Any, **kwargs: Any) -> tuple[int, str, str]:
        raise AssertionError("flag-off recovery must not make a request")

    monkeypatch.setattr(oll_module, "_direct_onlineleasing_get", unexpected_get)
    assert (
        await recover_onlineleasing_getunits(
            _ctx("https://8095670.onlineleasing.realpage.com/")
        )
        is None
    )


@pytest.mark.asyncio
async def test_recovery_isolates_first_root_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ma_poc.config.feature_flags as flags
    import ma_poc.pms.adapters.realpage_oll as oll_module

    calls: list[str] = []

    async def fake_get(url: str, root_id: str) -> tuple[int, str, str]:
        calls.append(url)
        if "8095670.onlineleasing" in url:
            raise RuntimeError("transient transport failure")
        return (
            200,
            json.dumps(_EXAMPLES["9146180"]),
            url,
        )

    monkeypatch.setattr(flags, "enable_cws_getunits", lambda: True)
    monkeypatch.setattr(oll_module, "_direct_onlineleasing_get", fake_get)
    ctx = _ctx(
        "https://example.test/",
        body=(
            b"https://8095670.onlineleasing.realpage.com/ "
            b"https://9146180.onlineleasing.realpage.com/"
        ),
    )

    result = await recover_onlineleasing_getunits(ctx)

    assert result is not None
    assert result.units[0]["unit_number"] == "0844"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_onesite_native_unit_response_keeps_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ma_poc.pms.adapters.realpage_oll as oll_module

    async def unexpected_recovery(ctx: AdapterContext) -> AdapterResult | None:
        raise AssertionError("native canonical+rent units must stay first")

    monkeypatch.setattr(
        oll_module,
        "recover_onlineleasing_getunits",
        unexpected_recovery,
    )
    response = {
        "url": "https://api.ws.realpage.com/v2/property/8095670/units",
        "body": {
            "response": [
                {
                    "id": 1,
                    "unitNumber": "A-101",
                    "minRent": 1800,
                    "maxRent": 1800,
                    "sqft": 720,
                    "bedRooms": 1,
                    "bathRooms": 1,
                }
            ]
        },
    }
    result = await OneSiteAdapter().extract(
        SimpleNamespace(),
        _ctx(
            "https://8095670.onlineleasing.realpage.com/",
            responses=[response],
        ),
    )

    assert result.tier_used == "TIER_1_API_ONESITE"
    assert result.units[0]["unit_number"] == "A-101"


@pytest.mark.asyncio
async def test_onesite_plan_catalogue_upgrades_and_preserves_plans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ma_poc.pms.adapters.realpage_oll as oll_module

    strict_rows = parse_scoped_onlineleasing_getunits(
        json.dumps(_EXAMPLES["8095670"]),
        _onlineleasing_getunits_url("8095670"),
        "8095670",
    )

    async def fake_recovery(ctx: AdapterContext) -> AdapterResult:
        return AdapterResult(
            units=strict_rows,
            tier_used=ONLINELEASING_GETUNITS_TIER,
            winning_url=_onlineleasing_getunits_url("8095670"),
            confidence=0.9,
        )

    monkeypatch.setattr(
        oll_module,
        "recover_onlineleasing_getunits",
        fake_recovery,
    )
    response = {
        "url": "https://api.ws.realpage.com/v2/property/8095670/floorplans",
        "body": {
            "response": {
                "floorplans": [
                    {
                        "id": "PLAN-A",
                        "name": "A1",
                        "bedRooms": "1",
                        "bathRooms": "1",
                        "minimumSquareFeet": "720",
                        "maximumSquareFeet": "720",
                        "minimumMarketRent": 1700,
                        "maximumMarketRent": 1800,
                    }
                ]
            }
        },
    }

    result = await OneSiteAdapter().extract(
        SimpleNamespace(),
        _ctx(
            "https://8095670.onlineleasing.realpage.com/",
            responses=[response],
        ),
    )

    assert result.tier_used == ONLINELEASING_GETUNITS_TIER
    assert [row["unit_number"] for row in result.units] == ["1295"]
    assert len(result.plan_summaries) == 1
    assert result.plan_summaries[0]["unit_number"] == ""
    assert result.plan_summaries[0]["source_ids"]["floorplan_id"] == "PLAN-A"


@pytest.mark.asyncio
async def test_realpage_oll_plan_workflow_upgrades_and_preserves_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ma_poc.pms.adapters.realpage_oll as oll_module

    strict_rows = parse_scoped_onlineleasing_getunits(
        json.dumps(_EXAMPLES["8095670"]),
        _onlineleasing_getunits_url("8095670"),
        "8095670",
    )

    async def fake_recovery(ctx: AdapterContext) -> AdapterResult:
        return AdapterResult(
            units=strict_rows,
            tier_used=ONLINELEASING_GETUNITS_TIER,
        )

    monkeypatch.setattr(
        oll_module,
        "recover_onlineleasing_getunits",
        fake_recovery,
    )
    plan_workflow = {
        "Workflow": {
            "ActivityGroups": [
                {
                    "GroupActivities": [
                        {
                            "__type": "ApartmentSelectionLeaseMgmtActivity",
                            "Floorplan": {
                                "Id": "FP-1",
                                "Name": "A1",
                                "Bedrooms": 1,
                                "Bathrooms": 1,
                                "MinSquareFeet": 720,
                                "MinPriceRange": 1700,
                                "MaxPriceRange": 1800,
                                "AvailableUnits": 0,
                            },
                            "Units": [],
                        }
                    ]
                }
            ]
        }
    }
    ctx = _ctx(
        "https://8095670.onlineleasing.realpage.com/",
        responses=[
            {
                "url": "https://leasing.realpage.com/appstate/v1/",
                "body": plan_workflow,
            }
        ],
    )

    result = await RealPageOllAdapter().extract(SimpleNamespace(), ctx)

    assert result.tier_used == ONLINELEASING_GETUNITS_TIER
    assert [row["unit_number"] for row in result.units] == ["1295"]
    assert len(result.plan_summaries) == 1
    assert result.plan_summaries[0]["unit_number"] == ""
    assert result.plan_summaries[0]["source_ids"]["floorplan_id"] == "FP-1"


@pytest.mark.asyncio
async def test_empty_onesite_uses_public_roster_before_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ma_poc.pms.adapters.onesite as onesite_module
    import ma_poc.pms.adapters.realpage_oll as oll_module

    strict_rows = parse_scoped_onlineleasing_getunits(
        json.dumps(_EXAMPLES["8095670"]),
        _onlineleasing_getunits_url("8095670"),
        "8095670",
    )

    async def fake_recovery(ctx: AdapterContext) -> AdapterResult:
        return AdapterResult(
            units=strict_rows,
            tier_used=ONLINELEASING_GETUNITS_TIER,
        )

    async def unexpected_workflow(ctx: AdapterContext) -> list[dict[str, Any]]:
        raise AssertionError("strict public roster must precede workflow probing")

    monkeypatch.setattr(
        oll_module,
        "recover_onlineleasing_getunits",
        fake_recovery,
    )
    monkeypatch.setattr(
        onesite_module,
        "_probe_onesite_workflowstartup",
        unexpected_workflow,
    )

    result = await OneSiteAdapter().extract(
        SimpleNamespace(),
        _ctx("https://8095670.onlineleasing.realpage.com/"),
    )
    assert result.tier_used == ONLINELEASING_GETUNITS_TIER
    assert result.units[0]["unit_number"] == "1295"
