"""Azure OpenAI LLM provider implementation."""
from __future__ import annotations

import base64
import json
import os
from typing import Any

from llm.base import LLMProvider

try:
    from openai import (
        APIError,
        AsyncAzureOpenAI,
        RateLimitError,
    )
except ImportError:  # pragma: no cover
    AsyncAzureOpenAI = None  # type: ignore[assignment,misc]

    class APIError(Exception):  # type: ignore[no-redef]
        pass

    class RateLimitError(Exception):  # type: ignore[no-redef]
        pass


AZURE_IMAGE_LIMIT_BYTES = 20 * 1024 * 1024


class AzureLLMProvider(LLMProvider):
    """GPT-4o / GPT-4o-mini via Azure OpenAI."""

    def __init__(self) -> None:
        if AsyncAzureOpenAI is None:
            raise RuntimeError("openai package not installed")
        self._client = AsyncAzureOpenAI(
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT", ""),
            api_key=os.getenv("AZURE_OPENAI_API_KEY", ""),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
        )
        self._text_model = os.getenv("AZURE_OPENAI_DEPLOYMENT_GPT4O_MINI", "gpt-4o-mini")
        self._vision_model = os.getenv("AZURE_OPENAI_DEPLOYMENT_GPT4O_VISION", "gpt-4o")

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
            response_format={"type": "json_object"},
        )
        # Capture usage for interaction logging (instance-scoped, not module-level).
        usage = resp.usage
        self._last_usage: dict[str, object] = {
            "input_tokens":  usage.prompt_tokens if usage else 0,
            "output_tokens": usage.completion_tokens if usage else 0,
            "model":         self._text_model,
            "call_type":     "text",
            "provider":      "azure",
        }
        return resp.choices[0].message.content or ""

    async def _extract_images_once(
        self, images: list[bytes], prompt: str, max_tokens: int,
    ) -> dict[str, Any]:
        from llm.images import check_size

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            sized = check_size(img, AZURE_IMAGE_LIMIT_BYTES)
            b64 = base64.b64encode(sized).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
        resp = await self._client.chat.completions.create(  # type: ignore[call-overload]
            model=self._vision_model,
            messages=[{"role": "user", "content": content}],
            temperature=0.0,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        # Capture usage for interaction logging.
        usage = resp.usage
        self._last_usage = {
            "input_tokens":  usage.prompt_tokens if usage else 0,
            "output_tokens": usage.completion_tokens if usage else 0,
            "model":         self._vision_model,
            "call_type":     "vision",
            "provider":      "azure",
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
        return AZURE_IMAGE_LIMIT_BYTES
