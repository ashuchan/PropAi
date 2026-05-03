"""PR24 Fix 12 — no bare imports in library files."""

from __future__ import annotations

import re
from pathlib import Path


_MA_POC = Path(__file__).resolve().parent.parent.parent  # ma_poc/


def _read(rel: str) -> str:
    return (_MA_POC / rel).read_text(encoding="utf-8")


def test_no_bare_imports_in_source_planner() -> None:
    """source_planner.py must not use bare 'from models.' or 'from services.'"""
    text = _read("services/source_planner.py")
    bare = re.findall(r"^\s*from\s+(models|services)\.", text, re.MULTILINE)
    assert not bare, f"bare imports in source_planner.py: {bare}"


def test_no_bare_imports_in_profile_updater() -> None:
    """profile_updater.py must not use bare 'from models.' or 'from services.'"""
    text = _read("services/profile_updater.py")
    bare = re.findall(r"^\s*from\s+(models|services)\.", text, re.MULTILINE)
    assert not bare, f"bare imports in profile_updater.py: {bare}"


def test_no_bare_imports_in_source_merger() -> None:
    """source_merger.py must not use bare 'from models.' or 'from services.'"""
    text = _read("services/source_merger.py")
    bare = re.findall(r"^\s*from\s+(models|services)\.", text, re.MULTILINE)
    assert not bare, f"bare imports in source_merger.py: {bare}"


def test_no_bare_imports_in_cluster_store() -> None:
    """cluster_store.py must not use bare 'from models.' or 'from services.'"""
    text = _read("services/cluster_store.py")
    bare = re.findall(r"^\s*from\s+(models|services)\.", text, re.MULTILINE)
    assert not bare, f"bare imports in cluster_store.py: {bare}"


def test_no_bare_imports_in_source_observer() -> None:
    """source_observer.py must not use bare 'from models.' or 'from services.'"""
    text = _read("services/source_observer.py")
    bare = re.findall(r"^\s*from\s+(models|services)\.", text, re.MULTILINE)
    assert not bare, f"bare imports in source_observer.py: {bare}"
