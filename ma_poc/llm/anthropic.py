"""Anthropic (Claude) LLM provider implementation."""
from __future__ import annotations

import base64
import json
import os
from typing import Any

from llm.base import LLMProvider

try:
    from anthropic import (
        AsyncAnthropic,
        APIError as AnthropicAPIError,
        RateLimitError as AnthropicRateLimitError,
    )
except ImportError:  # pragma: no cover
    AsyncAnthropic = None  # type: ignore[assignment,misc]

    class AnthropicAPIError(Exception):  # type: ignore[no-redef]
        pass

    class AnthropicRateLimitError(Exception):  # type: ignore[no-redef]
        pass


ANTHROPIC_IMAGE_LIMIT_BYTES = 5 * 1024 * 1024


class AnthropicLLMProvider(LLMProvider):
    """Claude via the Anthropic Messages API."""

    def __init__(self) -> None:
        if AsyncAnthropic is None:
            raise RuntimeError("anthropic package not installed")
        self._client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        self._text_model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        self._vision_model = os.getenv("ANTHROPIC_VISION_MODEL", "claude-3-5-sonnet-20241022")

    async def _complete_once(
        self, system: str, user: str, max_tokens: int,
    ) -> str:
        resp = await self._client.messages.create(
            model=self._text_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(getattr(blk, "text", "") for blk in resp.content)

    async def _extract_images_once(
        self, images: list[bytes], prompt: str, max_tokens: int,
    ) -> dict[str, Any]:
        from llm.images import check_size

        content: list[dict[str, Any]] = []
        for img in images:
            sized = check_size(img, ANTHROPIC_IMAGE_LIMIT_BYTES)
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(sized).decode("ascii"),
                },
            })
        content.append({"type": "text", "text": prompt})
        resp = await self._client.messages.create(
            model=self._vision_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}],  # type: ignore[typeddict-item]
        )
        text = "".join(getattr(blk, "text", "") for blk in resp.content)
        try:
            return json.loads(text)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            return {"units": [], "extraction_notes": "json_decode_failed"}

    def _is_rate_limit_error(self, exc: BaseException) -> bool:
        return isinstance(exc, AnthropicRateLimitError)

    @property
    def image_size_limit_bytes(self) -> int:
        return ANTHROPIC_IMAGE_LIMIT_BYTES
