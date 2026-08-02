"""
Knock vendor adapter — Doorway public API.

Research log
------------
Knock's web widget (``doorway.knck.io/latest/doorway.min.js``) is initialised
in static HTML with::

    window.knockDoorway.init('<public_key>', 'community', '<community_id>');

Where ``<public_key>`` is a 32-char hex application key and ``<community_id>``
is the property's identifier (16+ char hex). With those in hand, Knock's
public Doorway API returns full unit data without authentication:

  GET ``doorway-api.knockrentals.com/v1/property/community/<community_id>``
    → ``{property: {id: <numeric_id>, ...}}``

  GET ``doorway-api.knockrentals.com/v1/property/<numeric_id>/units``
    → ``{units_data: {units: [...], layouts: [...]}}``

Each unit entry observed in the wild::

    { area: 1200, available: true, availableOn: "2026-05-30",
      bathrooms: 2, bedrooms: 2, displayPrice: "2409", price: "2409",
      knockPrice: null, hidden: false, leased: false, occupied: false,
      reserved: false, layoutId: null, layoutName: null,
      name: "M6-202", propertyId: 2023560, ... }

Source: 2026-04-30 failure-recovery investigation. 26 of 38 Knock-flagged
properties recovered (619 units total) via this path.
"""

from __future__ import annotations

import html as html_lib
import re
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

# 2026-05-24: per-call capture buffer for the raw doorway-api responses.
# ``_fetch_knock_units`` resets and writes to this; the adapter reads from
# it after the fetch to surface the responses via
# ``result.api_responses.append(...)``. Single-call lifetime — fine for
# async because each fetch resets at the top.
LAST_FETCH_RAW_RESPONSES: list[dict[str, Any]] = []
_TASK_FETCH_RAW_RESPONSES: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "knock_fetch_raw_responses", default=None
)


def _reset_knock_responses() -> list[dict[str, Any]]:
    """Create a task-local capture buffer (adapter calls run concurrently)."""

    global LAST_FETCH_RAW_RESPONSES
    responses: list[dict[str, Any]] = []
    LAST_FETCH_RAW_RESPONSES = responses  # compatibility for external diagnostics
    _TASK_FETCH_RAW_RESPONSES.set(responses)
    return responses


def _current_knock_responses() -> list[dict[str, Any]]:
    responses = _TASK_FETCH_RAW_RESPONSES.get()
    return responses if responses is not None else LAST_FETCH_RAW_RESPONSES


def _capture_knock_response(response: dict[str, Any]) -> None:
    _current_knock_responses().append(response)

if TYPE_CHECKING:
    from playwright.async_api import Page

# Pattern matches the static-HTML init call. Captures (public_key, kind, id).
# 2026-05-20: ``\\?`` before each quote class so the regex also matches the
# JSON-escaped form ``knockDoorway.init(\"a8e...\",\"community\",\"69e...\")``
# emitted by Next.js / Nuxt SSR bundles (the call is inside a string-encoded
# JS source until the browser executes the bundle). Confirmed live against
# 3 raw-fetched HTMLs (flatirondistrictataustinranch, altaaptstarga,
# unionthompson) on 2026-05-20: original regex 0/3, relaxed regex 3/3.
# 2026-07-12: the public_key group was ``[a-f0-9]{20,40}`` (lowercase hex
# only), which silently failed on the newer BASE64 public keys Knock now
# emits, e.g. ``init(\"VzlINUlZMlNVRDBNN1JFTjpUWEgxOURPTjhRU1hLTlRP\",
# \"community\",\"3838514011eb718b\")`` (impressionsapts, ~57 prod props
# detected-as-knock that fell through to LLM). The community_id (group 3)
# was always well-formed; only the key class was too narrow. Broadened to
# the base64/base64url alphabet so both hex and base64 keys match. The
# community_id extraction — the only value the Tier-1 community API needs —
# is unchanged.
# 2026-07-12b: Jonah Digital sites (encoreskyline_template detection cohort)
# init the SAME Knock backend via a wrapper —
# ``JonahWidget.knock({init:['<public_key>','community','<community_id>']})``
# — with an identical (public_key, kind, community_id) argument shape. The
# prefix is now an alternation over both call forms; the three captured
# groups and the JSON-escaped-quote handling are shared. Verified: the
# community_ids from 6 such prod props resolve via the Doorway community API
# to the exact numeric ids prod link-hopped to, yielding 130 units with rent.
# Pairs with a detector reroute (jonahwidget.knock -> knock) so the Knock
# adapter is actually dispatched instead of encoreskyline_template.
_KNOCK_INIT_RE = re.compile(
    r"(?:knockDoorway\.init\s*\(|JonahWidget\.knock\s*\(\s*\{\s*init\s*:\s*\[)\s*"
    r"\\?['\"]([A-Za-z0-9+/=_-]{20,60})\\?['\"]\s*,\s*"
    r"\\?['\"](community|application|public)\\?['\"]\s*,\s*"
    r"\\?['\"]([a-zA-Z0-9_-]{8,40})\\?['\"]",
    re.IGNORECASE,
)

