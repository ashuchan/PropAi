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

import asyncio
import base64
import hashlib
import logging
import random
import re
import string
import time
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

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
# 2026-07-18 (RealPage/OnSite routing lever): the OneSite "welcomehome"
# portal surface — property.onesite.realpage.com/welcomehome?siteId=NNN —
# carries the SiteId directly in the URL query (no widgetLoader.js indirection).
# ~16 timeout/generic-family props link to this surface and previously fell to
# generic because neither the detector nor the SiteId parser recognised it.
_ONESITE_SITEID_FROM_WELCOMEHOME_RE = re.compile(
    r"onesite\.realpage\.com/welcomehome/?\?[^\"'\\\s]*siteId=(\d+)",
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
_ONESITE_WORKFLOW_SITEID_RE = re.compile(
    r"/workflowstartup/v1/(\d+)/English(?:[/?]|$)",
    re.IGNORECASE,
)

# LeaseStar/RealPage floor-plan widgets publish an RPFP_config block on the
# property's own floor-plan page.  Unlike the OLL workflow surface above, CWS
# exposes unit inventory through api.ws.realpage.com using the public widget
# credential.  These patterns intentionally require the complete identity
# tuple; a loose propertyId+apiKey match is not enough to attribute inventory.
_RPFP_PROPERTY_ID_RE = re.compile(
    r"\bpropertyId\s*(?:=|:)\s*['\"]?(\d+)['\"]?",
    re.IGNORECASE,
)
_RPFP_PROPERTY_KEY_RE = re.compile(
    r"\bpropertyKey\s*(?:=|:)\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_RPFP_API_KEY_RE = re.compile(
    r"\bapiKey\s*:\s*['\"]([0-9a-f-]{36})['\"]",
    re.IGNORECASE,
)
_RPFP_API_URL_RE = re.compile(
    r"\bapiUrl\s*:\s*['\"]https://c-leasestar-api\.realpage\.com/?['\"]",
    re.IGNORECASE,
)
_RPFP_PARTNER_PROPERTY_ID_RE = re.compile(
    r"\bPartnerPropertyId['\"]?\s*:\s*['\"](\d+)['\"]",
    re.IGNORECASE,
)
_RPFP_HREF_RE = re.compile(r"\bhref\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)

_STREET_TOKEN_ALIASES = {
    "avenue": "ave",
    "boulevard": "blvd",
    "circle": "cir",
    "court": "ct",
    "drive": "dr",
    "east": "e",
    "highway": "hwy",
    "lane": "ln",
    "north": "n",
    "parkway": "pkwy",
    "road": "rd",
    "south": "s",
    "street": "st",
    "west": "w",
}


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

    # Path A2 (2026-07-18): welcomehome portal URL carries siteId in the query
    # (property.onesite.realpage.com/welcomehome?siteId=NNN). Same downstream
    # consumer — the SiteId feeds _probe_onesite_workflowstartup unchanged.
    for m in _ONESITE_SITEID_FROM_WELCOMEHOME_RE.finditer(body):
        sid = m.group(1)
        if sid and sid not in ids:
            ids.append(sid)

    # Path B (DISABLED 2026-05-24 after live validation showed it's
    # unreliable): the subdomain prefix (e.g. ``9131096aff`` in
    # ``9131096aff.onlineleasing.realpage.com``) is NOT the SiteId
    # workflowstartup expects. The actual SiteId is published only in
    # the leasing subdomain's HTML via widgetLoader.js?siteId=...
    # (see Path C below — called from _probe_onesite_workflowstartup,
    # not from _extract_onesite_site_ids since it requires HTTP).
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


# 2026-05-24 — XYZ auth-token reverse engineering
# ------------------------------------------------
# Reverse-engineered from cs-cdn.realpage.com OLL bundle
# (react2angularComponents.bundle.30b05938b8b036bb706d.js, module 68043).
# The BackendServiceInterceptor sets ``n.headers.XYZ =
# rpTokenGeneratorService.getToken(siteId)`` on every authenticated
# call to leasing.realpage.com/RP.Leasing.AppService.WebHost/*.
#
# Algorithm (verified against the example xyz token from
# www.thepointatabington.com HAR, SiteId=4646505):
#
#   parts = (
#       charGen(1)                                # 1 random alnum char
#       + md5(site_id).hex.upper()                # 32 hex chars
#       + charGen(3)                              # 3 random
#       + md5(user_agent).hex.upper()             # 32 hex chars
#       + charGen(5)                              # 5 random
#       + base64(str(timestamp_ms))               # base64 of ms epoch
#       + charGen(7)                              # 7 random
#   )
#   xyz = base64(parts)
#
# charGen pool: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
# (uppercase + lowercase + digits = 62 chars).
#
# rpCalculate is the standard MD5 algorithm (verified by the
# 1732584193, 4023233417, 2562383102, 271733878 initial state
# constants in the JS — those are MD5's A/B/C/D init values).
#
# Validation example:
#   SiteId 4646505 → md5("4646505") = BAC7950C65FF98CFE97623E891524170
#   Matches the HAR token's char[1:33] exactly.
#
# X-AuthToken + X-Phased headers are empty for unauthenticated probes
# (workflowstartup doesn't require them — only logged-in user flows do).
_XYZ_CHARGEN_POOL = string.ascii_uppercase + string.ascii_lowercase + string.digits
# Chrome 116 UA — chosen for DataDome bypass. Live validation
# 2026-05-24 across all curl_cffi impersonations: chrome120/119/124
# get blocked by DataDome (403), but chrome116/110/107/104/101 plus
# edge99/101 plus safari17/15 all return 200 OK. RealPage's OneSite
# OLL accepts the xyz token regardless of the TLS fingerprint —
# DataDome's blocklist is what filters specific recent Chrome ja3
# hashes. Stick with chrome116 to consistently bypass.
_XYZ_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
)
# curl_cffi impersonation chain — try in order, stop on first 200.
# All entries verified 2026-05-24 to bypass DataDome on
# leasing.realpage.com when the xyz token is otherwise valid.
_XYZ_IMPERSONATE_CHAIN = ("chrome116", "edge99", "safari17_0", "chrome110")


def _xyz_char_gen(n: int) -> str:
    """Generate *n* random alphanumeric characters from the OLL pool."""
    return "".join(random.choice(_XYZ_CHARGEN_POOL) for _ in range(n))


def _xyz_md5_upper(s: str) -> str:
    """MD5 of *s* as uppercase hex (mirrors rpCalculate in the OLL bundle)."""
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def _generate_xyz_token(
    site_id: str,
    user_agent: str = _XYZ_USER_AGENT,
    ts_ms: int | None = None,
) -> str:
    """Generate the XYZ auth token that leasing.realpage.com requires.

    See module-level comment block for the algorithm derivation.
    """
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    parts = (
        _xyz_char_gen(1)
        + _xyz_md5_upper(site_id)
        + _xyz_char_gen(3)
        + _xyz_md5_upper(user_agent)
        + _xyz_char_gen(5)
        + base64.b64encode(str(ts_ms).encode()).decode()
        + _xyz_char_gen(7)
    )
    return base64.b64encode(parts.encode()).decode()


# 2026-05-25 — Deep-probe residue on 195 TIER_1_API_ONESITE_WORKFLOW props
# turned up three workflowstartup-body pollution patterns:
#
#   1. Huge-rent-range. Bridgewater Apartments (SiteId 3590166) returns
#      ``MinPriceRange=1630, MaxPriceRange=8914`` on 1016sqft 2BR plans —
#      the high value is short-term-lease / model-unit pricing leaking
#      into the asking-rent envelope. Downstream rent-comparison alerts
#      and unit-level matching get derailed by these implausible ranges.
#      Empirical threshold from live HARs across 5 props: spread > $5000
#      OR max/min ratio > 3.0 → max is unreliable; collapse to min.
#
#   2. Sqft cascade gap. The current cascade was
#      ``Squarefeet → MinSquareFeet → MinUnitSquareFeet`` only. When a
#      property publishes the upper bound only — ``MaxSquareFeet`` or
#      ``MaxUnitSquareFeet`` — sqft fell through to 0 and got rewritten
#      to the -1 "unknown" sentinel downstream.
#
#   3. Placeholder-row drop. The old guard skipped a plan only when
#      ``sqft=0 AND rent_lo=0`` — but plans with no availability and
#      ``MinPriceRange=MaxPriceRange=0`` (pure placeholder) still leaked
#      a sqft-only bare-name row. Widen the drop to also fire when
#      AvailableUnits=0 and both rent bounds are 0.
_HUGE_RANGE_SPREAD_USD = 5000
_HUGE_RANGE_RATIO = 3.0


def _collapse_huge_rent_range(
    rent_lo: int | None, rent_hi: int | None
) -> tuple[int | None, int | None, bool]:
    """Detect and collapse OneSite's short-term-lease MaxPriceRange leak.

    Returns ``(rent_lo, rent_hi, clamped)``. When the high end is more
    than ``_HUGE_RANGE_SPREAD_USD`` above the low OR the ratio exceeds
    ``_HUGE_RANGE_RATIO``, set high to low (the asking 12-month rent is
    the trustworthy floor; high reflects flex-term premiums or model
    units). When inputs are inverted (lo > hi), swap them first so the
    spread/ratio check is well-defined.
    """
    if rent_lo is None or rent_hi is None:
        return rent_lo, rent_hi, False
    if rent_lo <= 0 or rent_hi <= 0:
        return rent_lo, rent_hi, False
    if rent_lo > rent_hi:
        rent_lo, rent_hi = rent_hi, rent_lo
    spread = rent_hi - rent_lo
    ratio = rent_hi / rent_lo if rent_lo else 0.0
    if spread > _HUGE_RANGE_SPREAD_USD or ratio > _HUGE_RANGE_RATIO:
        return rent_lo, rent_lo, True
    return rent_lo, rent_hi, False


def parse_onesite_workflowstartup(body: dict[str, Any], url: str) -> list[dict[str, str]]:
    """Parse a ``workflowstartup/v1/{SITE_ID}/English`` response into
    standard unit dicts.

    Walks ``Workflow.ActivityGroups[*].GroupActivities[*].Floorplans[]``
    (the ``__type`` is ``FloorplanSearchLeaseMgmtActivity``). Each
    floorplan emits one plan-level row with Name, Bedrooms, Bathrooms,
    Squarefeet (or MinSquareFeet / MaxSquareFeet / MinUnitSquareFeet /
    MaxUnitSquareFeet — first-non-zero wins), MinPriceRange,
    MaxPriceRange, AvailableUnits.

    Drops plans that carry no usable signal:
      • sqft=0 AND rent_lo=0  → call-for-pricing placeholder
      • AvailableUnits=0 AND MinPriceRange=MaxPriceRange=0  → unlisted plan

    Collapses anomalous MaxPriceRange to MinPriceRange when the spread
    or ratio implies short-term-lease leakage (see ``_collapse_huge_rent_range``).
    """
    if not isinstance(body, dict):
        return []
    workflow = body.get("Workflow") or {}
    if not isinstance(workflow, dict):
        return []
    url_site_match = _ONESITE_WORKFLOW_SITEID_RE.search(url or "")
    url_site_id = url_site_match.group(1) if url_site_match else ""
    payload_site_id = str(workflow.get("SiteId") or "").strip()
    # A cross-host RealPage response is property-scoped only when the native
    # SiteId in the payload agrees with the exact SiteId requested.  Older
    # fixtures omit Workflow.SiteId, so URL-only provenance remains accepted;
    # an explicit disagreement always fails closed.
    if url_site_id and payload_site_id and url_site_id != payload_site_id:
        return []
    source_site_id = payload_site_id or url_site_id
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
                # Widened cascade (2026-05-25): include the Max* keys
                # so single-bound publications don't fall through to 0.
                sqft = (
                    fp.get("Squarefeet")
                    or fp.get("MinSquareFeet")
                    or fp.get("MaxSquareFeet")
                    or fp.get("MinUnitSquareFeet")
                    or fp.get("MaxUnitSquareFeet")
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
                try:
                    avail_i = int(avail_count) if avail_count else 0
                except (TypeError, ValueError):
                    avail_i = 0

                # Skip income-restricted / call-for-pricing rows
                # (sqft=0 AND no rent → no useful dimension, drops past
                # validity gate as a bare-name row).
                if sqft_i == 0 and not rent_lo_i:
                    continue
                # Skip placeholder rows: unlisted plans the operator
                # carries in the catalog with no availability and no
                # price floor. Pre-fix these contributed to the
                # workflow-tier zero-rent residue.
                if avail_i == 0 and not rent_lo_i and not rent_hi_i:
                    continue

                if rent_lo_i and not rent_hi_i:
                    rent_hi_i = rent_lo_i

                # Collapse implausibly wide ranges (short-term-lease leak).
                rent_lo_i, rent_hi_i, _clamped = _collapse_huge_rent_range(rent_lo_i, rent_hi_i)

                beds_i: int | None = None
                if isinstance(beds, (int, float)):
                    beds_i = int(beds)
                elif isinstance(beds, str) and beds.isdigit():
                    beds_i = int(beds)

                baths_str = ""
                if isinstance(baths, (int, float)):
                    baths_str = str(int(baths)) if float(baths).is_integer() else str(baths)
                elif isinstance(baths, str):
                    baths_str = baths

                # 2026-07-12: expand fp["UnitIds"] into real per-unit rows.
                # The workflowstartup JSON carries the actual available unit
                # numbers per floorplan; the old code discarded them and
                # emitted ONE blank-unit_number row per plan hardcoded
                # AVAILABLE — even for floorplans with AvailableUnits=0 but a
                # published price (142 such plans in the 25-prop local sample
                # were falsely marked AVAILABLE). Validated on real
                # workflowstartup bodies from a residential IP: 252 unit-level
                # rows across 20 of 25 sample properties.
                # Decide the (unit_number, status, available_units) rows to
                # emit for this floorplan:
                #  • available + unit ids → one real unit-level row per uid
                #  • available, no unit ids → keep a plan-level AVAILABLE row
                #  • not available (but priced) → ONE plan-level UNAVAILABLE
                #    row (fixes the old hardcoded-AVAILABLE mislabel; post_
                #    process routes no-unit-id rows to plan_summaries).
                _uids = [str(u) for u in (fp.get("UnitIds") or []) if str(u).strip()]
                if avail_i > 0 and _uids:
                    _rows = [(uid, "AVAILABLE", "1") for uid in _uids]
                elif avail_i > 0:
                    _rows = [("", "AVAILABLE", str(avail_i))]
                else:
                    _rows = [("", "UNAVAILABLE", "0")]

                for _unit_no, _status, _avail in _rows:
                    unit = make_unit_dict(
                        floor_plan_name=name,
                        bed_label=bed_label_from(beds_i, name),
                        bedrooms=str(beds_i) if beds_i is not None else "",
                        bathrooms=baths_str,
                        sqft=str(sqft_i) if sqft_i else "",
                        unit_number=_unit_no,
                        rent_range=format_rent_range(rent_lo_i, rent_hi_i),
                        rent_low=rent_lo_i,
                        rent_high=rent_hi_i,
                        availability_status=_status,
                        available_units=_avail,
                        source_api_url=url,
                        extraction_tier="TIER_1_API_ONESITE_WORKFLOW",
                    )
                    if source_site_id:
                        unit["source_property_id"] = source_site_id
                    units.append(unit)
    return units


async def _probe_onesite_workflowstartup(
    ctx: AdapterContext,
) -> list[dict[str, str]]:
    """Discover SiteId from page body + hit workflowstartup directly via
    curl_cffi.

    2026-05-24 — **AUTH CHAIN SOLVED**. Reverse-engineered the
    ``xyz`` header token generator from the OLL JS bundle (see
    ``_generate_xyz_token``). The endpoint accepts our generated
    token on direct curl_cffi calls — verified live against 5
    ONESITE_NO_RESPONSE properties.

    Returns parsed unit dicts (empty list when no SiteId discovered
    or probe failed). Never raises.

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
    site_id_provenance: dict[str, tuple[str, str]] = {sid: ("marketing_page_site_id", "") for sid in site_ids}

    # Path C: try fetching the leasing subdomain to find SiteId in ITS body
    # (the marketing site might just have a link to {prefix}.onlineleasing
    # — that subdomain's HTML has the widgetLoader.js?siteId reference)
    if not site_ids:
        # 2026-07-12: some marketing sites embed the leasing subdomain link
        # backslash-/entity-escaped inside a JSON string
        # (e.g. `href=\\"https:\\/\\/8452182.onlineleasing.realpage.com\\/`),
        # which the literal-slash regex misses. Unescape a copy before the
        # search so those properties (pid 19785 class) resolve their SiteId.
        import html as _html

        _sub_hay = _html.unescape(raw_body).replace("\\/", "/").replace("\\", "")
        sub_hosts: list[str] = []
        for sub_match in _ONESITE_SUBDOMAIN_RE.finditer(_sub_hay):
            sub_host = sub_match.group(0).rstrip("/") + "/"
            if sub_host not in sub_hosts:
                sub_hosts.append(sub_host)
        # A marketing page with multiple distinct OneSite tenant links is a
        # portfolio surface, not a property-scoped proof.  Never pick the first
        # sibling by source order.
        if len(sub_hosts) == 1:
            sub_host = sub_hosts[0]
            try:
                from ma_poc.pms.adapters._probe import probe_get

                # Off-loaded for the same reason as the web_unlocker_get leg
                # below — blocking urlopen inside an `async def` parks the loop.
                r = await asyncio.to_thread(
                    probe_get,
                    sub_host,
                    timeout=15,
                    unlocker=False,
                    retries=1,
                )
                if r.status_code == 200 and r.text:
                    site_ids = _extract_onesite_site_ids(r.text, sub_host)
                    site_id_provenance.update({sid: ("published_portal_shell", sub_host) for sid in site_ids})
            except Exception:
                pass

            # Exact-URL Hyperbrowser fallback.  The marketing page published
            # this numeric onlineleasing.realpage.com URL verbatim, but that
            # shell can intermittently 403 the direct probe under load.  Use
            # at most one clean HB session to read the same public shell and
            # recover its widgetLoader SiteId.  hb_raw_get shares the global
            # per-property cap, never retries, always closes, and its session
            # hard-disables CAPTCHA solving; production sets useStealth=false.
            if not site_ids:
                try:
                    from ma_poc.config.feature_flags import hb_enabled

                    if hb_enabled():
                        from ma_poc.fetch.hyperbrowser_backend import hb_raw_get

                        hb_status, hb_body = await hb_raw_get(
                            sub_host,
                            str(getattr(ctx, "property_id", "") or ""),
                        )
                        if hb_status == 200 and hb_body:
                            site_ids = _extract_onesite_site_ids(hb_body, sub_host)
                            site_id_provenance.update(
                                {sid: ("published_portal_shell", sub_host) for sid in site_ids}
                            )
                except Exception:
                    pass

    # Path D: G5-managed sites — partnerpropertyId in g5devops summary
    if not site_ids:
        g5_match = _ONESITE_G5_SUMMARY_RE.search(raw_body)
        if g5_match:
            try:
                from ma_poc.pms.adapters._probe import probe_get

                r = await asyncio.to_thread(probe_get, g5_match.group(1), timeout=15)
                if r.status_code == 200 and r.text:
                    pm = _ONESITE_G5_PARTNER_RE.search(r.text)
                    if pm:
                        site_ids = [pm.group(1)]
                        site_id_provenance[site_ids[0]] = (
                            "g5_partner_property",
                            g5_match.group(1),
                        )
            except Exception:
                pass

    if not site_ids:
        return []

    # Derive origin + referer from the marketing page so the
    # workflowstartup endpoint sees us as a legit cross-site fetch
    # from the operator's marketing host (matches HAR behavior).
    base_url = getattr(ctx, "base_url", "") or ""
    fr = getattr(ctx, "fetch_result", None)
    if fr is not None:
        base_url = str(getattr(fr, "final_url", "") or "") or base_url
    try:
        _bp = urlparse(base_url)
        origin_host = f"{_bp.scheme}://{_bp.netloc}" if _bp.scheme and _bp.netloc else ""
    except Exception:
        origin_host = ""

    # Need raw curl_cffi to set impersonate per call — probe_get
    # hardcodes chrome120 which DataDome blocks. Import lazily.
    try:
        from curl_cffi import requests as _cc
    except ImportError:
        from ma_poc.pms.adapters._probe import probe_get, web_unlocker_get  # noqa: F401

        _cc = None
    from ma_poc.pms.adapters._probe import web_unlocker_get

    for sid in site_ids[:3]:  # cap at 3 to avoid excess probing
        url = _onesite_workflowstartup_url(sid)
        # Build the headers RealPage's BackendServiceInterceptor
        # expects. ``xyz`` is the per-call auth token computed from
        # the SiteId via MD5 + base64 (see _generate_xyz_token).
        # ``Origin`` and ``Referer`` are the marketing host —
        # workflowstartup is a cross-site CORS call from there.
        # ``X-AuthToken`` and ``X-Phased`` stay empty for the
        # unauthenticated guest flow.
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": origin_host or "https://example.com",
            "Referer": (origin_host + "/") if origin_host else "https://example.com/",
            "User-Agent": _XYZ_USER_AGENT,
            "xyz": _generate_xyz_token(sid, _XYZ_USER_AGENT),
            "X-AuthToken": "",
            "X-Phased": "",
        }
        # Walk the impersonation chain — chrome116 first because it
        # consistently bypasses DataDome on the leasing.realpage.com
        # path. Fall through on transport error or 403/datadome; stop
        # on first 200 with parseable Workflow.
        body_text = ""
        # The normal chain predates COMPLIANCE_MODE and rotates through four
        # browser TLS fingerprints after a block.  Fingerprint rotation is not
        # allowed in production.  Keep one stable, previously validated
        # browser fingerprint under compliance mode; separately authorised
        # research runs retain the legacy chain.
        from ma_poc.config.feature_flags import compliance_mode

        _impersonations = _XYZ_IMPERSONATE_CHAIN[:1] if compliance_mode() else _XYZ_IMPERSONATE_CHAIN
        for imp in _impersonations if _cc is not None else ():
            try:
                r = _cc.get(
                    url,
                    headers=headers,
                    timeout=15,
                    impersonate=imp,
                )
            except Exception as exc:
                log.debug("onesite workflowstartup imp=%s err: %s", imp, exc)
                continue
            if r.status_code == 200 and r.text:
                # Quick sanity check: server returns 200 with
                # ``"Workflow":null`` when SiteId is unknown.  Skip
                # silently so we try the next impersonation / SiteId.
                if '"Workflow":null' in r.text[:120]:
                    log.debug(
                        "onesite.workflow.siteid_unknown sid=%s imp=%s",
                        sid,
                        imp,
                    )
                    continue
                body_text = r.text
                break
            if r.status_code == 403 and "datadome" in (r.text or "").lower()[:600]:
                # This impersonation gets DD'd; try the next.
                log.debug(
                    "onesite.workflow.datadome_block imp=%s sid=%s",
                    imp,
                    sid,
                )
                continue
            # Some other status — log and move on
            log.debug(
                "onesite.workflow.unexpected_status sid=%s imp=%s status=%d",
                sid,
                imp,
                r.status_code,
            )

        # Last-resort: Web Unlocker. Only fires when every TLS
        # fingerprint got blocked. Cap-protected via
        # WEB_UNLOCKER_MAX_CALLS_PER_JOB.
        if not body_text and not compliance_mode():
            try:
                # 2026-07-27: OFF-LOADED to a thread — identical fix to
                # rentcafe.py's SecureCafe Attempt-3 leg. ``web_unlocker_get``
                # is a blocking ``urllib.request.urlopen`` (``_probe.py:313``)
                # and this is an ``async def`` (``_probe_onesite_workflowstartup``
                # at :455), so the bare call parked the whole event loop for up
                # to 30s per site id, 3 site ids per property — the 2026-07-25
                # event-loop-starvation RCA's exact shape.
                _wu = await asyncio.to_thread(web_unlocker_get, url, timeout=30)
                if _wu.status_code == 200 and _wu.text:
                    body_text = _wu.text
                    log.info(
                        "onesite.workflow.web_unlocker_rescue sid=%s url=%s",
                        sid,
                        url[:80],
                    )
            except Exception as exc:
                log.debug("onesite workflowstartup WU err sid=%s: %s", sid, exc)

        if not body_text:
            continue
        try:
            import json as _json

            body = _json.loads(body_text)
        except Exception:
            continue
        units = parse_onesite_workflowstartup(body, url)
        if units:
            provenance, portal_url = site_id_provenance.get(sid, ("", ""))
            for unit in units:
                unit["source_property_id"] = sid
                unit["source_property_provenance"] = provenance
                unit["source_portal_url"] = portal_url
            return units

    return []


def _normalise_identity_words(value: Any, *, drop_name_noise: bool = False) -> str:
    """Canonicalise roster/API identity text without fuzzy matching."""
    words = re.findall(r"[a-z0-9]+", str(value or "").lower())
    if drop_name_noise:
        words = [word for word in words if word not in {"the", "apartments", "apartment", "homes", "home"}]
    else:
        words = [_STREET_TOKEN_ALIASES.get(word, word) for word in words]
    return " ".join(words)


def _same_property_identity(details: Any, ctx: AdapterContext, property_id: str) -> bool:
    """Require CWS PropertyDetails to match both page config and roster.

    This is deliberately fail-closed: incomplete roster identity is not enough
    to adopt an unfiltered unit payload, even when the public credential works.
    """
    if not isinstance(details, dict) or str(details.get("id") or "") != property_id:
        return False
    if details.get("active") is not True:
        return False

    expected_name = _normalise_identity_words(ctx.property_name, drop_name_noise=True)
    actual_name = _normalise_identity_words(details.get("name"), drop_name_noise=True)
    expected_address = _normalise_identity_words(ctx.address)
    address = details.get("address")
    if not isinstance(address, dict):
        return False
    actual_address = _normalise_identity_words(address.get("address1"))
    expected_city = _normalise_identity_words(ctx.city)
    actual_city = _normalise_identity_words(address.get("cityName"))
    expected_state = str(ctx.state or "").strip().upper()
    actual_state = str(address.get("stateCode") or "").strip().upper()
    expected_zip = re.sub(r"\D", "", str(ctx.zip_code or ""))[:5]
    actual_zip = re.sub(r"\D", "", str(address.get("postalCode") or ""))[:5]

    return bool(
        expected_name
        and actual_name == expected_name
        and expected_address
        and actual_address == expected_address
        and expected_city
        and actual_city == expected_city
        and expected_state
        and actual_state == expected_state
        and len(expected_zip) == 5
        and actual_zip == expected_zip
    )


def _published_same_origin_rpfp_pages(body: str, base_url: str) -> list[str]:
    """Return explicitly linked same-origin floor-plan pages, never guesses."""
    try:
        base = urlparse(base_url)
    except Exception:
        return []
    if base.scheme not in {"http", "https"} or not base.hostname:
        return []

    def _host(value: str) -> str:
        return (value or "").lower().removeprefix("www.")

    candidates: list[str] = []
    for match in _RPFP_HREF_RE.finditer(body or ""):
        absolute = urljoin(base_url, match.group(1).replace("&amp;", "&"))
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or _host(parsed.hostname or "") != _host(base.hostname):
            continue
        path = parsed.path.lower()
        if "floor-plan" not in path and "floorplan" not in path:
            continue
        clean = parsed._replace(fragment="").geturl()
        if clean not in candidates:
            candidates.append(clean)
    return candidates[:3]


def _extract_rpfp_config(body: str) -> dict[str, str] | None:
    """Extract a complete RPFP identity tuple from one property page."""
    values = {
        "property_id": set(_RPFP_PROPERTY_ID_RE.findall(body or "")),
        "property_key": set(_RPFP_PROPERTY_KEY_RE.findall(body or "")),
        "api_key": set(_RPFP_API_KEY_RE.findall(body or "")),
        "partner_property_id": set(_RPFP_PARTNER_PROPERTY_ID_RE.findall(body or "")),
    }
    # Repetition of one value is normal in CMS HTML; multiple distinct
    # values indicate a portfolio/multi-property surface and must not be
    # resolved by source order.
    if not _RPFP_API_URL_RE.search(body or "") or any(len(items) != 1 for items in values.values()):
        return None
    return {key: next(iter(items)) for key, items in values.items()}


async def _fetch_rpfp_json(url: str, api_key: str, origin: str) -> Any:
    """Fetch one public CWS JSON endpoint with no proxy or retry chain."""
    import httpx

    headers = {
        "Accept": "application/json",
        "Origin": origin,
        "Referer": origin + "/",
        "x-ws-authkey": api_key,
    }
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        response = await client.get(url, headers=headers)
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except Exception:
        return None


def _parse_strict_rpfp_units(
    units_body: Any,
    floorplans_body: Any,
    *,
    property_id: str,
    property_key: str,
    partner_property_id: str,
    source_url: str,
    source_page_url: str,
) -> list[dict[str, Any]]:
    """Join and admit only native, ready, positive-rent CWS unit rows."""
    if not isinstance(floorplans_body, dict) or floorplans_body.get("status") != 200:
        return []
    fp_response = floorplans_body.get("response")
    if not isinstance(fp_response, dict) or str(fp_response.get("propertyKey") or "") != property_key:
        return []
    floorplans = fp_response.get("floorplans")
    if not isinstance(floorplans, list):
        return []
    floorplan_map = {
        str(row.get("id")): row
        for row in floorplans
        if isinstance(row, dict) and str(row.get("id") or "")
    }
    if not floorplan_map:
        return []

    if not isinstance(units_body, dict) or units_body.get("status") != 200:
        return []
    units_response = units_body.get("response")
    if not isinstance(units_response, dict) or not isinstance(units_response.get("units"), list):
        return []

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in units_response["units"]:
        if not isinstance(raw, dict):
            continue
        native_id = str(raw.get("id") or "").strip()
        unit_number = str(raw.get("unitNumber") or "").strip()
        floorplan_id = str(raw.get("floorplanId") or "").strip()
        floorplan = floorplan_map.get(floorplan_id)
        try:
            rent = int(float(raw.get("rent") or 0))
        except (TypeError, ValueError):
            rent = 0
        if not (
            raw.get("active") is True
            and raw.get("leaseStatus") == "AVAILABLE_READY"
            and native_id
            and unit_number
            and rent > 0
            and floorplan is not None
            and str(raw.get("propertyId") or "") == property_id
            and str(raw.get("partnerPropertyId") or "") == partner_property_id
        ):
            continue
        dedupe_key = (native_id, unit_number)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        beds = floorplan.get("bedRooms")
        baths = floorplan.get("bathRooms")
        try:
            beds_label_value = int(float(beds)) if beds is not None else None
        except (TypeError, ValueError):
            beds_label_value = None
        sqft = raw.get("squareFeet") or floorplan.get("minimumSquareFeet") or ""
        available_date = str(raw.get("internalAvailableDate") or "")[:10]
        row = make_unit_dict(
            floor_plan_name=str(floorplan.get("name") or ""),
            bed_label=bed_label_from(beds_label_value, str(floorplan.get("name") or "")),
            bedrooms=str(beds) if beds is not None else "",
            bathrooms=str(baths) if baths is not None else "",
            sqft=str(sqft),
            unit_number=unit_number,
            rent_range=format_rent_range(rent, rent),
            rent_low=rent,
            rent_high=rent,
            availability_status="AVAILABLE",
            available_units="1",
            availability_date=available_date,
            source_api_url=source_url,
            extraction_tier="TIER_1_API_ONESITE_RPFP_CWS",
        )
        row.update(
            {
                "source_native_unit_id": native_id,
                "source_floorplan_id": floorplan_id,
                "source_property_id": property_id,
                "source_partner_property_id": partner_property_id,
                "source_property_provenance": "same_origin_rpfp_property_details",
                "source_portal_url": source_page_url,
            }
        )
        rows.append(row)
    return rows


async def _probe_same_origin_rpfp_cws(ctx: AdapterContext) -> list[dict[str, Any]]:
    """Recover exact-property CWS inventory from a published floor-plan link."""
    fetch_result = getattr(ctx, "fetch_result", None)
    raw_body = getattr(fetch_result, "body", None) if fetch_result is not None else None
    if isinstance(raw_body, bytes):
        raw_body = raw_body.decode("utf-8", errors="replace")
    if not isinstance(raw_body, str) or not raw_body:
        return []
    base_url = str(getattr(fetch_result, "final_url", "") or ctx.base_url or "")
    pages: list[tuple[str, str]] = []
    if _extract_rpfp_config(raw_body):
        pages.append((base_url, raw_body))
    else:
        published = _published_same_origin_rpfp_pages(raw_body, base_url)
        if len(published) != 1:
            return []
        try:
            from ma_poc.pms.adapters._probe import probe_get

            page_response = await asyncio.to_thread(
                probe_get,
                published[0],
                timeout=15,
                unlocker=False,
                retries=1,
            )
        except Exception:
            return []
        if page_response.status_code != 200 or not page_response.text:
            return []
        final_page_url = str(getattr(page_response, "url", "") or published[0])
        # Redirects may not cross to another property/portfolio host.
        if not _published_same_origin_rpfp_pages(
            f'<a href="{final_page_url}">floor-plans</a>', base_url
        ):
            return []
        pages.append((final_page_url, page_response.text))

    page_url, page_body = pages[0]
    config = _extract_rpfp_config(page_body)
    if config is None:
        return []
    property_id = config["property_id"]
    origin_bits = urlparse(page_url)
    origin = f"{origin_bits.scheme}://{origin_bits.netloc}"
    api_base = f"https://api.ws.realpage.com/v2/property/{property_id}"
    details_body, floorplans_body, units_body = await asyncio.gather(
        _fetch_rpfp_json(f"{api_base}/PropertyDetails", config["api_key"], origin),
        _fetch_rpfp_json(f"{api_base}/floorplans", config["api_key"], origin),
        _fetch_rpfp_json(f"{api_base}/units", config["api_key"], origin),
    )
    if not isinstance(details_body, dict) or details_body.get("status") != 200:
        return []
    details = details_body.get("response")
    if not _same_property_identity(details, ctx, property_id):
        return []
    if str(details.get("propertyKey") or "") != config["property_key"]:
        return []
    return _parse_strict_rpfp_units(
        units_body,
        floorplans_body,
        property_id=property_id,
        property_key=config["property_key"],
        partner_property_id=config["partner_property_id"],
        source_url=f"{api_base}/units",
        source_page_url=page_url,
    )


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
                unit_number="",
                rent_range=format_rent_range(rent_lo, rent_hi),
                deposit=deposit,
                availability_status="AVAILABLE",
                availability_date=avail_dt,
                available_units=num_units,
                source_api_url=url,
                extraction_tier="TIER_1_API_ONESITE",
                source_ids={"floorplan_id": fp.get("id")}
                if fp.get("id") is not None
                else None,
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
        all_units: list[dict[str, Any]] = []
        # Track whether any RealPage-shaped response was seen at all so
        # we can distinguish "page is an empty OLL shell" from "data was
        # there but everything failed validity".
        saw_any_realpage_response = False

        async def _try_rpfp_cws() -> bool:
            """Try the identity-bound CWS fallback and populate ``result``."""
            try:
                cws_units = await _probe_same_origin_rpfp_cws(ctx)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(
                    f"onesite-rpfp-cws-error: {type(exc).__name__}: {str(exc)[:90]}"
                )
                return False
            if not cws_units:
                return False

            from ma_poc.extraction.post_process import post_process

            processed = post_process(cws_units, property_id=getattr(ctx, "property_id", None))
            if processed.n_admitted <= 0:
                result.errors.append(
                    f"ONESITE_RPFP_VALIDITY_REJECTED: {len(cws_units)} strict rows failed post_process"
                )
                return False
            result.units = list(processed.units)
            existing_plans = list(result.plan_summaries)
            result.plan_summaries = existing_plans + [
                row
                for row in processed.plan_summaries
                if row not in existing_plans
            ]
            result.tier_used = "TIER_1_API_ONESITE_RPFP_CWS"
            result.winning_url = cws_units[0].get("source_portal_url")
            result.confidence = min(0.96, 0.82 + 0.01 * processed.n_admitted)
            result.api_responses.append(
                {
                    "url": cws_units[0].get("source_api_url", ""),
                    "status": 200,
                    "body": "<identity-bound-rpfp-cws>",
                    "via": "same_origin_rpfp_property_details",
                }
            )
            return True

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
            if _pp.units:
                result.units = list(_pp.units)
                result.plan_summaries = list(_pp.plan_summaries)
                result.winning_url = result.api_responses[0].get("url") if result.api_responses else None
                result.confidence = min(0.95, 0.7 + 0.05 * len(_pp.units))
            else:
                if _pp.plan_summaries:
                    # ``/floorplans`` ids are plan identifiers, not apartment
                    # numbers. Retain the catalogue as context and continue
                    # through the native unit routes below.
                    result.plan_summaries = list(_pp.plan_summaries)
                    result.tier_used = "TIER_1_API_ONESITE_PLAN_LEVEL"
                    result.winning_url = (
                        result.api_responses[0].get("url")
                        if result.api_responses
                        else None
                    )
                    result.confidence = min(
                        0.85,
                        0.6 + 0.04 * len(_pp.plan_summaries),
                    )
                    result.errors.append(
                        f"ONESITE_PLAN_CATALOGUE: {_pp_parsed} plan rows "
                        "retained; continuing to unit-level recoveries"
                    )
                else:
                    # Responses were captured but every row failed the
                    # validity gate. Let the recovery cascade and retry path
                    # try a property-scoped native roster.
                    result.tier_used = "TIER_1_API_ONESITE_EMPTY"
                    result.confidence = 0.0
                    result.errors.append(
                        f"ONESITE_VALIDITY_REJECTED: {_pp_parsed} parsed rows "
                        f"failed unit_validity (no numeric dimension)"
                    )

        if not result.units:
            # Numeric Online Leasing roots expose a public, property-scoped
            # GetUnits roster. Prefer it to the workflowstartup floor-plan
            # route, which may require gated session mechanics and often
            # publishes only aggregates.
            try:
                from ma_poc.pms.adapters.realpage_oll import (
                    recover_onlineleasing_getunits,
                )

                portal_units = await recover_onlineleasing_getunits(ctx)
            except Exception as exc:  # noqa: BLE001 - existing workflow survives
                portal_units = None
                result.errors.append(
                    "onlineleasing-getunits-error: "
                    f"{type(exc).__name__}: {str(exc)[:90]}"
                )
            if portal_units is not None:
                existing_plans = list(result.plan_summaries)
                portal_units.plan_summaries = existing_plans + [
                    row
                    for row in portal_units.plan_summaries
                    if row not in existing_plans
                ]
                portal_units.api_responses = list(result.api_responses) + list(
                    portal_units.api_responses
                )
                return portal_units
            # Swifty-hosted marketing sites publish an exact same-origin
            # apartment roster through signed-by-page WordPress AJAX actions.
            # Prefer that native unit table when present: workflowstartup may
            # expose only plan-level UnitIds and no dates (946 MLK), while the
            # visible roster carries the actual unit number, price, floor, and
            # future availability date.  The helper is exact-marker gated and
            # returns [] on every non-Swifty OneSite property.
            try:
                from ma_poc.pms.adapters._swifty_floorplans import (
                    SWIFTY_TIER,
                    recover_swifty_floorplans,
                )

                swifty_units = await recover_swifty_floorplans(ctx)
            except Exception as exc:  # noqa: BLE001
                swifty_units = []
                result.errors.append(f"onesite-swifty-probe-error: {type(exc).__name__}: {str(exc)[:90]}")
            if swifty_units:
                from ma_poc.extraction.post_process import post_process

                _pps = post_process(swifty_units, property_id=getattr(ctx, "property_id", None))
                if _pps.units:
                    result.units = list(_pps.units)
                    existing_plans = list(result.plan_summaries)
                    result.plan_summaries = existing_plans + [
                        row
                        for row in _pps.plan_summaries
                        if row not in existing_plans
                    ]
                    result.tier_used = SWIFTY_TIER
                    result.winning_url = str(swifty_units[0].get("source_api_url") or "")
                    result.confidence = min(0.94, 0.74 + 0.04 * len(_pps.units))
                    result.api_responses.append(
                        {
                            "url": result.winning_url,
                            "status": 200,
                            "body": "<swifty-unit-ajax>",
                            "via": "onesite_swifty_native_unit_recovery",
                        }
                    )
                    return result

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
                result.errors.append(f"onesite-workflow-probe-error: {type(exc).__name__}: {str(exc)[:90]}")
            if wf_units:
                from ma_poc.extraction.post_process import post_process

                _ppw = post_process(wf_units, property_id=getattr(ctx, "property_id", None))
                # Plan-only workflow rows are routed to plan_summaries by
                # post_process.  They are useful metadata, but are not a
                # terminal unit-level success and must not block the exact
                # same-origin RPFP/CWS native-unit fallback below.
                if _ppw.units:
                    result.units = list(_ppw.units)
                    existing_plans = list(result.plan_summaries)
                    result.plan_summaries = existing_plans + [
                        row
                        for row in _ppw.plan_summaries
                        if row not in existing_plans
                    ]
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
                if len(_ppw.plan_summaries) > len(result.plan_summaries):
                    result.plan_summaries = list(_ppw.plan_summaries)
                    result.tier_used = "TIER_1_API_ONESITE_WORKFLOW_PLAN_LEVEL"
                    result.confidence = min(
                        0.85,
                        0.6 + 0.04 * len(_ppw.plan_summaries),
                    )

            if await _try_rpfp_cws():
                return result

            # No usable RealPage data at all. Two flavors:
            #   _NO_RESPONSE — no RealPage-shaped responses captured (cluster
            #                  #6 OLL-widget-shell pattern: page bodyLen ~ 386,
            #                  OneSite floorplans API never fired)
            #   _EMPTY       — RealPage responses captured but floorplans/
            #                  units lists were all empty
            if not result.plan_summaries:
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
