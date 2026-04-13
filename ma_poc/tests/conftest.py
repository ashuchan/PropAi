"""Shared pytest fixtures."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Make ma_poc top-level packages importable from tests
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def rentcafe_html() -> str:
    return (FIXTURES / "rentcafe_sample.html").read_text(encoding="utf-8")


@pytest.fixture
def entrata_html() -> str:
    return (FIXTURES / "entrata_sample.html").read_text(encoding="utf-8")


@pytest.fixture
def appfolio_html() -> str:
    return (FIXTURES / "appfolio_sample.html").read_text(encoding="utf-8")


@pytest.fixture
def jsonld_html() -> str:
    return (FIXTURES / "jsonld_sample.html").read_text(encoding="utf-8")


@pytest.fixture
def api_response_sample() -> str:
    return (FIXTURES / "api_response_sample.json").read_text(encoding="utf-8")


def make_session(html: str | None = None, pms: str | None = None, **kw: Any) -> Any:
    """Build a BrowserSession without launching Playwright."""
    from scraper.browser import BrowserSession

    s = BrowserSession(
        property_id=kw.get("property_id", "TEST-001"),
        url=kw.get("url", "https://example.com/property"),
        pms_platform=pms,
    )
    s.html = html
    return s
