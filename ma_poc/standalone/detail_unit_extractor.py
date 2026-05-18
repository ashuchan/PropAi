"""Standalone detail/popup unit extractor — the A+B+C "separate solution".

Built 2026-05-17 (canary). NOT wired into the jugnu pipeline — it is a
self-contained module with its OWN patchright browser so it can be
tested on the ~456 "genuine-custom" properties WITHOUT any risk of
regressing the main pipeline (the regression gate concern).

Why standalone: the jugnu pipeline is fetch-then-parse and usually
passes ``page=None`` to adapters, so an adapter cannot drive a live
page to click popups / expand rows. This module owns the page and
drives the interaction the eyeball triage proved is required.

Recovery strategy per property (first hit wins):
  A. Engrain/SightMap embed  → capture the sightmap API → parse.
  B. URL detail pages         → discover /floorplan(s)|/unit|/apartments
                                /<…> links, navigate+render each, run the
                                proven adapter parsers + generic table.
  C. Interaction              → click "view detail" / "Check Availability"
                                / expand "+" controls, wait, parse the
                                now-rendered unit table.
Reuses the already-proven parsers (apts247/spherexx-ZRS/entrata-WP/
securecafe/sightmap). Emits unit-level rows; floorplan-level only when
no unit-level exists (policy: record it, but it counts as FLOORPLAN).
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ma_poc.pms.adapters.apts247 import (
    find_apts247_api_key,
    parse_apts247_floorplans,
)
from ma_poc.pms.adapters.entrata import (
    find_entrata_fp_detail_links,
    parse_entrata_available_units,
)
from ma_poc.pms.adapters.rentcafe import parse_securecafe_availableunits
from ma_poc.pms.adapters.spherexx import (
    find_zrs_detail_links,
    parse_zrs_floorplan_detail,
)

_SETTLE_MS = 2500
_NAV_TIMEOUT = 30000

# Generic per-unit row: a unit/apt/suite token near a $rent near an
# availability cue. Deliberately conservative — only fires on a real
# unit signal, never invents floorplan rows.
_UNIT_TOK = re.compile(
    r"\b(?:apt|unit|suite|apartment|residence)\s*#?\s*[A-Za-z]?\d{1,4}\b", re.I
)
_RENT = re.compile(r"\$\s?[1-9]\d{0,2}(?:,\d{3})?\d{0,3}(?:\.\d{2})?")
_AVAIL = re.compile(
    r"avail\w*|\bnow\b|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|move[- ]?in", re.I
)
_DETAIL_LINK = re.compile(
    r"/(?:floor-?plans?(?:-and-pricing)?|floorplan|floor-plan-cards|"
    r"unit|apartments?)/[a-z0-9][a-z0-9-]*", re.I
)
# Interaction control text (Phase C).
_CLICK_TEXT = re.compile(
    r"view\s*detail|check\s*availab|see\s*availab|view\s*(?:unit|apartment)s?|"
    r"available\s*(?:unit|apartment)s?|see\s*units|view\s*pricing", re.I
)


@dataclass
class PropResult:
    url: str
    klass: str = "NONE"          # UNIT | FLOORPLAN | NONE | DEAD
    phase: str = ""              # A_sightmap | B_url | C_interact | none
    n_units: int = 0
    units: list[dict[str, Any]] = field(default_factory=list)
    signal: str = ""
    error: str = ""


def _origin(u: str) -> str:
    p = urlparse(u if u.startswith("http") else "https://" + u)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


def _generic_unit_rows(html: str, src: str) -> list[dict[str, Any]]:
    """Conservative generic unit-table parser (the recurring shape)."""
    if not html:
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in _UNIT_TOK.finditer(html):
        win = html[m.start(): m.start() + 320]
        rent = _RENT.search(win)
        if not rent or not _AVAIL.search(win):
            continue
        tok = re.sub(r"\s+", " ", m.group(0)).strip()
        if tok.lower() in seen:
            continue
        seen.add(tok.lower())
        try:
            ri = int(round(float(rent.group(0).lstrip("$").replace(",", "").strip())))
        except (TypeError, ValueError):
            ri = None
        rows.append(
            {
                "unit_number": tok,
                "rent_low": ri,
                "rent_high": ri,
                "availability_status": "AVAILABLE",
                "source_api_url": src,
                "extraction_tier": "STANDALONE_GENERIC_UNIT_TABLE",
            }
        )
    return rows


def _proven_parsers(html: str, url: str) -> tuple[list[dict[str, Any]], str]:
    """Run every proven adapter parser; return (units, signal)."""
    try:
        u = parse_entrata_available_units(html, url)
        if u:
            return u, "entrata_available_units"
    except Exception:
        pass
    try:
        u = parse_zrs_floorplan_detail(html, url)
        if u:
            return u, "spherexx_zrs"
    except Exception:
        pass
    try:
        if "AvailUnitRow" in html:
            u = parse_securecafe_availableunits(html, url)
            if u:
                return u, "securecafe"
    except Exception:
        pass
    g = _generic_unit_rows(html, url)
    if g:
        return g, "generic_unit_table"
    return [], ""


async def _content(page: Any) -> str:
    try:
        return await page.content()
    except Exception:
        return ""


async def _text(page: Any) -> str:
    try:
        return await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
    except Exception:
        return ""


# Bare unit-id token as it appears in a table CELL (NOT preceded by a
# 'unit' keyword): "202", "4114", "05-101", "6203", "H0225", "A-204".
_ID_TOK = re.compile(r"^[A-Za-z]{0,2}\d{1,4}(?:[-\s]\d{1,4})?$")
_AVAIL_LINE = re.compile(
    r"available\s*(?:now|soon|\w+\.?\s?\d{0,2}|\d{1,2}[/-]\d{1,2})|"
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|move[- ]?in", re.I
)
_BAD_TOK = re.compile(r"^(?:19|20)\d{2}$")  # years
_FLOOR_WORDS = re.compile(
    r"\b(studio|bed(room)?s?|bath|sq\.?\s?ft|square feet)\b", re.I
)


def _generic_text_rows(text: str, src: str) -> list[dict[str, Any]]:
    """Keyword-free, row/line-based unit-table parser on rendered text.

    The recurring failure: unit numbers in these tables are BARE tokens
    in a column ("202", "118", "05-101", "4114"), not "Unit 202". A
    keyword regex can't read a columnar table. So instead: split the
    rendered innerText into lines; a unit row = a line that has a $rent
    AND an availability cue AND a short discrete id token (and isn't a
    floorplan-summary line). Conservative — under-emits rather than
    fabricates (floorplan-level fallback is acceptable per policy).
    """
    if not text:
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Modal/table text has tabs + newlines INSIDE one logical row
    # (rrac: "4\t4114\t\t$1,000\t\n$2,335\n\tAvailable Now\tLease Now").
    # Normalize ALL whitespace to single spaces FIRST, then split on the
    # row terminators ("Lease Now"/"Apply Now") — not raw \n which
    # fragments a unit across pieces. Fall back to raw-newline split for
    # pages that have neither terminator.
    flat = re.sub(r"\s+", " ", text).strip()
    segs = re.split(r"(?<=Lease Now)|(?<=Apply Now)|(?<=Lease now)", flat)
    if len(segs) < 2:
        segs = re.split(r"[\n\r]+", text)
    for raw in segs:
        line = re.sub(r"\s+", " ", raw).strip()
        if not (8 <= len(line) <= 240):
            continue
        rent = _RENT.search(line)
        if not rent or not _AVAIL_LINE.search(line):
            continue
        # find a discrete id token among the first few whitespace tokens
        rent_digits = rent.group(0).lstrip("$").replace(",", "")
        tok = ""
        for cand in line.split(" ")[:5]:
            c = cand.strip("#:,").strip()
            if not _ID_TOK.match(c) or _BAD_TOK.match(c):
                continue
            if c.replace("-", "").replace(" ", "") == rent_digits:
                continue  # the token IS the rent number
            # on a floorplan-summary line, a bare 3-4 digit is sqft, skip
            if _FLOOR_WORDS.search(line) and re.fullmatch(r"\d{3,4}", c):
                continue
            tok = c
            break
        if not tok or tok.lower() in seen:
            continue
        seen.add(tok.lower())
        try:
            ri = int(round(float(
                rent.group(0).lstrip("$").replace(",", "").strip()
            )))
        except (TypeError, ValueError):
            ri = None
        rows.append(
            {
                "unit_number": tok,
                "market_rent_low": ri,
                "market_rent_high": ri,
                "availability_status": "AVAILABLE",
                "source_api_url": src,
                "extraction_tier": "STANDALONE_GENERIC_TEXT",
            }
        )
    return rows


async def _goto(page: Any, url: str) -> bool:
    # domcontentloaded (NOT networkidle): analytics/pixel-heavy sites
    # never reach networkidle and time out (ironhorse). Settle covers
    # JS hydration of the unit table.
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
        await page.wait_for_timeout(_SETTLE_MS)
        return True
    except Exception:
        try:
            await page.wait_for_timeout(1500)
            return bool(await _content(page))
        except Exception:
            return False


async def _phase_a_sightmap(page: Any, base: str, res: PropResult) -> bool:
    """Engrain/SightMap: trigger the embed, parse captured API responses.

    SightMap embeds as ``sightmap.com/embed/<key>?enable_api=1`` and the
    real unit data arrives as XHR responses from sightmap.com. We let the
    page's response collector capture them (same shape the production
    SightMapAdapter consumes) and reuse ``parse_sightmap_payload``.
    """
    from ma_poc.pms.adapters.sightmap import (
        _is_sightmap_response,
        parse_sightmap_payload,
    )

    captured: list[dict[str, Any]] = getattr(page, "_cap", [])
    # SightMap usually lives on the floorplans page, not home — visit it
    # so the iframe loads and fires its API.
    for path in ("/floorplans/", "/floor-plans/", "/floorplans", ""):
        html = await _content(page)
        if "sightmap.com" in html.lower() or "engrain" in html.lower():
            break
        if not await _goto(page, base.rstrip("/") + path):
            continue
    html = await _content(page)
    if "sightmap.com" not in html.lower() and "engrain" not in html.lower():
        return False
    await page.wait_for_timeout(2500)  # let the iframe XHRs land
    for rsp in list(captured):
        body = rsp.get("body")
        try:
            if _is_sightmap_response(body):
                units, _n = parse_sightmap_payload(body, rsp.get("url", ""))
                if units:
                    res.units = units
                    res.signal = "sightmap_api"
                    res.phase = "A_sightmap"
                    return True
        except Exception:
            continue
    res.signal = "sightmap_detected_no_parseable_response"
    return False


async def _phase_b_url(page: Any, base: str, res: PropResult) -> bool:
    """Discover detail-page links, render+parse each."""
    home = await _content(page)
    links: list[str] = []
    # Harvest detail links from the home AND the floorplans index pages
    # (detail links + the unit table itself often live on /floorplans/,
    # not the homepage — chathamsquare had them on home by luck).
    pages_html = [home]
    for idx in ("/floorplans/", "/floor-plans/", "/floorplans"):
        if await _goto(page, base.rstrip("/") + idx):
            ih = await _content(page)
            if ih and ih not in pages_html:
                pages_html.append(ih)
                # the index page may itself carry the unit table
                u0, s0 = _proven_parsers(ih, base.rstrip("/") + idx)
                if u0:
                    res.units, res.signal, res.phase = u0, s0, "B_url"
                    return True
    for ph in pages_html:
        links += find_zrs_detail_links(ph, base)
        links += find_entrata_fp_detail_links(ph, base)
        for mm in _DETAIL_LINK.finditer(ph):
            p = mm.group(0)
            full = p if p.startswith("http") else base.rstrip("/") + p
            if full not in links:
                links.append(full)
    # apts247 same-origin API (no page nav needed)
    key = find_apts247_api_key(home)
    if key:
        try:
            raw = await page.evaluate(
                "async (u) => { try { return await (await fetch(u)).text(); }"
                " catch(e){ return ''; } }",
                f"{base}/api/v1/floorplans/?api_key={key}",
            )
            d = json.loads(raw) if raw else None
            if isinstance(d, dict):
                u = parse_apts247_floorplans(d, base + "/api/v1/floorplans/")
                if u:
                    res.units, res.signal, res.phase = u, "apts247_api", "B_url"
                    return True
        except Exception:
            pass
    for du in list(dict.fromkeys(links))[:25]:
        if not await _goto(page, du):
            continue
        h = await _content(page)
        units, sig = _proven_parsers(h, du)
        if not units:
            # JS-rendered template detail pages (jaxon/caf_v2): the
            # structured parsers don't match; the unit table is only
            # reliable in rendered innerText.
            units = _generic_text_rows(await _text(page), du)
            sig = "generic_text" if units else ""
        if units:
            res.units, res.signal, res.phase = units, sig, "B_url"
            return True
    return False


async def _phase_c_interact(page: Any, base: str, res: PropResult) -> bool:
    """Click view-detail / check-availability / expand controls, parse.

    The popup/expand controls live on the floorplans index (royce: only
    "VIEW AMENITIES" on the homepage; the per-row "View Details" popup
    is on /floor-plans). So land on the floorplans index first, then
    click the per-row controls and parse the modal/expanded text.
    """
    scan_pages = [
        base.rstrip("/") + p
        for p in ("/floor-plans", "/floor-plans/", "/floorplans/", "/floorplans", "/")
    ]
    # Strict CTA matcher: the popup TRIGGER ("View Details", "Check
    # Availability", "(N) Available", "See Units"), NOT bare status
    # labels like "Available Now" (which clicked nothing useful and was
    # the royce bug). The selector list is broad; the regex is strict.
    _CTRL_JS = (
        """() => {
          const out=[];
          const els=[...document.querySelectorAll('a,button,[role=button],[onclick],[class*=detail],[class*=availab]')];
          els.forEach((e,i)=>{ let t=((e.innerText||e.textContent||'')+' '+((e.getAttribute&&e.getAttribute('aria-label'))||'')).replace(/\\s+/g,' ').trim();
            if(!t) return;
            if(/view\\s*detail|^details?\\b|check\\s*availab|see\\s*(?:unit|apartment)s?|view\\s*(?:unit|apartment)s?|\\(\\s*\\d+\\s*\\)\\s*available|select\\s*(?:floor\\s*)?plan/i.test(t)
               && !/^available(\\s*now)?$/i.test(t))
              out.push(i); });
          return out.slice(0,14);
        }"""
    )
    for sp in scan_pages:
        if not await _goto(page, sp):
            continue
        # caf_v2 / rrac widget: trigger = <a class="btn btn-primary">
        # "View Details" (one per floorplan); click -> async-populates
        # .rrac_apartment_details_content with the Bldg#/Unit#/$/avail
        # table. Poll the container (rrac loads slowly), parse ITS text.
        # rrac loads ASYNC — must poll for the widget + the View-Details
        # anchors to render BEFORE scanning (this pre-poll is exactly
        # why the Chrome-MCP probe succeeded where blind scans failed).
        has_rrac = False
        n = 0
        for _ in range(28):  # up to ~14s (concurrency slows render)
            try:
                n = await page.evaluate(
                    """() => {
                      if(!document.querySelector('[class*=rrac]')) return -1;
                      return [...document.querySelectorAll('a,button')]
                        .filter(e=>/view\\s*detail/i.test((e.innerText||'').trim())
                        && (e.innerText||'').trim().length<25).length; }"""
                )
            except Exception:
                n = -1
            if n and n > 0:
                has_rrac = True
                break
            if n == -1 and _ >= 6:
                break  # no rrac widget at all — bail early
            await page.wait_for_timeout(500)
        if has_rrac:
            acc_r: list[dict[str, Any]] = []
            seen_r: set[str] = set()
            # Cap to a few floorplans: we need to CLASSIFY (units exist?),
            # not exhaust all popups. 14×10s polls blew the 120s cap.
            for vi in range(min(int(n or 0), 4)):
                try:
                    await page.evaluate(
                        """(vi)=>{ const vd=[...document.querySelectorAll('a,button')]
                            .filter(e=>/view\\s*detail/i.test((e.innerText||'').trim())
                            && (e.innerText||'').trim().length<25);
                          if(vd[vi]){ vd[vi].scrollIntoView(); vd[vi].click(); } }""",
                        vi,
                    )
                    txt = ""
                    for _ in range(16):  # poll up to ~8s for async modal
                        await page.wait_for_timeout(500)
                        txt = await page.evaluate(
                            """()=>{ const m=document.querySelector(
                              '.rrac_apartment_details_content,.rrac_modal_container,[class*=rrac-popup]');
                              return m ? (m.innerText||'').trim() : ''; }"""
                        )
                        if len(txt) > 60:
                            break
                    for u in _generic_text_rows(txt, page.url):
                        k = str(u.get("unit_number") or "") + str(
                            u.get("market_rent_low") or ""
                        )
                        if k and k not in seen_r:
                            seen_r.add(k)
                            acc_r.append(u)
                except Exception:
                    continue
            if acc_r:
                res.units = acc_r
                res.signal = "interact:rrac_popup"
                res.phase = "C_interact"
                return True
        try:
            ctrls = await page.evaluate(_CTRL_JS)
        except Exception:
            ctrls = []
        if not ctrls:
            continue
        acc: list[dict[str, Any]] = []
        seen_u: set[str] = set()
        for idx in ctrls[:12]:
            try:
                await page.evaluate(
                    """(i)=>{ const els=[...document.querySelectorAll('a,button,[role=button],[onclick],[class*=detail],[class*=availab]')];
                       if(els[i]) els[i].click(); }""",
                    idx,
                )
                await page.wait_for_timeout(2400)
                h = await _content(page)
                units, _ = _proven_parsers(h, page.url)
                if not units:
                    units = _generic_text_rows(await _text(page), page.url)
                for u in units:
                    k = str(u.get("unit_number") or "") + str(
                        u.get("market_rent_low") or u.get("rent_low") or ""
                    )
                    if k and k not in seen_u:
                        seen_u.add(k)
                        acc.append(u)
            except Exception:
                continue
        if acc:
            res.units = acc
            res.signal = "interact:popup_accumulated"
            res.phase = "C_interact"
            return True
    return False


_PORTAL_LINK = re.compile(
    r"online-leasing|/content/apply|check.?availab|apply\s*now|"
    r"onesite\.realpage\.com|leasing\.realpage\.com|doorway\.knck\.io|#k=",
    re.I,
)


_ONESITE_ROW = re.compile(
    r"#\s*([0-9][0-9A-Za-z]{1,8}(?:\s+[A-Z]{1,3})?)\s+"      # unit id "#1709C AB"
    r"\$\s?([\d,]+)(?:\s*-\s*\$\s?([\d,]+))?\*?\s+"           # $lo[ - $hi]*
    r"(Available\s+Now|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[a-z]*\.?\s+\d{1,2},?\s+\d{4})",                         # avail
    re.I,
)


def _parse_onesite_rows(text: str, src: str) -> list[dict[str, Any]]:
    """Parse a RealPage OneSite step-2 'Select Apartment' table.

    Rows render as: ``#1709C AB  $1,155 - $1,415*  Available Now``.
    Whitespace is normalized first (the frame innerText has tabs/
    newlines inside a logical row, same hazard as the rrac modal).
    """
    flat = re.sub(r"\s+", " ", text or "").strip()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in _ONESITE_ROW.finditer(flat):
        uid = m.group(1).strip()
        if not uid or uid.lower() in seen:
            continue
        seen.add(uid.lower())
        try:
            lo = int(m.group(2).replace(",", ""))
        except (TypeError, ValueError):
            lo = None
        try:
            hi = int(m.group(3).replace(",", "")) if m.group(3) else lo
        except (TypeError, ValueError):
            hi = lo
        out.append(
            {
                "unit_number": uid,
                "market_rent_low": lo,
                "market_rent_high": hi,
                "availability_status": "AVAILABLE",
                "source_api_url": src,
                "extraction_tier": "STANDALONE_ONESITE",
            }
        )
    return out


async def _realpage_frame(page: Any) -> Any:
    """Pick the cross-origin RealPage OneSite wizard frame.

    iframe#rp-leasing-widget's src is set via JS to a
    *.onesite.realpage.com URL, but it can be blank/srcdoc early, so
    URL detection alone is brittle. Try URL/name first, then fall back
    to content-sniffing for the wizard's step labels / "(N) Available".
    """
    frames = list(getattr(page, "frames", []) or [])
    for f in frames:
        try:
            u = (f.url or "").lower()
        except Exception:
            u = ""
        if "onesite.realpage" in u or "leasing.realpage" in u:
            return f
    for f in frames:
        try:
            t = await f.evaluate("()=>document.body?document.body.innerText:''")
        except Exception:
            t = ""
        if t and re.search(
            r"select\s+floor\s+plan|select\s+apartment|\(\s*\d+\s*\)\s*available",
            t, re.I,
        ):
            return f
    return None


async def _phase_d_portal_hop(page: Any, base: str, res: PropResult) -> bool:
    """OneSite / Knock #k=-hop (6th path).

    caf_v2 sites expose a "Check Availability"/"Apply"/online-leasing
    link or a /content/apply#k= / /online-leasing#k= URL that hops to
    RealPage OneSite (*.onesite.realpage.com / leasing.realpage.com) or
    Knock (doorway.knck.io). Those portals render "(N) Available -> units"
    cards (OneSite OLL) or a unit list (Knock). Follow the link, then
    reuse the interaction + generic-text + proven-parser machinery on
    the portal page.
    """
    for sp in (base.rstrip("/") + "/floor-plans", base.rstrip("/") + "/", base.rstrip("/") + "/floorplans/"):
        if not await _goto(page, sp):
            continue
        try:
            href = await page.evaluate(
                """() => {
                  const els=[...document.querySelectorAll('a[href]')];
                  for (const e of els){ const h=e.href||''; const t=(e.innerText||'').trim();
                    if(/online-leasing|\\/content\\/apply|onesite\\.realpage|leasing\\.realpage|doorway\\.knck|#k=/i.test(h)
                       || /check\\s*availab|apply\\s*now|online\\s*leasing/i.test(t)) return h; }
                  return ''; }"""
            )
        except Exception:
            href = ""
        if not href:
            continue
        if not await _goto(page, href):
            continue
        # The RealPage OneSite wizard is an SPA in iframe#rp-leasing-widget
        # (same-origin, hash-routed #!/loading → #!/floorplans). Headless
        # it is VERY slow: >23s to leave "Loading...", longer to reach
        # "Select Floor Plan". _realpage_frame content-sniffs for the
        # real wizard text (NOT "Loading..."), so poll long (~50s).
        fr = None
        for _ in range(100):  # up to ~50s for the wizard to populate
            await page.wait_for_timeout(500)
            try:
                fr = await _realpage_frame(page)
            except Exception:
                fr = None
            if fr is not None:
                break
        if fr is None:
            # No OneSite frame — fall back to the old top-document scan
            # (Knock/other portals that DON'T use the rp iframe).
            try:
                u_top = _generic_text_rows(await _text(page), page.url)
            except Exception:
                u_top = []
            if u_top:
                res.units = u_top
                res.signal = "portal_hop:top_text"
                res.phase = "D_portal_hop"
                return True
            continue
        # Step 1 "Select Floor Plan": each FP card has a "(N) Available"
        # button. Click each → step 2 "Select Apartment" table
        # (#unit | $lo - $hi* | avail). Parse, go Back, repeat.
        acc: list[dict[str, Any]] = []
        seen_o: set[str] = set()
        for ci in range(12):
            try:
                clicked = await fr.evaluate(
                    """(ci)=>{ const b=[...document.querySelectorAll('a,button,[role=button]')]
                        .filter(e=>/\\(\\s*\\d+\\s*\\)\\s*available/i.test((e.innerText||'').trim()));
                      if(!b[ci]) return -1; b[ci].scrollIntoView(); b[ci].click(); return b.length; }""",
                    ci,
                )
            except Exception:
                clicked = -1
            if clicked == -1:
                break
            txt = ""
            for _ in range(16):  # poll up to ~8s for step-2 table
                await page.wait_for_timeout(500)
                try:
                    txt = await fr.evaluate("()=>document.body.innerText||''")
                except Exception:
                    txt = ""
                if _ONESITE_ROW.search(re.sub(r"\s+", " ", txt or "")):
                    break
            for u in _parse_onesite_rows(txt, page.url):
                k = str(u.get("unit_number") or "")
                if k and k not in seen_o:
                    seen_o.add(k)
                    acc.append(u)
            # Back to step 1 for the next floorplan card.
            try:
                await fr.evaluate(
                    """()=>{ const bk=[...document.querySelectorAll('a,button,[role=button]')]
                        .find(e=>/^back$/i.test((e.innerText||'').trim()));
                      if(bk) bk.click(); }"""
                )
                await page.wait_for_timeout(1500)
            except Exception:
                pass
        if acc:
            res.units = acc
            res.signal = "portal_hop:onesite_wizard"
            res.phase = "D_portal_hop"
            return True
    return False


def _has_rent(u: dict[str, Any]) -> bool:
    # make_unit_dict (used by every proven parser) emits market_rent_*,
    # NOT rent_*. Accept both so parsed units aren't silently discarded.
    return bool(
        u.get("market_rent_low") or u.get("market_rent_high")
        or u.get("rent_low") or u.get("rent_high")
    )


def _classify(res: PropResult, home_html: str) -> None:
    real = [
        u for u in res.units
        if str(u.get("unit_number") or "").strip() and _has_rent(u)
    ]
    if real:
        res.klass = "UNIT"
        res.n_units = len(real)
        return
    # floorplan-level fallback: page advertises a $ price but no units
    if home_html and _RENT.search(home_html):
        res.klass = "FLOORPLAN"
    else:
        res.klass = "NONE"


async def extract_property(page: Any, url: str) -> PropResult:
    res = PropResult(url=url)
    base = _origin(url)
    if not base:
        res.klass = "DEAD"
        res.error = "bad-url"
        return res
    if not await _goto(page, url):
        res.klass = "DEAD"
        res.error = "home-nav-failed"
        return res
    home = await _content(page)
    low = home.lower()
    # EARLY SIGNATURE ROUTING — jump straight to the right phase so the
    # time budget isn't wasted running irrelevant phases (royce/lochraven
    # timed out doing A+B before reaching the rrac/portal path).
    is_sightmap = "sightmap.com" in low or "engrain" in low
    is_cafv2 = ("/community/version2" in low or "rrac" in low
                or "visitdata-" in low)
    is_portal = bool(_PORTAL_LINK.search(home))
    try:
        order = []
        if is_sightmap:
            order = [_phase_a_sightmap, _phase_c_interact, _phase_b_url, _phase_d_portal_hop]
        elif is_cafv2:
            order = [_phase_c_interact, _phase_d_portal_hop, _phase_b_url, _phase_a_sightmap]
        elif is_portal:
            order = [_phase_d_portal_hop, _phase_c_interact, _phase_b_url, _phase_a_sightmap]
        else:
            order = [_phase_b_url, _phase_a_sightmap, _phase_c_interact, _phase_d_portal_hop]
        for ph in order:
            await _goto(page, url)  # restore home between phases
            if await ph(page, base, res):
                _classify(res, home)
                return res
    except Exception as exc:
        res.error = f"{type(exc).__name__}:{str(exc)[:120]}"
    _classify(res, home)
    return res


def _proxy_ctx_opts() -> dict[str, Any]:
    """Parse PROXY_POOL_URLS (first entry) into patchright context opts.

    Empty/unset → {} so local runs are unchanged (no proxy, no regression).
    On Cloud Run the datacenter egress IP is bot-blocked by caf_v2/RealPage;
    BrightData residential proxy is required for a representative measurement.
    Mirrors browser_pool.py's str-proxy branch.
    """
    import os
    from urllib.parse import urlparse

    raw = os.getenv("PROXY_POOL_URLS", "").split(",")
    raw = [u.strip() for u in raw if u.strip()]
    if not raw:
        return {}
    p = urlparse(raw[0])
    port = f":{p.port}" if p.port else ""
    pw_proxy: dict[str, str] = {"server": f"{p.scheme}://{p.hostname}{port}"}
    if p.username:
        pw_proxy["username"] = p.username
    if p.password:
        pw_proxy["password"] = p.password
    # BrightData terminates TLS at the proxy edge — without this Chromium
    # aborts HTTPS navigations with ERR_CERT_AUTHORITY_INVALID.
    return {"proxy": pw_proxy, "ignore_https_errors": True}


async def run(urls: list[str], concurrency: int = 6) -> list[PropResult]:
    """Standalone harness: own patchright browser, no pipeline coupling."""
    import os

    from patchright.async_api import async_playwright

    results: list[PropResult] = []
    sem = asyncio.Semaphore(concurrency)
    ctx_opts = _proxy_ctx_opts()
    # Per-URL wall-clock cap. Env-driven so a slower no-proxy multi-phase
    # run can be given more budget without an image rebuild.
    _per_url_timeout = float(os.getenv("PER_URL_TIMEOUT", "200"))
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        async def one(u: str) -> None:
            async with sem:
                ctx = await browser.new_context(**ctx_opts)
                page = await ctx.new_page()
                cap: list[dict[str, Any]] = []
                page._cap = cap  # type: ignore[attr-defined]

                async def _on_resp(resp: Any) -> None:
                    try:
                        u = resp.url
                        if "sightmap" not in u.lower():
                            return
                        ct = (resp.headers or {}).get("content-type", "")
                        if "json" not in ct.lower():
                            return
                        cap.append({"url": u, "body": await resp.json()})
                    except Exception:
                        pass

                page.on("response", lambda r: asyncio.create_task(_on_resp(r)))
                try:
                    r = await asyncio.wait_for(
                        extract_property(page, u), timeout=_per_url_timeout
                    )
                except Exception as exc:
                    r = PropResult(url=u, klass="DEAD", error=f"timeout/{exc}")
                results.append(r)
                try:
                    await ctx.close()
                except Exception:
                    pass

        await asyncio.gather(*(one(u) for u in urls), return_exceptions=True)
        await browser.close()
    return results


if __name__ == "__main__":
    import sys

    src = sys.argv[1] if len(sys.argv) > 1 else "-"
    raw = (open(src).read() if src != "-" else sys.stdin.read()).split()
    out = asyncio.run(run(raw))
    from collections import Counter

    c = Counter(r.klass for r in out)
    for r in out:
        print(
            json.dumps(
                {
                    "url": r.url, "klass": r.klass, "phase": r.phase,
                    "n": r.n_units, "sig": r.signal, "err": r.error,
                }
            )
        )
    print("SUMMARY", dict(c), file=sys.stderr)
