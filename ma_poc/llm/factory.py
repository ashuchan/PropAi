"""Factory functions for obtaining LLM providers based on env config.

LLM_PROVIDER controls both text and vision by default.
VISION_PROVIDER overrides the vision provider independently if set.
"""
from __future__ import annotations

import os

from llm.base import LLMProvider


def _resolve(env_var: str, default: str = "anthropic") -> str:
    return os.getenv(env_var, default).strip().lower()


def _build_provider(name: str) -> LLMProvider:
    """Instantiate a provider by its canonical name."""
    if name == "anthropic":
        from llm.anthropic import AnthropicLLMProvider
        return AnthropicLLMProvider()
    if name == "openrouter":
        from llm.openrouter import OpenRouterLLMProvider
        return OpenRouterLLMProvider()
    from llm.azure import AzureLLMProvider
    return AzureLLMProvider()


def get_text_provider() -> LLMProvider:
    """Return a text-completion LLM provider based on LLM_PROVIDER env var."""
    return _build_provider(_resolve("LLM_PROVIDER"))


def get_vision_provider() -> LLMProvider:
    """Return a vision LLM provider.

    Uses VISION_PROVIDER if set, otherwise falls back to LLM_PROVIDER.
    """
    return _build_provider(_resolve("VISION_PROVIDER", os.getenv("LLM_PROVIDER", "anthropic")))
