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

from ma_poc.pms.adapters._iloveleasing_table import parse_iloveleasing_table
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
# 2026-07-11 audit: management landing pages expose only the PropertyListing
# widget URL (e.g. high.ua.rentmanager.com/PropertyListing?template=highlandProp)
# — no verbatim Search_Result. The Unit template is the PropertyListing
# template with the trailing 'Prop' swapped for 'Unit' (highlandProp →
# highlandUnit). This beats the eid-synthesis fallback, which wrongly builds
# '<eid>Unit' (highUnit) when the corp id (high) ≠ template prefix (highland).
_RM_PROPLIST_RE = re.compile(
    r"https?://([a-z0-9-]+\.(?:ua|twa|owa)\.rentmanager\.com)/PropertyListing\?"
    r"[^\s\"'<>]*?template=([A-Za-z0-9_]+)",
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


def _derive_search_url_from_property_listing(html: str) -> str:
    """Derive the Unit-Availability Search_Result URL from a PropertyListing
    widget URL: same host (forced to the ``ua.*`` API host), Unit template =
    PropertyListing template with a trailing 'Prop' swapped for 'Unit'.
    Returns '' when no PropertyListing template is present."""
    clean = (html or "").replace("&#038;", "&").replace("&amp;", "&")
    m = _RM_PROPLIST_RE.search(clean)
    if not m:
        return ""
    host, tmpl = m.group(1), m.group(2)
    unit_tmpl = (
        re.sub(r"Prop$", "Unit", tmpl)
        if tmpl.lower().endswith("prop")
        else tmpl + "Unit"
    )
    host = re.sub(r"\.(?:twa|owa)\.", ".ua.", host, flags=re.IGNORECASE)
    return (
        f"https://{host}/Search_Result?command=Search_Result"
        f"&template={unit_tmpl}&locations=default&maxperpage=9999"
    )


def find_rentmanager_search_url(html: str) -> str:
    """Return a ready-to-call ``ua.rentmanager.com/Search_Result`` URL.

    Prefer the verbatim URL in the static HTML (carries the right
    ``template``/``locations``). Bump ``maxperpage`` to pull every unit
    in one GET (avoids the 99/page pagination). Falls back to deriving the
    Unit template from a PropertyListing widget URL (management landing
    pages). '' when not RentManager.
    """
    m = _RM_SEARCH_URL_RE.search(html or "")
    if not m:
        # 2026-07-11 audit: PropertyListing-only landing pages carry no
        # verbatim Search_Result URL — derive it from the PropertyListing
        # template (highlandProp → highlandUnit) before giving up.
        return _derive_search_url_from_property_listing(html)
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


def parse_rentmanager_wp_cards(html: str, source_url: str) -> list[dict[str, Any]]:
    """Parse the WordPress RentManager plugin's availability cards.

    2026-07-12: the KRC/WP RentManager theme renders each available unit as
    ``<a class="individual-item" data-bed data-rent data-date>`` cards inside
    ``.rm-ua-container`` — NOT the ``tr.unit_avail_container`` table that
    ``parse_iloveleasing_table`` targets. So finelivingapts-class properties
    (20 real SSR unit cards, verified) parsed to 0 units and fell through to
    the LLM tier. Fields: data-rent (per-unit rent), data-bed (beds), the
    ``.availableDate`` div (real move-in date), the ``h2`` (floor-plan name +
    ``#unit`` in its span), and ``uid=`` in the detail href (stable id).

    Returns ``[]`` on absent markup / unparseable HTML — never raises.
    """
    if not html or "individual-item" not in html:
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    items = soup.select("a.individual-item")
    units: list[dict[str, Any]] = []
    for it in items:
        href = str(it.get("href") or "")
        rent = _rm_money(it.get("data-rent"))
        beds = it.get("data-bed")

        fp_name = ""
        unit_no = ""
        h2 = it.find("h2")
        if h2 is not None:
            span = h2.find("span")
            if span is not None:
                m = re.search(r"#\s*([A-Za-z0-9-]+)", span.get_text(" ", strip=True))
                if m:
                    unit_no = m.group(1)
                fp_name = h2.get_text(" ", strip=True).replace(
                    span.get_text(" ", strip=True), ""
                ).strip()
            else:
                fp_name = h2.get_text(" ", strip=True)

        avail = ""
        ad = it.find(class_="availableDate")
        if ad is not None:
            dm = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", ad.get_text(" ", strip=True))
            if dm:
                avail = _rm_iso(dm.group(1))
        if not avail and it.get("data-date"):
            dm = re.match(r"(\d{4})/(\d{1,2})", str(it.get("data-date")))
            if dm:
                avail = f"{dm.group(1)}-{int(dm.group(2)):02d}-01"

        bath_m = re.search(r"Bath\s+([\d.]+)", it.get_text(" ", strip=True), re.I)
        baths = bath_m.group(1) if bath_m else ""

        source_ids: dict[str, Any] = {}
        uid_m = re.search(r"uid=(\d+)", href)
        if uid_m:
            source_ids["rentmanager_uid"] = uid_m.group(1)
            if not unit_no:
                unit_no = uid_m.group(1)
        if not unit_no:
            continue

        units.append(
            make_unit_dict(
                floor_plan_name=fp_name,
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths),
                sqft="",
                unit_number=str(unit_no),
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",
                availability_date=avail,
                source_api_url=source_url,
                extraction_tier="TIER_1_DOM_RENTMANAGER_WP_CARDS",
                source_ids=source_ids,
            )
        )
    return units


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

        from ma_poc.extraction.post_process import post_process

        # ── Path A: server-side ua.rentmanager.com/Search_Result API ──────────
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
        if search_url:
            try:
                r = probe_get(search_url, timeout=30)
            except Exception as exc:  # noqa: BLE001 — never raise from an adapter
                result.errors.append(
                    f"rentmanager-fetch-error: {type(exc).__name__}: {str(exc)[:100]}"
                )
                r = None
            if r is not None and getattr(r, "status_code", 0) == 200:
                units = parse_rentmanager_search(r.text or "", search_url)
                if units:
                    _pp = post_process(
                        units, property_id=getattr(ctx, "property_id", None)
                    )
                    if _pp.n_admitted > 0:
                        result.units = _pp.admitted
                        result.plan_summaries = _pp.plan_summaries
                        result.winning_url = search_url
                        result.confidence = min(0.92, 0.7 + 0.04 * _pp.n_admitted)
                        result.api_responses.append(
                            {"url": search_url, "status": 200,
                             "via": "rentmanager_ua_probe"}
                        )
                        return result
                    # 2026-07-11 audit: rent-bearing admission exemption.
                    # Many operators' RentManager Unit templates expose
                    # unit# + market_rent + availability but NO beds/baths/
                    # sqft, so the shared dimension gate rejects every row
                    # (Highland: 1094 parsed / 0 admitted). A Search_Result
                    # record is authoritative Tier-1 operator data — real
                    # unit number + real market rent, deduped by unitid — so
                    # admit rows carrying a unit_number AND a rent even
                    # without dimensions. Scoped to THIS adapter; the shared
                    # validity gate is untouched. Tier is suffixed
                    # _RENT_ONLY and confidence capped so a dimensioned tier,
                    # if one ever exists for the property, still wins.
                    rent_bearing = [
                        u for u in units
                        if str(u.get("unit_number") or "").strip()
                        and (u.get("market_rent_low") or 0)
                    ]
                    if rent_bearing:
                        result.units = rent_bearing
                        result.winning_url = search_url
                        result.tier_used = f"{_TIER}_RENT_ONLY"
                        result.confidence = min(
                            0.85, 0.65 + 0.02 * len(rent_bearing)
                        )
                        result.api_responses.append(
                            {"url": search_url, "status": 200,
                             "via": "rentmanager_ua_probe_rent_only"}
                        )
                        return result
                    result.errors.append(
                        f"RENTMANAGER_VALIDITY_REJECTED: {len(units)} rows "
                        "failed unit_validity"
                    )
                else:
                    result.errors.append(
                        "RENTMANAGER: no units parsed from Search_Result"
                    )
            elif r is not None:
                result.errors.append(
                    f"RENTMANAGER: Search_Result HTTP "
                    f"{getattr(r, 'status_code', 0)}"
                )

        # ── Path B fallback: JS-injected iLoveLeasing availability table ──────
        # (krcapartments-class — render-only; server-side API absent.)
        page_url = str(getattr(ctx, "base_url", "") or "")
        il_units = parse_iloveleasing_table(html, page_url)
        if il_units:
            _pp = post_process(
                il_units, property_id=getattr(ctx, "property_id", None)
            )
            if _pp.n_admitted > 0:
                result.units = _pp.admitted
                result.plan_summaries = _pp.plan_summaries
                result.winning_url = page_url or None
                result.tier_used = "TIER_1_DOM_RENTMANAGER_ILOVELEASING"
                result.confidence = min(0.90, 0.7 + 0.04 * _pp.n_admitted)
                return result
            result.errors.append(
                f"RENTMANAGER_ILOVELEASING_VALIDITY_REJECTED: "
                f"{len(il_units)} rows failed unit_validity"
            )

        # ── Path C: WordPress RentManager plugin availability cards ───────────
        # (finelivingapts-class — <a class="individual-item" data-rent …>
        # cards inside .rm-ua-container that the iLoveLeasing table parser
        # can't see. 19 SSR unit cards verified live w/ rent+beds+date+uid.)
        wp_units = parse_rentmanager_wp_cards(html, page_url)
        if wp_units:
            _pp = post_process(
                wp_units, property_id=getattr(ctx, "property_id", None)
            )
            if _pp.n_admitted > 0:
                result.units = _pp.admitted
                result.plan_summaries = _pp.plan_summaries
                result.winning_url = page_url or None
                result.tier_used = "TIER_1_DOM_RENTMANAGER_WP_CARDS"
                result.confidence = min(0.90, 0.7 + 0.04 * _pp.n_admitted)
                return result
            result.errors.append(
                f"RENTMANAGER_WP_CARDS_VALIDITY_REJECTED: "
                f"{len(wp_units)} cards failed unit_validity"
            )

        if not result.errors:
            result.tier_used = f"{_TIER}_NO_ENDPOINT"
            result.errors.append(
                "RENTMANAGER: no ua.rentmanager.com Search_Result and "
                "no iLoveLeasing table"
            )
        result.confidence = 0.0
        return result

    def _origin(self, url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""
