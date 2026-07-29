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

  5. **Scope gate** (2026-07-29): reject cards that describe a *different
     property*. See below.

Tier: ``TIER_3_DOM_GENERIC`` (plan-level). Lower confidence than the
vendor-specific adapters.

Scope defect (2026-07-29 — canary run-2026-07-27-full-0d54ca7)
--------------------------------------------------------------
Riverwalk on the Falls (canonical_id 71534,
``wimmercommunities.com/apartments/menomonee-falls/riverwalk-on-the-falls/``)
shipped 33 units with ``SUCCESS_PLAN_LEVEL``. Every ``floor_plan_name`` was
a *sibling Wimmer community* — "Oakton Beach Apartments", "Falcon Glen",
"Forest Ridge Senior Community" — not a floor plan of Riverwalk.

Two independent bugs combined:

**(a) origin-rooted sub-path probing.** The sub-path walk fetched
``location.origin + p``. On a management-company site the property lives at
a *deep* path, so ``origin + "/apartments"`` is the company-wide community
directory — the parent index, not this property. Live-reproduced: that URL
returns cards of class ``property-card-info`` whose anchors point at
``/apartments/wi/brookfield/poplar-creek-town-center`` etc. Every card in
the winning set belonged to a different property. Fixed by probing the
property's **own path subtree first** (``/apartments/menomonee-falls/
riverwalk-on-the-falls/floorplans``) and only then falling back to the
origin-rooted paths.

**(b) no scope check on admitted cards.** The scanner picks the repeated
class with the most admitted cards; a directory grid (bed/bath + sqft + $
from a "starting at" summary) satisfies every content gate, so it
outscores or substitutes for the real plan grid. Fixed by
``filter_cards_by_scope``: a card whose anchors leave the property's own
URL subtree is off-scope, and a card set that is majority-off-scope is
rejected outright. On the Riverwalk directory grid this rejects all 33.

The guard **fails open** — no anchors, or a different host, is NEUTRAL, so
genuine plan grids (whose cards link to plan details under the property, or
carry no link at all) are unaffected. Only positive evidence that a card
points at *another* property rejects it.
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

# ── Scope guard (2026-07-29) ─────────────────────────────────────────────
# Reject cards that describe a DIFFERENT property. See the module docstring
# for the Riverwalk-on-the-Falls incident this closes.

# Schemes that are never a property-scope signal.
_NON_NAV_HREF = re.compile(r"^\s*(?:#|tel:|mailto:|sms:|javascript:|data:)", re.I)

# Path segments that mark a link as a PLAN / UNIT / LISTING detail page
# rather than another property's landing page.
#
# This distinction is load-bearing, and measured rather than assumed. A
# naive "any link outside the property's path subtree is off-scope" rule
# rejects 118/757 card sets in run-2026-07-27-full-0d54ca7, because a deep
# landing URL does NOT imply a multi-property site: single-property sites
# routinely put plans on an unrelated branch —
#   /Home.aspx            → /phoenix/bridge-lane.../floorplans/studio-1154964/
#   /wray-north-dallas-tx → /floorplans/one-bedroom/a1
#   /property/cottages-on-tazewell → /listings/detail/{uuid}   (AppFolio)
#   /apartments/cortland-at-raven  → …/available-apartments/1146/pricing/
# All four are this property's own data reached by an off-subtree link.
#
# A sibling-community card, by contrast, links to a bare property landing
# page carrying none of these markers:
#   /apartments/wi/brookfield/poplar-creek-town-center   (Wimmer)
#   /apartment-rentals/pa/lancaster/cherryhill-villas    (JCM Living)
#   /property/goldfinch-meadows                          (Shaool)
#   /locations/orange-county/irvine/westpark/san-carlo.html  (Irvine Co)
#   /apartment/annandale-terrace-apartments              (Anchor Pacifica)
# Matched against the START of a path segment, so vendor spellings like
# ``UnitFees`` (Equity) and ``available-apartments`` (Cortland) are caught.
_PLAN_LINK_SEGMENT = re.compile(
    r"^(?:"
    r"floor[-_]?plans?"
    r"|plans?"
    r"|units?"
    r"|listings?"
    r"|availab"
    r"|pricing"
    r"|appl(?:y|ication)"
    r"|details?"
    r"|lease"
    r")",
    re.IGNORECASE,
)

