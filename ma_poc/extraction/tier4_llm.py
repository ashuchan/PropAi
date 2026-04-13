"""
Tier 4 — LLM text extraction. Provider selected by LLM_PROVIDER env var.

Acceptance criteria (CLAUDE.md PR-03 / Tier 4):
- Use the EXACT system prompt below
- Strip <script>/<style> first; if remaining > 60K tokens extract pricing/unit
  section only and log truncation in failure_reason (bug-hunt #5)
- Wrap every json.loads in try/except JSONDecodeError; retry once with a
  fix-the-JSON instruction
- Backoff with random.uniform(0, cap) jitter for 429 (bug-hunt #13); 5 retries max
- Token count logged per call
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from bs4 import BeautifulSoup

from extraction.confidence import composite, low_confidence_fields
from models.extraction_result import ExtractionResult, ExtractionStatus, ExtractionTier
from scraper.browser import BrowserSession


SYSTEM_PROMPT = """You are a real estate data extraction agent.
Extract all apartment unit listings from the provided HTML.
Return a JSON object with this exact structure:
{
  "units": [
    {
      "unit_number": string,
      "floor_plan_type": string,
      "asking_rent": number,
      "availability_status": "AVAILABLE" | "UNAVAILABLE" | "UNKNOWN",
      "availability_date": string | null,
      "sqft": number | null,
      "_confidence": {
        "unit_number": number,
        "asking_rent": number,
        "availability_status": number
      }
    }
  ],
  "property_name": string | null,
  "extraction_notes": string
}
Return ONLY the JSON object. No markdown, no explanation."""

# Approximate ~4 chars/token. 60K tokens ≈ 240K chars.
MAX_HTML_CHARS = 60_000 * 4


def _strip_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def _focus_pricing_section(html: str) -> str:
    """If full HTML still too long, attempt to keep just pricing/unit blocks."""
    soup = BeautifulSoup(html, "lxml")
    keep: list[str] = []
    for sel in (
        ".pricing", ".pricingWrapper", ".unitContainer", ".availability",
        ".units", "#availability", "#pricing", "table.units",
    ):
        for el in soup.select(sel):
            keep.append(str(el))
    if keep:
        return "\n".join(keep)
    return html


def prepare_html(html: str) -> tuple[str, bool]:
    """Returns (text, truncated). Strips scripts/styles, optionally narrows."""
    if not html:
        return "", False
    text = _strip_html(html)
    if len(text) <= MAX_HTML_CHARS:
        return text, False
    focused = _strip_html(_focus_pricing_section(html))
    if len(focused) <= MAX_HTML_CHARS:
        return focused, True
    return focused[:MAX_HTML_CHARS], True


def _parse_units(content: str) -> tuple[list[dict[str, Any]], Optional[str]]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        return [], f"json_decode: {exc}"
    units = payload.get("units") or []
    if not isinstance(units, list):
        return [], "units_not_list"
    return [u for u in units if isinstance(u, dict)], None


def _is_enabled() -> bool:
    """Check ENABLE_TIER4_LLM env var. Defaults to enabled (true)."""
    return os.getenv("ENABLE_TIER4_LLM", "true").strip().lower() in ("true", "1", "yes")


def _get_provider() -> Any:
    """Return a text LLM provider or None if unconfigured."""
    try:
        from llm.factory import get_text_provider
        return get_text_provider()
    except Exception:
        return None


async def extract(session: BrowserSession) -> ExtractionResult:
    if not _is_enabled():
        return ExtractionResult(
            property_id=session.property_id,
            tier=ExtractionTier.LLM_GPT4O_MINI,
            status=ExtractionStatus.FAILED,
            confidence_score=0.0,
            error_message="tier4_disabled_by_feature_flag",
        )

    if not session.html:
        return ExtractionResult(
            property_id=session.property_id,
            tier=ExtractionTier.LLM_GPT4O_MINI,
            status=ExtractionStatus.FAILED,
            confidence_score=0.0,
            error_message="no html",
        )

    provider = _get_provider()
    if provider is None:
        return ExtractionResult(
            property_id=session.property_id,
            tier=ExtractionTier.LLM_GPT4O_MINI,
            status=ExtractionStatus.FAILED,
            confidence_score=0.0,
            error_message="llm_provider_unconfigured",
        )

    prompt_text, truncated = prepare_html(session.html)
    truncation_note: Optional[str] = "html_truncated" if truncated else None

    try:
        content = await provider.complete(SYSTEM_PROMPT, prompt_text)
    except Exception as exc:
        return ExtractionResult(
            property_id=session.property_id,
            tier=ExtractionTier.LLM_GPT4O_MINI,
            status=ExtractionStatus.FAILED,
            confidence_score=0.0,
            error_message=f"llm_call_failed: {exc}",
        )

    units, parse_err = _parse_units(content)
    if parse_err:
        # Retry once with explicit fix instruction
        try:
            fixup_content = await provider.complete(
                "You returned invalid JSON. Re-emit ONLY the JSON object — no commentary.",
                content,
            )
            units, parse_err = _parse_units(fixup_content)
        except Exception as exc:
            parse_err = f"fixup_failed: {exc}"

    if parse_err and not units:
        return ExtractionResult(
            property_id=session.property_id,
            tier=ExtractionTier.LLM_GPT4O_MINI,
            status=ExtractionStatus.FAILED,
            confidence_score=0.0,
            error_message=parse_err,
        )

    n = len(units)
    field_conf = {
        f: sum(1 for u in units if u.get(f) not in (None, "", "UNKNOWN")) / n
        for f in ("unit_number", "asking_rent", "availability_status", "sqft", "floor_plan_type")
    }
    avg = sum(composite(u) for u in units) / n if units else 0.0
    low = sorted({fld for u in units for fld in low_confidence_fields(u)})

    return ExtractionResult(
        property_id=session.property_id,
        tier=ExtractionTier.LLM_GPT4O_MINI,
        status=ExtractionStatus.SUCCESS if avg >= 0.7 else ExtractionStatus.FAILED,
        confidence_score=avg,
        raw_fields={"units": units, "tokens_in": len(prompt_text)},
        field_confidences=field_conf,
        low_confidence_fields=low,
        error_message=truncation_note,
    )


# Public re-exports for tests
__all__ = ["SYSTEM_PROMPT", "prepare_html", "extract", "_parse_units"]
