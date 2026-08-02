"""RentCafe "floorplans-layout-tab" theme — bedroom-tab listing + per-plan
drill at ``/floorplans/{slug}``.

Some RentCafe-tagged sites use a "layout-tab" theme where the
``/floorplans`` page is a bedroom-tab listing (Studio / 1 Bed / 2 Bed)
and clicking a plan navigates to ``/floorplans/{bedroom-or-plan-slug}``
which exposes the unit-level roster.

Verified live 2026-05-21 on:
  - www.tudorplaceapts.com/floorplans → drill ``/floorplans/two-bedrooms``
    has tabular rows: ``#840_09 900 $1,765.00 Specials Available``
  - www.campobassoapts.com/floorplans → drill ``/floorplans/studio``
    has text-based: ``Apartment: # F_205 Starting at: $1,425.00``

Detection: the ``.page-content-floorplans.floorplans-layout-tab`` class
on the listing page is unique to this theme (does not collide with
Market Apartments, RentCafe modern unit-roster theme, or RealPage CWS).

Extraction:
  1. On the /floorplans listing page, find every drill anchor matching
     ``/floorplans/{slug}`` pattern.
  2. Fetch each drill in-session.
  3. Parse the drill's body text for unit rows. Two formats are
     tolerated by the same regex set:
       * Tudorplace tabular: ``#840_09 900 $1,765.00 Specials Available``
       * Campobasso text:    ``Apartment: # F_205 Starting at: $1,425.00``
  4. Join unit rows back to the plan via the drill slug.

Plan-level fallback when a drill has no parseable units (e.g. plans
listed but Waitlist).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse, urlunparse

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters._rentcafe_availability import (
    availability_by_applyga_unit,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)


def is_rentcafe_layout_tab_html(html: str | bytes | None) -> bool:
    """Return whether *html* carries the exact RentCafe layout-tab marker.

    Both class tokens are required.  Either token by itself appears on other
    RentCafe themes and is not sufficient authority to spend the bounded
    ``/availableunits`` / detail-page recovery probes.
    """
    if isinstance(html, bytes):
        text = html.decode("utf-8", errors="replace")
    elif isinstance(html, str):
        text = html
    else:
        return False
    lowered = text.lower()
    return (
        "page-content-floorplans" in lowered
        and "floorplans-layout-tab" in lowered
    )


# Identifies a /floorplans/{slug} drill URL on the SAME origin.
_DRILL_HREF_RE = re.compile(r"^/floorplans/[^/?#][^/?#]*/?$")

_RENTCAFE_LT_DOM_JS = r"""
async () => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  // The listing page exposes .page-content-floorplans.floorplans-layout-tab.
  // If we're not there, try /floorplans first.
  let doc = document;
  let onListingPage = !!document.querySelector('.page-content-floorplans.floorplans-layout-tab');
  if (!onListingPage) {
    try {
      const r = await fetch(location.origin + '/floorplans', {credentials: 'include'});
      if (r.ok) {
        const candidate = new DOMParser().parseFromString(await r.text(), 'text/html');
        if (candidate.querySelector('.page-content-floorplans.floorplans-layout-tab')) {
          doc = candidate;
          onListingPage = true;
        }
      }
    } catch (e) { /* fall through */ }
  }
  if (!onListingPage) {
    return {ok: false, reason: 'no .page-content-floorplans.floorplans-layout-tab listing'};
  }
  // Collect per-plan drill hrefs (/floorplans/{slug}) — exclude the
  // listing itself.
  const drillSet = new Set();
  const drillItems = [];
  const anchors = Array.from(doc.querySelectorAll('a[href]'));
  for (const a of anchors) {
    const href = a.getAttribute('href') || '';
    const path = href.startsWith('http')
      ? (function() { try { return new URL(href).pathname; } catch (e) { return ''; } })()
      : href.split('?')[0].split('#')[0];
    if (/^\/floorplans\/[^/?#][^/?#]*\/?$/.test(path) && !drillSet.has(path)) {
      drillSet.add(path);
      drillItems.push({
        path: path,
        anchorText: T(a),
      });
    }
  }
  if (drillItems.length === 0) {
    return {ok: false, reason: 'no /floorplans/{slug} drill anchors on listing'};
  }
  // For each drill, fetch and capture the body innerText.
  const plans = [];
  for (const item of drillItems) {
    let bodyText = '';
    let h1Text = '';
    try {
      const r = await fetch(location.origin + item.path, {credentials: 'include'});
      if (r.ok) {
        const drillDoc = new DOMParser().parseFromString(await r.text(), 'text/html');
        h1Text = T(drillDoc.querySelector('h1'));
        // Prefer the main content area when present; fall back to body.
        const main = drillDoc.querySelector('main, .page-content-availableunits, .page-content-floorplans');
        bodyText = T(main || drillDoc.body);
        const seenScopes = new Set();
        const unitScopes = [];
        for (const el of drillDoc.querySelectorAll('[onclick*="applyGAClick"]')) {
          const scope = el.closest(
            'tr, .unit-container, .available-unit, .available-unit-card, .unit-card, .card-body, .card'
          ) || el.parentElement;
          if (scope && !seenScopes.has(scope)) {
            seenScopes.add(scope);
            unitScopes.push(scope.outerHTML);
          }
        }
        item.unitHtml = unitScopes.join('\n');
      }
    } catch (e) { /* skip */ }
    plans.push({
      drillPath: item.path,
      anchorText: item.anchorText,
      h1: h1Text,
      bodyText: bodyText.slice(0, 50000),
      unitHtml: item.unitHtml || '',
    });
  }
  return {ok: true, plans: plans};
}
"""

# Tudorplace-style row: "#840_09 900 $1,765.00 Specials Available Apply Now..."
_TABULAR_ROW_RE = re.compile(
    r"#\s*([A-Z0-9_\-]+)\s+(\d{2,5})\s+\$\s*([\d,]+(?:\.\d{2})?)",
    re.IGNORECASE,
)
# Campobasso-style row: "Apartment: # F_205 Starting at: $1,425.00"
_TEXT_ROW_RE = re.compile(
    r"Apartment\s*:\s*#?\s*([A-Z0-9_\-]+).*?Starting\s+at\s*:?\s*\$\s*([\d,]+(?:\.\d{2})?)",
    re.IGNORECASE | re.DOTALL,
)

# 2026-07-11 audit: modern RentCafe layout-tab drills render each available
# unit as an anchor whose onclick fires
#   applyGAClick('<plan>','<N Bed(s)>','<sqft>','<rentLow>','<rentHigh>','<unit#>')
# arg6 is the displayed unit number (preserves underscores like "840_09").
# This is SSR (present in raw HTML) so it works code-only — no browser.
_APPLY_GA_RE = re.compile(
    r"applyGAClick\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'"
    r"\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)",
    re.IGNORECASE,
)
# Drill anchors on raw listing HTML — accept BOTH root-relative
# (/floorplans/{slug}) and absolute (https://host/floorplans/{slug}) hrefs;
# the Playwright-rendered DOM often rewrites relative hrefs to absolute.
_DRILL_ANCHOR_RE = re.compile(
    r"""href=["'](?:https?://[^"'/]+)?(/floorplans/[^"'?#/][^"'?#]*)["']""",
    re.IGNORECASE,
)


