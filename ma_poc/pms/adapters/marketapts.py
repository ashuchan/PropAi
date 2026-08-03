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

import copy
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
    parse_published_area_pair,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.source_provenance import (
    build_unit_source_provenance,
    response_sha256,
    sanitise_source_url,
)

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

  // ── plan-name / leasing-special splitter ─────────────────────────
  // The MarketApts CMS renders the property's leasing-special banner
  // INSIDE the plan-title element:
  //   <div class="floorplan-title">A1-675<br>
  //     <span class="special red"><i …><div>For a limited time … $750.00
  //     …</div></i></span></div>
  // ``textContent`` therefore welds the banner onto the plan name
  // ("A1-675 For a limited time, you can enjoy …"). The plan name is
  // the title's own text; the banner is a child node with the CMS's
  // ``special`` class. Split them structurally (remove the node from a
  // clone) rather than by pattern-matching the copy — the banner is
  // free text and no content regex can be made safe against real plan
  // names that legitimately carry prices ("2 Bed Townhouse + $160
  // Garage"). Returns {name, special}; when stripping would leave an
  // EMPTY name the original text is kept verbatim and no special is
  // reported, so a site that styles its plan label with a ``special``
  // class can never lose its name.
  const SPECIAL_SEL = '.special, .specials, .floorplan-special';
  const TITLE = (el) => {
    if (!el) return {name: '', special: ''};
    const full = T(el);
    if (!el.querySelector(SPECIAL_SEL)) return {name: full, special: ''};
    const banner = Array.from(el.querySelectorAll(SPECIAL_SEL))
      .map(T).filter(Boolean).join(' ').trim();
    const clone = el.cloneNode(true);
    clone.querySelectorAll(SPECIAL_SEL).forEach((n) => n.remove());
    const stripped = T(clone);
    if (!stripped) return {name: full, special: ''};
    return {name: stripped, special: banner};
  };

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
    // 2026-05-26: some MarketApts SPA deployments (e.g. mountainridgemanor.com,
    // theazleeapartments.com, embarcatwestjordan.com) use server-side routing
    // that returns the homepage shell for all paths when fetched without XHR
    // headers. Adding ``X-Requested-With: XMLHttpRequest`` and the axios-style
    // Accept header triggers the server to return SSR HTML for the requested
    // path. This is safe for all other MarketApts sites (they ignore the header)
    // and for non-MarketApts sites (the self-fetch is not reached when the
    // current page already carries the plan-selector markers).
    const _xhrHeaders = {
      'Accept': 'application/json, text/plain, */*',
      'X-Requested-With': 'XMLHttpRequest'
    };
    for (const path of ['/floorplans', '/floor-plans', '/availability']) {
      try {
        const r = await fetch(location.origin + path, {credentials: 'include', headers: _xhrHeaders});
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
      const aTitle = TITLE(b.querySelector('.floorplan-name, .floorplan-title'));
      const plan = {
        template: 'A',
        beds: A(b, 'data-bedrooms'),
        baths: A(b, 'data-bathrooms'),
        name: aTitle.name,
        special: aTitle.special,
        // Floor-plan starting price often lives in a "from $X" element.
        startingPrice: T(b.querySelector('.floorplan-num, .floorplan-rent')),
        // Sandpiper-style cards publish plan area here. It may be a scalar
        // ("930 sq.ft.") or an honest family range ("1122 - 1197 sq.ft.").
        areaText: T(b.querySelector('.floorplan-sqft')),
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
  //
  // 2026-05-25 deep-probe fix (15 n_full=0 TIER_1_DOM_MARKETAPTS
  // properties): the drill-anchor matcher previously only accepted the
  // "View Available / View Details / See Available" wording. Live probe
  // of Hill Country Villas, Mountain Ridge -apts and Franklin Flats
  // shows the actual wording is "3 Units Available" / "1 Unit Available"
  // / "2 Apartments Available" — the count + noun + "Available" form.
  // Without matching it, the drill is never walked and the adapter
  // emits plan-only rows (n_full=0). Extend the regex to cover all
  // observed wordings AND match on ``href`` containing ``/unit/`` as a
  // defensive secondary signal (every probed Template B site uses that
  // path prefix).
  const bItems = Array.from(doc.querySelectorAll('.floorplan-item'));
  if (bItems.length > 0) {
    const planTasks = bItems.map((item) => {
      const drillAnchor = Array.from(item.querySelectorAll('a')).find((a) => {
        const href = a.getAttribute('href') || '';
        const t = (a.innerText || a.textContent || '').toLowerCase();
        return /\/unit\//.test(href) ||
               /view\s+available|view\s+details|see\s+available/.test(t) ||
               /\b\d+\s+units?\s+available\b/.test(t) ||
               /\b\d+\s+apartments?\s+available\b/.test(t);
      });
      const bTitle = TITLE(item.querySelector('.floorplan-title'));
      const features = T(item.querySelector('.floorplan-features'));
      const num = T(item.querySelector('.floorplan-num'));
      return {
        title: bTitle.name,
        special: bTitle.special,
        features: features,
        startingPrice: num,
        drillPath: drillAnchor ? drillAnchor.getAttribute('href') || '' : '',
      };
    });

    const planRows = [];
    for (const task of planTasks) {
      let units = [];
      if (task.drillPath) {
        // Normalise relative drill URLs (e.g. ``apartments/1-bedroom``
        // without a leading slash, observed on Brookstone Template D)
        // against ``document.baseURI`` so the fetch always resolves to
        // the site root rather than the current page's directory.
        let drillUrl = task.drillPath;
        try {
          drillUrl = new URL(drillUrl, document.baseURI).href;
        } catch (e) {
          if (drillUrl.startsWith('/')) drillUrl = location.origin + drillUrl;
        }
        try {
          const r = await fetch(drillUrl, {credentials: 'include'});
          if (r.ok) {
            const drillDoc = new DOMParser().parseFromString(await r.text(), 'text/html');
            units = Array.from(drillDoc.querySelectorAll('.unit-table-row')).map((row) => {
              // Strip mobile-only label spans before reading cell text.
              // Hill Country / sibling Template B drill rows wrap each
              // cell value with a ``.visible-xs.visible-sm`` ``<b>Unit:</b>``
              // label that shows on mobile but is hidden on desktop —
              // textContent picks it up regardless, contaminating the
              // cell with "Unit: 0827" instead of "0827". Cloning and
              // removing the labels keeps the live DOM unchanged.
              const rowClone = row.cloneNode(true);
              rowClone.querySelectorAll('.visible-xs, .visible-sm, .hidden-md, .hidden-lg').forEach((n) => n.remove());
              const cells = Array.from(rowClone.children).map((c) => T(c));
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
      try {
        drillUrl = new URL(drillUrl, document.baseURI).href;
      } catch (e) {
        if (drillUrl.startsWith('/')) drillUrl = location.origin + drillUrl;
      }
      let title = '';
      let special = '';
      let specsBlob = '';
      let units = [];
      try {
        const r = await fetch(drillUrl, {credentials: 'include'});
        if (r.ok) {
          const drillDoc = new DOMParser().parseFromString(await r.text(), 'text/html');
          const cTitle = TITLE(drillDoc.querySelector('h1'));
          title = cTitle.name;
          special = cTitle.special;
          // Drill body has "BED: X BATH: Y SQ. FEET: Z" — capture the
          // surrounding container that holds these labels.
          const bodyText = T(drillDoc.querySelector('section.unit, .section.unit')) ||
                           T(drillDoc.body).slice(0, 2000);
          specsBlob = bodyText;
          units = Array.from(drillDoc.querySelectorAll('.unit-details')).map((row) => {
            const rowClone = row.cloneNode(true);
            rowClone.querySelectorAll('.visible-xs, .visible-sm, .hidden-md, .hidden-lg').forEach((n) => n.remove());
            const cells = Array.from(rowClone.children).map((c) => T(c));
            return {cells: cells, dataAttrs: Object.assign({}, row.dataset || {})};
          });
        }
      } catch (e) { /* drill failure → skip this plan */ }
      planRows.push({title, special, specsBlob, drillPath: href, units, template: 'C'});
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
      const dTitle = TITLE(block.querySelector('h1, h2, h3, h4, h5, h6'));
      const heading = dTitle.name;
      const detailsBlock = T(block.querySelector('.floor-plans-block-details, .list-group'));
      // Pull a starting price out of the body text — Template D often
      // states "FROM: $X,XXX" inline.
      const bodyText = T(block);
      const fromMatch = bodyText.match(/FROM\s*:?\s*\$\s*([\d,]+)/i);
      const startingPrice = fromMatch ? '$' + fromMatch[1] : '';
      return {
        title: heading,
        special: dTitle.special,
        features: detailsBlock,
        startingPrice: startingPrice,
        drillPath: drillAnchor ? drillAnchor.getAttribute('href') || '' : '',
      };
    });

    const planRows = [];
    for (const task of planTasks) {
      let units = [];
      if (task.drillPath) {
        // Template D drill URLs sometimes lack a leading slash
        // (Brookstone Apartments emits ``apartments/1-bedroom``); use
        // ``new URL`` against ``document.baseURI`` so resolution is
        // explicit and origin-anchored regardless of the listing page
        // URL.
        let drillUrl = task.drillPath;
        try {
          drillUrl = new URL(drillUrl, document.baseURI).href;
        } catch (e) {
          if (drillUrl.startsWith('/')) drillUrl = location.origin + drillUrl;
        }
        try {
          const r = await fetch(drillUrl, {credentials: 'include'});
          if (r.ok) {
            const drillDoc = new DOMParser().parseFromString(await r.text(), 'text/html');
            units = Array.from(drillDoc.querySelectorAll('.unit-table-row')).map((row) => {
              const rowClone = row.cloneNode(true);
              rowClone.querySelectorAll('.visible-xs, .visible-sm, .hidden-md, .hidden-lg').forEach((n) => n.remove());
              const cells = Array.from(rowClone.children).map((c) => T(c));
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
      const eTitle = TITLE(nameEl);
      planRows.push({
        template: 'E',
        name: eTitle.name,
        special: eTitle.special,
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

  // ── Template G — plain-text <strong>Name:</strong> plan cards ──
  // 223etown-style /floor-plans: plans rendered as "<strong>Name:</strong> X
  // <strong>Square Feet:</strong> Y <strong>Starting Rent:</strong> $Z" with
  // no class-based plan container. Plan-level only (unit roster on
  // /availability = Template F). Emitted Template-B-shaped for reuse.
  const gPlans = [];
  let gCur = null;
  for (const s of Array.from(doc.querySelectorAll('strong'))) {
    const lab = T(s).replace(/:$/, '').toLowerCase();
    if (!['name', 'square feet', 'starting rent'].includes(lab)) continue;
    const sib = s.nextSibling;
    const val = sib ? (sib.textContent || sib.nodeValue || '').replace(/\s+/g, ' ').trim() : '';
    if (lab === 'name') {
      if (gCur) gPlans.push(gCur);
      gCur = {template: 'B', title: val, features: '', startingPrice: '', units: []};
    } else if (gCur && lab === 'square feet') {
      gCur.features = 'SQ. FEET: ' + val;
    } else if (gCur && lab === 'starting rent') {
      gCur.startingPrice = val;
    }
  }
  if (gCur) gPlans.push(gCur);
  if (gPlans.length > 0) return {template: 'G', plans: gPlans};

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
# Defensive Python-side fallback for the mobile-only label spans the JS
# strips before serialising cells. If a site renders the labels in a
# DOM shape the JS guard doesn't catch (e.g. ``<small class="d-md-none">``
# variants), the cell text comes through as ``"Unit: 0827"`` instead of
# ``"0827"``. This regex strips a leading single-word label (one of the
# known columns) plus its colon so positional parsing keeps working.
_DRILL_CELL_LABEL_RE = re.compile(
    r"^\s*(?:unit|rent|available|special|features?|sq\s*\.?\s*ft|sqft|sq\s*ft|sqfeet)\s*:\s*",
    re.IGNORECASE,
)


def _strip_drill_cell_label(cell: str) -> str:
    """Strip a mobile-only label prefix ("Unit:", "Rent:", etc.) from a
    drill-row cell value. Defensive — the JS layer already removes the
    ``.visible-xs`` / ``.visible-sm`` label spans on most sites; this
    handles edge cases where the label class doesn't match the JS
    selector list. Called for every drill cell in Templates B / C / D.
    """
    if not cell:
        return cell
    return _DRILL_CELL_LABEL_RE.sub("", cell, count=1).strip()


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
) -> list[dict[str, Any]]:
    """Emit unit-level rows from Template A ``.floorplan-block`` cards.

    One row per ``.floorplan-unit-single`` inside each plan card. Plan
    metadata (beds, baths) flows from ``data-bedrooms`` / ``data-bathrooms``
    on the parent card; sqft/baths fallback from the plan-name text if
    needed. Each unit row carries the unit number (from DOM text),
    its own price (``data-price``), and availability date (``data-when``,
    which is ISO and authoritative).
    """
    out: list[dict[str, Any]] = []
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        plan_name = str(plan.get("name") or "").strip()
        conc_text, conc_src = _ma_plan_concession(
            str(plan.get("special") or "").strip()
        )
        plan_beds_str = str(plan.get("beds") or "").strip()
        plan_baths_str = str(plan.get("baths") or "").strip()
        try:
            plan_beds: int | None = int(plan_beds_str) if plan_beds_str else None
        except (TypeError, ValueError):
            plan_beds = None
        area_text = str(plan.get("areaText") or "").strip()
        area_low, area_high = parse_published_area_pair(area_text)
        plan_sqft = (
            str(area_low)
            if area_low is not None and area_high is not None and area_low == area_high
            else ""
        )
        area_fields: dict[str, Any] = {}
        if area_low is not None and area_high is not None:
            area_fields = {
                "area_low": area_low,
                "area_high": area_high,
                "area_range": (
                    str(area_low)
                    if area_low == area_high
                    else f"{area_low}-{area_high}"
                ),
                "area_range_raw": area_text,
                "area_provenance": (
                    "published_plan_exact"
                    if area_low == area_high
                    else "published_plan_range_no_midpoint"
                ),
                "area_source_url": url,
                "source_record_locator": f"marketapts_template_a_plan:{plan_name}",
            }
        # Plan-level "FROM $X" — useful only as a fallback if a unit row
        # somehow lacks data-price.
        sp_text = str(plan.get("startingPrice") or "")
        sp_money = _MONEY_RE.findall(sp_text)
        plan_floor_price = money_to_int(sp_money[0]) if sp_money else None

        units = plan.get("units") or []
        if not isinstance(units, list) or not units:
            # Plan-level fallback when no unit roster present.
            if plan_name or plan_floor_price is not None:
                row = make_unit_dict(
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
                    floor_plan_name_provenance="marketapts.plan.name",
                    concession_text=conc_text,
                    concession_source=conc_src,
                )
                row.update(area_fields)
                out.append(row)
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
            row = make_unit_dict(
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
                floor_plan_name_provenance="marketapts.plan.name",
                concession_text=conc_text,
                concession_source=conc_src,
            )
            row.update(area_fields)
            out.append(row)
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
        conc_text, conc_src = _ma_plan_concession(
            str(plan.get("special") or "").strip()
        )
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
                        concession_text=conc_text,
                        concession_source=conc_src,
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
            # Strip empty cells AND any mobile-only "Label:" prefix the
            # JS layer didn't already remove. Apply-button cell sometimes
            # interleaves; the empty filter drops it.
            cells = [
                _strip_drill_cell_label(str(c).strip())
                for c in cells_raw
                if _strip_drill_cell_label(str(c).strip())
            ]
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
                    concession_text=conc_text,
                    concession_source=conc_src,
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
        conc_text, conc_src = _ma_plan_concession(
            str(plan.get("special") or "").strip()
        )
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
                        concession_text=conc_text,
                        concession_source=conc_src,
                    )
                )
            continue

        for u in units:
            if not isinstance(u, dict):
                continue
            cells_raw = u.get("cells") or []
            if not isinstance(cells_raw, list):
                continue
            cells = [
                _strip_drill_cell_label(str(c).strip())
                for c in cells_raw
                if _strip_drill_cell_label(str(c).strip())
            ]
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
                    concession_text=conc_text,
                    concession_source=conc_src,
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
        conc_text, conc_src = _ma_plan_concession(
            str(plan.get("special") or "").strip()
        )
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
                concession_text=conc_text,
                concession_source=conc_src,
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


# ─────────────────────────────────────────────────────────────────────────
# Fetch-only (page=None) recovery path
#
# ``MarketAptsAdapter.extract`` originally required a live Playwright page:
# the whole DOM cascade + per-plan drill fetch ran inside ``page.evaluate``.
# But the fetch ladder frequently serves a MarketApts site through the
# curl_cffi / Unlocker CF-bypass path — a plain HTTP GET that persists the
# SSR ``body`` yet leaves ``page=None``. In that case the adapter bailed
# ("no live page to parse") and emitted 0 units even though the full
# floorplan markup was in hand. The functions below re-derive the exact same
# ``{template, plans, rows}`` payload from the persisted body in pure Python
# (a faithful mirror of ``_MARKETAPTS_DOM_JS``), so the existing
# ``parse_marketapts_template_*`` transforms run unchanged. Per-plan ``/unit``
# drills route through ``PROBE_PROXY_URL`` (residential) via
# ``_probe.probe_get`` so the target site never sees the runner's own IP.
# ─────────────────────────────────────────────────────────────────────────

# Plan-card markers that mean "this document already carries floorplan data"
# (mirror of the ``hasMarker`` check in the JS blob).
_MA_PLAN_MARKERS: tuple[str, ...] = (
    ".floorplan-block",
    ".floorplan-item",
    ".floorplan-unit-single",
    ".floor-plans-block",
    ".floorplan-name",
)
# Extra markers accepted from a self-fetched candidate document.
_MA_CANDIDATE_MARKERS: tuple[str, ...] = _MA_PLAN_MARKERS + (
    ".floorplan-container",
    "table.table-hover",
)
# Some MarketApts SPA deployments return the homepage shell for every path
# unless the request carries these XHR headers (see JS blob 2026-05-26 note).
_MA_XHR_HEADERS: dict[str, str] = {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}
_MA_B_DRILL_TEXT = re.compile(
    r"view\s+available|view\s+details|see\s+available"
    r"|\b\d+\s+units?\s+available\b|\b\d+\s+apartments?\s+available\b",
    re.I,
)
_MA_D_DRILL_TEXT = re.compile(
    r"apartments?\s+available|view\s+available|view\s+details", re.I
)

# CSS classes the MarketApts CMS uses for the leasing-special banner it
# nests INSIDE the plan-title element. A CLASS match, never a content
# match — see ``_ma_split_title`` for why that distinction matters.
_MA_SPECIAL_SELECTOR = ".special, .specials, .floorplan-special"


def _ma_txt(el: Any) -> str:
    """Mirror of JS ``T(el)``: collapsed, trimmed textContent."""
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text()).strip()


def _ma_split_title(el: Any) -> tuple[str, str]:
    """Split a MarketApts plan-title node into ``(plan_name, special)``.

    Python mirror of the JS ``TITLE(el)`` helper. The CMS renders the
    property's leasing-special banner as a child of the plan-title
    element::

        <div class="floorplan-title">A1-675<br>
          <span class="special red"><i …><div>For a limited time … up to
          $750.00 …</div></i></span></div>

    A plain ``get_text()`` therefore welds the banner onto the plan name.
    The split is STRUCTURAL — the banner node is removed from a clone and
    the remainder is the name. It is deliberately NOT a content regex:
    real plan names legitimately carry prices and marketing-ish words
    ("2 Bed 2.5 bath Townhouse (2UP) + $160 Garage"), so any pattern over
    the copy would eventually eat one.

    Fail-safe: if removing the banner would leave an EMPTY name (a site
    that styles the plan label itself with a ``special`` class), the
    original full text is returned unchanged and no special is reported.
    Nothing can be lost by this helper that was not already lost.
    """
    if el is None:
        return "", ""
    full = _ma_txt(el)
    banner_nodes = el.select(_MA_SPECIAL_SELECTOR)
    if not banner_nodes:
        return full, ""
    banner = " ".join(t for t in (_ma_txt(n) for n in banner_nodes) if t).strip()
    clone = copy.copy(el)
    for node in clone.select(_MA_SPECIAL_SELECTOR):
        node.decompose()
    stripped = _ma_txt(clone)
    if not stripped:
        return full, ""
    return stripped, banner


def _ma_plan_concession(special: str) -> tuple[str | None, str | None]:
    """Route a stripped plan-card special banner to the concession field.

    Returns ``(concession_text, concession_source)`` — ``(None, None)``
    when the banner should not be promoted.

    The banner is promoted to ``concession_text`` only when the SHARED
    offer classifier (``ma_poc.core.offer_extract``) recognises it as a
    leasing offer — either it names an offer type, or it names both a
    target and a value. The decision is delegated to that module on
    purpose: this adapter adds no content regex of its own.

    Two reasons the gate exists instead of "always promote":

      * Not every ``.special`` banner is a concession. Live corpus
        (2026-07-28, 26 banners over 9 MarketApts properties) includes
        "Call for pricing and availability. …", "AMAZING LOFT
        FLOORPLAN! MOVE IN TODAY!" and "Great Rates! Join The Franklin
        Flats Community Today!" — marketing chatter with no offer.
      * A unit-level ``concession_text`` OUTRANKS the property-level
        banner in ``enrich_unit_concession_fields``. Writing chatter
        here would EVICT a real, richer property-level concession —
        e.g. mountainridge-apts plan C3's banner is "Call for pricing
        and availability", while the property banner carries a real
        "$400 off move in" ( concession_value=400.0 ).

    When the gate rejects a banner the concession fields are left
    untouched, so the property-level backfill still applies. The name
    is still cleaned either way — the split and the routing are
    independent.
    """
    if not special:
        return None, None
    try:
        from ma_poc.core.offer_extract import extract_offer

        fields = extract_offer(special)
    except Exception:  # pragma: no cover — classifier is best-effort
        return None, None
    if not isinstance(fields, dict):
        return None, None
    is_offer = bool(fields.get("offer_type")) or bool(
        fields.get("offer_value") and fields.get("offer_target")
    )
    if not is_offer:
        return None, None
    return special, "marketapts_plan_special"


def _ma_origin(url: str) -> str:
    try:
        p = urlparse(url)
    except Exception:
        return ""
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


def _ma_row_cells(row: Any) -> tuple[list[str], dict[str, str]]:
    """Direct-child cell texts (mobile-only label spans removed) + camelCased
    ``data-*`` attrs — mirror of the JS ``rowClone`` cell mapping + ``dataset``.
    """
    clone = copy.copy(row)
    for lab in clone.select(".visible-xs, .visible-sm, .hidden-md, .hidden-lg"):
        lab.decompose()
    cells = [_ma_txt(c) for c in clone.find_all(recursive=False)]
    data_attrs: dict[str, str] = {}
    for key, val in (row.attrs or {}).items():
        if not key.startswith("data-"):
            continue
        parts = key[len("data-"):].split("-")
        camel = parts[0] + "".join(p.title() for p in parts[1:])
        data_attrs[camel] = val if isinstance(val, str) else " ".join(val)
    return cells, data_attrs


def _ma_find_drill(item: Any, *, kind: str) -> str:
    """Return the drill-anchor href for a plan card (mirror of the JS
    ``drillAnchor`` finder). ``kind`` is ``"unit"`` (Templates B) or
    ``"apartments"`` (Template D)."""
    for a in item.select("a"):
        href = a.get("href", "") or ""
        text = _ma_txt(a).lower()
        if kind == "unit":
            if "/unit/" in href or _MA_B_DRILL_TEXT.search(text):
                return href
        else:
            if "/apartments/" in href or _MA_D_DRILL_TEXT.search(text):
                return href
    return ""


def _ma_template_a_unit(u: Any) -> dict[str, str]:
    text = _ma_txt(u)
    m = re.search(r"UNIT\s*[#:]?\s*([A-Z0-9\-]+)", text, re.I)
    return {
        "unitNumber": m.group(1) if m else "",
        "dataWhen": u.get("data-when", "") or "",
        "dataPrice": u.get("data-price", "") or "",
        "dataBeds": u.get("data-bedrooms", "") or "",
        "dataBaths": u.get("data-bathrooms", "") or "",
        "priceText": _ma_txt(u.select_one(".unit-price-single")),
        "availText": _ma_txt(u.select_one(".unit-available-single")),
    }


def _ma_template_g_plans(doc: Any) -> list[dict[str, Any]]:
    """Template G — plain-text plan cards labelled ``<strong>Name:</strong>``
    / ``<strong>Square Feet:</strong>`` / ``<strong>Starting Rent:</strong>``
    (observed on 223etown.com/floor-plans). Plan-level only — the unit roster
    for these sites lives on ``/availability`` (Template F). Emitted as
    Template-B-shaped dicts so ``parse_marketapts_template_b`` handles them
    with no new transform.
    """
    plans: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for s in doc.find_all("strong"):
        label = _ma_txt(s).rstrip(":").lower()
        if label not in ("name", "square feet", "starting rent"):
            continue
        sib = s.next_sibling
        if sib is None:
            val = ""
        elif isinstance(sib, str):
            val = sib.strip()
        else:
            val = _ma_txt(sib)
        if label == "name":
            if cur is not None:
                plans.append(cur)
            cur = {
                "template": "B",
                "title": val,
                "features": "",
                "startingPrice": "",
                "units": [],
            }
        elif cur is not None and label == "square feet":
            cur["features"] = f"SQ. FEET: {val}"
        elif cur is not None and label == "starting rent":
            cur["startingPrice"] = val
    if cur is not None:
        plans.append(cur)
    return plans


def marketapts_static_payload(
    html: str,
    base_url: str,
    drill_fetch: Callable[[str, bool], str | None],
) -> dict[str, Any]:
    """Pure-Python mirror of ``_MARKETAPTS_DOM_JS`` for the ``page=None`` path.

    Args:
        html: The persisted SSR response body.
        base_url: The property URL (used to resolve relative drill hrefs and
            to derive the origin for self-fetches).
        drill_fetch: ``(url, xhr) -> html | None`` — fetches a URL's HTML.
            ``xhr=True`` sends the XHR headers some SPA deployments require.
            Returns None on any failure (plan-level fallback then applies).

    Returns:
        ``{"template": <A|B|C|D|E|F|NONE>, "plans": [...], "rows": [...]}`` in
        the exact shape the ``parse_marketapts_template_*`` transforms expect.
    """
    soup = BeautifulSoup(html, "lxml")
    doc: Any = soup
    origin = _ma_origin(base_url)

    # ── locate the floorplans document (mirror of the JS self-fetch) ──
    if not any(soup.select_one(m) for m in _MA_PLAN_MARKERS):
        for path in ("/floorplans", "/floor-plans", "/availability"):
            if not origin:
                break
            cand_html = drill_fetch(origin + path, True)
            if not cand_html:
                continue
            cand = BeautifulSoup(cand_html, "lxml")
            if any(cand.select_one(m) for m in _MA_CANDIDATE_MARKERS):
                doc = cand
                break

    def _drill_rows(drill_path: str, selector: str) -> list[dict[str, Any]]:
        if not drill_path:
            return []
        drill_html = drill_fetch(urljoin(base_url, drill_path), False)
        if not drill_html:
            return []
        ddoc = BeautifulSoup(drill_html, "lxml")
        rows: list[dict[str, Any]] = []
        for row in ddoc.select(selector):
            cells, data_attrs = _ma_row_cells(row)
            rows.append({"cells": cells, "dataAttrs": data_attrs})
        return rows

    # ── Template A — inline unit rows ──
    a_blocks = doc.select(".floorplan-block")
    if a_blocks and doc.select_one(".floorplan-unit-single"):
        plans = []
        for b in a_blocks:
            a_name, a_special = _ma_split_title(
                b.select_one(".floorplan-name, .floorplan-title")
            )
            plans.append(
                {
                    "template": "A",
                    "beds": b.get("data-bedrooms", "") or "",
                    "baths": b.get("data-bathrooms", "") or "",
                    "name": a_name,
                    "special": a_special,
                    "startingPrice": _ma_txt(
                        b.select_one(".floorplan-num, .floorplan-rent")
                    ),
                    "areaText": _ma_txt(b.select_one(".floorplan-sqft")),
                    "units": [
                        _ma_template_a_unit(u)
                        for u in b.select(".floorplan-unit-single")
                    ],
                }
            )
        return {"template": "A", "plans": plans}

    # ── Template B — .floorplan-item + /unit/ drill ──
    b_items = doc.select(".floorplan-item")
    if b_items:
        plans = []
        for item in b_items:
            drill_path = _ma_find_drill(item, kind="unit")
            b_title, b_special = _ma_split_title(item.select_one(".floorplan-title"))
            plans.append(
                {
                    "template": "B",
                    "title": b_title,
                    "special": b_special,
                    "features": _ma_txt(item.select_one(".floorplan-features")),
                    "startingPrice": _ma_txt(item.select_one(".floorplan-num")),
                    "drillPath": drill_path,
                    "units": _drill_rows(drill_path, ".unit-table-row"),
                }
            )
        return {"template": "B", "plans": plans}

    # ── Template C — .floorplan-container gallery + /unit/ drill ──
    c_containers = doc.select(".floorplan-container")
    if c_containers:
        hrefs: list[str] = []
        for cont in c_containers:
            for a in cont.select('a[href*="/unit/"]'):
                href = a.get("href", "") or ""
                if href and href not in hrefs:
                    hrefs.append(href)
        if not hrefs:
            return {"template": "NONE", "plans": []}
        plans = []
        for href in hrefs:
            title = ""
            special = ""
            specs = ""
            units: list[dict[str, Any]] = []
            drill_html = drill_fetch(urljoin(base_url, href), False)
            if drill_html:
                ddoc = BeautifulSoup(drill_html, "lxml")
                title, special = _ma_split_title(ddoc.select_one("h1"))
                specs = _ma_txt(
                    ddoc.select_one("section.unit, .section.unit")
                ) or _ma_txt(ddoc.body)[:2000]
                for row in ddoc.select(".unit-details"):
                    cells, data_attrs = _ma_row_cells(row)
                    units.append({"cells": cells, "dataAttrs": data_attrs})
            plans.append(
                {
                    "title": title,
                    "special": special,
                    "specsBlob": specs,
                    "drillPath": href,
                    "units": units,
                    "template": "C",
                }
            )
        return {"template": "C", "plans": plans}

    # ── Template D — .floor-plans-block + /apartments/ drill ──
    d_blocks = doc.select(".floor-plans-block")
    if d_blocks:
        plans = []
        for block in d_blocks:
            drill_path = _ma_find_drill(block, kind="apartments")
            from_m = re.search(
                r"FROM\s*:?\s*\$\s*([\d,]+)", _ma_txt(block), re.I
            )
            d_title, d_special = _ma_split_title(
                block.select_one("h1, h2, h3, h4, h5, h6")
            )
            plans.append(
                {
                    "template": "D",
                    "title": d_title,
                    "special": d_special,
                    "features": _ma_txt(
                        block.select_one(".floor-plans-block-details, .list-group")
                    ),
                    "startingPrice": ("$" + from_m.group(1)) if from_m else "",
                    "drillPath": drill_path,
                    "units": _drill_rows(drill_path, ".unit-table-row"),
                }
            )
        return {"template": "D", "plans": plans}

    # ── Template E — tabbed accordion, plan-only ──
    e_cards = doc.select(".floorplan-name")
    if e_cards:
        seen: set[int] = set()
        plans = []
        for name_el in e_cards:
            card = name_el.find_parent(class_="card") or name_el.parent
            if card is None or id(card) in seen:
                continue
            seen.add(id(card))
            items = [
                t
                for t in (
                    _ma_txt(li)
                    for li in card.select(".list-group-item, .floorplan-info li")
                )
                if t
            ]
            e_name, e_special = _ma_split_title(name_el)
            plans.append(
                {
                    "template": "E",
                    "name": e_name,
                    "special": e_special,
                    "infoItems": items,
                }
            )
        if plans:
            return {"template": "E", "plans": plans}

    # ── Template F — /availability data tables ──
    avail_doc: Any = None
    if doc.select_one("table.table-hover"):
        avail_doc = doc
    elif origin:
        cand_html = drill_fetch(origin + "/availability", False)
        if cand_html:
            cand = BeautifulSoup(cand_html, "lxml")
            if cand.select_one("table.table-hover"):
                avail_doc = cand
    if avail_doc is not None:
        rows = []
        for table in avail_doc.select("table.table-hover"):
            table_name = " ".join(table.get("class", []) or [])
            for tr in table.select("tbody tr"):
                cells, data_attrs = _ma_row_cells(tr)
                rows.append(
                    {"cells": cells, "tableName": table_name, "dataAttrs": data_attrs}
                )
        if rows:
            return {"template": "F", "plans": [], "rows": rows}

    # ── Template G — plain-text <strong>Name:</strong> plan cards ──
    # Fallback for the plain-text plan layout (223etown-style /floor-plans)
    # that none of the class-based selectors match. Plan-level only.
    g_plans = _ma_template_g_plans(doc)
    if g_plans:
        return {"template": "G", "plans": g_plans}

    return {"template": "NONE", "plans": []}


def _ma_payload_has_data(payload: Any) -> bool:
    """True when a payload carries a usable template + at least one plan/row."""
    if not isinstance(payload, dict):
        return False
    if str(payload.get("template") or "NONE") == "NONE":
        return False
    plans = payload.get("plans") or []
    rows = payload.get("rows") or []
    return (isinstance(plans, list) and len(plans) > 0) or (
        isinstance(rows, list) and len(rows) > 0
    )


def marketapts_payload_to_units(payload: dict[str, Any], url: str) -> list[dict[str, Any]]:
    """Dispatch a ``{template, plans, rows}`` payload to the matching
    ``parse_marketapts_template_*`` transform. Shared by the live-page and
    the fetch-only paths so both produce identical rows."""
    template = str(payload.get("template") or "NONE")
    plans = payload.get("plans") or []
    if not isinstance(plans, list):
        plans = []
    if template == "A":
        return parse_marketapts_template_a(plans, url)
    if template == "B":
        return parse_marketapts_template_b(plans, url)
    if template == "C":
        return parse_marketapts_template_c(plans, url)
    if template == "D":
        # Template D drills share Template B's row shape.
        return parse_marketapts_template_b(plans, url)
    if template == "E":
        return parse_marketapts_template_e(plans, url)
    if template == "F":
        rows = payload.get("rows") or []
        return parse_marketapts_template_f(rows, url) if isinstance(rows, list) else []
    if template == "G":
        # Template G plans are Template-B-shaped (plan-level; the unit roster
        # for these sites lives on /availability = Template F).
        return parse_marketapts_template_b(plans, url)
    return []


def _ma_ctx_body(ctx: AdapterContext) -> str:
    """Decode the persisted response body from the AdapterContext, or ''."""
    fetch_result = getattr(ctx, "fetch_result", None)
    body = getattr(fetch_result, "body", None) if fetch_result is not None else None
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return str(body)


def _ma_residential_drill_fetch(url: str, xhr: bool = False) -> str | None:
    """Fetch a drill/self-fetch URL through ``PROBE_PROXY_URL`` (residential)
    via curl_cffi, so the target never sees the runner's own IP. Returns the
    response HTML, or None on any failure (→ plan-level fallback)."""
    try:
        from ma_poc.pms.adapters._probe import probe_get
    except ImportError:
        return None
    kwargs: dict[str, Any] = {"timeout": 30}
    if xhr:
        kwargs["headers"] = _MA_XHR_HEADERS
    try:
        resp = probe_get(url, unlocker=False, **kwargs)
    except Exception as exc:
        log.debug("marketapts drill fetch failed url=%s err=%s", url, exc)
        return None
    text = getattr(resp, "text", None)
    status = int(getattr(resp, "status_code", 0) or 0)
    if not text or status >= 400:
        return None
    return str(text)


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

        # Path 1 — live Playwright page: run the in-browser DOM cascade.
        evaluate = getattr(page, "evaluate", None)
        payload: Any = None
        if callable(evaluate):
            try:
                payload = await evaluate(_MARKETAPTS_DOM_JS)
            except Exception as exc:
                log.debug("marketapts DOM evaluate failed err=%s", exc)
                payload = None

        # Path 2 — fetch-only fallback. When there is no live page (the
        # curl_cffi / Unlocker CF-bypass path serves the body but leaves
        # page=None) — or the live page produced nothing usable — re-derive
        # the payload from the persisted response body with a pure-Python
        # mirror of the DOM JS. Per-plan ``/unit`` drills route through
        # PROBE_PROXY_URL (residential) so the target never sees the runner's
        # own IP.
        if not _ma_payload_has_data(payload):
            body = _ma_ctx_body(ctx)
            if body:
                try:
                    static_payload = marketapts_static_payload(
                        body,
                        getattr(ctx, "base_url", "") or "",
                        _ma_residential_drill_fetch,
                    )
                except Exception as exc:
                    log.debug("marketapts static parse failed err=%s", exc)
                    static_payload = {"template": "NONE", "plans": []}
                if _ma_payload_has_data(static_payload):
                    payload = static_payload
                    log.debug(
                        "marketapts: recovered via static body path (page=None)"
                    )

        if not _ma_payload_has_data(payload):
            result.confidence = 0.0
            result.errors.append(
                "marketapts: no live page and no parseable body — no "
                ".floorplan-block/.floorplan-item/.floor-plans-block/"
                ".floorplan-container/.floorplan-name cards and no "
                "/availability table found"
            )
            return result

        assert isinstance(payload, dict)
        template = str(payload.get("template") or "NONE")
        plans = payload.get("plans") or []
        winning = self._winning_url(page, ctx)
        units = marketapts_payload_to_units(payload, winning)

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
            # Tier label: append ``_UNIT_LEVEL`` when at least one
            # admitted row carries a unit_number — i.e. the per-plan
            # drill walked successfully. Without the suffix the row is
            # plan-level only (every unit_number empty). Downstream
            # reporting uses this to separate the "drill walked"
            # outcome from the "plan-only fallback" outcome introduced
            # by the 2026-05-25 deep-probe fix.
            has_unit_level = any(
                str(row.get("unit_number") or "").strip()
                for row in pp.admitted
            )
            suffix = "_UNIT_LEVEL" if has_unit_level else ""
            result.tier_used = f"TIER_1_DOM_MARKETAPTS_{template}{suffix}"
            result.confidence = min(0.92, 0.65 + 0.04 * pp.n_admitted)
            area_rows = [row for row in result.units if row.get("area_low") is not None]
            if area_rows:
                raw_html = _ma_ctx_body(ctx)
                source_body: Any = (
                    raw_html
                    if raw_html and "floorplan-sqft" in raw_html.casefold()
                    else payload
                )
                source_kind = (
                    "marketapts_template_a_html"
                    if isinstance(source_body, str)
                    else "marketapts_dom_extraction_payload"
                )
                source_hash = response_sha256(source_body)
                safe_winning = sanitise_source_url(winning)
                for row in area_rows:
                    row["source_response_sha256"] = source_hash
                    row["source_response_url"] = safe_winning
                result.html_responses.append(
                    {
                        "url": winning,
                        "status": 200,
                        "body": source_body,
                        "content_type": (
                            "text/html"
                            if isinstance(source_body, str)
                            else "application/json"
                        ),
                        "response_kind": source_kind,
                        "via": "marketapts_template_a",
                        "identity": {
                            "status": "CONFIGURED_PROPERTY_ROUTE",
                            "configured_property_id": str(ctx.property_id or ""),
                            "admitted_field_count": len(area_rows),
                        },
                    }
                )
                result.unit_source_provenance.append(
                    build_unit_source_provenance(
                        provider="marketapts",
                        source_url=winning,
                        body=source_body,
                        unit_count=len(area_rows),
                        identity={
                            "status": "CONFIGURED_PROPERTY_ROUTE",
                            "configured_property_id": str(ctx.property_id or ""),
                        },
                        response_kind=source_kind,
                    )
                )
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
