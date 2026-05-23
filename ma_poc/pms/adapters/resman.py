"""ResMan PMS adapter — UNIT-LEVEL.

2026-05-17 (canary 842-pool deep-probe): ResMan is a sizable cluster
(67 sites in the "other/custom" pool alone, more across the 5k) that
the detector did not recognise, so it fell to LLM/floorplan or failed.

ResMan marketing sites expose a PUBLIC availability portal:
  https://<client>.myresman.com/Portal/Applicants/Availability?a=<acct>&p=<guid>
linked from the property's ``/floorplans/`` page. That page embeds:
  <script>var unitTypes = [ {floorplan-group}, ... ];</script>
Each group: Bedrooms, Bathrooms, Min/MaxSquareFootage, MarketRent,
UnitTypeID, and ``Units: [ {Number, UnitType, Floor, AvailableDate:
"/Date(ms)/", Pricing:[{Rent, Term}]} ]``.

The portal is NOT Cloudflare-fronted (HTTP 200, no challenge, no auth
redirect) — so curl_cffi fetches it fine even from proxy-less Cloud
Run. Deterministic Tier-1, no LLM.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import make_unit_dict
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

_TIER = "TIER_1_API_RESMAN"

# <client>.myresman.com/Portal/Applicants/Availability?a=<acct>&p=<guid>
_RESMAN_AVAIL_RE = re.compile(
    r"https?://[a-z0-9-]+\.myresman\.com/Portal/Applicants/Availability"
    r"\?a=\d+&p=[a-f0-9-]+",
    re.IGNORECASE,
)
_RESMAN_HOST_RE = re.compile(r"https?://([a-z0-9-]+)\.myresman\.com", re.IGNORECASE)
_UNITTYPES_RE = re.compile(r"var\s+unitTypes\s*=\s*(\[)")
_MSDATE_RE = re.compile(r"/Date\((-?\d+)\)/")


def _ms_to_iso(val: Any) -> str:
    """ASP.NET ``/Date(ms)/`` → ISO date string, or '' for sentinels."""
    if not isinstance(val, str):
        return ""
    m = _MSDATE_RE.search(val)
    if not m:
        return ""
    ms = int(m.group(1))
    if ms <= 0:  # /Date(-62135596800000)/ = MinValue sentinel
        return ""
    import datetime as _dt

    try:
        return _dt.datetime.utcfromtimestamp(ms / 1000).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _extract_unittypes(html: str) -> list[dict[str, Any]] | None:
    """Bracket-match the ``var unitTypes = [...]`` JSON array."""
    m = _UNITTYPES_RE.search(html)
    if not m:
        return None
    start = m.start(1)
    depth = 0
    j = start
    n = len(html)
    while j < n:
        c = html[j]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                break
        j += 1
    try:
        data = json.loads(html[start : j + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, list) else None


def parse_resman_unittypes(
    data: list[dict[str, Any]], source_url: str
) -> list[dict[str, Any]]:
    """Floorplan-grouped ResMan ``unitTypes`` → unit-level dicts.

    Emits one row per available Unit (real unit Number + Pricing rent).
    Falls back to a plan-level row (group MarketRent) when a group has
    no available Units but advertises a rent — better than nothing and
    flagged plan-level downstream by post_process.
    """
    units: list[dict[str, Any]] = []
    for g in data:
        if not isinstance(g, dict):
            continue
        beds = g.get("Bedrooms")
        baths = g.get("Bathrooms")
        smin = g.get("MinSquareFootage") or 0
        smax = g.get("MaxSquareFootage") or 0
        sqft = str(smax or smin or "")
        glist = g.get("Units") or []
        if glist:
            for u in glist:
                if not isinstance(u, dict):
                    continue
                pricing = u.get("Pricing") or []
                rent = None
                if pricing and isinstance(pricing[0], dict):
                    rent = pricing[0].get("Rent") or pricing[0].get("TotalRent")
                if rent is None:
                    rent = g.get("MarketRent")
                try:
                    rent_i = int(round(float(rent))) if rent is not None else None
                except (TypeError, ValueError):
                    rent_i = None
                units.append(
                    make_unit_dict(
                        floor_plan_name=str(u.get("UnitType") or "").strip(),
                        bedrooms=str(beds) if beds is not None else "",
                        bathrooms=str(baths) if baths is not None else "",
                        sqft=str(u.get("SquareFootage") or sqft or ""),
                        unit_number=str(u.get("Number") or "").strip(),
                        floor=str(u.get("Floor") or ""),
                        rent_low=rent_i,
                        rent_high=rent_i,
                        availability_status="AVAILABLE",
                        availability_date=_ms_to_iso(u.get("AvailableDate")),
                        source_api_url=source_url,
                        extraction_tier=_TIER,
                    )
                )
        else:
            mr = g.get("MarketRent")
            try:
                mr_i = int(round(float(mr))) if mr else None
            except (TypeError, ValueError):
                mr_i = None
            if mr_i:
                units.append(
                    make_unit_dict(
                        floor_plan_name="",
                        bedrooms=str(beds) if beds is not None else "",
                        bathrooms=str(baths) if baths is not None else "",
                        sqft=sqft,
                        unit_number="",
                        rent_low=mr_i,
                        rent_high=mr_i,
                        availability_status="UNKNOWN",
                        source_api_url=source_url,
                        extraction_tier=_TIER,
                    )
                )
    return units


def find_resman_availability_url(html: str) -> str | None:
    """Return the ResMan Availability portal URL referenced in *html*."""
    if not html:
        return None
    m = _RESMAN_AVAIL_RE.search(html)
    return m.group(0) if m else None


async def _fetch(url: str, ctx: Any = None, stage: str = "resman_fetch") -> str:
    """ResMan portal probe. ctx + stage thread the proxy gate.

    ResMan availability portals are on ``myresman.com/portal/...``
    which is cross-origin to most marketing sites; Layer 4 considers
    via detection confidence + per-property hop budget.
    """
    from ma_poc.pms.adapters._probe import probe_get

    r = probe_get(url, ctx=ctx, stage=stage, timeout=25)
    if r.status_code != 200:
        return ""
    # Auth-redirect (no public availability) → empty.
    if "auth.myresman.com" in str(r.url).lower() or "/account/login" in str(r.url).lower():
        return ""
    return r.text or ""


class ResManAdapter:
    """ResMan ``Portal/Applicants/Availability`` unit-level extractor."""

    pms_name = "resman"

    def __init__(self) -> None:
        self._fingerprints = ["myresman.com", "resman"]

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    def matches_response_body(self, body: Any) -> bool:
        if isinstance(body, str):
            return "var unitTypes" in body and "myresman" in body.lower()
        return False

    async def extract(self, page: "Page", ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER)

        html = ""
        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        if isinstance(body, bytes):
            html = body.decode("utf-8", errors="replace")
        elif isinstance(body, str):
            html = body

        avail = find_resman_availability_url(html)

        # The marketing /floorplans/ page (not the homepage) carries the
        # Availability link. Same pattern as RentCafe-securecafe: the
        # rendered body / captured network log often miss it, so fall
        # back to a curl_cffi homepage+/floorplans/ refetch.
        if not avail:
            for resp in getattr(ctx, "_api_responses", []) or []:
                u = str(resp.get("url", "") or "")
                m = _RESMAN_AVAIL_RE.search(u)
                if m:
                    avail = m.group(0)
                    break
        if not avail:
            origin = ""
            if fr is not None:
                origin = str(getattr(fr, "final_url", "") or "")
            origin = origin or getattr(ctx, "base_url", "") or ""
            if origin:
                from urllib.parse import urlparse

                p = urlparse(origin)
                if p.scheme and p.netloc:
                    base = f"{p.scheme}://{p.netloc}"
                    for cand in (base + "/floorplans/", base + "/", base + "/floor-plans/"):
                        try:
                            hh = await _fetch(cand, ctx=ctx, stage="resman_portal_discover")
                        except Exception:
                            hh = ""
                        avail = find_resman_availability_url(hh)
                        if avail:
                            break

        if not avail:
            result.tier_used = f"{_TIER}_NO_PORTAL"
            result.confidence = 0.0
            result.errors.append("RESMAN: no Availability portal URL discoverable")
            return result

        try:
            ahtml = await _fetch(avail, ctx=ctx, stage="resman_availability")
        except Exception as exc:
            result.tier_used = f"{_TIER}_FETCH_ERROR"
            result.errors.append(f"resman-fetch-error: {type(exc).__name__}: {str(exc)[:120]}")
            return result

        data = _extract_unittypes(ahtml)
        if not data:
            result.tier_used = f"{_TIER}_SHAPE_REJECTED"
            result.errors.append("RESMAN: no var unitTypes JSON in Availability page")
            return result

        raw_units = parse_resman_unittypes(data, avail)
        if not raw_units:
            result.tier_used = f"{_TIER}_EMPTY"
            result.errors.append("RESMAN: unitTypes parsed but produced 0 rows")
            return result

        from ma_poc.extraction.post_process import post_process

        pp = post_process(raw_units, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = avail
            result.confidence = min(0.92, 0.7 + 0.04 * pp.n_admitted)
            result.tier_used = _TIER
            result.api_responses.append(
                {"url": avail, "status": 200, "body": "<resman-availability>", "via": "resman_probe"}
            )
            return result

        result.tier_used = f"{_TIER}_VALIDITY_REJECTED"
        result.errors.append(
            f"RESMAN: {len(raw_units)} rows failed unit_validity"
        )
        return result
