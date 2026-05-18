"""RentManager (iLoveLeasing) PMS adapter — UNIT-LEVEL.

Research log (2026-05-18, server-side curl verified)
-----------------------------------------------------
RentManager marketing sites embed the iLoveLeasing widget
(``www.iloveleasing.com/pub/widget/js/luv.js``) but the real unit feed
is the RentManager **Unit-Availability** ``Search_Result`` endpoint —
clean, NO auth, NO bot wall, server-side curl 200 (repli360/securecafe
class, NOT the JS-only iLoveLeasing widget):

  GET https://<eid>.ua.rentmanager.com/Search_Result?command=
      Search_Result&template=<tmpl>&locations=<loc>&maxperpage=<n>

The full URL is typically present verbatim in the property's STATIC
HTML (e.g. highlandapts.com →
``https://high.ua.rentmanager.com/Search_Result?command=Search_Result&
template=highlandUnit&locations=default&maxperpage=99``). ``<eid>`` is
the RentManager corp id; also derivable from ``<eid>.twa.rentmanager
.com`` / ``<eid>.owa.rentmanager.com``.

Response: a sequence of ``document.write("{ ... }")`` chunks, each a
backtick-delimited pseudo-JSON record:
  { `ppropertyname`:`…`, `pid`:`49`, `unitid`:`18302`, `unit`:`2600`,
    `availabilitydateresult`:`5/31/2023`, `marketrent`:`$4,971.00`, … }
(no beds/baths/sqft in this template). 99/page, ``Page X of N``
pagination — we request a large ``maxperpage`` to pull all in one GET.

Verified: high.ua.rentmanager.com → unit 2600 @ $4,971.00.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ma_poc.pms.adapters._parsing import make_unit_dict
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.generic import _get_page_html

if TYPE_CHECKING:
    from playwright.async_api import Page

_TIER = "TIER_1_API_RENTMANAGER"

# Full Search_Result URL as it appears verbatim in static HTML.
_RM_SEARCH_URL_RE = re.compile(
    r"https?://[a-z0-9-]+\.ua\.rentmanager\.com/Search_Result\?[^\s\"'<>]+",
    re.IGNORECASE,
)
# Fallback: derive the corp id (eid) from any rentmanager subdomain.
_RM_EID_RE = re.compile(
    r"https?://([a-z0-9-]+)\.(?:ua|twa|owa)\.rentmanager\.com",
    re.IGNORECASE,
)
_RM_MARK_RE = re.compile(
    r"\.ua\.rentmanager\.com|\.twa\.rentmanager\.com|cdn\.rentmanager\.com"
    r"|iloveleasing\.com",
    re.IGNORECASE,
)
# One unit record: a { ... } block containing a backtick `unitid` key.
_RM_REC_RE = re.compile(r"\{[^{}]*?`unitid`[^{}]*?\}")
_RM_FIELD_RE = re.compile(r"`([A-Za-z0-9_]+)`\s*:\s*`([^`]*)`")
_MONEY_RE = re.compile(r"[\d,]+")


def _rm_money(v: str) -> int | None:
    m = _MONEY_RE.search(str(v or ""))
    if not m:
        return None
    try:
        return int(round(float(m.group(0).replace(",", ""))))
    except (TypeError, ValueError):
        return None


def _rm_iso(s: str) -> str:
    """``5/31/2023`` | ``2023-05-31`` → ``2023-05-31``; else ''."""
    s = str(s or "").strip()
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.match(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def find_rentmanager_search_url(html: str) -> str:
    """Return a ready-to-call ``ua.rentmanager.com/Search_Result`` URL.

    Prefer the verbatim URL in the static HTML (carries the right
    ``template``/``locations``). Bump ``maxperpage`` to pull every unit
    in one GET (avoids the 99/page pagination). '' when not RentManager.
    """
    m = _RM_SEARCH_URL_RE.search(html or "")
    if not m:
        return ""
    url = m.group(0).replace("&#038;", "&").replace("&amp;", "&").rstrip("\"'")
    if re.search(r"[?&]maxperpage=\d+", url, re.IGNORECASE):
        url = re.sub(r"([?&]maxperpage=)\d+", r"\g<1>9999", url, flags=re.IGNORECASE)
    else:
        url += ("&" if "?" in url else "?") + "maxperpage=9999"
    return url


def parse_rentmanager_search(text: str, url: str) -> list[dict[str, Any]]:
    """Parse the RentManager Search_Result body → unit-level dicts.

    Each ``{ `key`:`val`, … }`` record with a ``unitid`` is one unit:
    ``unit`` = unit number, ``marketrent`` = rent, ``availabilitydate
    result`` = availability date. Deduped by unitid.
    """
    if not text or "`unitid`" not in text:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rm in _RM_REC_RE.finditer(text):
        rec = {
            k.lower(): v.strip()
            for k, v in _RM_FIELD_RE.findall(rm.group(0))
        }
        uid = rec.get("unitid", "").strip()
        unum = rec.get("unit", "").strip() or uid
        key = uid or unum
        if not key or key in seen:
            continue
        seen.add(key)
        rent = _rm_money(rec.get("marketrent", ""))
        out.append(
            make_unit_dict(
                unit_number=unum,
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                availability_date=_rm_iso(rec.get("availabilitydateresult", "")),
                source_api_url=url,
                extraction_tier=_TIER,
            )
        )
    return out


class RentManagerAdapter:
    """RentManager ``ua.rentmanager.com/Search_Result`` extractor.

    Server-side (probe_get), no auth, no bot wall — render-independent.
    """

    pms_name: str = "rentmanager"

    def static_fingerprints(self) -> list[str]:
        return ["ua.rentmanager.com", "twa.rentmanager.com", "iloveleasing.com"]

    def matches_response_body(self, body: Any) -> bool:
        if isinstance(body, str):
            return "`unitid`" in body and "rentmanager" in body.lower()
        return False

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER)
        html = await _get_page_html(page, ctx)
        if not html:
            result.tier_used = f"{_TIER}_NO_HTML"
            result.errors.append("RENTMANAGER: no page html")
            return result

        search_url = find_rentmanager_search_url(html)
        if not search_url:
            # No verbatim Search_Result URL — try to synthesise from eid
            # (template defaults to the common "<eid>Unit").
            me = _RM_EID_RE.search(html)
            if me:
                eid = me.group(1)
                search_url = (
                    f"https://{eid}.ua.rentmanager.com/Search_Result?"
                    f"command=Search_Result&template={eid}Unit"
                    f"&locations=default&maxperpage=9999"
                )
        if not search_url:
            result.tier_used = f"{_TIER}_NO_ENDPOINT"
            result.errors.append("RENTMANAGER: no ua.rentmanager.com Search_Result")
            return result

        try:
            r = probe_get(search_url, timeout=30)
        except Exception as exc:  # noqa: BLE001 — never raise from an adapter
            result.tier_used = f"{_TIER}_FETCH_ERROR"
            result.errors.append(
                f"rentmanager-fetch-error: {type(exc).__name__}: {str(exc)[:100]}"
            )
            return result
        if getattr(r, "status_code", 0) != 200:
            result.tier_used = f"{_TIER}_HTTP_{getattr(r, 'status_code', 0)}"
            return result

        units = parse_rentmanager_search(r.text or "", search_url)
        if not units:
            result.confidence = 0.0
            result.errors.append("RENTMANAGER: no units parsed from Search_Result")
            return result

        from ma_poc.extraction.post_process import post_process

        _pp = post_process(units, property_id=getattr(ctx, "property_id", None))
        if _pp.n_admitted > 0:
            result.units = _pp.admitted
            result.plan_summaries = _pp.plan_summaries
            result.winning_url = search_url
            result.confidence = min(0.92, 0.7 + 0.04 * _pp.n_admitted)
            result.api_responses.append(
                {"url": search_url, "status": 200, "via": "rentmanager_ua_probe"}
            )
            return result
        result.confidence = 0.0
        result.errors.append(
            f"RENTMANAGER_VALIDITY_REJECTED: {len(units)} rows failed unit_validity"
        )
        return result

    def _origin(self, url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""