# Generic page-tail segments that are not a property's identity. The
# landing URL of record often points at a sub-page ("…/avalon-alderwood/
# map-directions"), so the deepest segment is not always the slug.
_GENERIC_PATH_TAIL = frozenset(
    {
        "map-directions", "directions", "contact", "contact-us", "gallery",
        "photos", "photo-gallery", "amenities", "location", "locations",
        "neighborhood", "neighbourhood", "home", "index", "default",
        "floorplans", "floor-plans", "floorplan", "floor-plan", "plans",
        "availability", "available", "pricing", "apartments", "apartment",
        "units", "unit", "details", "detail", "property", "properties",
        "communities", "community", "residences", "overview", "tour",
    }
)

# Asset / file-serving endpoints. These are never a property landing page,
# so they carry no scope signal. Garden Communities gives every plan card
# its own brochure at ``/CMSPages/GetFile.aspx?guid=<unique>`` — a distinct
# URL per card that is nonetheless not another property.
_ASSET_PATH = re.compile(
    r"(?:^|/)(?:cmspages|getfile|download[s]?|media|assets?|uploads?|documents?|files?|images?|img|static|content/dam)(?:/|\.|$)"
    r"|\.(?:pdf|docx?|xlsx?|pptx?|zip|jpe?g|png|gif|svg|webp|mp4|mov)$",
    re.IGNORECASE,
)

# First path segments that mark a site-utility page or a CMS taxonomy
# archive rather than a property. WordPress property themes link cards to
# archives like ``/status/for-rent/`` and ``/label/spearhead/``, and plan
# cards routinely carry a plain ``/contact`` link.
_UTILITY_FIRST_SEGMENT = frozenset(
    {
        "contact", "contact-us", "about", "about-us", "blog", "news", "press",
        "careers", "jobs", "privacy", "terms", "legal", "accessibility",
        "sitemap", "search", "faq", "faqs", "resources", "resident",
        "residents", "gallery", "schedule", "tour", "tours", "amenities",
        "status", "label", "labels", "category", "categories", "tag", "tags",
        "type", "types", "feature", "features", "neighborhood", "region",
        "style", "styles", "specials", "reviews", "login", "portal",
    }
)

# Scope verdicts.
_SCOPE_IN = "IN"
_SCOPE_OUT = "OUT"
_SCOPE_NEUTRAL = "NEUTRAL"


def _property_slug(prop_path: str) -> str:
    """Most specific identifying segment of the property's own path.

    :param prop_path: normalised property path (see :func:`_norm_path`).
    :returns: the deepest non-generic path segment with any file extension
        stripped, or ``""`` when the path carries no distinctive segment.

    ``/washington/lynnwood-apartments/avalon-alderwood/map-directions``
    yields ``avalon-alderwood`` — ``map-directions`` is a page tail, not the
    property's identity.
    """
    for seg in reversed([s for s in prop_path.split("/") if s]):
        stem = re.sub(r"\.(?:html?|aspx?|php|jsp)$", "", seg, flags=re.IGNORECASE)
        if stem and stem not in _GENERIC_PATH_TAIL and len(stem) >= 4:
            return stem
    return ""


def _norm_host(host: str) -> str:
    """Lowercase host with a leading ``www.`` removed."""
    h = (host or "").lower().strip()
    return h[4:] if h.startswith("www.") else h


def _norm_path(path: str) -> str:
    """Lowercase path without a trailing slash. Root becomes ``''``."""
    p = (path or "").strip().lower()
    while p.endswith("/"):
        p = p[:-1]
    return p


