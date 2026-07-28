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

import html as _html
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
_STANDARD_LEASE_TERM_MONTHS = 12


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


def _selected_pricing(pricing: Any) -> tuple[int | None, str]:
    """Return a canonical public ResMan price and its lease-term provenance.

    A ResMan unit contains a matrix of prices by lease term.  The response
    commonly orders short terms first, so blindly reading ``Pricing[0]``
    turns a seven-month price into a false rent mismatch against the public
    12-month selection.  Prefer an explicit 12-month price; when absent,
    choose the numeric term nearest to 12 months.  Rows with no numeric term
    retain their source order as the final fallback.
    """
    if not isinstance(pricing, list):
        return None, ""
    candidates: list[tuple[int | None, int, int]] = []
    for position, item in enumerate(pricing):
        if not isinstance(item, dict):
            continue
        raw_rent = item.get("Rent")
        if raw_rent is None:
            raw_rent = item.get("RentToDisplay") or item.get("TotalRent")
        try:
            rent = int(round(float(raw_rent))) if raw_rent is not None else None
        except (TypeError, ValueError):
            rent = None
        if rent is None or rent <= 0:
            continue
        raw_term = item.get("Term")
        try:
            term = int(round(float(raw_term))) if raw_term is not None else None
        except (TypeError, ValueError):
            term = None
        candidates.append((term, rent, position))
    if not candidates:
        return None, ""
    term, rent, _ = min(
        candidates,
        key=lambda item: (
            0 if item[0] == _STANDARD_LEASE_TERM_MONTHS else 1,
            abs(item[0] - _STANDARD_LEASE_TERM_MONTHS)
            if item[0] is not None
            else 10_000,
            item[2],
        ),
    )
    return rent, str(term) if term is not None else ""


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
                rent_i, lease_term = _selected_pricing(u.get("Pricing"))
                if rent_i is None:
                    try:
                        rent_i = int(round(float(g.get("MarketRent"))))
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
                        lease_term=lease_term,
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
    if m:
        return m.group(0)
    # 2026-07-11 audit: CMS templates HTML-entity-encode the href — Regalia
    # serves ``?a=1450&amp;p=…`` and Bridge/Pine/Barrett serve
    # ``?a&#61;1956&amp;p&#61;…`` (both ``=``→``&#61;`` and ``&``→``&amp;``).
    # The raw regex misses all of them; unescape first, then re-match.
    m = _RESMAN_AVAIL_RE.search(_html.unescape(html))
    return m.group(0) if m else None


async def _fetch(url: str) -> str:
    from ma_poc.pms.adapters._probe import probe_get

    r = probe_get(url, timeout=25)
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

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
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
                            hh = await _fetch(cand)
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
            ahtml = await _fetch(avail)
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
