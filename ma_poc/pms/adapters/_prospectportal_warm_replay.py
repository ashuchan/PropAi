"""Property-sticky ProspectPortal warm-page and availability-XHR replay.

Acceptance criteria (2026-07-26 endpoint-discovery recovery):
* Fetch the public warm page and every availability request in one
  property-sticky Bright Data residential HTTP session.
* Expand only runtime values (floor-plan id and move-in date); the durable
  endpoint template never contains cookies or a one-day date.
* Treat a replay as verified only when a single parsed row has both a real
  unit identifier and a numeric rent.
* Preserve separately whether the public warm page listed floor plans and
  transparent plan-level price(s); this is never represented as unit proof.
* Bound the per-property fan-out and never raise from a failed public replay.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol
from urllib.parse import urlparse

from ma_poc.fetch.headers import chrome_header_set
from ma_poc.fetch.http_client import make_http_client
from ma_poc.fetch.proxy.base import ProxyTier
from ma_poc.fetch.proxy.brightdata import BrightDataProvider
from ma_poc.fetch.stealth import IdentityPool
from ma_poc.models.fetch_tier import FetchTier

_FLOORPLAN_ID_RE = re.compile(
    r"""(?:property_floorplan(?:\\\[id\\\]|\[id\])|data-floorplan)
        [^0-9]{0,40}(\d{4,10})""",
    re.IGNORECASE | re.VERBOSE,
)
_PROPERTY_ID_RE = re.compile(
    r"""(?:property(?:\[id\]|%5B[idID]%5D)|data-property-id)
        [^0-9]{0,40}(\d{4,12})""",
    re.IGNORECASE | re.VERBOSE,
)
_ALLOWED_TEMPLATE_TOKENS = frozenset(
    {"floorplan_id", "move_in_date", "number_of_bedrooms"}
)
_TEMPLATE_TOKEN_RE = re.compile(r"\{([a-z_]+)\}")
_DEFAULT_MAX_FLOORPLANS = 8
_DEFAULT_MAX_DOM_PLAN_PAGES = 6
_RESIDENTIAL_INTER_REQUEST_DELAY_S = 2.0
_IDENTITIES = IdentityPool()


class _RequestClient(Protocol):
    """Minimum async HTTP client surface needed by the replay helper."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        cookies: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> Any:
        """Issue one request and return an adapter-compatible response."""

    async def aclose(self) -> None:
        """Close the client and discard its in-memory cookie jar."""


@dataclass(frozen=True, slots=True)
class ProspectPortalWarmReplayRequest:
    """Public, durable inputs for one ProspectPortal replay attempt.

    ``endpoint_template`` may contain only the three documented placeholders.
    It intentionally has no cookie fields and does not retain the date used by
    a previous replay.
    """

    property_id: str
    warm_page_url: str
    endpoint_template: str | None = None
    max_floorplans: int = _DEFAULT_MAX_FLOORPLANS


@dataclass(frozen=True, slots=True)
class PublicPlanPricingSummary:
    """Visible plan-level pricing observed on one public warm page.

    This deliberately describes floor-plan records only. It prevents a
    visible ``From $1,200`` from being confused with a concrete unit/rent row,
    while preserving an auditable answer to whether pricing was transparent.
    """

    plans_observed: int = 0
    plans_with_numeric_price: int = 0
    price_low: int | None = None
    price_high: int | None = None

    @property
    def status(self) -> str:
        """Return the explicit plan-pricing state for checkpoint readers."""
        if self.plans_observed == 0:
            return "NO_PUBLIC_PLAN_RECORDS"
        if self.plans_with_numeric_price == 0:
            return "PLAN_RECORDS_WITHOUT_LISTED_PRICE"
        return "PLAN_RECORDS_WITH_LISTED_PRICE"


@dataclass(frozen=True, slots=True)
class ProspectPortalWarmReplayResult:
    """Outcome of one bounded sticky-session ProspectPortal replay."""

    warm_status: int | None
    floorplans_discovered: int
    endpoint_statuses: tuple[int | None, ...]
    verified_rows: tuple[dict[str, Any], ...]
    discovered_endpoint_template: str | None = None
    error: str | None = None
    public_plan_pricing: PublicPlanPricingSummary = PublicPlanPricingSummary()

    @property
    def verified(self) -> bool:
        """Return whether this replay produced a strict unit-plus-rent row."""
        return bool(self.verified_rows)


