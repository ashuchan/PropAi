"""SightMap subpage recovery (2026-05-24).

The dominant pattern in the prod-vs-canary P1 cohort (131 properties,
TIER_1_API_SIGHTMAP):

  * Prod scored SUCCESS via the SightMap API (often 20-60 units)
  * Canary's detector saw RentCafe / Funnel / nothing on the homepage
    and routed there — these primary adapters returned 0 units
  * The SightMap embed was never reached because its iframe lives on
    a ``/floorplans/`` deep path, NOT on the homepage

The existing ``Step 7b`` Entrata→SightMap secondary in scraper.py
covers the case where the *primary* adapter was Entrata. This module
generalises the recovery: when ANY primary adapter returned 0 units,
probe the homepage for a ``/floorplans/`` family link, fetch that page,
and let ``SightMapAdapter._try_direct_sightmap_api_probe`` discover
the embed code + canonical API URL.

Wired into ``_universal_recovery.recover_universal_embed`` as a 5th
recovery path (after appfolio_embed / leaseleads / portal_hop /
generic_floorplans). All four prior recoveries get first dibs; this
one only fires when none of them found units AND the homepage hints
at a SightMap-fronted floorplans page.

Live-verified 2026-05-24 on the P1 cohort:
  * 8/10 probed properties expose ``sightmap.com/embed/{TOKEN}`` at
    the ``/floorplans/`` path
  * Prod scored 21-52 units on each (the same data SightMap would
    return now)
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from ma_poc.pms.adapters._probe import probe_get

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ma_poc.pms.adapters.base import AdapterContext

log = logging.getLogger(__name__)


# Same families used by SightMapAdapter's own deep-path fallback.
# Listed in priority order — homepage links typically point at one of
# these via a "View Floor Plans" / "Availability" CTA.
_SIGHTMAP_SUBPATH_CANDIDATES: tuple[str, ...] = (
    "/floorplans/",
    "/floor-plans/",
    "/floorplans",
    "/availability/",
    "/availability",
    "/apartments/",
)

# Match the same SightMap markers SightMapAdapter looks for. We only
# need confirmation that the subpage HAS an embed before splicing it
# in — actual unit extraction happens via SightMapAdapter.extract().
_SIGHTMAP_MARKER_RE = re.compile(
    r"sightmap\.com(?:\\/|/)(?:embed|app(?:\\/|/)api)",
    re.IGNORECASE,
)

# Pull href= candidates that look like a floorplans/availability link
# off the homepage. We prefer same-origin links — third-party
# /floorplans/ pointers are usually CTAs to RentCafe or a portal we
# already tried via the primary adapter.
_FP_LINK_RE = re.compile(
    r'href=["\']'
    r'([^"\']*?'
    r'(?:floor[-_]?plans?|availability|apartments)'
    r'[^"\']*?)["\']',
    re.IGNORECASE,
)


def _decode_body(raw: Any) -> str:
    """Coerce a FetchResult body (bytes|str|None) to a string. Returns
    ``""`` for unsupported types so the caller can no-op cleanly."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8", "replace")
        except Exception:
            return ""
    if isinstance(raw, str):
        return raw
    return ""


def _homepage_html(ctx: "AdapterContext") -> str:
    fr = getattr(ctx, "fetch_result", None)
    if fr is None:
        return ""
    return _decode_body(getattr(fr, "body", None))


def _origin(ctx: "AdapterContext") -> tuple[str, str]:
    """Return ``(scheme://netloc, full_url)`` from the fetched final URL,
    falling back to ``ctx.base_url``. Returns ``("", "")`` when unknown."""
    fr = getattr(ctx, "fetch_result", None)
    final = ""
    if fr is not None:
        final = str(getattr(fr, "final_url", "") or "")
    final = final or str(getattr(ctx, "base_url", "") or "")
    if not final:
        return "", ""
    try:
        p = urlparse(final)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}", final
    except Exception:
        pass
    return "", final


