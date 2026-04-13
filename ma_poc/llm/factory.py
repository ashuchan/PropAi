"""Factory functions for obtaining LLM providers based on env config.

LLM_PROVIDER controls both text and vision by default.
VISION_PROVIDER overrides the vision provider independently if set.
"""
from __future__ import annotations

import os

from llm.base import LLMProvider


def _resolve(env_var: str, default: str = "anthropic") -> str:
    return os.getenv(env_var, default).strip().lower()


def get_text_provider() -> LLMProvider:
    """Return a text-completion LLM provider based on LLM_PROVIDER env var."""
    provider = _resolve("LLM_PROVIDER")
    if provider == "anthropic":
        from llm.anthropic import AnthropicLLMProvider
        return AnthropicLLMProvider()
    from llm.azure import AzureLLMProvider
    return AzureLLMProvider()


def get_vision_provider() -> LLMProvider:
    """Return a vision LLM provider.

    Uses VISION_PROVIDER if set, otherwise falls back to LLM_PROVIDER.
    """
    provider = _resolve("VISION_PROVIDER", os.getenv("LLM_PROVIDER", "anthropic"))
    if provider == "anthropic":
        from llm.anthropic import AnthropicLLMProvider
        return AnthropicLLMProvider()
    from llm.azure import AzureLLMProvider
    return AzureLLMProvider()
