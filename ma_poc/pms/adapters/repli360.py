"""Repli360 (rrac) PMS adapter — UNIT-LEVEL.

Research log (2026-05-17 Chrome-MCP + curl verification)
--------------------------------------------------------
The "rrac" / caf_v2 popup family (royce-like cluster, 158 properties
across the 5K, ~0 real units in production today — prod falls to
TIER_4_LLM floorplan-level) is a frontend over the Repli360 backend
(repli360 in turn fronts MRI ProspectConnect).

Mechanism (verified on royceattrumbull.com, site_id 1619):
  - The marketing site renders (JS-injected) per-floorplan "View
    Details" anchors whose onclick is literally:
        getUnitListByFloor(this,'A1AL' , 2 , 1619,``);
    i.e. getUnitListByFloor(this, <floorPlanID>, <template_type>,
    <site_id>, ...). site_id is constant per property; floorPlanID
    varies per plan. These attrs are absent from a static curl — they
    require the page to be rendered (the pipeline already renders
    JS-PMS sites).
  - POST https://app.repli360.com/admin/getUnitListByFloor (NO auth,
    NO bot wall — plain server-side POST works with Referer/Origin set
    to the property domain). Confirmed param set:
        floorPlanID, moveinDate ("%-d %b %Y" e.g. "17 May 2026"),
        site_id, template_type=2, mode=apt, type=2d,
        currentanuualterm="", AcademicTerm="", RentalLevel="",
        special=no, zpopUp=""
    (an empty moveinDate / mode returns the empty state — these
    values matter.)
  - Response JSON: {selected_units:[unitnum,...], str:<big HTML>}.
    The unit rows live in ``str`` as ``<tr class="unitlisting ...">``
    with ``data-available_date`` (ISO), ``<b class="unitNumber">``,
    a building ``<td>``, a deposit ``<td>``, and
    ``<span class="unit_price_value">$2,335</span>``.

Verified: royce fp A1AL → 7 real units (4114 Bldg4 $2,335 Available
Now, available_date 2026-05-17, …). Deterministic Tier-1.
"""

from __future__ import annotations

import datetime
import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ma_poc.pms.adapters._parsing import make_unit_dict
from ma_poc.pms.adapters._probe import probe_get, probe_post
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.generic import _get_page_html

if TYPE_CHECKING:
    from playwright.async_api import Page

_TIER = "TIER_1_API_REPLI360"
_API = "https://app.repli360.com/admin/getUnitListByFloor"
_TPL_RENDER = "https://app.repli360.com/admin/template-render"

# The per-property repli360 embed script — present in the property's
# STATIC HTML (no render needed): src=".../admin/rrac-website-script/
# <encrypted-token>". Fetching it yields ``var site_id = '<id>'``.
# 2026-05-22 bucket-B grind: the live URL is
# ``app.repli360.com/public/admin/rrac-website-script/<token>`` — the
# ``/public/`` path segment was absent from the original regex, so
# find_repli360_script_url returned '' and the adapter exited
# REPLI360_NO_FLOORPLANS on every current-variant property.
_SCRIPT_RE = re.compile(
    r"https?://app\.repli360\.com/(?:public/)?admin/rrac-website-script/"
    r"[A-Za-z0-9=_./+-]+",
    re.IGNORECASE,
)
_SITEID_RE = re.compile(
    r"""var\s+site_id\s*=\s*['"](\d{2,9})['"]""", re.IGNORECASE
)

