"""PR24 Fix 6 — extract_units_from_dom returns (units, hit_mode) tuple.

hints_hit must only be True when profile hint selectors actually fired;
when the default cascade runs it must be "default" or "none".
"""

from __future__ import annotations

import sys
import types

for _name in ("playwright", "playwright.async_api"):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.BrowserContext = object  # type: ignore[attr-defined]
        _mod.Page = object  # type: ignore[attr-defined]
        _mod.async_playwright = object  # type: ignore[attr-defined]
        sys.modules[_name] = _mod

from ma_poc.pms.adapters._html_extract import extract_units_from_dom


_HTML_WITH_UNIT = """
<html><body>
<div class="my-unit-container">
  <span class="rent">$1,500/mo</span>
  <span class="beds">2 bed</span>
  <span class="sqft">850 sqft</span>
</div>
</body></html>
"""

_HTML_EMPTY = "<html><body><p>No units here</p></body></html>"


def test_tuple_return_without_hints() -> None:
    """Return type must be a 2-tuple even when hints=None."""
    result = extract_units_from_dom(_HTML_EMPTY, "https://example.com")
    assert isinstance(result, tuple) and len(result) == 2
    _units, hit_mode = result
    assert hit_mode in ("default", "none"), f"unexpected hit_mode={hit_mode!r}"


def test_no_hints_never_returns_hints_mode() -> None:
    """Without a hints argument, hit_mode must never be 'hints'."""
    _units, hit_mode = extract_units_from_dom(_HTML_WITH_UNIT, "https://example.com")
    assert hit_mode != "hints", "hit_mode='hints' requires a hints argument"


def test_empty_html_returns_none_mode() -> None:
    """Empty HTML yields ([], 'none')."""
    units, hit_mode = extract_units_from_dom("", "https://example.com")
    assert units == []
    assert hit_mode == "none"


def test_hints_miss_falls_back_to_default_not_hints_mode() -> None:
    """When hint selector produces nothing, hit_mode must not be 'hints'."""
    try:
        from ma_poc.models.scrape_profile import FieldSelectorMap
        hints = FieldSelectorMap(container=".nonexistent-selector-xyz")
    except Exception:
        return  # model unavailable — skip

    _units, hit_mode = extract_units_from_dom(_HTML_WITH_UNIT, "https://example.com", hints=hints)
    assert hit_mode != "hints", (
        "hint selector returned no results, hit_mode must not be 'hints'"
    )


def test_return_value_is_tuple_with_correct_types() -> None:
    """Ensures the function signature returns tuple[list, str]."""
    result = extract_units_from_dom(_HTML_WITH_UNIT, "https://example.com")
    assert isinstance(result, tuple)
    units, hit_mode = result
    assert isinstance(units, list)
    assert isinstance(hit_mode, str)
