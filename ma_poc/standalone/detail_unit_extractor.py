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
_RENT = re.compile(r"\$\s?[1-9]\d{2,3}(?:\.\d{2})?")
_AVAIL = re.compile(
    r"avail\w*|\bnow\b|\d{1,2}/\d{1,2}/\d{2,4}|move[- ]?in", re.I
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


async def _goto(page: Any, url: str) -> bool:
    try:
        await page.goto(url, wait_until="networkidle", timeout=_NAV_TIMEOUT)
        await page.wait_for_timeout(_SETTLE_MS)
        return True
    except Exception:
        try:
            await page.wait_for_timeout(1500)
            return True
        except Exception:
            return False


async def _phase_a_sightmap(page: Any, url: str, res: PropResult) -> bool:
    """Engrain/SightMap: capture the sightmap API JSON and parse it."""
    html = await _content(page)
    if "sightmap.com" not in html.lower() and "engrain" not in html.lower():
        return False
    # The page already loaded the sightmap iframe/app; its API response
    # carries units. Pull it via an in-page fetch of the embed key.
    m = re.search(r"sightmap\.com/app/api/v1/([a-z0-9_-]+)", html, re.I)
    if not m:
        m = re.search(r"sightmap\.com/embed/([a-z0-9_-]+)", html, re.I)
    if not m:
        res.signal = "sightmap_detected_no_key"
        return False
    key = m.group(1)
    api = f"https://sightmap.com/app/api/v1/{key}"
    try:
        body = await page.evaluate(
            "async (u) => { try { const r = await fetch(u);"
            " return await r.text(); } catch(e){ return ''; } }",
            api,
        )
        from ma_poc.pms.adapters.sightmap import parse_sightmap_payload

        data = json.loads(body) if body else None
        units, _ = parse_sightmap_payload(data, api)
        if units:
            res.units, res.signal, res.phase = units, "sightmap_api", "A_sightmap"
            return True
    except Exception as exc:
        res.error = f"sightmap:{type(exc).__name__}"
    return False


async def _phase_b_url(page: Any, base: str, res: PropResult) -> bool:
    """Discover detail-page links, render+parse each."""
    home = await _content(page)
    links: list[str] = []
    links += find_zrs_detail_links(home, base)
    links += find_entrata_fp_detail_links(home, base)
    for mm in _DETAIL_LINK.finditer(home):
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
        if units:
            res.units, res.signal, res.phase = units, sig, "B_url"
            return True
    return False


async def _phase_c_interact(page: Any, res: PropResult) -> bool:
    """Click view-detail / check-availability / expand controls, parse."""
    try:
        ctrls = await page.evaluate(
            """() => {
              const out=[];
              const els=[...document.querySelectorAll('a,button,[role=button],[onclick]')];
              els.forEach((e,i)=>{ const t=(e.innerText||e.textContent||'').trim();
                if(t && /view\\s*detail|check\\s*availab|see\\s*availab|view\\s*units?|available\\s*units?|see\\s*units|view\\s*pricing/i.test(t))
                  out.push(i); });
              return out.slice(0,8);
            }"""
        )
    except Exception:
        ctrls = []
    if not ctrls:
        return False
    for idx in ctrls[:6]:
        try:
            await page.evaluate(
                """(i)=>{ const els=[...document.querySelectorAll('a,button,[role=button],[onclick]')];
                   if(els[i]) els[i].click(); }""",
                idx,
            )
            await page.wait_for_timeout(2200)
            h = await _content(page)
            units, sig = _proven_parsers(h, page.url)
            if units:
                res.units = units
                res.signal = f"interact:{sig}"
                res.phase = "C_interact"
                return True
        except Exception:
            continue
    return False


def _classify(res: PropResult, home_html: str) -> None:
    real = [
        u for u in res.units
        if str(u.get("unit_number") or "").strip()
        and (u.get("rent_low") or u.get("rent_high"))
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
    try:
        if await _phase_a_sightmap(page, url, res):
            _classify(res, home)
            return res
        if await _phase_b_url(page, base, res):
            _classify(res, home)
            return res
        if not await _goto(page, url):
            pass
        if await _phase_c_interact(page, res):
            _classify(res, home)
            return res
    except Exception as exc:
        res.error = f"{type(exc).__name__}:{str(exc)[:120]}"
    _classify(res, home)
    return res


async def run(urls: list[str], concurrency: int = 6) -> list[PropResult]:
    """Standalone harness: own patchright browser, no pipeline coupling."""
    from patchright.async_api import async_playwright

    results: list[PropResult] = []
    sem = asyncio.Semaphore(concurrency)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        async def one(u: str) -> None:
            async with sem:
                ctx = await browser.new_context()
                page = await ctx.new_page()
                try:
                    r = await asyncio.wait_for(
                        extract_property(page, u), timeout=120
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
