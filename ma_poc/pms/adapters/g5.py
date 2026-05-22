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
    # 2026-05-21 (G5 port follow-up): ``floorplanSpecials`` schema is now
    # an object (with ``id``/``name`` fields) — previously scalar. Sending
    # it as scalar returns ``Field must have selections (field
    # 'floorplanSpecials' returns FloorplanSpecials but has no selections)``.
    # Live-introspected the type to confirm shape; specify ``{id name}``
    # subfields so the query parses on the current schema while remaining
    # forward-compatible.
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


# 2026-05-22: Cloudinary asset-folder regex. G5 hosts every site's images
# under ``/g5/g5-c-<companyId>/g5-cl-<propertyUrn>/uploads/...`` — the
# property's favicon, og:image, and apple-touch-icon all point at THIS
# property's folder, never a sibling's. Anchoring URN selection on this
# CDN path eliminates the ambiguity that broke ``max(matches, key=len)``
# (live-verified 5/5 on Anson, Central Park, Brook Hollow, Ten68 West,
# Westgate Village 2026-05-22). The bare longest-match heuristic shipped
# sibling URNs for 5/5 of the same sample, including Maryland data for
# a California property.
_G5_CDN_PROPERTY_RE = re.compile(
    r"/g5/g5-c[a-z]?-[a-z0-9]+/(g5-cl[a-z]?-[a-z0-9-]+?)/uploads/",
    re.IGNORECASE,
)


def find_g5_urn_for_property(html: str, base_url: str = "") -> str | None:
    """Pick the URN belonging to THIS property — never a sibling.

    Three-step deterministic ranking:

    1. **Cloudinary CDN anchor** (primary). Match
       ``/g5/g5-c-<companyId>/g5-cl-<propertyUrn>/uploads/``. G5's CMS
       routes every asset upload to the tenant's company+property folder,
       so the favicon / og:image / apple-touch-icon URLs all reference
       the correct URN. Sibling property URNs that appear elsewhere on
       the page (switcher menus, parent-company hub links) live under
       their own ``g5-c-...`` folder and therefore never collide.
    2. **CDN-path frequency tie-break** when step 1 matches multiple
       URNs (rare; happens when an og:image and a favicon point at
       different image variants of the same property — both still belong
       to the property under inspection).
    3. **Most-frequent g5-cl-* anywhere on the page** (fallback). When
       step 1 misses entirely (theoretical: a G5 page with no
       Cloudinary-served favicon), the URN appearing most often wins.
       Sibling URNs on a switcher menu appear exactly once; the
       property's own URN appears 50-150+ times in the live samples.

    ``max(matches, key=len)`` is explicitly **NOT** used — it picks
    parent-company switcher URNs ("g5-cl-...-{property}-client-marketing")
    or ``g5-clw-`` property-scoped variants which return HTTP 404 from
    the GraphQL inventory endpoint. Verified 0/5 correct on live samples.

    ``base_url`` is accepted for forward compatibility (e.g. a future
    fallback that matches the property's URL slug against the URN body)
    but is not currently used.
    """
    del base_url  # reserved for future slug-match fallback
    if not html or "g5-cl" not in html.lower():
        return None
    # Step 1+2 — Cloudinary CDN paths. Frequency-rank in case the page
    # has multiple property-folder references.
    cdn_hits = [m.group(1).lower() for m in _G5_CDN_PROPERTY_RE.finditer(html)]
    if cdn_hits:
        from collections import Counter
        return Counter(cdn_hits).most_common(1)[0][0]
    # Step 3 — fallback: most-frequent g5-cl-* on the page.
    all_urns = [m.group(0).lower() for m in _G5_URN_RE.finditer(html)]
    if not all_urns:
        return None
    from collections import Counter
    return Counter(all_urns).most_common(1)[0][0]


# Backward-compat alias — existing tests call ``find_g5_urn``.
# Routes to the new deterministic implementation.
def find_g5_urn(html: str) -> str | None:
    """Deprecated alias for :func:`find_g5_urn_for_property`. Retained so
    older tests keep importing the old name; new callers should use the
    new function (which accepts an optional ``base_url`` for the future
    slug-match fallback).
    """
    return find_g5_urn_for_property(html, "")


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