# Harbor Group's current property template loads Knock through a small
# dynamic-DNI config rather than a literal ``knockDoorway.init(...)`` call:
#
#     dniLibrary: "https://doorway.knck.io/latest/doorway.min.js",
#     dniId: "91011ebb76019d4d",
#     dniApiKey: "ad96e5d25f696e657111eb979d127cae"
#
# The page later calls ``init(config.dniApiKey, 'community', config.dniId)``.
# The two values are therefore the same public key and community id consumed
# by the Doorway API path above. Verified live on three separate Harbor Group
# properties on 2026-08-01 (Bridgepoint, Spring Gate, Triangle Place); their
# community payloads bound to the exact property addresses and exposed 1, 6,
# and 17 currently priced native unit rows respectively.
_KNOCK_DNI_ID_RE = re.compile(
    r"\bdniId\s*:\s*['\"]([A-Za-z0-9_-]{8,40})['\"]", re.IGNORECASE
)
_KNOCK_DNI_API_KEY_RE = re.compile(
    r"\bdniApiKey\s*:\s*['\"]([A-Za-z0-9+/=_-]{20,60})['\"]", re.IGNORECASE
)

# A co-resident Knock widget is sometimes contact/scheduling only while the
# property's sole published apply route is a RealPage OneSite portal. Keep the
# portal gate deliberately narrow: a complete hosted-portal origin, not a bare
# RealPage CDN/string marker. Escaped ``https:\/\/`` URLs are common in SSR
# payloads, so callers normalize the body before matching.
_ONESITE_PUBLISHED_PORTAL_RE = re.compile(
    r"https://[a-z0-9-]+\.onlineleasing\.realpage\.com", re.IGNORECASE
)

_RENT_INT_RE = re.compile(r"(\d[\d,]*)")


def _validate_and_record_knock_identity(
    ctx: AdapterContext, result: AdapterResult
) -> Any | None:
    """Require Knock metadata to bind the roster to the configured property."""

    from ma_poc.pms.property_identity import (
        MATCH,
        evaluate_observed_from_context,
        knock_observed_identity,
    )

    observed: dict[str, str] = {"name": "", "address": "", "city": "", "state": "", "zip": ""}
    for response in _current_knock_responses():
        candidate = knock_observed_identity(response.get("body"))
        if candidate.get("name") or candidate.get("address"):
            observed = candidate
            break
    identity = evaluate_observed_from_context(ctx, observed)
    configured = bool(getattr(ctx, "property_name", "") or getattr(ctx, "address", ""))
    if configured and identity.status != MATCH:
        result.errors.append(
            "KNOCK_PROPERTY_IDENTITY_REJECTED: "
            f"status={identity.status} evidence={','.join(identity.evidence)} "
            f"observed={identity.observed_name or identity.observed_address!r}"
        )
        return None

    from ma_poc.pms.source_provenance import build_unit_source_provenance

    for response in _current_knock_responses():
        if response.get("via") != "knock_units":
            continue
        body = response.get("body")
        count = 0
        if isinstance(body, dict):
            units_data = body.get("units_data")
            units_list = units_data.get("units") if isinstance(units_data, dict) else None
            count = len(units_list) if isinstance(units_list, list) else 0
        result.unit_source_provenance.append(
            build_unit_source_provenance(
                provider="knock",
                source_url=str(response.get("url") or ""),
                body=body,
                unit_count=count,
                identity=identity,
            )
        )
    return identity


