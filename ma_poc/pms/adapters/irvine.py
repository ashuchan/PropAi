"""
Irvine Company Apartments adapter.

Research log
------------
Single REIT, single host (irvinecompanyapartments.com). AEM/Vue SPA.
The public Algolia surface (search.irvinecompanyapartments.com/search,
prod_ica_floorplan) is FLOORPLAN-level only (calc_min/maxRent ranges) —
not genuine Tier-1. Genuine per-unit data comes from a separate public
endpoint discovered via user HAR (2026-05-18):

  POST https://search.irvinecompanyapartments.com/units/rank
  body: {"communityId":"<communityIdAEM GUID>","unitsPerFloor":100,"env":"prod"}
  -> 200, application/json, list of floorplan groups; each group.units[]
     is genuine unit-level: unitID, floorplanBed/Bath, unitSqFt,
     unitLeasePrice[{term,price,date}], unitEarliestAvailable{date,price},
     buildingNumber, unitFloor. NO auth / NO cookie. Verified 5/5
     catalog communities (105/35/7/3/10 units).

The communityIdAEM GUID is in the community page HTML as the hidden
input ``id="contact_aemCommunityId" value="<GUID>"`` (also appears as
communityIdAEM / aemCommunityId / communityIDAEM elsewhere).

Recipe (fully deterministic, public):
  1. read community-page HTML
  2. regex communityIdAEM GUID
  3. POST /units/rank {communityId, unitsPerFloor:100, env:prod}
  4. flatten groups[].units[] -> unit dicts -> post_process
"""

from __future__ import annotations

import re
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

OLL_TIER = "TIER_1_API_IRVINE"
_RANK_URL = "https://search.irvinecompanyapartments.com/units/rank"

_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
# id="contact_aemCommunityId" value="<GUID>" (and communityIdAEM/aemCommunityId variants)
_CID_RE = re.compile(
    r"(?:contact_aemCommunityId|communityIdAEM|aemCommunityId|communityIDAEM)"
    r"\D{0,20}(" + _GUID + r")",
    re.IGNORECASE,
)


def _ymd(s: Any) -> str:
    """``20260621`` -> ``2026-06-21``; pass through anything else cleanly."""
    s = str(s or "").strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    if "T" in s:
        return s.split("T")[0]
    return s


def _unit_rents(u: dict[str, Any]) -> tuple[int | None, int | None]:
    """Low/high asking rent across the unit's lease-term price ladder."""
    prices: list[int] = []
    for lp in u.get("unitLeasePrice") or []:
        if isinstance(lp, dict):
            p = money_to_int(str(lp.get("price") or ""))
            if p:
                prices.append(p)
    ea = u.get("unitEarliestAvailable")
    if isinstance(ea, dict):
        p = money_to_int(str(ea.get("price") or ""))
        if p:
            prices.append(p)
    sp = money_to_int(str(u.get("unitStartingPrice") or ""))
    if sp:
        prices.append(sp)
    if not prices:
        return None, None
    return min(prices), max(prices)


