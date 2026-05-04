"""F4 — propertyId resolver tests.

Mocks ``httpx.AsyncClient.get`` to return canned JSON payloads; no
network is touched. Six tests cover EXACT, ZIP_AND_NAME_PREFIX,
ZIP_ONLY, NO_RESULTS, NO_ZIP_MATCH, vanity_domain tie-breaking, and
HTTP / parse error classification.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from ma_poc.pms.rentcafe_direct.propertyid_resolver import (
    ResolveResult,
    resolve_property_id,
)


def _client_returning(payload: object, status: int = 200) -> AsyncMock:
    """Build an ``httpx.AsyncClient``-shaped mock whose ``.get`` returns *payload*."""
    response = MagicMock()
    response.status_code = status
    response.json = MagicMock(return_value=payload)
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.aclose = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_f4_exact_match() -> None:
    payload = {
        "results": [
            {
                "property_id": "1234567",
                "name": "Hampshire Village",
                "zip": "94107",
                "vanity_domain": "hampshire.rentcafe.com",
            }
        ]
    }
    client = _client_returning(payload)
    result = await resolve_property_id(
        property_name="Hampshire Village",
        city="San Francisco",
        zip_code="94107",
        vanity_domain=None,
        client=client,
    )
    assert isinstance(result, ResolveResult)
    assert result.property_id == "1234567"
    assert result.confidence == "EXACT"
    assert result.failure_reason is None


@pytest.mark.asyncio
async def test_f4_zip_disambiguates_same_name_across_zips() -> None:
    """Two properties named 'Park Place' in different zips — match by zip."""
    payload = {
        "results": [
            {"property_id": "111", "name": "Park Place", "zip": "94107"},
            {"property_id": "222", "name": "Park Place", "zip": "60601"},
        ]
    }
    client = _client_returning(payload)
    result = await resolve_property_id(
        property_name="Park Place",
        city="Chicago",
        zip_code="60601",
        client=client,
    )
    assert result.property_id == "222"
    assert result.confidence == "EXACT"


@pytest.mark.asyncio
async def test_f4_no_results() -> None:
    client = _client_returning({"results": []})
    result = await resolve_property_id(
        property_name="Nowhere",
        city="Nowhere",
        zip_code="00000",
        client=client,
    )
    assert result.property_id is None
    assert result.confidence == "NONE"
    assert result.failure_reason == "NO_RESULTS"


@pytest.mark.asyncio
async def test_f4_no_zip_match() -> None:
    """Candidates exist but none share the target zip."""
    payload = {
        "results": [
            {"property_id": "111", "name": "Hampshire Village", "zip": "60601"},
        ]
    }
    client = _client_returning(payload)
    result = await resolve_property_id(
        property_name="Hampshire Village",
        city="San Francisco",
        zip_code="94107",
        client=client,
    )
    assert result.property_id is None
    assert result.failure_reason == "NO_ZIP_MATCH"


@pytest.mark.asyncio
async def test_f4_vanity_domain_tie_breaks_prefix_collision() -> None:
    """Two same-zip prefix matches — vanity_domain tie-breaker selects one."""
    payload = {
        "results": [
            {
                "property_id": "111",
                "name": "Cloister Gardens North",
                "zip": "94107",
                "vanity_domain": "cloistergardens-north.securecafe.com",
            },
            {
                "property_id": "222",
                "name": "Cloister Gardens South",
                "zip": "94107",
                "vanity_domain": "cloistergardens-south.securecafe.com",
            },
        ]
    }
    client = _client_returning(payload)
    result = await resolve_property_id(
        property_name="Cloister Gardens",
        city="San Francisco",
        zip_code="94107",
        vanity_domain="cloistergardens-south.securecafe.com",
        client=client,
    )
    assert result.property_id == "222"
    assert result.confidence == "ZIP_AND_NAME_PREFIX"


@pytest.mark.asyncio
async def test_f4_http_500_classified() -> None:
    response = MagicMock()
    response.status_code = 500
    response.json = MagicMock(return_value=None)
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.aclose = AsyncMock()
    result = await resolve_property_id(
        property_name="Anything",
        city="Anywhere",
        zip_code="00000",
        client=client,
    )
    assert result.property_id is None
    assert result.failure_reason == "SEARCH_HTTP_ERROR_500"


@pytest.mark.asyncio
async def test_f4_network_error_classified() -> None:
    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.NetworkError("connection reset"))
    client.aclose = AsyncMock()
    result = await resolve_property_id(
        property_name="Anything",
        city="Anywhere",
        zip_code="00000",
        client=client,
    )
    assert result.property_id is None
    assert result.failure_reason == "SEARCH_NETWORK_ERROR"


@pytest.mark.asyncio
async def test_f4_zip_only_when_single_same_zip_no_name_match() -> None:
    """One same-zip candidate with non-matching name → ZIP_ONLY."""
    payload = {
        "results": [
            {"property_id": "999", "name": "Some Other Property", "zip": "94107"},
        ]
    }
    client = _client_returning(payload)
    result = await resolve_property_id(
        property_name="Hampshire Village",
        city="San Francisco",
        zip_code="94107",
        client=client,
    )
    assert result.property_id == "999"
    assert result.confidence == "ZIP_ONLY"
