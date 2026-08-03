"""Edifice CMS adapter — UNIT-LEVEL via the public floorplans + units APIs.

Canary 1ef1060 regression #10: ``cobblestonephx.com/floorplans.php`` was
returning floorplan-only LLM output with no plan name and no unit-level
roster ("does not go to 2+available"). Live-probe (2026-05-25) revealed
the site is built on **Edifice CMS** — a tenant-multifamily SaaS CMS by
Hexagon IT Solutions, branded ``edificecms.com`` / ``beta.edificecms.com``
and serving asset bundles from ``assets.edificecms.com`` and a
site-local ``/edi-assets/`` path.

CMS fingerprints found in every Edifice page (all live-verified on
``cobblestonephx.com/floorplans.php``):

* Inline JS config:
  ``var BUILDER_LIVE = "https://beta.edificecms.com/builder/";``
  ``var EDIFICECMS = "https://beta.edificecms.com/";``
  ``var ASSETS_DIR = "https://cobblestonephx.com/edi-assets/";``
* Asset hosts: ``assets.edificecms.com/uploads/project{N}/...``
* Page chrome classes: ``eWidget ef{ts}``, ``efheading``, ``eRow ef``,
  ``eColumn ef``, ``data-element="widget"``, ``data-widget=...``.
* Plan card chrome: ``fp-html-rent-sqft``, ``fp-html-avail-btn``,
  ``fp-href-link``, ``fpPreLoader``.

The CMS-side JS at ``window.onload`` calls a public same-vendor REST
API to populate plan cards:

  GET https://edificecms.com/myresman/public/api/front/floorplans
        ?action=get_floorplans&property_id={UUID}

returning a JSON envelope::

  {
    "status": True, "msg": "Success",
    "property": "Cobblestone Apartments",
    "data": [
      {"Id":"A1-1X1-615", "Name":"1X1", "Bedroom":1, "Bathroom":"1",
       "SquareFeet":615, "MarketRent":1340, "Rent-Min":999,
       "Rent-Max":1340, "UnitsAvailable":"9", ...},
      ...
    ],
    "special_offer": {...}, "filter": {...}
  }

For each plan with ``UnitsAvailable > 0`` the page's JS hits the unit
endpoint::

  GET https://edificecms.com/myresman/public/api/front/units
        ?action=getunits&property_id={UUID}&u={plan_id}

which returns the per-unit roster keyed under ``units[plan_id]``::

  {"status": True, ...,
   "units": {"A1-1X1-615": [
      {"UnitID":"1053", "Id":"A1-1X1-615", "Name":"1X1",
       "Bedroom":1, "Bathroom":"1", "SquareFeet":615,
       "MarketRent":"999.00", "TotalRent":"999.00",
       "UnitOccupancyStatus":"vacant", "UnitLeasedStatus":"available",
       "BuildingID":"1", "FloorLevel":"1", "Deposit":"300.00",
       "Availability":{"VacateDate":"05/01/2026",
                       "MadeReadyDate":"05/07/2026"},
       "AvailDate":"Move in Today!"},
      ...
   ]}}

Every Edifice site embeds the property's UUID in the ``getFloorPlan``
``$.ajax`` call::

  property_id: "b63cc3f8-3edf-4ec9-be58-8bf4a77455bf"

so the UUID is recoverable from the static HTML body alone — no browser,
no SPA hydration, no auth. Deterministic Tier-1.

Edifice routes apply/portal traffic to ResMan (``myresman.com/Portal/
Applicants/Availability?a=N&p=UUID``) — the detector previously caught
``myresman.com`` and routed these properties to the ResMan adapter,
which can't extract per-unit data without a portal session. The
Edifice-CMS detection (confidence 0.92) outranks ResMan's 0.90 so
Edifice wins for any HTML that carries Edifice markers AND a ResMan
apply link.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import re
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import make_unit_dict
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

_TIER = "TIER_1_API_EDIFICECMS"

_FLOORPLANS_API = (
    "https://edificecms.com/myresman/public/api/front/floorplans"
)
_UNITS_API = "https://edificecms.com/myresman/public/api/front/units"

# Property UUID lives in the inline ``getFloorPlan()`` ajax block:
#   property_id: "b63cc3f8-3edf-4ec9-be58-8bf4a77455bf"
# Also appears on the ResMan apply link as ``...&p=UUID&...`` — match
# either as a fallback. UUID is the standard 8-4-4-4-12 hex format.
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_PROPERTY_ID_RE = re.compile(
    r"""property_id\s*[:=]\s*['"]([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"""
    r"""-[0-9a-f]{4}-[0-9a-f]{12})['"]""",
    re.IGNORECASE,
)
_RESMAN_APPLY_UUID_RE = re.compile(
    r"""myresman\.com/[^"']*?[?&]p=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"""
    r"""-[0-9a-f]{4}-[0-9a-f]{12})""",
    re.IGNORECASE,
)

