"""Universal embed-recovery chain.

Wraps the individual recovery paths in a single priority-ordered call:

  1. ``recover_appfolio_embed``      — Wix/Squarespace shell → AppFolio
                                       listings iframe ({tenant}.appfolio.com/listings)
  2. ``recover_leaseleads_embed``    — Squarespace shell → embed.leaseleads.co
                                       iframe + public api.leaseleads.co JSON
  3. ``recover_doorloop_listings``   — published DoorLoop company listing →
                                       exact-address public MITS unit feed
  4. ``recover_swifty_floorplans``   — same-origin Swifty WordPress AJAX
                                       roster with native apartment rows
  5. ``recover_pms_portal``          — marketing-shell links to ResMan
                                       Implicity / RentCafe SecureCafe
  6. ``recover_managebuilding``      — authored tenant link → scoped rentals index
  7. ``recover_knock_dni_config``    — Knock config-variable embed → Doorway API
  8. ``recover_betternoi_units``     — BetterNOI plan UUIDs → public unit API
  9. ``recover_funnel_spaces``       — authored own-site Spaces unit roster
 10. ``recover_avail_table``         — unit roster embedded in own-site HTML
 11. ``recover_elise_applications``  — authored Elise application unit API
 12. ``recover_jonah_ssr``           — Jonah plan-detail SSR unit JSON
 13. ``recover_rentvision_crossroute`` — marker-gated RentVision detail roster
 14. ``recover_sightmap_subpage``    — SightMap embed one navigation hop deep
 15. ``recover_rently``              — Rently scattered-site roster API
 16. ``recover_g5``                  — G5 inventory API after detector misroute
 17. ``recover_generic_floorplans``  — repeated SSR plan-card containers at
                                       /floor[-]plans (plan-level catchall)

Used by:

  * ``squarespace_nopms`` and ``wix_nopms`` — these run the chain *before*
    declaring SYNDICATION_ONLY (the fast path; saves a downstream Tier-4
    LLM call).
  * ``GenericAdapter.extract()`` — as the *final* fallback after all
    internal tiers. Closes the **cross-vendor misroute** gap: if the
    detector picked the wrong PMS (e.g. "entrata" on a site that's
    actually an AppFolio embed), the primary adapter returns 0, scraper
    Step 8 falls back to generic, and *now* generic also tries these
    recoveries.

Idempotency: sets ``ctx._embed_recovery_attempted = True`` after the
chain runs (with the recovery name that produced units, if any). The
GenericAdapter caller checks this attr first and skips a re-run when
the syndication adapter already covered it for the same property.

Each recovery function is independently safe to call (returns ``[]`` on
non-applicable pages). The chain stops at the first result carrying a
canonical apartment identity; plan-only results remain eligible as fallback
until every unit-capable arm has run.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ma_poc.pms.adapters.base import AdapterContext

log = logging.getLogger(__name__)


_RECOVERY_FLAG_ATTR = "_embed_recovery_attempted"
_RECOVERY_BLOCKS_ATTR = "_embed_recovery_blocks"
_RECOVERY_NOTES_ATTR = "_embed_recovery_notes"

# HTTP status codes that indicate the response was intercepted by a bot-wall
# (DataDome, Akamai, Cloudflare, IIS bot-protection) rather than a genuine
# "no resource" / "no data" outcome. Recording these separately from "empty
# body" lets downstream telemetry distinguish a misroute (no embed anywhere)
# from a routing-correct-but-blocked recovery (residential proxy + Camoufox
# may flip the same probe to a HIT in production).
_BOT_BLOCK_STATUSES: frozenset[int] = frozenset({401, 403, 429, 503})
RecoveryResult = TypeVar("RecoveryResult")


def is_bot_block(status: int) -> bool:
    """True when *status* indicates a bot-wall intercept (not a genuine empty)."""
    return status in _BOT_BLOCK_STATUSES


def already_attempted(ctx: AdapterContext) -> bool:
    """True if a prior caller in this scrape already ran the recovery
    chain on the same context. Used by GenericAdapter to skip a double
    run for sites that came through a syndication adapter.
    """
    return bool(getattr(ctx, _RECOVERY_FLAG_ATTR, False))


def mark_attempted(ctx: AdapterContext, winning_recovery: str = "") -> None:
    """Record that the chain ran; optionally name the winner."""
    try:
        setattr(ctx, _RECOVERY_FLAG_ATTR, True)
        if winning_recovery:
            ctx._embed_recovery_winner = winning_recovery  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover — defensive
        pass


def mark_blocked(ctx: AdapterContext, recovery: str, url: str, status: int) -> None:
    """Record that a recovery sub-fetch hit a bot-wall (HTTP 401/403/429/503).
    The scraper reads ``ctx._embed_recovery_blocks`` after the chain runs and
    appends ``universal_recovery_blocked:<recovery>:<status>`` entries to
    ``fallback_chain`` so DLQ/triage can distinguish bot-walled-but-routing-
    correct cases (worth retrying with proxy/Camoufox) from genuine no-signal
    misses.
    """
    if not is_bot_block(status):
        return
    try:
        existing = getattr(ctx, _RECOVERY_BLOCKS_ATTR, None)
        if not isinstance(existing, list):
            existing = []
            setattr(ctx, _RECOVERY_BLOCKS_ATTR, existing)
        existing.append({"recovery": recovery, "url": url, "status": int(status)})
    except Exception:  # pragma: no cover — defensive
        pass


def note_recovery(ctx: AdapterContext, recovery: str, reason: str, detail: str = "") -> None:
    """Record WHY a recovery declined to emit the rows it had in hand.

    A recovery that finds a data surface and then refuses it (2026-07-28:
    AppFolio account rosters that could not be scoped to the property) must
    not look identical to one that found nothing — otherwise "we declined to
    guess" is indistinguishable from "there is nothing here", which is the
    failure mode that produced the contamination in the first place. The
    scraper appends these to ``fallback_chain`` so triage can see the
    property is unresolved on purpose.
    """
    if not recovery or not reason:
        return
    try:
        existing = getattr(ctx, _RECOVERY_NOTES_ATTR, None)
        if not isinstance(existing, list):
            existing = []
            setattr(ctx, _RECOVERY_NOTES_ATTR, existing)
        existing.append({"recovery": recovery, "reason": reason, "detail": detail})
        log.info(
            "recovery declined recovery=%s reason=%s detail=%s",
            recovery,
            reason,
            detail,
        )
    except Exception:  # pragma: no cover — defensive
        pass


def get_notes(ctx: AdapterContext) -> list[dict[str, object]]:
    """Return the decline notes recorded on *ctx* during the recovery chain."""
    notes = getattr(ctx, _RECOVERY_NOTES_ATTR, None)
    if not isinstance(notes, list):
        return []
    return list(notes)


def get_blocks(ctx: AdapterContext) -> list[dict[str, object]]:
    """Return the list of bot-block observations recorded on *ctx* during
    the recovery chain. Empty list when nothing was blocked.
    """
    blocks = getattr(ctx, _RECOVERY_BLOCKS_ATTR, None)
    if not isinstance(blocks, list):
        return []
    return list(blocks)


_PLAN_FALLBACK_FIELDS: tuple[str, ...] = (
    "floor_plan_name",
    "floorplan_name",
    "name",
    "beds",
    "bedrooms",
    "baths",
    "bathrooms",
    "sqft",
    "area",
    "rent_low",
    "rent_high",
    "asking_rent",
    "market_rent_low",
    "market_rent_high",
    "availability_date",
    "available_date",
)


def _has_real_unit(rows: list[dict[str, str]]) -> bool:
    """Return whether an arm produced a canonically identified apartment."""
    from ma_poc.core.identity import unit_has_real_anchor

    return any(isinstance(row, dict) and unit_has_real_anchor(row) for row in (rows or []))


def _result_tier(rows: list[dict[str, str]], default: str) -> str:
    """Prefer an arm's row-level tier while remaining safe on malformed rows."""
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        tier = str(row.get("extraction_tier") or "").strip()
        if tier:
            return tier
    return default


