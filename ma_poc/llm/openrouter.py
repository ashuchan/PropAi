"""OpenRouter LLM provider implementation.

Uses the OpenAI-compatible API with a custom base URL pointing to OpenRouter.
Configure via env vars:
  OPENROUTER_API_KEY       — required
  OPENROUTER_MODEL         — text model (default: google/gemini-2.5-flash)
  OPENROUTER_VISION_MODEL  — vision model (default: google/gemini-2.5-flash)
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any

from llm.base import LLMProvider

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_IMAGE_LIMIT_BYTES = 20 * 1024 * 1024  # 20 MB conservative default

try:
    from openai import (
        AsyncOpenAI,
        RateLimitError,
    )
except ImportError:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore[assignment,misc]

    class RateLimitError(Exception):  # type: ignore[no-redef]
        pass


class OpenRouterLLMProvider(LLMProvider):
    """LLM provider routing through OpenRouter's OpenAI-compatible API."""

    def __init__(self) -> None:
        if AsyncOpenAI is None:
            raise RuntimeError("openai package not installed")
        self._client = AsyncOpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=os.getenv("OPENROUTER_API_KEY", ""),
        )
        self._text_model = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
        self._vision_model = os.getenv(
            "OPENROUTER_VISION_MODEL", "google/gemini-2.5-flash",
        )

    async def _complete_once(
        self, system: str, user: str, max_tokens: int,
    ) -> str:
        resp = await self._client.chat.completions.create(
            model=self._text_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=max_tokens,
        )
        usage = resp.usage
        self._last_usage: dict[str, object] = {
            "input_tokens": usage.prompt_tokens if usage else 0,
            "output_tokens": usage.completion_tokens if usage else 0,
            "model": self._text_model,
            "call_type": "text",
            "provider": "openrouter",
        }
        return resp.choices[0].message.content or ""

    async def _extract_images_once(
        self, images: list[bytes], prompt: str, max_tokens: int,
    ) -> dict[str, Any]:
        from llm.images import check_size

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            sized = check_size(img, OPENROUTER_IMAGE_LIMIT_BYTES)
            b64 = base64.b64encode(sized).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
        resp = await self._client.chat.completions.create(
            model=self._vision_model,
            messages=[{"role": "user", "content": content}],  # type: ignore[list-item]
            temperature=0.0,
            max_tokens=max_tokens,
        )
        usage = resp.usage
        self._last_usage = {
            "input_tokens": usage.prompt_tokens if usage else 0,
            "output_tokens": usage.completion_tokens if usage else 0,
            "model": self._vision_model,
            "call_type": "vision",
            "provider": "openrouter",
        }
        text = resp.choices[0].message.content or "{}"
        try:
            return json.loads(text)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            return {"units": [], "extraction_notes": "json_decode_failed"}

    def _is_rate_limit_error(self, exc: BaseException) -> bool:
        return isinstance(exc, RateLimitError)

    @property
    def image_size_limit_bytes(self) -> int:
        return OPENROUTER_IMAGE_LIMIT_BYTES