def parse_g5_apartments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert G5's ``apartmentComplex.apartments`` + ``floorplans`` lists.

    Emits both unit-level rows (one per apartment with a valid rent) and
    plan-only rows for floorplans that have no apartment representation
    (Phase 2b — 2026-05-22). Plan-only rows carry ``unit_number=""`` so
    ``post_process.classify()`` routes them to ``plan_summaries``;
    floorplans already represented by an emitted apartment are skipped
    to preserve the no-dup invariant.
    """
    ac = (payload.get("data") or {}).get("apartmentComplex") or {}
    apts = ac.get("apartments") or []
    floorplans = ac.get("floorplans") or []
    # Map floorplanId -> concession text + floorplan dict (HAR-confirmed schema).
    _fp_spec: dict[Any, str] = {}
    _fp_by_id: dict[Any, dict[str, Any]] = {}
    for _fp in floorplans:
        if isinstance(_fp, dict):
            fp_id = _fp.get("id")
            if fp_id is not None:
                _fp_by_id[fp_id] = _fp
            _s = _g5_specials_to_str(_fp.get("floorplanSpecials"))
            if _s:
                _fp_spec[fp_id] = _s

    out: list[dict[str, Any]] = []
    covered_fp_ids: set[Any] = set()

    for a in apts:
        if not isinstance(a, dict):
            continue
        rent = _price_to_int(a.get("prices"))
        if not rent or not (200 <= rent <= 50_000):
            continue
        fp = a.get("floorplan") or {}
        beds = fp.get("beds")
        baths = fp.get("baths")
        sqft = _sqft_to_int(fp.get("sqft")) or _sqft_to_int(a.get("sqftDisplay"))
        unit_number = a.get("name") or a.get("displayName") or ""
        avail = a.get("availabilityDate") or ""
        out.append(
            {
                "unit_number": str(unit_number),
                "floor_plan_name": str(fp.get("name") or ""),
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
                "extraction_tier": _TIER_BASE,
            }
        )
        if fp.get("id") is not None:
            covered_fp_ids.add(fp.get("id"))

    # 2026-05-22 Phase 2b — plan_summary emission for floorplans without
    # apartments. The G5 marketing site renders every floorplan card
    # regardless of live inventory; surface them as plan-level rows so
    # ``floor_plans[]`` reflects the full plan catalog.
    for fp_id, fp in _fp_by_id.items():
        if fp_id in covered_fp_ids:
            continue  # plan already covered by an apartment row
        name = fp.get("name") or ""
        beds = fp.get("beds")
        baths = fp.get("baths")
        sqft = _sqft_to_int(fp.get("sqft"))
        # G5 floorplan objects carry plan-level rent on minPrice/maxPrice
        # OR startingRate/endingRate (Apollo-shape variant). Defensive
        # against both.
        rent_lo = (
            _price_to_int(fp.get("minPrice"))
            or _price_to_int(fp.get("startingRate"))
        )
        rent_hi = (
            _price_to_int(fp.get("maxPrice"))
            or _price_to_int(fp.get("endingRate"))
            or rent_lo
        )
        # Bound check identical to the unit path.
        if rent_lo is not None and not (200 <= rent_lo <= 50_000):
            rent_lo = None
        if rent_hi is not None and not (200 <= rent_hi <= 50_000):
            rent_hi = None

        out.append(
            {
                "unit_number": "",  # → post_process routes to plan_summaries
                "floor_plan_name": str(name),
                "bedrooms": str(beds) if beds is not None else "",
                "bathrooms": str(baths) if baths is not None else "",
                "sqft": str(sqft) if sqft else "",
                "market_rent_low": rent_lo,
                "market_rent_high": rent_hi,
                "rent_range": (
                    f"{rent_lo}-{rent_hi}" if rent_lo and rent_hi and rent_lo != rent_hi
                    else (str(rent_lo) if rent_lo else "")
                ),
                "availability_status": "UNKNOWN",
                "availability_date": "",
                "building": "",
                "concession": _fp_spec.get(fp_id, ""),
                "extraction_tier": _TIER_BASE,
                "floor_plan_id": str(fp_id),
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
        """Discover URN → fetch units → emit AdapterResult.

        2026-05-22: two structural fixes that together unblock the 0-win
        G5 cohort:

        1. **Rendered HTML access**. Previously only ``ctx.fetch_result.body``
           was read. The G5 detector signals (g5-cl-* tokens) often live
           in JS-injected DOM that's absent from the SSR snapshot. Pull
           ``page.content()`` first (via the same helper GenericAdapter
           uses) and fall back to fetch_result.body if Playwright is
           unavailable. Mirror of the pattern at
           ``ma_poc/pms/adapters/generic.py:_get_page_html``.
        2. **Deterministic URN selection** (see ``find_g5_urn_for_property``).
        """
        from ma_poc.pms.adapters._adapter_telemetry import (
            log_adapter_diag,
            log_adapter_stage,
        )

        pid = str(getattr(ctx, "property_id", "") or "unknown")
        result = AdapterResult(tier_used=_TIER_BASE)

        # Pull rendered HTML when available (fixes G5 root cause #2 —
        # static body often lacks the URN, but the rendered DOM has it).
        html = ""
        try:
            from ma_poc.pms.adapters.generic import _get_page_html

            html = await _get_page_html(page, ctx)
        except Exception:
            html = ""
        # Fallback to fetch_result.body when the page lifecycle is gone
        # (test harnesses, fetch-only mode).
        if not html:
            fr = getattr(ctx, "fetch_result", None)
            body = getattr(fr, "body", None) if fr is not None else None
            if isinstance(body, bytes):
                try:
                    html = body.decode("utf-8", errors="replace")
                except Exception:
                    html = ""
            elif isinstance(body, str):
                html = body

        base_url = str(getattr(ctx, "base_url", "") or "")
        # Normalise html to "" so per-stage telemetry below never trips
        # on None (some test harnesses pass an empty/None body).
        html = html or ""
        urn = find_g5_urn_for_property(html, base_url) if html else None

        # Per-stage telemetry: URN selection outcome.
        cdn_hits = len(list(_G5_CDN_PROPERTY_RE.finditer(html)))
        urn_total = len(list(_G5_URN_RE.finditer(html)))
        urn_distinct = len({m.group(0).lower() for m in _G5_URN_RE.finditer(html)})
        log_adapter_stage(
            "g5",
            pid,
            "urn_pick",
            "ok" if urn else "no_urn_found",
            reason=f"urn={urn!r} body_len={len(html)} cdn_hits={cdn_hits} total={urn_total} distinct={urn_distinct}",
            urn_picked=urn,
            urn_cdn_anchored=cdn_hits > 0,
            urn_total=urn_total,
            urn_distinct=urn_distinct,
        )

        if not urn:
            # When the page contains g5-cl-* tokens but ranking returned
            # None (shouldn't happen with the new algorithm — but emit a
            # diag event anyway in case of unusual page shapes).
            if "g5-cl" in (html or "").lower():
                log_adapter_diag(
                    "g5",
                    pid,
                    "urn_pick",
                    html,
                    reason="g5-cl tokens present but no URN picked",
                )
            result.tier_used = _TIER_NO_URN
            result.errors.append("g5-adapter: no g5-cl-... URN in rendered HTML")
            return result

        # GraphQL fetch — telemetry on both error + empty paths.
        try:
            payload = await _fetch_g5_units(urn, base_url=base_url)
        except Exception as exc:
            log_adapter_stage(
                "g5",
                pid,
                "graphql_fetch",
                f"exception:{type(exc).__name__}",
                reason=f"urn={urn!r} {str(exc)[:120]}",
            )
            if await self._try_apollo(page, ctx, result):
                log_adapter_stage("g5", pid, "apollo_fallback", "ok", units=len(result.units))
                return result
            result.tier_used = _TIER_API_ERROR
            result.errors.append(
                f"g5-api-error: urn={urn!r} {type(exc).__name__}: {str(exc)[:120]}"
            )
            return result

        if not payload:
            log_adapter_stage(
                "g5",
                pid,
                "graphql_fetch",
                "empty_response",
                reason=f"urn={urn!r} (api 200 but empty data)",
            )
            if await self._try_apollo(page, ctx, result):
                log_adapter_stage("g5", pid, "apollo_fallback", "ok", units=len(result.units))
                return result
            result.tier_used = _TIER_API_ERROR
            result.errors.append(f"g5-api: empty response for urn={urn!r}")
            return result

        units = parse_g5_apartments(payload)
        log_adapter_stage(
            "g5",
            pid,
            "graphql_fetch",
            "ok" if units else "parse_returned_empty",
            units=len(units),
            reason=f"urn={urn!r}",
        )
        if not units:
            if await self._try_apollo(page, ctx, result):
                log_adapter_stage("g5", pid, "apollo_fallback", "ok", units=len(result.units))
                return result
            result.tier_used = _TIER_EMPTY
            result.errors.append(
                f"g5-api: returned 0 parseable units for urn={urn!r} "
                "(property may have no live inventory)"
            )
            return result

        result.units = units
        result.winning_url = f"{_G5_ENDPOINT} (urn={urn})"
        result.confidence = min(0.95, 0.7 + 0.02 * len(units))
        log_adapter_stage("g5", pid, "cascade_exit", "won_graphql", units=len(units))
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

    2026-05-21 port: G5's inventory endpoint returns 404 for requests
    missing proper ``Origin``/``Referer`` headers (verified by live probe
    — same payload, different headers, different outcome). Adding the
    property site as Origin/Referer makes the request indistinguishable
    from the browser's in-page POST and the server returns the data shape.
    """
    import httpx

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


def parse_g5_apollo_floorplans(
    fps: list[dict[str, object]], url: str
) -> list[dict[str, str]]:
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


def parse_g5_apollo_units(
    rows: list[dict[str, object]], url: str
) -> list[dict[str, str]]:
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