def parse_irvine_units(
    groups: list[dict[str, Any]],
    url: str,
    request_payload: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Flatten ``units/rank`` floorplan groups into standard unit dicts."""
    units: list[dict[str, str]] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        for u in g.get("units") or []:
            if not isinstance(u, dict):
                continue
            unit_no = str(u.get("unitID") or u.get("unitMarketingName") or u.get("objectID") or "").strip()
            beds_raw = u.get("floorplanBed")
            baths_raw = u.get("floorplanBath")
            if u.get("unitIsStudio") and not beds_raw:
                beds = 0
            else:
                try:
                    beds = int(float(beds_raw)) if beds_raw not in (None, "") else None
                except (TypeError, ValueError):
                    beds = None
            try:
                baths = float(baths_raw) if baths_raw not in (None, "") else None
            except (TypeError, ValueError):
                baths = None
            sqft_raw = u.get("unitSqFt") or u.get("floorplanSqFt")
            sqft = str(sqft_raw) if sqft_raw not in (None, "", 0) else ""

            rent_lo, rent_hi = _unit_rents(u)

            ea = u.get("unitEarliestAvailable")
            avail_date = _ymd(ea.get("date")) if isinstance(ea, dict) else ""

            fp_name = str(u.get("floorplanName") or u.get("unitTypeName") or "").strip()
            building = str(u.get("buildingNumber") or "").strip()
            floor = u.get("unitFloor")

            source_unit_id = str(u.get("unitID") or "").strip()
            source_property_id = str(u.get("propertyID") or "").strip()
            object_id = str(u.get("objectID") or "").strip()
            floorplan_id = str(u.get("floorplanID") or "").strip()
            floorplan_unique_id = str(u.get("floorplanUniqueID") or "").strip()
            community_id = str(u.get("communityIDAEM") or "").strip()
            community_name = str(u.get("communityMarketingName") or "").strip()
            property_address = str(u.get("propertyAddress") or "").strip()
            source_ids: dict[str, str] = {}
            if source_unit_id:
                source_ids["irvine_unit_id"] = source_unit_id
            if object_id:
                source_ids["irvine_object_id"] = object_id
            if floorplan_id:
                source_ids["irvine_floorplan_id"] = floorplan_id
            if floorplan_unique_id:
                source_ids["irvine_floorplan_unique_id"] = floorplan_unique_id
            if source_property_id:
                source_ids["irvine_property_id"] = source_property_id
            if community_id:
                source_ids["irvine_community_id"] = community_id

            row = make_unit_dict(
                floor_plan_name=fp_name,
                bed_label=bed_label_from(beds, fp_name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=(
                    str(int(baths))
                    if baths is not None and baths == int(baths)
                    else (str(baths) if baths is not None else "")
                ),
                sqft=sqft,
                unit_number=unit_no,
                unit_name=unit_no,
                floor=str(floor) if floor not in (None, "") else "",
                building=building,
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                concession="Discount" if u.get("unitHasDiscount") else "",
                availability_status="AVAILABLE",
                availability_date=avail_date,
                source_api_url=url,
                extraction_tier=OLL_TIER,
                source_ids=source_ids or None,
            )
            # unitID repeats across property groups inside Irvine master
            # communities. The public response proves propertyID + unitID is
            # unique, while objectID carries the same pair plus plan context.
            canonical_unit_id = (
                f"{source_property_id}:{source_unit_id}"
                if source_property_id and source_unit_id
                else object_id
            )
            if canonical_unit_id:
                row["unit_id"] = canonical_unit_id
            if source_property_id:
                row["source_property_id"] = source_property_id
            if community_name:
                row["source_property_name"] = community_name
            if property_address:
                row["source_property_address"] = property_address
            if request_payload:
                row["source_request_payload"] = dict(request_payload)
            if source_ids or request_payload:
                row["source_property_provenance"] = "irvine_units_rank_row"
            units.append(row)
    return units


def _extract_community_id(html: str) -> str | None:
    m = _CID_RE.search(html or "")
    return m.group(1) if m else None


def _groups_from_body(body: Any) -> list[dict[str, Any]]:
    """``units/rank`` returns a top-level list of floorplan groups."""
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return body
    if isinstance(body, dict):
        for k in ("groups", "data", "results"):
            c = body.get(k)
            if isinstance(c, list) and c and isinstance(c[0], dict):
                return c
    return []


def _is_irvine_rank(groups: list[dict[str, Any]]) -> bool:
    if not groups:
        return False
    g0 = groups[0]
    if not isinstance(g0, dict) or "units" not in g0:
        return False
    for g in groups:
        for u in g.get("units") or []:
            if isinstance(u, dict) and "unitID" in u:
                return True
    return False


async def _community_html(page: Page | None, ctx: AdapterContext) -> str:
    """Raw community HTML for communityIdAEM extraction (raw > hydrated)."""
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None)
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            body = None
    if isinstance(body, str) and _CID_RE.search(body):
        return body
    base = getattr(ctx, "base_url", "") or ""
    if base:
        try:
            from ma_poc.pms.adapters._probe import probe_get

            r = probe_get(base, timeout=20)
            if getattr(r, "status_code", 0) == 200 and r.text:
                return str(r.text)
        except Exception:
            pass
    if isinstance(body, str) and body:
        return body
    if page is not None:
        try:
            return await page.content()
        except Exception:
            return ""
    return ""


async def _active_fetch_irvine(page: Page | None, ctx: AdapterContext) -> list[dict[str, Any]]:
    """Resolve communityIdAEM from HTML and POST the public units/rank API.

    Public (no auth/cookie — confirmed), so curl_cffi via ``probe_post``
    is the primary path; browser context is the fallback. Never raises.
    """
    out: list[dict[str, Any]] = []
    html = await _community_html(page, ctx)
    cid = _extract_community_id(html)
    if not cid:
        return out
    payload = {"communityId": cid, "unitsPerFloor": 100, "env": "prod"}

    try:
        from ma_poc.pms.adapters._probe import probe_post

        r = probe_post(
            _RANK_URL,
            json=payload,
            timeout=20,
            headers={
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json;charset=UTF-8",
                "origin": "https://www.irvinecompanyapartments.com",
                "referer": "https://www.irvinecompanyapartments.com/",
            },
        )
        if getattr(r, "status_code", 0) == 200:
            groups = _groups_from_body(r.json())
            if _is_irvine_rank(groups):
                out.append(
                    {
                        "url": _RANK_URL,
                        "body": groups,
                        "request_payload": dict(payload),
                    }
                )
                return out
    except Exception:
        pass

    if page is not None:
        try:
            resp = await page.context.request.post(
                _RANK_URL,
                data=payload,
                headers={
                    "accept": "application/json, text/plain, */*",
                    "content-type": "application/json;charset=UTF-8",
                },
                timeout=15000,
            )
            if resp.ok:
                groups = _groups_from_body(await resp.json())
                if _is_irvine_rank(groups):
                    out.append(
                        {
                            "url": _RANK_URL,
                            "body": groups,
                            "request_payload": dict(payload),
                        }
                    )
        except Exception:
            return out
    return out


class IrvineAdapter:
    """Irvine Company Apartments PMS adapter."""

    pms_name: str = "irvine"
    _fingerprints: list[str] = ["irvinecompanyapartments.com", "communityIdAEM"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units via the public ``/units/rank`` endpoint."""
        result = AdapterResult(tier_used=OLL_TIER)
        all_units: list[dict[str, str]] = []

        sources: list[dict[str, Any]] = []
        for resp in getattr(ctx, "_api_responses", []) or []:
            groups = _groups_from_body(resp.get("body"))
            if _is_irvine_rank(groups):
                sources.append({"url": resp.get("url", ""), "body": groups})

        if not sources:
            sources = await _active_fetch_irvine(page, ctx)

        for src in sources:
            parsed = parse_irvine_units(
                src["body"],
                src.get("url", ""),
                src.get("request_payload"),
            )
            if parsed:
                all_units.extend(parsed)
                result.api_responses.append(src)

        if all_units:
            from ma_poc.extraction.post_process import post_process

            _pp_parsed = len(all_units)
            _pp = post_process(all_units, property_id=getattr(ctx, "property_id", None))
            if _pp.n_admitted > 0:
                result.units = _pp.admitted
                result.plan_summaries = _pp.plan_summaries
                result.winning_url = result.api_responses[0].get("url") if result.api_responses else None
                result.confidence = min(0.90, 0.7 + 0.05 * _pp.n_admitted)
            else:
                result.confidence = 0.0
                result.errors.append(
                    f"IRVINE_VALIDITY_REJECTED: {_pp_parsed} parsed rows failed unit_validity"
                )
        else:
            result.confidence = 0.0
            result.errors.append("No Irvine unit data (communityIdAEM not found or units/rank empty)")

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
