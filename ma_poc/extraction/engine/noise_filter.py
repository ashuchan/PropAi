"""Network-response noise filter for the extraction engine.

Extracted from scripts/entrata.py — pure functions with no Playwright
dependency, so they can be unit-tested without a browser.
"""
from __future__ import annotations

import urllib.parse
from typing import Any

# Hosts whose API responses are never apartment data.
_FALSE_POSITIVE_HOSTS: frozenset[str] = frozenset({
    "googleapis.com",
    "maps.googleapis.com",
    "go-mpulse.net",
    "c.go-mpulse.net",
    "visitor-analytics.io",
    "visits.visitor-analytics.io",
    "google-analytics.com",
    "www.google-analytics.com",
    "googletagmanager.com",
    "www.googletagmanager.com",
    "doubleclick.net",
    "facebook.com",
    "connect.facebook.net",
    "hotjar.com",
    "sentry.io",
    "meetelise.com",
    "app.meetelise.com",
    "sierra.chat",
    "theconversioncloud.com",
    "api.theconversioncloud.com",
    "nestiolistings.com",
    "rentgrata.com",
    "api.rentgrata.com",
    "g5marketingcloud.com",
    "client-leads.g5marketingcloud.com",
    "g5-api-proxy.g5marketingcloud.com",
    "userway.org",
    "api.userway.org",
    "omni.cafe",
    "webchat.omni.cafe",
    "comms.entrata.com",
})

# Subdomains of blocked hosts that actually serve unit/floor-plan data.
_ALLOWLIST_OVERRIDES: frozenset[str] = frozenset({
    "api.ws.realpage.com",
    "onlineleasing.realpage.com",
})

# URL path fragments that are never unit/availability data.
_FALSE_POSITIVE_PATH_FRAGMENTS: frozenset[str] = frozenset({
    "/tag-manager/",
    "/mapsjs/",
    "/gen_204",
    "/analytics/",
    "/gtag/",
    "/pixel",
    "/beacon",
    "/widget/inbox_members",
    "/widget/contact",
    "/widget/messages",
    "/widget/conversations",
    "/widget/campaigns",
    "/tour/availabilities",
    "/html_forms/",
    "/yext_reviews/",
    "/blurb/v1/",
})

# Entrata widget types that contain real floor plan / availability data.
_ENTRATA_PROPERTY_WIDGET_TYPES: frozenset[str] = frozenset({"floor_plans", "availability"})

# Entrata widget types that are known to NOT contain unit data.
_ENTRATA_NOISE_WIDGET_TYPES: frozenset[str] = frozenset({
    "custom",
    "directions",
    "events",
    "specials",
    "resident_login",
    "gallery",
    "contact",
    "reviews",
    "social",
    "blog",
    "amenities",
})

# Patterns that indicate a URL likely points to availability/unit data.
_AVAILABILITY_API_PATTERNS = [
    "/api/",
    "/availabilities",
    "/floor-plans",
    "floorplan",
    "availability",
    "/pricing",
    "/units",
    "/apartments",
    "getFloorPlans",
    "getAvailabilities",
    "propertyInfo",
]


def looks_like_availability_api(url: str) -> bool:
    """Return True if the URL looks like a property availability/units API call.

    Rejects known false-positive hosts (analytics, chatbots, CDNs) and path
    fragments that never contain apartment data.
    """
    url_lower = url.lower()

    try:
        host = urllib.parse.urlparse(url_lower).netloc
        if host not in _ALLOWLIST_OVERRIDES:
            for fp_host in _FALSE_POSITIVE_HOSTS:
                if host == fp_host or host.endswith("." + fp_host):
                    return False
    except Exception:
        pass

    for frag in _FALSE_POSITIVE_PATH_FRAGMENTS:
        if frag in url_lower:
            return False

    return any(p.lower() in url_lower for p in _AVAILABILITY_API_PATTERNS)


def _filter_entrata_widget_response(body: dict) -> dict | None:
    """Return body if it contains floor plan/availability data, else None."""
    widget_name = body.get("widget_name", "")
    if widget_name in _ENTRATA_NOISE_WIDGET_TYPES:
        return None
    widget_data = body.get("widget_data", {})
    content = widget_data.get("content", {})
    if isinstance(content, dict):
        fp_section = content.get("floor_plans", {})
        if isinstance(fp_section, dict):
            fp_list = fp_section.get("floor_plans", [])
            if isinstance(fp_list, list) and fp_list:
                return body
        avail_section = content.get("availability", {})
        if isinstance(avail_section, dict):
            avail_units = avail_section.get("units", [])
            if isinstance(avail_units, list) and avail_units:
                return body
    if widget_name not in _ENTRATA_PROPERTY_WIDGET_TYPES:
        return None
    return body


def filter_network_noise(
    api_responses: list[dict],
    profile: Any | None = None,
) -> list[dict]:
    """Filter captured API responses through the profile-specific blocklist.

    Global host/path filtering is already applied at capture time via
    looks_like_availability_api(). This layer adds per-property learned blocks.
    """
    if profile is None:
        return api_responses

    blocked_patterns: set[str] = set()
    api_hints = getattr(profile, "api_hints", None)
    if api_hints:
        for ep in getattr(api_hints, "blocked_endpoints", []):
            blocked_patterns.add(ep.url_pattern)

    if not blocked_patterns:
        return api_responses

    filtered = []
    for resp in api_responses:
        url = resp.get("url", "")
        if url in blocked_patterns:
            continue
        blocked = any(pattern in url for pattern in blocked_patterns)
        if not blocked:
            filtered.append(resp)

    return filtered
