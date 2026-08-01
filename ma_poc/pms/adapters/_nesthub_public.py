"""Fail-closed NestHub recovery through an exact first-party property chain.

This is deliberately not a generic property-manager portfolio parser.  It
runs only when the configured response is a native NestHub detail page whose
scoped description and address identify the configured property.  From there
it follows one same-host property community page, one same-host published
``Available Rentals`` roster, and revalidates every exact-address candidate on
its native detail page before emitting anything.

The manager roster is mixed, so address filtering alone is not admission.
Each emitted row must also have native-id agreement across card/path/canonical,
the configured property name in the detail's scoped description, ``For Rent``,
card/detail-equal positive rent, an exact provider availability date, complete
dimensions, a unique visible unit suffix, and a provider-published floor-plan
sentence.  All requests are ordinary direct GETs: no proxy, unlocker, render,
CAPTCHA solver, fingerprint rotation, or LLM is used.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from ma_poc.pms.adapters._parsing import bed_label_from, make_unit_dict
from ma_poc.pms.adapters.base import AdapterContext

_DETAIL_PATH_RE = re.compile(r"^/_system/listings/(?P<listing_id>[1-9][0-9]*)(?:/|$)")
_MAX_HTML_BYTES = 2_000_000
_MAX_PAGES = 5
_MAX_ROWS = 100
_MAX_CANDIDATES = 20
_ATTEMPTED_ATTR = "_nesthub_public_attempted"
_TELEMETRY_ATTR = "_nesthub_official_chain"
_NAME_STOPWORDS = frozenset(
    {
        "apartment",
        "apartments",
        "at",
        "community",
        "homes",
        "of",
        "the",
    }
)
_ADDRESS_ALIASES = {
    "avenue": "ave",
    "boulevard": "blvd",
    "court": "ct",
    "drive": "dr",
    "highway": "hwy",
    "lane": "ln",
    "parkway": "pkwy",
    "place": "pl",
    "road": "rd",
    "street": "st",
}
_UNIT_MARKERS = frozenset({"apartment", "apt", "ste", "suite", "unit"})
_FLOOR_PLAN_SENTENCE_RE = re.compile(
    r"\bThe\s+(?P<name>[A-Z][A-Za-z0-9' -]{1,40}?)\s+is\s+a\s+"
    r"\d+(?:\.\d+)?\s+bedroom\b",
)


def _norm(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _norm_address(value: object) -> str:
    return " ".join(_ADDRESS_ALIASES.get(token, token) for token in _norm(value).split())


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _host(value: str) -> str:
    host = (urlparse(value).hostname or "").casefold().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _canonical_http_url(base_url: str, value: object) -> str:
    raw = str(value or "").strip()
    if not raw or raw.startswith(("#", "javascript:", "mailto:", "tel:")):
        return ""
    try:
        parsed = urlparse(urljoin(base_url, raw))
    except Exception:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    return urlunparse(
        (
            parsed.scheme.casefold(),
            parsed.netloc,
            parsed.path or "/",
            "",
            parsed.query,
            "",
        )
    )


def _body_from_ctx(ctx: AdapterContext) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    body = getattr(fetch_result, "body", None) if fetch_result is not None else None
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    return body if isinstance(body, str) else ""


def _page_url(ctx: AdapterContext) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    final_url = getattr(fetch_result, "final_url", "") if fetch_result is not None else ""
    return str(final_url or getattr(ctx, "base_url", "") or "")


def _node_text(node: Any) -> str:
    return _clean(node.get_text(" ", strip=True)) if node is not None else ""


def _name_tokens(value: object) -> list[str]:
    return [token for token in _norm(value).split() if token and token not in _NAME_STOPWORDS]


def _name_matches(value: object, property_name: object) -> bool:
    tokens = _name_tokens(property_name)
    words = set(_norm(value).split())
    return bool(tokens and all(token in words for token in tokens))


def _listing_id(value: str) -> str:
    match = _DETAIL_PATH_RE.match(urlparse(value).path or "")
    return match.group("listing_id") if match else ""


def _money(value: object) -> int | None:
    match = re.search(r"\$\s*([0-9][0-9,]*)", str(value or ""))
    if not match:
        return None
    parsed = int(match.group(1).replace(",", ""))
    return parsed if parsed > 0 else None


def _number(value: object) -> str:
    match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", str(value or ""))
    if not match:
        return ""
    try:
        parsed = Decimal(match.group(1))
    except InvalidOperation:
        return ""
    if parsed <= 0:
        return ""
    return format(parsed.normalize(), "f")


def _sqft(value: object) -> str:
    match = re.search(r"(?<!\d)([1-9][0-9]{2,4})(?!\d)", str(value or ""))
    return match.group(1) if match else ""


def _date_iso(value: object) -> str:
    raw = _clean(value)
    for fmt in ("%m-%d-%Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def _sub_detail_value(soup: Any, label: str) -> str:
    wanted = _norm(label)
    for row in soup.select(".sub-detail"):
        key = _norm(_node_text(row.select_one(".sub-detail__label")))
        if key == wanted:
            return _node_text(row.select_one(".sub-detail__value"))
    return ""


def _unit_suffix(street: object, canonical_address: object) -> str:
    normalized_street = _norm_address(street)
    canonical = _norm_address(canonical_address)
    if not canonical or not normalized_street.startswith(f"{canonical} "):
        return ""
    tail = normalized_street[len(canonical) :].strip().split()
    tail = [token for token in tail if token not in _UNIT_MARKERS]
    if len(tail) != 1 or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,15}", tail[0]):
        return ""
    return tail[0].upper()


def _parse_detail(html: str, final_url: str) -> dict[str, Any]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    canonical_node = soup.select_one('link[rel="canonical"][href]')
    canonical_url = _canonical_http_url(
        final_url,
        canonical_node.get("href") if canonical_node is not None else "",
    )
    description = _node_text(soup.select_one(".description"))
    plan_names = {
        _clean(match.group("name"))
        for match in _FLOOR_PLAN_SENTENCE_RE.finditer(description)
        if _clean(match.group("name"))
    }
    status = _node_text(soup.select_one(".key-detail.rent .label"))
    if not status:
        status = _node_text(soup.select_one(".nhw-details__rented"))
    return {
        "listing_id": _listing_id(final_url),
        "canonical_url": canonical_url,
        "canonical_listing_id": _listing_id(canonical_url),
        "street": _node_text(soup.select_one(".nhw-details__header h1")),
        "city_state_zip": _node_text(soup.select_one(".nhw-details__header h2")),
        "rent": _money(_node_text(soup.select_one(".key-detail.price .value"))),
        "bedrooms": _number(_node_text(soup.select_one(".key-detail.bedrooms .value"))),
        "bathrooms": _number(_node_text(soup.select_one(".key-detail.bathrooms .value"))),
        "sqft": _sqft(_node_text(soup.select_one(".key-detail.sqft .value"))),
        "status": status,
        "availability_date_raw": _sub_detail_value(soup, "Date Available"),
        "availability_date": _date_iso(_sub_detail_value(soup, "Date Available")),
        "description": description,
        "floor_plan_names": sorted(plan_names),
        "nesthub_detail_marker": bool(soup.select_one("#nesthub-property-detail-view.nhw-details")),
        "nesthub_resource_marker": "resources.nesthub.com" in html.casefold(),
    }


def _configured_identity_reasons(
    record: dict[str, Any],
    ctx: AdapterContext,
    configured_url: str,
) -> list[str]:
    reasons: list[str] = []
    if not record["nesthub_detail_marker"] or not record["nesthub_resource_marker"]:
        reasons.append("not_native_nesthub_detail")
    if not record["listing_id"] or record["listing_id"] != record["canonical_listing_id"]:
        reasons.append("configured_native_id_mismatch")
    if _host(record["canonical_url"]) != _host(configured_url):
        reasons.append("configured_canonical_host_mismatch")
    if not _unit_suffix(record["street"], getattr(ctx, "address", "")):
        reasons.append("configured_address_or_unit_mismatch")
    if _norm(record["city_state_zip"]) != _norm(
        f"{getattr(ctx, 'city', '')} {getattr(ctx, 'state', '')} {getattr(ctx, 'zip_code', '')}"
    ):
        reasons.append("configured_city_state_zip_mismatch")
    if not _name_matches(record["description"], getattr(ctx, "property_name", "")):
        reasons.append("configured_scoped_property_name_absent")
    if record["status"] not in {"For Rent", "This Property Is Not Available"}:
        reasons.append("configured_provider_status_absent")
    return reasons


def _community_url(
    html: str,
    configured_url: str,
    property_name: str,
) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    candidates: list[str] = []
    for anchor in soup.select("a[href]"):
        url = _canonical_http_url(configured_url, anchor.get("href"))
        parsed = urlparse(url)
        context = " ".join(
            [
                _node_text(anchor),
                str(anchor.get("aria-label") or ""),
                str(anchor.get("title") or ""),
            ]
        )
        if (
            not url
            or _host(url) != _host(configured_url)
            or parsed.query
            or parsed.path.startswith("/_system/")
            or not _name_matches(context, property_name)
        ):
            continue
        if url not in candidates:
            candidates.append(url)
    return candidates[0] if len(candidates) == 1 else ""


def _community_boundary(
    html: str,
    final_url: str,
    ctx: AdapterContext,
) -> tuple[str, str]:
    """Return ``(published_filter, roster_url)`` or empty values."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    if "resources.nesthub.com" not in html.casefold():
        return "", ""
    heading = _node_text(soup.select_one("h1"))
    if not _name_matches(heading, getattr(ctx, "property_name", "")):
        return "", ""
    expected_address = _norm_address(
        f"{getattr(ctx, 'address', '')} {getattr(ctx, 'city', '')} "
        f"{getattr(ctx, 'state', '')} {getattr(ctx, 'zip_code', '')}"
    )
    if not any(
        expected_address and expected_address in _norm_address(_node_text(node))
        for node in soup.select("address")
    ):
        return "", ""
    widgets = soup.select('#nh-props[data-ion="listing-widget"][data-hard-filters]')
    if len(widgets) != 1:
        return "", ""
    published_filter = _clean(widgets[0].get("data-hard-filters") or "")
    if not re.fullmatch(r"search=[A-Za-z0-9_-]{1,64}", published_filter):
        return "", ""
    if "available units" not in _norm(_node_text(soup)):
        return "", ""

    candidates: list[str] = []
    for anchor in soup.select("a[href]"):
        context = _norm(
            " ".join(
                [
                    _node_text(anchor),
                    str(anchor.get("aria-label") or ""),
                    str(anchor.get("title") or ""),
                ]
            )
        )
        if "available rental" not in context:
            continue
        url = _canonical_http_url(final_url, anchor.get("href"))
        parsed = urlparse(url)
        if (
            url
            and _host(url) == _host(final_url)
            and not parsed.query
            and not parsed.path.startswith("/_system/")
            and url not in candidates
        ):
            candidates.append(url)
    return (published_filter, candidates[0]) if len(candidates) == 1 else ("", "")


