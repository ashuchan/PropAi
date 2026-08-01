"""MRI ProspectConnect property-search adapter.

Property marketing sites publish an exact ``*.mriprospectconnect.com`` link.
The provider index is a multi-tenant ASP.NET form: a GET establishes the
property-scoped session and anti-forgery token, then ``POST /Search/Search``
returns the public plan cards and native ``button[data-unitid]`` rows.

The adapter never guesses a portfolio property.  It accepts only a published
provider route carrying one community code and requires the provider heading,
street, city, state, ZIP, and ``data-propertyid`` to match the configured
property before the search POST is parsed.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from ma_poc.pms.adapters._parsing import bed_label_from, make_unit_dict
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

_TIER = "TIER_1_API_MRI_PROSPECTCONNECT"
_COMMUNITY_RE = re.compile(r"^[A-Za-z0-9_-]{2,20}$")
_INDEX_PATH_RE = re.compile(
    r"^/Search/Index/([A-Za-z0-9_-]{2,20})/?$",
    re.IGNORECASE,
)
_ROOT_CODE_PATH_RE = re.compile(r"^/([A-Za-z0-9_-]{2,20})/?$")
_MONEY_RE = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)")
_BED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Beds?\b", re.IGNORECASE)
_BATH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Baths?\b", re.IGNORECASE)
_NAME_STOPWORDS = frozenset(
    {
        "apartment",
        "apartments",
        "community",
        "home",
        "homes",
        "the",
    }
)
_ADDRESS_STOPWORDS = frozenset(
    {
        "avenue",
        "ave",
        "boulevard",
        "blvd",
        "cir",
        "circle",
        "court",
        "ct",
        "drive",
        "dr",
        "east",
        "e",
        "highway",
        "hwy",
        "lane",
        "ln",
        "north",
        "n",
        "parkway",
        "pkwy",
        "place",
        "pl",
        "road",
        "rd",
        "south",
        "s",
        "square",
        "street",
        "st",
        "west",
        "w",
    }
)


@dataclass(frozen=True)
class MriSearchResponse:
    index_url: str
    final_index_url: str
    community: str
    index_status: int
    index_html: str
    search_url: str
    search_status: int
    search_html: str
    error: str = ""


def _tokens(value: object) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(value or "").casefold())


def extract_mri_property_route(*urls: str) -> tuple[str, str]:
    """Return one canonical index URL and community code, else empty."""
    found: set[tuple[str, str]] = set()
    for raw_url in urls:
        if not raw_url:
            continue
        try:
            parsed = urlsplit(raw_url)
            host = (parsed.hostname or "").casefold().rstrip(".")
            if (
                parsed.scheme.casefold() not in {"http", "https"}
                or not host.endswith(".mriprospectconnect.com")
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port is not None
                or parsed.query
                or parsed.fragment
            ):
                continue
        except (TypeError, ValueError):
            continue
        match = _INDEX_PATH_RE.fullmatch(parsed.path or "")
        if match is None:
            match = _ROOT_CODE_PATH_RE.fullmatch(parsed.path or "")
        if match is None:
            continue
        community = match.group(1).upper()
        if not _COMMUNITY_RE.fullmatch(community):
            continue
        canonical = f"https://{host}/Search/Index/{community}"
        found.add((canonical, community))
    return next(iter(found)) if len(found) == 1 else ("", "")


def mri_property_identity_matches(
    index_html: str,
    ctx: AdapterContext,
    community: str,
) -> bool:
    """Require an exact configured-property match on the provider index."""
    if not index_html or not community:
        return False
    soup = BeautifulSoup(index_html, "html.parser")
    property_node = soup.find(attrs={"data-propertyid": True})
    heading_node = soup.find("h1")
    if not isinstance(property_node, Tag) or not isinstance(heading_node, Tag):
        return False
    provider_code = str(property_node.get("data-propertyid") or "").strip()
    if provider_code.casefold() != community.casefold():
        return False
    heading_tokens = set(
        token for token in _tokens(heading_node.get_text(" ", strip=True)) if token not in _NAME_STOPWORDS
    )
    expected_name = [token for token in _tokens(ctx.property_name) if token not in _NAME_STOPWORDS]
    page_tokens = set(_tokens(soup.get_text(" ", strip=True)))
    expected_address = _tokens(ctx.address)
    street_number = expected_address[0] if expected_address else ""
    street_words = [
        token for token in expected_address[1:] if token not in _ADDRESS_STOPWORDS and not token.isdigit()
    ]
    city_tokens = _tokens(ctx.city)
    state_tokens = _tokens(ctx.state)
    zip_tokens = _tokens(ctx.zip_code)
    return bool(
        expected_name
        and all(token in heading_tokens for token in expected_name)
        and street_number
        and street_number in page_tokens
        and street_words
        and all(token in page_tokens for token in street_words)
        and city_tokens
        and all(token in page_tokens for token in city_tokens)
        and state_tokens
        and all(token in page_tokens for token in state_tokens)
        and zip_tokens
        and all(token in page_tokens for token in zip_tokens)
    )


def _number(value: object) -> float | None:
    match = _MONEY_RE.search(str(value or ""))
    if match is None:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return number if number > 0 else None


def _cell_text(row: Tag, label: str) -> str:
    node = row.find("td", attrs={"data-th": re.compile(f"^{re.escape(label)}$", re.I)})
    return node.get_text(" ", strip=True) if isinstance(node, Tag) else ""


def parse_mri_search_units(
    search_html: str,
    *,
    community: str,
    source_url: str,
) -> list[dict[str, Any]]:
    """Parse provider-native unit buttons and their plan/table dimensions."""
    if not search_html:
        return []
    soup = BeautifulSoup(search_html, "html.parser")
    units: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for button in soup.select("button[data-unitid]"):
        if not isinstance(button, Tag):
            continue
        row = button.find_parent("tr")
        card = button.find_parent("div", class_="pc-card")
        if not isinstance(row, Tag) or not isinstance(card, Tag):
            continue
        unit_number = str(button.get("data-unitid") or "").strip()
        building = str(button.get("data-bldgid") or "").strip()
        native_key = (building.casefold(), unit_number.casefold())
        if not unit_number or native_key in seen:
            continue
        rent_node = row.find(attrs={"data-rent-range": True})
        rent = _number(rent_node.get("data-rent-range") if isinstance(rent_node, Tag) else "")
        sqft = _number(_cell_text(row, "Sqft"))
        if rent is None or sqft is None:
            continue
        title_node = card.select_one(".pc-card-title")
        subtitle_node = card.select_one(".pc-card-subtitle")
        plan_name = (
            " ".join(title_node.get_text(" ", strip=True).split()) if isinstance(title_node, Tag) else ""
        )
        plan_name = re.sub(r"\s+\d+\s+available\s*$", "", plan_name, flags=re.I)
        subtitle = subtitle_node.get_text(" ", strip=True) if isinstance(subtitle_node, Tag) else ""
        bed_match = _BED_RE.search(subtitle)
        bath_match = _BATH_RE.search(subtitle)
        bedrooms_number = int(float(bed_match.group(1))) if bed_match else None
        bedrooms = str(bedrooms_number) if bedrooms_number is not None else ""
        bathrooms = bath_match.group(1) if bath_match else ""
        rounded_rent = int(round(rent))
        native_unit_id = f"{building}:{unit_number}" if building else unit_number
        unit = make_unit_dict(
            floor_plan_name=plan_name,
            bed_label=bed_label_from(bedrooms_number, plan_name),
            bedrooms=bedrooms,
            bathrooms=bathrooms,
            sqft=str(int(sqft)),
            unit_number=unit_number,
            building=building,
            rent_low=rounded_rent,
            rent_high=rounded_rent,
            availability_status="AVAILABLE",
            available_units="1",
            availability_date=str(
                button.get("data-available-date") or _cell_text(row, "Available") or ""
            ).strip(),
            lease_term=str(button.get("data-term") or "").strip(),
            source_api_url=source_url,
            extraction_tier=_TIER,
            source_ids={"mri_unit_id": native_unit_id},
        )
        unit["source_property_id"] = community
        unit["provider_native_unit_id"] = native_unit_id
        unit["available_end_date"] = str(button.get("data-available-end-date") or "").strip()
        unit["unit_address"] = str(button.get("data-unit-address") or "").strip()
        seen.add(native_key)
        units.append(unit)
    return units


def _fetch_mri_search(index_url: str, community: str) -> MriSearchResponse:
    """Run the ordinary stateful GET + CSRF POST; no solver or impersonation."""
    from curl_cffi import requests

    session = requests.Session()
    search_url = f"{urlsplit(index_url).scheme}://{urlsplit(index_url).netloc}/Search/Search"
    try:
        index = session.get(
            index_url,
            timeout=30,
            allow_redirects=True,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        index_status = int(index.status_code or 0)
        index_html = str(index.text or "")
        if index_status != 200:
            return MriSearchResponse(
                index_url,
                str(index.url or index_url),
                community,
                index_status,
                index_html,
                search_url,
                0,
                "",
                f"INDEX_HTTP_{index_status}",
            )
        soup = BeautifulSoup(index_html, "html.parser")
        token_node = soup.find("input", attrs={"name": "__RequestVerificationToken"})
        if not isinstance(token_node, Tag):
            return MriSearchResponse(
                index_url,
                str(index.url or index_url),
                community,
                index_status,
                index_html,
                search_url,
                0,
                "",
                "MISSING_ANTIFORGERY_TOKEN",
            )
        final_index_url = str(index.url or index_url)
        parsed_final = urlsplit(final_index_url)
        origin = f"{parsed_final.scheme}://{parsed_final.netloc}"
        searched = session.post(
            search_url,
            data={
                "__RequestVerificationToken": str(token_node.get("value") or ""),
                "Community": community,
                "MarketId": "",
                "Bedroom": "",
                "ApartmentNumber": "",
                "MoveInDate": "",
            },
            headers={"Referer": final_index_url, "Origin": origin},
            timeout=30,
            allow_redirects=True,
        )
        return MriSearchResponse(
            index_url=index_url,
            final_index_url=final_index_url,
            community=community,
            index_status=index_status,
            index_html=index_html,
            search_url=search_url,
            search_status=int(searched.status_code or 0),
            search_html=str(searched.text or ""),
        )
    except Exception as exc:
        return MriSearchResponse(
            index_url,
            index_url,
            community,
            0,
            "",
            search_url,
            0,
            "",
            f"{type(exc).__name__}",
        )
    finally:
        try:
            session.close()
        except Exception:
            pass


class MriProspectConnectAdapter:
    """Property-scoped MRI ProspectConnect public search adapter."""

    pms_name: str = "mri_prospectconnect"
    _fingerprints = ["mriprospectconnect.com", "/Search/Index/"]

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    def matches_response_body(self, body: Any) -> bool:
        if not isinstance(body, (str, bytes)):
            return False
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        low = text.casefold()
        return "__requestverificationtoken" in low and "data-propertyid" in low

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER)
        fetch_result = getattr(ctx, "fetch_result", None)
        final_url = str(getattr(fetch_result, "final_url", "") or "")
        index_url, community = extract_mri_property_route(final_url, ctx.base_url)
        if not index_url or not community:
            result.tier_used = f"{_TIER}_NO_EXACT_PROPERTY_ROUTE"
            result.errors.append("MRI_NO_EXACT_PROPERTY_ROUTE: no sole property-scoped ProspectConnect route")
            return result

        response = await asyncio.to_thread(_fetch_mri_search, index_url, community)
        result.api_responses.extend(
            [
                {
                    "url": response.index_url,
                    "status": response.index_status,
                    "body": "<mri-property-index>",
                    "via": "mri_prospectconnect_session",
                },
                {
                    "url": response.search_url,
                    "status": response.search_status,
                    "body": "<mri-property-search>",
                    "via": "mri_prospectconnect_session",
                },
            ]
        )
        if response.error:
            result.tier_used = f"{_TIER}_{response.error}"
            result.errors.append(f"MRI_PROSPECTCONNECT_ERROR: {response.error}")
            return result
        if not mri_property_identity_matches(response.index_html, ctx, community):
            result.tier_used = f"{_TIER}_PROPERTY_IDENTITY_REJECTED"
            result.errors.append(
                "MRI_PROPERTY_IDENTITY_REJECTED: provider index does not match configured property"
            )
            return result
        if response.search_status != 200:
            result.tier_used = f"{_TIER}_SEARCH_HTTP_{response.search_status}"
            return result
        raw_units = parse_mri_search_units(
            response.search_html,
            community=community,
            source_url=response.search_url,
        )
        if not raw_units:
            result.tier_used = f"{_TIER}_PLAN_ONLY_OR_EMPTY"
            result.winning_url = response.search_url
            return result

        from ma_poc.extraction.post_process import post_process

        processed = post_process(raw_units, property_id=ctx.property_id)
        if processed.n_admitted <= 0:
            result.tier_used = f"{_TIER}_VALIDITY_REJECTED"
            result.errors.append(f"MRI_VALIDITY_REJECTED: {len(raw_units)} parsed rows failed unit validity")
            return result
        result.units = processed.admitted
        result.plan_summaries = processed.plan_summaries
        result.winning_url = response.search_url
        result.tier_used = _TIER
        result.confidence = min(0.94, 0.78 + 0.03 * processed.n_admitted)
        return result
