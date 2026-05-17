"""Apts247 / RentDynamics PMS adapter — UNIT-LEVEL.

2026-05-17 (canary iter-15, Chrome-MCP no-signature deep-probe): the
"no-signature" long-tail that static curl_cffi could not classify is
NOT random custom sites — a ≥4-site cluster runs on the Apts247 /
RentDynamics JS leasing widget (static2.apts247.info), invisible to
static HTML because the inventory is fetched client-side.

Apts247 marketing sites expose a SAME-ORIGIN REST API on the
property's own domain (NOT apts247.info, so it is not Cloudflare-
fronted the way securecafe is):

  https://<property-domain>/api/v1/floorplans/?api_key=<40-hex>

The ``api_key`` is embedded verbatim in the homepage HTML (the page's
own JS calls ``/api/v1/communitypromotion/?api_key=...`` etc.), so the
key is recoverable WITHOUT a browser — deterministic Tier-1.

Response envelope (Tastypie):
  {"meta": {"total_count": N, "next": null},
   "objects": [ {floorplan} ]}
Each floorplan: ``name``, ``apartment_type``/``display_bed`` ("Studio",
"1 Bed", ...), ``bath``, ``sq_ft``, ``rent`` ("$899"), and a nested
``units: [ {id, number, rent, available_date, floor, building,
sq_ft, display_bed} ]`` array of the actually-available units. Each
unit has a real numeric ``id`` + concrete ``rent`` ⇒ true unit-level.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import make_unit_dict
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

_TIER = "TIER_1_API_APTS247"

# api_key=<32-40 hex> as it appears in the page JS / inline config.
_APTS247_KEY_RE = re.compile(r"api_key['\"=:\s]+([a-f0-9]{32,40})", re.IGNORECASE)
_MONEY_RE = re.compile(r"[\d,]+")


def _rent_to_int(val: Any) -> int | None:
    """``"$1,899"`` / ``"$899"`` → int; ``"Call for details"`` / "" → None."""
    if val is None:
        return None
    m = _MONEY_RE.search(str(val))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _beds_from_label(label: Any) -> str:
    """``"Studio"`` → ``"0"``; ``"1 Bed"`` / ``"2 Bedroom"`` → ``"1"`` / ``"2"``."""
    s = str(label or "").strip()
    if not s:
        return ""
    if "studio" in s.lower():
        return "0"
    m = re.search(r"\d+", s)
    return m.group(0) if m else ""


def find_apts247_api_key(html: str) -> str | None:
    """Return the Apts247 ``api_key`` embedded in *html*, or None."""
    if not html:
        return None
    m = _APTS247_KEY_RE.search(html)
    return m.group(1) if m else None


def parse_apts247_floorplans(
    data: dict[str, Any], source_url: str
) -> list[dict[str, Any]]:
    """Apts247 ``/api/v1/floorplans/`` envelope → unit-level dicts.

    One row per available Unit (real unit ``id`` + concrete rent).
    Falls back to a plan-level row (floorplan ``rent``) when a plan has
    no available units but advertises a rent.
    """
    objects = data.get("objects")
    if not isinstance(objects, list):
        return []
    units: list[dict[str, Any]] = []
    for plan in objects:
        if not isinstance(plan, dict):
            continue
        plan_name = str(plan.get("name") or "").strip()
        beds = _beds_from_label(plan.get("display_bed") or plan.get("apartment_type"))
        baths = plan.get("bath")
        baths_s = str(baths) if baths is not None else ""
        plan_sqft = str(plan.get("sq_ft") or "").strip()
        plan_rent = _rent_to_int(plan.get("rent"))
        glist = plan.get("units") or []
        if isinstance(glist, list) and glist:
            for u in glist:
                if not isinstance(u, dict):
                    continue
                rent_i = _rent_to_int(u.get("rent"))
                if rent_i is None:
                    rent_i = plan_rent
                units.append(
                    make_unit_dict(
                        floor_plan_name=plan_name,
                        bed_label=str(plan.get("display_bed") or ""),
                        bedrooms=_beds_from_label(
                            u.get("display_bed") or plan.get("display_bed")
                        )
                        or beds,
                        bathrooms=baths_s,
                        sqft=str(u.get("sq_ft") or plan_sqft or ""),
                        unit_number=str(u.get("number") or "").strip(),
                        floor=str(u.get("floor") or ""),
                        building=str(u.get("building") or ""),
                        rent_low=rent_i,
                        rent_high=rent_i,
                        availability_status="AVAILABLE",
                        availability_date=str(u.get("available_date") or ""),
                        source_api_url=source_url,
                        extraction_tier=_TIER,
                    )
                )
        elif plan_rent is not None:
            units.append(
                make_unit_dict(
                    floor_plan_name=plan_name,
                    bed_label=str(plan.get("display_bed") or ""),
                    bedrooms=beds,
                    bathrooms=baths_s,
                    sqft=plan_sqft,
                    unit_number="",
                    rent_low=plan_rent,
                    rent_high=plan_rent,
                    availability_status="UNKNOWN",
                    source_api_url=source_url,
                    extraction_tier=_TIER,
                )
            )
    return units


def _origin_of(url: str) -> str:
    from urllib.parse import urlparse

    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


async def _fetch(url: str) -> str:
    from ma_poc.pms.adapters._probe import probe_get

    r = probe_get(url, timeout=25)
    if r.status_code != 200:
        return ""
    return r.text or ""


class Apts247Adapter:
    """Apts247 / RentDynamics same-origin ``/api/v1/floorplans/`` extractor."""

    pms_name = "apts247"

    def __init__(self) -> None:
        self._fingerprints = ["apts247", "rentdynamics", "static2.apts247.info"]

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    def matches_response_body(self, body: Any) -> bool:
        if isinstance(body, str):
            low = body.lower()
            return "apts247" in low or "rentdynamics" in low
        return False

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER)

        html = ""
        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        if isinstance(body, bytes):
            html = body.decode("utf-8", errors="replace")
        elif isinstance(body, str):
            html = body

        origin = ""
        if fr is not None:
            origin = _origin_of(str(getattr(fr, "final_url", "") or ""))
        origin = origin or _origin_of(getattr(ctx, "base_url", "") or "")
        if not origin:
            result.tier_used = f"{_TIER}_NO_ORIGIN"
            result.errors.append("APTS247: no resolvable origin")
            return result

        key = find_apts247_api_key(html)
        if not key:
            # Key not in the captured body — refetch the homepage.
            try:
                hh = await _fetch(origin + "/")
            except Exception:
                hh = ""
            key = find_apts247_api_key(hh)
        if not key:
            result.tier_used = f"{_TIER}_NO_KEY"
            result.confidence = 0.0
            result.errors.append("APTS247: api_key not discoverable in HTML")
            return result

        api_url = f"{origin}/api/v1/floorplans/?api_key={key}"
        try:
            raw = await _fetch(api_url)
        except Exception as exc:
            result.tier_used = f"{_TIER}_FETCH_ERROR"
            result.errors.append(
                f"apts247-fetch-error: {type(exc).__name__}: {str(exc)[:120]}"
            )
            return result

        import json

        try:
            data = json.loads(raw) if raw else None
        except (json.JSONDecodeError, ValueError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("objects"), list):
            result.tier_used = f"{_TIER}_SHAPE_REJECTED"
            result.errors.append("APTS247: no floorplans objects[] in API response")
            return result

        raw_units = parse_apts247_floorplans(data, api_url)
        if not raw_units:
            result.tier_used = f"{_TIER}_EMPTY"
            result.errors.append("APTS247: floorplans parsed but produced 0 rows")
            return result

        from ma_poc.extraction.post_process import post_process

        pp = post_process(raw_units, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = api_url
            result.confidence = min(0.92, 0.7 + 0.04 * pp.n_admitted)
            result.tier_used = _TIER
            result.api_responses.append(
                {
                    "url": api_url,
                    "status": 200,
                    "body": "<apts247-floorplans>",
                    "via": "apts247_probe",
                }
            )
            return result

        result.tier_used = f"{_TIER}_VALIDITY_REJECTED"
        result.errors.append(f"APTS247: {len(raw_units)} rows failed unit_validity")
        return result
