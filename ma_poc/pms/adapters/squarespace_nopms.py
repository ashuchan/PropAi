"""
Squarespace (no PMS) adapter.

Research log
------------
Web sources consulted:
  - https://www.squarespace.com/ — Squarespace website builder (accessed 2026-04-17)
  - Squarespace does not provide apartment management features
Real payloads inspected (from data/runs/*/raw_api/):
  - No Squarespace-specific API payloads with unit data found in captures
  - Squarespace sites in the dataset are marketing-only (no PMS backend)
Key findings:
  - Squarespace is a website builder, not a PMS. Properties using
    Squarespace typically have no structured unit data accessible via API.
  - A sizable minority *do* embed a real PMS one nav-hop deep:
    AppFolio listings iframe / LeaseLeads iframe / ResMan-RentCafe portal
    link / generic SSR plan grid. ``recover_universal_embed`` tries all
    four in priority order before we declare ``SYNDICATION_ONLY_*``.
  - When all recoveries miss, return empty so downstream knows no PMS
    backend is reachable.
"""

from __future__ import annotations

import asyncio
import dataclasses
import html as html_lib
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


_MAX_AUTHORED_INVENTORY_ROUTES = 2
_MAX_AUTHORED_PAGE_BYTES = 3_000_000
_AUTHORED_AVAILABILITY_RE = re.compile(
    r"\b(?:check\s+)?availability\b|\bavailable\s+apartments?\b",
    re.IGNORECASE,
)
_AUTHORED_PRICING_RE = re.compile(r"\bpricing\b", re.IGNORECASE)
_AUTHORED_FLOORPLANS_RE = re.compile(r"\bfloor\s*[- ]?\s*plans?\b", re.IGNORECASE)
_SIGHTMAP_MARKER_RE = re.compile(
    r"sightmap\.com(?:\\/|/)(?:embed|app(?:\\/|/)api)",
    re.IGNORECASE,
)


def _host_without_www(url: str) -> tuple[str, int | None]:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if host.startswith("www."):
            host = host[4:]
        return host, parsed.port
    except (ValueError, TypeError):
        return "", None


def _clean_http_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return ""
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def discover_squarespace_inventory_routes(body: str, base_url: str) -> list[str]:
    """Return at most two exact, same-site, operator-authored inventory URLs.

    The href and its visible label must both come from the captured Squarespace
    document.  Guessed paths, cross-host links, fragment-only CTAs and broad
    labels such as merely ``Apartments`` do not qualify.  This keeps a linked
    property roster reachable without turning a management-company navigation
    menu into a portfolio crawler.
    """
    if not body or not base_url:
        return []
    base = _clean_http_url(base_url)
    base_host = _host_without_www(base)
    if not base or not base_host[0]:
        return []
    try:
        soup = BeautifulSoup(body, "lxml")
    except Exception:
        return []

    ranked: list[tuple[int, int, str]] = []
    for index, anchor in enumerate(soup.select("a[href]")):
        label = " ".join(anchor.get_text(" ", strip=True).split())
        if _AUTHORED_AVAILABILITY_RE.search(label):
            rank = 0
        elif _AUTHORED_PRICING_RE.search(label):
            rank = 1
        elif _AUTHORED_FLOORPLANS_RE.search(label):
            rank = 2
        else:
            continue

        raw_href = html_lib.unescape(str(anchor.get("href") or "").strip())
        if not raw_href or raw_href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = _clean_http_url(urljoin(base, raw_href))
        if not absolute or _host_without_www(absolute) != base_host:
            continue
        if absolute == base:
            continue
        ranked.append((rank, index, absolute))

    ordered: list[str] = []
    seen: set[tuple[tuple[str, int | None], str, str]] = set()
    for _rank, _index, url in sorted(ranked):
        parsed = urlsplit(url)
        route_key = (_host_without_www(url), parsed.path.rstrip("/") or "/", parsed.query)
        if route_key in seen:
            continue
        seen.add(route_key)
        ordered.append(url)
        if len(ordered) >= _MAX_AUTHORED_INVENTORY_ROUTES:
            break
    return ordered


@dataclass(frozen=True)
class SquarespaceAuthoredPage:
    url: str
    body: str
    status: int = 200


@dataclass
class _AuthoredRecovery:
    units: list[dict[str, Any]] = field(default_factory=list)
    tier: str = ""
    winner: str = ""
    winning_url: str = ""
    errors: list[str] = field(default_factory=list)
    unit_source_provenance: list[dict[str, Any]] = field(default_factory=list)


