"""Phase 1 — Homepage Load + Full Network Capture.

Extracted from scripts/entrata.py scrape() function.
Navigates to the property homepage, captures API responses via NetworkCapture
(registered before goto), collects all internal links + anchor text links,
and extracts property metadata.
"""
from __future__ import annotations

import asyncio
import re

from extraction.engine.browser_session import _goto_robust
from extraction.engine.phase_context import PhaseContext

# Anchor text patterns that indicate an availability link.
_AVAILABILITY_ANCHOR_RE = re.compile(
    r"view\s+availab|see\s+availab|check\s+availab"
    r"|view\s+floor|see\s+floor|floor\s*plan"
    r"|view\s+pricing|see\s+pricing"
    r"|view\s+unit|see\s+unit|view\s+apartment"
    r"|availab\w*\s+unit|available\s+apartment"
    r"|view\s+all\s+unit|see\s+all\s+unit",
    re.IGNORECASE,
)

# URL errors that mean the page is completely unreachable.
_UNREACHABLE_SIGNATURES = (
    "err_ssl",
    "err_name_not_resolved",
    "err_connection_refused",
    "err_connection_timed_out",
    "err_too_many_redirects",
    "err_cert",
    "net::err_",
    "timeout",
)


def _norm_host(host: str) -> str:
    return host.lower().lstrip(".").removeprefix("www.")


def _normalise_url(base: str, href: str | None) -> str | None:
    import urllib.parse
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
        return None
    parsed = urllib.parse.urlparse(href)
    if parsed.scheme in ("http", "https"):
        base_host = _norm_host(urllib.parse.urlparse(base).netloc)
        link_host = _norm_host(parsed.netloc)
        if link_host != base_host and not link_host.endswith("." + base_host):
            return None
        return href
    if parsed.scheme:
        return None
    return urllib.parse.urljoin(base, href)


class Phase1HomepageLoad:
    """Navigate to the homepage, capture APIs, collect links, extract metadata."""

    async def run(self, ctx: PhaseContext) -> None:
        page = ctx.page
        if page is None:
            raise RuntimeError("Phase1HomepageLoad requires ctx.page to be set")

        # Navigate to homepage
        try:
            await _goto_robust(page, ctx.base_url, timeout_ms=60000)
        except Exception as e:
            msg = f"Homepage load error: {e}"
            ctx.errors.append(msg)
            err_str = str(e).lower()
            if any(sig in err_str for sig in _UNREACHABLE_SIGNATURES):
                ctx.page_unreachable = True
            return

        # Extract property metadata
        try:
            ctx.property_metadata = await self._extract_metadata(page)
            pname = (
                ctx.property_metadata.get("name")
                or ctx.property_metadata.get("title")
                or ""
            ).strip()
            if pname:
                ctx.property_name = pname
        except Exception:
            ctx.property_metadata = {}

        # Collect internal links + anchor text links
        try:
            all_links_with_text = await page.eval_on_selector_all(
                "a[href]",
                """els => els.map(e => ({
                    href: e.getAttribute('href'),
                    text: (e.innerText || e.textContent || '').trim().substring(0, 120)
                }))""",
            )
        except Exception:
            all_links_with_text = []

        for link_info in all_links_with_text:
            url = _normalise_url(ctx.base_url, link_info.get("href"))
            if url:
                ctx.internal_links.add(url)
                text = link_info.get("text", "")
                if text and _AVAILABILITY_ANCHOR_RE.search(text):
                    ctx.anchor_text_links.append(url)

        # Wait briefly for in-flight XHR/fetch responses (SPA sites fire APIs
        # after domcontentloaded — a 2s settle catches most of them).
        if not ctx.api_responses:
            await asyncio.sleep(2.0)

    async def _extract_metadata(self, page: object) -> dict:
        """Extract property name/address/geo from page metadata tags and JSON-LD."""
        try:
            return await page.evaluate("""() => {
                const meta = (name) => {
                    const el = document.querySelector(
                        `meta[property="${name}"], meta[name="${name}"]`
                    );
                    return el ? el.getAttribute('content') : null;
                };
                const result = {};
                result.name = meta('og:site_name') || meta('og:title')
                    || document.title || null;
                result.address = meta('og:street_address') || null;
                result.city = meta('og:locality') || null;
                result.state = meta('og:region') || null;
                result.zip = meta('og:postal-code') || null;
                result.lat = meta('og:latitude') || meta('geo.position')
                    ? (meta('og:latitude') || '').split(';')[0] : null;
                result.lng = meta('og:longitude') || null;
                result.phone = meta('og:phone_number') || null;
                return result;
            }""")
        except Exception:
            return {}
