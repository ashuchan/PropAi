"""Aspen Square Management adapter.

Aspen Square is a multifamily operator running a custom Strapi CMS at
``aspensquare.com``. Multi-property operator (memory-catalogued cluster):
each community lives at ``aspensquare.com/apartments/{state}/{city}/
{community}`` and the unit roster at the per-plan drill page
``.../{community}/floor-plans/{plan-slug}``.

Live-verified 2026-05-19 on southwood-acres + the-woodhaven drill (3
unit-level rows: 13-115 $2,043 Available Now; 10-63 $2,043 06/06/2026;
10-57 $2,023 08/07/2026).

Two-stage SSR DOM:

* Community page (``.aspen-c-full-width-card`` × N plans):
  - ``.aspen-c-heading``                  plan name ("The Woodhaven")
  - ``.aspen-c-badge__text``              avail status / count
                                            ("3 Available" / "Limited Availability")
  - ``.aspen-c-full-width-card__features``  "1 bed1 bath425 sq ft"
  - ``.aspen-c-full-width-card__detail--heading`` plan rent ("Call For Pricing")
  - ``a[href*="/floor-plans/"]``          drill URL → unit-level

* Drill page (``.aspen-c-unit-row`` rows, ``.aspen-c-table__cell`` cells):
  - cell[0] = unit number ("13 - 115")
  - cell[1] = rent ("$2,043")
  - cell[2] = availability ("Available Now" | "MM/DD/YYYY")

Probed cluster size: 3 in the deep-probe sample (16186, 6526, 14907) —
all aspensquare.com. Failure mode pre-fix: ``tier=NONE`` (no adapter).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

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

# Runs in the live page. If the document already has plan cards, parses
# them; otherwise fetches the community page (``location.pathname`` if
# we're somewhere under /apartments/, else does nothing). For each plan
# card discovered, fetches the per-plan drill ``/floor-plans/{slug}``
# in-session and joins unit-level rows back to plan dims.
_ASPENSQUARE_DOM_JS = r"""
async () => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  let doc = document;
  if (!document.querySelector('.aspen-c-full-width-card')) {
    // Try to fetch the community page if our pathname is the drill page
    // ('.../floor-plans/{slug}' → strip last 2 segments to get community).
    let communityPath = location.pathname;
    if (/\/floor-plans\/[^/]+\/?$/.test(communityPath)) {
      communityPath = communityPath.replace(/\/floor-plans\/[^/]+\/?$/, '');
    }
    try {
      const r = await fetch(location.origin + communityPath, {credentials: 'include'});
      if (r.ok) doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    } catch (e) { /* fall through */ }
  }

  const cards = Array.from(doc.querySelectorAll('.aspen-c-full-width-card')).map((c) => {
    const a = c.querySelector('a[href*="/floor-plans/"]');
    return {
      name: T(c.querySelector('.aspen-c-heading')),
      specs: T(c.querySelector('.aspen-c-full-width-card__features')),
      planRent: T(c.querySelector('.aspen-c-full-width-card__detail--heading')),
      badge: T(c.querySelector('.aspen-c-badge__text')),
      drillPath: a ? a.getAttribute('href') : '',
    };
  });

  const out = [];
  for (const card of cards) {
    if (!card.drillPath) {
      out.push({...card, units: []});
      continue;
    }
    let drillDoc = null;
    try {
      const r = await fetch(location.origin + card.drillPath, {credentials: 'include'});
      if (r.ok) drillDoc = new DOMParser().parseFromString(await r.text(), 'text/html');
    } catch (e) { /* drill failed → plan-level only */ }
    const units = drillDoc
      ? Array.from(drillDoc.querySelectorAll('.aspen-c-unit-row'))
          .filter((r) => !/aspen-u-is-hidden/.test(r.className))
          .map((r) => {
            const cells = Array.from(r.querySelectorAll('.aspen-c-table__cell')).map(T);
            return {unit: cells[0] || '', rent: cells[1] || '', avail: cells[2] || ''};
          })
      : [];
    out.push({...card, units});
  }
  return out;
}
"""

_BED_RE = re.compile(r"(\d+)\s*bed", re.IGNORECASE)
_BATH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*bath", re.IGNORECASE)
_SQFT_RE = re.compile(r"(\d[\d,]*)\s*sq", re.IGNORECASE)
_MONEY_RE = re.compile(r"\$([\d,]+)")
_AVAIL_COUNT_RE = re.compile(r"(\d+)\s*Available", re.IGNORECASE)

# AspenSquare's current Next.js app-router pages stream the property payload in
# React Server Component frames.  The second ``push`` argument is a normal JSON
# string; after decoding it, the value following ``"floorPlans":`` is an
# ordinary JSON object.  This deliberately parses the data contract rather
# than depending on generated component/chunk IDs.
_NEXT_RSC_PUSH_RE = re.compile(
    r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)', re.DOTALL
)


@dataclass(slots=True)
class AspenSquareSurface:
    """Exact current marketing catalogue embedded in one community page."""

    plans: list[dict[str, Any]]
    units: list[dict[str, Any]]
    source_url: str


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _capture_date_iso(value: date | str | None) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return value.strip()
    # The production formatter also captures in UTC.  Using the same calendar
    # boundary avoids introducing the one-day timezone shift found elsewhere
    # in the availability audit.
    return datetime.now(UTC).date().isoformat()


def _current_availability_token(raw: Any, capture_date: str) -> str:
    """Return visible current/future semantics from Aspen's availability."""
    if not isinstance(raw, dict):
        return ""
    value = str(raw.get("madeReadyDate") or raw.get("vacantDate") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return ""
    # Aspen's rendered table labels reached dates "Available Now".  Preserve
    # that source semantic token so the shared formatter uses its own capture
    # date and records ``available_now`` provenance.  Future dates remain
    # byte-for-byte exact.
    return "Available Now" if value <= capture_date else value


def _floorplans_objects_from_next_html(html: str) -> list[dict[str, Any]]:
    """Decode unique ``floorPlans`` objects from current Next.js HTML."""
    if not html or '"floorPlans"' not in html and r'\"floorPlans\"' not in html:
        return []
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _NEXT_RSC_PUSH_RE.finditer(html):
        try:
            payload = json.loads(match.group(1))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, str):
            continue
        cursor = 0
        while True:
            marker = payload.find('"floorPlans":', cursor)
            if marker < 0:
                break
            value_start = marker + len('"floorPlans":')
            try:
                candidate, _ = decoder.raw_decode(payload, value_start)
            except (TypeError, ValueError, json.JSONDecodeError):
                cursor = marker + 1
                continue
            cursor = marker + 1
            if not isinstance(candidate, dict) or not isinstance(candidate.get("styles"), list):
                continue
            fingerprint = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
            if fingerprint not in seen:
                seen.add(fingerprint)
                objects.append(candidate)
    return objects


