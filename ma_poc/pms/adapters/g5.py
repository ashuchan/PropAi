"""G5 Marketing Cloud adapter — merged (2026-05-19).

Two extraction paths, unified in ``G5Adapter``:

* **Path A — captured GraphQL response** (Patch #11, 2026-05-06 audit).
  ``inventory.g5marketingcloud.com/graphql`` was captured on 166 prod
  properties; 72 had zero rent because no parser knew the schema. The pure
  ``parse_g5_response`` parser (Floorplan + per-unit Apartment) handles a
  captured ``{"data": {...}}`` body. Tier ``TIER_1_API_G5_GRAPHQL``.
  Originally on branch ``claude/romantic-turing-9e0bb7`` (never merged to
  main); ported here verbatim.

* **Path B — Apollo cache fallback** (2026-05-19). G5 is a Vue SPA; the
  GraphQL POST fires once at SPA boot and is frequently *not* captured by
  XHR interception (the stale ``TIER_1_API_G5_EMPTY`` failures). The
  resolved data still lives in ``window.__APOLLO_CLIENT__.cache``. We read
  it directly: per-unit ``Apartment`` rows (unit #, availability date,
  rent via the ``Prices`` ref, dims inherited from the owning
  ``Floorplan``), falling back to plan-level ``Floorplan`` rows. Tier
  ``TIER_2_API_G5_APOLLO``. Verified live on livemarleymanor.com.

Path A is preferred when a GraphQL body was captured (richest, Tier-1);
Path B recovers the (more common) capture-miss case.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

__all__ = [
    "is_g5_graphql_url",
    "is_g5_graphql_body",
    "extract_floorplan_lists_from_g5_body",
    "parse_g5_floorplan",
    "parse_g5_apartment",
    "parse_g5_response",
    "parse_g5_apollo_floorplans",
    "parse_g5_apollo_units",
    "G5Adapter",
]


# ── Path A: captured GraphQL response parser (Patch #11, verbatim) ───────────


def is_g5_graphql_url(url: str) -> bool:
    """Return True iff the URL is the G5 Marketing Cloud GraphQL endpoint."""
    if not url:
        return False
    u = url.lower()
    return "inventory.g5marketingcloud.com" in u and "/graphql" in u


def is_g5_graphql_body(body: Any) -> bool:
    """True iff body looks like a G5 GraphQL response carrying a floorplan
    or apartment list. Pure / never raises.
    """
    if not isinstance(body, dict):
        return False
    data = body.get("data")
    if not isinstance(data, dict):
        return False
    return bool(_walk_for_floorplan_list(data) or _walk_for_apartment_list(data))


_FLOORPLAN_LIST_KEYS = (
    "floorplans",
    "floorplanList",
    "apartmentComplexFloorplans",
)
_APARTMENT_LIST_KEYS = (
    "apartments",
    "apartmentList",
    "units",
)
_FP_SIGNAL_KEYS = {
    "name",
    "beds",
    "baths",
    "sqft",
    "sqftDisplay",
    "startingRate",
    "endingRate",
    "rateDisplay",
    "totalAvailableUnits",
    "totalRentStarting",
}
_APT_SIGNAL_KEYS = {
    "name",
    "displayName",
    "building",
    "availabilityDate",
    "sqftDisplay",
    "prices",
    "floorplan",
}


def _looks_like_floorplan_list(items: list[Any]) -> bool:
    if not items or not isinstance(items[0], dict):
        return False
    return len(_FP_SIGNAL_KEYS & set(items[0].keys())) >= 2


def _looks_like_apartment_list(items: list[Any]) -> bool:
    if not items or not isinstance(items[0], dict):
        return False
    keys = set(items[0].keys())
    # Apartment must have ≥2 signal keys AND ≥1 apt-only key, else a
    # floorplan list (shares name+sqftDisplay) would match as both.
    apt_only = {"displayName", "building", "availabilityDate", "prices", "floorplan"}
    return len(_APT_SIGNAL_KEYS & keys) >= 2 and bool(apt_only & keys)


def _walk_for_floorplan_list(node: Any, depth: int = 0) -> list[dict[str, Any]] | None:
    if depth > 6:
        return None
    if isinstance(node, list):
        if _looks_like_floorplan_list(node):
            return list(node)
        return None
    if isinstance(node, dict):
        for key in _FLOORPLAN_LIST_KEYS:
            v = node.get(key)
            if isinstance(v, list) and _looks_like_floorplan_list(v):
                return list(v)
        for v in node.values():
            res = _walk_for_floorplan_list(v, depth + 1)
            if res:
                return res
    return None


def _walk_for_apartment_list(node: Any, depth: int = 0) -> list[dict[str, Any]] | None:
    if depth > 6:
        return None
    if isinstance(node, list):
        if _looks_like_apartment_list(node):
            return list(node)
        return None
    if isinstance(node, dict):
        for key in _APARTMENT_LIST_KEYS:
            v = node.get(key)
            if isinstance(v, list) and _looks_like_apartment_list(v):
                return list(v)
        for v in node.values():
            res = _walk_for_apartment_list(v, depth + 1)
            if res:
                return res
    return None


def extract_floorplan_lists_from_g5_body(body: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (floorplans, apartments) lists from a G5 body. Either may be
    empty. Pure / never raises.
    """
    if not isinstance(body, dict):
        return [], []
    data = body.get("data")
    if not isinstance(data, dict):
        return [], []
    fps = _walk_for_floorplan_list(data) or []
    apts = _walk_for_apartment_list(data) or []
    return fps, apts


