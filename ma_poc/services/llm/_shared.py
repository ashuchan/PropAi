"""Shared utilities for the services.llm package.

Contains HTML trimming, API response ranking, unit normalization, and
the unit signal keys constant used across sub-modules.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Unit-like field names used to rank API responses by relevance
_UNIT_SIGNAL_KEYS = frozenset(
    {
        "rent",
        "price",
        "sqft",
        "bed",
        "bath",
        "available",
        "unit",
        "floor",
        "plan",
        "bedroom",
        "bathroom",
        "floorplan",
        "floorPlan",
        "unitNumber",
        "unit_number",
        "asking_rent",
        "market_rent",
        "availability",
        "availableDate",
        "available_date",
    }
)


def _trim_html(html: str) -> str:
    """Remove non-content tags from HTML. Keep JSON-LD scripts."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["style", "svg", "noscript", "nav", "footer"]):
        tag.decompose()
    # Remove scripts EXCEPT JSON-LD
    for tag in soup.find_all("script"):
        if tag.get("type") != "application/ld+json":
            tag.decompose()
    # Remove cookie/consent banners
    for tag in soup.find_all(attrs={"class": re.compile(r"cookie|consent|gdpr", re.I)}):
        tag.decompose()
    # Try to keep just <main> or largest content div
    main = soup.find("main")
    if main:
        return str(main)
    return soup.get_text("\n", strip=True)


def _rank_api_responses(api_responses: list[dict]) -> list[dict]:
    """Rank API responses by overlap with unit-like field names. Return top 3."""
    if not api_responses:
        return []

    def _score(resp: dict) -> int:
        body = resp.get("body")
        if not body:
            return 0
        text = json.dumps(body) if isinstance(body, (dict, list)) else str(body)
        return sum(1 for key in _UNIT_SIGNAL_KEYS if key in text.lower())

    scored = [(resp, _score(resp)) for resp in api_responses]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [resp for resp, s in scored[:3] if s > 0]


def _parse_llm_response(text: str) -> dict[str, Any]:
    """Parse LLM response, handling markdown fences and other formatting."""
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code fences
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding first { to last }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    return {"units": [], "profile_hints": {}}


def _normalize_units(raw_units: list[dict]) -> list[dict]:
    """Normalize LLM-extracted units through parse functions."""
    normalized: list[dict] = []
    for u in raw_units:
        unit: dict[str, Any] = {}

        # Unit ID
        unit["unit_id"] = u.get("unit_id") or u.get("unit_number")

        # Floor plan
        unit["floor_plan_name"] = u.get("floor_plan_name") or u.get("floor_plan_type")

        # Bedrooms/bathrooms
        beds = u.get("bedrooms")
        if beds is not None:
            try:
                unit["bedrooms"] = int(float(beds))
            except (ValueError, TypeError):
                unit["bedrooms"] = None
        else:
            unit["bedrooms"] = None

        baths = u.get("bathrooms")
        if baths is not None:
            try:
                unit["bathrooms"] = float(baths)
            except (ValueError, TypeError):
                unit["bathrooms"] = None
        else:
            unit["bathrooms"] = None

        # Sqft
        sqft = u.get("sqft")
        if sqft is not None:
            try:
                unit["sqft"] = int(float(sqft))
            except (ValueError, TypeError):
                unit["sqft"] = None
        else:
            unit["sqft"] = None

        # Rent
        rent_low = u.get("market_rent_low") or u.get("asking_rent")
        rent_high = u.get("market_rent_high") or rent_low
        if rent_low is not None:
            try:
                unit["market_rent_low"] = float(rent_low)
            except (ValueError, TypeError):
                unit["market_rent_low"] = None
        else:
            unit["market_rent_low"] = None

        if rent_high is not None:
            try:
                unit["market_rent_high"] = float(rent_high)
            except (ValueError, TypeError):
                unit["market_rent_high"] = None
        else:
            unit["market_rent_high"] = None

        # Rent sanity bounds ($200 - $50,000)
        for key in ("market_rent_low", "market_rent_high"):
            val = unit.get(key)
            if val is not None and (val < 200 or val > 50_000):
                unit[key] = None

        # Availability
        unit["available_date"] = u.get("available_date")
        status = u.get("availability_status", "UNKNOWN")
        if isinstance(status, str):
            status = status.upper()
            if status not in ("AVAILABLE", "UNAVAILABLE", "WAITLIST", "UNKNOWN"):
                status = "UNKNOWN"
        unit["availability_status"] = status

        # Confidence
        conf = u.get("confidence", 0.7)
        try:
            unit["confidence"] = max(0.0, min(1.0, float(conf)))
        except (ValueError, TypeError):
            unit["confidence"] = 0.5

        # Only include if we have some meaningful data
        if unit.get("unit_id") or unit.get("floor_plan_name") or unit.get("market_rent_low"):
            normalized.append(unit)

    return normalized
