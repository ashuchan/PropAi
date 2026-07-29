"""AppFolio-Websites-on-Duda recovery path (deep-probe 2026-05-25).

Background
----------
A material slice of AppFolio properties publish their listings through the
``AppFolio Websites`` product — a Duda-hosted marketing CMS that mirrors the
operator's AppFolio listings into a Duda *collection* called
``appfolio-listings``. The AppFolio listing widget on the page renders
client-side from this collection via Duda's public collections REST API.

Statically, these pages look like AppFolio (they ship the
``cdn.appfoliowebsites.com/sites/resources/js/appfolio-global-scripts.js``
loader, and decoded base64 bindings reference ``site_collection.appfolio
-listings``), but they do NOT carry an ``<slug>.appfolio.com/listings``
iframe — the existing vanity-fallback path therefore returns 0 units.

Eight-of-eighty CSV sample (~10%): livescs, parkviewspringhill,
beaumontcove, pearlinvestment, artistsvillage, liveatthebiltmore,
mall-apartments — all serve full unit data through the same Duda
collection endpoint::

    GET https://{host}/rts/collections/public/{site_id}/runtime/collection
        /appfolio-listings/query-data?pageSize=100&pageNumber={N}&query=()
        &language=ENGLISH

Each record carries the standard AppFolio fields (``market_rent``,
``bedrooms``, ``bathrooms``, ``square_feet``, ``available``,
``available_date``, ``full_address``, ``listable_uid``,
``unit_template_name``, ``property_lists``).

Per-property pages scope the collection via a ``propertyGroup`` value
which the widget filters on by matching against ``property_lists[].name``
case-insensitively.

This module is the self-contained recovery.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

from ma_poc.pms.adapters._parsing import (
    address_unit_id,
    bed_label_from,
    format_rent_range,
    make_unit_dict,
)

log = logging.getLogger(__name__)


# Definitive marker: every AppFolio Websites page injects this loader.
_APPFOLIO_WEBSITES_MARKER_RE = re.compile(
    r"cdn\.appfoliowebsites\.com/sites/resources/",
    re.IGNORECASE,
)

# Duda site id from any cdn-website.com asset URL. The site id is the
# hex token in the URL prefix (verified consistent across irp/lirp/static
# subdomains on SCS, Beaumont Cove, Parkview, etc.).
_DUDA_SITE_ID_RE = re.compile(
    r"(?:irp|lirp|static)\.cdn-website\.com/(?P<id>[a-f0-9]{6,})/",
    re.IGNORECASE,
)

# Property-page widget config is stored as base64-encoded JSON in any of
# Duda's binding/data attributes. The encoded blob contains
# ``propertyGroup`` among other widget config keys. Long base64-ish
# tokens (>=64 chars) are the search universe.
_BASE64_TOKEN_RE = re.compile(r"[A-Za-z0-9+/]{64,}={0,2}")


def is_appfolio_websites_cms(html: str) -> bool:
    """Return True when the HTML carries the AppFolio Websites loader.

    The marker is the inclusion of
    ``cdn.appfoliowebsites.com/sites/resources/`` — that path is unique
    to the AppFolio Websites CMS and isn't present on bare AppFolio
    embed iframes or on the operator's separate marketing widgets.
    """
    if not html:
        return False
    return bool(_APPFOLIO_WEBSITES_MARKER_RE.search(html))


def extract_duda_site_id(html: str) -> str | None:
    """Return the Duda site id from any cdn-website.com asset URL.

    The site id is the hex token in URLs like
    ``https://irp.cdn-website.com/3885d159/...``. Used to construct the
    public collection-API URL.
    """
    if not html:
        return None
    m = _DUDA_SITE_ID_RE.search(html)
    return m.group("id").lower() if m else None


def extract_appfolio_websites_property_group(html: str) -> str | None:
    """Return ``propertyGroup`` from any base64-encoded widget config.

    Property pages on AppFolio Websites scope the listings widget via a
    ``propertyGroup`` value that lives inside a base64 JSON blob in one
    of Duda's binding/data attributes. Decode every long base64-ish
    token in the page and return the first ``propertyGroup`` string we
    find. Returns ``None`` when there's no per-property filter (site-
    wide pages like ``/availability`` set ``propertyGroup: ""``).
    """
    if not html:
        return None
    for m in _BASE64_TOKEN_RE.finditer(html):
        token = m.group(0)
        # Length must be a multiple of 4 for stdlib base64 (pad if needed).
        padded = token + ("=" * (-len(token) % 4))
        try:
            decoded = base64.b64decode(padded, validate=False).decode(
                "utf-8", errors="replace"
            )
        except (binascii.Error, ValueError):
            continue
        if "propertyGroup" not in decoded:
            continue
        try:
            obj = json.loads(decoded)
        except (json.JSONDecodeError, ValueError):
            continue
        # Two shapes seen in the wild:
        #   1. {"propertyGroup": "SCS Athens", "initialSort": ..., ...}
        #   2. [{"bindingName": "propertyGroup", "value": "..."}, ...]
        # Shape 2's value is itself a binding path like
        # ``dynamic_page_collection.Property Groups`` (NOT a real group
        # value); only shape 1's value is the literal group name.
        if isinstance(obj, dict):
            pg = obj.get("propertyGroup")
            if isinstance(pg, str) and pg.strip():
                return pg.strip()
    return None


def listing_matches_property_group(
    listing_data: dict[str, Any], property_group: str | None
) -> bool:
    """Return True when the listing belongs to ``property_group``.

    The widget filter is case-insensitive against the ``name`` field
    inside each entry of ``property_lists``. When ``property_group`` is
    None or empty, ALL listings match (site-wide page).
    """
    if not property_group:
        return True
    target = property_group.strip().lower()
    if not target:
        return True
    pls = listing_data.get("property_lists") or []
    if not isinstance(pls, list):
        return False
    for entry in pls:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name.strip().lower() == target:
                return True
    return False


def parse_appfolio_websites_listing(
    listing: dict[str, Any], source_url: str
) -> dict[str, Any] | None:
    """Convert one Duda collection record into a standard unit dict.

    Returns ``None`` when the record carries neither a numeric rent NOR
    any bed/bath/sqft dimension (matches the unit-validity gate that
    drops dim-less rows downstream).
    """
    if not isinstance(listing, dict):
        return None
    data = listing.get("data")
    if not isinstance(data, dict):
        return None

    rent_low: int | None = None
    rent_high: int | None = None
    rent_range_val = data.get("rent_range")
    if isinstance(rent_range_val, list) and len(rent_range_val) >= 1:
        try:
            rent_low = int(float(rent_range_val[0]))
        except (TypeError, ValueError):
            rent_low = None
        if len(rent_range_val) >= 2:
            try:
                rent_high = int(float(rent_range_val[1]))
            except (TypeError, ValueError):
                rent_high = None
    if rent_low is None:
        mr = data.get("market_rent")
        if mr is not None:
            try:
                rent_low = int(float(mr))
                rent_high = rent_low
            except (TypeError, ValueError):
                pass

    beds_raw = data.get("bedrooms")
    beds: int | None
    try:
        beds = int(beds_raw) if beds_raw is not None else None
    except (TypeError, ValueError):
        beds = None

    baths_raw = data.get("bathrooms")
    baths: float | None
    try:
        baths = float(baths_raw) if baths_raw is not None else None
    except (TypeError, ValueError):
        baths = None

    sqft_raw = data.get("square_feet")
    sqft = ""
    if sqft_raw is not None:
        try:
            sqft = str(int(float(sqft_raw)))
        except (TypeError, ValueError):
            sqft = ""

    # Drop rows with no numeric dimension AND no rent — they're the same
    # rows post_process drops later, so skip them up-front.
    if rent_low is None and beds is None and baths is None and not sqft:
        return None

    # Unit number: prefer ``address_address2`` (the apartment suffix —
    # ``#11``, ``#601``); fall back to the listing id.
    addr2 = (data.get("address_address2") or "").strip().lstrip("#")
    listable_uid = (data.get("listable_uid") or "").strip()
    appfolio_id_raw = data.get("id")
    appfolio_id = str(appfolio_id_raw) if appfolio_id_raw is not None else ""
    unit_number = addr2 or listable_uid[:12] or appfolio_id

    # 2026-07-28: ``unit_template_name`` is AppFolio's REAL plan label and is
    # the only one of these three fields that is one. ``full_address`` is a
    # street address and ``marketing_title`` is free-text marketing copy —
    # chaining them as fallbacks put addresses into the plan-name column
    # (the same defect the SSR/VANITY card parser had, which accounted for
    # 11,877 rows in run-2026-07-27-full-0d54ca7). Keep the plan name when
    # the operator publishes one; otherwise leave it EMPTY and route the
    # address to its own field rather than manufacturing a plan name.
    floor_plan_name = (data.get("unit_template_name") or "").strip()
    full_address = (data.get("full_address") or "").strip()
    unit_name = full_address or (data.get("marketing_title") or "").strip()

    avail = data.get("available")
    avail_status = "AVAILABLE" if avail else "UNAVAILABLE"
    avail_date = (data.get("available_date") or "").strip() if isinstance(
        data.get("available_date"), str
    ) else ""

    deposit_raw = data.get("deposit")
    deposit = ""
    if deposit_raw is not None:
        try:
            deposit = f"${int(float(deposit_raw)):,}"
        except (TypeError, ValueError):
            deposit = ""

    source_ids = {
        k: v
        for k, v in {
            "appfolio_listable_uid": listable_uid or None,
            "appfolio_id": appfolio_id or None,
            "appfolio_database_name": (data.get("database_name") or "").strip()
            or None,
            # 2026-07-14: keep the full street address as provenance so the
            # combine-point scattered-site id resolver (which sees all pages)
            # can identify which units carry an address-derived unit_id.
            "appfolio_full_address": (data.get("full_address") or "").strip()
            or None,
        }.items()
        if v
    }

    unit = make_unit_dict(
        floor_plan_name=floor_plan_name,
        unit_name=unit_name,
        bed_label=bed_label_from(beds, floor_plan_name),
        bedrooms=str(beds) if beds is not None else "",
        bathrooms=str(baths) if baths is not None else "",
        sqft=sqft,
        unit_number=unit_number,
        rent_range=format_rent_range(rent_low, rent_high),
        rent_low=rent_low,
        rent_high=rent_high,
        deposit=deposit,
        availability_status=avail_status,
        availability_date=avail_date,
        source_api_url=source_url,
        extraction_tier="TIER_1_API_APPFOLIO_DUDA",
    )
    # ``make_unit_dict`` on main doesn't accept ``source_ids`` as a kwarg
    # (the param is added on the cobblestone canary branch). Attaching the
    # provenance dict after construction keeps this helper portable
    # across both branch shapes — the downstream consumers read
    # ``unit["source_ids"]`` directly.
    unit["source_ids"] = source_ids
    # 2026-07-14 identity-layer fix: scattered-site AppFolio Websites feeds
    # carry the full street address in ``full_address``. Anchor unit_id to it
    # (marketing-visible + run-stable) instead of the volatile listable_uid /
    # AppFolio id. No-op when full_address isn't address-shaped.
    addr_uid = address_unit_id((data.get("full_address") or "").strip())
    if addr_uid:
        unit["unit_id"] = addr_uid
    return unit


def collection_url(host: str, site_id: str, page_number: int = 0) -> str:
    """Construct the Duda public collection-API URL for AppFolio listings.

    ``host`` is the scheme+netloc (e.g. ``https://www.livescs.com``).
    ``site_id`` is the hex token returned by :func:`extract_duda_site_id`.
    ``page_number`` is the 0-indexed page; the default page size is 100.
    """
    host = host.rstrip("/")
    return (
        f"{host}/rts/collections/public/{site_id}/runtime/collection/"
        f"appfolio-listings/query-data?pageSize=100&pageNumber={page_number}"
        f"&query=()&language=ENGLISH"
    )


def origin_from_url(url: str) -> str:
    """Return ``scheme://host`` for the URL, or '' if it can't be parsed."""
    if not url:
        return ""
    try:
        p = urlparse(url)
    except ValueError:
        return ""
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


def parse_collection_payload(
    payload: dict[str, Any],
    source_url: str,
    property_group: str | None,
) -> tuple[list[dict[str, Any]], int]:
    """Parse one collection-API page into unit dicts + remaining-pages count.

    Returns ``(units, total_pages)`` where ``total_pages`` is the total
    number of pages the API reports (used by the caller to drive
    pagination). When ``property_group`` is set, listings whose
    ``property_lists`` does NOT include a matching entry are filtered
    out before parsing.
    """
    if not isinstance(payload, dict):
        return [], 0
    values = payload.get("values") or []
    if not isinstance(values, list):
        return [], 0
    page_info = payload.get("page") or {}
    try:
        total_pages = int(page_info.get("totalPages") or 0)
    except (TypeError, ValueError):
        total_pages = 0

    units: list[dict[str, Any]] = []
    for v in values:
        if not isinstance(v, dict):
            continue
        data = v.get("data")
        if not isinstance(data, dict):
            continue
        if not listing_matches_property_group(data, property_group):
            continue
        unit = parse_appfolio_websites_listing(v, source_url)
        if unit is not None:
            units.append(unit)
    return units, total_pages