def _candidate_subpages(
    body: str,
    base: str,
) -> list[str]:
    """Build the list of subpages to probe for a SightMap embed.

    Priority:
      1. Same-origin floorplans/availability links discovered in the
         homepage body (operators sometimes namespace the slug, e.g.
         ``/communities/{name}/floorplans/`` — preserve that).
      2. Vanilla ``/floorplans/`` etc. directly off the origin.

    Returns a deduplicated list ordered by priority. Cap at 6 entries
    so we don't burn cost on a property whose embed truly isn't there.
    """
    out: list[str] = []
    seen: set[str] = set()

    base_host = urlparse(base).netloc.lower() if base else ""

    # 1. Body-derived same-origin links
    if body:
        for m in _FP_LINK_RE.finditer(body):
            link = m.group(1).strip()
            if not link or link.startswith("#"):
                continue
            # Skip mailto/tel/javascript: and similar
            if ":" in link[:10] and not link.startswith(("http://", "https://", "/")):
                continue
            absolute = urljoin(base + "/", link) if base else link
            if base_host and urlparse(absolute).netloc.lower() != base_host:
                # Different host — likely a CTA to a 3rd-party portal
                # we've already tried. Skip.
                continue
            absolute = absolute.split("#")[0].split("?")[0]
            if absolute not in seen:
                seen.add(absolute)
                out.append(absolute)
                if len(out) >= 4:
                    break

    # 2. Vanilla candidate paths off the origin (in case the homepage
    # link text didn't match our regex — some sites use icon-only CTAs).
    if base:
        for path in _SIGHTMAP_SUBPATH_CANDIDATES:
            candidate = (base + path).split("#")[0]
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)

    return out[:6]


async def recover_sightmap_subpage(
    page: "Page | None",
    ctx: "AdapterContext",
) -> list[dict[str, str]]:
    """Fire when no primary adapter (and no other universal recovery)
    found units, on the assumption the property hides a SightMap iframe
    one nav-hop deep.

    Strategy:
      1. Build candidate floorplans/availability URLs from the homepage.
      2. Probe each via ``probe_get`` (curl_cffi). Take the first one
         whose body has a ``sightmap.com/(embed|app/api)`` marker.
      3. Splice that body into ``ctx.fetch_result`` (via
         ``dataclasses.replace`` since FetchResult is frozen).
      4. Invoke ``SightMapAdapter().extract(page, ctx)`` — it already
         knows how to walk embed → /sightmaps/{ID} → JSON.

    Returns the SightMap adapter's units (already post-processed) or
    ``[]`` when nothing was found. Never raises — caller treats an
    empty return as a graceful no-op.
    """
    body = _homepage_html(ctx)
    if not body:
        return []

    # Cheap early exit: if the homepage already has a SightMap marker,
    # the primary adapter routing (or Step 7b) would have caught it.
    # Don't double-fire.
    if _SIGHTMAP_MARKER_RE.search(body):
        return []

    base, _final = _origin(ctx)
    if not base:
        return []

    candidates = _candidate_subpages(body, base)
    if not candidates:
        return []

    # Find a subpage with a SightMap marker. First match wins.
    matched_url: str | None = None
    matched_body: str | None = None
    for sub_url in candidates:
        try:
            r = probe_get(sub_url, timeout=15)
        except Exception as exc:
            log.debug(
                "sightmap-subpage-probe error url=%s err=%s",
                sub_url, exc,
            )
            continue
        if getattr(r, "status_code", 0) != 200:
            continue
        sub_body = getattr(r, "text", None) or ""
        if not sub_body:
            continue
        if _SIGHTMAP_MARKER_RE.search(sub_body):
            matched_url = sub_url
            matched_body = sub_body
            log.info(
                "sightmap-subpage-recovery: SightMap marker found "
                "url=%s (body=%d bytes)",
                sub_url, len(sub_body),
            )
            break

    if matched_url is None or matched_body is None:
        return []

    # Splice the matched subpage body into ctx so SightMapAdapter's
    # own embed-discovery logic picks it up.
    fr = getattr(ctx, "fetch_result", None)
    if fr is None:
        return []
    try:
        new_body = matched_body.encode("utf-8", "replace")
        ctx.fetch_result = dataclasses.replace(  # type: ignore[attr-defined]
            fr, body=new_body
        )
    except Exception as exc:
        log.warning(
            "sightmap-subpage-recovery: splice failed err=%s", exc
        )
        return []

    # Now run SightMapAdapter with the spliced body. It will find the
    # embed code, fetch sightmap.com/embed/{code}, learn the api token
    # + sightmap_id, and fetch the canonical /app/api/v1/.../sightmaps/{id}.
    try:
        from ma_poc.pms.adapters.sightmap import SightMapAdapter

        sm = SightMapAdapter()
        sm_result = await sm.extract(page, ctx)  # type: ignore[arg-type]
    except Exception as exc:
        log.warning(
            "sightmap-subpage-recovery: SightMapAdapter raised err=%s",
            exc,
        )
        return []

    units = list(sm_result.units or [])
    if not units:
        return []

    # Stamp the recovery tier so reporting can distinguish this path
    # from direct SightMap detection.
    for u in units:
        if isinstance(u, dict) and not u.get("extraction_tier"):
            u["extraction_tier"] = "TIER_1_API_SIGHTMAP_SUBPAGE_RECOVERY"

    return units
