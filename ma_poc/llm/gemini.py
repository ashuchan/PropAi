"""Gemini (Google) LLM provider implementation.

Uses the google-genai SDK directly (the same SDK Phase 8's concession cascade
already imports). Configure via env vars:

  GEMINI_API_KEY        — required
  GEMINI_TEXT_MODEL     — text model (default: gemini-2.5-flash)
  GEMINI_VISION_MODEL   — vision model (default: gemini-2.5-flash)

Why Gemini Flash by default? Per the 2026-04-30 failure-recovery
investigation: ~5× cheaper than GPT-4o-mini with comparable quality on the
extraction task. Flash also handles vision with the same model — no separate
vision deployment needed.
"""

from __future__ import annotations

import json
import os
from typing import Any

from llm.base import LLMProvider

try:
    from google import genai
    from google.genai import types as genai_types
    from google.genai.errors import APIError as GeminiAPIError
except ImportError:  # pragma: no cover
    genai = None  # type: ignore[assignment]
    genai_types = None  # type: ignore[assignment]

    class GeminiAPIError(Exception):  # type: ignore[no-redef]
        code: int = 0
        status: str | None = None


# Gemini's documented per-image inline limit. Total request also caps at
# ~20MB but a single banner image won't approach that — keep the check tight
# so we never silently fail on oversized hero images.
GEMINI_IMAGE_LIMIT_BYTES = 4 * 1024 * 1024


class GeminiLLMProvider(LLMProvider):
    """Gemini Flash via the google-genai SDK."""

    def __init__(self) -> None:
        if genai is None:
            raise RuntimeError("google-genai package not installed")
        api_key = os.getenv("GEMINI_API_KEY", "").strip().lstrip("﻿")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        self._client = genai.Client(api_key=api_key)
        self._text_model = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
        self._vision_model = os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-flash")
        self._last_usage: dict[str, object] = {}

    async def _complete_once(
        self,
        system: str,
        user: str,
        max_tokens: int,
    ) -> str:
        config = genai_types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            temperature=0.1,
        )
        resp = await self._client.aio.models.generate_content(
            model=self._text_model,
            contents=user,
            config=config,
        )
        self._capture_usage(resp, model=self._text_model, call_type="text")
        return resp.text or ""

    async def _extract_images_once(
        self,
        images: list[bytes],
        prompt: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        from llm.images import check_size

        parts: list[Any] = []
        for img in images:
            sized = check_size(img, GEMINI_IMAGE_LIMIT_BYTES)
            parts.append(genai_types.Part.from_bytes(data=sized, mime_type="image/png"))
        parts.append(genai_types.Part.from_text(text=prompt))
        content = genai_types.Content(role="user", parts=parts)

        config = genai_types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=0.1,
            response_mime_type="application/json",
        )
        resp = await self._client.aio.models.generate_content(
            model=self._vision_model,
            contents=content,
            config=config,
        )
        self._capture_usage(resp, model=self._vision_model, call_type="vision")
        text = resp.text or ""
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"units": [], "extraction_notes": "not_a_dict"}
        except json.JSONDecodeError:
            return {"units": [], "extraction_notes": "json_decode_failed"}

    def _capture_usage(self, resp: Any, *, model: str, call_type: str) -> None:
        """Record token usage for interaction logging.

        ``self._last_usage`` is instance-scoped — concurrent providers don't
        cross-contaminate (mirrors the AnthropicLLMProvider pattern).
        """
        usage = getattr(resp, "usage_metadata", None)
        self._last_usage = {
            "input_tokens": getattr(usage, "prompt_token_count", 0) if usage else 0,
            "output_tokens": getattr(usage, "candidates_token_count", 0) if usage else 0,
            "model": model,
            "call_type": call_type,
            "provider": "gemini",
        }

    def _is_rate_limit_error(self, exc: BaseException) -> bool:
        if isinstance(exc, GeminiAPIError):
            code = getattr(exc, "code", 0) or 0
            status = (getattr(exc, "status", "") or "").upper()
            return code == 429 or status == "RESOURCE_EXHAUSTED"
        return False

    @property
    def image_size_limit_bytes(self) -> int:
        return GEMINI_IMAGE_LIMIT_BYTES