# onclick="getUnitListByFloor(this,'A1AL' , 2 , 1619,``);"
_ONCLICK_RE = re.compile(
    r"getUnitListByFloor\(\s*this\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*"
    r"(\d+)\s*,\s*(\d+)",
    re.IGNORECASE,
)
_MARK_RE = re.compile(
    r"getUnitListByFloor\(|app\.repli360\.com|rrac_listAvailableUnit",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(r"[\d,]+")

# ── Per-unit SQFT (2026-07-28) ──────────────────────────────────────────────
# The getUnitListByFloor availability table has its own "Unit SQFT" column:
#
#   <tr><th>Unit Number</th><th>Unit SQFT</th>…</tr>
#   <tr class="unitlisting …">
#     <td><span class="unitNumberlbl mobile_rrac">Unit Number</span>
#         <b class="unitNumber">0219</b></td>
#     <td><span class="mobile_rrac">Unit SQFT</span><b>932 SQFT</b></td>
#     …
#
# Until this was read, sqft came ONLY from the plan card via
# merge_repli360_plan_meta — and that card writes a RANGE for multi-size
# plans ("<span>932 - 1084</span> sq.ft."), which _SQFT_META_RE (which
# requires a span of pure digits) does not match. Every unit of such a
# plan therefore shipped area=-1 even though its own row said "932 SQFT".
# Measured on the 2026-07-27 reference run: 37 rows across 5 properties.
#
# Column ORDER is not stable across templates (Enclave at Brookside puts
# Deposit/Special/Amenities in different positions and its Deposit cell is
# "$300"), so the cell is located by its per-cell ``span.mobile_rrac``
# LABEL, with a <th> header-index join as fallback. Both then require the
# remaining cell text to be a bare number with an optional sqft unit —
# "$1,485", "Available Now" and "-" can never be mistaken for an area.
_SQFT_LABELS: frozenset[str] = frozenset(
    {
        "sqft",
        "unitsqft",
        "sqfeet",
        "unitsqfeet",
        "squarefeet",
        "unitsquarefeet",
        "squarefootage",
        "unitsquarefootage",
        "size",
        "unitsize",
    }
)
_LABEL_NORM_RE = re.compile(r"[^a-z]")
# The cell, once its label span is removed, must be JUST a number with an
# optional sq-ft unit. Deliberately anchored: a substring search for
# digits would happily read "$1,485" out of the price cell.
_UNIT_SQFT_VALUE_RE = re.compile(
    r"^([\d,]{2,7})\s*(?:sq\.?\s*ft\.?|sqft|sq\.?\s*feet|sf)?$",
    re.IGNORECASE,
)

# Plan-meta extraction (the /admin/template-render HTML carries plan
# name, beds, baths, sqft right next to each getUnitListByFloor onclick;
# the per-unit getUnitListByFloor response has rent + unit_number but
# NO sqft. Walking the template-render HTML once and joining to each
# floorPlanID lifts repli360 from rent-only to rent+sqft+beds — the
# Surgex ≥1-unit-with-rent+sqft success bar).
_WORD_TO_DIGIT = {
    "studio": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
}
_H2_RE = re.compile(r"<h2[^>]*>\s*([^<]+?)\s*</h2>", re.IGNORECASE)
# "<span>670</span> sq.ft." / "<span>670</span> sqft" etc.
_SQFT_META_RE = re.compile(
    r"<span[^>]*>\s*(\d{2,5})\s*</span>\s*sq\.?\s*ft", re.IGNORECASE
)
# "1 Bath" / "2 Bathrooms" / "1.5 Baths"
_BATHS_META_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*Bath(?:room)?s?", re.IGNORECASE
)
# "One Bedroom" / "Two Bedrooms" / "1 Bedroom" / "Studio Bedroom" (rare)
_BEDS_META_RE = re.compile(
    r"\b(Studio|One|Two|Three|Four|Five|Six|\d+)\s+Bed(?:room)?s?",
    re.IGNORECASE,
)
# Standalone "Studio | 1 Bath" — no "Bedroom" word.
_STUDIO_META_RE = re.compile(
    r"\bStudio\b\s*\|\s*\d+(?:\.\d+)?\s*Bath", re.IGNORECASE
)


def _origin_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


def _movein_today() -> str:
    """Repli360 expects the move-in date as ``"%-d %b %Y"`` (no zero pad).

    ``%-d`` is non-portable (fails on Windows); build it explicitly.
    """
    now = datetime.datetime.now(datetime.UTC)
    return f"{now.day} {now.strftime('%b %Y')}"


def find_repli360_floorplans(html: str) -> tuple[str, list[tuple[str, str]]]:
    """Return ``(site_id, [(floorPlanID, template_type), ...])``.

    Parsed from the JS-rendered ``getUnitListByFloor(this,'<fp>',<tt>,
    <sid>)`` onclick attributes. Empty when the page was not rendered
    (the attrs are JS-injected and absent from static HTML) — the
    caller degrades gracefully.
    """
    site_id = ""
    seen: set[str] = set()
    fps: list[tuple[str, str]] = []
    for m in _ONCLICK_RE.finditer(html or ""):
        fpid = m.group(1).strip()
        ttype = m.group(2).strip()
        sid = m.group(3).strip()
        if sid:
            site_id = sid
        if fpid and fpid not in seen:
            seen.add(fpid)
            fps.append((fpid, ttype))
    return site_id, fps