def _to_int(v: Any) -> int | None:
    """Best-effort int conversion for Knock's mixed rent fields (str / int / float)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        n = int(v)
        return n if 0 < n < 1_000_000 else None
    if isinstance(v, str):
        m = _RENT_INT_RE.search(v)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                return None
    return None


def find_knock_ids(html: str) -> tuple[str | None, str | None, str | None]:
    """Return ``(public_key, kind, id)`` if a Knock init call is present.

    ``kind`` is one of ``community`` | ``application`` | ``public``.
    """
    if not html:
        return None, None, None
    lo = html.lower()
    if "knock" not in lo and "doorway.knck.io" not in lo:
        return None, None, None
    m = _KNOCK_INIT_RE.search(html)
    if m:
        return m.group(1), m.group(2).lower(), m.group(3)

    # Dynamic-DNI config: require both fields plus a genuine Knock Doorway
    # marker. Requiring all three avoids treating an unrelated analytics
    # ``dniId`` variable as a property-scoped inventory identifier.
    if "doorway.knck.io" in lo or "knockdoorway" in lo:
        dni_id = _KNOCK_DNI_ID_RE.search(html)
        dni_key = _KNOCK_DNI_API_KEY_RE.search(html)
        if dni_id and dni_key:
            return dni_key.group(1), "community", dni_id.group(1)
    return None, None, None


def find_published_onesite_portals(html: str) -> list[str]:
    """Return unique OneSite portal origins explicitly published in HTML."""
    if not html:
        return []
    normalized = html_lib.unescape(html).replace("\\/", "/")
    return sorted(
        {
            f"{match.group(0).lower().rstrip('/')}/"
            for match in _ONESITE_PUBLISHED_PORTAL_RE.finditer(normalized)
        }
    )


def _onesite_native_priced_units(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclude plan summaries before a OneSite result can replace empty Knock."""
    native: list[dict[str, Any]] = []
    for unit in units:
        unit_number = str(unit.get("unit_number") or "").strip()
        source_property_id = str(unit.get("source_property_id") or "").strip()
        source_url = str(unit.get("source_api_url") or "").lower()
        positive_rent = any(
            _to_int(unit.get(key)) is not None
            for key in (
                "market_rent_low",
                "market_rent_high",
                "rent_low",
                "rent_high",
                "rent",
            )
        )
        exact_workflow_source = bool(
            source_property_id
            and f"/workflowstartup/v1/{source_property_id.lower()}/" in source_url
        )
        if unit_number and positive_rent and exact_workflow_source:
            native.append(unit)
    return native


def parse_knock_units(
    units_payload: dict[str, Any], source_api_url: str = ""
) -> list[dict[str, Any]]:
    """Convert Knock's ``units_data.units`` array into standard unit dicts.

    Skips units flagged ``hidden`` / ``leased`` / ``reserved`` (no useful
    rent), and filters out any record without a price in the $200-$50K range.
    """
    units: list[dict[str, Any]] = []
    units_data = units_payload.get("units_data", {})
    raw_units = units_data.get("units") or []
    layouts_list = units_data.get("layouts") or []
    layouts: dict[Any, dict[str, Any]] = {
        layout.get("id"): layout for layout in layouts_list if isinstance(layout, dict)
    }

    for u in raw_units:
        if not isinstance(u, dict):
            continue
        if u.get("hidden") or u.get("leased") or u.get("reserved"):
            continue

        rent = (
            _to_int(u.get("price"))
            or _to_int(u.get("displayPrice"))
            or _to_int(u.get("knockPrice"))
            or _to_int(u.get("min_rent"))
            or _to_int(u.get("rent"))
        )
        if not rent or not (200 <= rent <= 50_000):
            continue

        layout_id = u.get("layoutId") or u.get("layout_id") or u.get("layout")
        layout = layouts.get(layout_id, {}) if layout_id else {}

        beds = u.get("bedrooms")
        if beds is None:
            beds = layout.get("bedrooms")
        baths = u.get("bathrooms")
        if baths is None:
            baths = layout.get("bathrooms")
        sqft = (
            _to_int(u.get("area"))
            or _to_int(u.get("square_feet"))
            or _to_int(u.get("sqft"))
            or _to_int(layout.get("area"))
            or _to_int(layout.get("square_feet"))
        )
        unit_number = (
            u.get("name") or u.get("unit_number") or u.get("apartment_number") or ""
        )
        avail = (
            u.get("availableOn")
            or u.get("available_on")
            or u.get("ready_date")
            or ""
        )
        # 2026-05-25 (canary 1ef1060 post-mortem): the prior rule was
        # ``status = AVAILABLE iff (u.available AND not u.occupied)``,
        # which mis-flagged ~8,580 of 8,597 Knock units as UNAVAILABLE
        # because Knock's `available` boolean is False/null on many
        # actively-rentable units (it tracks a different signal than
        # rentability). The units already passed the hidden/leased/
        # reserved filter at line 117 + a $200-$50,000 rent gate at
        # line 127, so by the time we're here they are real
        # rent-published offerings. Default AVAILABLE; only mark
        # UNAVAILABLE when explicitly occupied. This is what the
        # downstream consumer (and the schema_v2 Q1 fallback) needs
        # for the available_date scrape-time default to fire.
        status = "UNAVAILABLE" if u.get("occupied") else "AVAILABLE"

        # Knock exposes a stable UUID on every unit object. Preserve it as
        # both the canonical native unit_id and auditable source provenance.
        # Visible labels are not sufficient: live GSC validation found Duke
        # Manor's 169 rows collapse to only 20 distinct labels and Estes
        # Park's 61 rows to 17. Across five exact properties, all 440 eligible
        # rows had distinct UUIDs and repeated public fetches returned the
        # identical UUID sets.
        knock_unit_id = str(u.get("id") or "").strip()
        source_ids = {"knock_unit_id": knock_unit_id} if knock_unit_id else {}

        # 2026-05-19 capture-first: Knock payload carries concession as
        # `SpecialsDescription`/specials on the unit or layout. Was
        # unmapped (Knock = 9k genuine units, 0% concession). Alias-
        # tolerant; raw; empty when no active special (correct).
        concession = ""
        for _src in (u, layout):
            for _ck in ("SpecialsDescription", "specialsDescription",
                        "specials_description", "specials", "special",
                        "concession", "concessions", "leasingSpecial",
                        "incentive", "promotion", "offer"):
                _cv = _src.get(_ck) if isinstance(_src, dict) else None
                if isinstance(_cv, str) and _cv.strip():
                    concession = _cv.strip()
                    break
            if concession:
                break

        row = {
            "unit_number": str(unit_number),
            "floor_plan_name": str(u.get("layoutName") or layout.get("name") or ""),
            "bedrooms": str(beds) if beds is not None else "",
            "bathrooms": str(baths) if baths is not None else "",
            "sqft": str(sqft) if sqft else "",
            "market_rent_low": rent,
            "market_rent_high": rent,
            "rent_range": str(rent),
            "availability_status": status,
            "availability_date": str(avail)[:30],
            "building": str(u.get("buildingName") or ""),
            "concession": concession,
            "extraction_tier": "TIER_1_KNOCK_API",
            "source_ids": source_ids,
            "source_property_id": str(u.get("propertyId") or ""),
            "source_api_url": source_api_url,
        }
        if knock_unit_id:
            row["unit_id"] = f"knock_unit_id-{knock_unit_id}"
        units.append(row)
    return units


