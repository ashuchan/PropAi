"""F5 — direct fetcher for the RentCafe aggregator API.

Given a propertyId, calls the RentCafe centralized floorplans endpoint
and returns the response in the same envelope shape ``_api_responses``
elsewhere in the pipeline uses. The existing :class:`RentCafeAdapter`
parses the body unchanged — H7's invariant. This module imports the
shape-detection helpers from the existing adapter so the direct path
agrees byte-for-byte with the vanity-domain path on what counts as
"RentCafe-shaped".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from ma_poc.pms.adapters.rentcafe import _is_rentcafe_response, _unwrap_rentcafe_list

# UPDATE from Phase 0 research before merging. The propertyId list
# parameter `[]=` syntax is RentCafe's documented WP-JSON convention
# for getFloorplans — passed through to httpx as a literal URL since
# brackets are reserved characters and httpx percent-encodes them when
# they're sent through ``params=``.
_AGGREGATOR_FLOORPLANS_URL = (
    "https://www.rentcafe.com/wp-json/middleware/v1/getFloorplans/?propertyId%5B%5D={pid}"
)

# H8 — exact failure-mode tier strings. Drift checklist (§8.5) verifies
# this frozenset matches the spec count + names exactly.
TIER_CODES: frozenset[str] = frozenset(
    {
        "TIER_1_API_RENTCAFE_DIRECT",
        "TIER_1_API_RENTCAFE_DIRECT_LIST_EMPTY",
        "TIER_1_API_RENTCAFE_DIRECT_NO_RESPONSE",
        "TIER_1_API_RENTCAFE_DIRECT_SHAPE_REJECTED",
        "TIER_1_API_RENTCAFE_DIRECT_PROPERTY_ID_RESOLUTION_FAILED",
        "TIER_1_API_RENTCAFE_DIRECT_PROPERTY_ID_MISMATCH",
    }
)


@dataclass(frozen=True)
class DirectFetchResult:
    """Outcome of a single direct-path fetch.

    Attributes
    ----------
    property_id
        The propertyId we attempted to fetch.
    api_responses
        List of ``{url, body, status, headers}`` dicts in the shape the
        existing RentCafeAdapter expects. Populated even on
        ``LIST_EMPTY`` (empty IS the answer — see spec §5/F5).
    tier_code
        One of :data:`TIER_CODES`.
    tier_message
        Machine-readable failure detail (None on success).
    elapsed_ms
        Wall-clock time spent on the fetch.
    """

    property_id: str
    api_responses: list[dict[str, Any]]
    tier_code: str
    tier_message: str | None
    elapsed_ms: int


async def fetch_direct(
    property_id: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = 20.0,
) -> DirectFetchResult:
    """Fetch RentCafe floorplans directly by propertyId.

    Parameters
    ----------
    property_id : str
        Resolved or cached RentCafe propertyId.
    client : httpx.AsyncClient | None
        Optional reusable client (test injection). When None a single-
        shot client is created and closed in the ``finally``.
    timeout_s : float
        HTTP timeout.

    Returns
    -------
    DirectFetchResult
        Always returns; never raises. ``tier_code`` is in
        :data:`TIER_CODES`.
    """
    started = time.monotonic()
    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_s, follow_redirects=True)
    url = _AGGREGATOR_FLOORPLANS_URL.format(pid=property_id)

    def _ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        try:
            # Per-shared-host politeness gate: www.rentcafe.com is a single
            # shared aggregator host across all RentCafe-direct properties.
            from ma_poc.fetch.host_throttle import async_throttle
            async with async_throttle(url):
                resp = await client.get(url)
        except (httpx.NetworkError, httpx.TimeoutException) as e:
            return DirectFetchResult(
                property_id,
                [],
                "TIER_1_API_RENTCAFE_DIRECT_NO_RESPONSE",
                f"network_error: {type(e).__name__}",
                _ms(),
            )
        except Exception as e:
            # Defensive — any unanticipated client error becomes
            # NO_RESPONSE so the runner falls back (H4).
            return DirectFetchResult(
                property_id,
                [],
                "TIER_1_API_RENTCAFE_DIRECT_NO_RESPONSE",
                f"unexpected_error: {type(e).__name__}",
                _ms(),
            )

        if resp.status_code != 200:
            return DirectFetchResult(
                property_id,
                [],
                "TIER_1_API_RENTCAFE_DIRECT_NO_RESPONSE",
                f"http_{resp.status_code}",
                _ms(),
            )

        try:
            body = resp.json()
        except Exception:
            return DirectFetchResult(
                property_id,
                [],
                "TIER_1_API_RENTCAFE_DIRECT_SHAPE_REJECTED",
                "non_json",
                _ms(),
            )

        if not _is_rentcafe_response(body):
            return DirectFetchResult(
                property_id,
                [],
                "TIER_1_API_RENTCAFE_DIRECT_SHAPE_REJECTED",
                "envelope_did_not_match",
                _ms(),
            )

        items = _unwrap_rentcafe_list(body) or []
        api_response: dict[str, Any] = {
            "url": url,
            "body": body,
            "status": 200,
            "headers": dict(resp.headers),
        }

        if not items:
            # Legitimate empty — populate api_responses so the parser
            # sees the empty list (see §9 anti-scope: "empty IS the
            # answer"). The runner still treats LIST_EMPTY as a success
            # tier and short-circuits the vanity-domain fallback.
            return DirectFetchResult(
                property_id,
                [api_response],
                "TIER_1_API_RENTCAFE_DIRECT_LIST_EMPTY",
                "floorplans_list_empty",
                _ms(),
            )

        return DirectFetchResult(
            property_id,
            [api_response],
            "TIER_1_API_RENTCAFE_DIRECT",
            None,
            _ms(),
        )
    finally:
        if own_client:
            await client.aclose()