def _ga_int(s: str) -> int | None:
    try:
        return int(round(float(s.replace(",", "")))) if s else None
    except (TypeError, ValueError):
        return None


def _lt_unit_from_applyga(onclick: str, _element: object) -> str:
    match = _APPLY_GA_RE.search(onclick or "")
    return match.group(6).strip() if match else ""


def parse_rentcafe_lt_applyga(
    raw_html: str,
    drill_url: str,
    *,
    bathrooms: str = "",
) -> list[dict]:
    """Parse applyGAClick(plan,beds,sqft,rentLow,rentHigh,unit#) handlers from
    RAW drill HTML. Returns one row per handler (NO dedup — the caller applies
    a run-global dedup because a drill page can render the full roster)."""
    out: list[dict] = []
    unit_dates = availability_by_applyga_unit(
        raw_html or "", unit_from_element=_lt_unit_from_applyga
    )
    for m in _APPLY_GA_RE.finditer(raw_html or ""):
        plan, beds_lbl, sqft_raw, rlo_raw, rhi_raw, unit = (
            g.strip() for g in m.groups()
        )
        if not unit:
            continue
        beds = "0" if "studio" in beds_lbl.lower() else "".join(
            c for c in beds_lbl if c.isdigit()
        )
        sqft = "".join(c for c in sqft_raw if c.isdigit())
        rlo, rhi = _ga_int(rlo_raw), _ga_int(rhi_raw)
        out.append(
            make_unit_dict(
                floor_plan_name=plan,
                bed_label=beds_lbl or bed_label_from(None, plan),
                bedrooms=beds,
                bathrooms=bathrooms,
                sqft=sqft,
                unit_number=unit,
                rent_low=rlo,
                rent_high=(rhi or rlo),
                rent_range=format_rent_range(rlo, rhi or rlo),
                availability_status="AVAILABLE",
                availability_date=unit_dates.get(unit.upper(), ""),
                available_units="1",
                source_api_url=drill_url,
                extraction_tier="TIER_1_DOM_RENTCAFE_LT",
                floor_plan_name_provenance="rentcafe.layout-tab.plan-label",
            )
        )
    return out


