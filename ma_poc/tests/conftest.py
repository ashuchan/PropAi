"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path
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