def parse_aspensquare_next_surface(
    html: str,
    source_url: str,
    *,
    capture_date: date | str | None = None,
) -> AspenSquareSurface | None:
    """Parse Aspen's current catalogue and capped availability window.

    ``price`` is retained only for plans with a published apartment roster.
    Empty plans render "Call For Pricing" even though the RSC payload can
    carry an internal revenue-management number, so emitting that hidden
    number would contradict the public page.
    """
    objects = _floorplans_objects_from_next_html(html)
    if not objects:
        return None
    # A community should expose one object.  If a future page repeats it, use
    # the richest exact catalogue rather than unioning possible sibling data.
    floorplans = max(objects, key=lambda item: len(item.get("styles") or []))
    capture_iso = _capture_date_iso(capture_date)
    plans: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []

    for raw_plan in floorplans.get("styles") or []:
        if not isinstance(raw_plan, dict):
            continue
        plan_name = str(raw_plan.get("name") or "").strip()
        if not plan_name:
            continue
        asset_id = str(raw_plan.get("assetId") or "").strip()
        floor_plan_id = str(
            raw_plan.get("xRefFloorPlanID") or raw_plan.get("floorPlanID") or ""
        ).strip()
        bedrooms = _positive_int(raw_plan.get("bedrooms"))
        # Studio is a legitimate zero; preserve it separately from missing.
        if raw_plan.get("bedrooms") == 0:
            bedrooms = 0
        bathrooms = raw_plan.get("bathrooms")
        sqft = _positive_int(raw_plan.get("squareFeet"))
        available_units = [
            unit for unit in (raw_plan.get("availableUnits") or []) if isinstance(unit, dict)
        ]
        explicit_empty = bool(raw_plan.get("showAvailability") is True and not available_units)
        plan_price = (
            _positive_int(raw_plan.get("price"))
            if available_units and raw_plan.get("showFloorPlanPricing") is not False
            else None
        )
        plan_source_ids: dict[str, str] = {}
        if asset_id:
            plan_source_ids["aspensquare_asset_id"] = asset_id
        if floor_plan_id:
            plan_source_ids["aspensquare_floor_plan_id"] = floor_plan_id

        plan_row = make_unit_dict(
            floor_plan_name=plan_name,
            bed_label=bed_label_from(bedrooms, plan_name),
            bedrooms=str(bedrooms) if bedrooms is not None else "",
            bathrooms=str(bathrooms) if bathrooms is not None else "",
            sqft=str(sqft) if sqft is not None else "",
            rent_low=plan_price,
            rent_high=plan_price,
            availability_status="UNAVAILABLE" if explicit_empty else "AVAILABLE",
            available_units=str(len(available_units)),
            source_api_url=source_url,
            extraction_tier="TIER_1_DOM_ASPENSQUARE_NEXT",
            source_ids=plan_source_ids,
        )

        plan_meta: dict[str, Any] = {
            "name": plan_name,
            "bedrooms": bedrooms,
            "bathrooms": str(bathrooms) if bathrooms is not None else "",
            "sqft": sqft,
            "asset_id": asset_id,
            "floor_plan_id": floor_plan_id,
            "explicit_empty": explicit_empty,
            "internal_names": [],
            "row": plan_row,
        }

        for raw_unit in available_units:
            address = raw_unit.get("address")
            address = address if isinstance(address, dict) else {}
            nested_plan = raw_unit.get("floorPlan")
            nested_plan = nested_plan if isinstance(nested_plan, dict) else {}
            unit_number = str(address.get("unitNumber") or "").strip()
            building = str(address.get("buildingNumber") or "").strip()
            internal_name = str(nested_plan.get("floorPlanName") or "").strip()
            nested_plan_id = str(nested_plan.get("floorPlanID") or floor_plan_id).strip()
            unit_id = str(raw_unit.get("xRefUnitId") or address.get("unitID") or "").strip()
            unit_asset_id = str(raw_unit.get("assetId") or asset_id).strip()
            if internal_name and internal_name not in plan_meta["internal_names"]:
                plan_meta["internal_names"].append(internal_name)
            source_ids: dict[str, str] = {}
            if unit_asset_id:
                source_ids["aspensquare_asset_id"] = unit_asset_id
            if unit_id:
                source_ids["aspensquare_unit_id"] = unit_id
            if nested_plan_id:
                source_ids["aspensquare_floor_plan_id"] = nested_plan_id
            units.append(
                {
                    "unit_number": unit_number,
                    "building": building,
                    "floor_plan_name": str(raw_unit.get("floorPlanName") or plan_name).strip(),
                    "internal_plan_name": internal_name,
                    "bedrooms": bedrooms,
                    "bathrooms": str(bathrooms) if bathrooms is not None else "",
                    "sqft": sqft,
                    "availability_date": _current_availability_token(
                        raw_unit.get("availability"), capture_iso
                    ),
                    "source_ids": source_ids,
                    "floor_plan_id": nested_plan_id,
                }
            )
        plans.append(plan_meta)

    return AspenSquareSurface(plans=plans, units=units, source_url=source_url) if plans else None


