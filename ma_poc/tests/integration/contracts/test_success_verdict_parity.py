"""Contract test — Python ``_SUCCESS_VERDICTS`` ↔ TS ``SUCCESS_VERDICTS`` parity.

Background: the daily-failures email and the frontend dashboard both classify
properties as "succeeded" or "failed" using a verdict allowlist. The Python
runner is the source of truth (``ma_poc/reporting/verdict.py``), and the TS
service mirror lives at ``ma_poc/services/src/types/common.ts``. When the two
drift, the dashboard and email show a stale success rate while the analyzer
shows the correct one. (2026-05-18: drift had been masking ~495 successes;
both surfaces were reporting 79% while the analyzer correctly reported 89.7%.)

This test parses the TS source as text and asserts the two sets are equal.
Any new success-class verdict added on the Python side must be reflected in
the TS file in the same PR or this test fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from reporting.verdict import _SUCCESS_VERDICTS


_TS_COMMON = (
    Path(__file__).resolve().parents[3] / "services" / "src" / "types" / "common.ts"
)


def _parse_ts_success_verdicts(src: str) -> set[str]:
    """Extract the string literals inside the ``SUCCESS_VERDICTS`` set.

    Looks for ``new Set<ScrapeStatus>([ ... ])`` (with optional whitespace and
    optional type arg) and pulls each ``'XXX'`` or ``"XXX"`` literal out.
    Robust to formatting changes that re-flow the list across multiple lines.
    """
    m = re.search(
        r"SUCCESS_VERDICTS\s*:\s*ReadonlySet<[^>]+>\s*=\s*new\s+Set<[^>]+>\(\s*\[([^\]]+)\]\s*\)",
        src,
        re.DOTALL,
    )
    assert m, "Could not locate SUCCESS_VERDICTS declaration in common.ts"
    body = m.group(1)
    return set(re.findall(r"""['"]([A-Z][A-Z0-9_]+)['"]""", body))


def test_ts_common_file_exists() -> None:
    """If this fires, the TS file moved — update _TS_COMMON above."""
    assert _TS_COMMON.exists(), f"Expected TS source at {_TS_COMMON}"


def test_success_verdict_sets_match_across_python_and_ts() -> None:
    """The TS allowlist must be a superset of the Python allowlist.

    TS may keep extra legacy aliases (``SUCCESS_WITH_ERRORS`` is a frontend-
    only legacy status some old snapshots still carry). It must NOT be
    missing any Python verdict — that's the bug class this test prevents.
    """
    ts = _parse_ts_success_verdicts(_TS_COMMON.read_text(encoding="utf-8"))
    py = set(_SUCCESS_VERDICTS)
    missing_in_ts = py - ts
    assert not missing_in_ts, (
        f"TS SUCCESS_VERDICTS is missing Python verdicts {sorted(missing_in_ts)}. "
        f"Update {_TS_COMMON.name} to mirror reporting.verdict._SUCCESS_VERDICTS."
    )


def test_ts_legacy_extras_are_documented() -> None:
    """Any TS-only verdict (not in the Python set) must be a legacy alias.

    We allowlist the known legacy strings here so adding a new TS-only entry
    forces a deliberate update. The intent is to catch *accidental* drift
    (someone hand-adds a verdict to the TS list that doesn't exist in Python)
    without forbidding documented legacy aliases.
    """
    ts = _parse_ts_success_verdicts(_TS_COMMON.read_text(encoding="utf-8"))
    py = set(_SUCCESS_VERDICTS)
    known_legacy = {"SUCCESS_WITH_ERRORS"}
    unexpected = (ts - py) - known_legacy
    assert not unexpected, (
        f"TS SUCCESS_VERDICTS has unexpected verdicts {sorted(unexpected)} "
        f"not present in Python and not in known_legacy. Either add them to "
        f"reporting.verdict._SUCCESS_VERDICTS or, if they're a documented "
        f"legacy alias, add them to ``known_legacy`` in this test."
    )
