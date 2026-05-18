"""Pin Python 3.11 grammar compliance for ``backfill_property_soft_fields``.

Production Cloud Run image (``jugnu-adhoc-{env}``) runs **Python 3.11**.
Workstations and CI may be on 3.12+. PEP 701 (lifting the
"f-string expression cannot contain a backslash" rule) only landed in
3.12, so a script that parses fine locally can crash with ``SyntaxError``
at module-load time in production — exactly what happened on 2026-05-18T16:41
to execution ``jugnu-adhoc-production-9t4ls``:

    File "/app/ma_poc/scripts/diagnostics/backfill_property_soft_fields.py", line 272
        ""\"
           ^
    SyntaxError: f-string expression part cannot include a backslash

This test forces the parser into 3.11 grammar via ``ast.parse(...,
feature_version=(3, 11))`` so the same bug class cannot regress silently.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


_BACKFILL_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "diagnostics"
    / "backfill_property_soft_fields.py"
)


def test_backfill_script_exists() -> None:
    """Guard against the path moving without this test being updated."""
    assert _BACKFILL_SCRIPT.exists(), f"Expected backfill script at {_BACKFILL_SCRIPT}"


def test_backfill_script_parses_under_python_3_11_grammar() -> None:
    """Force-parse under 3.11 grammar to catch PEP 701 violations.

    The most common offender is a backslash inside an f-string expression::

        # Crashes on 3.11, fine on 3.12+:
        sql = f"... {",\\n            ".join(cols)} ..."

    Fix: pre-bind the joined string and interpolate the variable::

        joined = ",\\n            ".join(cols)
        sql = f"... {joined} ..."
    """
    src = _BACKFILL_SCRIPT.read_text(encoding="utf-8")
    try:
        ast.parse(src, feature_version=(3, 11))
    except SyntaxError as exc:
        pytest.fail(
            "Python 3.11 grammar violation in "
            f"{_BACKFILL_SCRIPT.name} at line {exc.lineno}: {exc.msg}\n"
            f"  source: {exc.text.strip() if exc.text else '(unavailable)'}\n"
            "Production Cloud Run runs Python 3.11 — see incident "
            "2026-05-18T16:41 (jugnu-adhoc-production-9t4ls). Refactor "
            "any f-string `{...}` expression that contains a backslash "
            "to pre-bind the value into a local variable first."
        )
