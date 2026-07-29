"""
AppFolio adapter.

Research log
------------
Web sources consulted:
  - https://www.appfolio.com/ — AppFolio property management platform (accessed 2026-04-17)
  - https://www.appfolio.com/property-manager — Listing format documentation
Real payloads inspected (from data/runs/*/raw_api/):
  - 12617 (Stoney Brook) — /api/v1/community_info/ returning community-level metadata
    (name, address, total_unit_count, available_unit_count) but no unit-level data
  - 12807 — /api/v3/tokens/lists/ returning pagination wrapper with community info
Key findings:
  - API endpoint: /api/v1/community_info/, /api/v1/community_extra_info/,
    /api/v3/tokens/lists/ — these return property-level metadata only
  - AppFolio listing pages typically embed unit data in HTML or use the
    tenant-application form endpoint under /listings/ for individual unit
    detail (path is on the resolver blacklist — see ma_poc/pms/resolver.py)
  - Response envelope: {meta: {limit, total_count, offset}, objects: [...]}
  - Unit ID field: not available in captured community-level APIs
  - Rent field(s): not available in community-level responses; unit pages have price in HTML
  - Known gotchas: AppFolio community API does not contain unit-level pricing; unit data
    comes from DOM parsing of the listing page. AppFolio uses a standard listing card
    layout with .js-listing-card containers. Less than 3 real payloads with unit data
    available — adapter handles API where present and falls through to DOM parsing.
"""

from __future__ import annotations