def _plan_fallback_score(rows: list[dict[str, str]]) -> tuple[int, int] | None:
    """Coverage-first score for a non-unit recovery result.

    Plan-only rows are useful output, but never proof that unit recovery is
    complete.  Keep the richest catalogue seen while later arms continue.
    """
    valid = [row for row in (rows or []) if isinstance(row, dict)]
    if not valid or _has_real_unit(valid):
        return None
    populated = sum(
        1 for row in valid for field in _PLAN_FALLBACK_FIELDS if row.get(field) not in (None, "", [], {})
    )
    return (len(valid), populated)


# Keep TypeVar syntax while the Cloud Run image is Python 3.11. The project
# lint target is 3.12, but PEP 695 syntax would make the deployed module fail
# at parse time before the canary starts.
async def _attempt_recovery(  # noqa: UP047
    recovery: str,
    operation: Awaitable[RecoveryResult],
    empty: RecoveryResult,
) -> RecoveryResult:
    """Run one arm without letting it make later recoveries unreachable."""
    try:
        return await operation
    except Exception as exc:  # pragma: no cover - defensive isolation
        log.debug("universal-recovery arm raised recovery=%s err=%s", recovery, exc)
        return empty


_JONAH_MAX_INDEX_FETCHES = 2
_JONAH_MAX_BODY_BYTES = 3_000_000
_JONAH_FETCH_CONCURRENCY = 4