class KnockAdapter:
    """Adapter for Knock-managed properties.

    Public-API path: extract the ``community_id`` from the static page,
    then call ``doorway-api.knockrentals.com`` directly. No browser needed.
    Falls back to the generic cascade if the init call isn't found in the
    static HTML.
    """

    pms_name = "knock"

    def __init__(self) -> None:
        self._fingerprints: list[str] = [
            "doorway.knck.io",
            "knockDoorway",
            "doorway-api.knockrentals.com",
        ]

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units via Knock's Doorway API.

        Two extraction paths:

        1. **knockDoorway.init() in static HTML** (existing, 2026-04-30):
           extract ``(public_key, kind, comm_id)`` from the JS init call,
           then call ``/v1/property/community/{comm_id}``.

        2. **By-domain resolver** (added 2026-05-20 per
           ``project_jsonld_recovery_2026-05-20.md``): when the init call
           isn't in static HTML — common on Aspen Square / brand portfolio
           sites where Knock loads dynamically — query
           ``/v1/profile?code=w&domain={SITE_URL}`` to resolve a property_id
           directly from the marketing-site URL, then hit
           ``/v1/property/{property_id}/units``.

        The ``page`` argument is unused — Knock units come from a public
        JSON API, no rendering is required.
        """
        result = AdapterResult(tier_used="TIER_1_KNOCK_API")

        # Pull HTML from the L1 fetch result.
        fr = getattr(ctx, "fetch_result", None)
        body = getattr(fr, "body", None) if fr is not None else None
        html: str = ""
        if isinstance(body, bytes):
            try:
                html = body.decode("utf-8", errors="replace")
            except Exception:
                html = ""
        elif isinstance(body, str):
            html = body

        # Path 1: knockDoorway.init() in static HTML.
        public_key, kind, comm_id = find_knock_ids(html) if html else (None, None, None)
        if public_key and comm_id:
            try:
                units = await _fetch_knock_units(comm_id, kind or "community")
            except Exception as exc:
                result.errors.append(f"knock-api-error: {exc}")
                units = []
            if units:
                if _validate_and_record_knock_identity(ctx, result) is None:
                    units = []
            if units:
                result.units = units
                result.winning_url = (
                    f"https://doorway-api.knockrentals.com/v1/property/community/{comm_id}"
                )
                result.confidence = min(0.9, 0.6 + 0.02 * len(units))
                # 2026-05-24: surface raw API responses so scraper.py
                # Step 9b can pull leasingSpecial / leasingSpecialIsActive
                # out of the community-API body. Without this, concession
                # capture silently misses for every Knock site.
                for _raw in _current_knock_responses():
                    result.api_responses.append(_raw)
                return result
            result.errors.append("knock-adapter: Doorway API returned no units for community_id")

        # Path 2: by-domain resolver. Trigger when the static HTML doesn't
        # have the init call AND there's a credible signal that Knock is
        # the primary inventory backend (NOT just a UTM tracking tag on a
        # RentCafe-hosted site — see project_jsonld_recovery memo's
        # utm_knock-is-red-herring rule).
        base_url = str(getattr(ctx, "base_url", "") or "")
        if base_url and _should_try_knock_by_domain(html, base_url):
            try:
                pid, units = await _fetch_knock_units_by_domain(base_url, html)
            except Exception as exc:
                result.errors.append(
                    f"knock-by-domain-error: {type(exc).__name__}: {str(exc)[:120]}"
                )
                pid, units = None, []
            if pid and units:
                if _validate_and_record_knock_identity(ctx, result) is None:
                    units = []
            if pid and units:
                result.units = units
                result.winning_url = (
                    f"https://doorway-api.knockrentals.com/v1/property/{pid}/units"
                )
                result.tier_used = "TIER_1_KNOCK_API_BY_DOMAIN"
                result.confidence = min(0.9, 0.6 + 0.02 * len(units))
                # Surface raw API responses (see Path 1 comment for rationale)
                for _raw in _current_knock_responses():
                    result.api_responses.append(_raw)
                return result
            if pid is None:
                result.errors.append(
                    "knock-by-domain: /v1/profile resolver returned no property_id"
                )
            elif not units:
                result.errors.append(
                    f"knock-by-domain: property_id={pid} /units returned no units"
                )

        # Empty-Knock → exact published OneSite portal fallback. A 2026-08-01
        # live three-property probe (Cove at Overlake, Copper Pointe, Post
        # House) verified the marketing-page → sole portal → OLLR SiteId →
        # workflowstartup contract. Copper currently exposes 20 native priced
        # units there; Cove is plan-only and must remain empty; Post House's 2
        # native units provide the independent end-to-end control. This runs
        # only after every Knock API path failed, preserving the 62-property
        # regression guard documented in detector.py where a working Knock
        # backend must beat an apply-only RealPage link.
        published_onesite_portals = find_published_onesite_portals(html)
        if not result.units and len(published_onesite_portals) == 1:
            try:
                from ma_poc.pms.adapters.onesite import OneSiteAdapter

                onesite_result = await OneSiteAdapter().extract(page, ctx)
            except Exception as exc:
                result.errors.append(
                    "knock-onesite-fallback-error: "
                    f"{type(exc).__name__}: {str(exc)[:120]}"
                )
            else:
                native_onesite_units = _onesite_native_priced_units(
                    list(onesite_result.units or [])
                )
                if native_onesite_units:
                    onesite_result.units = native_onesite_units
                    return onesite_result

        if not (public_key and comm_id) and not result.units:
            result.errors.append("knock-adapter: no knockDoorway.init() call in HTML")

        # ── Path 3: SSR SecureCafe/RentCafe AvailUnitRow table in body ─────
        # 2026-07-16 (Lever 2): some Knock-detected operators never sync
        # inventory to Knock's Doorway DB (Path 1 → 0 units) yet server-render
        # the full unit-level RentCafe/SecureCafe ``<tr class='AvailUnitRow'>``
        # table straight into the same marketing body we already fetched.
        # Parse that body directly instead of only emitting subpage hints —
        # mirrors the marketapts page=None fix (the data is in hand).
        if (public_key and comm_id) and not result.units and "AvailUnitRow" in html:
            from ma_poc.pms.adapters.rentcafe import parse_securecafe_availableunits

            ssr_units = parse_securecafe_availableunits(html, base_url)
            if ssr_units:
                result.units = ssr_units
                result.winning_url = base_url or None
                result.tier_used = "TIER_1_KNOCK_SSR_AVAILUNITROW"
                result.confidence = min(0.9, 0.6 + 0.02 * len(ssr_units))
                return result

        # Jonah's Knock wrapper can legitimately expose an empty Doorway
        # inventory while the same exact property publishes native apartments
        # in server-rendered per-plan Jonah resources. The detector gives
        # Knock first refusal; when that backend is empty, try this narrowly
        # gated route before falling back to generic link hints. This recovered
        # Duke Manor and Booker Creek from the exact 2026-07-31 FAILED_NO_DATA
        # cohort without a browser, unlocker, or CAPTCHA handling.
        if (
            (public_key and comm_id)
            and not result.units
            and "jonahwidget.knock" in html.lower()
        ):
            try:
                from ma_poc.pms.adapters.encoreskyline_template import (
                    EncoreSkylineTemplateAdapter,
                )

                jonah_result = await EncoreSkylineTemplateAdapter().extract(page, ctx)
            except Exception as exc:
                result.errors.append(
                    f"knock-jonah-fallback-error: {type(exc).__name__}: "
                    f"{str(exc)[:120]}"
                )
            else:
                if jonah_result.units:
                    # Keep the Knock public-API captures for existing
                    # concession/provenance consumers; Jonah is the
                    # data-bearing winner.
                    jonah_result.api_responses = [
                        *result.api_responses,
                        *jonah_result.api_responses,
                    ]
                    jonah_result.errors = [*result.errors, *jonah_result.errors]
                    return jonah_result

        # ── Empty-Knock-API fallthrough (2026-05-21) ───────────────────────
        # When Knock is correctly DETECTED (knockDoorway.init in HTML) but
        # the public Doorway API returns 0 units, the operator publishes
        # inventory via SSR HTML on /floorplans rather than syncing to
        # Knock's database. Empirically: ~5 properties in T4_code_merge_cross_page
        # (lochravenapts, manchesterlake, ...) follow this exact pattern.
        # See ``project_knock_empty_api_fallthrough_2026-05-21`` memo.
        #
        # Surface the standard floor-plan paths as subpage hints so the
        # orchestrator's link-hop fetches them. The Phase 6.2 HTML-table
        # extractor + Phase 6.3 data-attr extractor (shipped 2026-05-21)
        # handle the SSR'd unit data.
        if (public_key and comm_id) and not result.units and base_url:
            _knock_empty_emit_subpage_hints(result, base_url)
        return result


async def _knock_get(c: Any, url: str, headers: dict[str, str]) -> Any:
    """GET through the per-shared-host politeness throttle.

    Every Knock call hits the single global host
    ``doorway-api.knockrentals.com`` (registrable ``knockrentals.com``), so
    N concurrent property scrapes would otherwise hammer one backend with no
    throttle. No-op unless RATELIMIT_* is configured (safe generous defaults).
    """
    from ma_poc.fetch.host_throttle import async_throttle

    async with async_throttle(url):
        return await c.get(url, headers=headers)


async def _fetch_knock_units(comm_id: str, kind: str = "community") -> list[dict[str, Any]]:
    """Two-step Doorway API fetch: community → numeric_id → units.

    This is its own coroutine so the adapter ``extract`` method stays
    focused on orchestration.

    2026-05-24: also captures the community-API response (which carries
    ``property.data.leasing.terms.leasingSpecial``) into the module-level
    ``LAST_FETCH_RAW_RESPONSES`` so the adapter can surface it via
    ``result.api_responses.append(...)``. This is what scraper.py Step 9b
    reads to pull concession text out of Knock API responses.
    """
    import httpx

    # Reset capture buffer for this call (single-threaded inside an
    # async fetch; only the current adapter consumes it).
    _reset_knock_responses()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Origin": "https://doorway.knck.io",
        "Accept": "application/json",
    }
    base = "https://doorway-api.knockrentals.com/v1/property"
    if kind == "numeric_property":
        # Community API was already short-circuited to a numeric id by the
        # caller. Bind it through the public metadata endpoint before /units.
        metadata_url = f"{base}/{comm_id}"
        units_url = f"{base}/{comm_id}/units"
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
            mr = await _knock_get(c, metadata_url, headers)
            if mr.status_code == 200:
                try:
                    metadata_body = mr.json()
                except Exception:
                    metadata_body = None
                if isinstance(metadata_body, dict):
                    _capture_knock_response({
                        "url": metadata_url,
                        "status": 200,
                        "body": metadata_body,
                        "via": "knock_property_metadata",
                    })
            r = await _knock_get(c, units_url, headers)
            if r.status_code != 200:
                return []
            try:
                units_body = r.json()
                _capture_knock_response({
                    "url": units_url,
                    "status": 200,
                    "body": units_body,
                    "via": "knock_units",
                })
                return parse_knock_units(units_body, source_api_url=units_url)
            except Exception:
                return []

    # Community-keyed: fetch property meta first.
    community_url = f"{base}/community/{comm_id}"
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
        r = await _knock_get(c, community_url, headers)
        if r.status_code != 200:
            return []
        try:
            community_body = r.json()
            prop_data = community_body.get("property") or {}
        except Exception:
            return []
        # 2026-05-24: capture the community-API body so the adapter can
        # surface it for the scraper's concession scan. This is where
        # ``property.data.leasing.terms.leasingSpecial`` lives —
        # confirmed on livebrez.com / hamburgfarmslex.com HARs.
        _capture_knock_response({
            "url": community_url,
            "status": 200,
            "body": community_body,
            "via": "knock_community",
        })
        numeric_id = prop_data.get("id")
        if not numeric_id:
            return []
        units_url = f"{base}/{numeric_id}/units"
        r2 = await _knock_get(c, units_url, headers)
        if r2.status_code != 200:
            return []
        try:
            units_body = r2.json()
            _capture_knock_response({
                "url": units_url,
                "status": 200,
                "body": units_body,
                "via": "knock_units",
            })
            return parse_knock_units(units_body, source_api_url=units_url)
        except Exception:
            return []


# 2026-05-20: Knock-by-domain fallback signal detection.
#
# The JSON-LD recovery probe (project_jsonld_recovery_2026-05-20.md)
# confirmed two reliable signals AND one red-herring:
#
#   ✓ doorway-api.knockrentals.com URL in static HTML (any context) → Knock
#   ✓ knockrentals.com/widget URL → Knock
#   ✓ Aspen Square brand portfolio (aspensquare.com/apartments/{state}/{city}/{slug})
#     — verified live: every Aspen Square site has Knock as primary inventory
#   ✗ ``?utm_knock=`` URL param ALONE is NOT reliable — RentCafe-hosted
#     properties (10X Iona Lakes, Main Street Square) use this UTM for lead
#     tracking while their actual inventory lives in RentCafe + SecureCafe.
#     Only treat utm_knock as a Knock signal when RentCafe is ABSENT.

_KNOCK_API_HOST_RE = re.compile(
    r"doorway-api\.knockrentals\.com|knockrentals\.com/widget", re.IGNORECASE
)
_RENTCAFE_PRESENCE_RE = re.compile(r"resource\.rentcafe\.com", re.IGNORECASE)
_ASPEN_SQUARE_URL_RE = re.compile(
    r"https?://(?:www\.)?aspensquare\.com/apartments/[a-z-]+/[a-z-]+/[a-z0-9-]+",
    re.IGNORECASE,
)

# 2026-05-20 (post-canary e2e probe): Aspen Square and similar Knock-
# backed brands embed the Knock community hash in their static SSR HTML
# as a JSON-stringified config blob — typically inside a Next.js bundle
# or React Server Component data dump. The actual call is
# ``knockDoorway.init(<api_token>, "community", <community_hash>)`` but
# that call is INSIDE a string-encoded JS module that the browser
# executes — so the literal init call doesn't appear in static HTML.
# What DOES appear is the SSR-emitted config:
#
#   ":\"community\",\"enabled\":true,\"propertyId\":\"<16-char hex>\","
#   "apiToken\":\"<32-char hex>\""
#
# Captures both the unescaped (``"propertyId":"<hex>"``) and JSON-
# escaped (``\"propertyId\":\"<hex>\"``) forms.
# Verified live 2026-05-20 against 4 Aspen Square properties (Adley
# 72nd, The Avenue Cabot, Edgewood Court, Country Manor). All four
# carry a UNIQUE community hash + the SHARED Aspen Square api_token
# (7319ad04f51311e9abf012b98e995a24).
_KNOCK_COMMUNITY_HASH_RE = re.compile(
    r'propertyId\\?"\s*:\s*\\?"([a-f0-9]{14,18})\\?"', re.IGNORECASE
)


def find_knock_community_hash(html: str) -> str | None:
    """Extract the Knock community hash from a JSON-embedded config blob.

    Returns the 16-char hex hash on match, ``None`` otherwise. Used by the
    by-domain resolver for sites (like Aspen Square) that don't fire the
    ``knockDoorway.init()`` literal call in static HTML but DO ship the
    config object via SSR.
    """
    if not html:
        return None
    m = _KNOCK_COMMUNITY_HASH_RE.search(html)
    return m.group(1) if m else None


def _should_try_knock_by_domain(html: str, base_url: str) -> bool:
    """Decide whether to fire the Knock-by-domain resolver.

    Returns ``True`` only when there's positive evidence Knock is the
    primary inventory backend (not just a UTM-tracking layer on a
    RentCafe-hosted property).
    """
    # Aspen Square brand always uses Knock — match the URL directly without
    # requiring the JS bundle to expose the API host in static HTML.
    if base_url and _ASPEN_SQUARE_URL_RE.match(base_url):
        return True
    if not html:
        return False
    lo = html.lower()
    # Hard signal: Knock API host referenced anywhere in the HTML.
    if _KNOCK_API_HOST_RE.search(html):
        # Exclude RentCafe-hosted properties — Knock is just lead tracking
        # there, the inventory lives in RentCafe / SecureCafe.
        if _RENTCAFE_PRESENCE_RE.search(html):
            return False
        return True
    # Soft signal: ``utm_knock=`` URL param, but only when there's no
    # RentCafe CDN load (otherwise it's the utm_knock red-herring case).
    if "utm_knock=" in lo and not _RENTCAFE_PRESENCE_RE.search(html):
        return True
    return False


async def _fetch_knock_units_by_domain(
    base_url: str,
    html: str = "",
) -> tuple[str | None, list[dict[str, Any]]]:
    """Resolve property_id from marketing-site URL via Knock Doorway,
    then fetch units.

    Two-path strategy (2026-05-20):

    1. **Community-hash path** (preferred, verified 4/4 on Aspen Square):
       Extract the community hash from ``html`` (SSR-embedded JSON config),
       call ``/v1/property/community/{hash}`` to bootstrap and get the
       numeric property_id, then call ``/v1/property/{pid}/units``. This
       is the ONLY path that works on Aspen Square sites — the
       ``/v1/profile?domain=`` resolver returns 400 without a community
       bootstrap first.

    2. **Profile-by-domain path** (legacy fallback): call ``/v1/profile``
       with the URL as the ``domain`` param. Works on sites that publish
       the API host in static HTML but don't ship the SSR config blob.

    Args:
        base_url: The property's marketing-site URL.
        html: Static HTML body (optional). When supplied, the community-
            hash path is tried first.

    Returns:
        ``(property_id, units)``. ``property_id`` is ``None`` when both
        paths return nothing; ``units`` is the parsed list (may be empty
        if the property exists in Knock but has no available units).

    Never raises — exceptions return ``(None, [])``.
    """
    from urllib.parse import quote

    import httpx

    _reset_knock_responses()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Origin": "https://doorway.knck.io",
        "Accept": "application/json",
    }

    async def _fetch_units(c: httpx.AsyncClient, pid_str: str) -> list[dict[str, Any]]:
        metadata_url = f"https://doorway-api.knockrentals.com/v1/property/{pid_str}"
        mr = await _knock_get(c, metadata_url, headers)
        if mr.status_code == 200:
            try:
                metadata_body = mr.json()
            except Exception:
                metadata_body = None
            if isinstance(metadata_body, dict):
                _capture_knock_response({
                    "url": metadata_url,
                    "status": 200,
                    "body": metadata_body,
                    "via": "knock_property_metadata",
                })
        units_url = f"https://doorway-api.knockrentals.com/v1/property/{pid_str}/units"
        ur = await _knock_get(c, units_url, headers)
        if ur.status_code != 200:
            return []
        try:
            units_body = ur.json()
            _capture_knock_response({
                "url": units_url,
                "status": 200,
                "body": units_body,
                "via": "knock_units",
            })
            return parse_knock_units(units_body, source_api_url=units_url)
        except Exception:
            return []

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
            # Path 1: community-hash from SSR-embedded JSON config.
            community_hash = find_knock_community_hash(html) if html else None
            if community_hash:
                boot_url = (
                    f"https://doorway-api.knockrentals.com/v1/property/"
                    f"community/{community_hash}"
                )
                br = await _knock_get(c, boot_url, headers)
                if br.status_code == 200:
                    try:
                        boot_body = br.json()
                    except Exception:
                        boot_body = {}
                    _capture_knock_response({
                        "url": boot_url,
                        "status": 200,
                        "body": boot_body,
                        "via": "knock_community",
                    })
                    pid = (boot_body.get("property") or {}).get("id")
                    if pid:
                        pid_str = str(pid)
                        return pid_str, await _fetch_units(c, pid_str)

            # Path 2: legacy /v1/profile?domain= resolver.
            profile_url = (
                "https://doorway-api.knockrentals.com/v1/profile"
                f"?code=w&domain={quote(base_url, safe='')}&refresh=true"
            )
            pr = await _knock_get(c, profile_url, headers)
            if pr.status_code != 200:
                return None, []
            try:
                profile_body = pr.json()
            except Exception:
                return None, []
            _capture_knock_response({
                "url": profile_url,
                "status": 200,
                "body": profile_body,
                "via": "knock_profile",
            })
            pid = (profile_body.get("profile") or {}).get("property")
            if not pid:
                return None, []
            pid_str = str(pid)
            return pid_str, await _fetch_units(c, pid_str)
    except Exception:
        return None, []


# ── Empty-Knock-API fallthrough helper (2026-05-21) ─────────────────────────

# Standard floor-plan paths to try when Knock detection succeeds but the
# Doorway API returns no units. Ordered by observed prevalence —
# ``/floorplans`` (no hyphen) is the dominant form among Knock-empty
# properties (lochravenapts, manchesterlake); the hyphenated and
# ``/availability`` variants exist on a small tail. The orchestrator's
# link-hop tries each in order and stops at the first 200-with-units.
_KNOCK_EMPTY_FALLTHROUGH_PATHS: tuple[str, ...] = (
    "/floorplans",
    "/floor-plans",
    "/availability",
)


def _knock_empty_emit_subpage_hints(result: AdapterResult, base_url: str) -> None:
    """Append ``/floorplans`` family URLs as subpage hints on ``result``.

    Idempotent: if ``result._embedded_floorplan_subpage_hints`` already
    contains any of the candidate URLs, that one is not re-added. The
    hint shape ``(url, parser_id)`` matches the existing emission
    pattern in ``_html_extract.detect_floorplan_subpage_urls``.
    """
    from urllib.parse import urljoin, urlparse

    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return  # malformed base_url — do nothing rather than emit bad URLs

    # Honour same-host redirects implicitly — the orchestrator will follow
    # them. We always anchor on the configured base_url so the hop stays
    # on the property's own domain.
    existing = list(getattr(result, "_embedded_floorplan_subpage_hints", None) or [])
    existing_urls: set[str] = {u for u, _ in existing}

    additions: list[tuple[str, str]] = []
    for path in _KNOCK_EMPTY_FALLTHROUGH_PATHS:
        candidate = urljoin(base_url, path)
        if candidate in existing_urls:
            continue
        additions.append((candidate, "knock_empty_fallthrough"))

    if additions:
        result._embedded_floorplan_subpage_hints = (  # type: ignore[attr-defined]
            existing + additions
        )