_DATE_MMDDYYYY_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$")


def find_edificecms_property_ids(html: str) -> list[str]:
    """Recover every plausible Edifice UUID in deterministic priority order.

    HTML entity decoding is required for apply links such as
    ``...?a=2071&amp;p=<UUID>``.  Explicit ajax ``property_id`` values come
    first, followed by ResMan apply-link values.  A generic UUID scan is used
    only when neither canonical surface is present.
    """

    if not html:
        return []
    decoded = html_lib.unescape(html).replace("\\/", "/")
    candidates: list[str] = []
    for pattern in (_PROPERTY_ID_RE, _RESMAN_APPLY_UUID_RE):
        for match in pattern.finditer(decoded):
            value = match.group(1).lower()
            if value not in candidates:
                candidates.append(value)
    return candidates


def find_edificecms_property_id(html: str) -> str | None:
    """Recover the Edifice CMS property UUID embedded in *html*.

    Two sources, in priority order:

    1. The inline ``getFloorPlan()`` ``$.ajax`` block carries a literal
       ``property_id: "<UUID>"`` argument — that's the canonical
       Edifice-side identifier and matches what the floorplans API
       expects.
    2. Same UUID is mirrored on the ResMan apply link
       (``lpp.myresman.com/Portal/Applicants/Availability?a=2071&p=UUID``)
       which a vendor-hosted Edifice site is guaranteed to render.

    Returns ``None`` only when both sources are absent — a strong signal
    that the page is NOT an Edifice CMS property after all.
    """
    candidates = find_edificecms_property_ids(html)
    return candidates[0] if candidates else None