def _ctx_body_html(ctx: AdapterContext) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    body = getattr(fetch_result, "body", None)
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return body if isinstance(body, str) else ""


def _ctx_source_url(ctx: AdapterContext) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    return str(getattr(fetch_result, "final_url", "") or getattr(ctx, "base_url", "") or "").strip()


def _normalized_host(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host.removeprefix("www.")


def _jonah_index_candidates(source_url: str) -> list[str]:
    """Return at most two same-site floorplan-index candidates."""
    from urllib.parse import urljoin, urlsplit, urlunsplit

    try:
        parts = urlsplit(source_url)
    except ValueError:
        return []
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return []

    clean = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))
    if parts.path.rstrip("/").endswith("/floorplans"):
        nested = clean.rstrip("/") + "/"
    else:
        nested = urljoin(clean.rstrip("/") + "/", "floorplans/")
    origin = urlunsplit((parts.scheme, parts.netloc, "/floorplans/", "", ""))
    return list(dict.fromkeys((nested, origin)))[:_JONAH_MAX_INDEX_FETCHES]


async def _fetch_jonah_html_pages(
    urls: list[str],
) -> list[tuple[str, int, str, str]]:
    """Fetch bounded public HTML with plain HTTP only.

    This lane deliberately does not use ``_probe``: no proxy, browser
    impersonation/fingerprint rotation, Web Unlocker, CAPTCHA solver, or
    FlareSolverr is reachable.  ``trust_env=False`` also prevents ambient
    proxy variables from silently changing that contract.
    """
    import asyncio

    import httpx

    bounded = list(dict.fromkeys(urls))
    if not bounded:
        return []
    semaphore = asyncio.Semaphore(_JONAH_FETCH_CONCURRENCY)

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(20.0),
        trust_env=False,
        headers={"Accept": "text/html,application/xhtml+xml"},
    ) as client:

        async def fetch_one(url: str) -> tuple[str, int, str, str]:
            async with semaphore:
                try:
                    response = await client.get(url)
                except (httpx.HTTPError, ValueError):
                    return url, 0, "", url
            status = int(response.status_code)
            final_url = str(response.url)
            if not 200 <= status < 300:
                return url, status, "", final_url
            content = response.content
            if len(content) > _JONAH_MAX_BODY_BYTES:
                return url, status, "", final_url
            return url, status, response.text, final_url

        return list(await asyncio.gather(*(fetch_one(url) for url in bounded)))