def _parse_location(value: object) -> tuple[str, str, str, str]:
    parts = [part.strip() for part in _clean(value).rsplit(",", 2)]
    if len(parts) != 3:
        return "", "", "", ""
    state_zip = parts[2].split()
    if len(state_zip) != 2:
        return "", "", "", ""
    return parts[0], parts[1], state_zip[0], state_zip[1]


def _roster_page(
    html: str,
    final_url: str,
    roster_url: str,
    page_number: int,
) -> tuple[list[dict[str, Any]], set[int]] | None:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    if len(soup.select('#nesthub-property-list-view[data-ion="listing-list"]')) != 1:
        return None
    cards = soup.select(".nhw-list__item")
    if not cards or len(cards) > _MAX_ROWS:
        return None
    rows: list[dict[str, Any]] = []
    for card in cards:
        anchors = card.select("a[href][data-id]")
        if len(anchors) != 1:
            return None
        anchor = anchors[0]
        detail_url = _canonical_http_url(final_url, anchor.get("href"))
        native_id = _clean(anchor.get("data-id") or "")
        if (
            not re.fullmatch(r"[1-9][0-9]*", native_id)
            or _host(detail_url) != _host(roster_url)
            or _listing_id(detail_url) != native_id
        ):
            return None
        location = _node_text(card.select_one(".nhw-list__location"))
        street, city, state, zip_code = _parse_location(location)
        rows.append(
            {
                "provider_listing_id": native_id,
                "detail_url": detail_url,
                "rent": _money(_node_text(card.select_one(".nhw-list__price"))),
                "location": location,
                "street": street,
                "city": city,
                "state": state,
                "zip": zip_code,
                "availability_text": _node_text(card.select_one(".nhw-list__availability")),
                "property_type": _node_text(card.select_one(".nhw-list__prop-type")),
                "page": page_number,
            }
        )

    pages = {1}
    for anchor in soup.select(".nhw-pagination a[href]"):
        url = _canonical_http_url(final_url, anchor.get("href"))
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if (
            _host(url) != _host(roster_url)
            or parsed.path.rstrip("/") != urlparse(roster_url).path.rstrip("/")
            or set(query) != {"pg"}
            or len(query.get("pg", [])) != 1
            or not query["pg"][0].isdigit()
        ):
            return None
        pages.add(int(query["pg"][0]))
    if not pages or min(pages) != 1 or max(pages) > _MAX_PAGES:
        return None
    return rows, pages