def _parse_baths(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def parse_g5_floorplan(item: dict[str, Any], url: str) -> dict[str, str] | None:
    """Convert a G5 Floorplan record into a unit dict, or None."""
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    beds = item.get("beds")
    baths = _parse_baths(item.get("baths"))
    sqft_n = item.get("sqft")
    sqft_disp = str(item.get("sqftDisplay") or "").strip()
    if isinstance(sqft_n, (int, float)) and sqft_n > 0:
        sqft = str(int(sqft_n))
    else:
        sqft = sqft_disp

    rent_lo_n = item.get("startingRate") or item.get("totalRentStarting")
    rent_hi_n = item.get("endingRate") or item.get("totalRentEnding")
    rent_lo = money_to_int(str(rent_lo_n)) if rent_lo_n else None
    rent_hi = money_to_int(str(rent_hi_n)) if rent_hi_n else None

    if not (name or beds is not None or sqft or rent_lo):
        return None

    avail_n = item.get("totalAvailableUnits")
    avail = str(avail_n) if isinstance(avail_n, (int, float)) and avail_n > 0 else ""

    beds_str = str(int(beds)) if isinstance(beds, (int, float)) else ""
    baths_str = str(baths) if baths is not None else ""

    return make_unit_dict(
        floor_plan_name=name,
        bed_label=bed_label_from(int(beds), name) if isinstance(beds, (int, float)) else "",
        bedrooms=beds_str,
        bathrooms=baths_str,
        sqft=sqft,
        unit_number="",
        rent_range=format_rent_range(rent_lo, rent_hi),
        availability_status="AVAILABLE" if rent_lo or avail else "",
        available_units=avail,
        availability_date="",
        source_api_url=url,
        extraction_tier="TIER_1_API_G5_GRAPHQL",
    )


def parse_g5_apartment(item: dict[str, Any], url: str) -> dict[str, str] | None:
    """Convert a G5 Apartment (per-unit) record into a unit dict, or None."""
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or item.get("displayName") or "").strip()
    building = str(item.get("building") or "").strip()
    avail_dt = str(item.get("availabilityDate") or "").strip()
    sqft = str(item.get("sqftDisplay") or "").strip()

    prices = item.get("prices")
    rent_lo: int | None = None
    rent_hi: int | None = None
    if isinstance(prices, list):
        candidates: list[int] = []
        for p in prices:
            if not isinstance(p, dict):
                continue
            for k in ("min", "minRent", "starting", "amount", "value"):
                v = p.get(k)
                vi = money_to_int(str(v)) if v is not None else None
                if vi:
                    candidates.append(vi)
                    break
        if candidates:
            rent_lo = min(candidates)
            rent_hi = max(candidates)

    fp = item.get("floorplan")
    fp_name = ""
    beds = None
    baths = None
    if isinstance(fp, dict):
        fp_name = str(fp.get("name") or "").strip()
        beds = fp.get("beds")
        baths = _parse_baths(fp.get("baths"))

    if not (name or fp_name or rent_lo or sqft):
        return None

    return make_unit_dict(
        floor_plan_name=fp_name or name,
        bed_label=bed_label_from(int(beds), fp_name or name)
        if isinstance(beds, (int, float))
        else "",
        bedrooms=str(int(beds)) if isinstance(beds, (int, float)) else "",
        bathrooms=str(baths) if baths is not None else "",
        sqft=sqft,
        unit_number=name or "",
        building=building,
        rent_range=format_rent_range(rent_lo, rent_hi),
        availability_status="AVAILABLE" if rent_lo or avail_dt else "",
        available_units="1" if rent_lo else "",
        availability_date=avail_dt,
        source_api_url=url,
        extraction_tier="TIER_1_API_G5_GRAPHQL",
    )


