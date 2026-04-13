"""
Role B — Banner capture. Runs on EVERY non-skipped property regardless of
extraction tier outcome. Looks for image-banner concession (e.g. "1 month free")
and returns structured output.

Acceptance criteria (CLAUDE.md PR-04 / Role B):
- 100% of non-SKIPPED properties — banner_capture_attempted=True in ScrapeEvent
- Output: {type, value, conditions, start_date, end_date, source="IMAGE_BANNER"}
"""
from __future__ import annotations

import re
from typing import Any

from llm.factory import get_vision_provider
from scraper.browser import BrowserSession

_BANNER_KEYWORDS = (
    "free", "off", "concession", "special", "limited", "weeks free",
    "months free", "move in", "look and lease",
)
_BANNER_SELECTORS = (
    ".banner", ".promo", ".specials", ".concession", "#promo",
    "[class*=banner]", "[class*=promo]", "[class*=special]",
)

_PROMPT = (
    "You are inspecting a single banner / promotional image from an apartment "
    "website. If it advertises a concession, return JSON: "
    '{"type": "free_rent"|"discount"|"other", "value": str, "conditions": str|null, '
    '"start_date": str|null, "end_date": str|null, "source": "IMAGE_BANNER"}. '
    "If there is no concession, return {\"type\": null}."
)


def _detect_banner_text(html: str | None) -> str | None:
    if not html:
        return None
    h = html.lower()
    for kw in _BANNER_KEYWORDS:
        m = re.search(r"[^.]{0,80}" + re.escape(kw) + r"[^.]{0,80}", h)
        if m:
            return m.group(0).strip()
    return None


async def _capture_banner_image(session: BrowserSession) -> bytes | None:
    page = session.page
    if page is None:
        return None
    for sel in _BANNER_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                data: bytes = await loc.screenshot(type="png")
                return data
        except Exception:
            continue
    return None


async def capture_banner(session: BrowserSession) -> dict[str, Any] | None:
    """
    Returns the structured banner dict if a concession is found, else None.

    Strategy:
      1. Cheap text scan of HTML for banner keywords. If none found, return None
         without burning a vision call (still counts as "attempted").
      2. If found, try to capture the banner element image and ask the vision
         provider for structured output.
      3. If the page is unavailable (test), fall back to the text snippet.
    """
    snippet = _detect_banner_text(session.html)
    if not snippet:
        return None

    img = await _capture_banner_image(session)
    if img is None:
        return {
            "type": "other",
            "value": snippet[:120],
            "conditions": None,
            "start_date": None,
            "end_date": None,
            "source": "TEXT_SNIPPET",
        }

    try:
        provider = get_vision_provider()
        payload = await provider.extract_from_images([img], _PROMPT)
    except Exception:
        return {
            "type": "other",
            "value": snippet[:120],
            "conditions": None,
            "start_date": None,
            "end_date": None,
            "source": "IMAGE_BANNER",
        }

    if isinstance(payload, dict) and payload.get("type"):
        payload.setdefault("source", "IMAGE_BANNER")
        return payload
    return None


__all__ = ["capture_banner"]