_LT_PRIORITY_SHORTCUT = 100
_LT_PRIORITY_SECURECAFE = 200
_LT_PRIORITY_EXACT_DRILL = 300


def _mark_lt_source(rows: list[dict], priority: int) -> list[dict]:
    """Stamp source authority used only during RentCafe LT reconciliation."""
    for row in rows:
        row["_rentcafe_lt_priority"] = priority
    return rows


def _lt_row_completeness(row: dict) -> tuple[int, int]:
    """Tie-break same-authority duplicate rows without inventing data."""
    fields = (
        "floor_plan_name",
        "bedrooms",
        "bathrooms",
        "sqft",
        "market_rent_low",
        "market_rent_high",
        "availability_date",
    )
    populated = sum(row.get(field) not in (None, "") for field in fields)
    source_ids = row.get("source_ids")
    return populated, len(source_ids) if isinstance(source_ids, dict) else 0


def _reconcile_lt_rows(rows: list[dict]) -> list[dict]:
    """Union RentCafe surfaces by native apartment number.

    Plan-specific drills are authoritative over SecureCafe and the vanity
    ``/availableunits`` shortcut.  Lower-authority rows still add apartments
    absent from the exact drill set, so this is a union rather than a replace.
    Plan-level rows have no apartment identity and therefore remain separate.
    """
    physical: dict[str, tuple[int, tuple[int, int], int, dict]] = {}
    plan_rows: list[dict] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        unit = str(row.get("unit_number") or "").strip().upper()
        if not unit:
            plan_rows.append(row)
            continue
        priority = int(row.get("_rentcafe_lt_priority") or 0)
        candidate = (priority, _lt_row_completeness(row), -index, row)
        incumbent = physical.get(unit)
        if incumbent is None or candidate[:3] > incumbent[:3]:
            physical[unit] = candidate

    selected = [candidate[3] for candidate in physical.values()]
    reconciled = [*selected, *plan_rows]
    for row in reconciled:
        row.pop("_rentcafe_lt_priority", None)
    return reconciled


def _rentcafe_lt_source_url(base_url: str, drill: str) -> str:
    """Resolve an exact drill without duplicating the ``/floorplans`` path."""
    return urljoin(base_url.rstrip("/") + "/", drill) if drill else base_url


def _record_lt_provenance(
    result: AdapterResult,
    admitted: list[dict],
    source_bodies: dict[str, tuple[Any, int, str]],
    ctx: AdapterContext,
) -> None:
    """Hash only responses that contributed admitted physical apartments."""
    from ma_poc.pms.source_provenance import build_unit_source_provenance

    counts = Counter(
        str(row.get("source_api_url") or "")
        for row in admitted
        if str(row.get("unit_number") or "").strip()
        and str(row.get("source_api_url") or "").strip()
    )
    identity = {
        "property_id": str(getattr(ctx, "property_id", "") or ""),
        "property_name": str(getattr(ctx, "property_name", "") or ""),
        "marketing_url": str(getattr(ctx, "base_url", "") or ""),
    }
    for source_url, unit_count in counts.items():
        source = source_bodies.get(source_url)
        if source is None:
            continue
        body, status, response_kind = source
        result.unit_source_provenance.append(
            build_unit_source_provenance(
                provider="rentcafe_layout_tab",
                source_url=source_url,
                body=body,
                unit_count=unit_count,
                identity=identity,
                response_kind=response_kind,
                status=status,
            )
        )

