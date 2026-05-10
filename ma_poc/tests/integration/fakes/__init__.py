"""Shared test doubles for the scraper pipeline integration suite.

Each fake mocks at the process/network boundary only; all in-process
logic (llm_prompt, llm_gate, source_merger, etc.) runs real.
"""

from .event_spy import EventSpy
from .fake_browser import PLAYWRIGHT_STUB_MODULES, build_playwright_stub
from .fake_fetcher import FakeFetcher
from .fake_llm import FakeLLM
from .fake_vision import FakeVisionProvider

__all__ = [
    "EventSpy",
    "FakeFetcher",
    "FakeLLM",
    "FakeVisionProvider",
    "PLAYWRIGHT_STUB_MODULES",
    "build_playwright_stub",
]
