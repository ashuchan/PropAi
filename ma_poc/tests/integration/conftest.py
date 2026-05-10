"""Layer-level fixtures for the integration test suite.

This conftest provides shared infrastructure for all tests under
tests/integration/. It does NOT replace tests/conftest.py — the root
conftest's HTML fixtures (rentcafe_html, entrata_html, etc.) remain
available; this file adds the layer-crossing primitives described in
INTEGRATION_TEST_PLAN.md §3.

Fixture ownership:
  - patch_playwright_modules  autouse — stubs playwright/patchright globally
  - fake_fetcher              FakeFetcher (scripted HTTP boundary)
  - fake_llm                  FakeLLM (patches llm.factory.get_text_provider)
  - fake_vision               FakeVisionProvider (patches get_vision_provider)
  - event_spy                 EventSpy (captures observability.events.emit)
  - fs_provider               FileSystemDataProvider over tmp_path
  - sqlite_provider           SqliteDataProvider with in-memory DB
  - dual_provider             DualWriteDataProvider(fs_provider, sqlite_provider)
  - run_dir                   RunDirectoryFactory (builds on-disk run layouts)
  - profile_factory           ScrapeProfileFactory (auto-increments canonical_ids)
  - corpus                    CorpusLoader (loads files from tests/integration/corpus/)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.integration.fakes.event_spy import EventSpy
from tests.integration.fakes.fake_browser import (
    PLAYWRIGHT_STUB_MODULES,
    _build_playwright_stub,
)
from tests.integration.fakes.fake_fetcher import FakeFetcher
from tests.integration.fakes.fake_llm import FakeLLM
from tests.integration.fakes.fake_vision import FakeVisionProvider
from tests.integration.factories.run_directory import RunDirectoryFactory
from tests.integration.factories.scrape_profile import ScrapeProfileFactory

_CORPUS_DIR = Path(__file__).parent / "corpus"


# ---------------------------------------------------------------------------
# Browser stubs (autouse — every integration test gets this automatically)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def patch_playwright_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub playwright and patchright in sys.modules for every integration test.

    This consolidates the ad-hoc ``sys.modules["playwright"] = MagicMock()``
    calls scattered across legacy test_pr* files. New tests under
    tests/integration/ never need to do that themselves.

    The monkeypatch fixture ensures these stubs are removed cleanly after
    each test, preventing cross-test contamination.
    """
    import sys

    for name in PLAYWRIGHT_STUB_MODULES:
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, _build_playwright_stub(name))


# ---------------------------------------------------------------------------
# HTTP boundary fake
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_fetcher() -> FakeFetcher:
    """Return a fresh FakeFetcher.

    Usage::

        fake_fetcher.responses_for("https://example.com", [
            build_fetch_result(html="<html>...</html>"),
        ])
        result = await fake_fetcher.fetch(task)
        fake_fetcher.assert_url_attempted("https://example.com")
    """
    return FakeFetcher()