async def recover_jonah_ssr(ctx: AdapterContext) -> list[dict[str, Any]]:
    """Recover canonical units from Jonah plan-detail SSR JSON.

    Cross-label routing is safe because network drilling requires the exact
    Jonah CMS generator declaration, not a broad Elise/chat token.  Plan URLs
    and redirects are confined to the resolved index host, and the parser
    requires both canonical apartment identity and numeric fee-free base rent.
    """
    from ma_poc.pms.adapters._encoreskyline_units import (
        JONAH_MAX_PLAN_URLS,
        is_strong_jonah_generator_page,
        jonah_plan_urls_from_html,
        parse_jonah_ssr_units,
    )

    current_html = _ctx_body_html(ctx)
    source_url = _ctx_source_url(ctx)
    if not current_html or not source_url:
        return []

    # A resolver can hand us a plan-detail page directly. Exact unit-data is
    # sufficient on its own and avoids any secondary request.
    direct = parse_jonah_ssr_units(current_html, source_url)
    if direct:
        return direct

    if not is_strong_jonah_generator_page(current_html):
        return []

    plan_source_url = source_url
    plan_urls = jonah_plan_urls_from_html(current_html, source_url)
    if not plan_urls:
        for candidate in _jonah_index_candidates(source_url):
            fetched = await _fetch_jonah_html_pages([candidate])
            if not fetched:
                continue
            _, status, index_html, final_url = fetched[0]
            if not 200 <= status < 300 or not index_html:
                continue
            if not is_strong_jonah_generator_page(index_html):
                continue
            index_units = parse_jonah_ssr_units(index_html, final_url)
            if index_units:
                return index_units
            candidate_urls = jonah_plan_urls_from_html(index_html, final_url)
            if candidate_urls:
                plan_source_url = final_url
                plan_urls = candidate_urls
                break

    allowed_host = _normalized_host(plan_source_url)
    if not allowed_host:
        return []
    bounded_urls = [url for url in plan_urls if _normalized_host(url) == allowed_host][:JONAH_MAX_PLAN_URLS]
    if not bounded_urls:
        return []

    recovered: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for _, status, detail_html, final_url in await _fetch_jonah_html_pages(bounded_urls):
        if not 200 <= status < 300 or not detail_html or _normalized_host(final_url) != allowed_host:
            continue
        for row in parse_jonah_ssr_units(detail_html, final_url):
            key = (
                str(row.get("building") or "").strip().casefold(),
                str(row.get("unit_number") or "").strip().casefold(),
            )
            if not key[1] or key in seen:
                continue
            seen.add(key)
            recovered.append(row)
    return recovered


