"""Shared pytest fixtures."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

# Make ma_poc top-level packages importable from tests
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Make ma_poc/scripts importable as ``scripts.X`` (production runs daily_runner
# from this directory, so the legacy modules expect to be at the top level).
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Pre-load scripts/validation.py under the bare ``validation`` name. Without
# this, the first ``from scripts.schema_v2 import ...`` triggers
# ``import validation as V`` inside schema_v2.py, which Python resolves to
# ma_poc/validation/ (the package) — and that package's relative imports
# then explode under top-level invocation. Force-binding sys.modules
# guarantees the legacy issue-logger wins, matching production behavior
# where ma_poc/scripts is the working directory of daily_runner.
_existing = sys.modules.get("validation")
if _existing is None or not hasattr(_existing, "V2_INVALID_BEDS"):
    _spec = importlib.util.spec_from_file_location(
        "validation", SCRIPTS_DIR / "validation.py"
    )
    if _spec and _spec.loader:
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules["validation"] = _mod
        _spec.loader.exec_module(_mod)

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