def find_repli360_script_url(html: str) -> str:
    """Return the ``rrac-website-script/<token>`` embed URL, or ''.

    This is in the property's STATIC HTML — it does NOT require the page
    to be rendered (unlike the JS-injected onclick attrs). It is the
    render-independent entry point.
    """
    m = _SCRIPT_RE.search(html or "")
    return m.group(0) if m else ""


def fetch_repli360_site_id(script_url: str, referer: str = "") -> str:
    """GET the embed script, return its ``var site_id = '<id>'``, or ''.

    No auth, no bot wall (verified). Best-effort: any failure → ''.
    """
    if not script_url:
        return ""
    try:
        hdrs = {"Referer": referer} if referer else {}
        r = probe_get(script_url, headers=hdrs, timeout=20)
    except Exception:
        return ""
    if getattr(r, "status_code", 0) != 200:
        return ""
    m = _SITEID_RE.search(r.text or "")
    return m.group(1) if m else ""


def fetch_repli360_template_render(site_id: str, referer: str = "") -> str:
    """POST ``/admin/template-render`` → raw bootstrap widget HTML, or ''.

    The HTML body carries BOTH the per-floorplan ``getUnitListByFloor``
    onclick attrs AND the plan-level metadata (plan name in ``<h2>``,
    "X Bedroom | Y Bath | <span>SQFT</span> sq.ft." spans). One fetch
    serves both :func:`find_repli360_floorplans` and
    :func:`parse_repli360_plan_meta` — avoiding a second round trip.

    No auth / no bot wall (verified). Best-effort: any failure → ''.
    """
    if not site_id:
        return ""
    try:
        origin = _origin_of(referer)
        hdrs = {"Referer": referer or "", "Origin": origin}
        r = probe_post(
            _TPL_RENDER,
            data={
                "site_id": site_id,
                "template_type": "2",
                "action": "",
                "ready_script": "",
                "source": "",
                "property_id": "",
                "zpopUp": "",
            },
            headers=hdrs,
            timeout=25,
        )
    except Exception:
        return ""
    if getattr(r, "status_code", 0) != 200:
        return ""
    return r.text or ""


def fetch_repli360_floorplans(
    site_id: str, referer: str = ""
) -> list[tuple[str, str]]:
    """POST ``/admin/template-render`` → the floorplan list.

    Thin wrapper around :func:`fetch_repli360_template_render` →
    :func:`find_repli360_floorplans`. Kept for back-compat; the adapter
    itself now calls the template-render helper directly so it can also
    extract plan metadata from the same HTML (one fetch, two parses).
    """
    html = fetch_repli360_template_render(site_id, referer)
    if not html:
        return []
    _sid, fps = find_repli360_floorplans(html)
    return fps


def parse_repli360_plan_meta(html: str) -> dict[str, dict[str, str]]:
    """Return ``{floorPlanID: {floor_plan_name, bedrooms, bathrooms, sqft}}``.

    Walks each ``getUnitListByFloor(this,'<fpid>',...)`` onclick in
    ``html``; for each onclick, scans the preceding ~1500 chars for:
      - last ``<h2>`` (plan name, e.g. "1A")
      - last "X Bed[room]s" phrase (or standalone "Studio | … Bath")
      - last "N Bath[room]s" phrase
      - last "<span>SQFT</span> sq.ft." span

    The repli360 ``/admin/template-render`` HTML lays out each plan as:
        <h2>1A</h2>
        <p>One Bedroom | 1 Bath | <span>670</span> sq.ft. | <span>14</span>
           Units Available</p>
        ... <a onclick="getUnitListByFloor(this,'4832490',2,1649,'');">
    so the metadata sits directly before the onclick — a backward window
    join is robust to inter-plan markup variation. Per-floorPlan dedup
    (first occurrence wins) since the same fpid can appear multiple times
    (filter tabs, "View All", etc.) and the first is always the canonical
    listing block.

    Empty html → {}. Onclicks without resolvable meta still get an empty
    dict (caller's ``.get(fpid, {})`` pattern then skips merging).
    """
    if not html:
        return {}
    out: dict[str, dict[str, str]] = {}
    lookback = 1500
    for m in _ONCLICK_RE.finditer(html):
        fpid = m.group(1).strip()
        if not fpid or fpid in out:
            continue
        start = max(0, m.start() - lookback)
        chunk = html[start:m.start()]
        meta: dict[str, str] = {}

        # Find the LAST <h2> in the lookback chunk — this is the current
        # plan's name. Narrow the chunk to "everything after that <h2>"
        # so beds/baths/sqft from a previous plan card (which might still
        # be inside the 1500-char lookback when cards are short) cannot
        # leak in. Without this, the Studio case ("Studio | 1 Bath") on
        # a tight layout picks up the previous card's "Two Bedrooms".
        h2_matches = list(_H2_RE.finditer(chunk))
        if h2_matches:
            last_h2 = h2_matches[-1]
            name = last_h2.group(1).strip()
            if name:
                meta["floor_plan_name"] = name
            chunk = chunk[last_h2.end() :]

        sqft_matches = list(_SQFT_META_RE.finditer(chunk))
        if sqft_matches:
            meta["sqft"] = sqft_matches[-1].group(1)

        bath_matches = list(_BATHS_META_RE.finditer(chunk))
        if bath_matches:
            meta["bathrooms"] = bath_matches[-1].group(1)

        bed_matches = list(_BEDS_META_RE.finditer(chunk))
        if bed_matches:
            word = bed_matches[-1].group(1).lower()
            meta["bedrooms"] = _WORD_TO_DIGIT.get(word, word)
        elif _STUDIO_META_RE.search(chunk):
            # "Studio | 1 Bath | …" with no "Bedroom" word.
            meta["bedrooms"] = "0"

        out[fpid] = meta
    return out