@dataclass(frozen=True, slots=True)
class ProspectPortalDomRevalidationResult:
    """Fresh public per-plan DOM proof observed in the same sticky session."""

    warm_status: int | None
    plan_pages_discovered: int
    plan_page_statuses: tuple[int | None, ...]
    verified_rows: tuple[dict[str, Any], ...]
    unit_page_url: str | None = None
    error: str | None = None

    @property
    def verified(self) -> bool:
        """Return whether a freshly fetched DOM page has strict unit proof."""
        return bool(self.verified_rows)


def extract_floorplan_ids(warm_html: str, *, max_floorplans: int) -> list[str]:
    """Return distinct ProspectPortal floor-plan ids in page order.

    Args:
        warm_html: HTML returned by the public floor-plan or availability page.
        max_floorplans: Hard per-property fan-out cap. Values below one return
            no candidates.

    Returns:
        A bounded, de-duplicated list of numeric floor-plan identifiers.
    """
    if max_floorplans < 1 or not warm_html:
        return []
    found: list[str] = []
    for match in _FLOORPLAN_ID_RE.finditer(warm_html):
        floorplan_id = match.group(1)
        if floorplan_id not in found:
            found.append(floorplan_id)
        if len(found) >= max_floorplans:
            break
    return found


def expand_endpoint_template(
    endpoint_template: str,
    *,
    warm_page_url: str,
    floorplan_id: str,
    move_in_date: date,
) -> str | None:
    """Safely expand a saved public availability URL template at runtime.

    Args:
        endpoint_template: Previously verified endpoint pattern containing only
            approved placeholders.
        warm_page_url: Public page that seated the cookie-bearing session.
        floorplan_id: Runtime id extracted from that page.
        move_in_date: Runtime date; it is never stored in the profile template.

    Returns:
        The expanded same-origin URL, or ``None`` when the template is unsafe
        or incomplete.
    """
    tokens = set(_TEMPLATE_TOKEN_RE.findall(endpoint_template))
    if not tokens.issubset(_ALLOWED_TEMPLATE_TOKENS):
        return None
    values = {
        "floorplan_id": floorplan_id,
        "move_in_date": move_in_date.isoformat(),
        # The public endpoint accepts an empty value when its warm page does
        # not expose bedrooms. A successful returned row remains the gate.
        "number_of_bedrooms": "",
    }
    expanded = endpoint_template
    for token, value in values.items():
        expanded = expanded.replace("{" + token + "}", value)
    if _TEMPLATE_TOKEN_RE.search(expanded):
        return None

    try:
        endpoint = urlparse(expanded)
        warm = urlparse(warm_page_url)
    except ValueError:
        return None
    if (
        endpoint.scheme not in {"https", "http"}
        or endpoint.netloc.lower() != warm.netloc.lower()
    ):
        return None
    return expanded


def discover_endpoint_template(warm_page_url: str, warm_html: str) -> str | None:
    """Build the public ProspectPortal XHR template from a warmed grid page.

    Args:
        warm_page_url: The public, same-origin page that contains the plan
            grid and will become the XHR's Referer.
        warm_html: Its freshly retrieved HTML.

    Returns:
        A dynamic template with no cookies or static move-in date, or ``None``
        when the page lacks the property and floor-plan identifiers required to
        attempt a real unit request.
    """
    floorplan_ids = extract_floorplan_ids(warm_html, max_floorplans=1)
    property_match = _PROPERTY_ID_RE.search(warm_html)
    parsed = urlparse(warm_page_url)
    if not floorplan_ids or property_match is None or not parsed.scheme or not parsed.netloc:
        return None
    property_id = property_match.group(1)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return (
        f"{origin}/?module=check_availability&is_secure=1"
        f"&property[id]={property_id}&action=view_unit_spaces"
        "&property_floorplan[id]={floorplan_id}"
        "&number_of_bedrooms={number_of_bedrooms}"
        "&move_in_date={move_in_date}"
        "&selected_unit_space_amenities_ids=&occupancy_type=conventional"
        "&from_check_availability_filter=1"
    )