def _same_roster_page(final_url: str, roster_url: str, page: int) -> bool:
    parsed = urlparse(final_url)
    expected = urlparse(roster_url)
    query = parse_qs(parsed.query)
    if _host(final_url) != _host(roster_url) or parsed.path.rstrip("/") != expected.path.rstrip("/"):
        return False
    if page == 1:
        return not query or query == {"pg": ["1"]}
    return query == {"pg": [str(page)]}


def _candidate_reasons(row: dict[str, Any], ctx: AdapterContext) -> list[str]:
    reasons: list[str] = []
    suffix = _unit_suffix(row["street"], getattr(ctx, "address", ""))
    if not suffix:
        reasons.append("canonical_street_and_native_unit_suffix_mismatch")
    if _norm(row["city"]) != _norm(getattr(ctx, "city", "")):
        reasons.append("canonical_city_mismatch")
    if _norm(row["state"]) != _norm(getattr(ctx, "state", "")):
        reasons.append("canonical_state_mismatch")
    if str(row["zip"]).strip() != str(getattr(ctx, "zip_code", "") or "").strip():
        reasons.append("canonical_zip_mismatch")
    if row["rent"] is None:
        reasons.append("no_positive_roster_rent")
    raw_date = re.sub(r"^available\s*:\s*", "", row["availability_text"], flags=re.I)
    if not _date_iso(raw_date):
        reasons.append("no_exact_roster_availability_date")
    row["unit_number"] = suffix
    row["availability_date"] = _date_iso(raw_date)
    return reasons


