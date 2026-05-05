"""tests/scripts conftest — currently a placeholder.

The sys.path setup and ``validation`` module pre-load that scripts/-level
tests need are handled by the parent ``ma_poc/tests/conftest.py``. Keeping
this file in place so future scripts-only fixtures have a home, but it
does NOT duplicate the parent's bootstrap — running both would re-execute
``scripts/validation.py`` and replace its module reference under
sys.modules['validation'], invalidating any references already captured
by previously-imported modules.
"""
from __future__ import annotations
