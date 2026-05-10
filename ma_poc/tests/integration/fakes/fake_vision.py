"""FakeVisionProvider — LLMProvider double for vision-extraction paths.

Patches llm.factory.get_vision_provider so that vision_extractor.py
receives this fake. Designed to return scripted unit lists.

Usage (via fixture — prefer the conftest fixture)::

    fake_vision.returns_units([{"beds": 1, "rent_low": 1800}])

    # After test runs:
    assert fake_vision.call_count == 1
"""

from __future__ import annotations

from typing import Any

from ma_poc.llm.base import LLMProvider


class FakeVisionProvider(LLMProvider):
    """Scripted vision-extraction provider.

    Unit lists are consumed in FIFO order per call to
    ``extract_from_images``. When the queue is exhausted the fake
    returns an empty units list.
    """

    def __init__(self) -> None:
        self._unit_sequences: list[list[dict[str, Any]]] = []
        self._profile_hint_sequences: list[dict[str, Any]] = []
        self._call_count: int = 0

    # ------------------------------------------------------------------
    # Configuration API
    # ------------------------------------------------------------------

    def returns_units(
        self,
        units: list[dict[str, Any]],
        profile_hints: dict[str, Any] | None = None,
    ) -> "FakeVisionProvider":
        """Queue a unit list (and optional profile_hints) for one call."""
        self._unit_sequences.append(units)
        self._profile_hint_sequences.append(profile_hints or {})
        return self

    def reset(self) -> None:
        self._unit_sequences.clear()
        self._profile_hint_sequences.clear()
        self._call_count = 0

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def call_count(self) -> int:
        return self._call_count

    # ------------------------------------------------------------------
    # LLMProvider ABC implementation
    # ------------------------------------------------------------------

    async def _complete_once(self, system: str, user: str, max_tokens: int) -> str:
        # Vision provider should not be called for text completions.
        return '{"units": []}'

    async def _extract_images_once(
        self, images: list[bytes], prompt: str, max_tokens: int
    ) -> dict[str, Any]:
        self._call_count += 1
        units: list[dict[str, Any]] = []
        hints: dict[str, Any] = {}
        if self._unit_sequences:
            units = self._unit_sequences.pop(0)
            hints = self._profile_hint_sequences.pop(0)
        return {
            "units": units,
            "profile_hints": hints,
        }

    def _is_rate_limit_error(self, exc: BaseException) -> bool:
        return False

    @property
    def image_size_limit_bytes(self) -> int:
        return 5 * 1024 * 1024
