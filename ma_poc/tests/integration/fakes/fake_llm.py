"""FakeLLM — LLMProvider double for text-completion paths.

Patches llm.factory.get_text_provider so that llm_extractor, llm_prompt,
and all callers that do ``from llm.factory import get_text_provider`` receive
this fake at call time. llm_prompt and llm_gate are NOT patched — they run
real, which is the point.

Usage (via fixture — prefer the conftest fixture over direct instantiation)::

    fake_llm.returns({"units": [{"beds": 1, "rent_low": 1500}]})
    fake_llm.returns_text("some raw response")
    fake_llm.raises_rate_limit()

    # After test runs:
    assert len(fake_llm.recorded_prompts) == 1
    assert "rent" in fake_llm.recorded_prompts[0]["user"]
"""

from __future__ import annotations

import json
from typing import Any

from ma_poc.llm.base import LLMProvider


class _FakeRateLimitError(Exception):
    """Stands in for a 429 rate-limit error from any LLM SDK."""


class FakeLLM(LLMProvider):
    """Scripted text-completion provider.

    Responses are consumed in FIFO order. If the queue is empty the
    fake returns an empty-units JSON blob so tests that don't pre-configure
    a response don't crash with an unexpected exception.
    """

    def __init__(self) -> None:
        self._response_queue: list[str] = []
        self._error_queue: list[BaseException] = []
        self._recorded_prompts: list[dict[str, str]] = []
        self._call_count: int = 0

    # ------------------------------------------------------------------
    # Configuration API
    # ------------------------------------------------------------------

    def returns(self, payload: dict[str, Any] | str) -> "FakeLLM":
        """Queue a JSON response (dict is auto-serialised). Returns self."""
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        self._response_queue.append(payload)
        return self

    def returns_text(self, text: str) -> "FakeLLM":
        """Queue a raw text response (no serialisation). Returns self."""
        self._response_queue.append(text)
        return self

    def raises_rate_limit(self) -> "FakeLLM":
        """Queue a rate-limit error that the retry wrapper will absorb."""
        self._error_queue.append(_FakeRateLimitError("429 rate limited"))
        return self

    def reset(self) -> None:
        """Clear queues and recorded calls."""
        self._response_queue.clear()
        self._error_queue.clear()
        self._recorded_prompts.clear()
        self._call_count = 0

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def recorded_prompts(self) -> list[dict[str, str]]:
        """All (system, user) dicts in call order."""
        return self._recorded_prompts

    @property
    def call_count(self) -> int:
        return self._call_count

    # ------------------------------------------------------------------
    # LLMProvider ABC implementation
    # ------------------------------------------------------------------

    async def _complete_once(self, system: str, user: str, max_tokens: int) -> str:
        self._call_count += 1
        self._recorded_prompts.append({"system": system, "user": user})
        if self._error_queue:
            raise self._error_queue.pop(0)
        if self._response_queue:
            return self._response_queue.pop(0)
        return json.dumps({"units": []})

    async def _extract_images_once(
        self, images: list[bytes], prompt: str, max_tokens: int
    ) -> dict[str, Any]:
        # Text provider should not be called for vision; return empty.
        return {"units": [], "profile_hints": {}}

    def _is_rate_limit_error(self, exc: BaseException) -> bool:
        return isinstance(exc, _FakeRateLimitError)

    @property
    def image_size_limit_bytes(self) -> int:
        return 5 * 1024 * 1024
