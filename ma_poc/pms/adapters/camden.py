"""Camden Property Trust adapter for the exact public apartment roster.

Camden's property landing page exposes ``suggestedFloorPlans``.  That array is
only a discovery/preview surface: each plan carries one representative unit
while ``availableUnitIds`` can name apartments whose rent, move-in date and
native id belong to other rows.  Treating it as a plan x id cross-product
therefore fabricates unit values.

The authoritative public surface is bounded and first-party:

* ``/<property>/available-apartments`` lists the currently available plans;
* each ``/<slug>-floor-plan`` page embeds the exact ``floorPlan.units`` roster.

This adapter walks every advertised detail page (at most 28), binds every
response back to the configured Camden route and returned community metadata,
and emits nothing when the walk is incomplete.  Unit identity is the
community-qualified native key ``realPageCommunityId:unitId`` because Camden
North End currently reuses bare unit ids across two child communities.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from ma_poc.pms.adapters._parsing import bed_label_from, make_unit_dict, money_to_int
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.property_identity import MISMATCH, evaluate_from_context

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

CAMDEN_DETAIL_TIER = "TIER_1_DOM_CAMDEN_EXACT_DETAIL"
CAMDEN_INCOMPLETE_TIER = "TIER_1_DOM_CAMDEN_DETAIL_INCOMPLETE"
_MAX_PLAN_DETAILS = 28
_MAX_CONCURRENT_DETAILS = 4
_CAMDEN_HOST = "camdenliving.com"
# Camden authors a small number of literal parenthesised plan slugs
# (``spruce-(townhome)``, ``skyline-(midrise)``). Parentheses are the only
# extra path characters admitted; ``quote(..., safe='_-')`` percent-encodes
# them before fetching, while slash/dot/traversal characters stay rejected.
_SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_()\-]{0,119}$", re.IGNORECASE)
_NEXT_DATA_RE = re.compile(
    r"<script\b[^>]*\bid=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_ISO_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True)
class _PropertyRoute:
    city_slug: str
    community_slug: str
    root_url: str
    root_path: str

    @property
    def catalogue_url(self) -> str:
        return f"{self.root_url}/available-apartments"


@dataclass(frozen=True)
class _FetchedPage:
    requested_url: str
    final_url: str
    status: int
    body: str


@dataclass(frozen=True)
class _PlanRef:
    slug: str
    name: str
    floor_plan_id: int
    representative_unit: str
    detail_url: str


@dataclass(frozen=True)
class _Catalogue:
    route: _PropertyRoute
    community_name: str
    community_address: str
    parent_community_id: int
    plans: tuple[_PlanRef, ...]
    identity: dict[str, Any]
    last_update: str


@dataclass(frozen=True)
class _ParsedDetail:
    plan: _PlanRef
    page: _FetchedPage
    rows: tuple[dict[str, Any], ...]
    last_update: str


def _text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = int(float(str(value)))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _money(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = int(value)
    else:
        parsed = money_to_int(str(value)) or 0
    return parsed if parsed > 0 else None


def _date_only(value: Any) -> str:
    match = _ISO_DATE_RE.match(_text(value))
    return match.group(1) if match else ""


def _normal_host(url: str) -> str:
    host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _property_route(url: str) -> _PropertyRoute | None:
    """Return the exact Camden property route, stripping query/fragment.

    Only the first-party ``/apartments/<market>/<property>`` namespace is
    admitted.  Extra paths are tolerated because callers can arrive from an
    availability/detail URL, but they can never change the bound property.
    """

    raw = _text(url)
    if not raw:
        return None
    if "://" not in raw:
        raw = f"https://{raw.lstrip('/')}"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or _normal_host(raw) != _CAMDEN_HOST:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[0].casefold() != "apartments":
        return None
    city_slug, community_slug = parts[1].casefold(), parts[2].casefold()
    if not _SAFE_SLUG_RE.fullmatch(city_slug) or not _SAFE_SLUG_RE.fullmatch(community_slug):
        return None
    root_path = f"/apartments/{quote(city_slug, safe='-')}/{quote(community_slug, safe='-')}"
    root_url = urlunsplit(("https", "www.camdenliving.com", root_path, "", ""))
    return _PropertyRoute(city_slug, community_slug, root_url, root_path)


def _same_camden_path(url: str, expected_path: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and _normal_host(url) == _CAMDEN_HOST
        and parsed.path.rstrip("/").casefold() == expected_path.rstrip("/").casefold()
    )


def _next_data(body: str) -> dict[str, Any] | None:
    if not body or "__NEXT_DATA__" not in body:
        return None
    match = _NEXT_DATA_RE.search(body)
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _page_props(body: str) -> dict[str, Any] | None:
    data = _next_data(body)
    props = data.get("props") if isinstance(data, dict) else None
    page_props = props.get("pageProps") if isinstance(props, dict) else None
    return page_props if isinstance(page_props, dict) else None


def _detail_url(route: _PropertyRoute, entry: Mapping[str, Any]) -> str | None:
    slug = _text(entry.get("slug")).casefold()
    floor_plan_id = _positive_int(entry.get("realPageFloorPlanId"))
    representative = _text(entry.get("unitNumber") or entry.get("unitName"))
    if not _SAFE_SLUG_RE.fullmatch(slug) or floor_plan_id is None or not representative:
        return None
    path = f"{route.root_path}/available-apartments/{quote(slug, safe='_-')}-floor-plan"
    query = urlencode({"unit": representative, "floor": floor_plan_id})
    return urlunsplit(("https", "www.camdenliving.com", path, query, ""))


def _parse_catalogue(
    page: _FetchedPage,
    route: _PropertyRoute,
    ctx: AdapterContext,
) -> tuple[_Catalogue | None, str]:
    expected_path = f"{route.root_path}/available-apartments"
    if page.status != 200 or not page.body:
        return None, f"catalogue fetch status={page.status}"
    if not _same_camden_path(page.final_url, expected_path):
        return None, f"catalogue redirected outside bound route: {page.final_url}"

    page_props = _page_props(page.body)
    if page_props is None:
        return None, "catalogue missing parseable __NEXT_DATA__"
    if _text(page_props.get("citySlug")).casefold() != route.city_slug:
        return None, "catalogue citySlug does not match configured route"
    if _text(page_props.get("communitySlug")).casefold() != route.community_slug:
        return None, "catalogue communitySlug does not match configured route"

    data = page_props.get("data")
    community = data.get("community") if isinstance(data, Mapping) else None
    plans_raw = data.get("availableApartments") if isinstance(data, Mapping) else None
    if not isinstance(community, Mapping) or not isinstance(plans_raw, list):
        return None, "catalogue missing community or availableApartments"
    if _text(community.get("slug")).casefold() != route.community_slug:
        return None, "catalogue community metadata slug mismatch"

    community_name = _text(community.get("name"))
    community_address = _text(community.get("address"))
    parent_id = _positive_int(
        community.get("realPageParentCommunityId")
        or community.get("realPageCommunityId")
    )
    if not community_name or not community_address or parent_id is None:
        return None, "catalogue community metadata is incomplete"

    decision = evaluate_from_context(
        ctx,
        observed_name=community_name,
        observed_address=community_address,
    )
    if decision.status == MISMATCH:
        return None, f"catalogue configured identity mismatch: {','.join(decision.evidence)}"
    identity = decision.to_dict()
    identity.update(
        {
            "route_binding": "MATCH",
            "route_city_slug": route.city_slug,
            "route_community_slug": route.community_slug,
            "camden_parent_community_id": parent_id,
        }
    )

    if not plans_raw:
        return None, "catalogue publishes zero available plans"
    if len(plans_raw) > _MAX_PLAN_DETAILS:
        return None, (
            f"catalogue plan count {len(plans_raw)} exceeds bounded maximum "
            f"{_MAX_PLAN_DETAILS}"
        )

    plans: list[_PlanRef] = []
    by_slug: dict[str, _PlanRef] = {}
    for raw in plans_raw:
        if not isinstance(raw, Mapping):
            return None, "catalogue contains a non-object plan entry"
        slug = _text(raw.get("slug")).casefold()
        name = _text(raw.get("name") or (raw.get("media") or {}).get("overrideName"))
        floor_plan_id = _positive_int(raw.get("realPageFloorPlanId"))
        representative = _text(raw.get("unitNumber") or raw.get("unitName"))
        detail_url = _detail_url(route, raw)
        if not slug or not name or floor_plan_id is None or not representative or not detail_url:
            return None, f"catalogue plan entry lacks a bound detail route: {slug or '<unknown>'}"
        plan = _PlanRef(slug, name, floor_plan_id, representative, detail_url)
        previous = by_slug.get(slug)
        if previous is not None and previous != plan:
            return None, f"catalogue has conflicting duplicate plan slug: {slug}"
        if previous is None:
            by_slug[slug] = plan
            plans.append(plan)

    if len(plans) > _MAX_PLAN_DETAILS:
        return None, f"unique catalogue plan count exceeds {_MAX_PLAN_DETAILS}"
    last_update = _text(data.get("lastUpdate")) if isinstance(data, Mapping) else ""
    return (
        _Catalogue(
            route=route,
            community_name=community_name,
            community_address=community_address,
            parent_community_id=parent_id,
            plans=tuple(plans),
            identity=identity,
            last_update=last_update,
        ),
        "",
    )


def _building_from_label(full_label: str) -> str:
    parts = re.split(r"\s+-\s+", full_label, maxsplit=1)
    return parts[0].strip() if len(parts) == 2 and all(part.strip() for part in parts) else ""


def _parse_detail(
    page: _FetchedPage,
    catalogue: _Catalogue,
    plan: _PlanRef,
) -> tuple[_ParsedDetail | None, str]:
    expected_path = urlsplit(plan.detail_url).path
    if page.status != 200 or not page.body:
        return None, f"{plan.slug}: detail fetch status={page.status}"
    if not _same_camden_path(page.final_url, expected_path):
        return None, f"{plan.slug}: detail redirected outside bound route"

    page_props = _page_props(page.body)
    data = page_props.get("data") if isinstance(page_props, Mapping) else None
    community = data.get("community") if isinstance(data, Mapping) else None
    floor_plan = data.get("floorPlan") if isinstance(data, Mapping) else None
    if not isinstance(community, Mapping) or not isinstance(floor_plan, Mapping):
        return None, f"{plan.slug}: detail lacks community/floorPlan metadata"

    route = catalogue.route
    if _text(data.get("citySlug")).casefold() != route.city_slug:
        return None, f"{plan.slug}: detail citySlug mismatch"
    if _text(data.get("communitySlug")).casefold() != route.community_slug:
        return None, f"{plan.slug}: detail communitySlug mismatch"
    if _text(data.get("communityUrl")).rstrip("/").casefold() != route.root_path.casefold():
        return None, f"{plan.slug}: detail communityUrl mismatch"
    if _text(community.get("slug")).casefold() != route.community_slug:
        return None, f"{plan.slug}: detail community metadata slug mismatch"
    if _text(community.get("name")) != catalogue.community_name:
        return None, f"{plan.slug}: detail community name mismatch"
    if _text(community.get("address")) != catalogue.community_address:
        return None, f"{plan.slug}: detail community address mismatch"

    returned_slug = _text(floor_plan.get("slug")).casefold()
    returned_name = _text(floor_plan.get("name"))
    returned_id = _positive_int(floor_plan.get("realPageFloorPlanId"))
    if returned_slug != plan.slug or returned_name != plan.name or returned_id != plan.floor_plan_id:
        return None, f"{plan.slug}: detail floor-plan identity mismatch"
    expected_floor_plan_slug = f"{plan.slug}-floor-plan"
    if _text(data.get("floorPlanSlug")).casefold() != expected_floor_plan_slug:
        return None, f"{plan.slug}: detail floorPlanSlug mismatch"

    units = floor_plan.get("units")
    if not isinstance(units, list) or not units:
        return None, f"{plan.slug}: detail publishes no physical units"

    beds_raw = _text(floor_plan.get("bedrooms"))
    beds_int = 0 if beds_raw.casefold() == "studio" else _positive_int(beds_raw)
    baths = _text(floor_plan.get("bathrooms"))
    plan_sqft = _positive_int(floor_plan.get("squareFeet"))
    rows: list[dict[str, Any]] = []
    for index, unit in enumerate(units):
        if not isinstance(unit, Mapping):
            return None, f"{plan.slug}: unit[{index}] is not an object"
        community_id = _positive_int(unit.get("realPageCommunityId"))
        native_unit_id = _positive_int(unit.get("unitId"))
        full_label = _text(unit.get("unitName"))
        rent = _money(unit.get("monthlyRent"))
        sqft = _positive_int(unit.get("squareFeet")) or plan_sqft
        move_in = _date_only(unit.get("moveInDate"))
        if (
            community_id is None
            or native_unit_id is None
            or not full_label
            or rent is None
            or sqft is None
            or not move_in
        ):
            return None, f"{plan.slug}: unit[{index}] lacks exact identity/value fields"

        composite_unit_id = f"{community_id}:{native_unit_id}"
        composite_plan_id = f"{community_id}:{plan.floor_plan_id}"
        row = make_unit_dict(
            floor_plan_name=plan.name,
            bed_label=bed_label_from(beds_int, plan.name),
            bedrooms=str(beds_int) if beds_int is not None else beds_raw,
            bathrooms=baths,
            sqft=str(sqft),
            unit_number=full_label,
            unit_name=full_label,
            floor=_text(unit.get("floorNumber")),
            building=_building_from_label(full_label),
            rent_low=rent,
            rent_high=rent,
            availability_status="AVAILABLE",
            availability_date=move_in,
            lease_term=_text(unit.get("leaseTerm")),
            move_in_date=move_in,
            source_api_url=page.final_url,
            extraction_tier=CAMDEN_DETAIL_TIER,
            source_ids={
                "camden_community_unit_id": composite_unit_id,
                "camden_realpage_community_id": community_id,
                "camden_floor_plan_id": plan.floor_plan_id,
                "camden_community_floor_plan_id": composite_plan_id,
                "camden_floor_plan_slug": plan.slug,
            },
        )
        # Promote the exact community-qualified native key immediately.  The
        # registry keeps the new key UNIT_PENDING until a second run measures
        # cross-run stability, but current rows must not fall back to a plan
        # phenotype or collide on Camden's bare unit id.
        row["unit_id"] = f"camden_{community_id}_{native_unit_id}"
        total = _money(unit.get("totalMonthlyRent"))
        if total is not None and total != rent:
            row["rent_including_fees"] = total
        rows.append(row)

    return (
        _ParsedDetail(
            plan=plan,
            page=page,
            rows=tuple(rows),
            last_update=_text(data.get("lastUpdate")),
        ),
        "",
    )


def parse_camden_units(body: str, url: str) -> list[dict[str, Any]]:
    """Compatibility parser for the landing-page preview.

    The old implementation returned one representative per plan and the old
    generic fallback expanded every ``availableUnitId`` using that
    representative's values.  Neither is an exact physical roster, so this
    entry point now deliberately returns no rows.  :class:`CamdenAdapter`
    performs the bounded detail walk instead.
    """

    del body, url
    return []


def _fetch_camden_page_sync(url: str) -> _FetchedPage:
    """Fetch one first-party page without paid unlock/solver escalation."""

    try:
        from ma_poc.pms.adapters._probe import probe_get

        response = probe_get(
            url,
            unlocker=False,
            retries=1,
            timeout=20,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        status = int(getattr(response, "status_code", 0) or 0)
        body = getattr(response, "text", "") or ""
        final_url = str(getattr(response, "url", "") or url)
        return _FetchedPage(url, final_url, status, body if isinstance(body, str) else "")
    except Exception as exc:  # noqa: BLE001 - converted to explicit telemetry
        log.debug("camden direct fetch failed url=%s err=%s", url, exc)
        return _FetchedPage(url, url, 0, "")


async def _fetch_camden_page(url: str) -> _FetchedPage:
    return await asyncio.to_thread(_fetch_camden_page_sync, url)


async def _fetch_details(
    catalogue: _Catalogue,
) -> list[tuple[_PlanRef, _FetchedPage]]:
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DETAILS)

    async def one(plan: _PlanRef) -> tuple[_PlanRef, _FetchedPage]:
        async with semaphore:
            return plan, await _fetch_camden_page(plan.detail_url)

    return list(await asyncio.gather(*(one(plan) for plan in catalogue.plans)))


class CamdenAdapter:
    """Exact Camden public-roster adapter."""

    pms_name: str = "camden"
    _fingerprints: list[str] = ["camdenliving.com", "availableapartments"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        del page  # all authoritative data is in bounded first-party SSR pages
        result = AdapterResult(tier_used=CAMDEN_DETAIL_TIER)
        route = _property_route(getattr(ctx, "base_url", "") or "")
        if route is None:
            result.confidence = 0.0
            result.errors.append("camden: configured URL is not an exact Camden property route")
            return result

        catalogue_page = await _fetch_camden_page(route.catalogue_url)
        catalogue, catalogue_error = _parse_catalogue(catalogue_page, route, ctx)
        if catalogue is None:
            result.tier_used = CAMDEN_INCOMPLETE_TIER
            result.confidence = 0.0
            result.errors.append(f"camden: {catalogue_error}")
            return result

        fetched = await _fetch_details(catalogue)
        parsed_details: list[_ParsedDetail] = []
        failures: list[str] = []
        for plan, detail_page in fetched:
            parsed, error = _parse_detail(detail_page, catalogue, plan)
            if parsed is None:
                failures.append(error or f"{plan.slug}: unknown detail error")
            else:
                parsed_details.append(parsed)

        if failures or len(parsed_details) != len(catalogue.plans):
            result.tier_used = CAMDEN_INCOMPLETE_TIER
            result.confidence = 0.0
            result.winning_url = route.catalogue_url
            result.errors.append(
                "camden: incomplete exact detail walk "
                f"({len(parsed_details)}/{len(catalogue.plans)} plans); "
                + "; ".join(failures[:8])
            )
            return result

        raw_rows = [row for detail in parsed_details for row in detail.rows]
        composite_ids = [str(row.get("unit_id") or "") for row in raw_rows]
        if not raw_rows or len(set(composite_ids)) != len(composite_ids):
            result.tier_used = CAMDEN_INCOMPLETE_TIER
            result.confidence = 0.0
            result.errors.append("camden: empty or duplicate community-qualified unit roster")
            return result

        from ma_poc.extraction.post_process import post_process

        pp = post_process(raw_rows, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted != len(raw_rows):
            result.tier_used = CAMDEN_INCOMPLETE_TIER
            result.confidence = 0.0
            result.errors.append(
                "camden: exact roster failed closed after post-process "
                f"({pp.n_admitted}/{len(raw_rows)} admitted)"
            )
            return result

        result.units = pp.admitted
        result.plan_summaries = pp.plan_summaries
        result.winning_url = route.catalogue_url
        result.confidence = min(0.99, 0.94 + 0.001 * pp.n_admitted)

        from ma_poc.pms.source_provenance import (
            build_unit_source_provenance,
            response_sha256,
        )

        producing = [
            {"url": detail.page.final_url, "body": detail.page.body}
            for detail in parsed_details
        ]
        identity = dict(catalogue.identity)
        identity.update(
            {
                "detail_response_count": len(parsed_details),
                "physical_unit_count": pp.n_admitted,
                "catalogue_last_update": catalogue.last_update or None,
            }
        )
        result.api_responses.append(
            {
                "url": route.catalogue_url,
                "status": 200,
                "body": "<camden-floor-plan-detail-union>",
                "response_sha256": response_sha256(producing),
                "identity": identity,
                "via": "camden_exact_public_detail_walk",
            }
        )
        result.unit_source_provenance.append(
            build_unit_source_provenance(
                provider="camden",
                source_url=route.catalogue_url,
                body=producing,
                unit_count=pp.n_admitted,
                identity=identity,
                response_kind="floor_plan_detail_union",
                status=200,
            )
        )
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