_PLAN_SPECS_RE = re.compile(
    # Accept "Bed", "Beds", "Bedroom", "Bedrooms" without requiring a word
    # boundary right after the noun (Tudor Place uses "Beds", Campo Basso
    # "Bedroom"). Same for "Bath"/"Baths"/"Bathroom"/"Bathrooms".
    r"(\d+|studio)\s*(?:bed|bedroom)s?\b"
    r".*?(\d+(?:\.\d+)?)\s*(?:bath|bathroom)s?\b"
    r".*?(\d[\d,]*)\s*sq\.?\s*ft\.?",
    re.IGNORECASE | re.DOTALL,
)

# "4 Apartments Available" / "13 Available" → unit count hint
_AVAIL_COUNT_RE = re.compile(r"(\d+)\s+(?:Apartment|Unit)s?\s+Available", re.IGNORECASE)


def _parse_drill_units(body_text: str) -> list[dict]:
    """Extract unit rows from drill body text. Returns list of dicts
    with unit_number + sqft + rent + raw_text."""
    if not body_text:
        return []
    seen: set[str] = set()
    units: list[dict] = []
    # Tabular pattern first (more specific — has sqft column).
    for m in _TABULAR_ROW_RE.finditer(body_text):
        unit_no = m.group(1).strip()
        sqft = m.group(2).strip()
        rent = m.group(3).replace(",", "").split(".")[0]
        key = unit_no.upper()
        if key in seen:
            continue
        seen.add(key)
        units.append({
            "unit_number": unit_no,
            "sqft": sqft,
            "rent": rent,
        })
    # Text pattern second (Apartment: # X Starting at: $Y) — only adds
    # unit numbers not already captured.
    for m in _TEXT_ROW_RE.finditer(body_text):
        unit_no = m.group(1).strip()
        rent = m.group(2).replace(",", "").split(".")[0]
        key = unit_no.upper()
        if key in seen:
            continue
        seen.add(key)
        units.append({
            "unit_number": unit_no,
            "sqft": "",  # not present in text-style rows
            "rent": rent,
        })
    return units


def _parse_plan_specs(text: str) -> tuple[int | None, str, str]:
    """Extract (beds, baths, sqft) from drill text 'Studio 1 Bath 480 Sq. Ft.'
    or '2 Beds 1.5 Bath 900 Sq. Ft.'."""
    if not text:
        return None, "", ""
    if re.search(r"\bstudio\b", text, re.IGNORECASE):
        # Studio variant — try to find baths and sqft separately
        bm = re.search(r"(\d+(?:\.\d+)?)\s*bath", text, re.IGNORECASE)
        sm = re.search(r"(\d[\d,]*)\s*sq\.?\s*ft", text, re.IGNORECASE)
        return 0, (bm.group(1) if bm else ""), (sm.group(1).replace(",", "") if sm else "")
    m = _PLAN_SPECS_RE.search(text)
    if m:
        bed_v = m.group(1)
        beds = 0 if bed_v.lower() == "studio" else int(bed_v)
        return beds, m.group(2), m.group(3).replace(",", "")
    return None, "", ""


