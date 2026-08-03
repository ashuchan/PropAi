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


def _trpc_queries(next_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return dehydrated tRPC queries from both observed Next.js roots."""
    for path in (
        ["props", "pageProps", "trpcState", "json", "queries"],
        ["pageProps", "trpcState", "json", "queries"],
    ):
        queries = _walk(next_data, path)
        if isinstance(queries, list):
            return [query for query in queries if isinstance(query, dict)]
    return []


def _floorplan_arrays(next_data: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Find every queries[*].state.data array that looks like a floor-plan list.

    The shape is: pageProps.trpcState.json.queries -> [ { state: { data: [...] } } ].
    Some queries hold Prismic CMS data, others hold floor plans. We pick out the
    arrays whose first item has a ``floorplanName`` key — that's the load-bearing
    field across both submarket and property JSONs.
    """
    out: list[list[dict[str, Any]]] = []
    for q in _trpc_queries(next_data):
        data = _walk(q, ["state", "data"])
        if not isinstance(data, list) or not data:
            continue
        first = data[0]
        if isinstance(first, dict) and "floorplanName" in first:
            out.append(data)
    return out


def _identity_text(value: Any) -> str:
    if isinstance(value, bool) or value in (None, ""):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _amli_floorplan_query_records(next_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only exact ``["amli", "floorplans"]`` query records.

    The current property and submarket pages also carry floor-plan-shaped
    highlights and sibling arrays.  Query identity is the authoritative
    boundary; a bare list shape is not.
    """
    records: list[dict[str, Any]] = []
    for query in _trpc_queries(next_data):
        query_key = query.get("queryKey")
        if not isinstance(query_key, list) or not query_key:
            continue
        path = query_key[0]
        if path != ["amli", "floorplans"] and path != ("amli", "floorplans"):
            continue
        options = query_key[1] if len(query_key) > 1 and isinstance(query_key[1], dict) else {}
        query_input = options.get("input") if isinstance(options.get("input"), dict) else {}
        data = _walk(query, ["state", "data"])
        if not isinstance(data, list):
            continue
        records.append(
            {
                "floor_plans": data,
                "amli_property_id": _identity_text(query_input.get("amliPropertyId")),
                "prismic_property_id": _identity_text(
                    query_input.get("propertyId")
                    or query_input.get("propertyDocumentID")
                    or query_input.get("prismicPropertyId")
                ),
                "query_key": query_key,
            }
        )
    return records


def _select_amli_floorplan_query(
    next_data: dict[str, Any],
    *,
    property_slug: str | None,
    expected_identity: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Select one property-bound floor-plan query or explain the miss."""
    records = _amli_floorplan_query_records(next_data)
    if not records:
        return None, "missing_exact_query"

    expected_amli = _identity_text((expected_identity or {}).get("amli_property_id"))
    expected_prismic = _identity_text((expected_identity or {}).get("prismic_property_id"))
    candidates: list[dict[str, Any]] = []
    for record in records:
        if expected_amli and record["amli_property_id"] != expected_amli:
            continue
        if expected_prismic and record["prismic_property_id"] != expected_prismic:
            continue
        if not expected_identity and property_slug:
            observed_slugs = {
                slug
                for fp in record["floor_plans"]
                if isinstance(fp, dict)
                for slug in [_floorplan_property_uid(fp)]
                if slug
            }
            if observed_slugs and observed_slugs != {property_slug}:
                continue
        if not record["amli_property_id"] or not record["prismic_property_id"]:
            continue
        candidates.append(record)

    if not candidates:
        return None, "no_property_bound_query"
    identities = {
        (record["amli_property_id"], record["prismic_property_id"])
        for record in candidates
    }
    if len(identities) != 1:
        return None, "contradictory_exact_queries"
    # Duplicate dehydrated records with the same identity are harmless; use
    # the most complete response rather than unioning repeated snapshots.
    return max(candidates, key=lambda record: len(record["floor_plans"])), "matched"


def _amli_query_identity(record: dict[str, Any]) -> dict[str, str]:
    return {
        "amli_property_id": _identity_text(record.get("amli_property_id")),
        "prismic_property_id": _identity_text(record.get("prismic_property_id")),
    }


def _record_amli_unit_source(
    result: AdapterResult,
    *,
    source_url: str,
    record: dict[str, Any],
    unit_count: int,
    property_slug: str | None,
) -> None:
    from ma_poc.pms.source_provenance import build_unit_source_provenance

    identity = _amli_query_identity(record)
    result.unit_source_provenance.append(
        build_unit_source_provenance(
            provider="amli",
            source_url=source_url,
            body=record.get("floor_plans") or [],
            unit_count=unit_count,
            identity={
                "status": "MATCH",
                "evidence": ["exact_amli_floorplans_query", "query_property_ids"],
                "configured_slug": property_slug or "",
                **identity,
            },
        )
    )


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
    cms = fp.get("cms")
    cms_data = cms.get("data") if isinstance(cms, dict) else None
    properties = cms_data.get("properties") if isinstance(cms_data, dict) else None
    if isinstance(properties, list):
        for wrapper in properties:
            prop = wrapper.get("property") if isinstance(wrapper, dict) else None
            if not isinstance(prop, dict):
                continue
            for key in ("uid", "slug"):
                value = prop.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _floorplan_prismic_property_ids(fp: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    cms = fp.get("cms")
    cms_data = cms.get("data") if isinstance(cms, dict) else None
    properties = cms_data.get("properties") if isinstance(cms_data, dict) else None
    if not isinstance(properties, list):
        return out
    for wrapper in properties:
        prop = wrapper.get("property") if isinstance(wrapper, dict) else None
        if isinstance(prop, dict):
            value = _identity_text(prop.get("id"))
            if value:
                out.add(value)
    return out


def parse_amli_floor_plans(
    floor_plans: list[dict[str, Any]],
    property_slug: str | None,
    source_url: str,
    *,
    query_identity: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Convert AMLI floor-plan dicts into our standard unit dicts.

    Filters by ``property_slug`` when provided (submarket JSON contains plans
    for several properties). When ``property_slug`` is None or no entry matches,
    falls through to substring match on ``propertySlug`` / ``propertyUid``.
    Rows without a ``units[]`` array do not produce floor-plan-level fallback
    rows here — JSON-LD already covers that path elsewhere.
    """
    out: list[dict[str, Any]] = []

    # A current exact query is already property-bound.  Without one, require
    # exact row/CMS slug evidence; never accept an unscoped submarket array.
    if property_slug and not query_identity:
        matched = [fp for fp in floor_plans if _floorplan_property_uid(fp) == property_slug]
        floor_plans = matched

    expected_amli_id = _identity_text((query_identity or {}).get("amli_property_id"))
    expected_prismic_id = _identity_text((query_identity or {}).get("prismic_property_id"))

    for fp in floor_plans:
        row_amli_id = _identity_text(fp.get("propertyId") or fp.get("amliPropertyId"))
        if expected_amli_id and row_amli_id and row_amli_id != expected_amli_id:
            continue
        row_prismic_ids = _floorplan_prismic_property_ids(fp)
        if expected_prismic_id and row_prismic_ids and expected_prismic_id not in row_prismic_ids:
            continue
        fp_name = str(fp.get("floorplanName") or "")
        beds_value = fp.get("bedroomMax")
        if beds_value in (None, ""):
            beds_value = fp.get("bedroomMin")
        if beds_value in (None, ""):
            beds_value = fp.get("bedrooms")
        beds = _bedrooms_int(beds_value)
        baths_raw = fp.get("bathroomMax") or fp.get("bathroomMin") or fp.get("bathrooms")
        raw_units = fp.get("units")
        fp_units: list[Any] = raw_units if isinstance(raw_units, list) else []
        for u in fp_units:
            if not isinstance(u, dict):
                continue
            rent = _rent_int(u.get("rent") or u.get("priceMin"))
            unit_native_id = _identity_text(u.get("unitId"))
            public_number = str(u.get("unitNumber") or u.get("unit_number") or "")
            unit_sqft = u.get("squareFeet")
            if unit_sqft in (None, ""):
                unit_sqft = fp.get("sqftMin") or fp.get("sqftMax") or fp.get("sqft")
            source_ids: dict[str, Any] = {}
            if unit_native_id:
                source_ids["amli_unit_id"] = unit_native_id
            engrain_unit_id = _identity_text(u.get("engrainUnitId"))
            if engrain_unit_id:
                source_ids["amli_engrain_unit_id"] = engrain_unit_id
            entrata_unit_id = _identity_text(u.get("entrataUnitId"))
            if entrata_unit_id and entrata_unit_id != "0":
                source_ids["amli_entrata_unit_id"] = entrata_unit_id
            floor_plan_id = _identity_text(fp.get("floorplanId") or fp.get("id"))
            if floor_plan_id and floor_plan_id != "0":
                source_ids["amli_floor_plan_id"] = floor_plan_id
            if expected_amli_id:
                source_ids["amli_property_id"] = expected_amli_id
            if expected_prismic_id:
                source_ids["amli_prismic_property_id"] = expected_prismic_id
            entrata_property_id = _identity_text(fp.get("entrataPropertyId"))
            if entrata_property_id and entrata_property_id != "0":
                source_ids["amli_entrata_property_id"] = entrata_property_id
            row = make_unit_dict(
                    floor_plan_name=fp_name,
                    bed_label=bed_label_from(beds, fp_name),
                    bedrooms=str(beds) if beds is not None else "",
                    bathrooms=str(baths_raw) if baths_raw not in (None, "") else "",
                    sqft=str(unit_sqft) if unit_sqft not in (None, "") else "",
                    unit_number=public_number,
                    unit_name=public_number,
                    floor=str(u.get("floor") or ""),
                    building=str(u.get("buildingNumber") or ""),
                    rent_range=format_rent_range(rent, rent),
                    rent_low=rent,
                    rent_high=rent,
                    availability_status="AVAILABLE",
                    availability_date=_avail_date(u.get("rpAvailableDate") or u.get("availableDate")),
                    source_api_url=source_url,
                    extraction_tier=_TIER_FETCHED,
                    source_ids=source_ids,
                )
            if unit_native_id:
                row["unit_id"] = unit_native_id
            out.append(row)
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
            exact_record, exact_status = _select_amli_floorplan_query(
                next_data,
                property_slug=property_slug,
            )
            if exact_status == "contradictory_exact_queries":
                result.errors.append("AMLI: contradictory property-bound floorplan queries")
                return result
            if exact_record is not None:
                target_identity = _amli_query_identity(exact_record)
                result.units = parse_amli_floor_plans(
                    exact_record["floor_plans"],
                    property_slug,
                    ctx.base_url,
                    query_identity=target_identity,
                )
                result.tier_used = _TIER_INLINE
                result.winning_url = ctx.base_url
                _record_amli_unit_source(
                    result,
                    source_url=ctx.base_url,
                    record=exact_record,
                    unit_count=len(result.units),
                    property_slug=property_slug,
                )
                if not result.units:
                    result.errors.append("AMLI: exact property query published no physical units")
                    return result
            else:
                target_identity = None
                # Backward-compatible old schema: only exact row/CMS slugs are
                # admissible.  Unscoped arrays now produce zero rows.
                for fp_array in _floorplan_arrays(next_data):
                    result.units.extend(
                        parse_amli_floor_plans(fp_array, property_slug, ctx.base_url)
                    )
                if result.units:
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
                # The exact property-bound response was present; do not replace
                # a validity failure with a sibling-rich submarket response.
                if exact_record is not None:
                    return result
        else:
            target_identity = None

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

        exact_record, exact_status = _select_amli_floorplan_query(
            payload if isinstance(payload, dict) else {},
            property_slug=property_slug,
            expected_identity=target_identity,
        )
        if exact_status == "contradictory_exact_queries":
            result.errors.append("AMLI: contradictory submarket floorplan queries")
            return result
        if exact_record is not None:
            query_identity = _amli_query_identity(exact_record)
            result.units = parse_amli_floor_plans(
                exact_record["floor_plans"],
                property_slug,
                submarket_url,
                query_identity=query_identity,
            )
            _record_amli_unit_source(
                result,
                source_url=submarket_url,
                record=exact_record,
                unit_count=len(result.units),
                property_slug=property_slug,
            )
        else:
            # Legacy submarket schema: exact row/CMS slug filtering is still
            # permitted, but an identity-free array can no longer win.
            for fp_array in _floorplan_arrays(payload if isinstance(payload, dict) else {}):
                result.units.extend(
                    parse_amli_floor_plans(fp_array, property_slug, submarket_url)
                )

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
