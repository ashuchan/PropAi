"""
AMLI Residential adapter.

Research log
------------
- amli.com is a Next.js + tRPC application; floor-plan + unit data is
  served via SSR-bundled JSON at:
    https://www.amli.com/_next/data/{BUILD_ID}/en/apartments/{region}/{subregion}.json
  The submarket-level JSON contains a floor-plan array with full unit
  inventory for every property in that submarket. Each entry carries a
  ``propertyUid`` field — we filter to the target property's slug.
- Property-level _next/data JSONs (one level deeper) contain Prismic CMS
  metadata only (no rent). Inline ``<script id="__NEXT_DATA__">`` on the
  property page sometimes contains the same submarket query (Next.js prefetch),
  giving a free fast-path before we issue a secondary fetch.
- BUILD_ID rotates on every AMLI deploy. Always extract from the live
  HTML's ``__NEXT_DATA__.buildId``; never cache it.

Why a dedicated adapter
-----------------------
Without this adapter, AMLI properties failed every deterministic tier
and burned the full LLM cascade — 5–15 minutes/property, 0 units
extracted, ~$0.04/property in OpenRouter cost. Shard 17 on 2026-05-05
hung for hours because every AMLI property in its slice landed on the
unlucky link-hop ranker path. This adapter is the fix.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


_TIER_INLINE = "TIER_1_API_AMLI_INLINE"
_TIER_FETCHED = "TIER_1_API_AMLI_NEXT_DATA"

# /apartments/{region}/{subregion}/{property-slug}
# property-slug is optional — submarket pages omit it.
_AMLI_URL_RE = re.compile(
    r"^/apartments/(?P<region>[a-z0-9-]+)/(?P<subregion>[a-z0-9-]+)(?:/(?P<property>[a-z0-9-]+))?/?$"
)
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _extract_next_data(html: str) -> dict[str, Any] | None:
    """Pull and JSON-parse the inline ``__NEXT_DATA__`` blob. None on miss."""
    if not html:
        return None
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))  # type: ignore[no-any-return]
    except (json.JSONDecodeError, TypeError):
        return None


def _walk(obj: Any, path: list[str]) -> Any:
    """Best-effort dotted-path walk. Returns None on any miss/type-mismatch."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _floorplan_arrays(next_data: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Find every queries[*].state.data array that looks like a floor-plan list.

    The shape is: pageProps.trpcState.json.queries -> [ { state: { data: [...] } } ].
    Some queries hold Prismic CMS data, others hold floor plans. We pick out the
    arrays whose first item has a ``floorplanName`` key — that's the load-bearing
    field across both submarket and property JSONs.
    """
    queries = _walk(next_data, ["pageProps", "trpcState", "json", "queries"])
    if not isinstance(queries, list):
        return []
    out: list[list[dict[str, Any]]] = []
    for q in queries:
        data = _walk(q, ["state", "data"])
        if not isinstance(data, list) or not data:
            continue
        first = data[0]
        if isinstance(first, dict) and "floorplanName" in first:
            out.append(data)
    return out


def _bedrooms_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _rent_int(v: Any) -> int | None:
    """AMLI rents come through as floats with cents — coerce to int dollars."""
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _avail_date(s: Any) -> str:
    """rpAvailableDate is ISO date or ISO timestamp; trim to YYYY-MM-DD."""
    if not s:
        return ""
    text = str(s)
    return text.split("T", 1)[0]


def _floorplan_property_uid(fp: dict[str, Any]) -> str | None:
    """AMLI may key the property association under several names depending on
    the schema generation. Probe the common ones."""
    for key in ("propertyUid", "propertySlug", "propertyName", "property_slug"):
        v = fp.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def parse_amli_floor_plans(
    floor_plans: list[dict[str, Any]],
    property_slug: str | None,
    source_url: str,
) -> list[dict[str, str]]:
    """Convert AMLI floor-plan dicts into our standard unit dicts.

    Filters by ``property_slug`` when provided (submarket JSON contains plans
    for several properties). When ``property_slug`` is None or no entry matches,
    falls through to substring match on ``propertySlug`` / ``propertyUid``.
    Rows without a ``units[]`` array do not produce floor-plan-level fallback
    rows here — JSON-LD already covers that path elsewhere.
    """
    out: list[dict[str, str]] = []

    # Filter: exact-match propertyUid wins; otherwise accept all (submarket
    # JSONs that don't carry propertyUid would be unusable if we filtered
    # too aggressively).
    if property_slug:
        matched = [fp for fp in floor_plans if _floorplan_property_uid(fp) == property_slug]
        if not matched:
            # Substring fallback handles slug renames (e.g. "amli-evanston" vs
            # "evanston"). Better one false-positive than zero recovery.
            matched = [
                fp for fp in floor_plans
                if property_slug in (_floorplan_property_uid(fp) or "")
            ]
        # If still nothing matched, AMLI may have served a single-property
        # JSON where every entry implicitly belongs to this property. Fall
        # through to all entries.
        if not matched:
            uids_seen = {_floorplan_property_uid(fp) for fp in floor_plans}
            uids_seen.discard(None)
            if not uids_seen:
                matched = floor_plans
        floor_plans = matched

    for fp in floor_plans:
        fp_name = str(fp.get("floorplanName") or "")
        beds = _bedrooms_int(fp.get("bedrooms"))
        baths_raw = fp.get("bathroomMax") or fp.get("bathroomMin") or fp.get("bathrooms")
        sqft_raw = fp.get("sqftMin") or fp.get("sqftMax") or fp.get("sqft")
        raw_units = fp.get("units")
        fp_units: list[Any] = raw_units if isinstance(raw_units, list) else []
        for u in fp_units:
            if not isinstance(u, dict):
                continue
            rent = _rent_int(u.get("rent") or u.get("priceMin"))
            out.append(
                make_unit_dict(
                    floor_plan_name=fp_name,
                    bed_label=bed_label_from(beds, fp_name),
                    bedrooms=str(beds) if beds is not None else "",
                    bathrooms=str(baths_raw) if baths_raw not in (None, "") else "",
                    sqft=str(sqft_raw) if sqft_raw not in (None, "") else "",
                    unit_number=str(u.get("unitNumber") or u.get("unit_number") or ""),
                    floor=str(u.get("floor") or ""),
                    rent_range=format_rent_range(rent, rent),
                    rent_low=rent,
                    rent_high=rent,
                    availability_status="AVAILABLE",
                    availability_date=_avail_date(u.get("rpAvailableDate") or u.get("availableDate")),
                    source_api_url=source_url,
                    extraction_tier=_TIER_FETCHED,
                )
            )
    return out


def _looks_like_amli_next_data(body: Any) -> bool:
    """Cheap shape check used by detector.confirm_detection."""
    if not isinstance(body, dict):
        return False
    fps = _floorplan_arrays(body)
    return bool(fps)


class AmliAdapter:
    """AMLI Residential PMS adapter (Next.js _next/data extraction)."""

    pms_name: str = "amli"
    _fingerprints: list[str] = ["amli.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER_FETCHED)

        try:
            parsed_url = urllib.parse.urlparse(ctx.base_url)
        except (ValueError, TypeError):
            result.errors.append("AMLI: unparseable base_url")
            return result

        path_match = _AMLI_URL_RE.match(parsed_url.path or "")
        if not path_match:
            result.errors.append(f"AMLI: URL path does not match property pattern ({parsed_url.path!r})")
            return result
        region = path_match.group("region")
        subregion = path_match.group("subregion")
        property_slug = path_match.group("property")

        # Decode HTML body from the L1 fetch result so we can pull the
        # inline __NEXT_DATA__ blob and the build_id.
        html = ""
        body = getattr(ctx.fetch_result, "body", None) if ctx.fetch_result is not None else None
        if isinstance(body, (bytes, bytearray)):
            html = body.decode("utf-8", errors="ignore")
        elif isinstance(body, str):
            html = body

        next_data = _extract_next_data(html)
        build_id = (next_data or {}).get("buildId") if isinstance(next_data, dict) else None

        # Step 1 — inline fast path. Most property pages do NOT carry a
        # populated floor-plan array inline (Prismic-only), so this usually
        # produces 0 units, but it's free when it works.
        if next_data:
            inline_fps = _floorplan_arrays(next_data)
            for fp_array in inline_fps:
                inline_units = parse_amli_floor_plans(fp_array, property_slug, ctx.base_url)
                if inline_units:
                    result.units.extend(inline_units)
                    result.tier_used = _TIER_INLINE
                    result.winning_url = ctx.base_url
            if result.units:
                # Stage 1 validity gate. If every parsed inline unit fails
                # validity, clear them and fall through to the submarket
                # refetch path.
                from ma_poc.extraction.post_process import post_process

                _pp_parsed = len(result.units)
                _pp = post_process(
                    result.units, property_id=getattr(ctx, "property_id", None)
                )
                if _pp.n_admitted > 0:
                    result.units = _pp.admitted
                    result.plan_summaries = _pp.plan_summaries
                    result.confidence = min(0.95, 0.7 + 0.05 * _pp.n_admitted)
                    return result
                result.units = []
                result.errors.append(
                    f"AMLI_INLINE_VALIDITY_REJECTED: {_pp_parsed} parsed rows "
                    f"failed unit_validity (no numeric dimension)"
                )

        # Step 2 — refetch the SUBMARKET _next/data JSON. This is the
        # authoritative source — it always has units when AMLI has them.
        if not build_id:
            result.errors.append("AMLI: no buildId in __NEXT_DATA__; cannot fetch submarket JSON")
            return result

        submarket_url = (
            f"https://www.amli.com/_next/data/{build_id}/en/apartments/"
            f"{region}/{subregion}.json"
        )
        # Inherit cookies + UA from the already-rendered page so Cloudflare
        # treats this as a continuation of the human-looking session.
        # 2026-07-11 adapter audit: the jugnu fetch-only path dispatches
        # with a STUB page (no .context) — the prior hard requirement made
        # every AMLI property die here (AttributeError) and fall through
        # to rent-less cross-page-merge junk. curl_cffi chrome
        # impersonation fetches the same _next/data JSON fine (amli.com
        # doesn't interactive-challenge that path), so use it whenever a
        # live browser context isn't available.
        payload: Any = None
        browser_ctx = getattr(page, "context", None)
        if browser_ctx is not None:
            try:
                request = browser_ctx.request
                resp = await request.get(submarket_url, timeout=15000)
            except Exception as exc:  # noqa: BLE001 — adapter never raises
                result.errors.append(
                    f"AMLI: submarket fetch failed: {type(exc).__name__}: {exc}"
                )
                return result
            if not resp.ok:
                result.errors.append(
                    f"AMLI: submarket fetch returned HTTP {resp.status}"
                )
                return result
            try:
                payload = await resp.json()
            except Exception as exc:  # noqa: BLE001
                result.errors.append(
                    f"AMLI: submarket JSON parse failed: {type(exc).__name__}"
                )
                return result
        else:
            try:
                import json as _json

                from ma_poc.pms.adapters._probe import probe_get

                _static_resp = probe_get(submarket_url, timeout=15)
                _status = getattr(_static_resp, "status_code", 0)
                if _status != 200:
                    result.errors.append(
                        f"AMLI: static submarket fetch returned HTTP {_status}"
                    )
                    return result
                payload = _json.loads(getattr(_static_resp, "text", "") or "")
                result.errors.append(
                    "AMLI: submarket JSON via static fetch (no live page context)"
                )
            except Exception as exc:  # noqa: BLE001
                result.errors.append(
                    f"AMLI: static submarket fetch failed: {type(exc).__name__}: {exc}"
                )
                return result

        result.api_responses.append({"url": submarket_url, "body": payload})

        for fp_array in _floorplan_arrays(payload):
            result.units.extend(parse_amli_floor_plans(fp_array, property_slug, submarket_url))

        if not result.units:
            result.errors.append(
                f"AMLI: submarket JSON had no matching floor plans (property={property_slug!r})"
            )
            return result

        # Stage 1 validity gate on submarket-fetched units.
        from ma_poc.extraction.post_process import post_process

        _pp_parsed = len(result.units)
        _pp = post_process(result.units, property_id=getattr(ctx, "property_id", None))
        if _pp.n_admitted == 0:
            result.units = []
            result.errors.append(
                f"AMLI_VALIDITY_REJECTED: {_pp_parsed} parsed rows "
                f"failed unit_validity (no numeric dimension)"
            )
            return result

        result.units = _pp.admitted
        result.plan_summaries = _pp.plan_summaries
        result.tier_used = _TIER_FETCHED
        result.winning_url = submarket_url
        result.confidence = min(0.95, 0.7 + 0.05 * _pp.n_admitted)
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    def matches_response_body(self, body: Any) -> bool:
        return _looks_like_amli_next_data(body)
