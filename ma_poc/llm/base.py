"""Abstract base class for LLM providers."""
from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from typing import Any

# Bug-hunt #13: random jitter on retry, NOT fixed backoff.
# Tests override RETRY_WAIT_CAP_SECONDS to 0 so unit tests don't sleep.
MAX_RETRIES = 5
RETRY_WAIT_CAP_SECONDS = 8.0


class LLMProvider(ABC):
    """Unified interface for text completions and vision extraction."""

    @abstractmethod
    async def _complete_once(
        self, system: str, user: str, max_tokens: int,
    ) -> str:
        """Single attempt at a text completion. Must raise rate-limit errors."""
        ...

    @abstractmethod
    async def _extract_images_once(
        self, images: list[bytes], prompt: str, max_tokens: int,
    ) -> dict[str, Any]:
        """Single attempt at a vision extraction. Must raise rate-limit errors."""
        ...

    @abstractmethod
    def _is_rate_limit_error(self, exc: BaseException) -> bool:
        """Return True if *exc* is a rate-limit / 429 error for this provider."""
        ...

    @property
    @abstractmethod
    def image_size_limit_bytes(self) -> int:
        """Max image payload size in bytes for this provider."""
        ...

    # ------------------------------------------------------------------
    # Public API — retry wrapper (bug-hunt #13)
    # ------------------------------------------------------------------

    async def complete(
        self, system: str, user: str, *, max_tokens: int = 4096,
    ) -> str:
        """Text completion with jitter-based retry on rate limits."""
        result: str = await self._with_retry(self._complete_once, system, user, max_tokens)
        return result

    async def extract_from_images(
        self, images: list[bytes], prompt: str, *, max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Vision extraction with jitter-based retry on rate limits."""
        result: dict[str, Any] = await self._with_retry(self._extract_images_once, images, prompt, max_tokens)
        return result

    async def _with_retry(self, fn: Any, *args: Any) -> Any:
        last_exc: BaseException | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return await fn(*args)
            except Exception as exc:
                if not self._is_rate_limit_error(exc):
                    raise
                last_exc = exc
                if attempt + 1 >= MAX_RETRIES:
                    raise
                await asyncio.sleep(random.uniform(0, RETRY_WAIT_CAP_SECONDS))
        assert last_exc is not None
        raise last_exc
