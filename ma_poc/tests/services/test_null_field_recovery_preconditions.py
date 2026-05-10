"""Preconditions for ``_run_null_field_recovery`` (F2 LLM rescue).

Two checks gate F2 before any LLM cost:
  1. At least one ``raw_api`` has a non-empty dict/list body — empty
     bodies have nothing to recover from.
  2. NOT every unit has BOTH rent_low AND unit_id null — when both are
     uniformly null this is parser-tier failure, not field-recovery
     territory; F2 won't help.

These tests target the ``_f2_has_recoverable_body`` helper directly. The
full ``_run_null_field_recovery`` is async + relies on disk-side
artifact dirs + makes LLM calls, so helper-level tests are sufficient
for the precondition logic; the runner-level wiring is exercised
end-to-end in the cloud-run integration suite.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Add ma_poc/ root to sys.path so `from scripts.runners.jugnu` works.
_MA_POC_ROOT = Path(__file__).resolve().parents[2]
if str(_MA_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_MA_POC_ROOT))


@pytest.fixture
def f2_helper() -> Any:
    """Lazy-import the helper. The module imports playwright et al at
    top level, but the helper is pure-python and reachable via the
    module attribute even if some submodule imports fail."""
    from scripts.runners.jugnu import _f2_has_recoverable_body
    return _f2_has_recoverable_body


# ── _f2_has_recoverable_body ───────────────────────────────────────────────


class TestHasRecoverableBody:
    def test_empty_list_is_not_recoverable(self, f2_helper: Any) -> None:
        assert f2_helper([]) is False

    def test_all_none_bodies_not_recoverable(self, f2_helper: Any) -> None:
        assert f2_helper([{"url": "x", "body": None}]) is False

    def test_all_empty_dict_bodies_not_recoverable(self, f2_helper: Any) -> None:
        assert f2_helper([{"url": "x", "body": {}}]) is False

    def test_all_empty_list_bodies_not_recoverable(self, f2_helper: Any) -> None:
        assert f2_helper([{"url": "x", "body": []}]) is False

    def test_one_non_empty_dict_body_is_recoverable(self, f2_helper: Any) -> None:
        assert f2_helper([
            {"url": "x", "body": None},
            {"url": "y", "body": {"floorplans": [{"id": 1}]}},
        ]) is True

    def test_one_non_empty_list_body_is_recoverable(self, f2_helper: Any) -> None:
        assert f2_helper([{"url": "x", "body": [{"id": 1}]}]) is True

    def test_non_dict_entries_skipped(self, f2_helper: Any) -> None:
        """Defensive against malformed _raw_api_responses payloads."""
        assert f2_helper(["not a dict", None, 42]) is False

    def test_dict_without_body_key_skipped(self, f2_helper: Any) -> None:
        assert f2_helper([{"url": "x"}]) is False