def classify_href_scope(property_url: str, href: str, doc_url: str = "") -> str:
    """Classify *href* relative to the property's own URL subtree.

    Returns ``"IN"`` (this property), ``"OUT"`` (positively a different
    property) or ``"NEUTRAL"`` (no evidence either way).

    :param property_url: absolute URL of the property under scrape.
    :param href: raw ``href`` attribute from a card anchor. May be relative.
    :param doc_url: absolute URL of the document the card came from —
        relative hrefs resolve against this. Falls back to *property_url*.
    :returns: one of ``"IN"``, ``"OUT"``, ``"NEUTRAL"``.

    The guard is deliberately asymmetric: only *positive* evidence that a
    link leaves the property's subtree yields ``OUT``. Anything unknown —
    a bare anchor, a cross-host application portal, an ancestor/breadcrumb
    link — is ``NEUTRAL`` so real plan grids are never penalised.
    """
    from urllib.parse import parse_qsl, urljoin, urlparse

    raw = (href or "").strip()
    if not raw or _NON_NAV_HREF.match(raw):
        return _SCOPE_NEUTRAL
    if not (property_url or "").strip():
        return _SCOPE_NEUTRAL
    try:
        prop = urlparse(property_url)
        target = urlparse(urljoin(doc_url or property_url, raw))
    except Exception:  # pragma: no cover — defensive against odd hrefs
        return _SCOPE_NEUTRAL

    prop_host, target_host = _norm_host(prop.netloc), _norm_host(target.netloc)
    if not prop_host or not target_host:
        return _SCOPE_NEUTRAL
    # A different host is not evidence about THIS property — application
    # portals, map links and PMS iframes all legitimately leave the host.
    if prop_host != target_host:
        return _SCOPE_NEUTRAL

    prop_path, target_path = _norm_path(prop.path), _norm_path(target.path)

    # An asset / file-download endpoint says nothing about property identity.
    if _ASSET_PATH.search(target_path):
        return _SCOPE_NEUTRAL

    # Property sits at the site root → the whole host is its subtree.
    if not prop_path:
        return _SCOPE_IN

    if target_path == prop_path:
        # Same path: the property may still be identified by a query
        # param (``/details/?pid=30``). A card linking to the same page
        # with a DIFFERENT value for a param the property URL carries is
        # a sibling property.
        prop_q = dict(parse_qsl(prop.query, keep_blank_values=True))
        target_q = dict(parse_qsl(target.query, keep_blank_values=True))
        for key, prop_val in prop_q.items():
            if key in target_q and target_q[key] != prop_val:
                return _SCOPE_OUT
        return _SCOPE_IN

    # Descendant → a plan-detail page under this property.
    if target_path.startswith(prop_path + "/"):
        return _SCOPE_IN

    # Ancestor → breadcrumb / section index. Not evidence of another
    # property (``/apartments`` from ``/apartments/city/slug``).
    if prop_path.startswith(target_path + "/"):
        return _SCOPE_NEUTRAL

    # Off-subtree, same host. This is only evidence of a DIFFERENT property
    # when the link looks like a property landing page. A link carrying a
    # plan/unit/listing marker is this property's own data on another
    # branch of the site — extremely common on single-property sites.
    segments = [s for s in target_path.split("/") if s]
    if any(_PLAN_LINK_SEGMENT.match(s) for s in segments):
        return _SCOPE_NEUTRAL

    # Site-utility page or CMS taxonomy archive — not a property.
    if segments and segments[0] in _UTILITY_FIRST_SEGMENT:
        return _SCOPE_NEUTRAL

    # The link still names THIS property. Happens when the landing URL of
    # record is a sub-page (``…/avalon-alderwood/map-directions`` linking to
    # ``…/avalon-alderwood/apartment/WA025-00A-A414``) or when plans are
    # modelled as separate CMS posts (``/summerset`` →
    # ``/property/summerset-at-international-crossings-2-2``).
    slug = _property_slug(prop_path)
    if slug and slug in target_path:
        return _SCOPE_NEUTRAL

    # A bare off-subtree page: on a management-company site this is a
    # sibling community's landing page.
    return _SCOPE_OUT


def card_scope_verdict(card: dict[str, Any], property_url: str, doc_url: str = "") -> str:
    """Scope verdict for one card, from its anchors.

    A card is ``OUT`` only when it has at least one off-scope anchor and no
    in-scope anchor — a card that links to this property anywhere is kept
    even if it also carries an unrelated link.

    :param card: card dict from either scanner; reads the ``hrefs`` key.
    :param property_url: absolute URL of the property under scrape.
    :param doc_url: absolute URL of the document the card came from.
    :returns: one of ``"IN"``, ``"OUT"``, ``"NEUTRAL"``.
    """
    hrefs = card.get("hrefs") if isinstance(card, dict) else None
    if not isinstance(hrefs, list) or not hrefs:
        return _SCOPE_NEUTRAL
    verdicts = {
        classify_href_scope(property_url, str(h or ""), doc_url) for h in hrefs
    }
    if _SCOPE_IN in verdicts:
        return _SCOPE_IN
    if _SCOPE_OUT in verdicts:
        return _SCOPE_OUT
    return _SCOPE_NEUTRAL


