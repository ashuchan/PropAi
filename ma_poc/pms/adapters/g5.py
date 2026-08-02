"""G5 Marketing Cloud adapter.

Research log
------------
G5 (https://www.getg5.com/) is the marketing platform powering Morgan
Properties and many other multifamily operators (Aimco, Bell Partners, ZRS,
JMG, BH Companies). The platform exposes a public GraphQL inventory API at
``inventory.g5marketingcloud.com/graphql``. Discovered 2026-05-13 via Chrome
MCP probe of www.morgan-properties.com/apartments/pa/harrisburg/kings-manor-apartments/.

Key findings (2026-05-13)
-------------------------
* Endpoint: ``POST https://inventory.g5marketingcloud.com/graphql``
* No auth required for read queries (introspection enabled).
* Property identified by ``locationUrn`` — the full G5 slug, e.g.
  ``g5-cl-1jsdmzcxpf-king-s-manor-apartments``. Slug is discoverable from
  any ``g5-cl-...`` reference in the property's HTML (image CDN paths
  carry it: ``g5-assets-cld-res.cloudinary.com/.../g5-cl-{slug}/uploads/...``).
* Query path that returns unit-level data:
  ``apartmentComplex(locationUrn:$urn){apartments(perPage:200){...}}``
  — each apartment carries ``name``, ``availabilityDate``, ``prices`` (list
  of priced lease terms), ``sqftDisplay``, plus a joined ``floorplan`` with
  ``beds``/``baths``/``sqft``/``name``.
* Sample: King's Manor returned 21 apartments, every one with
  ``availabilityDate`` populated — a strict improvement over the current
  generic Tier 1 pipeline which loses the date.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


_G5_ENDPOINT = "https://inventory.g5marketingcloud.com/graphql"

# Property URN regex — matches ``g5-cl-<id>[-<slug>]`` and the property-
# level ``g5-cl<x>-<id>[-<slug>]`` variants (e.g. ``g5-clw-...`` is the
# COMPLEX-scoped form used by Villas Willow Glen / similar single-
# property sites; ``g5-cl-`` is the company-scoped form used by Lincoln
# Property Company and other multi-property operators). The image CDN
# paths and inline data attrs carry the slug on every G5-hosted page.
# Verified live 2026-05-20: g5-clw-guhjplm75w-villas-willow-glen-...
# returned 200 from the GraphQL endpoint when passed as ``locationUrn``.
_G5_URN_RE = re.compile(r"g5-cl[a-z]?-[a-z0-9]+(?:-[a-z0-9]+)*", re.IGNORECASE)

# 2026-05-23: G5 dataLayer canonical property URN — the single source of
# truth for which ``locationUrn`` the GraphQL API accepts. Always the
# ``g5-cl-*`` (no ``w``) form. The ``g5-clw-*-{32-hex-suffix}`` URN
# returns 404 from the inventory GraphQL endpoint (validated 21/21 on
# the canary G5_API_ERROR cohort) but was being picked by the longest-
# match heuristic, blocking unit extraction for the whole cohort.
_G5_STORE_ID_RE = re.compile(
    r'"G5_STORE_ID"\s*:\s*"(g5-cl-[a-z0-9-]+)"',
    re.IGNORECASE,
)

# GraphQL query — returns the unit + floor-plan join in one round trip.
# perPage:200 covers any normal property; pagination would need the API's
# total-count hint (not currently used).
# 2026-05-19: concession fields added — HAR-confirmed valid on
# ``apartmentComplex`` (morgan-properties browser query returned 200
# with these exact fields). G5 specials are property/floorplan-level:
# ``hasApartmentSpecials``/``hasFloorplanSpecials`` (bool) +
# ``floorplans[].floorplanSpecials`` (array, joined by floorplan id).
# No auth token (scalable). NOTE: value-shape of floorplanSpecials is
# INFERRED (the captured HAR property had no active special, [] /
# false) — parsed defensively; revalidate when a populated G5 special
# is observed.
_G5_UNITS_QUERY = (
    # 2026-05-20: ``floorplanSpecials`` schema is now an object (with
    # ``id``/``name`` fields) — previously scalar. Sending it as scalar
    # returns ``Field must have selections (field 'floorplanSpecials'
    # returns FloorplanSpecials but has no selections)``. Live-introspected
    # the type to confirm shape; specify ``{id name}`` subfields so the
    # query parses on the current schema while remaining forward-compatible.
    "query($urn:String!){apartmentComplex(locationUrn:$urn){"
    "id name hasApartmentSpecials hasFloorplanSpecials "
    "floorplans{id name floorplanSpecials{id name}} "
    "apartments(perPage:200){"
    "id name displayName building availabilityDate sqftDisplay "
    "prices{value formattedPrice priceType} "
    "floorplan{id name beds baths sqft sqftDisplay}"
    "}}}"
)

_TIER_BASE = "TIER_1_API_G5"
_TIER_NO_URN = f"{_TIER_BASE}_NO_URN"
_TIER_API_ERROR = f"{_TIER_BASE}_API_ERROR"
_TIER_EMPTY = f"{_TIER_BASE}_EMPTY"


def find_g5_urn(html: str) -> str | None:
    """Return the canonical ``locationUrn`` for the property, or ``None``.

    Selection priority (2026-05-23 fix — see canary G5_API_ERROR 21/21
    failure cohort root cause):
      1. ``G5_STORE_ID`` from the inline ``dataLayer`` script. This is
         the SINGLE source of truth — the GraphQL API only accepts the
         ``g5-cl-<id>-<slug>`` form found here. Always present on every
         G5-hosted page (validated across 5+ Goldoller / Bayshore /
         GK Management / single-property sites).
      2. Longest ``g5-cl-*`` match (NOT ``g5-clw-*``) from the HTML —
         fallback when the dataLayer script is missing or the URN was
         stripped from it.

    The ``g5-clw-*-{32-hex}`` URN is the WEBSITE/wrapper URN, NOT a
    valid GraphQL ``locationUrn``. The old longest-match heuristic
    selected it because the 32-hex hash suffix made it longer than the
    real ``g5-cl-*`` URN — that's the bug. Multi-property portfolios
    (Goldoller, GK Management, Bayshore) also leak SIBLING ``g5-cl-*``
    URNs onto the page (CDN image paths for menu thumbnails), so the
    raw-longest fallback alone isn't safe either — the dataLayer is
    what tells us which sibling THIS page belongs to.
    """
    candidates = find_g5_urn_candidates(html)
    return candidates[0] if candidates else None


def find_g5_urn_candidates(html: str) -> list[str]:
    """Return up-to-3 ranked URN candidates for ``html``, best-first.

    The single-URN ``find_g5_urn`` returns the winner; this returns the
    full priority list so the adapter can retry with sibling URNs when
    the primary returns empty data. Multi-property portfolios
    (Goldoller, GK Management, Bayshore) leak sibling ``g5-cl-*`` URNs
    onto every page (CDN thumbnail paths in the site nav) — when the
    dataLayer-derived URN happens to be wrong (e.g. dataLayer points at
    the portfolio root, but inventory lives on a sibling), a fallback
    URN often works.

    Priority order:
      1. ``G5_STORE_ID`` from dataLayer (canonical when present)
      2. Longest ``g5-cl-*`` match other than the dataLayer one
      3. Longest ``g5-clw-*`` match (rare, mostly noise — last resort)

    All comparisons are lowercase. Empty list when html has no g5 URNs.
    """
    if not html or "g5-cl" not in html.lower():
        return []

    ordered: list[str] = []
    seen: set[str] = set()

    # Priority 1: dataLayer canonical URN.
    m = _G5_STORE_ID_RE.search(html)
    if m:
        winner = m.group(1).lower()
        ordered.append(winner)
        seen.add(winner)

    # Collect ALL g5-cl-* and g5-clw-* matches; bucket separately.
    matches = {mm.group(0).lower() for mm in _G5_URN_RE.finditer(html)}
    g5_cl = sorted(
        (u for u in matches if u.startswith("g5-cl-") and u not in seen),
        key=len,
        reverse=True,
    )
    g5_clw = sorted(
        (u for u in matches if u.startswith("g5-clw-") and u not in seen),
        key=len,
        reverse=True,
    )

    # Priority 2: longest g5-cl-* siblings (the dataLayer-confirmed one
    # is already in ordered; these are candidates for multi-property
    # portfolios where dataLayer points at a different sibling).
    for u in g5_cl[:2]:
        if u not in seen:
            ordered.append(u)
            seen.add(u)

    # Priority 3: g5-clw-* as last resort. These almost always 404 the
    # inventory API but a single match is sometimes the only URN on the
    # page — better than nothing.
    if not ordered and g5_clw:
        ordered.append(g5_clw[0])

    return ordered[:3]


def _price_to_int(prices: Any) -> int | None:
    """Pull the first sensible price out of G5's ``prices: [...]`` list.

    G5 lists multiple ``priceType`` entries (``min_rent``, ``rate``,
    lease-term-specific). Prefer ``min_rent`` then ``rate`` then any.
    """
    if not isinstance(prices, list):
        return None
    by_type: dict[str, float | None] = {}
    fallback: float | None = None
    for p in prices:
        if not isinstance(p, dict):
            continue
        val = p.get("value")
        try:
            n = float(val) if val is not None else None
        except (TypeError, ValueError):
            n = None
        if n is None:
            continue
        pt = (p.get("priceType") or "").lower()
        if pt and pt not in by_type:
            by_type[pt] = n
        if fallback is None:
            fallback = n
    for key in ("min_rent", "rate", "market_rent"):
        if by_type.get(key) is not None:
            return int(by_type[key])  # type: ignore[arg-type]
    return int(fallback) if fallback is not None else None


def _sqft_to_int(val: Any) -> int | None:
    """G5 sqft can be int or display string like '950'."""
    if val is None:
        return None
    if isinstance(val, int):
        return val if 50 < val < 20_000 else None
    if isinstance(val, str):
        m = re.search(r"(\d{2,5})", val)
        if m:
            n = int(m.group(1))
            return n if 50 < n < 20_000 else None
    return None


def _g5_specials_to_str(val: Any) -> str:
    """Defensive: G5 floorplanSpecials value-shape is inferred (no
    populated HAR example). Coerce list[str] / list[dict] / str / dict
    → a single raw string, capture-first. '' when absent."""
    if not val:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        for k in ("description", "title", "text", "name", "value"):
            if isinstance(val.get(k), str) and val[k].strip():
                return val[k].strip()
        return ""
    if isinstance(val, list):
        parts: list[str] = []
        for it in val:
            s = _g5_specials_to_str(it)
            if s:
                parts.append(s)
        return " | ".join(parts)
    return ""


_G5_DIMENSION_PAIR_RE = re.compile(r"(?<!\d)([1-9]\d?)\s*[xX]\s*([1-9]\d?(?:\.\d+)?)(?!\d)")
_G5_BED_RE = re.compile(r"(?<!\d)([1-9]\d?)\s*bed(?:room)?s?\b", re.IGNORECASE)
_G5_BATH_RE = re.compile(r"(?<!\d)([1-9]\d?(?:\.\d+)?)\s*bath(?:room)?s?\b", re.IGNORECASE)


def _is_numeric_zero(value: Any) -> bool:
    try:
        return float(value) == 0
    except (TypeError, ValueError):
        return False


def _g5_explicit_dimensions(*labels: Any) -> tuple[int | None, float | None, str]:
    """Infer only explicit non-studio dimensions from G5 names/codes."""

    texts = [str(label).strip() for label in labels if str(label or "").strip()]
    for text in texts:
        pair = _G5_DIMENSION_PAIR_RE.search(text)
        if pair:
            return int(pair.group(1)), float(pair.group(2)), text

    beds: int | None = None
    baths: float | None = None
    evidence = ""
    for text in texts:
        # Zero-bedroom is normally a valid studio. Never override it from a
        # studio/S-code heuristic; only explicit positive words or NxM win.
        if "studio" in text.casefold():
            continue
        bed_match = _G5_BED_RE.search(text)
        bath_match = _G5_BATH_RE.search(text)
        if beds is None and bed_match:
            beds = int(bed_match.group(1))
            evidence = evidence or text
        if baths is None and bath_match:
            baths = float(bath_match.group(1))
            evidence = evidence or text
    return beds, baths, evidence


def _repair_g5_dimension_contradiction(
    beds: Any,
    baths: Any,
    *,
    apartment_code: Any,
    floor_plan_name: Any,
) -> tuple[Any, Any, str]:
    """Repair source zeroes only when an explicit positive token disagrees."""

    explicit_beds, explicit_baths, evidence = _g5_explicit_dimensions(
        apartment_code,
        floor_plan_name,
    )
    repaired: list[str] = []
    if _is_numeric_zero(beds) and explicit_beds is not None and explicit_beds > 0:
        beds = explicit_beds
        repaired.append(f"beds:0->{explicit_beds}")
    if _is_numeric_zero(baths) and explicit_baths is not None and explicit_baths > 0:
        baths = explicit_baths
        repaired.append(f"baths:0->{explicit_baths:g}")
    correction = ";".join(repaired)
    if correction and evidence:
        correction = f"{correction};evidence={evidence}"
    return beds, baths, correction


def parse_g5_apartments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert G5's ``apartmentComplex.apartments`` list into unit dicts."""
    ac = (payload.get("data") or {}).get("apartmentComplex") or {}
    apts = ac.get("apartments") or []
    # Map floorplanId -> concession text (HAR-confirmed schema path).
    _fp_spec: dict[Any, str] = {}
    for _fp in ac.get("floorplans") or []:
        if isinstance(_fp, dict):
            _s = _g5_specials_to_str(_fp.get("floorplanSpecials"))
            if _s:
                _fp_spec[_fp.get("id")] = _s
    out: list[dict[str, Any]] = []
    for a in apts:
        if not isinstance(a, dict):
            continue
        rent = _price_to_int(a.get("prices"))
        if not rent or not (200 <= rent <= 50_000):
            continue
        fp = a.get("floorplan") or {}
        beds = fp.get("beds")
        baths = fp.get("baths")
        beds, baths, dimension_correction = _repair_g5_dimension_contradiction(
            beds,
            baths,
            apartment_code=a.get("name"),
            floor_plan_name=fp.get("name"),
        )
        sqft = _sqft_to_int(fp.get("sqft")) or _sqft_to_int(a.get("sqftDisplay"))
        native_id = a.get("id")
        public_label = a.get("displayName") or ""
        plan_name = fp.get("name") or a.get("name") or ""
        avail = a.get("availabilityDate") or ""
        out.append(
            {
                # G5 ``name`` is a repeated apartment/plan type on the
                # affected portfolio. Native ``id`` is the physical anchor;
                # ``displayName`` is what the operator shows to prospects.
                "unit_id": str(native_id) if native_id not in (None, "") else "",
                "unit_number": str(public_label or native_id or ""),
                "unit_name": str(public_label) if public_label else None,
                "floor_plan_name": str(plan_name),
                "bedrooms": str(beds) if beds is not None else "",
                "bathrooms": str(baths) if baths is not None else "",
                "sqft": str(sqft) if sqft else "",
                "market_rent_low": rent,
                "market_rent_high": rent,
                "rent_range": str(rent),
                "availability_status": "AVAILABLE",
                "availability_date": str(avail)[:30],
                "building": str(a.get("building") or ""),
                "concession": _fp_spec.get(fp.get("id"), ""),
                "source_ids": {
                    key: str(value)
                    for key, value in {
                        "g5_apartment_id": native_id,
                        "g5_floor_plan_id": fp.get("id"),
                        "g5_property_id": ac.get("id"),
                    }.items()
                    if value not in (None, "")
                },
                "data_quality_flag": ("G5_EXPLICIT_DIMENSION_CORRECTION" if dimension_correction else None),
                "dimension_correction_provenance": dimension_correction or None,
                "extraction_tier": _TIER_BASE,
            }
        )
    return out