def strict_unit_rent_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only rows that meet the unit identity plus numeric-rent contract.

    Args:
        rows: Parsed response rows from one endpoint response.

    Returns:
        Rows with a non-empty concrete ``unit_number`` and a numeric
        ``market_rent_low`` or ``asking_rent`` value. Plan-only and inferred
        identifiers do not qualify.
    """
    verified: list[dict[str, Any]] = []
    for row in rows:
        unit_number = str(row.get("unit_number") or "").strip()
        if not unit_number or unit_number.startswith("inferred_"):
            continue
        rent = row.get("market_rent_low", row.get("asking_rent"))
        if isinstance(rent, bool) or not isinstance(rent, (int, float)):
            continue
        verified.append(row)
    return verified


def summarize_public_plan_pricing(rows: list[dict[str, Any]]) -> PublicPlanPricingSummary:
    """Summarize public plan rows without treating plan rents as unit rents.

    Args:
        rows: Floor-plan rows parsed from the exact public warm-page response.

    Returns:
        Counts and the visible numeric plan-price envelope. A blank
        ``unit_number`` is expected for plan rows and cannot qualify as a
        unit-level record.
    """
    plan_count = 0
    priced_count = 0
    prices: list[int] = []
    for row in rows:
        if str(row.get("unit_number") or "").strip():
            continue
        has_plan_signal = any(
            str(row.get(field) or "").strip()
            for field in ("floor_plan_name", "bedrooms", "bathrooms", "sqft")
        )
        if not has_plan_signal:
            continue
        plan_count += 1
        row_prices: list[int] = []
        for field in (
            "market_rent_low",
            "market_rent_high",
            "asking_rent",
            "rent_low",
            "rent_high",
        ):
            value = row.get(field)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value > 0:
                row_prices.append(int(value))
        if row_prices:
            priced_count += 1
            prices.extend(row_prices)
    return PublicPlanPricingSummary(
        plans_observed=plan_count,
        plans_with_numeric_price=priced_count,
        price_low=min(prices) if prices else None,
        price_high=max(prices) if prices else None,
    )


def _response_text(response: Any) -> str:
    """Decode a normalized adapter response without leaking raw content."""
    content = getattr(response, "content", b"")
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return str(content or "")


async def replay_with_client(
    request: ProspectPortalWarmReplayRequest,
    client: _RequestClient,
    *,
    parser: Callable[[str, str], list[dict[str, Any]]],
    plan_parser: Callable[[str, str], list[dict[str, Any]]] | None = None,
    replay_date: date | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    inter_request_delay_s: float = _RESIDENTIAL_INTER_REQUEST_DELAY_S,
) -> ProspectPortalWarmReplayResult:
    """Warm a page and replay its expanded unit endpoints on one client.

    Args:
        request: Public URLs and durable dynamic endpoint pattern.
        client: One already-created sticky-session HTTP client. It is not
            closed here so callers can own cleanup deterministically.
        parser: Adapter parser for an endpoint HTML fragment.
        plan_parser: Optional parser for plan cards on the warmed public page.
            Its result is checkpoint evidence only and can never verify a unit.
        replay_date: Optional testable runtime date; defaults to today.
        sleep: Injectable delay function for tests.
        inter_request_delay_s: Cost-control pause between residential calls.

    Returns:
        A result whose ``verified_rows`` is non-empty only when a returned row
        contains both a real unit identifier and numeric rent. Public fetch
        failures return an error result rather than raising.
    """
    identity = _IDENTITIES.pick_chrome_only(sticky_key=request.property_id)
    try:
        warm_response = await client.request(
            "GET",
            request.warm_page_url,
            headers=chrome_header_set(identity, cold_visit=True),
            cookies={},
            timeout=45.0,
        )
    except Exception as exc:  # public replay failure must not abort a batch
        return ProspectPortalWarmReplayResult(
            None, 0, (), (), error=f"warm-error:{type(exc).__name__}"
        )

    warm_status = getattr(warm_response, "status_code", None)
    if warm_status != 200:
        return ProspectPortalWarmReplayResult(warm_status, 0, (), (), error="warm-not-ok")
    warm_html = _response_text(warm_response)
    plan_pricing = summarize_public_plan_pricing(
        plan_parser(warm_html, request.warm_page_url) if plan_parser else []
    )
    floorplan_ids = extract_floorplan_ids(warm_html, max_floorplans=request.max_floorplans)
    if not floorplan_ids:
        return ProspectPortalWarmReplayResult(
            warm_status, 0, (), (), error="no-floorplan-ids", public_plan_pricing=plan_pricing
        )
    endpoint_template = request.endpoint_template or discover_endpoint_template(
        request.warm_page_url, warm_html
    )
    if endpoint_template is None:
        return ProspectPortalWarmReplayResult(
            warm_status,
            len(floorplan_ids),
            (),
            (),
            error="no-endpoint-template",
            public_plan_pricing=plan_pricing,
        )

    endpoint_statuses: list[int | None] = []
    verified: list[dict[str, Any]] = []
    runtime_date = replay_date or date.today()
    xhr_headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "text/html, */*; q=0.01",
        "Referer": request.warm_page_url,
    }
    for index, floorplan_id in enumerate(floorplan_ids):
        endpoint_url = expand_endpoint_template(
            endpoint_template,
            warm_page_url=request.warm_page_url,
            floorplan_id=floorplan_id,
            move_in_date=runtime_date,
        )
        if endpoint_url is None:
            return ProspectPortalWarmReplayResult(
                warm_status, len(floorplan_ids), tuple(endpoint_statuses),
                tuple(verified), endpoint_template, "unsafe-or-incomplete-template", plan_pricing,
            )
        if index and inter_request_delay_s > 0:
            await sleep(inter_request_delay_s)
        try:
            response = await client.request(
                "GET", endpoint_url, headers=xhr_headers, cookies={}, timeout=45.0
            )
        except Exception:
            endpoint_statuses.append(None)
            continue
        status = getattr(response, "status_code", None)
        endpoint_statuses.append(status)
        if status != 200:
            continue
        try:
            verified.extend(strict_unit_rent_rows(parser(_response_text(response), endpoint_url)))
        except Exception:
            continue
        # Endpoint discovery needs one strict returned row, not an expensive
        # full-roster scrape. Stop immediately once that proof exists; the
        # production extractor can enumerate every plan on its separate run.
        if verified:
            break

    return ProspectPortalWarmReplayResult(
        warm_status,
        len(floorplan_ids),
        tuple(endpoint_statuses),
        tuple(verified),
        endpoint_template,
        None,
        plan_pricing,
    )


async def revalidate_dom_with_client(
    request: ProspectPortalWarmReplayRequest,
    client: _RequestClient,
    *,
    plan_link_parser: Callable[[str, str], list[str]],
    unit_parser: Callable[[str, str], list[dict[str, Any]]],
    max_plan_pages: int = _DEFAULT_MAX_DOM_PLAN_PAGES,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    inter_request_delay_s: float = _RESIDENTIAL_INTER_REQUEST_DELAY_S,
) -> ProspectPortalDomRevalidationResult:
    """Freshly drill public per-plan DOM pages on the existing sticky client.

    Args:
        request: The public warm-page input for one property.
        client: The same property-sticky client that already warmed/replayed
            this property. It remains open for the caller to close.
        plan_link_parser: Extracts same-property per-plan public URLs from
            fresh warm HTML.
        unit_parser: Parses concrete unit rows from one per-plan page.
        max_plan_pages: Bounded public page fan-out for this recovery rung.
        sleep: Injectable delay used between plan-page navigations.
        inter_request_delay_s: Residential pacing delay between plan pages.

    Returns:
        Strict DOM rows only when a single newly fetched per-plan response has
        a concrete unit ID and numeric rent in the same parsed row. The warm
        page and exact proof-page URL are returned separately for profiling.
    """
    if max_plan_pages < 1:
        return ProspectPortalDomRevalidationResult(None, 0, (), (), error="dom-cap-zero")
    identity = _IDENTITIES.pick_chrome_only(sticky_key=request.property_id)
    try:
        warm_response = await client.request(
            "GET",
            request.warm_page_url,
            headers=chrome_header_set(identity, cold_visit=False),
            cookies={},
            timeout=45.0,
        )
    except Exception as exc:
        return ProspectPortalDomRevalidationResult(
            None, 0, (), (), error=f"dom-warm-error:{type(exc).__name__}"
        )
    warm_status = getattr(warm_response, "status_code", None)
    if warm_status != 200:
        return ProspectPortalDomRevalidationResult(
            warm_status, 0, (), (), error="dom-warm-not-ok"
        )
    try:
        plan_urls = plan_link_parser(_response_text(warm_response), request.warm_page_url)
    except Exception:
        return ProspectPortalDomRevalidationResult(
            warm_status, 0, (), (), error="dom-plan-link-parser-error"
        )
    bounded_urls = tuple(dict.fromkeys(plan_urls))[:max_plan_pages]
    if not bounded_urls:
        return ProspectPortalDomRevalidationResult(
            warm_status, 0, (), (), error="no-dom-plan-links"
        )

    statuses: list[int | None] = []
    for index, plan_url in enumerate(bounded_urls):
        if index and inter_request_delay_s > 0:
            await sleep(inter_request_delay_s)
        headers = chrome_header_set(identity, cold_visit=False)
        headers["Referer"] = request.warm_page_url
        try:
            response = await client.request(
                "GET", plan_url, headers=headers, cookies={}, timeout=45.0
            )
        except Exception:
            statuses.append(None)
            continue
        status = getattr(response, "status_code", None)
        statuses.append(status)
        if status != 200:
            continue
        try:
            strict_rows = strict_unit_rent_rows(unit_parser(_response_text(response), plan_url))
        except Exception:
            continue
        if strict_rows:
            return ProspectPortalDomRevalidationResult(
                warm_status,
                len(bounded_urls),
                tuple(statuses),
                tuple(strict_rows),
                unit_page_url=plan_url,
            )
    return ProspectPortalDomRevalidationResult(
        warm_status,
        len(bounded_urls),
        tuple(statuses),
        (),
        error="no-strict-dom-unit-rows",
    )


async def replay_and_revalidate_with_residential_session(
    request: ProspectPortalWarmReplayRequest,
    *,
    parser: Callable[[str, str], list[dict[str, Any]]],
    plan_parser: Callable[[str, str], list[dict[str, Any]]] | None,
    plan_link_parser: Callable[[str, str], list[str]],
    dom_unit_parser: Callable[[str, str], list[dict[str, Any]]],
    replay_date: date | None = None,
) -> tuple[ProspectPortalWarmReplayResult, ProspectPortalDomRevalidationResult]:
    """Try direct endpoint replay, then DOM proof in one Bright session.

    The DOM fallback is deliberately skipped after a direct endpoint proof.
    For non-proven replays it makes a fresh warm navigation followed by bounded
    per-plan navigations on the same property-sticky client; no cookie is
    stored outside that client.
    """
    proxy = BrightDataProvider().get_config(
        tier=ProxyTier.RESIDENTIAL, canonical_id=request.property_id
    ).to_httpx_url()
    client = make_http_client(FetchTier.RESIDENTIAL, proxy)
    try:
        replay = await replay_with_client(
            request,
            client,
            parser=parser,
            plan_parser=plan_parser,
            replay_date=replay_date,
        )
        if replay.verified or replay.warm_status != 200:
            return replay, ProspectPortalDomRevalidationResult(
                replay.warm_status, 0, (), (), error="dom-not-needed-or-warm-unavailable"
            )
        dom = await revalidate_dom_with_client(
            request,
            client,
            plan_link_parser=plan_link_parser,
            unit_parser=dom_unit_parser,
        )
        return replay, dom
    finally:
        await client.aclose()


async def replay_with_residential_session(
    request: ProspectPortalWarmReplayRequest,
    *,
    parser: Callable[[str, str], list[dict[str, Any]]],
    plan_parser: Callable[[str, str], list[dict[str, Any]]] | None = None,
    replay_date: date | None = None,
) -> ProspectPortalWarmReplayResult:
    """Run a bounded warm/replay using one property-sticky Bright session.

    Args:
        request: Public durable inputs for a single property.
        parser: ProspectPortal HTML-fragment parser.
        plan_parser: Optional public warm-page plan-card parser.
        replay_date: Optional testable date; production derives today's date.

    Returns:
        The strict replay result. Cookies exist only inside the client and are
        discarded when it closes.
    """
    proxy = BrightDataProvider().get_config(
        tier=ProxyTier.RESIDENTIAL, canonical_id=request.property_id
    ).to_httpx_url()
    client = make_http_client(FetchTier.RESIDENTIAL, proxy)
    try:
        return await replay_with_client(
            request,
            client,
            parser=parser,
            plan_parser=plan_parser,
            replay_date=replay_date,
        )
    finally:
        await client.aclose()