def filter_cards_by_scope(
    cards: list[dict[str, Any]], property_url: str, doc_url: str = ""
) -> list[dict[str, Any]]:
    """Drop cards that belong to a different property.

    :param cards: card dicts from either scanner.
    :param property_url: absolute URL of the property under scrape. Empty
        disables the guard (returns *cards* unchanged).
    :param doc_url: absolute URL of the document the cards came from.
    :returns: the surviving cards; ``[]`` when fewer than 2 survive, which
        re-applies the module's "≥2 admitted cards" confidence invariant
        after filtering.

    # PR-03 AC: extraction must be attributed to the correct property — a
    # card set scraped from a sibling-community directory is not this
    # property's floor-plan data and must not be admitted.
    """
    if not cards or not (property_url or "").strip():
        return cards
    kept: list[dict[str, Any]] = []
    off_scope = 0
    for c in cards:
        if not isinstance(c, dict):
            continue
        if card_scope_verdict(c, property_url, doc_url) == _SCOPE_OUT:
            off_scope += 1
            continue
        kept.append(c)
    if not off_scope:
        return kept
    total = off_scope + len(kept)
    # Below the ≥2 threshold the remainder is not a plan grid — typically
    # the property's own "starting at" summary row inside a directory of
    # sibling communities. Emitting it would be plan-level noise.
    if len(kept) < 2:
        log.info(
            "generic-dom scope guard REJECTED card set: %d/%d cards belong to "
            "another property property_url=%s doc_url=%s",
            off_scope,
            total,
            property_url,
            doc_url,
        )
        return []
    log.info(
        "generic-dom scope guard dropped %d/%d off-scope cards property_url=%s",
        off_scope,
        total,
        property_url,
    )
    return kept


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

  const findCardsIn = (doc, base) => {
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
      // Card anchors, absolutised against the document they came from.
      // The Python-side scope guard uses these to reject cards that
      // belong to a DIFFERENT property (see module docstring: the
      // Riverwalk / wimmercommunities.com community-directory defect).
      // DOMParser documents have no base URI, so resolve explicitly.
      const hrefs = [];
      el.querySelectorAll('a[href]').forEach((a) => {
        const raw = a.getAttribute('href') || '';
        if (!raw) return;
        try { hrefs.push(new URL(raw, base).href); } catch (e) { hrefs.push(raw); }
      });
      return {name, text, klass: best.cn, hrefs: hrefs.slice(0, 6)};
    });
  };

  // 1) try the live document (we may already be on the right page)
  let cards = findCardsIn(document, location.href);
  let winningPath = location.pathname;
  let docUrl = location.href;

  // Probe helper — fetch `path`, parse, and return cards found there.
  const probePath = async (path) => {
    if (location.pathname.replace(/\/$/, '') === path.replace(/\/$/, '')) return null;
    try {
      const abs = location.origin + path;
      const r = await fetch(abs, {credentials: 'include'});
      if (!r.ok) return null;
      const html = await r.text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const found = findCardsIn(doc, abs);
      if (found.length >= 2) return {found, path, abs};
    } catch (e) { /* next */ }
    return null;
  };

  // 2a) Sub-paths under the PROPERTY'S OWN path first.
  // On a management-company site the property lives at a deep path
  // (`/apartments/{city}/{slug}/`), so `origin + "/apartments"` is the
  // company-wide community directory — a grid of OTHER properties. Probing
  // the property's own subtree keeps the scan attributed to this property.
  const propBase = location.pathname.replace(/\/$/, '');
  if (cards.length < 2 && propBase && propBase !== '') {
    for (const p of SUBPATHS) {
      const hit = await probePath(propBase + p);
      if (hit) { cards = hit.found; winningPath = hit.path; docUrl = hit.abs; break; }
    }
  }

  // 2b) fall back to origin-rooted sub-paths (correct for single-property
  // sites, where the property IS the site root). Results from here are
  // still scope-guarded on the Python side.
  if (cards.length < 2) {
    for (const p of SUBPATHS) {
      const hit = await probePath(p);
      if (hit) { cards = hit.found; winningPath = hit.path; docUrl = hit.abs; break; }
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
    // A brand page lists EVERY community in the brand, so DOM order is not
    // this property. Probe paths inside the property's own subtree first.
    brandPaths.sort((a, b) => {
      const inA = propBase && a.toLowerCase().startsWith(propBase.toLowerCase()) ? 0 : 1;
      const inB = propBase && b.toLowerCase().startsWith(propBase.toLowerCase()) ? 0 : 1;
      return inA - inB;
    });
    for (const p of brandPaths) {
      try {
        const abs = location.origin + p;
        const r = await fetch(abs, {credentials: 'include'});
        if (!r.ok) continue;
        const html = await r.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const found = findCardsIn(doc, abs);
        if (found.length >= 2) { cards = found; winningPath = p; docUrl = abs; break; }
      } catch (e) { /* next */ }
    }
  }

  return {cards, winningPath, docUrl, count: cards.length};
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
    cards: list[dict[str, Any]],
    url: str,
    property_url: str = "",
    doc_url: str = "",
) -> list[dict[str, str]]:
    """Parse the JS extractor's card list into plan-level unit dicts.

    :param cards: card dicts from either the JS or the static scanner.
    :param url: provenance URL stamped onto each unit's ``source_api_url``.
    :param property_url: absolute URL of the property under scrape. When
        supplied, the scope guard rejects cards belonging to a different
        property (see module docstring). Empty disables the guard.
    :param doc_url: absolute URL of the document the cards came from —
        relative anchor hrefs resolve against it. Defaults to *url*.
    :returns: plan-level unit dicts; ``[]`` when the card set is rejected.
    """
    # Scope gate runs BEFORE parsing so rejected sets cost no work and
    # never reach post_process.
    cards = filter_cards_by_scope(cards, property_url, doc_url or url)
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
                # The property URL the scope guard measures against — the
                # live page URL, falling back to the configured base URL.
                property_url = str(
                    getattr(page, "url", "") or getattr(ctx, "base_url", "") or ""
                )
                # Build a provenance URL from page.url + the winning sub-path.
                win_url = ""
                try:
                    from urllib.parse import urlparse, urlunparse

                    p = urlparse(property_url)
                    if p.scheme and p.netloc:
                        win_url = urlunparse(
                            (p.scheme, p.netloc, winning_path or p.path, "", "", "")
                        )
                except Exception:
                    win_url = ""

                # The JS absolutises card hrefs against the document they
                # came from; prefer that URL for relative-href resolution.
                doc_url = str(scan.get("docUrl") or "") or win_url
                units = parse_generic_floorplan_cards(
                    cards, win_url, property_url=property_url, doc_url=doc_url
                )
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
    # The property's own URL — fixed for the whole scan. ``base_url`` moves
    # to the winning sub-path below, so the scope guard needs its own copy.
    property_url = base_url

    # Try the homepage body first.
    cards = _scan_static_html_for_cards(body)
    winning_path = ""
    doc_url = base_url

    # If homepage didn't yield enough, probe known sub-paths.
    if len(cards) < 2:
        try:
            from urllib.parse import urlparse

            from ma_poc.pms.adapters._probe import probe_get

            p = urlparse(base_url)
            if not (p.scheme and p.netloc):
                return [], ""
            origin = f"{p.scheme}://{p.netloc}"

            # Property-subtree paths first, then origin-rooted (mirrors the
            # JS extractor). On a management-company site `origin + sub` is
            # the company-wide directory, not this property — see the
            # module docstring.
            prop_base = _norm_path(p.path)
            candidates = [
                (prop_base + sub, sub) for sub in _FLOORPLAN_SUBPATHS if prop_base
            ] + [(sub, sub) for sub in _FLOORPLAN_SUBPATHS]

            sub_found = False
            for full_path, sub in candidates:
                try:
                    r = probe_get(origin + full_path, timeout=12)
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
                    base_url = origin + full_path
                    doc_url = base_url
                    sub_found = True
                    break
            if not sub_found:
                return [], ""
        except Exception as exc:
            log.debug("generic-dom static sub-probe failed err=%s", exc)
            return [], ""

    if len(cards) < 2:
        return [], winning_path

    units = parse_generic_floorplan_cards(
        cards, base_url, property_url=property_url, doc_url=doc_url
    )
    return units, winning_path


def _scan_static_html_for_cards(html: str) -> list[dict[str, Any]]:
    """Scan ``html`` for repeated plan-card-shaped containers.

    Returns a list of dicts in the same shape the JS extractor produces:
        ``[{"text": ..., "name": ..., "hrefs": [...]}, ...]``

    ``hrefs`` carries each card's raw anchor targets so the caller's scope
    guard can reject cards belonging to a different property.

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
        # bs4 returns a list for multi-valued attrs but a str when the
        # parser has not split them — normalise before joining.
        raw_classes: str | list[str] = el.get("class") or []
        classes: list[str] = (
            [raw_classes] if isinstance(raw_classes, str) else list(raw_classes)
        )
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
            # Anchor targets for the scope guard. Kept raw — relative hrefs
            # are resolved against the document URL by the guard itself.
            hrefs = [
                str(a.get("href") or "")
                for a in el.find_all("a", href=True)[:6]
                if a.get("href")
            ]
            cards.append({"text": text, "name": name, "hrefs": hrefs})
        if len(cards) > len(best_cards):
            best_cards = cards

    return best_cards
