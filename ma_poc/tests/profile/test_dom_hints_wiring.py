"""Phase 8 — DOM hints wiring + drift eviction tests."""

from __future__ import annotations

import pytest

from ma_poc.models.scrape_profile import FieldSelectorMap
from ma_poc.pms.adapters._html_extract import extract_units_from_dom, extract_with_hints


_HTML_SAMPLE = """
<html><body>
<div class="my-special-unit">
  <div>Unit 305 — 1 bed, 1 bath, 750 sqft</div>
  <div>$1,500/mo</div>
</div>
<div class="my-special-unit">
  <div>Unit 410 — 2 bed, 2 bath, 1100 sqft</div>
  <div>$2,200/mo</div>
</div>
</body></html>
"""

_HTML_NO_HINT_MATCH = """
<html><body>
<div class="standard-rentcafe-unit">
  <div>Unit 101 — Studio, 500 sqft</div>
  <div>$1,200/mo</div>
</div>
</body></html>
"""


def test_hint_hit_extracts_units() -> None:
    hints = FieldSelectorMap(container=".my-special-unit")
    units = extract_with_hints(_HTML_SAMPLE, "https://x.com", hints)
    assert len(units) == 2


def test_hint_miss_returns_empty() -> None:
    hints = FieldSelectorMap(container=".not-present")
    units = extract_with_hints(_HTML_SAMPLE, "https://x.com", hints)
    assert units == []


def test_extract_units_from_dom_back_compat_without_hints() -> None:
    """Without hints, returns (units, hit_mode) tuple."""
    units, hit_mode = extract_units_from_dom(_HTML_SAMPLE, "https://x.com")
    assert isinstance(units, list)
    assert hit_mode in ("default", "none")


def test_extract_units_from_dom_with_hints_falls_back_on_miss() -> None:
    """When hints don't match, default cascade runs and hit_mode is not 'hints'."""
    hints = FieldSelectorMap(container=".not-present")
    units, hit_mode = extract_units_from_dom(_HTML_NO_HINT_MATCH, "https://x.com", hints=hints)
    assert isinstance(units, list)
    assert hit_mode != "hints"


def test_extract_units_from_dom_hints_hit_mode() -> None:
    """When hints match, hit_mode is 'hints'."""
    hints = FieldSelectorMap(container=".my-special-unit")
    units, hit_mode = extract_units_from_dom(_HTML_SAMPLE, "https://x.com", hints=hints)
    assert len(units) == 2
    assert hit_mode == "hints"


def test_extract_with_hints_no_container_returns_empty() -> None:
    hints = FieldSelectorMap()  # no container set
    units = extract_with_hints(_HTML_SAMPLE, "https://x.com", hints)
    assert units == []


def test_dom_hints_consecutive_misses_field_exists() -> None:
    """The ScrapeProfile.dom_hints model has the new consecutive_misses field."""
    from ma_poc.models.scrape_profile import ScrapeProfile
    p = ScrapeProfile(canonical_id="phase8-001")
    assert hasattr(p.dom_hints, "consecutive_misses")
    assert p.dom_hints.consecutive_misses == 0
    p.dom_hints.consecutive_misses = 2
    # round-trip via model_dump/model_validate
    data = p.model_dump()
    p2 = ScrapeProfile.model_validate(data)
    assert p2.dom_hints.consecutive_misses == 2