# ---------------------------------------------------------------------------
# LLM boundary fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """Return a FakeLLM that replaces the real text provider.

    Patches ``llm.factory.get_text_provider`` so that all in-process callers
    (llm_extractor, llm_prompt) receive the fake at call time. llm_gate and
    llm_prompt themselves run real — only the SDK boundary is mocked.

    Usage::

        fake_llm.returns({"units": [{"beds": 1, "rent_low": 1500}]})
        # ... run test that triggers LLM extraction ...
        assert fake_llm.call_count == 1
    """
    import llm.factory as _llm_factory

    fake = FakeLLM()
    monkeypatch.setattr(_llm_factory, "get_text_provider", lambda: fake)
    return fake


@pytest.fixture
def fake_vision(monkeypatch: pytest.MonkeyPatch) -> FakeVisionProvider:
    """Return a FakeVisionProvider that replaces the real vision provider.

    Patches ``llm.factory.get_vision_provider``.

    Usage::

        fake_vision.returns_units([{"beds": 2, "rent_low": 2200}])
        # ... run test that triggers Tier-5 vision fallback ...
        assert fake_vision.call_count == 1
    """
    import llm.factory as _llm_factory

    fake = FakeVisionProvider()
    monkeypatch.setattr(_llm_factory, "get_vision_provider", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# Event observability spy
# ---------------------------------------------------------------------------


@pytest.fixture
def event_spy(monkeypatch: pytest.MonkeyPatch) -> EventSpy:
    """Capture all observability.events.emit() calls for the duration of a test.

    Usage::

        # After running code that calls emit():
        event_spy.assert_emitted(EventKind.PMS_DETECTED, property_id="prop-001")
        tier_won = event_spy.all_of_kind(EventKind.TIER_WON)
        assert len(tier_won) == 1
    """
    import ma_poc.observability.events as _events_mod

    spy = EventSpy()
    monkeypatch.setattr(_events_mod, "emit", spy.record)
    return spy


# ---------------------------------------------------------------------------
# Data provider fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fs_provider(tmp_path: Path) -> Any:
    """FileSystemDataProvider backed by tmp_path.

    Creates the necessary directory layout (state/, config/profiles/, etc.)
    and returns a ready-to-use provider. Always use this in tests — never
    create FileSystemDataProvider directly because the directory layout must
    match what the factory method builds.
    """
    from ma_poc.data_provider.filesystem import FileSystemDataProvider

    run_factory = RunDirectoryFactory(tmp_path)
    base_dir, config_dir = run_factory.build_fs_provider_dirs()
    return FileSystemDataProvider(base_dir=base_dir, config_dir=config_dir)


@pytest.fixture
def sqlite_provider() -> Any:
    """SqliteDataProvider backed by an in-memory SQLite database.

    A fresh in-memory DB is created for each test — no teardown needed.
    """
    from ma_poc.data_provider.sqlite import SqliteDataProvider

    return SqliteDataProvider(url="sqlite:///:memory:", create_schema=True)


@pytest.fixture
def dual_provider(fs_provider: Any, sqlite_provider: Any) -> Any:
    """DualWriteDataProvider wrapping fs_provider (primary) and sqlite_provider (secondary).

    This is the exact real-world DualWrite contract: writes go to both,
    reads come from the FS primary. Tests that verify dual-write consistency
    use both ``dual_provider.property_state`` (reads FS) and
    ``sqlite_provider.property_state`` (reads SQLite directly).
    """
    from ma_poc.data_provider.dual_write import DualWriteDataProvider

    return DualWriteDataProvider(primary=fs_provider, secondary=sqlite_provider)


# ---------------------------------------------------------------------------
# Run directory factory
# ---------------------------------------------------------------------------


@pytest.fixture
def run_dir(tmp_path: Path) -> RunDirectoryFactory:
    """Return a RunDirectoryFactory rooted at tmp_path.

    Usage::

        run_path = run_dir.build(
            date="2026-05-10",
            properties=[{"canonical_id": "prop-001", "verdict": "SUCCESS"}],
        )
        base_dir, config_dir = run_dir.build_fs_provider_dirs()
    """
    return RunDirectoryFactory(tmp_path)


# ---------------------------------------------------------------------------
# Profile factory
# ---------------------------------------------------------------------------


@pytest.fixture
def profile_factory() -> ScrapeProfileFactory:
    """Return a ScrapeProfileFactory with auto-incrementing canonical_ids.

    Usage::

        p1 = profile_factory.build(maturity=ProfileMaturity.HOT)
        p2 = profile_factory.build(preferred_tier=1, consecutive_successes=3)
    """
    return ScrapeProfileFactory()


# ---------------------------------------------------------------------------
# Corpus loader
# ---------------------------------------------------------------------------


class CorpusLoader:
    """Loads versioned HTML/JSON fixture files from tests/integration/corpus/.

    Obtain via the ``corpus`` pytest fixture. Corpus files are checked into
    the repo and must not be generated at test time.
    """

    def __init__(self, corpus_dir: Path) -> None:
        self._dir = corpus_dir

    def load(self, relative_path: str) -> str:
        """Load a text file (HTML, XML, etc.) from the corpus.

        Args:
            relative_path: Path relative to the corpus directory,
                e.g. ``"rentcafe/listing.html"``.

        Raises:
            FileNotFoundError: When the file doesn't exist yet.
                Add it per the instructions in corpus/README.md.
        """
        path = self._dir / relative_path
        if not path.exists():
            raise FileNotFoundError(
                f"Corpus file not found: {path}\n"
                "See tests/integration/corpus/README.md for instructions on adding corpus entries."
            )
        return path.read_text(encoding="utf-8")

    def load_json(self, relative_path: str) -> Any:
        """Load and parse a JSON file from the corpus."""
        return json.loads(self.load(relative_path))

    def load_bytes(self, relative_path: str) -> bytes:
        """Load a binary file (e.g. screenshot PNG) from the corpus."""
        path = self._dir / relative_path
        if not path.exists():
            raise FileNotFoundError(
                f"Corpus file not found: {path}\n"
                "See tests/integration/corpus/README.md for instructions on adding corpus entries."
            )
        return path.read_bytes()

    def exists(self, relative_path: str) -> bool:
        """Return True if the corpus file exists (useful for conditional tests)."""
        return (self._dir / relative_path).exists()


@pytest.fixture
def corpus() -> CorpusLoader:
    """Return a CorpusLoader pointed at tests/integration/corpus/.

    Usage::

        html = corpus.load("rentcafe/listing.html")
        api  = corpus.load_json("rentcafe/api_units.json")
    """
    return CorpusLoader(_CORPUS_DIR)
