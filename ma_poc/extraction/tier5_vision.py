"""
Tier 5 — Vision LLM (Role A: fallback when Tiers 1–4 fail or confidence < 0.6).

Acceptance criteria (CLAUDE.md PR-04):
- VisionProvider abstraction now lives in llm/ package
- Role A captures TARGETED section screenshots (not full-page) via page.locator
  and passes all sections in a single API call
- Bug-hunt #6: check base64 size before every call (handled by llm providers)
- Returns unit records with method=VISION_FALLBACK; target ≤5% of properties
"""
from __future__ import annotations

from extraction.confidence import composite, low_confidence_fields
from llm.factory import get_vision_provider
from llm.images import check_size
from models.extraction_result import ExtractionResult, ExtractionStatus, ExtractionTier
from scraper.browser import BrowserSession

# Re-export for backward compatibility (vision_banner, vision_sample import these)
ANTHROPIC_LIMIT_BYTES = 5 * 1024 * 1024
AZURE_LIMIT_BYTES = 20 * 1024 * 1024
_check_size = check_size

VISION_PROMPT = """You are a real estate vision extraction agent.
You see screenshots of an apartment property's pricing/availability page.
Return a JSON object: {"units": [{"unit_number": str, "floor_plan_type": str,
"asking_rent": number, "availability_status": "AVAILABLE"|"UNAVAILABLE"|"UNKNOWN",
"availability_date": str|null, "sqft": int|null}], "extraction_notes": str}
Return ONLY the JSON object."""


async def _capture_targeted_sections(session: BrowserSession) -> list[bytes]:
    """
    Capture targeted section screenshots (pricing panel, availability table,
    concession area) using page.locator — NOT full-page screenshots.
    Falls back to the existing full-page PNG if Playwright not available.
    """
    images: list[bytes] = []
    page = session.page
    if page is None:
        if session.screenshot_path and session.screenshot_path.exists():
            images.append(session.screenshot_path.read_bytes())
        return images

    section_selectors = (
        ".pricing", ".pricingWrapper", ".availability", "#availability",
        "#pricing", "table.units", ".unitContainer",
    )
    for sel in section_selectors:
        try:
            locator = page.locator(sel).first
            if await locator.count() > 0:
                images.append(await locator.screenshot(type="png"))
        except Exception:
            continue

    if not images and session.screenshot_path and session.screenshot_path.exists():
        images.append(session.screenshot_path.read_bytes())
    return images


async def maybe_run_vision_fallback(session: BrowserSession) -> ExtractionResult | None:
    """Run Tier 5 vision extraction. Returns None if provider unavailable."""
    try:
        provider = get_vision_provider()
    except Exception as exc:
        return ExtractionResult(
            property_id=session.property_id,
            tier=ExtractionTier.VISION_FALLBACK,
            status=ExtractionStatus.FAILED,
            confidence_score=0.0,
            error_message=f"vision_provider_unavailable: {exc}",
        )

    images = await _capture_targeted_sections(session)
    if not images:
        return ExtractionResult(
            property_id=session.property_id,
            tier=ExtractionTier.VISION_FALLBACK,
            status=ExtractionStatus.FAILED,
            confidence_score=0.0,
            error_message="no_screenshots_available",
        )

    try:
        payload = await provider.extract_from_images(images, VISION_PROMPT)
    except Exception as exc:
        return ExtractionResult(
            property_id=session.property_id,
            tier=ExtractionTier.VISION_FALLBACK,
            status=ExtractionStatus.FAILED,
            confidence_score=0.0,
            error_message=f"vision_call_failed: {exc}",
        )

    units = [u for u in payload.get("units", []) if isinstance(u, dict)]
    if not units:
        return ExtractionResult(
            property_id=session.property_id,
            tier=ExtractionTier.VISION_FALLBACK,
            status=ExtractionStatus.FAILED,
            confidence_score=0.0,
            error_message="no_units_extracted",
        )

    n = len(units)
    field_conf = {
        f: sum(1 for u in units if u.get(f) not in (None, "", "UNKNOWN")) / n
        for f in ("unit_number", "asking_rent", "availability_status", "sqft", "floor_plan_type")
    }
    avg = sum(composite(u) for u in units) / n
    low = sorted({fld for u in units for fld in low_confidence_fields(u)})
    return ExtractionResult(
        property_id=session.property_id,
        tier=ExtractionTier.VISION_FALLBACK,
        status=ExtractionStatus.SUCCESS if avg >= 0.7 else ExtractionStatus.FAILED,
        confidence_score=avg,
        raw_fields={"units": units, "method": "VISION_FALLBACK"},
        field_confidences=field_conf,
        low_confidence_fields=low,
    )