import json
import logging
import re
from enum import Enum
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._appfolio_websites_duda import (
    collection_url,
    extract_appfolio_websites_property_group,
    extract_duda_site_id,
    is_appfolio_websites_cms,
    origin_from_url,
    parse_collection_payload,
)
from ma_poc.pms.adapters._parsing import (
    address_unit_id,
    bed_label_from,
    format_rent_range,
    get_field,
    make_unit_dict,
    money_to_int,
    resolve_scattered_site_ids,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.appfolio_urls import (
    APPFOLIO_RESERVED_SLUGS,
    is_account_index_url,
    scoped_listings_url,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

# Bug 7 (2026-05-09 deep-dive): AppFolio /listings/detail/<uuid> single-property
# detail pages aren't matched by ``data-listing-id=`` (that's the index marker)
# and don't return the ``{"objects": [...]}`` API envelope. Today the adapter
# bails on these pages even though the unit data is right there in the HTML.
# These regexes pull the standard rent/bed/bath/sqft fields from the detail
# page's main content.
_DETAIL_RENT_RE = re.compile(
    r"\$([0-9,]{3,7})(?:\s*/?\s*month|\s*/?\s*mo)?", re.IGNORECASE
)
_DETAIL_BED_RE = re.compile(r"(\d+)\s*(?:bed|bd|br)\b", re.IGNORECASE)
_DETAIL_BATH_RE = re.compile(r"(\d+(?:\.\d)?)\s*(?:bath|ba)\b", re.IGNORECASE)
_DETAIL_SQFT_RE = re.compile(
    r"([0-9,]{3,5})\s*(?:sq\s*ft|sqft|sf)\b", re.IGNORECASE
)
_DETAIL_H1_RE = re.compile(
    r"<h1[^>]*>(?P<txt>[^<]{1,200})</h1>", re.IGNORECASE | re.DOTALL
)

# 2026-05-25 (deep-probe sqft=-1 cohort): AppFolio operators publish
# non-housing listings (parking spaces, storage units, garages) into the
# same /listings endpoint as actual apartments. These show up with
# very-low rents ($200-$400) and addresses containing "Non-Resident
# Parking", "Storage", "Garage", or "Locker". Skipping them removes
# false-positive zero-sqft "units" + drops them from QC counts.
# Sample signature: pid=54745 unit='05' fp='...Non-Resident Parking 05'
# rent=$300.
_NON_HOUSING_RE = re.compile(
    r"\b(?:"
    r"parking|garage|locker|storage|"
    r"bike\s+(?:room|storage)|"
    r"non[-\s]?resident|"
    r"car\s*port"
    r")\b",
    re.IGNORECASE,
)


def _is_non_housing_listing(*text_fields: str) -> bool:
    """Return True if any text_field contains a non-housing keyword.

    Used to skip AppFolio listings for parking spaces, storage units, etc.
    that share the /listings endpoint with actual apartment listings.

    Pass only NAMING fields — the address, the API item's ``name`` /
    ``listing_type``. Never pass a card body, a description or an amenity
    list: those state what an apartment HAS, not what the listing IS, and
    every one of "ample storage", "attached garage", "off-street parking",
    "Bike Room", "Covered Carport" is ordinary apartment copy. Measured
    2026-07-29 across 65 live tenants: scanning the card body dropped 2,352
    of 8,430 rows and caught nothing the address arm did not (see the call
    site in ``parse_appfolio_listings_ssr``).
    """
    for t in text_fields:
        if t and _NON_HOUSING_RE.search(t):
            return True
    return False
_DETAIL_MAIN_RE = re.compile(
    r"<main[^>]*>(?P<inner>.*?)</main>", re.IGNORECASE | re.DOTALL
)
_DETAIL_TAG_RE = re.compile(r"<[^>]+>")

# Vanity-domain slug discovery: every AppFolio-hosted vanity site references
# its PMC subdomain at least once (a ``connect`` / ``request_access`` link).
# We extract that slug and follow it to ``<slug>.appfolio.com/listings``.
# Source: 2026-04-30 failure-recovery investigation (33/46 recoveries).
_APPFOLIO_SLUG_RE = re.compile(
    r"https?://([a-z0-9-]+)\.appfolio\.com", re.IGNORECASE
)
# One list, defined in ``ma_poc.pms.appfolio_urls`` so the URL grader and the
# slug discovery below cannot drift apart. Alias kept — it is the name this
# module and its tests have used since 2026-04-30.
_APPFOLIO_SKIP_SLUGS = APPFOLIO_RESERVED_SLUGS


# 2026-05-25 (canary 1ef1060 regr#11 follow-up): also recognise the
# ``hostUrl: 'X.appfolio.com'`` embed-JS config form. PROSPER Azalea
# City's /check-availability page sets ``hostUrl: 'dlpcapital.appfolio
# .com'`` in the Appfolio.Listing widget config — no ``https://``
# prefix, so the legacy slug regex above misses it entirely. This is
# why the propertyGroup filter wasn't getting applied: we had the
# propertyGroup but no slug → couldn't build the listings URL.
_APPFOLIO_HOST_URL_RE = re.compile(
    r"hostUrl\s*:\s*['\"]([a-z0-9-]+)\.appfolio\.com['\"]",
    re.IGNORECASE,
)


def find_appfolio_slug(html: str) -> str | None:
    """Return the first AppFolio PMC slug in the HTML (or None).

    Skips known non-PMC subdomains (www, app, support, etc.). Used by the
    vanity-domain fallback path in :meth:`AppFolioAdapter.extract` when the
    page being scraped is the property's marketing site rather than an
    ``<slug>.appfolio.com`` subdomain.

    2026-05-25 (regr#11 follow-up): tries TWO discovery paths so the
    embed-JS config form is also recognised:
      1. URL form ``https://<slug>.appfolio.com`` (legacy)
      2. Config form ``hostUrl: '<slug>.appfolio.com'`` (new — Prosper
         Azalea City + likely others use this without an https-prefixed
         hostname elsewhere on the page).
    """
    if not html or "appfolio.com" not in html.lower():
        return None
    # Path 1: URL form
    for m in _APPFOLIO_SLUG_RE.finditer(html):
        slug = m.group(1).lower()
        if slug and slug not in _APPFOLIO_SKIP_SLUGS:
            return slug
    # Path 2: embed-JS config form (``hostUrl: 'X.appfolio.com'``)
    host_m = _APPFOLIO_HOST_URL_RE.search(html)
    if host_m:
        slug = host_m.group(1).lower()
        if slug and slug not in _APPFOLIO_SKIP_SLUGS:
            return slug
    return None


# 2026-05-25 (canary 1ef1060 user-flagged regr#11 CRITICAL):
# When a PMC manages multiple properties under one AppFolio account, the
# marketing site embeds the listings widget with a propertyGroup filter
# to scope the listings to just this property:
#
#   Appfolio.Listing({
#     hostUrl: 'dlpcapital.appfolio.com',
#     propertyGroup: 'PM - PROSPER Azalea City',   // ← per-property filter
#     ...
#   })
#
# AppFolio's /listings endpoint accepts the filter via URL parameter
# ``filters[property_list]={propertyGroup}``. Without it, our vanity
# fallback was pulling ALL of the PMC's properties: PROSPER Azalea City
# (Valdosta GA 31602) was getting 300 units from St. Augustine FL 32086
# (a different property in the same PMC). Cohort: 239 AppFolio VANITY
# properties — many on multi-property PMC accounts.
#
# The regex tolerates either ``'PG'`` or ``"PG"`` quotes and any
# whitespace around the colon. Returns the value verbatim (preserves
# spaces / dashes that operators include in their group names).
_APPFOLIO_PROPERTY_GROUP_RE = re.compile(
    r"propertyGroup\s*:\s*['\"]([^'\"]+)['\"]",
)

# The placeholder values in AppFolio's own copy-paste embed snippet. A scope
# read out of the template is worse than no scope: it builds a
# filters[property_list] that matches no server-side list, the widget returns
# zero, and the zero is indistinguishable from "no vacancies".
_PROPERTY_GROUP_PLACEHOLDERS = frozenset(
    {
        "my group name",
        "your group name",
        "group name",
        "property group",
        "propertygroup",
    }
)


def _commented_out(html: str, pos: int) -> bool:
    """True when the match at *pos* sits behind a ``//`` line comment.

    Scheme separators are not comments, so ``://`` is skipped — without that
    check an embed whose ``hostUrl`` carries a full ``https://`` URL on the
    same line would have its real propertyGroup discarded.
    """
    prefix = html[html.rfind("\n", 0, pos) + 1 : pos]
    idx = prefix.find("//")
    while idx != -1:
        if idx == 0 or prefix[idx - 1] != ":":
            return True
        idx = prefix.find("//", idx + 2)
    return False


def find_appfolio_property_group(html: str) -> str | None:
    """Return the ``propertyGroup`` filter from the AppFolio embed JS, or
    None when the listings widget isn't scoped (single-property account).

    The PMC-scoped filter appears in the marketing page's embed JS config
    block, typically inside an ``Appfolio.Listing({...})`` call. When
    present, it MUST be applied to the /listings URL via
    ``filters[property_list]={value}`` or the adapter will pull ALL
    properties under the PMC's account (cross-property contamination).

    2026-07-28 (QC): the snippet AppFolio hands operators ships the scope line
    COMMENTED OUT with a placeholder — ``//propertyGroup: 'My Group Name',`` —
    and Danish Village (pid 46582) serves it verbatim. Reading it built
    ``filters[property_list]=My%20Group%20Name``, which matches no server-side
    list, so the widget returned zero and the property was recorded as having
    no availability. Both guards below are independent on purpose.
    """
    if not html or "propertyGroup" not in html:
        return None
    for m in _APPFOLIO_PROPERTY_GROUP_RE.finditer(html):
        if _commented_out(html, m.start()):
            continue
        group = m.group(1).strip()
        if not group or group.lower() in _PROPERTY_GROUP_PLACEHOLDERS:
            continue
        return group
    return None


# ─────────────────────────────────────────────────────────────────────
# 2026-05-25 (canary 1ef1060 regr#11b follow-up to chip #100):
# Post-fetch address filter for AppFolio multi-property PMC vanity-path
# cross-contamination.
#
# Chip #100 added a URL-level ``filters[property_list]`` filter when the
# embed JS exposes a ``propertyGroup``. But some multi-property PMCs
# (e.g. ``riedman`` — Academy Place in Corning NY) ship an embed JS
# WITHOUT a propertyGroup at all. The propertyGroup-based filter never
# fires for those PMCs, so the vanity /listings response still leaks
# the entire PMC: Academy Place was getting 190 units across Erie PA,
# Canandaigua NY, Grand Island NY, Ithaca NY, Rochester NY, etc.
#
# This filter runs AFTER the SSR parse and operates on the parsed
# units list. It uses the property's CSV-sourced street address +
# ZIP (threaded through ``AdapterContext.address`` / ``zip_code``) to
# drop listings whose address doesn't match the target. Strict by
# default: ZIP must match exactly and street must fuzzy-match at
# ≥85% via rapidfuzz ``token_set_ratio``.
#
# Returns ``(filtered_units, telemetry)``. Telemetry includes
# ``filter_activated``, ``kept``, ``dropped``, and ``reason`` for the
# adapter's error log + downstream observability.
# ─────────────────────────────────────────────────────────────────────


_APPFOLIO_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_APPFOLIO_HOUSE_NUM_RE = re.compile(r"^\s*(\d+)")
_APPFOLIO_APT_SUFFIX_RE = re.compile(
    # ``\b`` anchors are required: without them, the previous ``t``
    # alternative matched the final ``t`` inside ``West`` (followed by
    # `` Third``) and ate half the street name. ``#`` doesn't need word
    # boundary so it's a separate alternative.
    r"\b(?:apt|apartment|unit|suite|ste)\b\.?\s*[\w\-]+|#\s*[\w\-]+",
    re.IGNORECASE,
)
_DIRECTION_WORDS = {
    "n": "north", "s": "south", "e": "east", "w": "west",
    "ne": "northeast", "nw": "northwest", "se": "southeast", "sw": "southwest",
}
_ORDINAL_WORDS = {
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
    "sixth": "6", "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10",
    "eleventh": "11", "twelfth": "12",
}
_STREET_TYPE_NORM = {
    "st": "st", "street": "st",
    "ave": "ave", "avenue": "ave", "av": "ave",
    "rd": "rd", "road": "rd",
    "blvd": "blvd", "boulevard": "blvd",
    "dr": "dr", "drive": "dr",
    "pkwy": "pkwy", "parkway": "pkwy", "pky": "pkwy",
    "ln": "ln", "lane": "ln",
    "ct": "ct", "court": "ct",
    "cir": "cir", "circle": "cir",
    "ter": "ter", "terrace": "ter",
    "pl": "pl", "place": "pl",
    "way": "way",
    "trl": "trl", "trail": "trl",
    "hwy": "hwy", "highway": "hwy",
}


def _normalize_street(s: str) -> str:
    """Normalize a street address for fuzzy comparison.

    Handles common AppFolio listing variations vs CSV-sourced canonical
    addresses: direction abbreviations ("W" ↔ "West"), ordinal forms
    ("3rd" ↔ "Third"), street-type abbreviations ("St" ↔ "Street"),
    apartment/unit suffixes, and punctuation noise.

    "11 W 3rd St" and "11 West Third St., Apt.111" both normalize to
    "11 west 3 st" so ``rapidfuzz.token_set_ratio`` scores them ≈100.
    """
    if not s:
        return ""
    s = s.lower()
    # Drop apt/unit suffixes BEFORE other token rewrites so they don't
    # leak into the comparison string.
    s = _APPFOLIO_APT_SUFFIX_RE.sub(" ", s)
    # Ordinal words → numeric ("third" → "3").
    for word, num in _ORDINAL_WORDS.items():
        s = re.sub(rf"\b{word}\b", num, s)
    # Strip ordinal suffixes ("3rd" → "3", "22nd" → "22").
    s = re.sub(r"(\d+)(?:st|nd|rd|th)\b", r"\1", s)
    # Direction abbreviations → full words ("w" → "west").
    s = re.sub(
        r"\b([nsew]|ne|nw|se|sw)\b\.?",
        lambda m: _DIRECTION_WORDS.get(m.group(1), m.group(1)),
        s,
    )
    # Street-type abbreviations → canonical short form.
    def _norm_type(m: re.Match[str]) -> str:
        return _STREET_TYPE_NORM.get(m.group(1), m.group(1))
    s = re.sub(
        r"\b(street|avenue|av|road|boulevard|drive|parkway|pky|lane|court|circle|terrace|place|trail|highway|st|ave|rd|blvd|dr|pkwy|ln|ct|cir|ter|pl|trl|hwy|way)\b\.?",
        _norm_type,
        s,
    )
    # Strip punctuation, squeeze whitespace.
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _extract_zip(s: str, *, prefer_last: bool = False) -> str:
    """Return the 5-digit ZIP from a string (strips +4 suffix), or ''.

    ``prefer_last=True`` for full listing addresses. A five-digit HOUSE NUMBER
    is indistinguishable from a ZIP by shape, and it comes first:

        "12224 NE 8th St., 210, Bellevue, WA 98005"  → first match ``12224``

    which then never equals the property's real ZIP, so every listing on a
    five-digit-numbered street was rejected as belonging to someone else. US
    addresses put the ZIP last, so the last match is the right one. Bare ZIP
    fields (``ctx.zip_code``) have exactly one match either way.
    """
    if not s:
        return ""
    if prefer_last:
        matches = _APPFOLIO_ZIP_RE.findall(s)
        return matches[-1] if matches else ""
    m = _APPFOLIO_ZIP_RE.search(s)
    return m.group(1) if m else ""


def _extract_house_number(s: str) -> str:
    """Return the leading house number from a street string, or ''."""
    if not s:
        return ""
    m = _APPFOLIO_HOUSE_NUM_RE.search(s)
    return m.group(1) if m else ""


def _strip_leading_house_number(normalized_street: str) -> str:
    """Drop the leading house number from an already-normalized street."""
    return re.sub(r"^\d+[a-z]?\s+", "", normalized_street).strip()


def _street_candidates(listing_address: str) -> list[str]:
    """Comma-segments of *listing_address* that could be the street.

    Usually the street is the first segment. It is not when the operator
    prefixes the community name — AppFolio ships both shapes in one roster:

        "Oakpoint Apartments, 5018 Marconi Avenue #115, Carmichael, CA 95608"
        "River Oaks Apartments, 700 Bogue Rd. #69, Yuba City, CA 95991"

    Reading only segment 0 compares "Oakpoint Apartments" to "5018 Marconi
    Ave" and rejects the property's OWN five units along with the sixteen
    that belong to Yuba City. Later segments qualify only when they start
    with a house number, which keeps city/state/ZIP and bare unit suffixes
    ("A101") out of the comparison.
    """
    segments = [s.strip() for s in listing_address.split(",")]
    candidates = [segments[0]] if segments else []
    candidates += [
        s for s in segments[1:] if s and _APPFOLIO_HOUSE_NUM_RE.search(s)
    ]
    return [c for c in candidates if c]


def _address_matches(
    listing_address: str,
    ctx_address: str,
    ctx_zip: str,
    fuzzy_threshold: int,
) -> bool:
    """Return True when listing_address matches the property's address.

    Match rules (strict):
      - ZIP exact match (after stripping +4 suffix). If ctx_zip is empty,
        this check is skipped (street-only fallback).
      - Street fuzzy match via rapidfuzz token_set_ratio ≥ threshold against
        any comma-segment of the listing that could be a street (see
        ``_street_candidates``). If both sides have leading house numbers they
        must match exactly (rapidfuzz is too lenient on differing numbers) —
        unless the listing is on the same street in the same ZIP, which makes
        it a neighbouring building of the same community.
    """
    if not listing_address:
        return False

    if ctx_zip:
        zip_target = _extract_zip(ctx_zip)
        zip_listing = _extract_zip(listing_address, prefer_last=True)
        if zip_target and zip_listing and zip_target != zip_listing:
            return False

    if not ctx_address:
        return True
    # Take the street portion of the listing, so city/state/zip noise doesn't
    # dilute the fuzzy score. Any comma-segment that could be a street counts.
    return any(
        _street_matches(seg, listing_address, ctx_address, ctx_zip, fuzzy_threshold)
        for seg in _street_candidates(listing_address)
    )


def _street_matches(
    listing_street: str,
    listing_address: str,
    ctx_address: str,
    ctx_zip: str,
    fuzzy_threshold: int,
) -> bool:
    """One street-segment of a listing address vs the property's address."""
    from rapidfuzz import fuzz

    # House number must match exactly when both sides supply one — otherwise
    # "11 W 3rd St" and "171 E First St" can score >85 under token_set_ratio
    # (small token sets, lots of overlap).
    hn_target = _extract_house_number(ctx_address)
    hn_listing = _extract_house_number(listing_street)
    norm_target = _normalize_street(ctx_address)
    norm_listing = _normalize_street(listing_street)
    if not norm_target or not norm_listing:
        return False

    if hn_target and hn_listing and hn_target != hn_listing:
        # 2026-07-28 — a community is not always one street number.
        # The Lofts (pid 299097, CSV 14912 Mallett Rd, Biloxi MS 39532) lists
        # half its units at 15080 Mallett Rd; Conway Club (pid 19154, 1900 S
        # Conway Rd) also lists 1908 and 1910 S Conway Rd. Rejecting on the
        # house number alone threw those away — the very data loss this filter
        # exists to prevent. A DIFFERENT number on the SAME street name in the
        # SAME ZIP is a neighbouring building of the same property; a different
        # street is not, which is what still rejects Chasewood's Lubbock
        # listings (CSV: Amarillo) and Heritage Amity's Perkasie roster (CSV:
        # Douglassville). Both ZIPs must be present — an unverifiable guess
        # stays a reject.
        zip_t = _extract_zip(ctx_zip)
        zip_l = _extract_zip(listing_address, prefer_last=True)
        if not (zip_t and zip_l and zip_t == zip_l):
            return False
        street_t = _strip_leading_house_number(norm_target)
        street_l = _strip_leading_house_number(norm_listing)
        if not street_t or not street_l:
            return False
        return bool(fuzz.token_set_ratio(street_t, street_l) >= fuzzy_threshold)

    return bool(fuzz.token_set_ratio(norm_target, norm_listing) >= fuzzy_threshold)


# ─────────────────────────────────────────────────────────────────────
# 2026-07-28 — ONE strictness contract for the address filter.
#
# Three code paths reached this filter holding three different grades of
# evidence, and two of them independently added a boolean named
# ``strict_scope`` with OPPOSITE defaults and OPPOSITE meanings for the same
# value. Merged naively, the AppFolio-embed recovery's strict-is-True call
# would have stopped meaning "tightened" and started meaning "baseline",
# silently reopening the two pass-throughs it exists to close. A boolean
# cannot carry three states; that is the whole bug. Hence an enum, and hence
# the deliberate absence of any parameter named ``strict_scope`` — a stale
# caller must raise ``TypeError`` rather than quietly flip behaviour.
# ─────────────────────────────────────────────────────────────────────


class ScopeEvidence(Enum):
    """How much the caller actually knows about whose listings these are.

    ``OPERATOR_SCOPED``
        The operator scoped the surface themselves — a
        ``filters[property_list]`` in the URL they published, a
        ``propertyGroup`` in their embed JS, or their own per-property
        marketing page with the cards SSR'd into it. Live-verified
        2026-07-28: ``yourmetropolitan.com/properties/tareyton-estates/``
        serves 2 cards and ``/properties/bala/`` 2 different ones, out of one
        AppFolio account. Most trustworthy grade: rows whose address our
        parser could not read are KEPT (dropping them would manufacture an
        absence out of a parse gap), and a reject-everything outcome returns
        the ORIGINAL rows rather than zeroing the property.

    ``PUBLISHED_INDEX``
        An account-wide roster reached from an index the operator DID publish
        — a ``{tenant}.appfolio.com/listings`` widget on the property's own
        page, or the vanity/DUDA path's own fetch. The historical
        pass-throughs apply: a single-address response is a single property,
        and a ctx with no address and no ZIP cannot be filtered. Nothing
        matching is still the 2026-07-18 demote (emit nothing).

    ``WEAK_EVIDENCE``
        An account-wide roster reached from something that identifies only
        the TENANT: a ``/connect/users/sign_in`` link, a per-unit
        application / showing / detail deep link, a bare tenant root, or a
        URL we synthesized ourselves. Nothing here claimed to be this
        property's listings surface, so the pass-throughs are removed — a
        2-row roster at one street that is NOT this property is exactly the
        shape of pids 237787 / 5346 / 38167 / 50129, and a roster with
        nothing to check it against is not data.
    """

    OPERATOR_SCOPED = "operator_scoped"
    PUBLISHED_INDEX = "published_index"
    WEAK_EVIDENCE = "weak_evidence"


def _listing_address_is_judgeable(listing_address: str) -> bool:
    """True when *listing_address* carries something the address filter
    can actually compare against a CSV property context.

    A listing card whose address span the SSR parser could not read falls
    back to the literal ``AppFolio listing 8925`` — that string has no
    ZIP and no leading house number, so ``_address_matches`` can only ever
    return False for it. Rejecting such a row is a parse gap being
    reported as a mismatch, which is why ``OPERATOR_SCOPED`` keeps it.
    """
    if not listing_address:
        return False
    if _APPFOLIO_ZIP_RE.search(listing_address):
        return True
    return bool(_APPFOLIO_HOUSE_NUM_RE.search(listing_address))


def filter_listings_by_property_address(
    units: list[dict[str, Any]],
    ctx_address: str,
    ctx_zip: str,
    *,
    fuzzy_threshold: int = 85,
    address_field: str = "floor_plan_name",
    evidence: ScopeEvidence = ScopeEvidence.PUBLISHED_INDEX,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filter AppFolio vanity-path units down to those matching ctx address.

    Used as the fallback when chip #100's ``propertyGroup`` URL filter
    cannot fire (multi-property PMC whose embed JS omits propertyGroup —
    e.g. ``riedman`` for Academy Place). The PMC-wide /listings response
    leaks ALL of the PMC's units; this function drops those whose street
    + ZIP don't match the target property.

    Behaviour matrix:
      - No ctx_address AND no ctx_zip → no-op (filter cannot run; pass-through)
      - Only one distinct address in ``units`` → no-op (single-property PMC)
      - Multi-address response + ctx supplied → filter runs; matched units
        returned, plus telemetry (kept / dropped / reason)
      - Filter rejects everything → return the ORIGINAL units with a
        warning in telemetry (``reason='filter_rejected_all_fallback'``).
        Emitting zero units would surface as a worse failure mode than
        the contamination it was trying to fix; defer to validation
        downstream to flag the address mismatch.

    The matrix above is the ``PUBLISHED_INDEX`` grade — the historical
    behaviour, byte for byte, and the default. ``evidence`` selects the other
    two grades; see :class:`ScopeEvidence` for what each one is and why.
    There is deliberately no ``strict_scope`` parameter: that name carried two
    contradictory meanings across the branches this function reconciles, so a
    stale caller raises ``TypeError`` instead of silently flipping.

    Returns ``(filtered_units, telemetry_dict)`` so the caller can log
    activation + drop counts in ``AdapterResult.errors`` for the run
    report.
    """
    telemetry: dict[str, Any] = {
        "filter_activated": False,
        "kept": len(units),
        "dropped": 0,
        "reason": "",
    }

    if not units:
        telemetry["reason"] = "no_units_to_filter"
        return units, telemetry

    weak = evidence is ScopeEvidence.WEAK_EVIDENCE
    lenient = evidence is ScopeEvidence.OPERATOR_SCOPED

    distinct_addresses = {
        (u.get(address_field) or "").strip() for u in units
    }
    distinct_addresses.discard("")
    if len(distinct_addresses) <= 1 and not weak:
        # Single-address response = a single property; nothing to scope.
        telemetry["reason"] = "single_address_in_response"
        return units, telemetry

    if not ctx_address and not ctx_zip:
        if weak:
            # An unscopeable roster with nothing to check it against is not
            # data. "Could not look" is never "it is ours".
            telemetry["filter_activated"] = True
            telemetry["kept"] = 0
            telemetry["dropped"] = len(units)
            telemetry["reason"] = "no_ctx_address_or_zip_demote"
            telemetry["unscopeable"] = True
            return [], telemetry
        # No CSV context to scope by — can't distinguish a legit scattered
        # single-property from a PMC dump, so leave it (rare: CSV row with no
        # address column). The 94-prop contamination cohort all HAVE a ctx
        # ZIP, so they are handled by the rejected-all demote below, not here.
        telemetry["reason"] = "no_ctx_address_or_zip"
        return units, telemetry

    matched: list[dict[str, Any]] = [
        u
        for u in units
        if (lenient and not _listing_address_is_judgeable(u.get(address_field) or ""))
        or _address_matches(
            u.get(address_field) or "", ctx_address, ctx_zip, fuzzy_threshold
        )
    ]
    if not matched and lenient:
        # Every row judgeable, none matched, on a surface the operator scoped
        # themselves. The honest read is "our CSV address disagrees with the
        # operator", not "this property has no units". Keep the rows and let
        # downstream validation flag the mismatch; never invent a zero.
        telemetry["filter_activated"] = True
        telemetry["kept"] = len(units)
        telemetry["dropped"] = 0
        telemetry["reason"] = "filter_rejected_all_lenient_keep"
        return units, telemetry
    if not matched:
        # 2026-07-18 contamination fix: the response is MULTI-address (a
        # whole-PMC scattered dump) and NONE matched the target property's
        # address/ZIP — emit NOTHING so it demotes to FAILED_NO_DATA rather
        # than shipping OTHER properties' (often other-city) rents. Prior
        # behaviour returned the ORIGINAL units here, which WAS the leak
        # (94/259 AppFolio props in the 2026-07-17 canary: identical unit sets
        # across 7+ communities; single props spanning 100+ ZIPs).
        telemetry["filter_activated"] = True
        telemetry["kept"] = 0
        telemetry["dropped"] = len(units)
        telemetry["reason"] = "filter_rejected_all_demote"
        telemetry["unscopeable"] = True
        return [], telemetry

    telemetry["filter_activated"] = True
    telemetry["kept"] = len(matched)
    telemetry["dropped"] = len(units) - len(matched)
    telemetry["reason"] = "address_filter_applied"
    return matched, telemetry


async def _fetch_appfolio_listings_html(url: str, result: Any) -> str:
    """Plain unauthenticated GET of an AppFolio listings URL.

    Returns the response body on HTTP 200, ``""`` on any other status or
    on any transport error — callers treat ``""`` as "could not look",
    never as "nothing there". Errors are appended to *result* so the run
    report can tell a failed scope refetch apart from a scope that
    legitimately returned nothing. Mirrors the vanity path's fetch.
    """
    try:
        import httpx
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,*/*;q=0.8",
        }
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
            r = await c.get(url, headers=headers)
        if r.status_code != 200:
            result.errors.append(
                f"appfolio-scope-fetch-status: url={url} status={r.status_code}"
            )
            return ""
        return str(r.text or "")
    except Exception as exc:
        result.errors.append(
            f"appfolio-scope-fetch-error: url={url} {type(exc).__name__}"
        )
        return ""


def parse_appfolio_detail_page(html: str, source_url: str) -> list[dict[str, Any]]:
    """Bug 7: parse a single AppFolio /listings/detail/<uuid> page into one unit.

    Returns ``[]`` when the page doesn't carry an extractable rent — pages
    without a recognisable ``$XXX`` token are almost certainly the
    sign-in/auth interstitial, not a real listing.
    """
    if not html:
        return []

    main_match = _DETAIL_MAIN_RE.search(html)
    main_html = main_match.group("inner") if main_match else html
    text = _DETAIL_TAG_RE.sub(" ", main_html)
    text = re.sub(r"\s+", " ", text).strip()

    rent = _DETAIL_RENT_RE.search(text)
    if not rent:
        return []
    try:
        rent_val = int(rent.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return []

    h1 = _DETAIL_H1_RE.search(html)
    floor_plan_name = ""
    if h1:
        floor_plan_name = _DETAIL_TAG_RE.sub("", h1.group("txt")).strip()

    beds = _DETAIL_BED_RE.search(text)
    baths = _DETAIL_BATH_RE.search(text)
    sqft = _DETAIL_SQFT_RE.search(text)

    sqft_str = sqft.group(1).replace(",", "") if sqft else ""
    # 2026-05-25 sqft-gap probe (cohort: 1,095 units across ~104 props):
    # 11/11 sampled TIER_1_DOM_APPFOLIO_* sqft="" cases were verified as
    # true operator-data-gaps. Stamp the documented-gap fields so
    # validation.schema_gate._has_area treats the unit as area-present
    # and the verdict ships as SUCCESS, not SUCCESS_PLAN_LEVEL.
    unit: dict[str, Any] = {
        "unit_number": "",
        "floor_plan_name": floor_plan_name,
        "bedrooms": beds.group(1) if beds else "",
        "bathrooms": baths.group(1) if baths else "",
        "sqft": sqft_str,
        "rent_range": f"${rent_val:,}",
        "market_rent_low": rent_val,
        "market_rent_high": rent_val,
        "source_api_url": source_url,
        "extraction_tier": "TIER_1_DOM_APPFOLIO_DETAIL",
    }
    if not sqft_str:
        unit["data_gaps"] = ["sqft"]
        unit["data_quality_flag"] = "SQFT_NOT_PUBLISHED"
    # 2026-07-14 identity-layer fix: the detail page has no unit_number, so
    # without this it falls through to an ``inferred_<hash>`` id. When the h1
    # is a street address (scattered-site homes), anchor unit_id to it — a
    # stable, marketing-visible key instead of a physical-attribute hash.
    addr_uid = address_unit_id(floor_plan_name)
    if addr_uid:
        unit["unit_id"] = addr_uid
    return [unit]


# F11 — SSR DOM card extractors. Verified live against:
#   richelsonmanagement.appfolio.com (8 cards), becovic (300), pillarrei (23),
#   blackrealtymanagement (82), plentyofplaces (44).
# All five tenants emit identical class names; absent classes survive as None.
_LISTING_BLOCK_RE = re.compile(
    r'<[^>]*data-listing-id="(?P<id>[0-9]+)"[^>]*>(?P<body>.*?)(?=<[^>]*data-listing-id="[0-9]+"|<footer|</main|$)',
    re.IGNORECASE | re.DOTALL,
)
_RENT_RE = re.compile(r'js-listing-blurb-rent[^>]*>([^<]+)<', re.IGNORECASE)
_BED_BATH_RE = re.compile(r'js-listing-blurb-bed-bath[^>]*>([^<]+)<', re.IGNORECASE)
_SQFT_RE = re.compile(r'js-listing-square-feet[^>]*>([^<]+)<', re.IGNORECASE)
_AVAIL_RE = re.compile(r'js-listing-available[^>]*>([^<]+)<', re.IGNORECASE)
# 2026-05-24 (audit xlsx 2026-05-23): the prior regex
#   js-listing-address[^>]*>\s*<[^>]+>([^<]+)<
# required an inner ``<a>``/``<i>`` tag between the span and the address
# text. Several tenants (e.g. kelseymanagement / Brantley Pines I,
# americancapitalrealty / Citadel Village) emit the address text DIRECTLY
# inside the span with no inner tag, so address capture failed and
# ``floor_plan_name`` collapsed to the literal ``AppFolio listing
# {listing_id}`` fallback. That made the audit fail with
# ``didn't find this unit`` because we had no real address shown AND
# the unit_number was the internal listing_id (see fix below).
# Make the inner-tag group optional so both shapes match.
_ADDRESS_RE = re.compile(
    r'js-listing-address[^>]*>(?:\s*<[^>]+>)?\s*([^<]+)',
    re.IGNORECASE,
)

# 2026-05-24 (audit xlsx 2026-05-23): the production AppFolio SSR adapter
# stored ``unit_number = listing_id`` — AppFolio's INTERNAL listing id
# (e.g. ``760``, ``5599``). The audit flagged 9 properties with
# "Fail - didn't find this unit" because the website displays the
# apartment number that sits INSIDE the address suffix (e.g.
# ``1422 Som Center Road #810`` → unit ``#810``, not 760).
# This module extracts the unit number from the address string, trying
# the common suffix shapes in priority order. Returns "" when the
# address looks like a single-family / townhouse with no apartment
# unit (e.g. ``355 Monument Road, Jacksonville, FL``).
_UNIT_FROM_ADDR_PATTERNS = [
    # Pattern 1 — hash-prefixed: '#810', '#2D', '#1O', '#3H'
    # Most common — Carlton, Becovic.
    re.compile(r"#\s*([A-Za-z0-9]+(?:-[A-Za-z0-9]+)?)\b"),
    # Pattern 2 — Apt/Apartment/Apt.: 'Apt 429', 'Apartment 8'
    # kelseymanagement (Brantley Pines I shape).
    re.compile(
        r"\bApt(?:\.|artment)?\s+([A-Za-z0-9]+(?:-[A-Za-z0-9]+)?)\b",
        re.IGNORECASE,
    ),
    # Pattern 3 — Suite/Unit/Ste: 'Suite 12', 'Unit 5', 'Ste 200'
    re.compile(
        r"\b(?:Suite|Unit|Ste)\s+([A-Za-z0-9]+(?:-[A-Za-z0-9]+)?)\b",
        re.IGNORECASE,
    ),
    # Pattern 4 — dash-separated suffix before comma: '- V024,',
    # '- 3050-302,', '- 1116,'. bargeprops (Quail Creek) +
    # becovic. Captures the token between '- ' and ',' — handles
    # internal hyphens in the unit id like '3050-302'.
    re.compile(r"-\s+([A-Za-z0-9]+(?:-[A-Za-z0-9]+)?)\s*,"),
    # Pattern 5 — trailing numeric/alphanumeric before first comma:
    # '301 W. Hawkins Parkway 1116, Longview' → '1116'. Must start
    # with a digit OR be letter+digit (e.g. 'G1-47') to avoid matching
    # the street name itself. bargeprops (Quail Creek shape 2).
    re.compile(
        r"\s+(\d+[A-Za-z0-9\-]*|[A-Za-z]\d[A-Za-z0-9\-]*)\s*,"
    ),
    # Pattern 6 — inter-comma alphanumeric token: '4121 San Antonio
    # St, 614, Odessa' → '614', '1349 Redmond Circle, G1-47, Rome' →
    # 'G1-47'. americancapitalrealty (Citadel) + Riverside North.
    re.compile(
        r",\s*(\d+[A-Za-z0-9\-]*|[A-Za-z]+\d[A-Za-z0-9\-]*)\s*,"
    ),
]


def _extract_unit_from_address(addr: str) -> str:
    """Extract the apartment/unit number from a full street address.

    Returns ``""`` (not the listing_id) when no unit suffix is found —
    the caller decides what to do with that signal. Single-family /
    townhouse addresses (e.g. ``355 Monument Road, Jacksonville, FL``)
    correctly yield empty.

    Verified against live-fetched address shapes from 5 AppFolio
    tenants on 2026-05-24 (Carlton, Becovic, Bargeprops, Citadel,
    Kelseymanagement) — see tests for the parametric coverage.
    """
    if not addr:
        return ""
    for pat in _UNIT_FROM_ADDR_PATTERNS:
        m = pat.search(addr)
        if m:
            candidate = m.group(1).strip()
            # Sanity — unit must contain at least one digit OR be a
            # bare letter (e.g. 'D' for building-letter notation).
            # Filters out cases where the regex backtracks onto a
            # city name token like 'Naples' or 'Tampa'.
            if any(c.isdigit() for c in candidate) or (
                len(candidate) == 1 and candidate.isalpha()
            ):
                return candidate
    return ""
# Verified against pablogroup.appfolio.com on 2026-05-05: the redirect lands
# on https://www.appfolio.com/page-not-found-sub which renders a page whose
# <title> includes "AppFolio - Page Not Found" alongside the canonical URL.
# Either signal alone is sufficient to classify the tenant as offboarded.
_OFFBOARDED_RE = re.compile(
    r'appfolio\.com/page-not-found-sub'
    r'|<title>\s*AppFolio\s*-\s*Page\s+Not\s+Found',
    re.IGNORECASE,
)


def _parse_bed_bath(text: str) -> tuple[int | None, float | None]:
    """Parse 'X bd / Y ba' or 'Studio / Y ba' into (beds, baths).

    AppFolio's bed_bath blurb is the only place beds/baths are reliable on
    the SSR card; sqft and rent live in dedicated divs.
    """
    if not text:
        return None, None
    s = text.strip().lower()
    beds: int | None = None
    baths: float | None = None
    if "studio" in s:
        beds = 0
    else:
        m = re.search(r'(\d+)\s*bd', s)
        if m:
            try:
                beds = int(m.group(1))
            except ValueError:
                beds = None
    m = re.search(r'(\d+(?:\.\d+)?)\s*ba', s)
    if m:
        try:
            baths = float(m.group(1))
        except ValueError:
            baths = None
    return beds, baths


def _parse_sqft_blurb(text: str) -> str:
    """Parse 'Square Feet: 1,342' to '1342'."""
    if not text:
        return ""
    m = re.search(r'([0-9][0-9,]*)', text)
    if not m:
        return ""
    return m.group(1).replace(",", "")


def parse_appfolio_listings_ssr(html: str, url: str) -> list[dict[str, str]]:
    """F11: parse AppFolio /listings SSR HTML into unit dicts.

    Uses regex on stable `js-listing-*` class names — every AppFolio tenant
    we sampled emits these identically. Skipping a full HTML parser keeps
    the path dependency-free and fast.
    """
    units: list[dict[str, str]] = []
    addr_units: list[dict[str, Any]] = []
    for m in _LISTING_BLOCK_RE.finditer(html):
        body = m.group("body")
        listing_id = m.group("id")
        rent_m = _RENT_RE.search(body)
        bb_m = _BED_BATH_RE.search(body)
        sqft_m = _SQFT_RE.search(body)
        avail_m = _AVAIL_RE.search(body)
        addr_m = _ADDRESS_RE.search(body)

        rent_val = money_to_int(rent_m.group(1).strip()) if rent_m else None
        beds, baths = _parse_bed_bath(bb_m.group(1) if bb_m else "")
        sqft = _parse_sqft_blurb(sqft_m.group(1) if sqft_m else "")
        avail_raw = avail_m.group(1).strip() if avail_m else ""
        address = addr_m.group(1).strip() if addr_m else ""

        # 2026-05-25: skip non-housing listings (parking, storage, etc.).
        # These pollute the unit count + show up as low-rent zero-sqft rows.
        #
        # 2026-07-29 — the second argument used to be ``body``, the WHOLE
        # card, "as a safety net for variants". Measured live the same day
        # (65 tenants, {slug}.appfolio.com/listings, plain static GET,
        # curl_cffi impersonate=chrome, all 65 HTTP 200):
        #
        #   card containers               8,430
        #   dropped by the ADDRESS arm        1   <- the only genuine catch
        #   dropped by the BODY-only arm  2,352   <- 2,094 have a bed/bath
        #                                            blurb AND rent >= $500
        #
        # The body arm was matching a normal apartment's own amenity list
        # and description: "ample storage", "attached garage", "off-street
        # parking", "Bike Room". richelsonmanagement lost 10 of 10 rows, all
        # on the word "storage" in "lots of storage space with multiple
        # closets". On that sample the body arm contributed 100% of the loss
        # and 0% of the intended benefit — the one true non-housing listing
        # on the whole sample (gmholdings, "2001-15 E Glenwood Ave - Storage
        # Units", $175) is caught by the ADDRESS arm alone.
        #
        # The card title (``js-listing-title``) is NOT a safe substitute
        # either: it is a marketing headline. Scanning it would have dropped
        # a further 199 rows on the same sample ("The Luray - 2 Bedroom with
        # Attached Garage", "FREE RENT! WAIVING ALL PET, AND CARPORT FEES!").
        # The address is the only field that states what the listing IS.
        if _is_non_housing_listing(address):
            continue

        # 2026-05-24 (audit xlsx 2026-05-23): prefer the apartment
        # suffix parsed out of the address (the value the website
        # actually displays). Fall back to listing_id ONLY when the
        # address has no recognisable unit suffix AND we don't want
        # to lose row identity. Preserve listing_id in source_ids for
        # downstream provenance regardless.
        unit_from_addr = _extract_unit_from_address(address)
        unit_number_display = unit_from_addr or listing_id

        # 2026-05-25 sqft-gap probe (cohort: 1,095 units across ~104 props):
        # all 11/11 sampled TIER_1_DOM_APPFOLIO_VANITY[_PLAN_LEVEL] sqft=""
        # cases were verified as true operator-data-gaps (AppFolio listings
        # don't always include sqft). Stamping the documented-gap fields
        # lets validation.schema_gate._has_area treat the unit as area-
        # present so the no_area retry doesn't fire and the verdict lands
        # SUCCESS instead of SUCCESS_PLAN_LEVEL.
        sqft_gap = not sqft or sqft == "0"
        unit = make_unit_dict(
            floor_plan_name=address or f"AppFolio listing {listing_id}",
            bed_label=bed_label_from(beds, address),
            bedrooms=str(beds) if beds is not None else "",
            bathrooms=str(baths) if baths is not None else "",
            sqft=sqft,
            unit_number=unit_number_display,
            # AppFolio's listing address IS the operator's as-displayed label
            # for a scattered-site unit ("1420 Oak St Apt 3"). It is currently
            # also written into floor_plan_name above for want of anywhere
            # better; capture it in its own field so consumers stop having to
            # read an address out of a plan-name column.
            unit_name=address or "",
            rent_range=format_rent_range(rent_val, rent_val),
            availability_status="AVAILABLE",
            availability_date=avail_raw or "",
            source_ids={"appfolio_listing_id": listing_id} if listing_id else {},
            source_api_url=url,
            extraction_tier="TIER_1_DOM_APPFOLIO_SSR",
            data_gaps=["sqft"] if sqft_gap else None,
            data_quality_flag="SQFT_NOT_PUBLISHED" if sqft_gap else "",
        )
        # 2026-07-14 identity-layer fix: scattered-site AppFolio lists each
        # home by its full street address. Anchor unit_id to that address
        # (the marketing-visible, run-stable, collision-free key) instead of
        # the rotating listing_id / bare suffix. Display suffix stays in
        # unit_number. No-op for real multifamily (plan-name floor_plan_name).
        addr_uid = address_unit_id(address)
        if addr_uid:
            unit["unit_id"] = addr_uid
            addr_units.append(unit)
        units.append(unit)
    # Disambiguate no-suffix addresses that AppFolio lists more than once
    # (e.g. "234 Sherman Ave" ×2) with the stable listing id.
    resolve_scattered_site_ids(addr_units)
    return units


def parse_appfolio_listings(items: list[dict[str, Any]], url: str) -> list[dict[str, str]]:
    """Parse AppFolio listing/floorplan objects into standard unit dicts.

    Handles both /listings endpoint (bedrooms, price, sqft) and
    /floorplans/all endpoint (bed, bath, rent, sq_ft).
    """
    units: list[dict[str, str]] = []
    addr_units: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = get_field(item, "name", "listing_type", "property_type", "apartment_type")
        # 2026-05-25: skip non-housing listings (parking spaces, storage,
        # etc.) that share the AppFolio /listings endpoint. See the
        # docstring of _is_non_housing_listing for the signature.
        address_field = get_field(item, "address", "address_address1", "address_line1", "street")
        listing_type = get_field(item, "listing_type", "type", "category")
        if _is_non_housing_listing(name, address_field, listing_type):
            continue
        beds_str = get_field(item, "bed", "bedrooms", "beds", "bedroom_count")
        baths_str = get_field(item, "bath", "bathrooms", "baths", "bathroom_count")
        beds = int(float(beds_str)) if beds_str else None
        baths = int(float(baths_str)) if baths_str else None
        sqft = get_field(item, "sq_ft", "sqft", "square_feet", "squareFeet", "area")

        rent_lo = money_to_int(get_field(item, "price", "rent", "minRent", "asking_rent"))
        rent_hi = money_to_int(get_field(item, "maxRent", "max_rent"))

        unit_num = get_field(item, "unit_number", "unitNumber", "unit_id", "id", "label")
        avail_date = get_field(item, "available_date", "availableDate", "move_in_date")
        status = get_field(item, "status", "availability_status")

        # 2026-05-25 sqft-gap probe: AppFolio /listings + /floorplans/all
        # responses sometimes omit sq_ft/sqft/square_feet/area entirely
        # because the operator hasn't published it. Flag it the same way
        # as the SSR path so downstream gates can distinguish operator-
        # gap from parser-miss.
        sqft_gap = not sqft or str(sqft).strip() in ("", "0")
        unit = make_unit_dict(
            floor_plan_name=name,
            bed_label=bed_label_from(beds, name),
            bedrooms=str(beds) if beds is not None else "",
            bathrooms=str(baths) if baths is not None else "",
            sqft=sqft,
            unit_number=unit_num,
            rent_range=format_rent_range(rent_lo, rent_hi),
            availability_status="AVAILABLE"
            if not status or "avail" in status.lower()
            else status.upper(),
            availability_date=avail_date,
            source_ids={
                k: v
                for k, v in {
                    "appfolio_id": item.get("id"),
                    "appfolio_unit_id": item.get("unit_id"),
                }.items()
                if v
            },
            source_api_url=url,
            extraction_tier="TIER_1_API_APPFOLIO",
            data_gaps=["sqft"] if sqft_gap else None,
            data_quality_flag="SQFT_NOT_PUBLISHED" if sqft_gap else "",
        )
        # 2026-07-14 identity-layer fix: anchor unit_id to the street address
        # for scattered-site listings (the address field, not the plan name,
        # carries the marketing identity here). No-op when not address-shaped.
        addr_uid = address_unit_id(address_field)
        if addr_uid:
            unit["unit_id"] = addr_uid
            addr_units.append(unit)
        units.append(unit)
    resolve_scattered_site_ids(addr_units)
    return units


def _is_appfolio_response(body: Any) -> bool:
    """Check if a response body looks like AppFolio listing/floorplan data.

    Requires at least two unit-signal keys (price/rent + beds/sqft) to avoid
    false positives on community-level metadata endpoints.

    AppFolio/Apts247 uses: bed, bath, rent, sq_ft, name (floorplans endpoint)
    or bedrooms, price, sqft (listings endpoint).
    """
    _UNIT_SIGNAL_KEYS = {
        "sqft",
        "bedrooms",
        "price",
        "rent",
        "listing_type",
        "square_feet",
        "asking_rent",
        "beds",
        "bathrooms",
        "bed",
        "bath",
        "sq_ft",
        "rent_from",
    }

    def _has_signals(items: list[dict[str, Any]]) -> bool:
        if not items or not isinstance(items[0], dict):
            return False
        return len(_UNIT_SIGNAL_KEYS & set(items[0].keys())) >= 2

    if isinstance(body, dict):
        objects = body.get("objects") or body.get("results") or body.get("listings")
        if isinstance(objects, list):
            return _has_signals(objects)
    if isinstance(body, list):
        return _has_signals(body)
    return False


class AppFolioAdapter:
    """AppFolio PMS adapter."""

    pms_name: str = "appfolio"
    _fingerprints: list[str] = ["appfolio.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from AppFolio API or fall back to SSR DOM parse.

        Path:
          1. Try captured API responses (community_info etc.); succeeds on
             tenants where the API tier serves unit data.
          2. F11 fallback: when the API path returns 0 units, parse the SSR
             /listings HTML using js-listing-* selectors. Production
             verification: this path recovers ~449 units across the
             becovic/pillarrei/blackrealtymanagement/plentyofplaces cohort
             that today fails as BOT_BLOCKED.
          3. Detect offboarded tenants (302 → page-not-found-sub) and emit
             TENANT_OFFBOARDED so the run report distinguishes "tenant gone"
             from a real fetch failure.
        """
        result = AdapterResult(tier_used="TIER_1_API_APPFOLIO")
        all_units: list[dict[str, str]] = []

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        for resp in api_responses:
            body = resp.get("body")
            if _is_appfolio_response(body):
                url = resp.get("url", "")
                items: list[dict[str, Any]] = []
                if isinstance(body, dict):
                    items = body.get("objects") or body.get("results") or body.get("listings") or []
                elif isinstance(body, list):
                    items = body
                units = parse_appfolio_listings(items, url)
                if units:
                    all_units.extend(units)
                    result.api_responses.append(resp)

        if all_units:
            # Stage 1 validity gate — drops dim-less rows.
            from ma_poc.extraction.post_process import post_process

            _pp_parsed = len(all_units)
            _pp = post_process(all_units, property_id=getattr(ctx, "property_id", None))
            if _pp.n_admitted > 0:
                result.units = _pp.admitted
                result.plan_summaries = _pp.plan_summaries
                result.winning_url = (
                    result.api_responses[0].get("url") if result.api_responses else None
                )
                result.confidence = min(0.95, 0.7 + 0.05 * _pp.n_admitted)
                return result
            result.errors.append(
                f"APPFOLIO_VALIDITY_REJECTED: {_pp_parsed} parsed rows "
                f"failed unit_validity (no numeric dimension)"
            )

        # F11 SSR fallback. Pull HTML from fetch_result first (Jugnu's
        # cached body avoids re-fetching), falling back to live page.
        page_html: str | None = None
        fetch_result = getattr(ctx, "fetch_result", None)
        if fetch_result is not None:
            body = getattr(fetch_result, "body", None)
            if isinstance(body, bytes):
                try:
                    page_html = body.decode("utf-8", errors="replace")
                except Exception:
                    page_html = None
            elif isinstance(body, str):
                page_html = body
        if page_html is None and page is not None:
            try:
                page_html = await page.content()
            except Exception:
                page_html = None

        if page_html and _OFFBOARDED_RE.search(page_html):
            try:
                from ma_poc.observability.events import EventKind, emit
                emit(
                    EventKind.TENANT_OFFBOARDED,
                    getattr(ctx, "property_id", "unknown"),
                    base_url=getattr(ctx, "base_url", ""),
                )
            except (ImportError, AttributeError):
                pass
            result.confidence = 0.0
            result.errors.append("AppFolio tenant offboarded (page-not-found-sub)")
            result.tier_used = "TIER_1_APPFOLIO_TENANT_OFFBOARDED"
            return result

        if page_html and "data-listing-id=" in page_html:
            # 2026-07-28 (defect ``ssr-scope-and-filter``): this branch used
            # to parse every card in the body and return, with no scope of
            # any kind — no propertyGroup lookup, no
            # ``filters[property_list]``, no address filter — even though
            # ``ctx.address``/``ctx.zip_code`` are populated here and
            # ``find_appfolio_property_group`` / the address filter live in
            # this same module. Measured on run 0d54ca7 (100/100 shards):
            # 93.6% of TIER_1_DOM_APPFOLIO_SSR's address-bearing rows sat at
            # a ZIP other than the property's.
            #
            # Ladder, mirroring the vanity path: prefer the SERVER-side
            # scope, fall back to the post-hoc address filter, and grade the
            # filter's strictness on whether the body is an account-wide
            # roster (``PUBLISHED_INDEX`` — the operator published the index,
            # but it is the whole account) or a surface the operator scoped
            # themselves (``OPERATOR_SCOPED``).
            ssr_source_url = ""
            _ssr_fr = getattr(ctx, "fetch_result", None)
            if _ssr_fr is not None:
                ssr_source_url = getattr(_ssr_fr, "final_url", "") or ""
            if not ssr_source_url:
                ssr_source_url = getattr(ctx, "base_url", "") or ""

            ssr_account_index = is_account_index_url(ssr_source_url)
            ssr_html = page_html
            # Unchanged for every non-account-index body: the parser still
            # receives ``ctx.base_url`` as the row's ``source_api_url``.
            ssr_parse_url = getattr(ctx, "base_url", "") or ""

            if ssr_account_index:
                ssr_group = find_appfolio_property_group(page_html)
                if ssr_group:
                    scoped_url = scoped_listings_url(ssr_source_url, ssr_group)
                    scoped_html = await _fetch_appfolio_listings_html(
                        scoped_url, result
                    )
                    # A scoped refetch that comes back empty is NOT evidence
                    # of absence (a stale group name matches no server-side
                    # list). Fall through to the address filter on the body
                    # we already have.
                    if scoped_html and "data-listing-id=" in scoped_html:
                        ssr_html = scoped_html
                        ssr_parse_url = scoped_url
                        ssr_account_index = False
                        result.errors.append(
                            "appfolio-ssr-server-scope: "
                            f"property_group={ssr_group!r} url={scoped_url}"
                        )

            ssr_units = parse_appfolio_listings_ssr(ssr_html, ssr_parse_url)
            if ssr_units:
                ssr_ctx_address = getattr(ctx, "address", "") or ""
                ssr_ctx_zip = getattr(ctx, "zip_code", "") or ""
                ssr_evidence = (
                    ScopeEvidence.PUBLISHED_INDEX
                    if ssr_account_index
                    else ScopeEvidence.OPERATOR_SCOPED
                )
                ssr_units, ssr_tel = filter_listings_by_property_address(
                    ssr_units,
                    ssr_ctx_address,
                    ssr_ctx_zip,
                    evidence=ssr_evidence,
                )
                if ssr_tel.get("filter_activated"):
                    result.errors.append(
                        "appfolio-ssr-address-filter: "
                        f"evidence={ssr_evidence.value} "
                        f"reason={ssr_tel['reason']} "
                        f"kept={ssr_tel['kept']} dropped={ssr_tel['dropped']} "
                        f"ctx_addr={ssr_ctx_address!r} "
                        f"ctx_zip={ssr_ctx_zip!r}"
                    )
            if ssr_units:
                result.units = ssr_units
                result.tier_used = "TIER_1_DOM_APPFOLIO_SSR"
                result.winning_url = (
                    ssr_parse_url or getattr(ctx, "base_url", "") or None
                )
                result.confidence = min(0.95, 0.7 + 0.05 * len(ssr_units))
                return result

        # Bug 7 (2026-05-09 deep-dive): /listings/detail/<uuid> single-listing
        # pages don't carry data-listing-id and aren't an API envelope, but
        # the unit data is in the HTML. Parse it directly when the URL shape
        # signals a detail page. Confidence stays at 0.85 (single record →
        # we can't cross-check) so downstream gates still treat it as a
        # provisional Tier-1 result.
        if page_html:
            current_url = ""
            ff = getattr(ctx, "fetch_result", None)
            if ff is not None:
                current_url = getattr(ff, "final_url", "") or ""
            if not current_url:
                current_url = getattr(ctx, "base_url", "") or ""
            if "/listings/detail/" in current_url:
                detail_units = parse_appfolio_detail_page(page_html, current_url)
                if detail_units:
                    result.units = detail_units
                    result.tier_used = "TIER_1_DOM_APPFOLIO_DETAIL"
                    result.winning_url = current_url or None
                    result.confidence = 0.85
                    return result

        # Vanity-domain fallback: the page is a marketing site that links to
        # ``<slug>.appfolio.com``. Discover the slug, fetch the SSR listings
        # page directly, and parse it with the existing SSR parser.
        # Source: 2026-04-30 failure-recovery (33/46 demonstrated).
        #
        # 2026-05-25 (canary 1ef1060 regr#11 CRITICAL): also extract
        # ``propertyGroup`` from the embed JS and append the
        # ``filters[property_list]={propertyGroup}`` query parameter to
        # the listings URL. Without this filter, multi-property PMC
        # accounts cross-contaminate. See ``find_appfolio_property_group``.
        if page_html:
            slug = find_appfolio_slug(page_html)
            if slug:
                property_group = find_appfolio_property_group(page_html)
                listings_url = f"https://{slug}.appfolio.com/listings"
                if property_group:
                    from urllib.parse import quote
                    listings_url = (
                        f"{listings_url}?filters%5Bproperty_list%5D="
                        f"{quote(property_group)}"
                    )
                try:
                    import httpx
                    headers = {
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        "Accept": "text/html,*/*;q=0.8",
                    }
                    async with httpx.AsyncClient(
                        timeout=15.0, follow_redirects=True
                    ) as c:
                        r = await c.get(listings_url, headers=headers)
                    if r.status_code == 200 and "data-listing-id=" in r.text:
                        vanity_units = parse_appfolio_listings_ssr(r.text, listings_url)
                        # 2026-05-25 (regr#11b): when chip #100's URL-level
                        # propertyGroup filter could not fire (PMC embed JS
                        # has no ``propertyGroup``), the vanity response
                        # still leaks the entire PMC. Drop listings whose
                        # address + ZIP don't match the target property.
                        # 2026-07-18: always scope, even when propertyGroup is
                        # set — the URL-level propertyGroup filter proved
                        # unreliable (94 props leaked the whole PMC despite it).
                        if vanity_units:
                            ctx_address = getattr(ctx, "address", "") or ""
                            ctx_zip = getattr(ctx, "zip_code", "") or ""
                            vanity_units, addr_filter_tel = (
                                filter_listings_by_property_address(
                                    vanity_units, ctx_address, ctx_zip
                                )
                            )
                            if addr_filter_tel.get("filter_activated"):
                                result.errors.append(
                                    "appfolio-vanity-address-filter: "
                                    f"reason={addr_filter_tel['reason']} "
                                    f"kept={addr_filter_tel['kept']} "
                                    f"dropped={addr_filter_tel['dropped']} "
                                    f"ctx_addr={ctx_address!r} "
                                    f"ctx_zip={ctx_zip!r}"
                                )
                        if vanity_units:
                            result.units = vanity_units
                            result.tier_used = "TIER_1_DOM_APPFOLIO_VANITY"
                            result.winning_url = listings_url
                            result.confidence = min(0.90, 0.65 + 0.05 * len(vanity_units))
                            return result
                except Exception as exc:
                    result.errors.append(
                        f"appfolio-vanity-fetch-error: slug={slug!r} "
                        f"property_group={property_group!r} {type(exc).__name__}"
                    )

        # AppFolio Websites CMS (Duda-hosted) fallback (deep-probe 2026-05-25,
        # chip #2). ~10% of sampled CSV rows publish their AppFolio listings
        # through the ``AppFolio Websites`` product — a Duda marketing CMS
        # that mirrors the listings into a Duda collection. The widget
        # renders client-side from Duda's public collections REST API, so
        # the /listings + SSR / slug-vanity paths above return 0. The
        # collection endpoint serves the SAME AppFolio fields
        # (market_rent, bedrooms, bathrooms, square_feet, available,
        # available_date, full_address, listable_uid, property_lists).
        # Verified live on livescs (256 listings), parkviewspringhill
        # (37), beaumontcove (56), pearlinvestment/wind-chase (17),
        # liveatthebiltmore (4), mall-apartments (3). See
        # ``_appfolio_websites_duda`` for the helpers.
        if page_html and is_appfolio_websites_cms(page_html):
            duda_site_id = extract_duda_site_id(page_html)
            duda_origin = origin_from_url(
                getattr(ctx, "base_url", "")
                or (getattr(getattr(ctx, "fetch_result", None), "final_url", "") or "")
            )
            if duda_site_id and duda_origin:
                duda_property_group = extract_appfolio_websites_property_group(
                    page_html
                )
                try:
                    import httpx
                    duda_headers = {
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        "Accept": "application/json,*/*;q=0.8",
                    }
                    duda_units: list[dict[str, Any]] = []
                    duda_winning_url: str | None = None
                    async with httpx.AsyncClient(
                        timeout=15.0, follow_redirects=True
                    ) as c:
                        page_number = 0
                        total_pages = 1
                        # Cap iterations defensively — the largest cohort
                        # site we sampled has 3 pages; ten is plenty of
                        # headroom and bounds the worst-case fetch fan-out.
                        while page_number < total_pages and page_number < 10:
                            duda_url = collection_url(
                                duda_origin, duda_site_id, page_number
                            )
                            r = await c.get(duda_url, headers=duda_headers)
                            if r.status_code != 200:
                                break
                            try:
                                payload = r.json()
                            except (json.JSONDecodeError, ValueError):
                                break
                            units, page_total = parse_collection_payload(
                                payload, duda_url, duda_property_group
                            )
                            duda_units.extend(units)
                            if duda_winning_url is None:
                                duda_winning_url = duda_url
                            if page_total <= 0:
                                break
                            total_pages = page_total
                            page_number += 1
                    # 2026-05-26 (canary 87b837b QC follow-up): PMC-wide
                    # contamination on the DUDA path. axiomproperties.com
                    # returned 455 units across 106 distinct fp_names
                    # (= 106 distinct street addresses); gbatx.com 440
                    # units / 266 addresses. The Duda collection API
                    # serves the FULL PMC inventory unless the property-
                    # group query parameter is set, and on these two PMC
                    # sites the embed JS lacks ``propertyGroup`` so the
                    # collection comes back un-scoped.
                    #
                    # Same fix as the VANITY path (chip #107): drop
                    # units whose address + ZIP don't match the CSV
                    # property context. DUDA stores the address in
                    # ``full_address`` which is mapped to
                    # ``floor_plan_name`` by ``parse_collection_payload``
                    # — the filter's default ``address_field`` matches.
                    # 2026-07-18: always scope (see the vanity path) — the
                    # propertyGroup URL-filter is not reliable enough to skip it.
                    if duda_units:
                        ctx_address = getattr(ctx, "address", "") or ""
                        ctx_zip = getattr(ctx, "zip_code", "") or ""
                        duda_units, addr_filter_tel = (
                            filter_listings_by_property_address(
                                duda_units, ctx_address, ctx_zip
                            )
                        )
                        if addr_filter_tel.get("filter_activated"):
                            result.errors.append(
                                "appfolio-duda-address-filter: "
                                f"reason={addr_filter_tel['reason']} "
                                f"kept={addr_filter_tel['kept']} "
                                f"dropped={addr_filter_tel['dropped']} "
                                f"ctx_addr={ctx_address!r} "
                                f"ctx_zip={ctx_zip!r}"
                            )
                    if duda_units:
                        # Resolve scattered-site id collisions across ALL
                        # paginated pages (a no-suffix address split across
                        # pages must still disambiguate). Only units the Duda
                        # helper anchored to their full_address are touched.
                        duda_addr_units = [
                            u
                            for u in duda_units
                            if (u.get("source_ids") or {}).get(
                                "appfolio_full_address"
                            )
                            and isinstance(u.get("unit_id"), str)
                            and u["unit_id"]
                            == address_unit_id(
                                u["source_ids"]["appfolio_full_address"]
                            )
                        ]
                        resolve_scattered_site_ids(duda_addr_units)
                        result.units = duda_units
                        result.tier_used = "TIER_1_API_APPFOLIO_DUDA"
                        result.winning_url = duda_winning_url
                        result.confidence = min(
                            0.95, 0.7 + 0.05 * len(duda_units)
                        )
                        return result
                except Exception as exc:
                    result.errors.append(
                        f"appfolio-websites-duda-error: site_id={duda_site_id!r} "
                        f"property_group={duda_property_group!r} {type(exc).__name__}"
                    )

        result.confidence = 0.0
        result.errors.append("No AppFolio unit data found in captured API responses or SSR DOM")
        # 2026-07-18: mark the no-units exit as an EMPTY-EXIT so the Path-B
        # retry (scraper.py::_retry_trigger_reason) re-dispatches on the SAME
        # page and lets generic DOM/plan extraction read the marketing site's
        # own "Floor Plans" section (plan-level rents). This is the recovery
        # for props whose AppFolio listings widget returned an un-scopeable
        # whole-PMC dump that the address filter correctly demoted (as well as
        # any genuinely-empty AppFolio page). Bare TIER_1_API_APPFOLIO is a
        # SUCCESS label (not retryable); the ``_EMPTY`` suffix flips
        # is_empty_exit() to True.
        if not result.units:
            result.tier_used = "TIER_1_API_APPFOLIO_EMPTY"
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