def _is_sqft_label(text: str) -> bool:
    """True when *text* is a column label meaning "square feet".

    Exact-set membership on the letters-only normalisation ("Unit SQFT" →
    "unitsqft"), NOT a substring search. A substring rule is how a fee
    regex once matched "Feet" inside "Square Feet"; matching whole labels
    fails closed (area stays absent) instead of grabbing the wrong cell.
    """
    return _LABEL_NORM_RE.sub("", (text or "").lower()) in _SQFT_LABELS


def _sqft_from_cell(cell_text: str, label_text: str = "") -> str:
    """Return the integer sqft in one table cell, or ''.

    ``label_text`` (the cell's own ``span.mobile_rrac`` label) is stripped
    first; the remainder must be a bare number with an optional sq-ft unit
    — "932 SQFT" → "932". "$1,485", "Available Now", "-" and a
    "932 - 1084" range all return '' rather than a guess.
    """
    txt = (cell_text or "").strip()
    if label_text:
        txt = txt.replace(label_text, "", 1).strip()
    m = _UNIT_SQFT_VALUE_RE.match(txt)
    if not m:
        return ""
    digits = m.group(1).replace(",", "")
    if not digits.isdigit() or not (2 <= len(digits) <= 5):
        return ""
    return digits


def _header_sqft_index(tr: Any) -> int | None:
    """Index of the "Unit SQFT" ``<th>`` in *tr*'s own table, or None.

    Fallback for templates that omit the per-cell ``span.mobile_rrac``
    labels. Column order is not stable across repli360 templates, so the
    index is read from that table's header row rather than assumed.
    """
    try:
        table = tr.find_parent("table")
        if table is None:
            return None
        ths = table.find_all("th")
        for idx, th in enumerate(ths):
            if _is_sqft_label(th.get_text(" ", strip=True)):
                return idx
    except Exception:  # noqa: BLE001 — never raise from a parser
        return None
    return None


def extract_unit_sqft(tr: Any) -> str:
    """Return the per-unit sqft for one ``tr.unitlisting`` row, or ''.

    Reads the row's OWN "Unit SQFT" cell — this is unit-level evidence,
    not a plan-level inference, so it needs no provenance flag. Located by
    the cell's label span first, by the table's ``<th>`` header index
    second. Never raises.
    """
    try:
        tds = tr.find_all("td")
        for td in tds:
            lbl = td.select_one("span.mobile_rrac")
            if lbl is None:
                continue
            label_text = lbl.get_text(" ", strip=True)
            if not _is_sqft_label(label_text):
                continue
            got = _sqft_from_cell(td.get_text(" ", strip=True), label_text)
            if got:
                return got
        idx = _header_sqft_index(tr)
        if idx is not None and 0 <= idx < len(tds):
            td = tds[idx]
            lbl = td.select_one("span.mobile_rrac")
            label_text = lbl.get_text(" ", strip=True) if lbl is not None else ""
            return _sqft_from_cell(td.get_text(" ", strip=True), label_text)
    except Exception:  # noqa: BLE001 — never raise from a parser
        return ""
    return ""


