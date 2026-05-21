"""Market Apartments CMS (marketapts.com) adapter.

Market Apartments is a multifamily marketing-website vendor whose CMS
templates the property's customer-facing site. The same vendor powers
13+ properties in the 600-property grind sample (originally tagged as
``GoPrisma`` because they link to a residents-only GoPrisma SPA portal,
but the actual unit-level data lives on the marketing CMS itself).

Detector signal ``marketing_marketapts`` already recognises the marker
(``marketapts.com`` asset host) but no adapter consumed it before this
file — properties fell through to the generic LLM cascade and landed
in the ``t2_llm_only`` bucket. This adapter routes them to a strict
Tier-1/2 DOM extraction instead.

Two SSR template variants are handled (probed live 2026-05-21):

  * **Template A — inline unit rows** (1 / 13 cohort sites, e.g.
    Sandpiper SaltLake City, ``623SAN``): the ``/floorplans`` page
    server-renders ``.floorplan-block`` plan cards with embedded
    ``.floorplan-unit-single`` unit rows. Each unit row carries
    ``data-when`` (ISO date), ``data-price``, ``data-bedrooms``,
    ``data-bathrooms``, plus DOM text ``UNIT #<n>``, ``$<rent>``,
    ``<availability text>``. Plan card carries ``data-bedrooms`` and
    ``data-bathrooms`` so plan-to-unit roll-up is by these attrs.

  * **Template B — drill-to-/unit/** (10 / 13 cohort sites, e.g.
    Aspire Thunderbird ``1073ATHB``, The Landmark ``286LMK``, Live at
    Coventry ``785CTT``, mountainridge-apts ``104CSC``): the
    ``/floorplans`` page server-renders ``.floorplan-item`` plan cards
    with ``.floorplan-title`` (plan code), ``.floorplan-features``
    (specs), ``.floorplan-num`` (starting price), and a "View Available
    Units" anchor that links to ``/unit/{plan-slug}``. The per-plan
    drill page server-renders a ``.unit-table-row`` × N table with
    columns Unit / Rent / Available / Special / Features / Apply.

  * **Template C — gallery + /unit/ drill** (1 / 13, e.g.
    thereserveatwatertowervillage ``130RWT``): listing page is a
    single ``.floorplan-container`` gallery with N plans rendered as
    flat text and "N Available" anchors that drill to ``/unit/
    {plan-slug}``. The drill page exposes ``.unit-details`` rows
    (columns Unit / SqFt / Rent / Available), and plan specs live in
    the drill h1 + body text ("BED: X BATH: Y SQ. FEET: Z").
    Originally deferred (eyeball missed the badge-link drill); re-
    probed 2026-05-21 after user pointed out the link in the badge.

  * **Template D — /apartments/{plan-slug} drill** (1 / 13, e.g.
    Riverbank ``20RIB``): hyphenated ``/floor-plans`` listing of
    ``.floor-plans-block`` plan cards that drill to ``/apartments/
    {plan-slug}``. Drill rows are ``.unit-table-row`` (SAME selector
    as Template B) with the bonus that each row carries
    ``data-available-date`` (ISO, authoritative). The Template B
    parser is reused here; only listing-side detection differs.

  * **Template E — subpath tabbed accordion (plan-only)** (2 / 32
    fleet sites — sylispm.com/woodway-apartments,
    sylispm.com/oakstone-apartment-homes): shared-host CMS where
    each property lives at ``{cms-host}/{property-slug}/`` and the
    floor-plans render inline at ``#floorplans`` as Bootstrap card
    panes inside ``.tab-pane`` × N (one pane per bedroom count).
    Cards expose ``.floorplan-name`` + ``.list-group.floorplan-info``
    items ("0 Bedroom" / "1 Bathroom" / "504 Square Feet" / "880"
    where the bare number is the rent). No per-unit drill — plan-
    level only. Discovered 2026-05-21 by fleet grep + live probe.

  * **Template F — /availability data-table (unit-level)** (1 / 32
    fleet sites — 223etown.com): ``/floorplans`` is plan-only but
    ``/availability`` publishes the unit roster as
    ``<table class="table table-hover f{N} Bedroom Apartments">``
    tables partitioned by bedroom count. Cells (positionally
    stable): Beds / Bath / Rent / Sq.Ft. / Style (= plan name) /
    Features / Available date. No explicit ``unit_number`` —
    downstream dedup is by (plan, rent, sqft). "Call For Details"
    in the Available column maps to UNAVAILABLE. Discovered 2026-05-
    21 by fleet grep + live probe.

URL-path note (2026-05-21 user-validation correction): the
``/floorplans`` (no hyphen) vs ``/floor-plans`` (hyphenated) split
crosses templates — Templates A/B/C usually serve ``/floorplans``;
Template D serves ``/floor-plans``. Earlier eyeball probing missed 4
properties (mountainridgemanor, theazleeapartments, liveatthejuniper,
embarcatwestjordan) by checking only ``/floorplans`` — re-probe
confirmed they all render Template D at ``/floor-plans``. The DOM JS
self-fetch already tries both paths in order, so the adapter routes
them correctly in production; the misclassification was a probing
mistake, not a code gap.

Probed cohort size: 13 GoPrisma-tagged + 4 marketapts-tagged in
results_deep.jsonl = 17 confirmed properties; the fleet-wide signal is
broader because ``marketing_marketapts`` is a generic detector marker
on any site that uses the CMS.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

# Templates that exist in the cohort but are not handled in v1. Listed
# in source so the next adapter author finds them without re-probing.
# All four template variants observed in the 13-site cohort are
# handled. This tuple is retained for future variants discovered
# beyond the original cohort.
_DEFERRED_TEMPLATES: tuple[str, ...] = ()

# Self-fetch /floorplans if the live page isn't there, then probe for
# Template A first (inline unit rows). If found, return them with their
# parent plan metadata. Otherwise fall through to Template B (drill to
# /unit/{slug} per plan) — fetches each plan's drill page in-session and
# joins the .unit-table-row rows back to the plan metadata.
_MARKETAPTS_DOM_JS = r"""
async () => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const A = (el, name) => (el ? (el.getAttribute(name) || '') : '');

  // ── locate the floorplans document ───────────────────────────────
  // Live page already on a floorplans-style path? Use document directly.
  // Otherwise probe ``/floorplans`` first (Templates A/B/C), then
  // ``/floor-plans`` (Template D, hyphenated). credentials: 'include'
  // so cookie-walled sites still serve their SSR markup.
  let doc = document;
  const hasMarker = !!(
    document.querySelector('.floorplan-block') ||
    document.querySelector('.floorplan-item') ||
    document.querySelector('.floorplan-unit-single') ||
    document.querySelector('.floor-plans-block') ||
    document.querySelector('.floorplan-name')
  );
  if (!hasMarker) {
    // 2026-05-21 HAR validation: 223etown.com keeps /floorplans plan-only
    // and publishes unit data at /availability. Other sites use /floor-plans
    // (hyphenated, Template D pattern). Probe both spellings + the
    // /availability fallback. The self-fetch only "wins" when the candidate
    // doc carries an expected selector OR a Template-F table, so adding
    // paths has no false-positive risk.
    for (const path of ['/floorplans', '/floor-plans', '/availability']) {
      try {
        const r = await fetch(location.origin + path, {credentials: 'include'});
        if (r.ok) {
          const candidate = new DOMParser().parseFromString(await r.text(), 'text/html');
          if (
            candidate.querySelector('.floorplan-block') ||
            candidate.querySelector('.floorplan-item') ||
            candidate.querySelector('.floorplan-unit-single') ||
            candidate.querySelector('.floor-plans-block') ||
            candidate.querySelector('.floorplan-container') ||
            candidate.querySelector('.floorplan-name') ||
            candidate.querySelector('table.table-hover')
          ) {
            doc = candidate;
            break;
          }
        }
      } catch (e) { /* try next */ }
    }
  }

  // ── Template A — inline unit rows (.floorplan-block + .floorplan-unit-single)
  //
  // CLASS-COLLISION GUARD (2026-05-21 HAR validation finding): the
  // ``.floorplan-block`` class is ALSO used by a RentCafe / Yardi ASPX
  // legacy theme (observed on somaresidences.com/Floor-Plans.aspx,
  // thefrankestate.com/, etc.) where the data shape is
  // ``data-rent`` / ``data-numunits`` / ``data-bed`` — completely
  // different from Market Apartments' Template A
  // (``data-bedrooms`` / ``data-bathrooms`` / ``data-when``).
  //
  // The guard ``&& doc.querySelector('.floorplan-unit-single')`` is the
  // discriminator: real Market Apartments Template A pages ALWAYS embed
  // ``.floorplan-unit-single`` unit rows inside each ``.floorplan-block``,
  // while the RentCafe-ASPX theme has plan cards but NO unit-row class.
  // Do NOT simplify this to just ``.floorplan-block`` — it would
  // mis-route 9+ RentCafe-ASPX sites observed in the HAR set.
  const aBlocks = Array.from(doc.querySelectorAll('.floorplan-block'));
  if (aBlocks.length > 0 && doc.querySelector('.floorplan-unit-single')) {
    const planRows = aBlocks.map((b) => {
      const plan = {
        template: 'A',
        beds: A(b, 'data-bedrooms'),
        baths: A(b, 'data-bathrooms'),
        name: T(b.querySelector('.floorplan-name, .floorplan-title')),
        // Floor-plan starting price often lives in a "from $X" element.
        startingPrice: T(b.querySelector('.floorplan-num, .floorplan-rent')),
        units: Array.from(b.querySelectorAll('.floorplan-unit-single')).map((u) => ({
          unitNumber: (T(u).match(/UNIT\s*[#:]?\s*([A-Z0-9\-]+)/i) || [])[1] || '',
          dataWhen: A(u, 'data-when'),
          dataPrice: A(u, 'data-price'),
          dataBeds: A(u, 'data-bedrooms'),
          dataBaths: A(u, 'data-bathrooms'),
          priceText: T(u.querySelector('.unit-price-single')),
          availText: T(u.querySelector('.unit-available-single')),
        })),
      };
      return plan;
    });
    return {template: 'A', plans: planRows};
  }

  // ── Template B — drill-per-plan (.floorplan-item + /unit/{slug})
  const bItems = Array.from(doc.querySelectorAll('.floorplan-item'));
  if (bItems.length > 0) {
    const planTasks = bItems.map((item) => {
      const drillAnchor = Array.from(item.querySelectorAll('a')).find((a) => {
        const t = (a.innerText || a.textContent || '').toLowerCase();
        return /view\s+available|view\s+details|see\s+available/.test(t);
      });
      const title = T(item.querySelector('.floorplan-title'));
      const features = T(item.querySelector('.floorplan-features'));
      const num = T(item.querySelector('.floorplan-num'));
      return {
        title: title,
        features: features,
        startingPrice: num,
        drillPath: drillAnchor ? drillAnchor.getAttribute('href') || '' : '',
      };
    });

    const planRows = [];
    for (const task of planTasks) {
      let units = [];
      if (task.drillPath) {
        let drillUrl = task.drillPath;
        if (drillUrl.startsWith('/')) drillUrl = location.origin + drillUrl;
        try {
          const r = await fetch(drillUrl, {credentials: 'include'});
          if (r.ok) {
            const drillDoc = new DOMParser().parseFromString(await r.text(), 'text/html');
            units = Array.from(drillDoc.querySelectorAll('.unit-table-row')).map((row) => {
              const cells = Array.from(row.children).map((c) => T(c));
              return {cells: cells, dataAttrs: Object.assign({}, row.dataset || {})};
            });
          }
        } catch (e) { /* per-plan drill failure → plan-level only */ }
      }
      planRows.push({...task, template: 'B', units});
    }
    return {template: 'B', plans: planRows};
  }

  // ── Template C — gallery (.floorplan-container) with /unit/{slug} drills
  // Listing page is a single .floorplan-container with N plans rendered
  // as flat text + "N Available" anchors to /unit/{slug}. Drill page
  // exposes .unit-details rows with Unit / SqFt / Rent / Available cells.
  // Plan title + specs live in the drill h1 + body text — not the
  // listing — so the listing-side card extraction is just URL discovery.
  const cContainers = Array.from(doc.querySelectorAll('.floorplan-container'));
  if (cContainers.length > 0) {
    const unitHrefs = [];
    for (const cont of cContainers) {
      const anchors = Array.from(cont.querySelectorAll('a[href*="/unit/"]'));
      for (const a of anchors) {
        const href = a.getAttribute('href') || '';
        if (href && !unitHrefs.includes(href)) unitHrefs.push(href);
      }
    }
    if (unitHrefs.length === 0) {
      return {template: 'NONE', plans: []};
    }
    const planRows = [];
    for (const href of unitHrefs) {
      let drillUrl = href;
      if (drillUrl.startsWith('/')) drillUrl = location.origin + drillUrl;
      let title = '';
      let specsBlob = '';
      let units = [];
      try {
        const r = await fetch(drillUrl, {credentials: 'include'});
        if (r.ok) {
          const drillDoc = new DOMParser().parseFromString(await r.text(), 'text/html');
          title = T(drillDoc.querySelector('h1'));
          // Drill body has "BED: X BATH: Y SQ. FEET: Z" — capture the
          // surrounding container that holds these labels.
          const bodyText = T(drillDoc.querySelector('section.unit, .section.unit')) ||
                           T(drillDoc.body).slice(0, 2000);
          specsBlob = bodyText;
          units = Array.from(drillDoc.querySelectorAll('.unit-details')).map((row) => {
            const cells = Array.from(row.children).map((c) => T(c));
            return {cells: cells, dataAttrs: Object.assign({}, row.dataset || {})};
          });
        }
      } catch (e) { /* drill failure → skip this plan */ }
      planRows.push({title, specsBlob, drillPath: href, units, template: 'C'});
    }
    return {template: 'C', plans: planRows};
  }

  // ── Template D — hyphenated /floor-plans + /apartments/{slug} drill
  // Listing-side ``.floor-plans-block`` plan cards each carry an
  // "N APARTMENTS AVAILABLE" anchor pointing to ``/apartments/{slug}``.
  // The drill page exposes ``.unit-table-row`` (SAME selector as
  // Template B's drill) with cells Unit / Rent / Available / Special /
  // Apply. Drill rows often carry ``data-available-date`` (ISO),
  // ``data-beds``, ``data-baths`` — captured here for downstream use
  // (the current parser is happy to fall back to cell-walking).
  const dBlocks = Array.from(doc.querySelectorAll('.floor-plans-block'));
  if (dBlocks.length > 0) {
    const planTasks = dBlocks.map((block) => {
      const drillAnchor = Array.from(block.querySelectorAll('a')).find((a) => {
        const href = a.getAttribute('href') || '';
        const t = (a.innerText || a.textContent || '').toLowerCase();
        return /\/apartments\//.test(href) ||
               /apartments?\s+available|view\s+available|view\s+details/.test(t);
      });
      const heading = T(block.querySelector('h1, h2, h3, h4, h5, h6'));
      const detailsBlock = T(block.querySelector('.floor-plans-block-details, .list-group'));
      // Pull a starting price out of the body text — Template D often
      // states "FROM: $X,XXX" inline.
      const bodyText = T(block);
      const fromMatch = bodyText.match(/FROM\s*:?\s*\$\s*([\d,]+)/i);
      const startingPrice = fromMatch ? '$' + fromMatch[1] : '';
      return {
        title: heading,
        features: detailsBlock,
        startingPrice: startingPrice,
        drillPath: drillAnchor ? drillAnchor.getAttribute('href') || '' : '',
      };
    });

    const planRows = [];
    for (const task of planTasks) {
      let units = [];
      if (task.drillPath) {
        let drillUrl = task.drillPath;
        if (drillUrl.startsWith('/')) drillUrl = location.origin + drillUrl;
        try {
          const r = await fetch(drillUrl, {credentials: 'include'});
          if (r.ok) {
            const drillDoc = new DOMParser().parseFromString(await r.text(), 'text/html');
            units = Array.from(drillDoc.querySelectorAll('.unit-table-row')).map((row) => {
              const cells = Array.from(row.children).map((c) => T(c));
              return {cells: cells, dataAttrs: Object.assign({}, row.dataset || {})};
            });
          }
        } catch (e) { /* per-plan drill failure → plan-level only */ }
      }
      planRows.push({...task, template: 'D', units});
    }
    return {template: 'D', plans: planRows};
  }

  // ── Template E — subpath site with tabbed accordion (plan-only)
  // Used by shared-host operators like sylispm.com where each property
  // lives at sylispm.com/{property-slug}/ and the floor-plans render
  // inline as Bootstrap card panes inside ``.tab-pane`` elements (one
  // pane per bedroom count). Each plan card has a ``.floorplan-name``
  // heading + ``.list-group.floorplan-info > li`` items carrying
  // "N Bedroom" / "N Bathroom" / "NNN Square Feet" / "NNN" (rent, no $).
  // No per-unit drill — these sites only publish plan-level data.
  const eCards = Array.from(doc.querySelectorAll('.floorplan-name'));
  if (eCards.length > 0) {
    // De-duplicate: Bootstrap nests .card inside .card-block which both
    // contain the .floorplan-name, so the same plan can be matched twice.
    // Group by the nearest .card ancestor.
    const seenCards = new Set();
    const planRows = [];
    for (const nameEl of eCards) {
      const card = nameEl.closest('.card') || nameEl.parentElement;
      if (!card || seenCards.has(card)) continue;
      seenCards.add(card);
      const items = Array.from(card.querySelectorAll('.list-group-item, .floorplan-info li'))
        .map((li) => T(li))
        .filter((t) => t);
      planRows.push({
        template: 'E',
        name: T(nameEl),
        infoItems: items,
      });
    }
    if (planRows.length > 0) {
      return {template: 'E', plans: planRows};
    }
  }

  // ── Template F — separate /availability sub-page with data tables
  // Some Market Apartments sites (e.g. 223etown.com) keep /floorplans
  // plan-only and publish the unit-level roster on /availability as
  // <table class="table table-hover f{N} Bedroom Apartments"> tables
  // (one per bedroom count). Cell columns: Beds / Bath / Rent / Sq.Ft.
  // / Style (plan name) / Features / Available.
  let availDoc = null;
  if (document.querySelector('table.table-hover')) {
    availDoc = document;
  } else {
    try {
      const r = await fetch(location.origin + '/availability', {credentials: 'include'});
      if (r.ok) {
        const candidate = new DOMParser().parseFromString(await r.text(), 'text/html');
        if (candidate.querySelector('table.table-hover')) {
          availDoc = candidate;
        }
      }
    } catch (e) { /* fall through */ }
  }
  if (availDoc) {
    const tables = Array.from(availDoc.querySelectorAll('table.table-hover'));
    const rows = [];
    for (const t of tables) {
      const tableName = t.className || '';  // "table table-hover f1 Bedroom Apartments"
      const trs = Array.from(t.querySelectorAll('tbody tr'));
      for (const tr of trs) {
        const cells = Array.from(tr.children).map((c) => T(c));
        rows.push({cells: cells, tableName: tableName, dataAttrs: Object.assign({}, tr.dataset || {})});
      }
    }
    if (rows.length > 0) {
      return {template: 'F', plans: [], rows: rows};
    }
  }

  return {template: 'NONE', plans: []};
}
"""

# BED/BATH/SQ.FEET labels appear in two shapes:
#   * Template B: "BEDROOMS: 1 BATHROOMS: 1.0 SQ. FEET: 702" (plural long form)
#   * Template C: "BED: Studio BATH: 1 SQ. FEET: 450"        (short form)
# The (?:ROOMS?|S)? clause accepts BED / BEDS / BEDROOM / BEDROOMS and the
# analogous BATH variants without false-matching unrelated words.
_BED_RE = re.compile(r"BED(?:ROOMS?|S)?\s*:?\s*(\d+|studio)", re.IGNORECASE)
_BATH_RE = re.compile(r"BATH(?:ROOMS?|S)?\s*:?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_SQFT_RE = re.compile(r"SQ\s*[\.\s]?\s*FEET\s*:?\s*(\d[\d,]*)", re.IGNORECASE)
_DEPOSIT_RE = re.compile(r"DEPOSIT\s*:?\s*\$?([\w,]+)", re.IGNORECASE)
_MONEY_RE = re.compile(r"\$\s*([\d,]+)")
# Common "Available {date or 'Now'}" cell text in Template B; data-when
# in Template A is already ISO. Plain ISO dates pass straight through.
_DATE_LIKE_RE = re.compile(
    r"\b("
    r"now|today|"
    r"\d{4}-\d{2}-\d{2}|"
    r"(?:january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+\d{1,2},?\s*\d{4}|"
    r"\d{1,2}/\d{1,2}/\d{2,4}"
    r")\b",
    re.IGNORECASE,
)


def _parse_template_a_features(features_text: str) -> tuple[int | None, str, str]:
    """Pull (beds, baths, sqft) out of Template A plan-level body text.

    Template A plans don't always have a dedicated features block — the
    DOM stamps ``data-bedrooms`` / ``data-bathrooms`` on the parent
    ``.floorplan-block`` and a "Beds X | Baths X | XXX SF" string in the
    floorplan-details box. ``_parse_specs_blob`` handles either flavor.
    """
    return _parse_specs_blob(features_text)


def _parse_specs_blob(specs: str) -> tuple[int | None, str, str]:
    """Tolerant parse of "BEDROOMS: 1 BATHROOMS: 1.0 SQ. FEET: 702" style.

    Returns ``(beds, baths, sqft)`` where:
      * ``beds`` is an int (0 for studio) or ``None`` if not parseable
      * ``baths`` and ``sqft`` are strings (empty on miss)
    """
    if not specs:
        return None, "", ""
    if re.search(r"\bstudio\b", specs, re.IGNORECASE):
        beds: int | None = 0
    else:
        bm = _BED_RE.search(specs)
        if bm and bm.group(1).lower() == "studio":
            beds = 0
        elif bm:
            try:
                beds = int(bm.group(1))
            except (TypeError, ValueError):
                beds = None
        else:
            beds = None
    bath_m = _BATH_RE.search(specs)
    baths = bath_m.group(1) if bath_m else ""
    sqft_m = _SQFT_RE.search(specs)
    sqft = sqft_m.group(1).replace(",", "") if sqft_m else ""
    return beds, baths, sqft


def _parse_avail_text(text: str) -> str:
    """Extract a Template B availability date string from "Available Now"
    / "May 21, 2026" / "Now" / ISO / MM/DD/YYYY. Returns empty for misses.

    "Now" and "Today" map to empty string (= immediately available, no
    future date to surface); ISO/long-form/slash-form dates pass
    through as-is. Downstream date-normalisation runs in post_process.
    """
    if not text:
        return ""
    m = _DATE_LIKE_RE.search(text)
    if not m:
        return ""
    val = m.group(1)
    if val.lower() in {"now", "today"}:
        return ""
    return val


def parse_marketapts_template_a(
    plans: list[dict[str, object]], url: str
) -> list[dict[str, str]]:
    """Emit unit-level rows from Template A ``.floorplan-block`` cards.

    One row per ``.floorplan-unit-single`` inside each plan card. Plan
    metadata (beds, baths) flows from ``data-bedrooms`` / ``data-bathrooms``
    on the parent card; sqft/baths fallback from the plan-name text if
    needed. Each unit row carries the unit number (from DOM text),
    its own price (``data-price``), and availability date (``data-when``,
    which is ISO and authoritative).
    """
    out: list[dict[str, str]] = []
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        plan_name = str(plan.get("name") or "").strip()
        plan_beds_str = str(plan.get("beds") or "").strip()
        plan_baths_str = str(plan.get("baths") or "").strip()
        try:
            plan_beds: int | None = int(plan_beds_str) if plan_beds_str else None
        except (TypeError, ValueError):
            plan_beds = None
        plan_sqft = ""
        # Plan-level "FROM $X" — useful only as a fallback if a unit row
        # somehow lacks data-price.
        sp_text = str(plan.get("startingPrice") or "")
        sp_money = _MONEY_RE.findall(sp_text)
        plan_floor_price = money_to_int(sp_money[0]) if sp_money else None

        units = plan.get("units") or []
        if not isinstance(units, list) or not units:
            # Plan-level fallback when no unit roster present.
            if plan_name or plan_floor_price is not None:
                out.append(
                    make_unit_dict(
                        floor_plan_name=plan_name,
                        bed_label=bed_label_from(plan_beds, plan_name),
                        bedrooms=str(plan_beds) if plan_beds is not None else "",
                        bathrooms=plan_baths_str,
                        sqft=plan_sqft,
                        unit_number="",
                        rent_low=plan_floor_price,
                        rent_high=plan_floor_price,
                        rent_range=format_rent_range(plan_floor_price, plan_floor_price),
                        availability_status="AVAILABLE",
                        source_api_url=url,
                        extraction_tier="TIER_1_DOM_MARKETAPTS",
                    )
                )
            continue

        for u in units:
            if not isinstance(u, dict):
                continue
            unit_no = str(u.get("unitNumber") or "").strip()
            # Prefer data-price (numeric) over text price (".unit-price-single")
            data_price = str(u.get("dataPrice") or "").strip()
            rent: int | None
            if data_price.isdigit():
                rent = int(data_price)
            else:
                price_text = str(u.get("priceText") or "")
                tm = _MONEY_RE.findall(price_text)
                rent = money_to_int(tm[0]) if tm else None
            # data-when is ISO; trust it. avail-text is humanised display.
            avail_iso = str(u.get("dataWhen") or "").strip()
            avail_date = avail_iso if avail_iso else _parse_avail_text(
                str(u.get("availText") or "")
            )
            if not unit_no and rent is None:
                continue
            # Per-unit beds/baths from data-bedrooms/-bathrooms; falls back
            # to plan card values.
            u_beds_str = str(u.get("dataBeds") or "").strip() or plan_beds_str
            u_baths_str = str(u.get("dataBaths") or "").strip() or plan_baths_str
            try:
                u_beds: int | None = int(u_beds_str) if u_beds_str else plan_beds
            except (TypeError, ValueError):
                u_beds = plan_beds
            out.append(
                make_unit_dict(
                    floor_plan_name=plan_name,
                    bed_label=bed_label_from(u_beds, plan_name),
                    bedrooms=str(u_beds) if u_beds is not None else "",
                    bathrooms=u_baths_str,
                    sqft=plan_sqft,
                    unit_number=unit_no,
                    rent_low=rent,
                    rent_high=rent,
                    availability_status="AVAILABLE",
                    available_units="1",
                    availability_date=avail_date,
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_MARKETAPTS",
                )
            )
    return out


def parse_marketapts_template_b(
    plans: list[dict[str, object]], url: str
) -> list[dict[str, str]]:
    """Emit unit-level rows from Template B per-plan ``/unit/{slug}`` drill.

    Each plan exposes ``.floorplan-title`` / ``.floorplan-features`` /
    ``.floorplan-num`` (starting price) at the listing level. The drill
    page lists ``.unit-table-row`` rows; columns are (Unit, Rent,
    Available, Special, Features, Apply) per the Aspire Thunderbird /
    Landmark / Coventry probes. We pull the first three positionally
    and let an unrelated "Apply" cell trail without consuming it.
    """
    out: list[dict[str, str]] = []
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        title = str(plan.get("title") or "").strip()
        features = str(plan.get("features") or "")
        beds, baths, sqft = _parse_specs_blob(features)
        sp_text = str(plan.get("startingPrice") or "")
        sp_money = _MONEY_RE.findall(sp_text)
        plan_floor_price = money_to_int(sp_money[0]) if sp_money else None

        units = plan.get("units") or []
        if not isinstance(units, list) or not units:
            # No drill or empty drill — plan-level row only (still useful
            # as the verdict that the plan exists with a starting rent).
            if title or plan_floor_price is not None:
                out.append(
                    make_unit_dict(
                        floor_plan_name=title,
                        bed_label=bed_label_from(beds, title),
                        bedrooms=str(beds) if beds is not None else "",
                        bathrooms=baths,
                        sqft=sqft,
                        unit_number="",
                        rent_low=plan_floor_price,
                        rent_high=plan_floor_price,
                        rent_range=format_rent_range(plan_floor_price, plan_floor_price),
                        availability_status="AVAILABLE" if plan_floor_price else "UNAVAILABLE",
                        source_api_url=url,
                        extraction_tier="TIER_1_DOM_MARKETAPTS",
                    )
                )
            continue

        for u in units:
            if not isinstance(u, dict):
                continue
            cells_raw = u.get("cells") or []
            data_attrs = u.get("dataAttrs") or {}
            if not isinstance(cells_raw, list):
                continue
            # Strip empty cells; Apply-button cell sometimes interleaves.
            cells = [str(c).strip() for c in cells_raw if str(c).strip()]
            if not cells:
                continue
            unit_no = cells[0]
            rent: int | None = None
            # Prefer the row's ``data-available-date`` (ISO, authoritative)
            # when present — Template D's drill rows carry it. Falls back
            # to cell-walking text parse for Templates B/C drills that
            # don't.
            avail_date = ""
            if isinstance(data_attrs, dict):
                iso_candidate = str(data_attrs.get("availableDate") or "").strip()
                if iso_candidate:
                    avail_date = iso_candidate
            # Walk remaining cells positionally to find rent + availability.
            # Defensive against column drift between properties.
            for cell in cells[1:]:
                if rent is None:
                    m = _MONEY_RE.findall(cell)
                    if m:
                        rent = money_to_int(m[0])
                        continue
                if not avail_date:
                    candidate = _parse_avail_text(cell)
                    # Treat "Now"/"Today" as a positive match too — that
                    # consumes the cell even though avail_date stays empty
                    # (= immediately available).
                    if candidate or _DATE_LIKE_RE.search(cell):
                        avail_date = candidate
                        continue
            if not unit_no and rent is None:
                continue
            out.append(
                make_unit_dict(
                    floor_plan_name=title,
                    bed_label=bed_label_from(beds, title),
                    bedrooms=str(beds) if beds is not None else "",
                    bathrooms=baths,
                    sqft=sqft,
                    unit_number=unit_no,
                    rent_low=rent,
                    rent_high=rent,
                    availability_status="AVAILABLE",
                    available_units="1",
                    availability_date=avail_date,
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_MARKETAPTS",
                )
            )
    return out


def parse_marketapts_template_c(
    plans: list[dict[str, object]], url: str
) -> list[dict[str, str]]:
    """Emit unit-level rows from Template C ``.floorplan-container`` gallery.

    Listing-side is a single ``.floorplan-container`` with flat-text plans
    and ``<a href="/unit/{slug}">N Available</a>`` drill links. The drill
    page is the source of truth for both plan specs (h1 + body labels
    BED/BATH/SQ.FEET) AND the per-unit roster (``.unit-details`` rows
    with cells Unit / SqFt / Rent / Available). Each plan dict here
    carries ``title``, ``specsBlob``, ``drillPath``, and ``units``.
    """
    out: list[dict[str, str]] = []
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        title = str(plan.get("title") or "").strip()
        specs_blob = str(plan.get("specsBlob") or "")
        beds, baths, sqft = _parse_specs_blob(specs_blob)

        units = plan.get("units") or []
        if not isinstance(units, list) or not units:
            # Drill failed or returned no rows — surface a plan-level
            # marker so the verdict reflects the plan exists.
            if title or specs_blob:
                out.append(
                    make_unit_dict(
                        floor_plan_name=title,
                        bed_label=bed_label_from(beds, title),
                        bedrooms=str(beds) if beds is not None else "",
                        bathrooms=baths,
                        sqft=sqft,
                        unit_number="",
                        availability_status="UNAVAILABLE",
                        source_api_url=url,
                        extraction_tier="TIER_1_DOM_MARKETAPTS",
                    )
                )
            continue

        for u in units:
            if not isinstance(u, dict):
                continue
            cells_raw = u.get("cells") or []
            if not isinstance(cells_raw, list):
                continue
            cells = [str(c).strip() for c in cells_raw if str(c).strip()]
            if not cells:
                continue
            unit_no = cells[0]
            rent: int | None = None
            avail_date = ""
            row_sqft = sqft
            for cell in cells[1:]:
                if rent is None:
                    m = _MONEY_RE.findall(cell)
                    if m:
                        rent = money_to_int(m[0])
                        continue
                # Template C interleaves sqft before rent. If the cell is
                # pure digits and sqft from the specs blob is empty, use
                # this as the per-unit sqft.
                if not row_sqft and cell.replace(",", "").isdigit():
                    row_sqft = cell.replace(",", "")
                    continue
                if not avail_date:
                    candidate = _parse_avail_text(cell)
                    if candidate or _DATE_LIKE_RE.search(cell):
                        avail_date = candidate
                        continue
            if not unit_no and rent is None:
                continue
            out.append(
                make_unit_dict(
                    floor_plan_name=title,
                    bed_label=bed_label_from(beds, title),
                    bedrooms=str(beds) if beds is not None else "",
                    bathrooms=baths,
                    sqft=row_sqft,
                    unit_number=unit_no,
                    rent_low=rent,
                    rent_high=rent,
                    availability_status="AVAILABLE",
                    available_units="1",
                    availability_date=avail_date,
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_MARKETAPTS",
                )
            )
    return out


def parse_marketapts_template_e(
    plans: list[dict[str, object]], url: str
) -> list[dict[str, str]]:
    """Emit plan-level rows from Template E ``.floorplan-name`` cards.

    Used by sylispm.com-style subpath sites. Cards live in ``.tab-pane``
    panes (one per bedroom count) and expose plan name + a
    ``.list-group.floorplan-info`` with items like "0 Bedroom" /
    "1 Bathroom" / "504 Square Feet" / "880" (the bare-number item is
    the rent — no ``$`` prefix). No per-unit drill exists; these are
    intentionally plan-only rows.
    """
    out: list[dict[str, str]] = []
    item_bed_re = re.compile(r"(\d+|studio)\s*bedrooms?", re.IGNORECASE)
    item_bath_re = re.compile(r"(\d+(?:\.\d+)?)\s*bathrooms?", re.IGNORECASE)
    item_sqft_re = re.compile(r"(\d[\d,]*)\s*square\s*feet", re.IGNORECASE)
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        name = str(plan.get("name") or "").strip()
        items_raw = plan.get("infoItems") or []
        if not isinstance(items_raw, list):
            continue
        items = [str(x).strip() for x in items_raw if str(x).strip()]
        beds: int | None = None
        baths = ""
        sqft = ""
        rent: int | None = None
        # Walk the list items to extract specs; the bare-number item
        # (no $, no unit suffix) is the rent.
        for it in items:
            if beds is None:
                bm = item_bed_re.search(it)
                if bm:
                    v = bm.group(1)
                    beds = 0 if v.lower() == "studio" else int(v)
                    continue
            if not baths:
                bm2 = item_bath_re.search(it)
                if bm2:
                    baths = bm2.group(1)
                    continue
            if not sqft:
                sm = item_sqft_re.search(it)
                if sm:
                    sqft = sm.group(1).replace(",", "")
                    continue
            # The rent item is a bare number — no $, no other tokens
            # except optional commas. Filter out address / "Apply Now
            # For More Information" / etc.
            if rent is None:
                stripped = it.replace(",", "").strip()
                if stripped.isdigit():
                    rent_val = int(stripped)
                    # Defensive: filter implausible rent values (a stray
                    # year, an address number — must be >= 200 and < 99999).
                    if 200 <= rent_val < 99999:
                        rent = rent_val
                        continue
        if not name and rent is None:
            continue
        out.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=baths,
                sqft=sqft,
                unit_number="",
                rent_low=rent,
                rent_high=rent,
                rent_range=format_rent_range(rent, rent),
                availability_status="AVAILABLE" if rent is not None else "UNAVAILABLE",
                source_api_url=url,
                extraction_tier="TIER_1_DOM_MARKETAPTS",
            )
        )
    return out


def parse_marketapts_template_f(
    rows: list[dict[str, object]], url: str
) -> list[dict[str, str]]:
    """Emit unit-level rows from Template F ``/availability`` tables.

    223etown.com-style sites publish unit inventory as
    ``<table class="table table-hover f{N} Bedroom Apartments">`` rows
    with cells [Beds, Bath, Rent, Sq.Ft., Style (plan name), Features,
    Available date]. Each row is a unit (no explicit unit number — the
    Style + Sqft combo is the closest soft identifier; downstream
    dedup-by-(plan, rent, sqft) handles uniqueness).

    "Call For Details" in the Available column means contact-gated;
    we surface those as UNAVAILABLE so they don't masquerade as
    immediately-available unit roster.
    """
    out: list[dict[str, str]] = []
    bed_cell_re = re.compile(r"(\d+|studio)\s*bed", re.IGNORECASE)
    bath_cell_re = re.compile(r"(\d+(?:\.\d+)?)\s*bath", re.IGNORECASE)
    sqft_cell_re = re.compile(r"(\d[\d,]*)\s*sq", re.IGNORECASE)
    for row in rows:
        if not isinstance(row, dict):
            continue
        cells_raw = row.get("cells") or []
        if not isinstance(cells_raw, list):
            continue
        cells = [str(c).strip() for c in cells_raw if str(c).strip()]
        if len(cells) < 3:
            continue
        # Cell positions are stable per the 223etown table:
        # [0] Beds, [1] Bath, [2] Rent, [3] Sqft, [4] Style (plan name),
        # [5] Features (concession/upgrade), [6] Available date.
        bed_cell = cells[0]
        bath_cell = cells[1]
        rent_cell = cells[2]
        sqft_cell = cells[3] if len(cells) > 3 else ""
        plan_name = cells[4] if len(cells) > 4 else ""
        avail_cell = cells[6] if len(cells) > 6 else (cells[5] if len(cells) > 5 else "")

        bm = bed_cell_re.search(bed_cell)
        if bm:
            v = bm.group(1)
            beds: int | None = 0 if v.lower() == "studio" else int(v)
        else:
            beds = None
        bath_m = bath_cell_re.search(bath_cell)
        baths = bath_m.group(1) if bath_m else ""
        sqft_m = sqft_cell_re.search(sqft_cell)
        sqft = sqft_m.group(1).replace(",", "") if sqft_m else ""

        money = _MONEY_RE.findall(rent_cell)
        rent = money_to_int(money[0]) if money else None

        # Availability: "Call For Details" → unavailable; else date or
        # empty.
        avail_status = "AVAILABLE"
        avail_date = ""
        if avail_cell:
            if re.search(r"call\s+for", avail_cell, re.IGNORECASE):
                avail_status = "UNAVAILABLE"
            else:
                avail_date = _parse_avail_text(avail_cell)
                if not avail_date and not _DATE_LIKE_RE.search(avail_cell):
                    # Unparseable but non-empty — leave date blank, keep
                    # available-status optimistic only if rent is known.
                    if rent is None:
                        avail_status = "UNAVAILABLE"

        if rent is None and not plan_name:
            continue
        out.append(
            make_unit_dict(
                floor_plan_name=plan_name,
                bed_label=bed_label_from(beds, plan_name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=baths,
                sqft=sqft,
                unit_number="",  # no explicit unit number in the table
                rent_low=rent,
                rent_high=rent,
                availability_status=avail_status,
                available_units="1" if avail_status == "AVAILABLE" else "",
                availability_date=avail_date,
                source_api_url=url,
                extraction_tier="TIER_1_DOM_MARKETAPTS",
            )
        )
    return out


class MarketAptsAdapter:
    """Market Apartments CMS adapter — handles Templates A/B/C/D/E/F."""

    pms_name: str = "marketapts"
    _fingerprints: list[str] = [
        "marketapts.com",
        "assets.marketapts.com",
        "api.marketapts.com",
        "Powered by MarketApts",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Discover plan cards on the /floorplans page, parse Template A
        inline unit rows OR drill Template B per-plan /unit/{slug} pages,
        emit unit-level dicts.
        """
        result = AdapterResult(tier_used="TIER_1_DOM_MARKETAPTS")
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            result.confidence = 0.0
            result.errors.append("marketapts: no live page to parse")
            return result

        try:
            payload = await evaluate(_MARKETAPTS_DOM_JS)
        except Exception as exc:
            log.debug("marketapts DOM evaluate failed err=%s", exc)
            payload = None

        if not isinstance(payload, dict):
            result.confidence = 0.0
            result.errors.append("marketapts: DOM JS returned non-dict")
            return result

        template = str(payload.get("template") or "NONE")
        plans = payload.get("plans") or []
        # Template F carries its data under ``rows`` (not ``plans``) — the
        # /availability table format has no plan-level container, just a
        # flat list of unit rows. Don't bail when plans is empty if F
        # provides rows.
        f_rows = payload.get("rows") or []
        has_data = (
            (isinstance(plans, list) and len(plans) > 0)
            or (isinstance(f_rows, list) and len(f_rows) > 0)
        )
        if template == "NONE" or not has_data:
            result.confidence = 0.0
            result.errors.append(
                "marketapts: no .floorplan-block/.floorplan-item/.floor-plans-block/"
                ".floorplan-container/.floorplan-name cards and no /availability "
                "table found"
            )
            return result

        winning = self._winning_url(page, ctx)
        if template == "A":
            units = parse_marketapts_template_a(plans, winning)
        elif template == "B":
            units = parse_marketapts_template_b(plans, winning)
        elif template == "C":
            units = parse_marketapts_template_c(plans, winning)
        elif template == "D":
            # Template D's drill rows share Template B's selector and
            # cell shape; the row-level ``data-available-date`` is
            # consumed by ``parse_marketapts_template_b`` (see dataAttrs
            # branch). Listing-side title/features come from the
            # ``.floor-plans-block`` heading + details, ``startingPrice``
            # synthesised from "FROM: $X,XXX".
            units = parse_marketapts_template_b(plans, winning)
        elif template == "E":
            # Subpath tabbed accordion (sylispm-style). Plan-level only;
            # cards expose ``.floorplan-name`` + ``.list-group`` items.
            units = parse_marketapts_template_e(plans, winning)
        elif template == "F":
            # ``/availability`` data-tables (223etown-style). Plans live
            # in the per-row "Style" cell; unit roster has no explicit
            # unit_number.
            payload_rows = payload.get("rows") or []
            if isinstance(payload_rows, list):
                units = parse_marketapts_template_f(payload_rows, winning)
            else:
                units = []
        else:
            units = []

        if not units:
            result.confidence = 0.0
            result.errors.append(
                f"marketapts: template={template} parser produced no rows from {len(plans)} plans"
            )
            return result

        from ma_poc.extraction.post_process import post_process

        pp = post_process(units, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = winning
            result.tier_used = f"TIER_1_DOM_MARKETAPTS_{template}"
            result.confidence = min(0.92, 0.65 + 0.04 * pp.n_admitted)
            return result

        result.errors.append(
            f"MARKETAPTS_VALIDITY_REJECTED: {len(units)} rows failed unit_validity"
        )
        result.confidence = 0.0
        return result

    @staticmethod
    def _winning_url(page: Page, ctx: AdapterContext) -> str:
        candidate = ""
        try:
            candidate = page.url or ""
        except Exception:
            candidate = ""
        if not candidate:
            candidate = getattr(ctx, "base_url", "") or ""
        try:
            p = urlparse(candidate)
        except Exception:
            return candidate
        if not p.scheme or not p.netloc:
            return candidate
        return urlunparse((p.scheme, p.netloc, "/floorplans", "", "", ""))

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
