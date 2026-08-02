"""Property-scoped quarantine for confirmed contaminated warm routes.

These rules are intentionally narrow: a URL is blocked only for the property
whose live vendor metadata proved it belongs elsewhere.  Historical profile
objects remain in their existing stores; they are sanitised in memory on read
and on subsequent writes, while a clean versioned snapshot can be built
without deleting the old July evidence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuarantineRule:
    canonical_id: str
    fragment: str
    reason: str


RULES: tuple[QuarantineRule, ...] = (
    QuarantineRule(
        "264077",
        "yjp2415rvxl/sightmaps/104541",
        "live SightMap asset is NOVI Rise, not Novi Flats",
    ),
    QuarantineRule(
        "264077",
        "/sightmaps/104541",
        "live SightMap asset is NOVI Rise, not Novi Flats",
    ),
    QuarantineRule(
        "49364",
        "m9pzdr7mvk1/sightmaps/77845",
        "live SightMap asset is Kelson Row, not Brookside Commons",
    ),
    QuarantineRule(
        "49364",
        "/sightmaps/77845",
        "live SightMap asset is Kelson Row, not Brookside Commons",
    ),
    QuarantineRule(
        "222652",
        "/property/2016765/units",
        "live Knock metadata is The Onyx in Las Vegas, not Turtle Dove I",
    ),
    QuarantineRule(
        "222652",
        "318beef3-c0ee-4d07-a9c7-a9624bb13238",
        "Edifice UUID resolves to Turtle Dove 2, not Turtle Dove I",
    ),
    QuarantineRule(
        "22187",
        "/ann-arbor/glencoe-oaks/",
        "sibling-community recommendation link, not Golfside Lake Apartments",
    ),
)


def quarantine_reason(property_id: Any, url: Any) -> str | None:
    pid = str(property_id or "")
    candidate = str(url or "").casefold()
    if not pid or not candidate:
        return None
    for rule in RULES:
        if rule.canonical_id == pid and rule.fragment.casefold() in candidate:
            return rule.reason
    return None


def route_is_quarantined(property_id: Any, url: Any) -> bool:
    return quarantine_reason(property_id, url) is not None


def _url_of(item: Any, *fields: str) -> str:
    if isinstance(item, dict):
        for field in fields:
            value = item.get(field)
            if value:
                return str(value)
        return ""
    for field in fields:
        value = getattr(item, field, None)
        if value:
            return str(value)
    return ""


def _filter_items(
    items: Any,
    property_id: str,
    fields: tuple[str, ...],
    removed: list[dict[str, str]],
    surface: str,
) -> list[Any]:
    kept: list[Any] = []
    for item in list(items or []):
        url = _url_of(item, *fields) if fields else str(item or "")
        reason = quarantine_reason(property_id, url)
        if reason:
            removed.append({"surface": surface, "url": url, "reason": reason})
        else:
            kept.append(item)
    return kept


def sanitise_profile_routes(profile: Any, property_id: Any | None = None) -> tuple[Any, list[dict[str, str]]]:
    """Remove only property-scoped confirmed-bad routes from *profile*.

    The profile object is mutated and returned.  This works with both the
    Pydantic profile and light-weight test doubles.
    """

    if profile is None:
        return profile, []
    pid = str(property_id or getattr(profile, "canonical_id", "") or "")
    if not pid:
        return profile, []

    removed: list[dict[str, str]] = []
    nav = getattr(profile, "navigation", None)
    if nav is not None:
        winning = getattr(nav, "winning_page_url", None)
        reason = quarantine_reason(pid, winning)
        if reason:
            removed.append({"surface": "navigation.winning_page_url", "url": str(winning), "reason": reason})
            nav.winning_page_url = None
            nav.availability_page_path = None
        availability_path = getattr(nav, "availability_page_path", None)
        path_reason = quarantine_reason(pid, availability_path)
        if path_reason:
            removed.append(
                {
                    "surface": "navigation.availability_page_path",
                    "url": str(availability_path),
                    "reason": path_reason,
                }
            )
            nav.availability_page_path = None
        nav.availability_links = _filter_items(
            getattr(nav, "availability_links", []),
            pid,
            (),
            removed,
            "navigation.availability_links",
        )
        nav.last_navigation_hints = _filter_items(
            getattr(nav, "last_navigation_hints", []),
            pid,
            (),
            removed,
            "navigation.last_navigation_hints",
        )

    api = getattr(profile, "api_hints", None)
    if api is not None:
        api.known_endpoints = _filter_items(
            getattr(api, "known_endpoints", []),
            pid,
            ("url_pattern", "url"),
            removed,
            "api_hints.known_endpoints",
        )
        api.widget_endpoints = _filter_items(
            getattr(api, "widget_endpoints", []),
            pid,
            (),
            removed,
            "api_hints.widget_endpoints",
        )
        api.llm_field_mappings = _filter_items(
            getattr(api, "llm_field_mappings", []),
            pid,
            ("api_url_pattern", "url_pattern", "url"),
            removed,
            "api_hints.llm_field_mappings",
        )
        api.field_patches = _filter_items(
            getattr(api, "field_patches", []),
            pid,
            ("api_url_pattern", "url_pattern", "url"),
            removed,
            "api_hints.field_patches",
        )

    artifacts = getattr(profile, "llm_artifacts", None)
    verdicts = getattr(artifacts, "last_api_analysis_results", None) if artifacts else None
    if isinstance(verdicts, dict):
        for url in list(verdicts):
            reason = quarantine_reason(pid, url)
            if reason:
                removed.append(
                    {"surface": "llm_artifacts.last_api_analysis_results", "url": url, "reason": reason}
                )
                verdicts.pop(url, None)

    if nav is not None:
        explored = list(getattr(nav, "explored_links", []) or [])
        for item in removed:
            url = item.get("url", "")
            if url.startswith(("http://", "https://")) and url not in explored:
                explored.append(url)
        nav.explored_links = explored[-50:]

    if removed:
        confidence = getattr(profile, "confidence", None)
        if confidence is not None:
            maturity_type = type(getattr(confidence, "maturity", "COLD"))
            try:
                confidence.maturity = maturity_type.COLD
            except Exception:
                try:
                    confidence.maturity = maturity_type("COLD")
                except Exception:
                    confidence.maturity = "COLD"
            confidence.consecutive_failures = max(3, int(getattr(confidence, "consecutive_failures", 0) or 0))
        log.warning("quarantined %d confirmed bad profile routes for %s", len(removed), pid)
    return profile, removed