class G5Adapter:
    """Adapter for G5-hosted multifamily marketing sites.

    G5 sites embed unit data via JS calls to ``inventory.g5marketingcloud.com``
    that PropAi's network capture often misses (the call fires after the
    settle window). The adapter discovers the property URN from any
    ``g5-cl-...`` slug in the rendered HTML and queries the GraphQL API
    directly. No browser required for the API call itself.
    """

    pms_name: str = "g5"
    _fingerprints: list[str] = [
        "inventory.g5marketingcloud.com",
        "g5-assets-cld-res.cloudinary.com",
        "g5marketingcloud",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Discover URN → fetch units → emit AdapterResult."""
        result = AdapterResult(tier_used=_TIER_BASE)

        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        html = ""
        if isinstance(body, bytes):
            try:
                html = body.decode("utf-8", errors="replace")
            except Exception:
                html = ""
        elif isinstance(body, str):
            html = body

        # 2026-05-24: try ALL ranked URN candidates, not just the
        # winner. Multi-property portfolios leak sibling URNs into
        # every page; the dataLayer's pick is canonical for most sites
        # but occasionally targets the wrong sibling. Trying up-to-3
        # candidates costs at most 3 GraphQL POSTs (still <1s total
        # against G5's fast endpoint).
        candidates = find_g5_urn_candidates(html) if html else []
        if not candidates:
            result.tier_used = _TIER_NO_URN
            result.errors.append("g5-adapter: no g5-cl-... URN in rendered HTML")
            return result

        base_url = str(getattr(ctx, "base_url", "") or "")
        last_error: str = ""
        winning_urn: str = ""
        winning_payload: dict[str, Any] | None = None

        for cand_urn in candidates:
            try:
                payload = await _fetch_g5_units(cand_urn, base_url=base_url)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:80]}"
                continue
            if not payload:
                last_error = "empty response"
                continue
            # Successful network call. Does the URN actually resolve to
            # a complex with apartments? Empty apartments == wrong URN
            # → try the next candidate.
            ac = (payload.get("data") or {}).get("apartmentComplex") or {}
            apts = ac.get("apartments") or []
            if not apts:
                last_error = "apartmentComplex.apartments was empty"
                continue
            # First non-empty payload wins
            winning_urn = cand_urn
            winning_payload = payload
            break

        if winning_payload is None:
            if await self._try_apollo(page, ctx, result):
                return result
            result.tier_used = _TIER_API_ERROR
            result.errors.append(
                f"g5-api-error: tried {len(candidates)} URN candidate(s) "
                f"({candidates!r}); last={last_error or 'unknown'}"
            )
            return result

        units = parse_g5_apartments(winning_payload)
        if not units:
            if await self._try_apollo(page, ctx, result):
                return result
            result.tier_used = _TIER_EMPTY
            result.errors.append(
                f"g5-api: returned 0 parseable units for urn={winning_urn!r} "
                "(property may have no live inventory)"
            )
            return result

        result.units = units
        result.winning_url = f"{_G5_ENDPOINT} (urn={winning_urn})"
        result.confidence = min(0.95, 0.7 + 0.02 * len(units))
        # 2026-05-24: expose the GraphQL response so scraper.py's
        # Step 9b (api_concession_extract on adapter_result.api_responses)
        # can pull floorplanSpecials / hasFloorplanSpecials out of the
        # payload. Without this, the adapter consumes the payload locally
        # and discards it — concession capture misses for every G5 site.
        result.api_responses.append(
            {
                "url": f"{_G5_ENDPOINT}?urn={winning_urn}",
                "status": 200,
                "body": winning_payload,
                "via": "g5_graphql_direct",
            }
        )
        from ma_poc.pms.source_provenance import build_unit_source_provenance

        complex_payload = (winning_payload.get("data") or {}).get("apartmentComplex") or {}
        result.unit_source_provenance.append(
            build_unit_source_provenance(
                provider="g5",
                source_url=f"{_G5_ENDPOINT}?urn={winning_urn}",
                body=winning_payload,
                unit_count=len(units),
                identity={
                    "property_id": str(getattr(ctx, "property_id", "") or ""),
                    "g5_urn": winning_urn,
                    "g5_property_id": str(complex_payload.get("id") or ""),
                    "g5_property_name": str(complex_payload.get("name") or ""),
                },
            )
        )
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    async def _try_apollo(
        self,
        page: Page,
        ctx: AdapterContext,
        result: AdapterResult,
    ) -> bool:
        """2026-05-19 Apollo-cache fallback. G5 is a Vue SPA whose GraphQL
        XHR is frequently not captured (it fires once at boot, before
        interception). The resolved data still lives in
        ``window.__APOLLO_CLIENT__.cache`` — read it directly when the
        primary URN+POST path failed. Unit-level Apartment↔Prices↔Floorplan
        join preferred; falls back to plan-level Floorplan rows.
        """
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            return False
        try:
            cache = await evaluate(_G5_APOLLO_JS)
        except Exception:
            return False
        if not isinstance(cache, dict):
            return False
        win = ""
        try:
            win = page.url or getattr(ctx, "base_url", "") or ""
        except Exception:
            win = getattr(ctx, "base_url", "") or ""
        for parsed in (
            parse_g5_apollo_units(cache.get("units") or [], win),
            parse_g5_apollo_floorplans(cache.get("floorplans") or [], win),
        ):
            if not parsed:
                continue
            from ma_poc.extraction.post_process import post_process

            pp = post_process(parsed, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                result.units = pp.admitted
                result.plan_summaries = pp.plan_summaries
                result.tier_used = "TIER_2_API_G5_APOLLO"
                result.winning_url = win
                result.confidence = min(0.92, 0.7 + 0.05 * pp.n_admitted)
                return True
        return False


async def _fetch_g5_units(urn: str, base_url: str = "") -> dict[str, Any] | None:
    """Hit the G5 GraphQL endpoint with our units query.

    2026-05-20: G5's inventory endpoint returns 404 for requests missing
    proper ``Origin``/``Referer`` headers (verified by live probe — same
    payload, different headers, different outcome). Adding the property
    site as Origin/Referer makes the request indistinguishable from the
    browser's in-page POST and the server returns the data shape.

    2026-05-24: switched from ``httpx`` to ``curl_cffi`` with
    ``impersonate="chrome120"``. Live probe on the 22-prop P1
    TIER_1_API_G5 cohort confirmed the GraphQL query + URN are correct
    (5/5 sampled URNs returned 100+ apartments), yet canary reported
    ``TIER_1_API_G5_API_ERROR`` on 13/22 properties. Root cause was
    ``httpx``'s ALPN/cipher fingerprint occasionally being rejected by
    G5's CDN edge for specific request signatures (intermittent,
    region-dependent). Curl_cffi chrome120 ships a byte-for-byte real
    Chrome TLS handshake — same pattern that fixed the L1 fetcher GET
    path (commit 59b9102).
    """
    origin = ""
    referer = ""
    if base_url:
        try:
            from urllib.parse import urlparse

            p = urlparse(base_url)
            if p.scheme and p.netloc:
                origin = f"{p.scheme}://{p.netloc}"
                referer = base_url
        except Exception:
            pass

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if origin:
        headers["Origin"] = origin
    if referer:
        headers["Referer"] = referer
    payload = {"query": _G5_UNITS_QUERY, "variables": {"urn": urn}}

    # curl_cffi import deferred so the adapter loads even when curl_cffi
    # is absent (test envs); fall back to httpx in that case.
    try:
        # curl_cffi is synchronous — wrap in asyncio.to_thread so the
        # adapter's outer ``async`` flow doesn't block the event loop.
        import asyncio as _asyncio

        from curl_cffi import requests as _cr

        def _do_post() -> tuple[int, str]:
            r = _cr.post(
                _G5_ENDPOINT,
                json=payload,
                headers=headers,
                impersonate="chrome120",
                timeout=15,
            )
            return r.status_code, (r.text or "")

        status, text = await _asyncio.to_thread(_do_post)
        if status != 200:
            return None
        try:
            import json as _json

            return _json.loads(text)
        except Exception:
            return None
    except ImportError:
        # Fallback: plain httpx (kept so the adapter still works in
        # minimal envs without curl_cffi).
        import httpx

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
            r = await c.post(_G5_ENDPOINT, json=payload, headers=headers)
            if r.status_code != 200:
                return None
            try:
                return r.json()
            except Exception:
                return None


# ── Apollo-cache fallback helpers (2026-05-19) ───────────────────────────────
# G5 is a Vue SPA; the GraphQL POST fires once at boot and is frequently NOT
# captured by XHR interception. The resolved inventory still lives in the
# Apollo client cache (`window.__APOLLO_CLIENT__.cache`). Verified live on
# livemarleymanor.com: cache held 7 Floorplan + 20 Apartment + 20 Prices
# objects with clean numeric rents.

_G5_APOLLO_JS = r"""
() => {
  const c = window.__APOLLO_CLIENT__;
  if (!c || !c.cache || typeof c.cache.extract !== 'function') return {floorplans: [], units: []};
  let d;
  try { d = c.cache.extract(); } catch (e) { return {floorplans: [], units: []}; }
  const ents = Object.entries(d);
  const fpById = {};
  const floorplans = [];
  for (const [, o] of ents) {
    if (o && o.__typename === 'Floorplan') {
      fpById[String(o.id)] = {name: o.name || '', beds: o.beds, baths: o.baths, sqft: o.sqft};
      floorplans.push({
        name: o.name || '', beds: o.beds, baths: o.baths, sqft: o.sqft,
        startingRate: o.startingRate, endingRate: o.endingRate,
        available: o.totalAvailableUnits, hasSpecials: !!o.hasSpecials,
      });
    }
  }
  const price = (ref) => {
    const id = ref && (ref.id || ref.__ref);
    return id ? d[id] : null;
  };
  const units = [];
  for (const [k, o] of ents) {
    if (!o || o.__typename !== 'Apartment') continue;
    const m = k.match(/floorplanId"?\s*:\s*"?(\d+)/);
    const fp = m ? fpById[m[1]] : null;
    const vals = (o.prices || [])
      .map(price).filter(Boolean)
      .map((p) => parseFloat(p.value)).filter((v) => !isNaN(v) && v > 0);
    units.push({
      unit: o.name || '', avail: o.availabilityDate || '',
      rentLow: vals.length ? Math.min.apply(null, vals) : null,
      rentHigh: vals.length ? Math.max.apply(null, vals) : null,
      fpName: fp ? fp.name : '', beds: fp ? fp.beds : null,
      baths: fp ? fp.baths : null, sqft: fp ? fp.sqft : null,
    });
  }
  return {floorplans, units};
}
"""


def _to_int(v: object) -> int | None:
    try:
        if v is None or v == "":
            return None
        return int(float(v))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_g5_apollo_floorplans(fps: list[dict[str, object]], url: str) -> list[dict[str, str]]:
    """Plan-level rows from Apollo ``Floorplan`` objects (rates numeric)."""
    units: list[dict[str, str]] = []
    for fp in fps:
        if not isinstance(fp, dict):
            continue
        name = str(fp.get("name") or "").strip()
        beds = _to_int(fp.get("beds"))
        baths_raw = fp.get("baths")
        baths = str(baths_raw).strip() if baths_raw not in (None, "") else ""
        sqft = _to_int(fp.get("sqft"))
        rent_lo = _to_int(fp.get("startingRate"))
        rent_hi = _to_int(fp.get("endingRate"))
        avail = _to_int(fp.get("available"))
        if not name and beds is None and sqft is None:
            continue
        units.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=baths,
                sqft=str(sqft) if sqft is not None else "",
                unit_number="",
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                availability_status="AVAILABLE",
                available_units=str(avail) if avail is not None else "",
                concession="SPECIAL" if fp.get("hasSpecials") else "",
                source_api_url=url,
                extraction_tier="TIER_2_API_G5_APOLLO",
            )
        )
    return units


def parse_g5_apollo_units(rows: list[dict[str, object]], url: str) -> list[dict[str, str]]:
    """Unit-level rows from Apollo Apartment↔Prices↔Floorplan join."""
    units: list[dict[str, str]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        unit_no = str(r.get("unit") or "").strip()
        fp_name = str(r.get("fpName") or "").strip()
        rent_lo = _to_int(r.get("rentLow"))
        rent_hi = _to_int(r.get("rentHigh"))
        if not unit_no and not fp_name and rent_lo is None:
            continue
        beds = _to_int(r.get("beds"))
        baths_raw = r.get("baths")
        baths = str(baths_raw).strip() if baths_raw not in (None, "") else ""
        sqft = _to_int(r.get("sqft"))
        units.append(
            make_unit_dict(
                floor_plan_name=fp_name,
                bed_label=bed_label_from(beds, fp_name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=baths,
                sqft=str(sqft) if sqft is not None else "",
                unit_number=unit_no,
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                availability_status="AVAILABLE",
                available_units="1",
                availability_date=str(r.get("avail") or "").strip(),
                source_api_url=url,
                extraction_tier="TIER_2_API_G5_APOLLO",
            )
        )
    return units


# ─────────────────────────────────────────────────────────────────────────────
# Captured-response defense-in-depth shims for generic.py dispatch.
# generic.py imports these to parse a G5 GraphQL body if one was captured
# during the page load but detection didn't route to G5Adapter. The current
# G5Adapter does its own fetching via _fetch_g5_units; these shims handle
# the captured-response edge case without re-implementing the parser.
# ─────────────────────────────────────────────────────────────────────────────


def is_g5_graphql_url(url: str) -> bool:
    """True iff the URL is the G5 Marketing Cloud GraphQL endpoint."""
    if not url:
        return False
    u = url.lower()
    return "inventory.g5marketingcloud.com" in u and "/graphql" in u


def is_g5_graphql_body(body: Any) -> bool:
    """True iff body looks like a G5 GraphQL response with apartmentComplex
    data. Pure / never raises."""
    if not isinstance(body, dict):
        return False
    data = body.get("data")
    if not isinstance(data, dict):
        return False
    ac = data.get("apartmentComplex")
    return isinstance(ac, dict) and isinstance(ac.get("apartments"), list)


def parse_g5_response(body: Any, url: str) -> list[dict[str, str]]:
    """Parse a captured G5 GraphQL response body into unit dicts.

    Thin wrapper around ``parse_g5_apartments``; ``url`` is accepted for
    parity with the generic.py dispatch signature but unused (the parser
    tags ``extraction_tier`` itself).
    """
    del url  # signature parity only
    try:
        return parse_g5_apartments(body) if isinstance(body, dict) else []
    except Exception:  # defensive — never raise into dispatch
        return []