async def recover_universal_embed(
    page: Page | None,
    ctx: AdapterContext,
    *,
    body_only: bool = False,
) -> tuple[list[dict[str, str]], str, str]:
    """Run the recovery arms in priority order. Returns
    ``(rows, tier_used, recovery_name)``. ``recovery_name`` is the string
    identifier of the first arm that produced canonical apartments, or of the
    richest plan fallback when no arm did; it is ``""`` only when every arm
    returned empty.

    This function does NOT call ``post_process`` — the caller decides
    whether to admit.

    ``body_only=True`` is the cheap pre-retry pass.  It deliberately hides a
    live page from the arms so they may use only the already-fetched response
    body plus tightly marker-gated public APIs.  Navigation-dependent SightMap
    and generic-DOM recovery are skipped.  A miss does *not* mark the full
    chain attempted, leaving the ordinary late fallback unchanged; a genuine
    apartment win does mark it because no later arm can improve its identity.

    In the ordinary mode, marking ``ctx._embed_recovery_attempted`` remains
    handled here on every invocation, irrespective of outcome.
    """
    # Late imports to avoid a circular import at module load time
    # (these recovery modules each import from ``adapters._parsing``).
    from ma_poc.pms.adapters._appfolio_embed import recover_appfolio_embed
    from ma_poc.pms.adapters._g5_recovery import recover_g5
    from ma_poc.pms.adapters._generic_dom_floorplans import (
        recover_generic_floorplans,
    )
    from ma_poc.pms.adapters._knock_dni_recovery import recover_knock_dni_config
    from ma_poc.pms.adapters._leaseleads_embed import recover_leaseleads_embed
    from ma_poc.pms.adapters._pms_portal_hop import recover_pms_portal
    from ma_poc.pms.adapters._sightmap_subpage_recovery import (
        recover_sightmap_subpage,
    )
    from ma_poc.pms.adapters._swifty_floorplans import (
        SWIFTY_TIER,
        recover_swifty_floorplans,
    )
    from ma_poc.pms.adapters.funnel import recover_funnel_spaces
    from ma_poc.pms.adapters.rentvision import recover_rentvision_crossroute

    # The pre-retry pass must never spend browser-navigation budget.  Passing
    # ``None`` also makes portal discovery scan the exact fetched body instead
    # of probing the broad marketing-site subpath list.
    recovery_page = None if body_only else page

    # A non-empty response is not necessarily a unit recovery. LeaseLeads,
    # ResMan and generic DOM can all return one row per floor plan with no
    # apartment identity. Retain the richest such catalogue as fallback, but
    # let every later unit-capable arm run before returning it.
    best_plan: (
        tuple[
            tuple[int, int],
            list[dict[str, str]],
            str,
            str,
        ]
        | None
    ) = None

    def remember_plan(
        rows: list[dict[str, str]],
        tier: str,
        recovery: str,
    ) -> None:
        nonlocal best_plan
        score = _plan_fallback_score(rows)
        if score is not None and (best_plan is None or score > best_plan[0]):
            best_plan = (score, rows, tier, recovery)

    units = await _attempt_recovery(
        "appfolio_embed",
        recover_appfolio_embed(recovery_page, ctx),
        [],
    )
    if _has_real_unit(units):
        mark_attempted(ctx, "appfolio_embed")
        return units, "TIER_1_DOM_APPFOLIO_SSR", "appfolio_embed"
    remember_plan(units, _result_tier(units, "TIER_1_DOM_APPFOLIO_SSR"), "appfolio_embed")

    ll_units = await _attempt_recovery(
        "leaseleads_embed",
        recover_leaseleads_embed(recovery_page, ctx),
        [],
    )
    if _has_real_unit(ll_units):
        mark_attempted(ctx, "leaseleads_embed")
        return ll_units, "TIER_1_API_LEASELEADS", "leaseleads_embed"
    remember_plan(
        ll_units,
        _result_tier(ll_units, "TIER_1_API_LEASELEADS"),
        "leaseleads_embed",
    )

    # DoorLoop's exact company listing URL is authored by the property page;
    # its public MITS feed then exposes native listing/building ids and visible
    # apartment numbers. Run this marker-gated route before broad navigation
    # recoveries so a Wix shell does not spend a SightMap subpage probe first.
    from ma_poc.pms.adapters._doorloop_listings import (
        recover_doorloop_listings,
    )

    doorloop_units = await _attempt_recovery(
        "doorloop_listings",
        recover_doorloop_listings(ctx),
        [],
    )
    if _has_real_unit(doorloop_units):
        mark_attempted(ctx, "doorloop_listings")
        return (
            doorloop_units,
            "TIER_1_API_DOORLOOP_MITS",
            "doorloop_listings",
        )
    remember_plan(
        doorloop_units,
        _result_tier(doorloop_units, "TIER_1_API_DOORLOOP_MITS"),
        "doorloop_listings",
    )

    # A same-origin Swifty roster can coexist with an empty or plan-only linked
    # portal. Its helper is marker-gated and emits only native apartment rows.
    swifty_units = await _attempt_recovery(
        "swifty_floorplans",
        recover_swifty_floorplans(ctx),
        [],
    )
    if _has_real_unit(swifty_units):
        mark_attempted(ctx, "swifty_floorplans")
        return swifty_units, SWIFTY_TIER, "swifty_floorplans"
    remember_plan(
        swifty_units,
        _result_tier(swifty_units, SWIFTY_TIER),
        "swifty_floorplans",
    )

    portal_units = await _attempt_recovery(
        "pms_portal_hop",
        recover_pms_portal(recovery_page, ctx),
        [],
    )
    if _has_real_unit(portal_units):
        mark_attempted(ctx, "pms_portal_hop")
        # The portal-hop recovery sets ``extraction_tier`` on its
        # rows. Prefer that label when present (carries the specific
        # backend) over a generic ``TIER_1_PMS_PORTAL_HOP``.
        tier = _result_tier(portal_units, "TIER_1_PMS_PORTAL_HOP")
        return portal_units, tier, "pms_portal_hop"
    remember_plan(
        portal_units,
        _result_tier(portal_units, "TIER_1_PMS_PORTAL_HOP"),
        "pms_portal_hop",
    )

    # ManageBuilding route promotion (2026-08-01): only an exact tenant link
    # authored by the marketing page is eligible.  The recovery fetches one
    # public rentals index and scopes every card by either an authored listing
    # whitelist or an exact account/property label, plus CSV city/state/ZIP.
    # This is intentionally before broad cross-vendor arms so an account-wide
    # portfolio can never leak into a generic DOM result.
    from ma_poc.pms.adapters._managebuilding_recovery import (
        recover_managebuilding,
    )

    managebuilding_units = await _attempt_recovery(
        "managebuilding",
        recover_managebuilding(ctx),
        [],
    )
    if _has_real_unit(managebuilding_units):
        mark_attempted(ctx, "managebuilding")
        tier = _result_tier(
            managebuilding_units,
            "TIER_1_DOM_MANAGEBUILDING",
        )
        return managebuilding_units, tier, "managebuilding"
    remember_plan(
        managebuilding_units,
        _result_tier(managebuilding_units, "TIER_1_DOM_MANAGEBUILDING"),
        "managebuilding",
    )

    # Harbor Group's Knock template declares ``dniId`` in a config object and
    # calls ``knockDoorway.init`` with variables.  Detector routing treats
    # Knock as a marketing signal on these pages, so the primary Knock adapter
    # never sees it.  The narrow recovery uses the public Doorway API directly,
    # validates its community address against the CSV property, and emits only
    # explicit unit numbers with positive numeric rent.
    knock_units = await _attempt_recovery("knock_dni_config", recover_knock_dni_config(ctx), [])
    if _has_real_unit(knock_units):
        mark_attempted(ctx, "knock_dni_config")
        tier = _result_tier(knock_units, "TIER_1_API_KNOCK_DNI_CONFIG")
        return knock_units, tier, "knock_dni_config"
    remember_plan(
        knock_units,
        _result_tier(knock_units, "TIER_1_API_KNOCK_DNI_CONFIG"),
        "knock_dni_config",
    )

    # BetterNOI public roster recovery (2026-08-01): the site's own floor-plan
    # buttons pair a property client UUID with each floor-plan UUID, and its
    # JavaScript calls ares.betternoi.com's read-only availability endpoint.
    # The strict arm is body-only, direct HTTP, and requires request/response
    # UUID agreement plus city/state/ZIP corroboration before emitting units.
    from ma_poc.pms.adapters._betternoi_recovery import recover_betternoi_units

    betternoi_units = await _attempt_recovery(
        "betternoi",
        recover_betternoi_units(ctx),
        [],
    )
    if _has_real_unit(betternoi_units):
        mark_attempted(ctx, "betternoi")
        tier = _result_tier(betternoi_units, "TIER_1_API_BETTERNOI")
        return betternoi_units, tier, "betternoi"
    remember_plan(
        betternoi_units,
        _result_tier(betternoi_units, "TIER_1_API_BETTERNOI"),
        "betternoi",
    )

    # Funnel/Engrain Spaces cross-route recovery (2026-08-01): the exact
    # unit cards can live on one authored same-origin ``/apartments/`` or
    # ``/floorplans/`` route even when detector routing selected Encore or a
    # generic adapter. The arm is tightly plugin/template gated and uses one
    # direct HTTP request with no proxy, unlocker, browser or CAPTCHA path.
    spaces_units = await _attempt_recovery(
        "funnel_spaces",
        recover_funnel_spaces(ctx),
        [],
    )
    if _has_real_unit(spaces_units):
        mark_attempted(ctx, "funnel_spaces")
        tier = _result_tier(spaces_units, "TIER_1_DOM_FUNNEL_SPACES")
        return spaces_units, tier, "funnel_spaces"
    remember_plan(
        spaces_units,
        _result_tier(spaces_units, "TIER_1_DOM_FUNNEL_SPACES"),
        "funnel_spaces",
    )

    # Available-units roster embedded in the property's OWN page in a
    # shape the primary tiers miss (2026-07-31, #93). MITS-ILS
    # ``window.__FP_DATA__`` etc. — code-only, unit-level. Runs BEFORE the
    # generic-DOM catchall so a real unit roster wins over a plan summary.
    from ma_poc.pms.adapters._avail_table_recovery import recover_avail_table

    avail_units = await _attempt_recovery("avail_table", recover_avail_table(ctx), [])
    if _has_real_unit(avail_units):
        mark_attempted(ctx, "avail_table")
        tier = _result_tier(avail_units, "TIER_1_EMBEDDED_MITS_ILS")
        return avail_units, tier, "avail_table"
    remember_plan(
        avail_units,
        _result_tier(avail_units, "TIER_1_EMBEDDED_MITS_ILS"),
        "avail_table",
    )

    # Elise application recovery (2026-08-01): certain Jonah pages publish
    # plan cards locally but author an exact applications.eliseai.com building
    # link whose public API carries native apartment rows.  The arm requires
    # source-page name/address/ZIP plus configuration-name corroboration before
    # fetching the bounded unit list. It is direct HTTP only.
    from ma_poc.pms.adapters._elise_applications_recovery import (
        recover_elise_applications,
    )

    elise_units = await _attempt_recovery(
        "elise_applications",
        recover_elise_applications(ctx),
        [],
    )
    if _has_real_unit(elise_units):
        mark_attempted(ctx, "elise_applications")
        tier = _result_tier(elise_units, "TIER_1_API_ELISE_APPLICATIONS")
        return elise_units, tier, "elise_applications"
    remember_plan(
        elise_units,
        _result_tier(elise_units, "TIER_1_API_ELISE_APPLICATIONS"),
        "elise_applications",
    )

    # Jonah Systems SSR recovery (2026-07-31): the floorplan index is often
    # plan-only, while each public /floorplans/{slug}/ page contains strict
    # ``script[data-jd-fp-selector=unit-data]`` JSON.  This arm is cross-label
    # because the cohort was detected as Generic, Funnel, Knock, OneSite,
    # RentCafe, Onsite and Encore. It uses plain HTTP only and can never
    # replace a canonical result from an earlier arm.
    jonah_units = await _attempt_recovery("jonah_ssr", recover_jonah_ssr(ctx), [])
    if _has_real_unit(jonah_units):
        mark_attempted(ctx, "jonah_ssr")
        tier = _result_tier(jonah_units, "TIER_1_DOM_JONAH_SSR_UNITS")
        return jonah_units, tier, "jonah_ssr"
    remember_plan(
        jonah_units,
        _result_tier(jonah_units, "TIER_1_DOM_JONAH_SSR_UNITS"),
        "jonah_ssr",
    )

    # RentVision cross-route recovery (2026-07-31): detector conflicts can
    # route the marketing page through Generic/RentCafe even though its exact
    # CMS footer marker and public per-plan detail pages expose a strict unit
    # roster. Native and earlier universal recoveries have priority. This arm
    # uses bounded same-property plain HTTP and declines plan-only/no-rent rows.
    rv_units = await _attempt_recovery(
        "rentvision_crossroute",
        recover_rentvision_crossroute(ctx),
        [],
    )
    if _has_real_unit(rv_units):
        mark_attempted(ctx, "rentvision_crossroute")
        tier = _result_tier(rv_units, "TIER_3_DOM_RENTVISION_UNIT_LEVEL")
        return rv_units, tier, "rentvision_crossroute"
    remember_plan(
        rv_units,
        _result_tier(rv_units, "TIER_3_DOM_RENTVISION_UNIT_LEVEL"),
        "rentvision_crossroute",
    )

    # SightMap subpage recovery (2026-05-24): closes the
    # TIER_1_API_SIGHTMAP P1 cohort (131 props) where prod scored
    # SUCCESS via SightMap but canary's detector misrouted to
    # RentCafe/Funnel/etc. because the embed only lives at
    # /floorplans/ one nav-hop deep. Probes that family of
    # subpaths, splices a matching body into ctx, and lets
    # SightMapAdapter discover the embed code + canonical API URL.
    # Live-verified 8/10 in the cohort sample.
    if not body_only:
        sm_units = await _attempt_recovery(
            "sightmap_subpage",
            recover_sightmap_subpage(page, ctx),
            [],
        )
        if _has_real_unit(sm_units):
            mark_attempted(ctx, "sightmap_subpage")
            # The recovery stamps its own extraction_tier; prefer that
            # over a generic label to keep cohort reporting accurate.
            tier = _result_tier(
                sm_units,
                "TIER_1_API_SIGHTMAP_SUBPAGE_RECOVERY",
            )
            return sm_units, tier, "sightmap_subpage"
        remember_plan(
            sm_units,
            _result_tier(sm_units, "TIER_1_API_SIGHTMAP_SUBPAGE_RECOVERY"),
            "sightmap_subpage",
        )

    # Rently recovery (2026-07-30, #89): a property whose own site
    # redirects to ``u{ID}.rently.com`` (scattered single-family / BTR)
    # detects as generic/plan-text with no data. Extract the managerID
    # from the resolved host / body and fetch the searchQuery JSON
    # endpoint code-only. Address = scattered-site identity (#29).
    from ma_poc.pms.adapters.rently import recover_rently

    rently_units = await _attempt_recovery("rently", recover_rently(ctx), [])
    if _has_real_unit(rently_units):
        mark_attempted(ctx, "rently")
        return rently_units, "TIER_1_API_RENTLY", "rently"
    remember_plan(
        rently_units,
        _result_tier(rently_units, "TIER_1_API_RENTLY"),
        "rently",
    )

    # G5 recovery (2026-05-24): closes the TIER_1_API generic /
    # Knock-misroute sub-cohort where the property has a g5-cl-*
    # URN in its body but the detector picked a different PMS
    # adapter that returned 0 units. Pairs with the G5 adapter's
    # own curl_cffi + URN-candidate retry (commit 642c41b) — this
    # wrapper just makes G5 reachable from the misroute path.
    g5_units = await _attempt_recovery(
        "g5_recovery",
        recover_g5(recovery_page, ctx),
        [],
    )
    if _has_real_unit(g5_units):
        mark_attempted(ctx, "g5_recovery")
        tier = _result_tier(g5_units, "TIER_1_API_G5_RECOVERY")
        return g5_units, tier, "g5_recovery"
    remember_plan(
        g5_units,
        _result_tier(g5_units, "TIER_1_API_G5_RECOVERY"),
        "g5_recovery",
    )

    # Generic DOM is intentionally last. It commonly returns one row per
    # floor plan with an empty unit number; allowing those rows to win sooner
    # makes every later unit-capable recovery structurally unreachable.
    if not body_only:
        generic_result = await _attempt_recovery(
            "generic_dom",
            recover_generic_floorplans(page, ctx),
            ([], ""),
        )
        generic_units, _ = generic_result
        if _has_real_unit(generic_units):
            mark_attempted(ctx, "generic_dom")
            tier = _result_tier(generic_units, "TIER_3_DOM_GENERIC")
            return generic_units, tier, "generic_dom"
        remember_plan(
            generic_units,
            _result_tier(generic_units, "TIER_3_DOM_GENERIC"),
            "generic_dom",
        )

    if body_only:
        return [], "", ""
    if best_plan is not None:
        _, plan_rows, plan_tier, plan_recovery = best_plan
        mark_attempted(ctx, plan_recovery)
        return plan_rows, plan_tier, plan_recovery

    mark_attempted(ctx, "")
    return [], "", ""