def parse_g5_response(body: Any, url: str) -> list[dict[str, str]]:
    """Top-level captured-response parser. Prefer per-unit Apartment records
    (richer); fall back to Floorplan records (one per plan).
    """
    if not isinstance(body, dict):
        return []
    fps, apts = extract_floorplan_lists_from_g5_body(body)
    out: list[dict[str, str]] = []
    if apts:
        for it in apts:
            u = parse_g5_apartment(it, url)
            if u:
                out.append(u)
    if not out and fps:
        for it in fps:
            u = parse_g5_floorplan(it, url)
            if u:
                out.append(u)
    return out


# ── Path B: Apollo cache fallback (plan-level + unit-level join) ─────────────

# Reads window.__APOLLO_CLIENT__.cache. Returns {floorplans, units}:
#  - floorplans: plan-level rows (numeric startingRate/endingRate).
#  - units: per-unit Apartment rows. The Apollo cache stores apartments
#    under ROOT_QUERY.units({...floorplanId:N...}) keys, so the owning
#    floorplan id is recovered from the cache key; rent is the Prices ref
#    (prices:[{id:"Prices:NNN"}] → cache["Prices:NNN"].value); dims are
#    inherited from the Floorplan with matching id.
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
    """Unit-level rows from the Apollo Apartment↔Prices↔Floorplan join."""
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


class G5Adapter:
    """G5 (g5marketingcloud) adapter. Captured-GraphQL first, Apollo fallback."""

    pms_name: str = "g5"
    _fingerprints: list[str] = ["g5marketingcloud", "g5dxm.com", "g5-c-"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used="TIER_1_API_G5_GRAPHQL")

        # Path A — captured inventory.g5marketingcloud.com/graphql response.
        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", []) or []
        a_units: list[dict[str, str]] = []
        for resp in api_responses:
            url = resp.get("url") or ""
            body = resp.get("body")
            if body is not None and is_g5_graphql_url(url) and is_g5_graphql_body(body):
                try:
                    a_units.extend(parse_g5_response(body, url))
                except Exception as exc:
                    result.errors.append(f"g5-graphql-parse-error: {exc}")
        if a_units:
            from ma_poc.extraction.post_process import post_process

            pp = post_process(a_units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                result.units = pp.admitted
                result.plan_summaries = pp.plan_summaries
                result.tier_used = "TIER_1_API_G5_GRAPHQL"
                result.confidence = min(0.95, 0.7 + 0.05 * pp.n_admitted)
                return result

        # Path B — Apollo cache fallback (the common capture-miss case).
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            result.confidence = 0.0
            result.errors.append("G5: no captured GraphQL and no live page")
            return result
        try:
            cache = await evaluate(_G5_APOLLO_JS)
        except Exception as exc:
            log.debug("G5 Apollo extract failed err=%s", exc)
            cache = None

        fps = cache.get("floorplans") if isinstance(cache, dict) else None
        urows = cache.get("units") if isinstance(cache, dict) else None
        win = self._winning_url(page, ctx)

        # Prefer unit-level rows; fall back to plan-level Floorplan rows.
        for parsed in (
            parse_g5_apollo_units(urows or [], win),
            parse_g5_apollo_floorplans(fps or [], win),
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
                return result

        result.confidence = 0.0
        result.errors.append(
            "G5: no captured GraphQL response and no Apollo Floorplan/unit data"
        )
        return result

    @staticmethod
    def _winning_url(page: Page, ctx: AdapterContext) -> str:
        try:
            return page.url or getattr(ctx, "base_url", "") or ""
        except Exception:
            return getattr(ctx, "base_url", "") or ""

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