def parse_repli360_str(str_html: str, source_url: str) -> list[dict[str, Any]]:
    """Parse the ``str`` HTML from getUnitListByFloor → unit-level dicts.

    Each ``<tr class="unitlisting ...">`` is one available unit:
    building (1st td), unit number (``b.unitNumber``), deposit,
    ``span.unit_price_value`` rent, availability td, and the row's
    ``data-available_date`` (already ISO ``YYYY-MM-DD``).
    """
    if not str_html:
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(str_html, "lxml")
    out: list[dict[str, Any]] = []
    for tr in soup.select("tr.unitlisting"):
        b = tr.select_one("b.unitNumber")
        unit = b.get_text(strip=True) if b else ""
        if not unit:
            continue
        avail_date = str(tr.get("data-available_date") or "").strip()
        tds = tr.find_all("td")
        building = ""
        if tds:
            building = (
                tds[0].get_text(strip=True).replace("Building Number", "").strip()
            )
        rent: int | None = None
        price_el = tr.select_one("span.unit_price_value")
        if price_el is not None:
            mm = _MONEY_RE.search(price_el.get_text())
            if mm:
                try:
                    rent = int(mm.group(0).replace(",", ""))
                except (TypeError, ValueError):
                    rent = None
        # The row's own "Unit SQFT" cell (2026-07-28). '' when the
        # template has no such column — merge_repli360_plan_meta then
        # falls back to the plan card and stamps the area as derived.
        sqft = extract_unit_sqft(tr)
        # 2026-05-26 fix (#116 residue): getUnitListByFloor only returns
        # available units — unavailable ones are excluded at the API level.
        # Future-dated units show a US-format date ("06-05-2026") in the
        # Availability cell, not "Available Now", so the prior check
        #   "available" in avail_txt.lower()
        # marked ~69% of units (all future-dated ones) as UNKNOWN.
        # All rows returned by this endpoint are AVAILABLE; status should
        # be unconditional. The ISO availability_date (from data-available_date)
        # is the authoritative date signal regardless of the cell text.
        out.append(
            make_unit_dict(
                unit_number=unit,
                building=building,
                sqft=sqft,
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE",  # all getUnitListByFloor rows are available
                availability_date=avail_date,
                source_api_url=source_url,
                extraction_tier=_TIER,
            )
        )
    return out


_AREA_FROM_FP_CARD = "AREA_FROM_FP_CARD"


def _stamp_area_from_plan_card(unit: dict[str, Any]) -> None:
    """Append the ``AREA_FROM_FP_CARD`` provenance flag, once.

    Uses the existing pipe-delimited ``data_quality_flag`` convention (see
    ``pms/scraper.py::_append_quality_flag``) — no parallel mechanism.
    """
    flags = [
        part.strip()
        for part in str(unit.get("data_quality_flag") or "").split("|")
        if part.strip()
    ]
    if _AREA_FROM_FP_CARD not in flags:
        flags.append(_AREA_FROM_FP_CARD)
    unit["data_quality_flag"] = "|".join(flags)


def merge_repli360_plan_meta(
    units: list[dict[str, Any]], meta: dict[str, str]
) -> None:
    """Fill empty plan-level fields on each unit dict from ``meta``.

    In-place mutation. Per-unit values (if already set) WIN; ``meta``
    only ever fills gaps. Skips silently when ``meta`` is empty.

    The getUnitListByFloor per-unit response carries rent + unit_number
    + availability, and (since 2026-07-28) sqft when the availability
    table has a "Unit SQFT" column; the template-render HTML carries
    plan_name/beds/baths/sqft on the plan card. This helper is the join:
    one ``meta`` per floorplan, applied to all the units we got back
    from that floorplan's getUnitListByFloor call.

    PROVENANCE: beds/baths/plan_name are genuine plan attributes, but an
    *area* taken from the plan card is not read off the unit's own row —
    so a unit that gets its sqft here is stamped
    ``data_quality_flag="AREA_FROM_FP_CARD"``. The token deliberately
    avoids the substring "PLAN": ``extraction/post_process.py`` and
    ``core/schema_v2.py`` treat any flag containing "PLAN" as evidence
    the row is a floor-plan placeholder, which would demote a real,
    unit-anchored row.
    """
    if not meta:
        return
    fillable = ("floor_plan_name", "sqft", "bedrooms", "bathrooms")
    for u in units:
        for key in fillable:
            v = meta.get(key)
            if v and not u.get(key):
                u[key] = v
                if key == "sqft":
                    _stamp_area_from_plan_card(u)