def _rent_to_int(val: Any) -> int | None:
    """``"999.00"``, ``999``, ``"$1,340"`` → 999/1340; sentinels → None.

    Edifice mixes types — floorplan-level rent comes as int (``999``),
    unit-level rent comes as a 2-decimal string (``"999.00"``). Strip
    decimals, commas, currency symbols.
    """
    if val is None:
        return None
    if isinstance(val, bool):  # bool is an int subclass — exclude.
        return None
    if isinstance(val, (int, float)):
        try:
            v = int(val)
        except (TypeError, ValueError, OverflowError):
            return None
        return v if v > 0 else None
    s = str(val).strip()
    if not s:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return int(float(m.group(0).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _int_or_empty(val: Any) -> str:
    """Edifice ints/strs → canonical str; missing → ``""``."""
    if val is None or val == "":
        return ""
    if isinstance(val, bool):
        return ""
    if isinstance(val, (int, float)):
        try:
            return str(int(val))
        except (TypeError, ValueError, OverflowError):
            return ""
    s = str(val).strip()
    return s


def _avail_date(unit: dict[str, Any]) -> str:
    """Pick the unit's earliest leasable date.

    Edifice's ``AvailDate`` is a human-facing label ("Move in Today!",
    "Available 03/30/2026"). The structured ``Availability.MadeReadyDate``
    (MM/DD/YYYY) is the move-in-ready date; ``Availability.VacateDate``
    is when the prior resident leaves. MadeReadyDate is the right one
    for downstream consumers — ISO-format it (YYYY-MM-DD) so it parses.

    Returns ``""`` when no structured date is present (e.g. plan-level
    rows that don't carry an Availability block).
    """
    avail = unit.get("Availability")
    raw = ""
    if isinstance(avail, dict):
        # MadeReadyDate is what the leasing widget displays; prefer it
        # but fall back to VacateDate when only that exists.
        raw = str(avail.get("MadeReadyDate") or avail.get("VacateDate") or "").strip()
    if not raw:
        # Plan-level rows or older Edifice tenants may stash a parseable
        # date on the top-level ``AvailDate``. ``"Move in Today!"`` and
        # similar prose are filtered by the regex below.
        raw = str(unit.get("AvailDate") or "").strip()
    m = _DATE_MMDDYYYY_RE.match(raw)
    if not m:
        return ""
    mm, dd, yyyy = m.group(1), m.group(2), m.group(3)
    if len(yyyy) == 2:
        # Two-digit years on a CMS this current are PM Y2K cruft —
        # treat 00–69 as 20xx, 70–99 as 19xx (POSIX strptime convention).
        yi = int(yyyy)
        yyyy = f"20{yyyy}" if yi <= 69 else f"19{yyyy}"
    return f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}"


def _capture_date_iso(value: date | str | None = None) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return value.strip()
    return datetime.now(UTC).date().isoformat()


def _avail_status(
    unit: dict[str, Any], *, capture_date: date | str | None = None
) -> str:
    """Edifice unit-roster shape → AVAILABLE / UNAVAILABLE / UNKNOWN.

    Only units the API actually returns in the ``units[plan_id]`` list
    are leasable — the API filters out occupied/notice-given units
    server-side. Treat any unit-row missing both leased AND occupancy
    fields as AVAILABLE (matches the live CMS UI which renders every
    returned unit under "Available now/soon").
    """
    leased = str(unit.get("UnitLeasedStatus") or "").strip().lower()
    occupancy = str(unit.get("UnitOccupancyStatus") or "").strip().lower()
    available_date = _avail_date(unit)
    # Edifice includes on-notice apartments in a plan's positive
    # ``UnitsAvailable`` roster once they have an explicit future ready date.
    # Present occupancy therefore describes today's tenancy, not whether the
    # apartment can be leased for that published date.  This exact shape is
    # present on all five current properties (30/89 rows).  A stale/historical
    # date does not receive the exception.
    if (
        leased == "on_notice"
        and available_date
        and available_date > _capture_date_iso(capture_date)
    ):
        return "AVAILABLE"
    if leased == "available" or (occupancy == "vacant" and leased != "leased"):
        return "AVAILABLE"
    if leased in ("leased", "rented") or occupancy == "occupied":
        return "UNAVAILABLE"
    return "AVAILABLE"


def _plan_dims(plan: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """Floor-plan dimensions extracted from the floorplans envelope.

    Returns ``(name, beds, baths, sqft, plan_id)``. Empty strings on
    missing fields — downstream ``make_unit_dict`` accepts them.

    The ``Name`` field is the human-facing label ("1X1", "1X1 Partial
    Reno") — that's what the operator advertises and what the floorplan
    cards show. The ``Id`` field is the API key (e.g. "A1-1X1-615") used
    on the ``/units?u=`` lookup.
    """
    plan_id = str(plan.get("Id") or "").strip()
    name = str(plan.get("Name") or plan_id).strip()
    beds = _int_or_empty(plan.get("Bedroom"))
    baths = _int_or_empty(plan.get("Bathroom"))
    sqft = _int_or_empty(plan.get("SquareFeet"))
    return name, beds, baths, sqft, plan_id


def parse_edificecms_units(
    plan: dict[str, Any],
    units_list: list[dict[str, Any]],
    source_url: str,
    *,
    capture_date: date | str | None = None,
) -> list[dict[str, Any]]:
    """Emit one unit-level dict per row in the per-plan ``units`` array.

    Joins the floorplan-level dims (name/beds/baths/sqft) onto each
    per-unit record so a single roster pass produces fully populated
    UnitRecord rows.
    """
    name, beds, baths, sqft, _plan_id = _plan_dims(plan)
    out: list[dict[str, Any]] = []
    for u in units_list:
        if not isinstance(u, dict):
            continue
        unit_no = _int_or_empty(u.get("UnitID"))
        if not unit_no:
            # No natural unit identifier ⇒ skip (post_process would
            # demote to inferred_ anyway). Edifice ALWAYS sets UnitID,
            # but defensive against schema drift.
            continue
        # Per-unit rent is the canonical figure. TotalRent === MarketRent
        # in 9/9 sampled rows (no concessions stripping at the API layer)
        # but prefer MarketRent as the operator-published price.
        rent_i = _rent_to_int(u.get("MarketRent")) or _rent_to_int(u.get("TotalRent"))
        u_sqft = _int_or_empty(u.get("SquareFeet")) or sqft
        u_beds = _int_or_empty(u.get("Bedroom")) or beds
        u_baths = _int_or_empty(u.get("Bathroom")) or baths
        building = _int_or_empty(u.get("BuildingID"))
        floor = _int_or_empty(u.get("FloorLevel"))
        deposit = ""
        dep = u.get("Deposit")
        dep_i = _rent_to_int(dep)
        if dep_i is not None:
            deposit = str(dep_i)
        out.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=f"{u_beds} Bed" if u_beds else "",
                bedrooms=u_beds,
                bathrooms=u_baths,
                sqft=u_sqft,
                unit_number=unit_no,
                floor=floor,
                building=building,
                rent_low=rent_i,
                rent_high=rent_i,
                deposit=deposit,
                availability_status=_avail_status(u, capture_date=capture_date),
                availability_date=_avail_date(u),
                source_api_url=source_url,
                extraction_tier=_TIER,
                source_ids={
                    "edifice_unit_id": unit_no,
                    "edifice_plan_id": str(u.get("Id") or _plan_id or ""),
                },
            )
        )
    return out


