"""Strict page-local recovery for first-party team-card unit rosters.

Tor View Village publishes current apartments inside WordPress ``team-list``
floor-plan cards.  A listing label contains three different concepts:
``21I`` (physical unit identity), ``Hasbrouck Drive`` (location evidence), and
the enclosing ``A Style`` heading (floor-plan association).  This parser keeps
those fields separate and never treats either the street or plan as a unit id.

No I/O occurs here.  The configured page must prove exact property identity,
the final URL may not cross hosts, and every priced availability row must fit
one unambiguous card-local shape or the whole recovery fails closed.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from ma_poc.pms.adapters._parsing import bed_label_from, format_rent_range, make_unit_dict
from ma_poc.pms.adapters.base import AdapterContext

_UNIT_LABEL_RE = re.compile(
    r"^(?P<unit>\d{1,4}[A-Z])\s+"
    r"(?P<street>[A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*){0,3}\s+"
    r"(?:Avenue|Boulevard|Circle|Court|Drive|Lane|Place|Road|Street|Way))"
    r"\s*(?:[-–—]\s*)?\$\s*(?P<rent>[\d,]+(?:\.\d{1,2})?)$",
    re.IGNORECASE,
)
_PLAN_RE_TEMPLATE = (
    r"\b(?P<beds>[0-6])\s+Bedrooms?\s*[-–—]\s*{plan}\b"
)
_SQFT_RE = re.compile(r"\bSQFT\s*[-–—]\s*(\d{3,5})\b", re.IGNORECASE)
_ADDRESS_ALIASES = {
    "avenue": "ave",
    "boulevard": "blvd",
    "circle": "cir",
    "court": "ct",
    "drive": "dr",
    "east": "e",
    "lane": "ln",
    "place": "pl",
    "road": "rd",
    "street": "st",
    "west": "w",
}
_NAME_NOISE = {"apartment", "apartments", "at", "community", "homes", "of", "the"}
_STATE_FULL_NAMES = {"NY": "new york"}


def _norm(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _norm_address(value: object) -> str:
    return " ".join(_ADDRESS_ALIASES.get(token, token) for token in _norm(value).split())


def _contains_phrase(haystack: str, needle: str) -> bool:
    return bool(needle and f" {needle} " in f" {haystack} ")


def _body_and_url(ctx: AdapterContext) -> tuple[str, str, str]:
    fetch_result = getattr(ctx, "fetch_result", None)
    body = getattr(fetch_result, "body", None) if fetch_result is not None else None
    if isinstance(body, bytes):
        html = body.decode("utf-8", errors="replace")
    elif isinstance(body, str):
        html = body
    else:
        html = ""
    final_url = str(
        getattr(fetch_result, "final_url", "") if fetch_result is not None else ""
    ) or str(getattr(ctx, "base_url", "") or "")
    base_url = str(getattr(ctx, "base_url", "") or "")
    return html, final_url, base_url


def _same_configured_host(final_url: str, base_url: str) -> bool:
    def _host(url: str) -> str:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        return (parsed.hostname or "").casefold().removeprefix("www.")

    final_host = _host(final_url)
    base_host = _host(base_url)
    return bool(final_host and base_host and final_host == base_host)


def _property_identity_matches(soup: BeautifulSoup, ctx: AdapterContext) -> bool:
    visible = _norm(soup.get_text(" ", strip=True))
    visible_words = set(visible.split())
    address_visible = _norm_address(visible)
    name = " ".join(
        token
        for token in _norm(getattr(ctx, "property_name", "")).split()
        if token not in _NAME_NOISE
    )
    address = _norm_address(getattr(ctx, "address", ""))
    city = _norm(getattr(ctx, "city", ""))
    state = str(getattr(ctx, "state", "") or "").strip().upper()
    state_name = _STATE_FULL_NAMES.get(state, "")
    zip_code = re.sub(r"\D", "", str(getattr(ctx, "zip_code", "") or ""))[:5]
    state_matches = state.casefold() in visible_words or _contains_phrase(visible, state_name)
    return bool(
        name
        and _contains_phrase(visible, name)
        and address
        and _contains_phrase(address_visible, address)
        and city
        and _contains_phrase(visible, city)
        and state
        and state_matches
        and len(zip_code) == 5
        and zip_code in visible_words
    )


def has_static_team_unit_roster_shape(ctx: AdapterContext) -> bool:
    """True only for the distinctive server-rendered team-card roster."""
    html, final_url, base_url = _body_and_url(ctx)
    if not html or not _same_configured_host(final_url, base_url):
        return False
    parsed = urlparse(final_url if "://" in final_url else f"https://{final_url}")
    if parsed.path not in {"", "/"}:
        return False
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select(".team-list .team-detail")
    return bool(
        cards
        and any(
            "available units" in _norm(card.get_text(" ", strip=True))
            and "$" in card.get_text(" ", strip=True)
            for card in cards
        )
    )


def recover_static_team_unit_roster(ctx: AdapterContext) -> list[dict[str, Any]]:
    """Return card-local physical units or ``[]`` on any ambiguity."""
    html, page_url, base_url = _body_and_url(ctx)
    if not has_static_team_unit_roster_shape(ctx):
        return []
    soup = BeautifulSoup(html, "lxml")
    if not _same_configured_host(page_url, base_url) or not _property_identity_matches(soup, ctx):
        return []

    cards = soup.select(".team-list")
    if not cards or len(cards) > 100:
        return []
    units: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for card in cards:
        if not isinstance(card, Tag):
            return []
        detail = card.select_one(":scope > .team-detail")
        heading = detail.find("h3") if isinstance(detail, Tag) else None
        if not isinstance(detail, Tag) or not isinstance(heading, Tag):
            return []
        card_text = " ".join(detail.get_text(" ", strip=True).split())
        priced_links = [
            link
            for link in detail.select("a[href]")
            if "$" in link.get_text(" ", strip=True)
        ]
        if not priced_links:
            continue
        if "available units" not in _norm(card_text):
            return []

        plan_name = " ".join(heading.get_text(" ", strip=True).split())
        plan_matches = list(
            re.finditer(
                _PLAN_RE_TEMPLATE.format(plan=re.escape(plan_name)),
                card_text,
                re.IGNORECASE,
            )
        )
        sqft_matches = _SQFT_RE.findall(card_text)
        if len(plan_matches) != 1 or len(set(sqft_matches)) != 1:
            return []
        beds = int(plan_matches[0].group("beds"))
        sqft = int(sqft_matches[0])
        if not 150 <= sqft <= 10_000:
            return []

        for link in priced_links:
            label = " ".join(link.get_text(" ", strip=True).replace("\xa0", " ").split())
            match = _UNIT_LABEL_RE.fullmatch(label)
            if match is None:
                return []
            unit_number = match.group("unit").upper()
            street_label = " ".join(match.group("street").split())
            try:
                rent = int(float(match.group("rent").replace(",", "")))
            except (TypeError, ValueError):
                return []
            if not 200 <= rent <= 50_000:
                return []

            listing_url = str(link.get("href") or "").strip()
            listing = urlparse(listing_url)
            if listing.scheme not in {"http", "https"} or not listing.hostname:
                return []
            unit = make_unit_dict(
                floor_plan_name=plan_name,
                bed_label=bed_label_from(beds, plan_name),
                bedrooms=str(beds),
                bathrooms="",
                sqft=str(sqft),
                unit_number=unit_number,
                unit_name=f"{unit_number} {street_label}",
                rent_low=rent,
                rent_high=rent,
                rent_range=format_rent_range(rent, rent),
                availability_status="AVAILABLE",
                available_units="1",
                availability_date="",
                source_api_url=page_url,
                extraction_tier="TIER_1_DOM_STATIC_TEAM_UNIT_ROSTER",
                data_gaps=["bathrooms", "availability_date"],
                data_quality_flag="STATIC_TEAM_UNIT_ROSTER_BATH_DATE_NOT_PUBLISHED",
            )
            unit.update(
                {
                    "source_native_unit_id": unit_number,
                    "source_unit_address_label": f"{unit_number} {street_label}",
                    "source_street_label": street_label,
                    "source_listing_url": listing_url,
                    "source_property_name": str(getattr(ctx, "property_name", "") or ""),
                    "source_property_provenance": "exact_configured_property_team_card_roster",
                    "floor_plan_name_provenance": "enclosing_first_party_team_card_heading",
                    "availability_date_provenance": "current_roster_no_explicit_date",
                }
            )
            units.append(unit)
            evidence.append(
                {
                    "unit_number": unit_number,
                    "street_label": street_label,
                    "floor_plan_name": plan_name,
                    "rent": rent,
                }
            )

    native_ids = [str(unit.get("source_native_unit_id") or "").casefold() for unit in units]
    address_labels = [str(unit.get("source_unit_address_label") or "").casefold() for unit in units]
    if not units or len(native_ids) != len(set(native_ids)) or len(address_labels) != len(set(address_labels)):
        return []
    try:
        ctx._static_team_unit_roster_telemetry = {
            "accepted_units": len(units),
            "source_url": page_url,
            "identity_fields_kept_separate": True,
            "rows": evidence,
        }
    except Exception:
        pass
    return units


__all__ = ["has_static_team_unit_roster_shape", "recover_static_team_unit_roster"]