def _normalized_identity(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _dimension_key(row: dict[str, Any]) -> tuple[str, str, str]:
    def norm_number(value: Any) -> str:
        try:
            number = float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return ""
        return str(int(number)) if number.is_integer() else str(number)

    return (
        norm_number(row.get("bedrooms")),
        norm_number(row.get("bathrooms")),
        norm_number(row.get("sqft")),
    )


def reconcile_aspensquare_knock_units(
    knock_units: list[dict[str, Any]],
    surface: AspenSquareSurface,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Bind stable Knock apartments to Aspen's exact public catalogue."""
    direct_by_triplet: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    direct_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    direct_by_number: dict[str, list[dict[str, Any]]] = {}
    plan_by_name = {
        _normalized_identity(plan.get("name")): plan for plan in surface.plans
    }
    for direct in surface.units:
        number = _normalized_identity(direct.get("unit_number"))
        building = _normalized_identity(direct.get("building"))
        internal_plan = _normalized_identity(direct.get("internal_plan_name"))
        direct_by_triplet.setdefault((building, number, internal_plan), []).append(direct)
        direct_by_pair.setdefault((building, number), []).append(direct)
        direct_by_number.setdefault(number, []).append(direct)

    internal_plan_index: dict[str, list[dict[str, Any]]] = {}
    dimension_plan_index: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for plan in surface.plans:
        for internal_name in plan.get("internal_names") or []:
            internal_plan_index.setdefault(_normalized_identity(internal_name), []).append(plan)
        dimension_plan_index.setdefault(_dimension_key(plan), []).append(plan)

    def exact_one(values: list[dict[str, Any]] | None) -> dict[str, Any] | None:
        return values[0] if values and len(values) == 1 else None

    admitted: list[dict[str, Any]] = []
    conflicts: list[str] = []
    for source_row in knock_units:
        row = dict(source_row)
        number = _normalized_identity(row.get("unit_name") or row.get("unit_number"))
        building = _normalized_identity(row.get("building"))
        internal_plan = _normalized_identity(row.get("floor_plan_name"))
        direct = exact_one(direct_by_triplet.get((building, number, internal_plan)))
        # Some vendors omit either building or internal plan.  Relax only when
        # the remaining public key is still one-to-one; never let repeated
        # labels such as Waters Edge's five distinct ``11 / 103`` apartments
        # inherit one displayed source row across sibling layouts.
        if direct is None and not internal_plan:
            direct = exact_one(direct_by_pair.get((building, number)))
        if direct is None and not building:
            direct = exact_one(direct_by_number.get(number))

        plan: dict[str, Any] | None = None
        if direct is not None:
            plan = plan_by_name.get(_normalized_identity(direct.get("floor_plan_name")))
        if plan is None:
            plan = exact_one(
                internal_plan_index.get(_normalized_identity(row.get("floor_plan_name")))
            )
        if plan is None:
            plan = exact_one(dimension_plan_index.get(_dimension_key(row)))
        if plan is None:
            knock_name = _normalized_identity(row.get("floor_plan_name"))
            contained = [
                candidate
                for candidate in surface.plans
                if _normalized_identity(candidate.get("name"))
                and _normalized_identity(candidate.get("name")) in knock_name
            ]
            plan = exact_one(contained)

        native_id = str((row.get("source_ids") or {}).get("knock_unit_id") or "")
        if plan is None:
            conflicts.append(f"unmapped_plan:{native_id or number}")
            continue
        if plan.get("explicit_empty"):
            conflicts.append(
                f"marketing_empty_withheld:{plan.get('name')}:{native_id or number}"
            )
            continue

        row["floor_plan_name"] = str(plan.get("name") or row.get("floor_plan_name") or "")
        source_ids = dict(row.get("source_ids") or {})
        if plan.get("asset_id"):
            source_ids["aspensquare_asset_id"] = str(plan["asset_id"])
        if plan.get("floor_plan_id"):
            source_ids["aspensquare_floor_plan_id"] = str(plan["floor_plan_id"])

        if direct is not None:
            row["unit_name"] = str(direct.get("unit_number") or row.get("unit_name") or "")
            row["building"] = str(direct.get("building") or row.get("building") or "")
            direct_source_ids = direct.get("source_ids")
            if isinstance(direct_source_ids, dict):
                source_ids.update(
                    {str(key): str(value) for key, value in direct_source_ids.items() if value}
                )
            visible_date = str(direct.get("availability_date") or "")
            if visible_date:
                row["availability_date"] = visible_date
                row["available_date"] = visible_date
            row["availability_status"] = "AVAILABLE"
        else:
            existing_flag = str(row.get("data_quality_flag") or "").strip()
            fallback_flag = "ASPENSQUARE_KNOCK_FALLBACK_NOT_IN_PUBLIC_WINDOW"
            row["data_quality_flag"] = (
                f"{existing_flag}|{fallback_flag}" if existing_flag else fallback_flag
            )

        row["source_ids"] = source_ids
        row["extraction_tier"] = "TIER_1_API_ASPENSQUARE_KNOCK_RECONCILED"
        admitted.append(row)
    return admitted, conflicts


def _surface_plan_rows(surface: AspenSquareSurface) -> list[dict[str, Any]]:
    return [dict(plan["row"]) for plan in surface.plans if isinstance(plan.get("row"), dict)]


def _parse_specs(specs: str) -> tuple[int | None, str, str]:
    """'1 bed1 bath425 sq ft' → (1, '1', '425')."""
    if not specs:
        return None, "", ""
    # AspenSquare concatenates fields ("Studio1 bath350 sq ft"), so no
    # trailing word boundary — use a negative lookahead to avoid "studios".
    if re.search(r"\bstudio(?![a-z])", specs, re.IGNORECASE):
        beds: int | None = 0
    else:
        bm = _BED_RE.search(specs)
        beds = int(bm.group(1)) if bm else None
    bath_m = _BATH_RE.search(specs)
    baths = bath_m.group(1) if bath_m else ""
    sqft_m = _SQFT_RE.search(specs)
    sqft = sqft_m.group(1).replace(",", "") if sqft_m else ""
    return beds, baths, sqft


def parse_aspensquare_cards(
    cards: list[dict[str, object]], url: str
) -> list[dict[str, str]]:
    """Emit unit-level rows when the drill page yielded units; else
    plan-level rows. Both tier as ``TIER_1_DOM_ASPENSQUARE``.
    """
    out: list[dict[str, str]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        name = str(card.get("name") or "").strip()
        specs = str(card.get("specs") or "")
        beds, baths, sqft = _parse_specs(specs)

        plan_rent_str = str(card.get("planRent") or "")
        money = _MONEY_RE.findall(plan_rent_str)
        plan_rent_lo = money_to_int(money[0]) if money else None
        plan_rent_hi = money_to_int(money[-1]) if money else None

        badge = str(card.get("badge") or "")
        count_m = _AVAIL_COUNT_RE.search(badge)
        plan_avail_count = count_m.group(1) if count_m else ""

        units_raw = card.get("units") or []

        if isinstance(units_raw, list) and units_raw:
            # Unit-level rows from the drill table.
            for u in units_raw:
                if not isinstance(u, dict):
                    continue
                unit_no = str(u.get("unit") or "").strip()
                rent_str = str(u.get("rent") or "")
                u_money = _MONEY_RE.findall(rent_str)
                rent = money_to_int(u_money[0]) if u_money else None
                avail = str(u.get("avail") or "").strip()
                status = "AVAILABLE"
                avail_date = ""
                if re.match(r"available\s+now", avail, re.IGNORECASE):
                    avail_date = ""
                elif re.match(r"\d{1,2}/\d{1,2}/\d{2,4}", avail):
                    avail_date = avail
                if not unit_no and rent is None:
                    continue
                out.append(
                    make_unit_dict(
                        floor_plan_name=name,
                        bed_label=bed_label_from(beds, name),
                        bedrooms=str(beds) if beds is not None else "",
                        bathrooms=str(baths),
                        sqft=sqft,
                        unit_number=unit_no,
                        rent_low=rent,
                        rent_high=rent,
                        availability_status=status,
                        available_units="1",
                        availability_date=avail_date,
                        source_api_url=url,
                        extraction_tier="TIER_1_DOM_ASPENSQUARE",
                    )
                )
        elif name:
            # Plan-level fallback (no drill / no units).
            status = (
                "UNAVAILABLE"
                if re.search(r"waitlist|limited", badge, re.IGNORECASE) and plan_rent_lo is None
                else "AVAILABLE"
            )
            out.append(
                make_unit_dict(
                    floor_plan_name=name,
                    bed_label=bed_label_from(beds, name),
                    bedrooms=str(beds) if beds is not None else "",
                    bathrooms=str(baths),
                    sqft=sqft,
                    unit_number="",
                    rent_range=format_rent_range(plan_rent_lo, plan_rent_hi),
                    rent_low=plan_rent_lo,
                    rent_high=plan_rent_hi,
                    availability_status=status,
                    available_units=plan_avail_count,
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_ASPENSQUARE",
                )
            )
    return out


class AspenSquareAdapter:
    """Aspen Square Management adapter — community page + per-plan unit drill."""

    pms_name: str = "aspensquare"
    _fingerprints: list[str] = ["aspensquare.com", "static.aspensquare.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Discover plans on the community page, drill each /floor-plans/
        {slug} for unit rows, emit unit-level dicts.

        2026-05-20: when no live Playwright page is available (L1-only
        pipeline mode), fall back to the Knock-community-hash path which
        works from the static HTML body alone. Every Aspen Square site
        embeds a Knock community hash + apiToken in the SSR-rendered
        config blob; calling Knock's public API resolves to unit-level
        inventory without needing the live DOM. Verified live 4/4 on
        Adley 72nd / The Avenue / Edgewood Court / Country Manor.
        """
        result = AdapterResult(tier_used="TIER_1_DOM_ASPENSQUARE")

        # The current site no longer renders the legacy card/table selectors;
        # its complete catalogue is embedded in the L1 Next.js response.  Give
        # that exact, property-scoped surface first priority and reconcile it
        # with Knock before considering the legacy DOM path.
        fetch_result = getattr(ctx, "fetch_result", None)
        fetch_body = getattr(fetch_result, "body", None) if fetch_result is not None else None
        if isinstance(fetch_body, bytes):
            modern_html = fetch_body.decode("utf-8", errors="replace")
        elif isinstance(fetch_body, str):
            modern_html = fetch_body
        else:
            modern_html = ""
        if parse_aspensquare_next_surface(modern_html, str(ctx.base_url or "")) is not None:
            modern = await self._try_knock_community_fallback(ctx, result)
            if modern is not None and (modern.units or modern.plan_summaries):
                return modern

        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            # L1-only fallback: try the Knock community-hash recovery.
            knock_units = await self._try_knock_community_fallback(ctx, result)
            if knock_units:
                return knock_units
            result.confidence = 0.0
            result.errors.append("aspensquare: no live page to parse")
            return result

        try:
            cards = await evaluate(_ASPENSQUARE_DOM_JS)
        except Exception as exc:
            log.debug("AspenSquare DOM evaluate failed err=%s", exc)
            cards = None

        if not isinstance(cards, list) or not cards:
            # A rendered page can still carry only the modern Next.js shape.
            # If the first attempt was skipped (for example a minimal L1 body),
            # let Knock recovery run before declaring the adapter empty.
            knock_result = await self._try_knock_community_fallback(ctx, result)
            if knock_result is not None and (
                knock_result.units or knock_result.plan_summaries
            ):
                return knock_result
            result.confidence = 0.0
            result.errors.append("aspensquare: no .aspen-c-full-width-card blocks found")
            return result

        units = parse_aspensquare_cards(cards, self._winning_url(page, ctx))
        if units:
            from ma_poc.extraction.post_process import post_process

            pp = post_process(units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                result.units = pp.admitted
                result.plan_summaries = pp.plan_summaries
                result.winning_url = self._winning_url(page, ctx)
                result.confidence = min(0.95, 0.7 + 0.04 * pp.n_admitted)
                return result
            result.errors.append(
                f"ASPENSQUARE_VALIDITY_REJECTED: {len(units)} rows failed unit_validity"
            )

        result.confidence = 0.0
        result.errors.append("aspensquare: no parseable plan/unit data")
        return result

    @staticmethod
    def _winning_url(page: Page, ctx: AdapterContext) -> str:
        try:
            return page.url or getattr(ctx, "base_url", "") or ""
        except Exception:
            return getattr(ctx, "base_url", "") or ""

    @staticmethod
    async def _try_knock_community_fallback(
        ctx: AdapterContext, result: AdapterResult
    ) -> AdapterResult | None:
        """When the live-page DOM path can't run, try Knock-by-domain
        recovery from the static HTML body. Returns a populated
        ``AdapterResult`` (with ``tier_used`` stamped) on success, or
        ``None`` to fall through to the failure path.

        Every Aspen Square site embeds a Knock community hash + apiToken
        in the SSR config (verified 4/4 live 2026-05-20). The two-call
        Knock API resolves the property_id and returns unit-level
        inventory without auth.
        """
        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        if isinstance(body, bytes):
            try:
                html = body.decode("utf-8", errors="replace")
            except Exception:
                return None
        elif isinstance(body, str):
            html = body
        else:
            return None
        base_url = str(getattr(ctx, "base_url", "") or "")
        if not html or not base_url:
            return None
        surface = parse_aspensquare_next_surface(html, base_url)

        def surface_only() -> AdapterResult | None:
            if surface is None:
                return None
            result.plan_summaries = _surface_plan_rows(surface)
            result.tier_used = "TIER_1_DOM_ASPENSQUARE_NEXT"
            result.winning_url = base_url
            result.confidence = min(0.90, 0.72 + 0.03 * len(result.plan_summaries))
            return result

        try:
            from ma_poc.pms.adapters.knock import (
                _current_knock_responses,
                _fetch_knock_units_by_domain,
                _validate_and_record_knock_identity,
                find_knock_community_hash,
            )
        except ImportError:
            return None
        # Only fire when the static HTML actually carries the Knock
        # config blob — otherwise the API calls would burn time on a
        # property that isn't really Knock-backed.
        if not find_knock_community_hash(html):
            return surface_only()
        try:
            pid, units = await _fetch_knock_units_by_domain(base_url, html)
        except Exception as exc:
            result.errors.append(
                f"aspensquare-knock-fallback-error: {type(exc).__name__}: "
                f"{str(exc)[:120]}"
            )
            return None
        if not pid:
            return surface_only()
        if _validate_and_record_knock_identity(ctx, result) is None:
            return surface_only()
        result.api_responses.extend(_current_knock_responses())
        from ma_poc.extraction.post_process import post_process

        reconciled = units
        conflicts: list[str] = []
        if surface is not None:
            reconciled, conflicts = reconcile_aspensquare_knock_units(units, surface)
            if conflicts:
                withheld = sum(item.startswith("marketing_empty_withheld:") for item in conflicts)
                unmapped = sum(item.startswith("unmapped_plan:") for item in conflicts)
                result.errors.append(
                    "ASPENSQUARE_MARKETING_RECONCILIATION: "
                    f"withheld_empty={withheld} unmapped={unmapped}"
                )

        pp = post_process(reconciled, property_id=getattr(ctx, "property_id", None))
        plan_summaries = _surface_plan_rows(surface) if surface is not None else []
        for plan in pp.plan_summaries:
            if isinstance(plan, dict) and plan not in plan_summaries:
                plan_summaries.append(plan)
        result.plan_summaries = plan_summaries
        if pp.n_admitted == 0:
            if result.plan_summaries:
                result.tier_used = "TIER_1_API_ASPENSQUARE_KNOCK_RECONCILED"
                result.winning_url = base_url
                result.confidence = min(
                    0.90, 0.72 + 0.03 * len(result.plan_summaries)
                )
                return result
            return None
        result.units = pp.admitted
        result.tier_used = (
            "TIER_1_API_ASPENSQUARE_KNOCK_RECONCILED"
            if surface is not None
            else "TIER_1_API_ASPENSQUARE_KNOCK"
        )
        result.winning_url = (
            f"https://doorway-api.knockrentals.com/v1/property/{pid}/units"
        )
        result.confidence = min(0.92, 0.65 + 0.04 * pp.n_admitted)
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