def _detail_reasons(
    row: dict[str, Any],
    detail: dict[str, Any],
    ctx: AdapterContext,
    final_url: str,
) -> list[str]:
    reasons: list[str] = []
    provider_id = str(row["provider_listing_id"])
    if (
        _host(final_url) != _host(row["detail_url"])
        or detail["listing_id"] != provider_id
        or detail["canonical_listing_id"] != provider_id
        or _host(detail["canonical_url"]) != _host(row["detail_url"])
    ):
        reasons.append("detail_native_id_or_host_mismatch")
    if not detail["nesthub_detail_marker"] or not detail["nesthub_resource_marker"]:
        reasons.append("detail_nesthub_marker_absent")
    detail_suffix = _unit_suffix(detail["street"], getattr(ctx, "address", ""))
    if detail_suffix != row["unit_number"] or _norm_address(detail["street"]) != _norm_address(row["street"]):
        reasons.append("detail_address_or_unit_mismatch")
    if _norm(detail["city_state_zip"]) != _norm(
        f"{getattr(ctx, 'city', '')} {getattr(ctx, 'state', '')} {getattr(ctx, 'zip_code', '')}"
    ):
        reasons.append("detail_city_state_zip_mismatch")
    if not _name_matches(detail["description"], getattr(ctx, "property_name", "")):
        reasons.append("detail_scoped_property_name_absent")
    expected_scoped_address = _norm_address(f"{getattr(ctx, 'address', '')} {row['unit_number']}")
    if expected_scoped_address not in _norm_address(detail["description"]):
        reasons.append("detail_scoped_property_address_absent")
    if detail["status"] != "For Rent":
        reasons.append("detail_not_for_rent")
    if detail["rent"] is None or detail["rent"] != row["rent"]:
        reasons.append("detail_roster_rent_mismatch")
    if detail["availability_date"] != row["availability_date"]:
        reasons.append("detail_roster_availability_date_mismatch")
    if not detail["bedrooms"] or not detail["bathrooms"] or not detail["sqft"]:
        reasons.append("detail_dimensions_absent")
    if len(detail["floor_plan_names"]) != 1:
        reasons.append("provider_floor_plan_name_absent_or_ambiguous")
    return reasons