def parse_rentcafe_layout_tab(plans: list[dict], url: str) -> list[dict]:
    out: list[dict] = []
    for p in plans:
        if not isinstance(p, dict):
            continue
        drill = str(p.get("drillPath") or "")
        slug = drill.rstrip("/").split("/")[-1] if drill else ""
        anchor = str(p.get("anchorText") or "").strip()
        h1 = str(p.get("h1") or "").strip()
        body = str(p.get("bodyText") or "")
        unit_html = str(p.get("unitHtml") or "")
        plan_name = h1 or anchor or slug or ""
        beds, baths, sqft = _parse_plan_specs(body)

        # Browser extraction returns only the bounded per-unit snippets, not
        # a page-sized HTML blob.  Reuse the exact static applyGAClick parser
        # so browser and code-only paths preserve availability identically.
        if unit_html:
            applyga_rows = parse_rentcafe_lt_applyga(
                unit_html,
                _rentcafe_lt_source_url(url, drill),
                bathrooms=baths,
            )
            if applyga_rows:
                out.extend(
                    _mark_lt_source(applyga_rows, _LT_PRIORITY_EXACT_DRILL)
                )
                continue

        units_parsed = _parse_drill_units(body)
        avail_match = _AVAIL_COUNT_RE.search(body)
        avail_count = avail_match.group(1) if avail_match else ""

        if not units_parsed:
            # No unit roster — emit plan-level row if there's a starting rent.
            # The anchor text often carries it, e.g. "2 Beds 1 Bath 13 Available $1,765.00".
            sp_match = re.search(r"Starting\s+at\s*:?\s*\$\s*([\d,]+)", body, re.IGNORECASE)
            sp_rent = None
            if sp_match:
                sp_rent = money_to_int(sp_match.group(1))
            if not sp_rent and anchor:
                am = re.search(r"\$\s*([\d,]+)", anchor)
                if am:
                    sp_rent = money_to_int(am.group(1))
            if plan_name or sp_rent is not None:
                out.append(
                    make_unit_dict(
                        floor_plan_name=plan_name,
                        bed_label=bed_label_from(beds, plan_name),
                        bedrooms=str(beds) if beds is not None else "",
                        bathrooms=baths,
                        sqft=sqft,
                        unit_number="",
                        rent_low=sp_rent,
                        rent_high=sp_rent,
                        rent_range=format_rent_range(sp_rent, sp_rent),
                        # A starting rent proves a marketed plan, not a
                        # currently available apartment.  Only an explicit
                        # positive availability count supports AVAILABLE.
                        availability_status="AVAILABLE" if avail_count else "UNKNOWN",
                        available_units=avail_count,
                        source_api_url=_rentcafe_lt_source_url(url, drill),
                        extraction_tier="TIER_1_DOM_RENTCAFE_LT",
                    )
                )
            continue

        for u in units_parsed:
            unit_no = u.get("unit_number") or ""
            row_sqft = u.get("sqft") or sqft
            rent_str = u.get("rent") or ""
            try:
                rent = int(rent_str) if rent_str else None
            except (TypeError, ValueError):
                rent = None
            if not unit_no and rent is None:
                continue
            out.append(
                make_unit_dict(
                    floor_plan_name=plan_name,
                    bed_label=bed_label_from(beds, plan_name),
                    bedrooms=str(beds) if beds is not None else "",
                    bathrooms=baths,
                    sqft=row_sqft,
                    unit_number=unit_no,
                    rent_low=rent,
                    rent_high=rent,
                    availability_status="AVAILABLE",
                    available_units="1",
                    source_api_url=_rentcafe_lt_source_url(url, drill),
                    extraction_tier="TIER_1_DOM_RENTCAFE_LT",
                )
            )
    return _reconcile_lt_rows(out)


