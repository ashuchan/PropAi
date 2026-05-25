"""Generic SSR floor-plan DOM fallback (2026-05-19).

The long-tail catcher. For custom-CMS sites the deep probe found
plan-level data is rendered into a repeated container element with the
canonical shape: ``plan name + bd/ba + sqft + $rent + (availability)``,
reachable one labelled nav-hop deep at ``/floorplans``, ``/floor-plans``,
``/availability``, ``/pricing``, ``/apartments``. No shared vendor.

Strategy (deliberately conservative against false positives):

  1. **Scan live DOM** for repeated containers (class name with
     ``plan|floorplan|fp-|unit|listing|card|model|tile|item``) appearing
     2-50 times. If the current page has no such markup, fetch each
     known sub-path via in-session ``fetch`` + ``DOMParser`` and try
     there. First sub-path with a parseable plan grid wins.

  2. **Per-container, require** ≥2 of {bed/bath text, sqft, $ amount},
     plus container text length < 800 chars. This filters navigation,
     footers, image galleries, comparison tables, blog cards, etc.

  3. **Semantic regex parse**, not class-specific:
       - name: first heading text (h1-h4, .name, .title) under the container
       - beds: ``\\bstudio\\b`` → 0, else ``(\\d+)\\s*(bed|bd|br)``
       - baths: ``(\\d+(?:\\.\\d+)?)\\s*(bath|ba)``
       - sqft: ``(\\d[\\d,]*)\\s*(sq|sqft|s.f.|square)``
       - rent: ``\\$(\\d[\\d,]*)(?:\\s*[-–]\\s*\\$?(\\d[\\d,]*))?``
       - avail: "available now", "available <date>", "waitlist", "call",
                "N units available"

  4. **Quality gate**: emit only when ≥2 containers admit. ``post_process``
     downstream still applies the unit-validity check.

Tier: ``TIER_3_DOM_GENERIC`` (plan-level). Lower confidence than the
vendor-specific adapters.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    BATH_RE,
    SQFT_RE,
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ma_poc.pms.adapters.base import AdapterContext

log = logging.getLogger(__name__)

# Sub-paths to probe when the current page has no plan grid. Ordered by
# observed frequency in the 2026-05-19 deep probe (35/46 = 76% under the
# first two paths alone).
_FLOORPLAN_SUBPATHS: tuple[str, ...] = (
    "/floorplans",
    "/floor-plans",
    "/floorplans/",
    "/floor-plans/",
    "/availability",
    "/pricing",
    "/apartments",
    "/units",
)

# Words a "plan-like" class name must contain (anywhere, case-insensitive).
# Deliberately broad — quality is enforced by the per-container content
# checks below, not by class-name filtering.
_PLAN_CLASS_WORDS: tuple[str, ...] = (
    "plan",
    "floorplan",
    "fp-",
    "unit",
    "listing",
    "card",
    "model",
    "tile",
    "item",
)

# Semantic field regexes (operate on container text).
# Bath/sqft delegate to the canonical patterns in _parsing.py so the
# fixes for regressions #13 ("Bathroom" full word) and #16 (ft² / ft2)
# from canary 1ef1060 propagate here without divergence.
_RE_STUDIO = re.compile(r"\bstudio\b", re.IGNORECASE)
_RE_BED = re.compile(r"(\d+)\s*(?:bed|bd|br)\b", re.IGNORECASE)
_RE_BATH = BATH_RE
_RE_SQFT = SQFT_RE
# Rent: $1,234 [- $1,500]. Permissive capture; the post-parse filter
# (value >= 100) rejects "$1 deposit"-style noise.
_RE_RENT = re.compile(r"\$(\d[\d,]*)(?:\s*[-–]\s*\$?(\d[\d,]*))?")
_RE_AVAIL_NOW = re.compile(r"available\s*now", re.IGNORECASE)
_RE_WAITLIST = re.compile(r"waitlist|join\s*the\s*waitlist", re.IGNORECASE)
_RE_UNITS_AVAIL = re.compile(r"(\d+)\s*units?\s*available", re.IGNORECASE)
_RE_CALL = re.compile(r"call\s*(?:for|us)", re.IGNORECASE)


# In-page JS extractor. Returns up to one "best" sub-path's plan cards.
# Each card: {name, text, html_len}. Conservative: containers must
# satisfy class-word match AND ≥2 of (bed/bath, sqft, $) AND text<800.
_GENERIC_DOM_JS = r"""
async () => {
  const TXT = (el) => (el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '');
  const CLASS_WORDS = ["plan", "floorplan", "fp-", "unit", "listing", "card", "model", "tile", "item"];
  const SUBPATHS = ["/floorplans", "/floor-plans", "/floorplans/", "/floor-plans/", "/availability", "/pricing", "/apartments", "/units"];

  const classMatches = (cn) => {
    if (typeof cn !== 'string') return false;
    const c = cn.toLowerCase();
    return CLASS_WORDS.some((w) => c.includes(w));
  };

  const scoreCard = (t) => {
    if (!t || t.length > 800) return 0;
    const hasBed = /\b(?:studio|\d+\s*(?:bed|bd|br))\b/i.test(t);
    const hasBath = /\b\d+(?:\.\d+)?\s*(?:bath|ba)\b/i.test(t);
    const hasSqft = /\b\d[\d,]*\s*(?:sq|sqft|square)/i.test(t);
    const hasDollar = /\$\d{3,}/.test(t);
    return (hasBed?1:0) + (hasBath?1:0) + (hasSqft?1:0) + (hasDollar?1:0);
  };

  const findCardsIn = (doc) => {
    // 1) collect every plan-class-like className that appears in [2, 50] instances
    const freq = new Map();
    doc.querySelectorAll('div,li,article,section').forEach((el) => {
      const cn = typeof el.className === 'string' ? el.className.trim() : '';
      if (!cn || !classMatches(cn)) return;
      freq.set(cn, (freq.get(cn) || 0) + 1);
    });
    const candidates = [...freq.entries()].filter(([, n]) => n >= 2 && n <= 50);
    if (candidates.length === 0) return [];

    // 2) score each candidate by ADMITTED-card count (not raw frequency).
    //    A class with 5 cards that admit beats a class with 48 cards that
    //    all fail the score gate (e.g. Webflow's nested detail containers).
    //    Tiebreaker: admission rate; then raw count (least bad fallback).
    let best = null;
    for (const [cn] of candidates) {
      const containers = Array.from(doc.querySelectorAll('[class]'))
        .filter((el) => typeof el.className === 'string' && el.className === cn);
      const admitted = [];
      for (const c of containers) {
        const t = TXT(c);
        if (scoreCard(t) >= 2) admitted.push({el: c, text: t});
      }
      if (admitted.length === 0) continue;
      const rate = admitted.length / containers.length;
      if (
        !best
        || admitted.length > best.admitted.length
        || (admitted.length === best.admitted.length && rate > best.rate)
      ) {
        best = {cn, admitted, rate};
      }
    }
    if (!best) return [];

    return best.admitted.map(({el, text}) => {
      let name = '';
      const head = el.querySelector('h1, h2, h3, h4, [class*="name"], [class*="title"], [class*="heading"]');
      if (head) name = TXT(head);
      if (!name) name = text.split(/[|•\n]/)[0].slice(0, 60).trim();
      return {name, text, klass: best.cn};
    });
  };

  // 1) try the live document (we may already be on the right page)
  let cards = findCardsIn(document);
  let winningPath = location.pathname;

  // 2) fall back to sub-paths
  if (cards.length < 2) {
    for (const p of SUBPATHS) {
      if (location.pathname.replace(/\/$/, '') === p.replace(/\/$/, '')) continue;
      try {
        const r = await fetch(location.origin + p, {credentials: 'include'});
        if (!r.ok) continue;
        const html = await r.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const found = findCardsIn(doc);
        if (found.length >= 2) { cards = found; winningPath = p; break; }
      } catch (e) { /* next */ }
    }
  }

  // 3) Brand-CMS `/apartments/{state}/{city}/floor-plans` URL discovery.
  // The 2026-05-20 TIER_3_DOM + TIER_MERGED ALL_fail probes (see
  // project_tier3_dom_recovery_2026-05-20.md + project_tier_merged_recovery_
  // 2026-05-20.md) found that 16-18% of failed properties use a
  // multi-property brand template where `/floorplans` returns 404 but
  // floor plans actually live at `/apartments/{state}/{city}/floor-plans`.
  // Brands observed: Lincoln Property Co (Fairways 5, Museum Terrace,
  // Villas Willow Glen, Renaissance), McKinley (Roundtree, Golfside Lake),
  // HG Living (Alcove at Seahurst), MG Properties (Bristol at Sunset).
  // When earlier subpath probes failed, scan landing-page anchor hrefs
  // for the brand-CMS pattern and probe each unique match.
  if (cards.length < 2) {
    // Pattern: /apartments/{state}/{city}[/{property-slug}]/floor-plans
    // 3-segment form: Lincoln, McKinley, Renaissance (state/city/tail)
    // 4-segment form: HG Living (state/city/property-slug/tail)
    const seen = new Set();
    const brandPaths = [];
    document.querySelectorAll('a[href]').forEach((a) => {
      const href = a.getAttribute('href') || '';
      const m = href.match(/^(\/apartments\/[a-z-]+\/[a-z0-9-]+(?:\/[a-z0-9-]+)?\/(?:floor-plans|floorplans))(?:[?#].*)?$/i);
      if (m && !seen.has(m[1])) {
        seen.add(m[1]);
        brandPaths.push(m[1]);
      }
    });
    for (const p of brandPaths) {
      try {
        const r = await fetch(location.origin + p, {credentials: 'include'});
        if (!r.ok) continue;
        const html = await r.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const found = findCardsIn(doc);
        if (found.length >= 2) { cards = found; winningPath = p; break; }
      } catch (e) { /* next */ }
    }
  }

  return {cards, winningPath, count: cards.length};
}
"""


def _parse_card_to_unit(card: dict[str, Any], url: str) -> dict[str, str] | None:
    """Parse a single card text blob into a unit dict via semantic regex."""
    text = str(card.get("text") or "")
    if not text:
        return None
    # Beds — Studio first, else explicit number.
    if _RE_STUDIO.search(text):
        beds: int | None = 0
    else:
        m = _RE_BED.search(text)
        beds = int(m.group(1)) if m else None
    # Baths.
    m = _RE_BATH.search(text)
    baths = m.group(1) if m else ""
    # Sqft.
    m = _RE_SQFT.search(text)
    sqft = m.group(1).replace(",", "") if m else ""
    # Rent (lo/hi).
    m = _RE_RENT.search(text)
    rent_lo: int | None = None
    rent_hi: int | None = None
    if m:
        rent_lo = money_to_int(m.group(1))
        rent_hi = money_to_int(m.group(2)) if m.group(2) else rent_lo
        # Reject sub-$100 "rent" — it's a deposit/fee, not the asking rent.
        if rent_lo is not None and rent_lo < 100:
            rent_lo = None
        if rent_hi is not None and rent_hi < 100:
            rent_hi = None
    # Availability.
    avail_count = ""
    avail_count_m = _RE_UNITS_AVAIL.search(text)
    if avail_count_m:
        avail_count = avail_count_m.group(1)
    if _RE_WAITLIST.search(text):
        status = "UNAVAILABLE"
    elif _RE_CALL.search(text) and rent_lo is None:
        status = "UNAVAILABLE"
    else:
        status = "AVAILABLE"
    # Name.
    name = str(card.get("name") or "").strip()
    # Quality gate: same ≥2-signal criterion the JS scanner uses. Catches
    # navigation summaries / fee tables that have a single $ or single
    # "1 bed" mention with no other dim. Studio counts as a known beds.
    signals = (
        (1 if beds is not None else 0)
        + (1 if baths else 0)
        + (1 if sqft else 0)
        + (1 if rent_lo is not None else 0)
    )
    if signals < 2:
        return None
    if not name and beds is None and rent_lo is None:
        return None
    return make_unit_dict(
        floor_plan_name=name,
        bed_label=bed_label_from(beds, name),
        bedrooms=str(beds) if beds is not None else "",
        bathrooms=str(baths),
        sqft=sqft,
        unit_number="",
        rent_range=format_rent_range(rent_lo, rent_hi),
        rent_low=rent_lo,
        rent_high=rent_hi,
        availability_status=status,
        available_units=avail_count,
        source_api_url=url,
        extraction_tier="TIER_3_DOM_GENERIC",
    )


def parse_generic_floorplan_cards(
    cards: list[dict[str, Any]], url: str
) -> list[dict[str, str]]:
    """Parse the JS extractor's card list into plan-level unit dicts."""
    out: list[dict[str, str]] = []
    for c in cards:
        if not isinstance(c, dict):
            continue
        unit = _parse_card_to_unit(c, url)
        if unit is None:
            continue
        # Final guard: drop cards with neither sqft nor rent (just bd/ba
        # without size/price is probably a navigation summary).
        if not unit["sqft"] and unit["market_rent_low"] is None:
            continue
        out.append(unit)
    return out


async def recover_generic_floorplans(
    page: Page,
    ctx: AdapterContext,
) -> tuple[list[dict[str, str]], str]:
    """Discover repeated plan-card containers + parse them. Returns (units,
    winning_path). Returns ``([], '')`` when no plan grid is discoverable
    (so vendor-specific paths aren't shadowed).

    Two-track strategy (2026-05-24):
      * Track A (live DOM): when ``page.evaluate`` is available, scan the
        rendered DOM via the canonical JS extractor. This catches
        post-JS-injected unit cards (Engrain / SightMap iframes etc.).
      * Track B (static HTML): when Playwright is not available (the
        fetcher is in plain-curl mode after today's GET-path auto-
        escalation), scan the fetched body + sub-paths with BeautifulSoup.
        Catches the TIER_3_DOM P1 cohort properties where prod scanned
        the static HTML but canary's curl_cffi body was never handed to
        any DOM-aware recovery.

    Both tracks delegate to the shared ``parse_generic_floorplan_cards``
    for the per-card text → unit conversion.
    """
    evaluate = getattr(page, "evaluate", None)
    if callable(evaluate):
        try:
            scan = await evaluate(_GENERIC_DOM_JS)
        except Exception as exc:
            log.debug("generic-dom scan failed err=%s", exc)
            scan = None
        # Tolerate string-JSON or dict.
        if isinstance(scan, str):
            try:
                scan = json.loads(scan)
            except json.JSONDecodeError:
                scan = None
        if isinstance(scan, dict):
            cards = scan.get("cards") or []
            winning_path = str(scan.get("winningPath") or "")
            if isinstance(cards, list) and len(cards) >= 2:
                # Build a provenance URL from page.url + the winning sub-path.
                win_url = ""
                try:
                    from urllib.parse import urlparse, urlunparse

                    p = urlparse(page.url or getattr(ctx, "base_url", "") or "")
                    if p.scheme and p.netloc:
                        win_url = urlunparse(
                            (p.scheme, p.netloc, winning_path or p.path, "", "", "")
                        )
                except Exception:
                    win_url = ""

                units = parse_generic_floorplan_cards(cards, win_url)
                if units:
                    return units, winning_path

    # Track B — static HTML fallback (no Playwright required)
    return await _recover_generic_floorplans_static(ctx)


async def _recover_generic_floorplans_static(
    ctx: AdapterContext,
) -> tuple[list[dict[str, str]], str]:
    """Pure-HTML version of the floor-plan card scanner.

    Used when ``page.evaluate`` isn't available — typically when today's
    fetcher GET-path auto-escalation delivers a curl_cffi-fetched body
    that never had a Playwright session attached.

    Algorithm mirrors the JS extractor:
      1. Pick the HTML to scan: prefer ``ctx.fetch_result.body``; if
         that has no plan-like containers, probe the same sub-paths the
         JS scanner walks (``/floorplans``, ``/floor-plans``, etc.)
         and take the first sub-path that yields ≥2 candidate
         containers.
      2. Filter containers by class name (``plan|floorplan|fp-|unit|
         listing|card|model|tile|item``), text length (<800 chars),
         and signal count (≥2 of bed/bath/sqft/rent).
      3. Build dicts in the same shape as the JS extractor's output
         and delegate to ``parse_generic_floorplan_cards``.

    Returns ``([], '')`` on any failure — caller treats as a graceful
    no-op (vendor-specific paths still get tried elsewhere).
    """
    fr = getattr(ctx, "fetch_result", None)
    if fr is None:
        return [], ""
    raw = getattr(fr, "body", None)
    body = ""
    if isinstance(raw, bytes):
        try:
            body = raw.decode("utf-8", "replace")
        except Exception:
            body = ""
    elif isinstance(raw, str):
        body = raw
    if not body:
        return [], ""

    # Resolve a base URL up-front so the provenance is correct whether
    # we extract from the homepage body or a probed sub-path.
    base_url = (
        str(getattr(fr, "final_url", "") or "")
        or str(getattr(ctx, "base_url", "") or "")
    )

    # Try the homepage body first.
    cards = _scan_static_html_for_cards(body)
    winning_path = ""

    # If homepage didn't yield enough, probe known sub-paths.
    if len(cards) < 2:
        try:
            from urllib.parse import urlparse

            from ma_poc.pms.adapters._probe import probe_get

            p = urlparse(base_url)
            if not (p.scheme and p.netloc):
                return [], ""
            origin = f"{p.scheme}://{p.netloc}"

            sub_found = False
            for sub in _FLOORPLAN_SUBPATHS:
                try:
                    r = probe_get(origin + sub, timeout=12)
                except Exception:
                    continue
                if getattr(r, "status_code", 0) != 200:
                    continue
                sub_body = getattr(r, "text", None) or ""
                if not sub_body:
                    continue
                sub_cards = _scan_static_html_for_cards(sub_body)
                if len(sub_cards) >= 2:
                    cards = sub_cards
                    winning_path = sub
                    base_url = origin + sub
                    sub_found = True
                    break
            if not sub_found:
                return [], ""
        except Exception as exc:
            log.debug("generic-dom static sub-probe failed err=%s", exc)
            return [], ""

    if len(cards) < 2:
        return [], winning_path

    units = parse_generic_floorplan_cards(cards, base_url)
    return units, winning_path


def _scan_static_html_for_cards(html: str) -> list[dict[str, Any]]:
    """Scan ``html`` for repeated plan-card-shaped containers.

    Returns a list of dicts in the same shape the JS extractor produces:
        ``[{"text": <container text>, "name": <heading text>}, ...]``

    Empty list when the page has no qualifying containers — caller
    treats as no-op.
    """
    # Guard against trivially small bodies (CF challenge stub is ~5KB
    # but classified as BOT_BLOCKED before reaching us; pure 404/empty
    # bodies are well under 50 chars). 50 is small enough to admit any
    # real plan-card HTML even with minimal chrome.
    if not html or len(html) < 50:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return []

    # Walk every element with a class attribute and bucket by the
    # full class string — the JS scanner does the same (looks for
    # repeated *classes*, not repeated tags). This works because
    # plan-card-shaped containers tend to share a common class
    # name across siblings.
    by_class: dict[str, list[Any]] = {}
    for el in soup.find_all(class_=True):
        classes = el.get("class") or []
        if not classes:
            continue
        class_key = " ".join(classes)
        # Filter: at least one class word must hint "plan-like".
        joined_lower = class_key.lower()
        if not any(w in joined_lower for w in _PLAN_CLASS_WORDS):
            continue
        by_class.setdefault(class_key, []).append(el)

    # Pick the class with the highest count in the 2..50 range.
    # Multiple class strings can match — choose the one whose
    # containers yield the most qualifying cards.
    best_cards: list[dict[str, Any]] = []
    for class_key, elements in by_class.items():
        if not (2 <= len(elements) <= 50):
            continue
        cards: list[dict[str, Any]] = []
        for el in elements:
            text = el.get_text(separator=" ", strip=True)
            if not text or len(text) > 800:
                continue
            # Skip empty containers — must have at least 2 signals
            # (bed/bath/sqft/rent) per the JS scanner's quality gate.
            signals = (
                (1 if _RE_BED.search(text) or _RE_STUDIO.search(text) else 0)
                + (1 if _RE_BATH.search(text) else 0)
                + (1 if _RE_SQFT.search(text) else 0)
                + (1 if _RE_RENT.search(text) else 0)
            )
            if signals < 2:
                continue
            # Heading for the name field.
            name_el = el.find(["h1", "h2", "h3", "h4", "h5"])
            if not name_el:
                # Fall back to .name / .title class
                name_el = el.find(class_=re.compile(r"\bname\b|\btitle\b", re.IGNORECASE))
            name = name_el.get_text(" ", strip=True) if name_el else ""
            cards.append({"text": text, "name": name})
        if len(cards) > len(best_cards):
            best_cards = cards

    return best_cards
