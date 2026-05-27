"""Wix HtmlComponent iframe walker — surfaces AppFolio embed tenants.

Many Wix-built property sites (millenniumnw, liveallureva, etc.) embed
their AppFolio listings widget inside a Wix HtmlComponent iframe whose
``src`` points at a CDN URL like:

    https://www-millenniumnw-com.filesusr.com/html/<32-hex>.html

That HTML is a tiny stub that document.writes the AppFolio listing.js
loader and calls ``Appfolio.Listing({hostUrl: 'TENANT.appfolio.com'})``.
The vanity-domain HTML alone never references appfolio.com — only the
filesusr.com iframe content does.

This module:
  1. ``detect_wix_html_iframes(html)`` — returns the list of
     filesusr.com/html/*.html iframe src URLs found in ``html``.
  2. ``extract_appfolio_tenant(iframe_body)`` — pulls the
     ``hostUrl: '...'`` value out of a fetched iframe HTML, returns the
     ``{tenant}.appfolio.com`` host or ``None``.
  3. ``build_appfolio_listings_url(tenant_host)`` — convenience that
     produces the canonical ``https://{tenant_host}/listings`` URL the
     existing _appfolio_embed adapter knows how to parse.

The caller pattern (in generic.py): when the page has a Wix marker AND
no units yet, walk the iframes, fetch each filesusr.com iframe body,
extract any AppFolio tenant, surface as portal hint. The orchestrator's
link-hop then fetches the listings page and the existing AppFolio SSR
parser handles the rest.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# ── Detection of Wix HtmlComponent iframes ──────────────────────────────────

# Matches <iframe ... src="https://*.filesusr.com/html/<hex>.html ...">.
# Captures the URL portion only.
_WIX_IFRAME_RE = re.compile(
    r"""<iframe[^>]+src\s*=\s*["']"""
    r"""(https?://[a-z0-9-]+\.filesusr\.com/html/[a-z0-9_-]+\.html"""
    r"""[^"']*)["']""",
    re.IGNORECASE,
)


def detect_wix_html_iframes(html: str) -> list[str]:
    """Return all Wix HtmlComponent iframe URLs in ``html``.

    Returns an empty list when:
      • input is empty
      • no ``filesusr.com/html/`` iframes match

    Duplicates are removed while preserving source order — Wix pages
    sometimes render the same widget at desktop + mobile breakpoints.
    """
    if not html:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _WIX_IFRAME_RE.finditer(html):
        url = m.group(1)
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


# ── AppFolio tenant extraction from iframe body ─────────────────────────────

# Matches the Appfolio.Listing JS init: hostUrl: '<tenant>.appfolio.com'
# Tolerant of single OR double quotes and arbitrary whitespace.
_APPFOLIO_HOST_RE = re.compile(
    r"""hostUrl\s*:\s*["']"""
    r"""([a-z0-9-]+\.appfolio\.com)"""
    r"""["']""",
    re.IGNORECASE,
)

# Fallback: bare appfolio.com host inside a string literal OR a URL.
# Used when the operator's bundle minifies the hostUrl assignment so
# the structured regex doesn't match. The host must be preceded by a
# delimiter (``"``, ``'``, ``/``, ``=``, or whitespace) so we don't
# false-match on partial-domain substrings.
_APPFOLIO_BARE_HOST_RE = re.compile(
    r"""(?:["'/=\s])([a-z0-9-]+\.appfolio\.com)(?=["'/\s?#])""",
    re.IGNORECASE,
)


def extract_appfolio_tenant(iframe_body: str) -> str | None:
    """Pull the ``{tenant}.appfolio.com`` host from an iframe body.

    Returns ``None`` when:
      • body is empty
      • neither the structured ``hostUrl:`` form nor a bare
        ``{tenant}.appfolio.com`` string literal is present

    The two-regex fallback chain catches both unminified and minified
    Wix HtmlComponent embeds.
    """
    if not iframe_body:
        return None
    m = _APPFOLIO_HOST_RE.search(iframe_body)
    if m:
        return m.group(1).lower()
    m = _APPFOLIO_BARE_HOST_RE.search(iframe_body)
    if m:
        host = m.group(1).lower()
        # Defensive: reject the bare ``appfolio.com`` (no subdomain) —
        # that's never a tenant host.
        if host == "appfolio.com":
            return None
        return host
    return None


# ── URL helpers ─────────────────────────────────────────────────────────────


def build_appfolio_listings_url(tenant_host: str) -> str | None:
    """Build the canonical ``https://{tenant_host}/listings`` URL for an
    extracted AppFolio tenant. Returns ``None`` for empty / invalid input.
    """
    if not tenant_host:
        return None
    # Defensive parse — reject anything that doesn't look like a host.
    try:
        # Prepend a scheme to parse the netloc cleanly.
        parsed = urlparse(f"https://{tenant_host}")
    except Exception:
        return None
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return f"https://{parsed.netloc.lower()}/listings"
