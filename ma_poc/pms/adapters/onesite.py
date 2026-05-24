"""
OneSite (RealPage) adapter.

Research log
------------
Web sources consulted:
  - https://www.realpage.com/property-management-software/onesite/ — OneSite product (accessed 2026-04-17)
  - RealPage API patterns from scripts/entrata.py and scrape_properties.py
Real payloads inspected (from data/runs/*/raw_api/):
  - 293707 — api.ws.realpage.com/v2/property/7824595/floorplans returning
    {status, message, response: {propertyKey, floorplans: [...]}} with fields:
    id, name, bedRooms, bathRooms, minimumSquareFeet, maximumSquareFeet,
    minimumMarketRent, maximumMarketRent, rentRange, depositAmount, numberOfUnitsDisplay
  - 293707 (run 2026-04-14) — same endpoint, identical schema
Key findings:
  - API endpoint: api.ws.realpage.com/v2/property/{property_id}/floorplans
    and api.ws.realpage.com/v2/property/{property_id}/units (may be null)
  - Response envelope: {status, message, response: {floorplans: [...]}}
  - Unit ID field: id (floorplan-level)
  - Rent field(s): minimumMarketRent/maximumMarketRent (numbers), rentRange (display string)
  - Known gotchas: /units endpoint can return null, [], or {response: null} — three
    shapes for "no availability". When /units is null, emit floorplan-level records
    with rent but no unit_number. Split-endpoint pattern: floorplans + units are
    separate API calls. OneSite URLs have numeric prefix: {id}.onlineleasing.realpage.com
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._daily_runner_parsers import (
    realpage_units_to_adapter_shape as _dr_realpage_units,
)
from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

# 2026-05-24 HAR-driven addition: OneSite OLL "workflowstartup" endpoint
# is the canonical source of unit data for marketing-shell OneSite sites
# where the homepage doesn't fire any RealPage XHRs. Discovery chain
# (live-verified across 7 HARs in batch1+2):
#
#   1. Marketing homepage links to ``{prefix}.onlineleasing.realpage.com``
#      OR the leasing subdomain itself
#   2. That subdomain's HTML loads
#      ``property.onesite.realpage.com/ollr/widgetLoader.js?siteId=({SITE_ID})``
#      — SiteId is the numeric query parameter
#   3. (Alternate, G5-managed sites) Page body links to
#      ``marketing-center-data.g5devops.com/summary/<slug>.json`` which
#      has ``"partnerName":"OneSite","partnerpropertyId":"<SITE_ID>"``
#   4. Hit ``leasing.realpage.com/RP.Leasing.AppService.WebHost/
#      workflowstartup/v1/{SITE_ID}/English?BpmId=OLL.WorkflowStartUp
#      &BpmSequence=0&LogSequence=3&ClientSessionID={UUID}`` — returns
#      10-15KB JSON
#   5. Walk ``Workflow.ActivityGroups[0].GroupActivities[0].Floorplans[]``
#      — each item has Name, Bedrooms, Bathrooms, MinSquareFeet,
#      MinPriceRange, MaxPriceRange, AvailableUnits, UnitIds[]
#
# This rescues TIER_1_API_ONESITE_NO_RESPONSE properties where the page
# is a static marketing shell that links to the OLL portal without
# loading it inline.
_ONESITE_SITEID_FROM_WIDGET_RE = re.compile(
    r"widgetLoader\.js\?siteId=(\d+)",
    re.IGNORECASE,
)
_ONESITE_SUBDOMAIN_RE = re.compile(
    r"https?://([\w-]+)\.onlineleasing\.realpage\.com",
    re.IGNORECASE,
)
_ONESITE_G5_SUMMARY_RE = re.compile(
    r'(https?://marketing-center-data\.g5devops\.com/summary/[^"\'\\\s]+\.json)',
    re.IGNORECASE,
)
_ONESITE_G5_PARTNER_RE = re.compile(
    r'"partnerName"\s*:\s*"OneSite"[^}]*?"partnerpropertyId"\s*:\s*"(\d+)"',
    re.IGNORECASE,
)


def _extract_onesite_site_ids(body: str, base_url: str) -> list[str]:
    """Discover OneSite SiteId values from a marketing/leasing page body.

    Returns a list (deduped, order preserved) of candidate numeric site
    ids the workflowstartup endpoint can be called with. Returns ``[]``
    when the body has no OneSite markers.
    """
    if not body:
        return []
    ids: list[str] = []

    # Path A: direct widgetLoader.js?siteId reference
    for m in _ONESITE_SITEID_FROM_WIDGET_RE.finditer(body):
        sid = m.group(1)
        if sid and sid not in ids:
            ids.append(sid)

    # Path B: marketing page links to {prefix}.onlineleasing.realpage.com
    # — the numeric prefix is NOT always the SiteId, but we keep it as a
    # weak candidate to fall back on
    for m in _ONESITE_SUBDOMAIN_RE.finditer(body):
        prefix = m.group(1)
        # Some prefixes have a non-numeric suffix (e.g. ``8312231b``);
        # strip non-digits to make it usable
        sid = re.sub(r"\D", "", prefix)
        if sid and sid not in ids:
            # Only add prefix-based as a candidate when no widgetLoader-
            # derived id is already in the list (the widget one is canonical)
            pass

    return ids


def _onesite_workflowstartup_url(site_id: str) -> str:
    """Build the workflowstartup URL with a fresh ClientSessionID UUID."""
    sid_uuid = str(uuid.uuid4())
    return (
        f"https://leasing.realpage.com/RP.Leasing.AppService.WebHost/"
        f"workflowstartup/v1/{site_id}/English?"
        f"BpmId=OLL.WorkflowStartUp&BpmSequence=0&LogSequence=3"
        f"&ClientSessionID={sid_uuid}"
    )


def parse_onesite_workflowstartup(
    body: dict[str, Any], url: str
) -> list[dict[str, str]]:
    """Parse a ``workflowstartup/v1/{SITE_ID}/English`` response into
    standard unit dicts.

    Walks ``Workflow.ActivityGroups[*].GroupActivities[*].Floorplans[]``
    (the ``__type`` is ``FloorplanSearchLeaseMgmtActivity``). Each
    floorplan emits one plan-level row with Name, Bedrooms, Bathrooms,
    Squarefeet (or MinSquareFeet), MinPriceRange, MaxPriceRange,
    AvailableUnits.

    Skips rows where both rent and sqft are zero (income-restricted
    or call-for-pricing placeholders).
    """
    if not isinstance(body, dict):
        return []
    workflow = body.get("Workflow") or {}
    activity_groups = workflow.get("ActivityGroups") or []
    if not isinstance(activity_groups, list):
        return []

    units: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for grp in activity_groups:
        if not isinstance(grp, dict):
            continue
        activities = grp.get("GroupActivities") or []
        if not isinstance(activities, list):
            continue
        for act in activities:
            if not isinstance(act, dict):
                continue
            fps = act.get("Floorplans") or []
            if not isinstance(fps, list):
                continue
            for fp in fps:
                if not isinstance(fp, dict):
                    continue
                fp_id = str(fp.get("Id") or fp.get("MarketingId") or "")
                if fp_id and fp_id in seen_ids:
                    continue
                if fp_id:
                    seen_ids.add(fp_id)

                name = str(fp.get("Name") or "")
                beds = fp.get("Bedrooms")
                baths = fp.get("Bathrooms")
                sqft = (
                    fp.get("Squarefeet")
                    or fp.get("MinSquareFeet")
                    or fp.get("MinUnitSquareFeet")
                    or 0
                )
                rent_lo = fp.get("MinPriceRange") or 0
                rent_hi = fp.get("MaxPriceRange") or 0
                avail_count = fp.get("AvailableUnits") or 0

                # Cast to ints — these come as numeric from the JSON
                try:
                    sqft_i = int(sqft) if sqft else 0
                except (TypeError, ValueError):
                    sqft_i = 0
                try:
                    rent_lo_i = int(rent_lo) if rent_lo else None
                    rent_hi_i = int(rent_hi) if rent_hi else None
                except (TypeError, ValueError):
                    rent_lo_i = None
                    rent_hi_i = None

                # Skip income-restricted-only or call-for-pricing rows
                # (sqft=0 AND no rent → no useful dimension, drops past
                # validity gate as a bare-name row).
                if sqft_i == 0 and not rent_lo_i:
                    continue

                if rent_lo_i and not rent_hi_i:
                    rent_hi_i = rent_lo_i

                beds_i: int | None = None
                if isinstance(beds, (int, float)):
                    beds_i = int(beds)
                elif isinstance(beds, str) and beds.isdigit():
                    beds_i = int(beds)

                baths_str = ""
                if isinstance(baths, (int, float)):
                    baths_str = (
                        str(int(baths)) if float(baths).is_integer() else str(baths)
                    )
                elif isinstance(baths, str):
                    baths_str = baths

                units.append(
                    make_unit_dict(
                        floor_plan_name=name,
                        bed_label=bed_label_from(beds_i, name),
                        bedrooms=str(beds_i) if beds_i is not None else "",
                        bathrooms=baths_str,
                        sqft=str(sqft_i) if sqft_i else "",
                        unit_number="",
                        rent_range=format_rent_range(rent_lo_i, rent_hi_i),
                        rent_low=rent_lo_i,
                        rent_high=rent_hi_i,
                        availability_status="AVAILABLE",
                        available_units=str(avail_count) if avail_count else "",
                        source_api_url=url,
                        extraction_tier="TIER_1_API_ONESITE_WORKFLOW",
                    )
                )
    return units


async def _probe_onesite_workflowstartup(
    ctx: AdapterContext,
) -> list[dict[str, str]]:
    """Discover SiteId from page body + hit workflowstartup directly via
    curl_cffi.

    Returns parsed unit dicts (empty list when no SiteId discovered or
    probe failed). Never raises.

    Tier label set on returned units is
    ``TIER_1_API_ONESITE_WORKFLOW`` so the run report can distinguish
    this fallback path from the captured-XHR path.
    """
    fr = getattr(ctx, "fetch_result", None)
    raw_body = getattr(fr, "body", None) if fr is not None else None
    if isinstance(raw_body, bytes):
        try:
            raw_body = raw_body.decode("utf-8", errors="replace")
        except Exception:
            raw_body = ""
    if not isinstance(raw_body, str) or not raw_body:
        return []

    base_url = getattr(ctx, "base_url", "") or ""
    site_ids = _extract_onesite_site_ids(raw_body, base_url)

    # Path C: try fetching the leasing subdomain to find SiteId in ITS body
    # (the marketing site might just have a link to {prefix}.onlineleasing
    # — that subdomain's HTML has the widgetLoader.js?siteId reference)
    if not site_ids:
        sub_match = _ONESITE_SUBDOMAIN_RE.search(raw_body)
        if sub_match:
            sub_host = sub_match.group(0).rstrip("/") + "/"
            try:
                from ma_poc.pms.adapters._probe import probe_get

                r = probe_get(sub_host, timeout=15)
                if r.status_code == 200 and r.text:
                    site_ids = _extract_onesite_site_ids(r.text, sub_host)
            except Exception:
                pass

    # Path D: G5-managed sites — partnerpropertyId in g5devops summary
    if not site_ids:
        g5_match = _ONESITE_G5_SUMMARY_RE.search(raw_body)
        if g5_match:
            try:
                from ma_poc.pms.adapters._probe import probe_get

                r = probe_get(g5_match.group(1), timeout=15)
                if r.status_code == 200 and r.text:
                    pm = _ONESITE_G5_PARTNER_RE.search(r.text)
                    if pm:
                        site_ids = [pm.group(1)]
            except Exception:
                pass

    if not site_ids:
        return []

    # Try each candidate site_id until one yields units
    from ma_poc.pms.adapters._probe import probe_get

    for sid in site_ids[:3]:  # cap at 3 to avoid excess probing
        url = _onesite_workflowstartup_url(sid)
        try:
            r = probe_get(url, timeout=15)
        except Exception as exc:
            log.debug("onesite workflowstartup err sid=%s: %s", sid, exc)
            continue
        if r.status_code != 200 or not r.text:
            continue
        try:
            import json as _json

            body = _json.loads(r.text)
        except Exception:
            continue
        units = parse_onesite_workflowstartup(body, url)
        if units:
            return units

    return []


def parse_realpage_floorplans(body: dict[str, Any], url: str) -> list[dict[str, str]]:
    """Parse RealPage /floorplans response into standard unit dicts."""
    units: list[dict[str, str]] = []
    response = body.get("response", {})
    if not isinstance(response, dict):
        return units
    floorplans = response.get("floorplans") or []
    if not isinstance(floorplans, list):
        return units

    for fp in floorplans:
        if not isinstance(fp, dict):
            continue
        name = str(fp.get("name") or "")
        beds_raw = fp.get("bedRooms")
        baths_raw = fp.get("bathRooms")
        beds = int(beds_raw) if beds_raw is not None and str(beds_raw).isdigit() else None
        baths = int(baths_raw) if baths_raw is not None and str(baths_raw).isdigit() else None

        sqft_lo = str(fp.get("minimumSquareFeet") or "")
        sqft_hi = str(fp.get("maximumSquareFeet") or "")
        sqft = sqft_lo if sqft_lo == sqft_hi or not sqft_hi else f"{sqft_lo}-{sqft_hi}"

        rent_lo_raw = fp.get("minimumMarketRent")
        rent_hi_raw = fp.get("maximumMarketRent")
        rent_lo = int(rent_lo_raw) if isinstance(rent_lo_raw, (int, float)) else None
        rent_hi = int(rent_hi_raw) if isinstance(rent_hi_raw, (int, float)) else None

        deposit = str(fp.get("depositAmount") or "")
        num_units = str(fp.get("numberOfUnitsDisplay") or "")
        # 2026-05-19: OneSite floorplan objects sometimes carry a first-
        # available date the adapter previously dropped (fleet-wide 0%
        # available_date on TIER_1_API_ONESITE). Alias-tolerant + additive:
        # empty when absent, so no existing output changes. schema_v2.
        # _format_date handles "NOW"/relative/2-digit forms downstream.
        avail_dt = next(
            (
                str(fp[k])
                for k in (
                    "availableDate",
                    "firstAvailableDate",
                    "dateAvailable",
                    "minimumAvailableDate",
                    "availabilityDate",
                    "available_date",
                    "minAvailableDate",
                )
                if fp.get(k)
            ),
            "",
        )

        units.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths) if baths is not None else "",
                sqft=sqft,
                unit_number=str(fp.get("id") or ""),
                rent_range=format_rent_range(rent_lo, rent_hi),
                deposit=deposit,
                availability_status="AVAILABLE",
                availability_date=avail_dt,
                available_units=num_units,
                source_api_url=url,
                extraction_tier="TIER_1_API_ONESITE",
            )
        )
    return units


def _is_realpage_response(body: Any) -> bool:
    """Check if a response body looks like a RealPage API response."""
    if not isinstance(body, dict):
        return False
    response = body.get("response")
    if isinstance(response, dict) and "floorplans" in response:
        return True
    return False


def _is_realpage_units_response(body: Any, url: str) -> bool:
    """Check for the RealPage /units endpoint shape.

    RealPage splits data across /floorplans and /units. The /units endpoint
    can return null, [], or {response: null} when nothing is available, and
    a list of unit dicts when there is. daily_runner's
    _realpage_units_from_body handles all three shapes.
    """
    if "realpage" not in url.lower():
        return False
    if "/units" not in url.lower():
        return False
    # Accept null / [] / {response: [...]}
    return True


class OneSiteAdapter:
    """OneSite (RealPage) PMS adapter."""

    pms_name: str = "onesite"
    _fingerprints: list[str] = ["onlineleasing.realpage.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from RealPage API responses captured during page load.

        Tier labels (post-2026-05-20 cluster #6 fix):

          ``TIER_1_API_ONESITE``           — real unit-level data parsed
          ``TIER_1_API_ONESITE_EMPTY``     — parsed but post_process admitted 0
                                             (validity gate rejected everything)
          ``TIER_1_API_ONESITE_NO_RESPONSE`` — no RealPage-shaped responses at
                                             all (page is an OLL widget shell
                                             that hasn't fired its API yet —
                                             cluster #6 pattern)

        Empty-exit labels (``_EMPTY``, ``_NO_RESPONSE``) trigger the
        scraper's Path B/C retry mechanism with the next-best PMS, AND
        let Step 8 generic fallback run. Previously the adapter set the
        bare success label even on no-data, blocking both recovery paths.
        """
        result = AdapterResult(tier_used="TIER_1_API_ONESITE")
        all_units: list[dict[str, str]] = []
        # Track whether any RealPage-shaped response was seen at all so
        # we can distinguish "page is an empty OLL shell" from "data was
        # there but everything failed validity".
        saw_any_realpage_response = False

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        for resp in api_responses:
            body = resp.get("body")
            url = resp.get("url", "")
            if _is_realpage_response(body) and isinstance(body, dict):
                saw_any_realpage_response = True
                units = parse_realpage_floorplans(body, url)
                if units:
                    all_units.extend(units)
                    result.api_responses.append(resp)
            elif _is_realpage_units_response(body, url):
                saw_any_realpage_response = True
                # RealPage /units endpoint — body may be null/[]/{response:[...]}
                try:
                    units = _dr_realpage_units(body, url) or []
                except Exception as exc:
                    units = []
                    result.errors.append(f"realpage-units-parse-error: {exc}")
                if units:
                    all_units.extend(units)
                    result.api_responses.append(resp)

        if all_units:
            # Stage 1 validity gate.
            from ma_poc.extraction.post_process import post_process

            _pp_parsed = len(all_units)
            _pp = post_process(all_units, property_id=getattr(ctx, "property_id", None))
            if _pp.n_admitted > 0:
                result.units = _pp.admitted
                result.plan_summaries = _pp.plan_summaries
                result.winning_url = (
                    result.api_responses[0].get("url") if result.api_responses else None
                )
                result.confidence = min(0.95, 0.7 + 0.05 * _pp.n_admitted)
            else:
                # Responses were captured but every row failed the validity
                # gate. Mark as empty-exit so retry / Step 8 generic can try.
                result.tier_used = "TIER_1_API_ONESITE_EMPTY"
                result.confidence = 0.0
                result.errors.append(
                    f"ONESITE_VALIDITY_REJECTED: {_pp_parsed} parsed rows "
                    f"failed unit_validity (no numeric dimension)"
                )
        else:
            # 2026-05-24 HAR-driven fallback: try the workflowstartup
            # endpoint directly via curl_cffi. Marketing-shell OneSite
            # sites don't fire the OLL XHRs from the homepage; this
            # probe discovers SiteId via widgetLoader.js?siteId, the
            # ``{prefix}.onlineleasing.realpage.com`` subdomain HTML,
            # or the G5 marketing-center-data summary (partnerpropertyId),
            # then calls ``workflowstartup/v1/{SITE_ID}/English`` and
            # parses ``Workflow.ActivityGroups[*].GroupActivities[*].
            # Floorplans[]`` directly.
            try:
                wf_units = await _probe_onesite_workflowstartup(ctx)
            except Exception as exc:  # noqa: BLE001
                wf_units = []
                result.errors.append(
                    f"onesite-workflow-probe-error: {type(exc).__name__}: {str(exc)[:90]}"
                )
            if wf_units:
                from ma_poc.extraction.post_process import post_process

                _ppw = post_process(
                    wf_units, property_id=getattr(ctx, "property_id", None)
                )
                if _ppw.n_admitted > 0:
                    result.units = _ppw.admitted
                    result.plan_summaries = _ppw.plan_summaries
                    result.tier_used = "TIER_1_API_ONESITE_WORKFLOW"
                    result.confidence = min(0.92, 0.7 + 0.04 * _ppw.n_admitted)
                    result.api_responses.append(
                        {
                            "url": wf_units[0].get("source_api_url", ""),
                            "status": 200,
                            "body": "<onesite-workflowstartup>",
                            "via": "onesite_workflow_probe",
                        }
                    )
                    return result

            # No usable RealPage data at all. Two flavors:
            #   _NO_RESPONSE — no RealPage-shaped responses captured (cluster
            #                  #6 OLL-widget-shell pattern: page bodyLen ~ 386,
            #                  OneSite floorplans API never fired)
            #   _EMPTY       — RealPage responses captured but floorplans/
            #                  units lists were all empty
            if saw_any_realpage_response:
                result.tier_used = "TIER_1_API_ONESITE_EMPTY"
            else:
                result.tier_used = "TIER_1_API_ONESITE_NO_RESPONSE"
            result.confidence = 0.0
            result.errors.append(
                "No RealPage/OneSite floorplan data found in captured API responses"
            )

        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