async def _fetch_direct_html(url: str, referer: str) -> tuple[str, str] | None:
    """Fetch one bounded HTML page directly; never use a proxy/unlocker."""
    try:
        from ma_poc.pms.adapters._probe import probe_get

        response = await asyncio.to_thread(
            probe_get,
            url,
            timeout=25,
            unlocker=False,
            retries=0,
            proxies={},
            headers={"Referer": referer},
        )
    except Exception:
        return None
    status = int(getattr(response, "status_code", 0) or 0)
    final_url = str(getattr(response, "url", "") or url)
    if status != 200:
        return None
    body = getattr(response, "content", None)
    if isinstance(body, bytes):
        if not body or len(body) > _MAX_HTML_BYTES:
            return None
        raw = body
        html = body.decode("utf-8", "replace")
    else:
        html = str(getattr(response, "text", "") or "")
        raw = html.encode("utf-8", "replace")
        if not raw or len(raw) > _MAX_HTML_BYTES:
            return None
    try:
        from ma_poc.fetch.captcha_detect import looks_like_captcha

        if looks_like_captcha(raw)[0]:
            return None
    except Exception:
        pass
    return html, final_url


def _set_telemetry(ctx: AdapterContext, **values: Any) -> None:
    try:
        current = getattr(ctx, _TELEMETRY_ATTR, None)
        payload = dict(current) if isinstance(current, dict) else {}
        payload.update(values)
        setattr(ctx, _TELEMETRY_ATTR, payload)
    except Exception:
        pass