class RentCafeLayoutTabAdapter:
    """RentCafe layout-tab theme — bedroom-tab listing + per-plan
    ``/floorplans/{slug}`` drill with unit roster."""

    pms_name: str = "rentcafe_layout_tab"
    _fingerprints: list[str] = [
        "floorplans-layout-tab",
        "page-content-floorplans",
        "page-content-availableunits",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used="TIER_1_DOM_RENTCAFE_LT")
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            # 2026-07-11 audit: jugnu fetch-only path passes a stub page, so
            # the DOM-JS drill-walker can't run. Extract the same applyGAClick
            # SSR data statically via curl subpage hops instead of dead-ending.
            return await self._extract_code_only(page, ctx, result)
        try:
            payload = await evaluate(_RENTCAFE_LT_DOM_JS)
        except Exception as exc:
            log.debug("rentcafe_lt evaluate failed err=%s", exc)
            payload = None
        if not isinstance(payload, dict) or not payload.get("ok"):
            reason = payload.get("reason") if isinstance(payload, dict) else "non-dict payload"
            result.confidence = 0.0
            result.errors.append(f"rentcafe_lt: {reason}")
            return result
        plans = payload.get("plans") or []
        if not isinstance(plans, list) or not plans:
            result.confidence = 0.0
            result.errors.append("rentcafe_lt: zero plans in payload")
            return result
        winning = self._winning_url(page, ctx)
        source_bodies: dict[str, tuple[Any, int, str]] = {}
        for plan in plans:
            if not isinstance(plan, dict):
                continue
            drill = str(plan.get("drillPath") or "")
            source_url = _rentcafe_lt_source_url(winning, drill)
            body = str(plan.get("unitHtml") or plan.get("bodyText") or "")
            if source_url and body:
                source_bodies[source_url] = (
                    body,
                    200,
                    "rentcafe_plan_drill",
                )
        rows = parse_rentcafe_layout_tab(plans, winning)
        if not rows:
            result.confidence = 0.0
            result.errors.append(
                f"rentcafe_lt: parser produced zero rows from {len(plans)} drills"
            )
            return result
        from ma_poc.extraction.post_process import post_process

        pp = post_process(rows, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = next(
                (
                    str(row.get("source_api_url") or "")
                    for row in pp.admitted
                    if str(row.get("unit_number") or "").strip()
                    and str(row.get("source_api_url") or "").strip()
                ),
                winning,
            )
            result.confidence = min(0.92, 0.7 + 0.02 * pp.n_admitted)
            _record_lt_provenance(result, pp.admitted, source_bodies, ctx)
            return result
        result.confidence = 0.0
        result.errors.append(
            f"rentcafe_lt: {len(rows)} rows failed unit_validity post-process"
        )
        return result

    async def _extract_code_only(
        self, page: Page, ctx: AdapterContext, result: AdapterResult
    ) -> AdapterResult:
        """Static drill-walk for the jugnu fetch-only path. Reads the listing
        HTML from ctx.fetch_result.body (hopping to /floorplans if needed),
        curls each /floorplans/{slug} drill, parses applyGAClick SSR rows, and
        applies a RUN-GLOBAL dedup by unit_number — a drill page can render the
        FULL roster, so per-drill dedup alone over-counts (Pickwick: 5 drills ×
        44 units = 220 without the global dedup)."""
        from urllib.parse import urlparse

        from ma_poc.pms.adapters._probe import probe_get

        # Listing HTML: prefer the already-fetched body, else curl /floorplans.
        fr = getattr(ctx, "fetch_result", None)
        raw = getattr(fr, "body", None) if fr is not None else None
        if isinstance(raw, bytes):
            listing = raw.decode("utf-8", errors="replace")
        elif isinstance(raw, str):
            listing = raw
        else:
            listing = ""
        base = str(getattr(ctx, "base_url", "") or "")
        p = urlparse(base)
        origin = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else base.rstrip("/")
        source_bodies: dict[str, tuple[Any, int, str]] = {}

        # `{origin}/availableunits` is a useful one-request roster candidate,
        # including on sites that do not link it. It is not authoritative:
        # the 2026-08-02 complete layout-tab audit measured 89 shortcut rows
        # versus 187 exact plan-drill rows, with 13 semantic conflicts. Keep
        # it as a lower-priority union source and always inspect exact drills.
        # 2026-07-27 — every ``probe_get`` in this coroutine is OFF-LOADED to a
        # thread. ``_probe.probe_get`` is a blocking ``urllib.request.urlopen``
        # and this is an ``async def``, so a bare call parks the whole event
        # loop for the duration — up to 4 sequential fetches here, plus one per
        # drill page in the fan-out below. That is the 2026-07-25 timeout RCA's
        # exact failure mode (sync probe on the loop → mean property 305s
        # against a 600s cap → victims rotate), on the cohort the RCA was
        # about. Guarded structurally by ``test_no_blocking_probe_in_async_def``.
        avail_url = origin + "/availableunits"
        roster_units: list[dict] = []
        _attempted_attr = "_rentcafe_vanity_availableunits_attempted"
        if not bool(getattr(ctx, _attempted_attr, False)):
            try:
                setattr(ctx, _attempted_attr, True)
            except Exception:
                pass
            try:
                ra = await asyncio.to_thread(
                    probe_get,
                    avail_url,
                    timeout=20,
                    unlocker=False,
                    proxies={},
                    verify=True,
                    retries=1,
                )
                if getattr(ra, "status_code", 0) == 200:
                    roster_html = getattr(ra, "text", "") or ""
                    roster_units = _mark_lt_source(
                        parse_rentcafe_lt_applyga(
                            roster_html, avail_url
                        ),
                        _LT_PRIORITY_SHORTCUT,
                    )
                    if roster_units:
                        source_bodies[avail_url] = (
                            roster_html,
                            200,
                            "rentcafe_vanity_shortcut",
                        )
            except Exception as exc:  # noqa: BLE001 — best-effort, never fatal
                result.errors.append(
                    f"rentcafe_lt: /availableunits probe failed: {exc}"
                )

        # Always fetch the RAW /floorplans server HTML via curl: the
        # Playwright-rendered fetch_result.body drops the raw root-relative
        # drill hrefs (Tudor: 0 drills from the rendered DOM vs 1 from raw).
        # Prefer the raw curl listing; fall back to the rendered body only if
        # the curl fails.
        try:
            r = await asyncio.to_thread(probe_get, origin + "/floorplans", timeout=20)
            if getattr(r, "status_code", 0) == 200 and getattr(r, "text", ""):
                listing = r.text
                source_bodies[origin + "/floorplans"] = (
                    listing,
                    200,
                    "rentcafe_floorplans_listing",
                )
        except Exception as exc:  # noqa: BLE001 — best-effort
            result.errors.append(f"rentcafe_lt: /floorplans probe failed: {exc}")

        # SecureCafe is another roster candidate. The same property can expose
        # a smaller vanity roster and a larger portal roster (Black Hawk: 3
        # versus 9), so discover it even when the vanity route succeeds. It is
        # still lower authority than a property-bound plan drill.
        # The Yardi leasing portal normally lives at
        # ``{sub}.securecafe.com/onlineleasing/{slug}/availableunits.aspx``.
        #
        # Measured on the 2026-07-25 plan-level cohort: 571 of 1,126 (51%)
        # carry a SecureCafe fingerprint — the single largest surface in the
        # cohort. A live sample of 40 showed the vanity route already recovers
        # 70% of them; the remaining ~30% are exactly the 404/403 cases this
        # covers.
        #
        # Both halves already existed — _find_all_securecafe_bases discovers
        # the base and parse_securecafe_availableunits reads the page — but
        # nothing connected them on this path. Same "parser exists, discovery
        # missing" shape as the /availableunits lever itself.
        drills = sorted({m for m in _DRILL_ANCHOR_RE.findall(listing)})
        _sc_carry: list[dict] = []
        _sc_src = ""
        try:
            from ma_poc.pms.adapters.rentcafe import (
                _find_all_securecafe_bases,
                parse_securecafe_availableunits,
            )

            for _base in _find_all_securecafe_bases(listing, ctx)[:3]:
                _sc_url = _base.rstrip("/") + "/availableunits.aspx"
                try:
                    _sr = await asyncio.to_thread(probe_get, _sc_url, timeout=20)
                except Exception:
                    continue
                if getattr(_sr, "status_code", 0) != 200:
                    continue
                _sc_rows = parse_securecafe_availableunits(
                    getattr(_sr, "text", "") or "", _sc_url
                )
                if not _sc_rows:
                    continue
                _sc_carry = _mark_lt_source(
                    _sc_rows, _LT_PRIORITY_SECURECAFE
                )
                _sc_src = _sc_url
                source_bodies[_sc_url] = (
                    getattr(_sr, "text", "") or "",
                    200,
                    "securecafe_availableunits",
                )
                break
        except Exception as exc:  # noqa: BLE001 — best-effort, never fatal
            result.errors.append(f"rentcafe_lt: securecafe hop failed: {exc}")

        collected: list[dict] = []
        # The vanity shortcut remains useful evidence, but it can no longer
        # preempt exact plan drills.  Northview, Broadway, Franklin and Jasper
        # all publish smaller, semantically wrong shortcut rosters today.
        collected.extend(roster_units)
        # The rendered listing may already carry unit anchors inline — parse
        # them too (harmless; global dedup below removes any overlap).
        collected.extend(
            _mark_lt_source(
                parse_rentcafe_lt_applyga(listing, origin + "/floorplans"),
                _LT_PRIORITY_SHORTCUT,
            )
        )
        # Portal rows that did not clear the floor still count — the global
        # dedup in _finish_code_only removes any overlap with the drill rows.
        collected.extend(_sc_carry)
        if not drills and not collected:
            result.confidence = 0.0
            result.errors.append("rentcafe_lt: no drill anchors in listing (code-only)")
            return result

        for d in drills:
            try:
                rr = await asyncio.to_thread(probe_get, origin + d, timeout=20)
            except Exception:
                continue
            if getattr(rr, "status_code", 0) != 200:
                continue
            drill_html = getattr(rr, "text", "") or ""
            if not drill_html:
                continue
            # Preserve word boundaries and decode entities before parsing the
            # exact plan header. A regex tag-strip loses values on several
            # live themes (Northview, Franklin, Wildwood).
            from bs4 import BeautifulSoup

            drill_text = BeautifulSoup(
                drill_html, "html.parser"
            ).get_text(" ", strip=True)
            _, drill_baths, _ = _parse_plan_specs(drill_text)
            rows = parse_rentcafe_lt_applyga(
                drill_html,
                origin + d,
                bathrooms=drill_baths,
            )
            if not rows:
                # Legacy tabular/text drills (Tudorplace/Campobasso) —
                # reuse the existing text parser as a fallback.
                rows = parse_rentcafe_layout_tab(
                    [{"drillPath": d, "bodyText": drill_html}], origin
                )
            if rows:
                source_bodies[origin + d] = (
                    drill_html,
                    200,
                    "rentcafe_plan_drill",
                )
            collected.extend(_mark_lt_source(rows, _LT_PRIORITY_EXACT_DRILL))

        return self._finish_code_only(
            ctx, result, collected, _sc_src or (origin + "/floorplans"),
            detail=(
                f"{len(drills)} drills + {len(_sc_carry)} securecafe rows"
                if _sc_carry
                else f"{len(drills)} drills"
            ),
            source_bodies=source_bodies,
        )

    @staticmethod
    def _finish_code_only(
        ctx: AdapterContext,
        result: AdapterResult,
        collected: list[dict],
        winning_url: str,
        *,
        detail: str = "/availableunits roster",
        source_bodies: dict[str, tuple[Any, int, str]] | None = None,
    ) -> AdapterResult:
        """Dedup, post-process and score rows from the code-only path.

        Extracted 2026-07-25 so the ``/availableunits`` whole-roster
        short-circuit and the ``/floorplans`` drill fan-out finish through
        IDENTICAL logic — a second copy of the dedup/scoring tail is exactly
        how this file would drift, and drift between two copies of the same
        rule is already a recurring defect class in this repo.
        """
        # Run-global union by native apartment number. Exact plan drills win
        # semantic conflicts; lower-authority surfaces can still contribute
        # apartments that the drills do not publish.
        deduped = _reconcile_lt_rows(collected)

        if not deduped:
            result.confidence = 0.0
            result.errors.append(
                f"rentcafe_lt: {detail} yielded zero units (code-only)"
            )
            return result

        physical_source = next(
            (
                str(row.get("source_api_url") or "")
                for row in deduped
                if str(row.get("unit_number") or "").strip()
                and str(row.get("source_api_url") or "").strip()
            ),
            "",
        )

        from ma_poc.extraction.post_process import post_process

        pp = post_process(deduped, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = physical_source or winning_url
            result.confidence = min(0.92, 0.7 + 0.02 * pp.n_admitted)
            _record_lt_provenance(
                result,
                pp.admitted,
                source_bodies or {},
                ctx,
            )
            return result
        result.confidence = 0.0
        result.errors.append(
            f"rentcafe_lt: {len(deduped)} rows failed unit_validity (code-only)"
        )
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