def parse_edificecms_plan_summary(
    plan: dict[str, Any],
    source_url: str,
) -> dict[str, Any] | None:
    """Emit a plan-level row for floorplans with ``UnitsAvailable == 0``.

    The roster API skips empty plans, but the floorplans envelope still
    advertises rent and unit count. Plan-level rows let downstream
    consumers know the plan exists (and is unleasable) so day-over-day
    diffs can flag a plan going from N→0.
    """
    name, beds, baths, sqft, plan_id = _plan_dims(plan)
    if not name and not plan_id:
        return None
    rent_min = _rent_to_int(plan.get("Rent-Min"))
    rent_max = _rent_to_int(plan.get("Rent-Max"))
    market = _rent_to_int(plan.get("MarketRent"))
    rent_lo = rent_min if rent_min is not None else market
    rent_hi = rent_max if rent_max is not None else market
    avail_count = _int_or_empty(plan.get("UnitsAvailable"))
    unit_count = _int_or_empty(plan.get("UnitCount"))
    # UNAVAILABLE when avail=0 but plan exists in the catalog — the
    # operator hasn't pulled it from the marketing site. UNKNOWN when
    # both avail and rent are missing (catalog placeholder).
    if avail_count == "0" or avail_count == "":
        status = "UNAVAILABLE" if (rent_lo or rent_hi) else "UNKNOWN"
    else:
        status = "AVAILABLE"
    return make_unit_dict(
        floor_plan_name=name,
        bed_label=f"{beds} Bed" if beds else "",
        bedrooms=beds,
        bathrooms=baths,
        sqft=sqft,
        unit_number="",
        rent_low=rent_lo,
        rent_high=rent_hi,
        availability_status=status,
        available_units=avail_count or unit_count,
        source_api_url=source_url,
        extraction_tier=_TIER,
        source_ids={"edifice_plan_id": plan_id},
    )


