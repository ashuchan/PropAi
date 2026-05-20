"""Vision-LLM banner-concession capture (lazy, env-gated fallback).

Fires only when:

1. The text-based capture in :func:`ma_poc.pms.scraper._capture_concession_from_html`
   returned ``None`` AND the /specials URL probe also returned nothing.
2. A Playwright ``page`` object is available so we can screenshot the
   pricing / hero region without spinning up a second browser.
3. A vision provider is configured via env vars
   (``ANTHROPIC_API_KEY`` for Claude vision, ``AZURE_OPENAI_API_KEY``
   + ``AZURE_OPENAI_DEPLOYMENT_GPT4O_VISION`` for GPT-4o vision).
   When neither is configured this module is a no-op — vision capture
   is opt-in, never required for the pipeline to function.

Output contract — *raw is the source of truth, structured is a hint*:

    :func:`capture_banner` returns either ``None`` (no banner found,
    provider missing, vision call failed) or a dict shaped like
    :func:`ma_poc.core.concession_normalize.normalize_concession`'s
    output PLUS a ``"text"`` field carrying the verbatim banner copy
    the vision model read. The caller folds this into the scraper
    result so downstream emitters (schema_v2, jugnu.py output) treat
    the vision capture identically to a text-scrape capture.

Cost guard:

    Vision calls are bounded to **one screenshot per property** and
    the image is cropped to the top-third of the viewport (where
    banners typically sit) and downsampled to keep the base64 payload
    under 1 MB. There is no retry on failure — the structured-fallback
    chain ends here. If the vision call fails for any reason, the
    function returns ``None`` and the raw concession field stays
    ``None`` (matching the no-text-no-vision path).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

# Banner-position keywords used to confirm we're looking at promotional
# copy rather than navigation / footer junk. The vision-extracted text
# must contain at least one of these tokens to be accepted; this is the
# same safety belt the text-based capture uses.
_BANNER_KEYWORDS: tuple[str, ...] = (
    "free", "off", "concession", "special", "limited",
    "weeks free", "months free", "move in", "move-in",
    "look and lease", "save", "bonus", "credit",
    "waived", "discount", "promotion", "deal",
)

_PROMPT = (
    "You are inspecting a screenshot of an apartment-marketing website "
    "for a promotional banner offering a rent concession, deposit waiver, "
    "free-rent period, move-in bonus, or similar incentive.\n\n"
    "If the banner advertises a concession, return ONLY a JSON object:\n"
    "{\n"
    "  \"text\": <verbatim banner copy, max 300 chars>,\n"
    "  \"type\": \"free_rent\" | \"discount\" | \"percent_off\" | "
    "\"waived_fee\" | \"reduced_deposit\" | \"look_and_lease\" | \"other\",\n"
    "  \"value\": <human-readable value e.g. \"2 months\" or \"$500\">,\n"
    "  \"deadline\": <move-in / lease-by date string or null>,\n"
    "  \"conditions\": <restrictions copy or null>\n"
    "}\n\n"
    "If there is no concession banner visible, return ONLY: {\"type\": null}\n"
    "No markdown, no explanation, no code fences."
)


async def capture_banner(
    page: Any,
    *,
    property_id: str | None = None,
) -> dict[str, Any] | None:
    """Capture a property-level banner concession via vision LLM.

    Returns a structured dict (see module docstring) on success,
    ``None`` on any failure path. Never raises — concession capture
    is best-effort, never blocks the primary scrape.
    """
    provider = _select_provider()
    if provider is None:
        return None
    if page is None or not hasattr(page, "screenshot"):
        return None

    try:
        image_bytes = await asyncio.wait_for(
            page.screenshot(
                full_page=False,
                clip={"x": 0, "y": 0, "width": 1280, "height": 600},
                type="jpeg",
                quality=70,
            ),
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("vision_banner screenshot failed for %s: %s", property_id, exc)
        return None

    if not image_bytes or len(image_bytes) > 1_500_000:
        # Don't ship oversize payloads to the API.
        return None

    try:
        raw_response = await provider(image_bytes, _PROMPT)
    except Exception as exc:  # noqa: BLE001
        log.debug("vision_banner call failed for %s: %s", property_id, exc)
        return None

    parsed = _parse_vision_response(raw_response)
    if parsed is None:
        return None

    text = parsed.get("text")
    if not isinstance(text, str) or not _looks_like_banner(text):
        return None

    return {
        "text": text[:300].strip(),
        "type": parsed.get("type"),
        "value": parsed.get("value"),
        "deadline": parsed.get("deadline"),
        "conditions": parsed.get("conditions"),
        "source": "IMAGE_BANNER",
    }


# ─────────────────────────────────────────────────────────────────────
# Provider selection — opt-in via env vars; no provider = no-op
# ─────────────────────────────────────────────────────────────────────


def _select_provider() -> Any | None:
    """Return an async callable ``(image_bytes, prompt) -> str`` or None.

    Anthropic is preferred when both providers are configured because
    Claude 3.5 Sonnet has been more reliable at terse-JSON output in
    our tests. Env-var changes take effect on next call (no module-
    level caching).
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _anthropic_call
    if (
        os.environ.get("AZURE_OPENAI_API_KEY")
        and os.environ.get("AZURE_OPENAI_DEPLOYMENT_GPT4O_VISION")
        and os.environ.get("AZURE_OPENAI_ENDPOINT")
    ):
        return _azure_openai_call
    return None


async def _anthropic_call(image_bytes: bytes, prompt: str) -> str:
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return ""
    client = AsyncAnthropic()
    b64 = base64.b64encode(image_bytes).decode("ascii")
    response = await client.messages.create(
        model=os.environ.get("ANTHROPIC_VISION_MODEL", "claude-3-5-sonnet-20241022"),
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )
    if response.content and getattr(response.content[0], "text", None):
        return str(response.content[0].text)
    return ""


async def _azure_openai_call(image_bytes: bytes, prompt: str) -> str:
    try:
        from openai import AsyncAzureOpenAI
    except ImportError:
        return ""
    client = AsyncAzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-02-15-preview"),
    )
    b64 = base64.b64encode(image_bytes).decode("ascii")
    response = await client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_DEPLOYMENT_GPT4O_VISION"],
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }
        ],
    )
    choice = response.choices[0] if response.choices else None
    if choice and choice.message and choice.message.content:
        return str(choice.message.content)
    return ""


# ─────────────────────────────────────────────────────────────────────
# Response parsing
# ─────────────────────────────────────────────────────────────────────


_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def _parse_vision_response(raw: str) -> dict[str, Any] | None:
    """Extract a JSON object from a vision LLM response.

    Tolerates markdown fences and trailing commentary by scanning for
    the first ``{...}`` block. Returns ``None`` when nothing parses
    or when the LLM reported ``type=null`` (no banner found).
    """
    if not raw or not isinstance(raw, str):
        return None
    m = _JSON_OBJECT_RE.search(raw)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("type") is None:
        return None
    return obj


def _looks_like_banner(text: str) -> bool:
    """Sanity-check that vision-extracted text is plausibly a banner.

    Guards against the LLM hallucinating a banner from unrelated
    page chrome. Any keyword from :data:`_BANNER_KEYWORDS` is enough
    — the same set the text-based capture uses.
    """
    low = text.lower()
    return any(kw in low for kw in _BANNER_KEYWORDS)
