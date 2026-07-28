"""Tests for the property-sticky ProspectPortal warm/replay helper."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from ma_poc.pms.adapters._prospectportal_warm_replay import (
    ProspectPortalWarmReplayRequest,
    discover_endpoint_template,
    expand_endpoint_template,
    extract_floorplan_ids,
    replay_with_client,
    revalidate_dom_with_client,
    strict_unit_rent_rows,
    summarize_public_plan_pricing,
)

_WARM_URL = "https://example.prospectportal.com/austin/example/conventional/"
_TEMPLATE = (
    "https://example.prospectportal.com/?module=check_availability&is_secure=1"
    "&property[id]=10&action=view_unit_spaces"
    "&property_floorplan[id]={floorplan_id}&move_in_date={move_in_date}"
    "&number_of_bedrooms={number_of_bedrooms}&occupancy_type=conventional"
)


class _Response:
    """Small adapter-compatible test response."""

    def __init__(self, status_code: int, content: str) -> None:
        self.status_code = status_code
        self.content = content.encode()


class _StickyClient:
    """Records calls so tests assert warm-before-XHR on the same client."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        cookies: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> _Response:
        self.calls.append((method, url, headers))
        if url == _WARM_URL:
            return _Response(200, 'data-floorplan="1071063" data-floorplan="1071065"')
        return _Response(200, "unit-fragment")

    async def aclose(self) -> None:
        """Satisfy the replay-client protocol."""


def test_extract_floorplan_ids_dedupes_and_respects_cap() -> None:
    """Floor-plan expansion is bounded and page-ordered."""
    html = (
        'property_floorplan[id]=1111 data-floorplan="2222" '
        'property_floorplan[id]=1111 data-floorplan="3333"'
    )
    assert extract_floorplan_ids(html, max_floorplans=2) == ["1111", "2222"]


def test_expand_template_runtime_date_and_same_origin_only() -> None:
    """Saved templates receive date at replay time and cannot change origin."""
    expanded = expand_endpoint_template(
        _TEMPLATE,
        warm_page_url=_WARM_URL,
        floorplan_id="1071065",
        move_in_date=date(2026, 7, 26),
    )
    assert expanded is not None
    assert "property_floorplan[id]=1071065" in expanded
    assert "move_in_date=2026-07-26" in expanded
    assert "number_of_bedrooms=" in expanded
    assert expand_endpoint_template(
        _TEMPLATE.replace("example.prospectportal.com", "elsewhere.test", 1),
        warm_page_url=_WARM_URL,
        floorplan_id="1071065",
        move_in_date=date(2026, 7, 26),
    ) is None


def test_expand_template_rejects_unknown_placeholder() -> None:
    """Only dynamic values understood by the replayer can be expanded."""
    assert expand_endpoint_template(
        _TEMPLATE + "&secret={cookie}",
        warm_page_url=_WARM_URL,
        floorplan_id="1071065",
        move_in_date=date(2026, 7, 26),
    ) is None


def test_discover_template_uses_only_runtime_values() -> None:
    """A warm grid yields a reusable template, never a frozen date or cookie."""
    template = discover_endpoint_template(
        _WARM_URL,
        'property[id]=1108495 data-floorplan="1071065"',
    )
    assert template is not None
    assert "property[id]=1108495" in template
    assert "property_floorplan[id]={floorplan_id}" in template
    assert "move_in_date={move_in_date}" in template


def test_strict_rows_exclude_plan_rows_and_non_numeric_rents() -> None:
    """A result is not verified without a real identifier and numeric rent."""
    rows: list[dict[str, Any]] = [
        {"floor_plan_name": "A1", "market_rent_low": 1200},
        {"unit_number": "inferred_a1", "market_rent_low": 1200},
        {"unit_number": "101", "market_rent_low": "1200"},
        {"unit_number": "102", "market_rent_low": 1250.0},
    ]
    assert strict_unit_rent_rows(rows) == [rows[-1]]