def select_edificecms_catalogue(
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """Choose one identity-matched catalogue without unioning sibling data.

    Each candidate contains ``property_id``, ``response``, ``identity`` and a
    non-empty ``plan_ids`` set.  A strict superset is the aggregate catalogue
    (Newport Village's five-plan response over its two-plan phase subset).
    Identical sets are equivalent and preserve source order.  Overlapping or
    disjoint non-contained sets are ambiguous and fail closed; the caller must
    not manufacture a cross-property union.
    """
    if not candidates:
        return None, "none"
    if len(candidates) == 1:
        return candidates[0], "single"

    plan_sets = [set(candidate.get("plan_ids") or set()) for candidate in candidates]
    aggregate_indexes = [
        index
        for index, plan_ids in enumerate(plan_sets)
        if plan_ids and all(other <= plan_ids for other in plan_sets)
    ]
    if len(aggregate_indexes) == 1:
        return candidates[aggregate_indexes[0]], "aggregate_over_strict_subset"
    if plan_sets and all(plan_ids == plan_sets[0] for plan_ids in plan_sets[1:]):
        return candidates[0], "equivalent_duplicate"
    return None, "ambiguous_noncontained"


async def _fetch_json(url: str, params: dict[str, str]) -> dict[str, Any] | None:
    """Issue a GET against an Edifice CMS endpoint and parse JSON.

    Uses the shared :mod:`_probe` curl_cffi wrapper so the call
    inherits proxy / clearance-cookie support. Returns ``None`` on any
    non-200 / non-JSON response so the adapter can fall through cleanly.
    """
    # ``params`` is small (action + property_id + optional u) — encode
    # inline so we don't need to thread a session through.
    from urllib.parse import urlencode

    from ma_poc.pms.adapters._probe import probe_get

    full = f"{url}?{urlencode(params)}"
    try:
        r = probe_get(
            full,
            timeout=25,
            headers={
                "Accept": "application/json, text/plain, */*",
                # Edifice's API is permissive but a matching Referer is
                # cheap insurance against future hardening.
                "Referer": "https://www.cobblestonephx.com/",
            },
        )
    except Exception as exc:
        log.debug("edificecms fetch error url=%s err=%s", full, exc)
        return None
    if getattr(r, "status_code", 0) != 200:
        return None
    text = getattr(r, "text", "") or ""
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


class EdificeCmsAdapter:
    """Edifice CMS (Hexagon IT Solutions) public-API extractor.

    Two-call dispatch:

    1. Resolve property UUID from inline HTML (the
       ``getFloorPlan()`` ajax block, or the ResMan apply link as a
       fallback).
    2. ``GET .../floorplans?action=get_floorplans&property_id={UUID}``
       → iterate ``data[]``; for each plan with ``UnitsAvailable > 0``
       follow up with ``.../units?action=getunits&u={plan_id}``.

    Plans with ``UnitsAvailable == 0`` emit a plan-level summary so the
    downstream day-over-day diff sees the empty plans.
    """

    pms_name: str = "edificecms"

    def __init__(self) -> None:
        # Markers that uniquely identify Edifice CMS in static HTML.
        # All five are present on every Edifice page (live-verified on
        # cobblestonephx.com 2026-05-25). ``edificecms.com`` alone is
        # sufficient — the rest are belt-and-suspenders for asset-host
        # rewrites.
        self._fingerprints = [
            "edificecms.com",
            "beta.edificecms.com",
            "assets.edificecms.com",
            "/edi-assets/",
            "BUILDER_LIVE",
        ]

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    def matches_response_body(self, body: Any) -> bool:
        """Detector body-shape check — used by ``confirm_detection`` to
        validate a URL-based detection survives a body inspection."""
        if isinstance(body, bytes):
            try:
                body = body.decode("utf-8", errors="replace")
            except Exception:
                return False
        if isinstance(body, str):
            low = body.lower()
            return "edificecms.com" in low or "/edi-assets/" in low
        return False

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER)

        # 1. Recover the HTML body (L1 fetch or live page).
        html = self._read_html(ctx)
        if not html:
            result.tier_used = f"{_TIER}_NO_HTML"
            result.errors.append("EDIFICECMS: no HTML body available")
            return result

        # 2. Confirm Edifice markers — guard against a misroute.
        if not any(fp.lower() in html.lower() for fp in self._fingerprints):
            result.tier_used = f"{_TIER}_NO_FINGERPRINT"
            result.errors.append(
                "EDIFICECMS: detector matched but body lacks Edifice markers"
            )
            return result

        # 3. Recover every candidate UUID. Co-resident recommendation/apply
        # widgets can expose sibling UUIDs, so no candidate is trusted until
        # its floorplans response identifies the configured property.
        property_uuids = find_edificecms_property_ids(html)
        if not property_uuids:
            result.tier_used = f"{_TIER}_NO_PROPERTY_ID"
            result.errors.append(
                "EDIFICECMS: property_id UUID not discoverable in HTML"
            )
            return result

        # 4. Fetch and identity-check candidate floorplan catalogs.
        fp_url = _FLOORPLANS_API
        property_uuid = ""
        fp_resp: dict[str, Any] | None = None
        identity = None
        catalogue_relation = "none"
        configured_identity = bool(getattr(ctx, "property_name", "") or getattr(ctx, "address", ""))
        from ma_poc.pms.property_identity import MATCH, evaluate_from_context

        matched_catalogues: list[dict[str, Any]] = []
        for candidate_uuid in property_uuids:
            candidate_resp = await _fetch_json(
                fp_url,
                {"action": "get_floorplans", "property_id": candidate_uuid},
            )
            if not candidate_resp or not candidate_resp.get("status"):
                result.errors.append(
                    f"EDIFICECMS: floorplans API failed for property_id={candidate_uuid}"
                )
                continue
            candidate_identity = evaluate_from_context(
                ctx, observed_name=candidate_resp.get("property") or ""
            )
            if configured_identity and candidate_identity.status != MATCH:
                result.errors.append(
                    "EDIFICECMS_PROPERTY_IDENTITY_REJECTED: "
                    f"property_id={candidate_uuid} status={candidate_identity.status} "
                    f"observed={candidate_identity.observed_name!r} "
                    f"evidence={','.join(candidate_identity.evidence)}"
                )
                continue
            candidate_plans = candidate_resp.get("data")
            if not isinstance(candidate_plans, list) or not candidate_plans:
                result.errors.append(
                    "EDIFICECMS: floorplans API returned 0 plans for "
                    f"property_id={candidate_uuid}"
                )
                continue
            plan_ids = {
                str(plan.get("Id") or "").strip()
                for plan in candidate_plans
                if isinstance(plan, dict) and str(plan.get("Id") or "").strip()
            }
            matched_catalogues.append(
                {
                    "property_id": candidate_uuid,
                    "response": candidate_resp,
                    "identity": candidate_identity,
                    "plan_ids": plan_ids,
                }
            )

        selected_catalogue, catalogue_relation = select_edificecms_catalogue(
            matched_catalogues
        )
        if selected_catalogue is not None:
            property_uuid = str(selected_catalogue["property_id"])
            fp_resp = selected_catalogue["response"]
            identity = selected_catalogue["identity"]
            if len(matched_catalogues) > 1:
                rejected_ids = [
                    str(candidate["property_id"])
                    for candidate in matched_catalogues
                    if candidate is not selected_catalogue
                ]
                result.errors.append(
                    "EDIFICECMS_CATALOGUE_RELATION: "
                    f"selected={property_uuid} relation={catalogue_relation} "
                    f"other={','.join(rejected_ids)}"
                )

        if not fp_resp:
            if catalogue_relation == "ambiguous_noncontained":
                result.tier_used = f"{_TIER}_CATALOGUE_AMBIGUOUS"
                result.errors.append(
                    "EDIFICECMS_CATALOGUE_AMBIGUOUS: identity-matched UUIDs "
                    "publish non-contained plan sets"
                )
            elif any("PROPERTY_IDENTITY_REJECTED" in error for error in result.errors):
                result.tier_used = f"{_TIER}_PROPERTY_IDENTITY_REJECTED"
            else:
                result.tier_used = f"{_TIER}_FLOORPLANS_FAIL"
            return result
        plans = fp_resp.get("data")
        if not isinstance(plans, list) or not plans:
            result.tier_used = f"{_TIER}_FLOORPLANS_EMPTY"
            result.errors.append(
                "EDIFICECMS: floorplans API returned 0 plans"
            )
            return result

        # 5. For each plan with availability, hit the units endpoint and
        #    splice unit-level rows. For empties, emit a plan-level row.
        raw_units: list[dict[str, Any]] = []
        from ma_poc.pms.source_provenance import build_unit_source_provenance

        unit_response_count = 0
        fp_source_url = (
            f"{fp_url}?action=get_floorplans&property_id={property_uuid}"
        )
        for plan in plans:
            if not isinstance(plan, dict):
                continue
            plan_id = str(plan.get("Id") or "").strip()
            avail_count = _rent_to_int(plan.get("UnitsAvailable"))
            if plan_id and avail_count and avail_count > 0:
                units_resp = await _fetch_json(
                    _UNITS_API,
                    {
                        "action": "getunits",
                        "property_id": property_uuid,
                        "u": plan_id,
                    },
                )
                units_for_plan: list[dict[str, Any]] = []
                if isinstance(units_resp, dict) and units_resp.get("status"):
                    units_dict = units_resp.get("units")
                    if isinstance(units_dict, dict):
                        ulist = units_dict.get(plan_id)
                        if isinstance(ulist, list):
                            units_for_plan = ulist
                if units_for_plan:
                    unit_source_url = (
                        f"{_UNITS_API}?action=getunits&"
                        f"property_id={property_uuid}&u={plan_id}"
                    )
                    raw_units.extend(
                        parse_edificecms_units(plan, units_for_plan, unit_source_url)
                    )
                    unit_response_count += len(units_for_plan)
                    result.unit_source_provenance.append(
                        build_unit_source_provenance(
                            provider="edificecms",
                            source_url=unit_source_url,
                            body=units_resp,
                            unit_count=len(units_for_plan),
                            identity=identity,
                        )
                    )
                    continue
                # Fell through — plan claimed availability but the units
                # call returned nothing. Emit plan-level so the catalog
                # entry isn't lost.
            plan_row = parse_edificecms_plan_summary(plan, fp_source_url)
            if plan_row is not None:
                raw_units.append(plan_row)

        if not raw_units:
            result.tier_used = f"{_TIER}_EMPTY"
            result.errors.append(
                "EDIFICECMS: catalog and per-plan rosters yielded 0 rows"
            )
            return result

        # 6. Post-process + emit.
        from ma_poc.extraction.post_process import post_process

        pp = post_process(raw_units, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted == 0:
            result.tier_used = f"{_TIER}_VALIDITY_REJECTED"
            result.errors.append(
                f"EDIFICECMS: {len(raw_units)} rows failed unit_validity"
            )
            return result

        result.units = pp.units
        result.plan_summaries = pp.plan_summaries
        result.winning_url = fp_source_url
        result.tier_used = _TIER
        result.confidence = min(0.95, 0.7 + 0.04 * pp.n_admitted)
        result.api_responses.append(
            {
                "url": fp_source_url,
                "status": 200,
                "body": "<edificecms-floorplans>",
                "via": "edificecms_probe",
                "property_id": property_uuid,
                "catalogue_relation": catalogue_relation,
            }
        )
        # Catalog provenance is needed whenever plan summaries survive; if no
        # per-plan unit response produced rows, it is the sole source.
        if result.plan_summaries or unit_response_count == 0:
            result.unit_source_provenance.append(
                build_unit_source_provenance(
                    provider="edificecms",
                    source_url=fp_source_url,
                    body=fp_resp,
                    unit_count=len(result.plan_summaries)
                    if result.plan_summaries
                    else len(result.units),
                    identity=identity,
                    response_kind="floorplan_catalog",
                )
            )
        return result

    @staticmethod
    def _read_html(ctx: AdapterContext) -> str:
        """Recover the property page HTML from the L1 fetch_result."""
        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        if isinstance(body, bytes):
            try:
                return body.decode("utf-8", errors="replace")
            except Exception:
                return ""
        if isinstance(body, str):
            return body
        return ""