class Repli360Adapter:
    """Repli360 / rrac same-domain ``getUnitListByFloor`` extractor."""

    pms_name: str = "repli360"

    def static_fingerprints(self) -> list[str]:
        return ["app.repli360.com", "getUnitListByFloor", "rrac_listAvailableUnit"]

    def matches_response_body(self, body: Any) -> bool:
        if isinstance(body, str):
            return bool(_MARK_RE.search(body))
        return False

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER)

        html = await _get_page_html(page, ctx)
        if not html:
            result.tier_used = f"{_TIER}_NO_HTML"
            result.errors.append("REPLI360: no page html")
            return result

        origin = _origin_of(
            str(getattr(getattr(ctx, "fetch_result", None), "final_url", "") or "")
        ) or _origin_of(getattr(ctx, "base_url", "") or "")
        referer = origin + "/" if origin else ""

        # PRIMARY (render-independent): the embed-script URL is in the
        # STATIC HTML → fetch it → site_id → /admin/template-render →
        # floorplan list. All server-side, no auth, no bot wall, no
        # browser. This is what lifts repli360 past the render cap.
        #
        # 2026-05-22: ONE fetch of /admin/template-render now serves two
        # parses — floorplan list (find_repli360_floorplans) AND plan-
        # level metadata (parse_repli360_plan_meta) for the rent+sqft
        # success bar. The per-unit getUnitListByFloor response carries
        # rent + unit_number but NO sqft/beds/baths/plan_name; without
        # this merge every repli360 property is PLAN_LEVEL-only PARTIAL.
        site_id = ""
        fps: list[tuple[str, str]] = []
        plan_meta: dict[str, dict[str, str]] = {}
        script_url = find_repli360_script_url(html)
        if script_url:
            site_id = fetch_repli360_site_id(script_url, referer)
            if site_id:
                tpl_html = fetch_repli360_template_render(site_id, referer)
                if tpl_html:
                    _sid, fps = find_repli360_floorplans(tpl_html)
                    plan_meta = parse_repli360_plan_meta(tpl_html)

        # FALLBACK: if the static chain didn't resolve (no script tag, or
        # template-render empty), use JS-rendered onclick attrs if the
        # page happened to be rendered. Keeps the prior behaviour; the
        # rendered page also carries the same plan-meta layout so try
        # parsing it too — costs nothing when there's no match.
        if not site_id or not fps:
            r_sid, r_fps = find_repli360_floorplans(html)
            site_id = site_id or r_sid
            fps = fps or r_fps
            if not plan_meta:
                plan_meta = parse_repli360_plan_meta(html)

        if not site_id or not fps:
            result.tier_used = f"{_TIER}_NO_FLOORPLANS"
            result.errors.append(
                "REPLI360: site_id/floorPlanID not resolvable "
                "(static script chain + rendered onclick both empty)"
            )
            return result
        movein = _movein_today()
        all_units: list[dict[str, Any]] = []
        seen_units: set[str] = set()
        for fpid, ttype in fps:
            data = {
                "floorPlanID": fpid,
                "moveinDate": movein,
                "site_id": site_id,
                "template_type": ttype or "2",
                "mode": "apt",
                "type": "2d",
                "currentanuualterm": "",
                "AcademicTerm": "",
                "RentalLevel": "",
                "special": "no",
                "zpopUp": "",
            }
            headers = {"Referer": origin + "/" if origin else "", "Origin": origin}
            try:
                resp = probe_post(_API, data=data, headers=headers, timeout=25)
            except Exception as exc:  # noqa: BLE001 — never raise from an adapter
                result.errors.append(
                    f"repli360-fetch-error[{fpid}]: "
                    f"{type(exc).__name__}: {str(exc)[:100]}"
                )
                continue
            if getattr(resp, "status_code", 0) != 200:
                continue
            try:
                j = json.loads(resp.text or "{}")
            except (json.JSONDecodeError, ValueError):
                continue
            fp_units = parse_repli360_str(str(j.get("str") or ""), _API)
            # Plan-meta join: getUnitListByFloor returns rent +
            # unit_number + availability but NEVER sqft/beds/baths/
            # plan_name. The template-render HTML carries those on the
            # plan card next to the onclick — merge them in. Per-unit
            # values (if any) win; meta only fills gaps.
            merge_repli360_plan_meta(fp_units, plan_meta.get(fpid, {}))
            for u in fp_units:
                key = f"{u.get('unit_number')}|{u.get('building')}"
                if key in seen_units:
                    continue
                seen_units.add(key)
                all_units.append(u)

        result.units = all_units
        result.confidence = 1.0 if all_units else 0.0
        if not all_units:
            result.errors.append("REPLI360: no units parsed from any floorplan")
        return result
