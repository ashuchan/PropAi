"""Guard: no test under ``tests/pms`` may resolve a path against the CWD.

2026-07-26: 77 of the 82 failures seen by anyone running
``cd ma_poc && pytest tests/pms`` were a single bug repeated across 11
files — fixture/source paths written as bare relative literals
(``Path("ma_poc/tests/fixtures/...")``). Those only resolve when the
process CWD happens to be the repo root, so the directory passed in the
full-suite run (launched from the root) and failed standalone. The
symptom reads like flake or ordering; it is neither.

Paths in tests must be anchored on ``__file__``:

    Path(__file__).resolve().parents[2] / "fixtures" / "<name>"   # tests/fixtures
    Path(__file__).parent / "fixtures" / "<name>"                 # colocated
"""

from __future__ import annotations

import re
from pathlib import Path

_PMS_TESTS = Path(__file__).resolve().parent

# ``Path("ma_poc/...")`` / ``open('ma_poc/...')`` and the same shapes for a
# bare ``tests/`` prefix — every one of these is CWD-relative.
_CWD_RELATIVE = re.compile(
    r"""(?:Path|open)\(\s*["'](?:ma_poc/|tests/)""",
)


def _python_sources() -> list[Path]:
    return sorted(p for p in _PMS_TESTS.rglob("*.py") if p != Path(__file__))


def test_no_cwd_relative_paths_under_tests_pms() -> None:
    """Every path literal must be anchored on ``__file__``, not the CWD."""
    offenders: list[str] = []
    for path in _python_sources():
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _CWD_RELATIVE.search(line):
                rel = path.relative_to(_PMS_TESTS)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "CWD-relative path literal(s) in tests/pms — these break "
        "`cd ma_poc && pytest tests/pms` while passing from the repo root. "
        "Anchor on Path(__file__) instead:\n  " + "\n  ".join(offenders)
    )