def _ctx_body_and_url(ctx: AdapterContext) -> tuple[str, str]:
    fetch_result = getattr(ctx, "fetch_result", None)
    raw = getattr(fetch_result, "body", None)
    if isinstance(raw, bytes):
        body = raw.decode("utf-8", errors="replace")
    elif isinstance(raw, str):
        body = raw
    else:
        body = ""
    url = str(
        getattr(fetch_result, "final_url", "")
        or getattr(ctx, "base_url", "")
        or ""
    ).strip()
    return body, url


async def fetch_squarespace_authored_page(
    url: str,
    *,
    entry_url: str,
) -> SquarespaceAuthoredPage | None:
    """Fetch one exact authored route with direct HTTP and fail closed.

    No proxy, Web Unlocker, CAPTCHA solver, browser fingerprint rotation or
    guessed fallback is reachable from this helper.  Redirects must remain on
    the entry property's normalized host and response bodies are hard-capped.
    """
    from ma_poc.pms.adapters._probe import probe_get

    expected_host = _host_without_www(entry_url)
    if not expected_host[0] or _host_without_www(url) != expected_host:
        return None
    try:
        response = await asyncio.to_thread(
            probe_get,
            url,
            unlocker=False,
            retries=1,
            timeout=20,
        )
    except Exception:
        return None
    try:
        status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    final_url = _clean_http_url(str(getattr(response, "url", "") or url))
    raw_body = getattr(response, "text", "")
    body = raw_body if isinstance(raw_body, str) else ""
    if (
        status != 200
        or not body
        or not final_url
        or _host_without_www(final_url) != expected_host
        or len(body.encode("utf-8", errors="replace")) > _MAX_AUTHORED_PAGE_BYTES
    ):
        return None
    return SquarespaceAuthoredPage(url=final_url, body=body, status=status)


def _context_for_authored_page(
    ctx: AdapterContext,
    page: SquarespaceAuthoredPage,
) -> AdapterContext:
    current = getattr(ctx, "fetch_result", None)
    if current is not None and dataclasses.is_dataclass(current):
        try:
            fetch_result = dataclasses.replace(
                current,
                url=page.url,
                status=page.status,
                body=page.body.encode("utf-8", errors="replace"),
                final_url=page.url,
                network_log=[],
            )
        except (TypeError, ValueError):
            fetch_result = SimpleNamespace(
                url=page.url,
                status=page.status,
                body=page.body.encode("utf-8", errors="replace"),
                final_url=page.url,
                network_log=[],
            )
    else:
        fetch_result = SimpleNamespace(
            url=page.url,
            status=page.status,
            body=page.body.encode("utf-8", errors="replace"),
            final_url=page.url,
            network_log=[],
        )
    return dataclasses.replace(
        ctx,
        base_url=page.url,
        fetch_result=fetch_result,
        budget=dict(getattr(ctx, "budget", {}) or {}),
        inventory_paths_attempted=list(getattr(ctx, "inventory_paths_attempted", []) or []),
        inventory_pages_reachable=list(getattr(ctx, "inventory_pages_reachable", []) or []),
    )


def _own_page_provenance(
    *,
    source_url: str,
    body: str,
    unit_count: int,
) -> list[dict[str, Any]]:
    from ma_poc.pms.source_provenance import build_unit_source_provenance

    return [
        build_unit_source_provenance(
            provider="squarespace",
            source_url=source_url,
            body=body,
            unit_count=unit_count,
            response_kind="marketing_html_unit_roster",
        )
    ]


def _first_unit_source_url(units: list[dict[str, Any]], fallback: str) -> str:
    for unit in units:
        source = str(unit.get("source_api_url") or "").strip()
        if source:
            return source
    return fallback


def _copy_recovery_telemetry(child: AdapterContext, parent: AdapterContext) -> None:
    from ma_poc.pms.adapters._universal_recovery import (
        get_blocks,
        get_notes,
        mark_blocked,
        note_recovery,
    )

    for block in get_blocks(child):
        try:
            mark_blocked(
                parent,
                str(block.get("recovery") or ""),
                str(block.get("url") or ""),
                int(block.get("status") or 0),
            )
        except (TypeError, ValueError):
            continue
    for note in get_notes(child):
        note_recovery(
            parent,
            str(note.get("recovery") or ""),
            str(note.get("reason") or ""),
            str(note.get("detail") or ""),
        )


