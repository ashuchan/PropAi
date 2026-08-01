"""Property-scoped ManageBuilding route promotion.

Some marketing sites publish their real apartment inventory on a linked
``{tenant}.managebuilding.com`` account.  The public rentals index can be an
account-wide portfolio, so discovering the tenant is not sufficient.  This
recovery accepts only an operator-authored tenant link and then requires one
of two property boundaries before emitting a row:

* the marketing page authored exact ``listingId`` values, which become a
  whitelist; or
* the ManageBuilding account label exactly matches the CSV property name.

Every emitted card must additionally match CSV city, state and ZIP.  The
implementation uses one bounded plain-HTTP request; no browser, proxy,
fingerprint impersonation, unlocker or CAPTCHA path is reachable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from bs4 import BeautifulSoup

from ma_poc.pms.adapters._html_extract import (
    extract_managebuilding_rentals_index,
    is_managebuilding_rentals_index_url,
)
from ma_poc.pms.adapters.base import AdapterContext

_HOST_SUFFIX = ".managebuilding.com"
_MAX_BODY_BYTES = 3_000_000


@dataclass(frozen=True)
class ManageBuildingRoute:
    index_url: str
    listing_ids: frozenset[str]


def _tenant_host(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not host.endswith(_HOST_SUFFIX)
        or host == _HOST_SUFFIX.lstrip(".")
    ):
        return ""
    return host


def _listing_id(url: str) -> str:
    try:
        for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=False):
            if key.casefold() == "listingid" and value.strip().isdigit():
                return value.strip()
    except ValueError:
        return ""
    return ""


def discover_managebuilding_route(body: str) -> ManageBuildingRoute | None:
    """Return the one tenant explicitly linked by the marketing document.

    Multiple tenant hosts are ambiguous and fail closed.  Text/script mentions
    do not qualify: the URL must be the value of an authored link, frame or
    form attribute and must stay under a real tenant subdomain.
    """
    if not body or "managebuilding.com" not in body.casefold():
        return None
    try:
        soup = BeautifulSoup(body, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(body, "html.parser")
        except Exception:
            return None

    by_host: dict[str, set[str]] = {}
    selectors = (
        ("a[href]", "href"),
        ("iframe[src]", "src"),
        ("form[action]", "action"),
    )
    for selector, attr in selectors:
        for element in soup.select(selector):
            raw = str(element.get(attr) or "").strip()
            host = _tenant_host(raw)
            if not host:
                continue
            try:
                path = urlsplit(raw).path.casefold()
            except ValueError:
                continue
            if not path.startswith("/resident/"):
                continue
            by_host.setdefault(host, set())
            listing_id = _listing_id(raw)
            if listing_id:
                by_host[host].add(listing_id)

    if len(by_host) != 1:
        return None
    host, listing_ids = next(iter(by_host.items()))
    return ManageBuildingRoute(
        index_url=f"https://{host}/Resident/public/rentals",
        listing_ids=frozenset(listing_ids),
    )


async def _fetch_index(index_url: str) -> tuple[str, str] | None:
    """Fetch one public index with direct HTTP and validate its final route."""
    import httpx

    expected_host = _tenant_host(index_url)
    if not expected_host or not is_managebuilding_rentals_index_url(index_url):
        return None
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(20.0),
            trust_env=False,
            headers={"Accept": "text/html,application/xhtml+xml"},
        ) as client:
            response = await client.get(index_url)
    except (httpx.HTTPError, ValueError):
        return None
    if response.status_code != 200 or len(response.content) > _MAX_BODY_BYTES:
        return None
    final_url = str(response.url)
    if (
        _tenant_host(final_url) != expected_host
        or not is_managebuilding_rentals_index_url(final_url)
    ):
        return None
    return response.text, final_url


def _ctx_body(ctx: AdapterContext) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    body = getattr(fetch_result, "body", None)
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return body if isinstance(body, str) else ""


async def recover_managebuilding(ctx: AdapterContext) -> list[dict[str, Any]]:
    """Recover property-scoped native listings from an authored tenant link."""
    route = discover_managebuilding_route(_ctx_body(ctx))
    if route is None:
        return []
    fetched = await _fetch_index(route.index_url)
    if fetched is None:
        return []
    index_body, final_url = fetched
    return extract_managebuilding_rentals_index(
        index_body,
        final_url,
        property_name=str(getattr(ctx, "property_name", "") or ""),
        city=str(getattr(ctx, "city", "") or ""),
        state=str(getattr(ctx, "state", "") or ""),
        zip_code=str(getattr(ctx, "zip_code", "") or ""),
        listing_id_whitelist=set(route.listing_ids),
    )