async def recover_nesthub_public(ctx: AdapterContext) -> list[dict[str, Any]]:
    """Return exact-property native units from first-party NestHub SSR."""
    if bool(getattr(ctx, _ATTEMPTED_ATTR, False)):
        return []
    html = _body_from_ctx(ctx)
    configured_url = _page_url(ctx)
    if not html or len(html.encode("utf-8", "replace")) > _MAX_HTML_BYTES:
        return []
    configured = _parse_detail(html, configured_url)
    configured_reasons = _configured_identity_reasons(
        configured,
        ctx,
        configured_url,
    )
    if configured_reasons:
        return []
    community_url = _community_url(
        html,
        configured_url,
        str(getattr(ctx, "property_name", "") or ""),
    )
    if not community_url:
        return []

    try:
        setattr(ctx, _ATTEMPTED_ATTR, True)
    except Exception:
        pass
    _set_telemetry(
        ctx,
        attempted=True,
        configured_url=configured_url,
        configured_listing_id=configured["listing_id"],
        configured_status=configured["status"],
        configured_listing_must_not_emit=(configured["status"] == "This Property Is Not Available"),
        community_url=community_url,
    )

    community_response = await _fetch_direct_html(community_url, configured_url)
    if community_response is None:
        _set_telemetry(ctx, failure_reason="community_fetch_failed")
        return []
    community_html, community_final_url = community_response
    if _host(community_final_url) != _host(configured_url):
        _set_telemetry(ctx, failure_reason="community_host_mismatch")
        return []
    published_filter, roster_url = _community_boundary(
        community_html,
        community_final_url,
        ctx,
    )
    if not published_filter or not roster_url:
        _set_telemetry(ctx, failure_reason="community_property_or_roster_boundary_failed")
        return []
    _set_telemetry(
        ctx,
        community_url=community_final_url,
        published_property_filter=published_filter,
        roster_url=roster_url,
    )

    first_response = await _fetch_direct_html(roster_url, community_final_url)
    if first_response is None:
        _set_telemetry(ctx, failure_reason="roster_fetch_failed")
        return []
    first_html, first_final_url = first_response
    if not _same_roster_page(first_final_url, roster_url, 1):
        _set_telemetry(ctx, failure_reason="roster_redirect_mismatch")
        return []

    pending_pages = {1}
    seen_pages: set[int] = set()
    page_payloads: dict[int, tuple[str, str]] = {1: (first_html, first_final_url)}
    rows: list[dict[str, Any]] = []
    page_telemetry: list[dict[str, Any]] = []
    while pending_pages:
        page_number = min(pending_pages)
        pending_pages.remove(page_number)
        if page_number in seen_pages or page_number > _MAX_PAGES:
            _set_telemetry(ctx, failure_reason="roster_pagination_not_bounded")
            return []
        seen_pages.add(page_number)
        if page_number in page_payloads:
            page_html, page_final_url = page_payloads[page_number]
        else:
            page_url = f"{roster_url}?{urlencode({'pg': page_number})}"
            response = await _fetch_direct_html(page_url, roster_url)
            if response is None:
                _set_telemetry(ctx, failure_reason="roster_pagination_fetch_failed")
                return []
            page_html, page_final_url = response
            if not _same_roster_page(page_final_url, roster_url, page_number):
                _set_telemetry(ctx, failure_reason="roster_pagination_redirect_mismatch")
                return []
        parsed = _roster_page(
            page_html,
            page_final_url,
            roster_url,
            page_number,
        )
        if parsed is None:
            _set_telemetry(ctx, failure_reason="roster_page_shape_rejected")
            return []
        page_rows, discovered_pages = parsed
        page_telemetry.append(
            {
                "page": page_number,
                "url": page_final_url,
                "rows": len(page_rows),
            }
        )
        rows.extend(page_rows)
        if len(rows) > _MAX_ROWS:
            _set_telemetry(ctx, failure_reason="roster_row_cap_exceeded")
            return []
        pending_pages.update(discovered_pages - seen_pages)

    expected_pages = set(range(1, max(seen_pages) + 1))
    native_ids = [str(row["provider_listing_id"]) for row in rows]
    if seen_pages != expected_pages or not rows or len(native_ids) != len(set(native_ids)):
        _set_telemetry(ctx, failure_reason="property_identity_or_pagination_rejected")
        return []

    parsed_rows: list[tuple[dict[str, Any], list[str]]] = []
    candidates: list[dict[str, Any]] = []
    for row in rows:
        reasons = _candidate_reasons(row, ctx)
        parsed_rows.append((row, reasons))
        if not reasons:
            candidates.append(row)
    if not candidates or len(candidates) > _MAX_CANDIDATES:
        _set_telemetry(
            ctx,
            pages=page_telemetry,
            portfolio_rows=len(rows),
            failure_reason="no_bounded_exact_address_candidates",
        )
        return []

    accepted: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for row in candidates:
        detail_response = await _fetch_direct_html(
            str(row["detail_url"]),
            roster_url,
        )
        if detail_response is None:
            _set_telemetry(ctx, failure_reason="candidate_detail_fetch_failed")
            return []
        detail_html, detail_final_url = detail_response
        detail = _parse_detail(detail_html, detail_final_url)
        reasons = _detail_reasons(row, detail, ctx, detail_final_url)
        if reasons:
            # Preserve both the roster-level reasons and stronger detail
            # reasons in one auditable rejection list.
            for index, (existing, existing_reasons) in enumerate(parsed_rows):
                if existing is row:
                    parsed_rows[index] = (existing, existing_reasons + reasons)
                    break
            continue
        accepted.append((row, detail, detail_final_url))

    accepted_ids = [str(row["provider_listing_id"]) for row, _, _ in accepted]
    accepted_units = [str(row["unit_number"]).casefold() for row, _, _ in accepted]
    if (
        not accepted
        or len(accepted_ids) != len(set(accepted_ids))
        or len(accepted_units) != len(set(accepted_units))
    ):
        _set_telemetry(ctx, failure_reason="no_unique_detail_revalidated_rows")
        return []

    units: list[dict[str, Any]] = []
    for row, detail, detail_final_url in accepted:
        floor_plan_name = str(detail["floor_plan_names"][0])
        rent = int(detail["rent"])
        unit = make_unit_dict(
            floor_plan_name=floor_plan_name,
            bed_label=bed_label_from(str(detail["bedrooms"]), floor_plan_name),
            bedrooms=str(detail["bedrooms"]),
            bathrooms=str(detail["bathrooms"]),
            sqft=str(detail["sqft"]),
            unit_number=str(row["unit_number"]),
            unit_name=str(detail["street"]),
            rent_low=rent,
            rent_high=rent,
            availability_status="AVAILABLE",
            available_units="1",
            availability_date=str(detail["availability_date"]),
            source_api_url=detail_final_url,
            extraction_tier="TIER_1_PUBLIC_NESTHUB_SSR_EXACT_PROPERTY",
            source_ids={
                "nesthub_listing_id": str(row["provider_listing_id"]),
            },
        )
        unit.update(
            {
                "provider_unit_address": str(detail["street"]),
                "availability_text": str(row["availability_text"]),
                "availability_date_provenance": ("provider_roster_and_detail_exact_date_agree"),
                "floor_plan_name_provenance": ("provider_detail_scoped_the_name_is_a_bedroom_sentence"),
                "source_listing_url": detail_final_url,
                "source_portal_url": roster_url,
                "source_community_url": community_final_url,
                "source_property_name": str(getattr(ctx, "property_name", "") or ""),
                "source_property_provenance": (
                    "exact_configured_nesthub_detail_same_host_community_"
                    "published_filter_roster_exact_address_detail_revalidation"
                ),
            }
        )
        units.append(unit)

    rejected = [
        {
            "provider_listing_id": str(row.get("provider_listing_id") or ""),
            "location": str(row.get("location") or ""),
            "reasons": reasons,
        }
        for row, reasons in parsed_rows
        if reasons
    ]
    _set_telemetry(
        ctx,
        community_url=community_final_url,
        roster_url=roster_url,
        published_property_filter=published_filter,
        pages=page_telemetry,
        portfolio_rows=len(rows),
        exact_address_candidates=len(candidates),
        accepted_rows=len(units),
        native_listing_ids=accepted_ids,
        rejected_rows=rejected,
        failure_reason="",
    )
    return units


__all__ = ["recover_nesthub_public"]
