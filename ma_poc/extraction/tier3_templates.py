"""
Tier 3 — Playwright template dispatcher.

Acceptance criteria (CLAUDE.md PR-03 / Tier 3):
- Dispatch to RentCafe / Entrata / AppFolio template based on session.pms_platform
  with HTML signature fallback if pms_platform is unset
- Each template returns [] (not raise) on selector failure
- Failure rate must stay <10% per platform (tracked by validate_outputs)
- Click expander buttons to reveal hidden unit data before extraction
- Scroll to bottom for lazy-loaded content (Entrata)
- Re-capture HTML after interaction so templates parse the full DOM
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from extraction.confidence import composite, low_confidence_fields
from models.extraction_result import ExtractionResult, ExtractionStatus, ExtractionTier
from models.unit_record import UnitRecord
from scraper.browser import BrowserSession
from templates import appfolio, entrata, rentcafe

# Button text patterns that reveal hidden unit rows / floor plan details.
# Ported from the production scraper (scripts/entrata.py).
_EXPAND_BUTTON_PATTERNS = [
    re.compile(r"available\s+unit", re.IGNORECASE),
    re.compile(r"view\s+unit", re.IGNORECASE),
    re.compile(r"see\s+unit", re.IGNORECASE),
    re.compile(r"check\s+avail", re.IGNORECASE),
    re.compile(r"floor\s+plan", re.IGNORECASE),
    re.compile(r"show\s+more", re.IGNORECASE),
    re.compile(r"view\s+all", re.IGNORECASE),
    re.compile(r"see\s+all", re.IGNORECASE),
    re.compile(r"view\s+pricing", re.IGNORECASE),
    re.compile(r"see\s+pricing", re.IGNORECASE),
]


def _detect_platform(html: str) -> str:
    if not html:
        return "unknown"
    h = html.lower()
    if "rentcafe" in h or "rentcafeapi" in h:
        return "rentcafe"
    if "entrata" in h:
        return "entrata"
    if "appfolio" in h:
        return "appfolio"
    return "unknown"


def _records_to_dicts(records: list[UnitRecord]) -> list[dict[str, Any]]:
    return [r.model_dump(mode="json") for r in records]


async def _click_expanders(page: Any) -> bool:
    """
    Click buttons that reveal hidden unit data ('View Available Units',
    'Show More', etc.). Returns True if any button was clicked.

    Only targets actually-clickable elements (button, a, [role=button]).
    """
    clicked_any = False
    try:
        clickable = page.locator(
            "button, a, [role='button'], input[type='button'], input[type='submit']"
        )
        for pattern in _EXPAND_BUTTON_PATTERNS:
            try:
                btns = await clickable.filter(has_text=pattern).all()
            except Exception:
                continue
            for btn in btns[:5]:
                try:
                    if not await btn.is_visible():
                        continue
                    await btn.click(timeout=3000, no_wait_after=True)
                    clicked_any = True
                    await asyncio.sleep(1.0)
                except Exception:
                    continue
    except Exception:
        pass
    return clicked_any


async def _scroll_to_bottom(page: Any) -> None:
    """
    Scroll the page to the bottom to trigger lazy-loaded content.
    Required for Entrata sites that load unit rows on scroll.
    """
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.5)
        # Second pass in case more content loaded
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.0)
    except Exception:
        pass


async def _interact_and_refresh_html(session: BrowserSession) -> None:
    """
    If a live Playwright page is available, click expanders + scroll to bottom
    to reveal hidden/lazy-loaded content, then re-capture the HTML.
    """
    page = session.page
    if page is None:
        return

    try:
        # Scroll to bottom for lazy-loaded content
        await _scroll_to_bottom(page)

        # Click expander buttons
        clicked = await _click_expanders(page)

        if clicked:
            # Wait for any dynamically loaded content to settle
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            await asyncio.sleep(1.0)

        # Re-capture HTML with the expanded content
        try:
            session.html = await page.content()
        except Exception:
            pass  # Keep whatever HTML we already had
    except Exception:
        pass  # Best-effort interaction; continue with existing HTML


async def extract(session: BrowserSession) -> ExtractionResult:
    if not session.html:
        return ExtractionResult(
            property_id=session.property_id,
            tier=ExtractionTier.PLAYWRIGHT_TPL,
            status=ExtractionStatus.FAILED,
            confidence_score=0.0,
            error_message="no html",
        )

    platform = (session.pms_platform or "").lower() or _detect_platform(session.html)

    # Interact with the page to reveal hidden content before parsing HTML
    await _interact_and_refresh_html(session)

    if platform == "rentcafe":
        records = rentcafe.extract(session.html, session.property_id)
    elif platform == "entrata":
        records = entrata.extract(session.html, session.property_id)
    elif platform == "appfolio":
        records = appfolio.extract(session.html, session.property_id)
    else:
        # Try each in turn — first non-empty wins.
        for fn in (rentcafe.extract, entrata.extract, appfolio.extract):
            records = fn(session.html, session.property_id)
            if records:
                break
        else:
            records = []

    if not records:
        return ExtractionResult(
            property_id=session.property_id,
            tier=ExtractionTier.PLAYWRIGHT_TPL,
            status=ExtractionStatus.FAILED,
            confidence_score=0.0,
            error_message=f"template_failed:{platform}",
        )

    units = _records_to_dicts(records)
    n = len(units)
    field_conf = {
        f: sum(1 for u in units if u.get(f) not in (None, "", "UNKNOWN")) / n
        for f in ("unit_number", "asking_rent", "availability_status", "sqft", "floor_plan_type")
    }
    avg = sum(composite(u) for u in units) / n
    low = sorted({fld for u in units for fld in low_confidence_fields(u)})

    return ExtractionResult(
        property_id=session.property_id,
        tier=ExtractionTier.PLAYWRIGHT_TPL,
        status=ExtractionStatus.SUCCESS if avg >= 0.7 else ExtractionStatus.FAILED,
        confidence_score=avg,
        raw_fields={"units": units, "platform": platform},
        field_confidences=field_conf,
        low_confidence_fields=low,
    )