async def recover_squarespace_authored_inventory(
    ctx: AdapterContext,
) -> _AuthoredRecovery:
    """Recover physical inventory from at most two operator-authored pages."""
    from ma_poc.core.identity import unit_has_real_anchor
    from ma_poc.pms.adapters._universal_recovery import (
        mark_attempted,
        recover_universal_embed,
    )

    entry_body, entry_url = _ctx_body_and_url(ctx)
    routes = discover_squarespace_inventory_routes(entry_body, entry_url)
    for route in routes:
        if route not in ctx.inventory_paths_attempted:
            ctx.inventory_paths_attempted.append(route)
        authored = await fetch_squarespace_authored_page(route, entry_url=entry_url)
        if authored is None:
            continue
        if authored.url not in ctx.inventory_pages_reachable:
            ctx.inventory_pages_reachable.append(authored.url)
        child = _context_for_authored_page(ctx, authored)

        units, tier, winner = await recover_universal_embed(
            None,
            child,
            body_only=True,
        )
        _copy_recovery_telemetry(child, ctx)
        if any(unit_has_real_anchor(unit) for unit in units if isinstance(unit, dict)):
            mark_attempted(ctx, f"squarespace_authored:{winner}")
            provenance = (
                _own_page_provenance(
                    source_url=authored.url,
                    body=authored.body,
                    unit_count=len(units),
                )
                if winner == "avail_table"
                else []
            )
            return _AuthoredRecovery(
                units=units,
                tier=tier,
                winner=f"squarespace_authored:{winner}",
                winning_url=_first_unit_source_url(units, authored.url),
                unit_source_provenance=provenance,
            )

        # ``body_only`` deliberately skips navigation-dependent SightMap.  An
        # exact authored page carrying the embed is already the bounded route,
        # so invoke the dedicated adapter directly; its asset metadata gate
        # remains mandatory before any row is admitted.
        if _SIGHTMAP_MARKER_RE.search(authored.body):
            from ma_poc.pms.adapters.sightmap import SightMapAdapter

            sightmap_result = await SightMapAdapter().extract(None, child)  # type: ignore[arg-type]
            if any(
                unit_has_real_anchor(unit)
                for unit in sightmap_result.units
                if isinstance(unit, dict)
            ):
                mark_attempted(ctx, "squarespace_authored:sightmap")
                return _AuthoredRecovery(
                    units=list(sightmap_result.units),
                    tier=sightmap_result.tier_used,
                    winner="squarespace_authored:sightmap",
                    winning_url=str(sightmap_result.winning_url or authored.url),
                    errors=list(sightmap_result.errors),
                    unit_source_provenance=list(sightmap_result.unit_source_provenance),
                )
    return _AuthoredRecovery()


class SquarespaceNoPmsAdapter:
    """Squarespace (no PMS) adapter — runs universal embed-recovery first."""

    pms_name: str = "squarespace_nopms"
    _fingerprints: list[str] = ["squarespace.com", "static1.squarespace.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Try bounded authored routes, then the universal recovery chain.

        Physical inventory from an exact, same-site ``Availability`` /
        ``Pricing`` / ``Floor Plans`` link must outrank a generic price-only
        interpretation of the shell.  Every provider reached from the authored
        page still runs its ordinary provider/property identity gate.
        """
        from ma_poc.extraction.post_process import post_process
        from ma_poc.pms.adapters._universal_recovery import (
            recover_universal_embed,
        )

        authored = await recover_squarespace_authored_inventory(ctx)
        if authored.units:
            pp = post_process(
                authored.units,
                property_id=getattr(ctx, "property_id", None),
            )
            if pp.n_unit_level > 0:
                return AdapterResult(
                    units=pp.admitted,
                    plan_summaries=pp.plan_summaries,
                    tier_used=authored.tier,
                    winning_url=authored.winning_url or None,
                    errors=authored.errors,
                    confidence=min(0.95, 0.7 + 0.04 * pp.n_unit_level),
                    unit_source_provenance=authored.unit_source_provenance,
                )

        units, tier, _winner = await recover_universal_embed(page, ctx)
        if units:
            pp = post_process(units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                body, source_url = _ctx_body_and_url(ctx)
                provenance = (
                    _own_page_provenance(
                        source_url=source_url,
                        body=body,
                        unit_count=pp.n_unit_level,
                    )
                    if _winner == "avail_table" and pp.n_unit_level > 0
                    else []
                )
                return AdapterResult(
                    units=pp.admitted,
                    plan_summaries=pp.plan_summaries,
                    tier_used=tier,
                    winning_url=_first_unit_source_url(pp.admitted, source_url) or None,
                    confidence=min(0.95, 0.7 + 0.04 * pp.n_admitted),
                    unit_source_provenance=provenance,
                )

        return AdapterResult(
            tier_used="SYNDICATION_ONLY_SQUARESPACE",
            confidence=0.0,
            errors=["Squarespace site detected — no PMS backend, syndication_only strategy"],
        )

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
