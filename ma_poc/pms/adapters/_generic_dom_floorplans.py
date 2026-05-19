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
_RE_STUDIO = re.compile(r"\bstudio\b", re.IGNORECASE)
_RE_BED = re.compile(r"(\d+)\s*(?:bed|bd|br)\b", re.IGNORECASE)
_RE_BATH = re.compile(r"(\d+(?:\.\d+)?)\s*(?:bath|ba)\b", re.IGNORECASE)
_RE_SQFT = re.compile(r"(\d[\d,]*)\s*(?:sq\.?\s?ft|sqft|s\.f\.|square\s*f(?:ee|oo)t)", re.IGNORECASE)
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

  const findCardsIn = (doc) => {
    // group sibling-of-same-class containers, count >=2 and <=50
    const freq = new Map();
    doc.querySelectorAll('div,li,article,section').forEach((el) => {
      const cn = typeof el.className === 'string' ? el.className.trim() : '';
      if (!cn || !classMatches(cn)) return;
      freq.set(cn, (freq.get(cn) || 0) + 1);
    });
    // pick the class with the most matches in the [2,50] sweet spot
    let bestClass = null, bestCount = 0;
    freq.forEach((n, c) => { if (n >= 2 && n <= 50 && n > bestCount) { bestClass = c; bestCount = n; } });
    if (!bestClass) return [];
    const containers = Array.from(doc.querySelectorAll('[class]'))
      .filter((el) => typeof el.className === 'string' && el.className === bestClass);

    const cards = [];
    for (const c of containers) {
      const t = TXT(c);
      if (!t || t.length > 800) continue;
      const hasBed = /\b(?:studio|\d+\s*(?:bed|bd|br))\b/i.test(t);
      const hasBath = /\b\d+(?:\.\d+)?\s*(?:bath|ba)\b/i.test(t);
      const hasSqft = /\b\d[\d,]*\s*(?:sq|sqft|square)/i.test(t);
      const hasDollar = /\$\d{3,}/.test(t);
      const score = (hasBed?1:0) + (hasBath?1:0) + (hasSqft?1:0) + (hasDollar?1:0);
      if (score < 2) continue;
      // grab "name" candidate: first heading or .name/.title under container
      let name = '';
      const head = c.querySelector('h1, h2, h3, h4, [class*="name"], [class*="title"], [class*="heading"]');
      if (head) name = TXT(head);
      if (!name) name = t.split(/[|•\n]/)[0].slice(0, 60).trim();
      cards.push({name, text: t, klass: bestClass});
    }
    return cards;
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
    """
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return [], ""
    try:
        scan = await evaluate(_GENERIC_DOM_JS)
    except Exception as exc:
        log.debug("generic-dom scan failed err=%s", exc)
        return [], ""
    # Tolerate string-JSON or dict.
    if isinstance(scan, str):
        try:
            scan = json.loads(scan)
        except json.JSONDecodeError:
            return [], ""
    if not isinstance(scan, dict):
        return [], ""
    cards = scan.get("cards") or []
    winning_path = str(scan.get("winningPath") or "")
    if not isinstance(cards, list) or len(cards) < 2:
        return [], winning_path

    # Build a provenance URL from page.url + the winning sub-path.
    win_url = ""
    try:
        from urllib.parse import urlparse, urlunparse

        p = urlparse(page.url or getattr(ctx, "base_url", "") or "")
        if p.scheme and p.netloc:
            win_url = urlunparse((p.scheme, p.netloc, winning_path or p.path, "", "", ""))
    except Exception:
        win_url = ""

    units = parse_generic_floorplan_cards(cards, win_url)
    return units, winning_path