def test_public_plan_pricing_is_logged_but_never_promoted_to_a_unit() -> None:
    """Plan-card rents remain a separate public-pricing observation."""
    summary = summarize_public_plan_pricing(
        [
            {"floor_plan_name": "A1", "market_rent_low": 1200, "market_rent_high": 1300},
            {"floor_plan_name": "B1", "sqft": "800"},
            {"unit_number": "101", "market_rent_low": 1400},
        ]
    )
    assert summary.plans_observed == 2
    assert summary.plans_with_numeric_price == 1
    assert summary.price_low == 1200
    assert summary.price_high == 1300
    assert summary.status == "PLAN_RECORDS_WITH_LISTED_PRICE"


@pytest.mark.asyncio
async def test_replay_warms_then_replays_on_one_client() -> None:
    """The XHR shares the warm client and only strict rows set verified."""
    client = _StickyClient()
    request = ProspectPortalWarmReplayRequest(
        property_id="43", warm_page_url=_WARM_URL, endpoint_template=_TEMPLATE
    )

    async def no_sleep(_: float) -> None:
        return None

    result = await replay_with_client(
        request,
        client,
        replay_date=date(2026, 7, 26),
        sleep=no_sleep,
        inter_request_delay_s=0,
        parser=lambda _html, _url: [
            {"floor_plan_name": "A1", "market_rent_low": 1200},
            {"unit_number": "D205", "market_rent_low": 800.0},
        ],
        plan_parser=lambda _html, _url: [
            {"floor_plan_name": "A1", "market_rent_low": 1200}
        ],
    )
    assert result.verified is True
    assert [call[1] for call in client.calls][0] == _WARM_URL
    assert all(call[0] == "GET" for call in client.calls)
    assert all("X-Requested-With" in call[2] for call in client.calls[1:])
    assert len(client.calls) == 2  # warm page + one strict endpoint response
    assert result.verified_rows[0]["unit_number"] == "D205"
    assert result.discovered_endpoint_template == _TEMPLATE
    assert result.public_plan_pricing.status == "PLAN_RECORDS_WITH_LISTED_PRICE"
    assert result.public_plan_pricing.price_low == 1200


@pytest.mark.asyncio
async def test_replay_discovers_template_from_warm_page() -> None:
    """A cold warm-page candidate can create a template only after replay."""
    client = _StickyClient()
    request = ProspectPortalWarmReplayRequest(property_id="43", warm_page_url=_WARM_URL)

    async def no_sleep(_: float) -> None:
        return None

    # Add the public property id needed by the discovery function to the
    # warm fixture without baking a date into any durable template.
    async def request_with_property_id(*args: Any, **kwargs: Any) -> _Response:
        response = await _StickyClient.request(client, *args, **kwargs)
        if args[1] == _WARM_URL:
            return _Response(200, 'property[id]=1108495 data-floorplan="1071063"')
        return response

    client.request = request_with_property_id  # type: ignore[method-assign]
    result = await replay_with_client(
        request,
        client,
        replay_date=date(2026, 7, 26),
        sleep=no_sleep,
        inter_request_delay_s=0,
        parser=lambda _html, _url: [{"unit_number": "D205", "market_rent_low": 800}],
    )
    assert result.verified is True
    assert result.discovered_endpoint_template is not None


@pytest.mark.asyncio
async def test_dom_revalidation_keeps_warm_and_proof_page_distinct() -> None:
    """A fresh per-plan DOM row is strict proof, not an API claim."""
    plan_url = "https://example.prospectportal.com/floorplans/a1-1071065-1/"

    class _DomClient(_StickyClient):
        async def request(
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str],
            cookies: dict[str, str] | None = None,
            timeout: float = 30.0,
        ) -> _Response:
            self.calls.append((method, url, headers))
            return _Response(200, "warm") if url == _WARM_URL else _Response(200, "unit-card")

    async def no_sleep(_: float) -> None:
        return None

    client = _DomClient()
    result = await revalidate_dom_with_client(
        ProspectPortalWarmReplayRequest(property_id="43", warm_page_url=_WARM_URL),
        client,
        plan_link_parser=lambda _html, _origin: [plan_url],
        unit_parser=lambda _html, _url: [
            {"unit_number": "110", "market_rent_low": 1245}
        ],
        sleep=no_sleep,
        inter_request_delay_s=0,
    )
    assert result.verified is True
    assert result.unit_page_url == plan_url
    assert result.plan_page_statuses == (200,)
