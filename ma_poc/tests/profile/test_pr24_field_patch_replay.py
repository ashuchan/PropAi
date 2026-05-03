"""PR24 Fix 7 — sub-tier 0b: _apply_field_patches positional join."""

from __future__ import annotations

import sys
import types

import pytest


def _stub_playwright() -> None:
    for name in ("playwright", "playwright.async_api"):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            mod.BrowserContext = object  # type: ignore[attr-defined]
            mod.Page = object  # type: ignore[attr-defined]
            mod.async_playwright = object  # type: ignore[attr-defined]
            sys.modules[name] = mod


_stub_playwright()
from ma_poc.pms.adapters.generic import _apply_field_patches  # noqa: E402


class _Patch:
    def __init__(self, url_pattern: str, field_name: str, json_path: str) -> None:
        self.api_url_pattern = url_pattern
        self.field_name = field_name
        self.json_path = json_path


def test_positional_fill_adds_missing_field() -> None:
    """Values from the patch body are joined positionally onto units."""
    units = [{"unit_id": "U1"}, {"unit_id": "U2"}]
    api_responses = [
        {
            "url": "https://example.com/api/availability",
            "body": {"rents": [1500, 1700]},
        }
    ]
    patches = [_Patch("api/availability", "asking_rent", "rents")]
    result = _apply_field_patches(units, api_responses, patches)
    assert result[0]["asking_rent"] == 1500
    assert result[1]["asking_rent"] == 1700


def test_existing_field_not_overwritten() -> None:
    """_apply_field_patches must never overwrite a non-null existing field."""
    units = [{"unit_id": "U1", "asking_rent": 9999}]
    api_responses = [
        {"url": "https://example.com/api/rents", "body": {"rents": [1500]}}
    ]
    patches = [_Patch("api/rents", "asking_rent", "rents")]
    result = _apply_field_patches(units, api_responses, patches)
    assert result[0]["asking_rent"] == 9999, "existing field must not be overwritten"


def test_empty_units_returns_unchanged() -> None:
    """Empty units list is returned as-is."""
    result = _apply_field_patches([], [{"url": "x", "body": {"v": [1]}}], [_Patch("x", "f", "v")])
    assert result == []


def test_no_matching_url_pattern_skips_patch() -> None:
    """When api_url_pattern doesn't match any response, unit is unchanged."""
    units = [{"unit_id": "U1"}]
    api_responses = [{"url": "https://other.com/api/units", "body": {"rents": [1500]}}]
    patches = [_Patch("api/availability", "asking_rent", "rents")]
    result = _apply_field_patches(units, api_responses, patches)
    assert "asking_rent" not in result[0]


def test_nested_json_path() -> None:
    """Dot-separated JSON path correctly navigates nested objects."""
    units = [{"unit_id": "U1"}]
    api_responses = [
        {"url": "https://example.com/api/data", "body": {"data": {"values": [2000]}}}
    ]
    patches = [_Patch("api/data", "asking_rent", "data.values")]
    result = _apply_field_patches(units, api_responses, patches)
    assert result[0]["asking_rent"] == 2000


def test_more_units_than_patch_values_safe() -> None:
    """When there are fewer patch values than units, extra units are unchanged."""
    units = [{"unit_id": "U1"}, {"unit_id": "U2"}, {"unit_id": "U3"}]
    api_responses = [{"url": "x.com/api", "body": {"rents": [1200]}}]
    patches = [_Patch("x.com/api", "asking_rent", "rents")]
    result = _apply_field_patches(units, api_responses, patches)
    assert result[0]["asking_rent"] == 1200
    assert "asking_rent" not in result[1]
    assert "asking_rent" not in result[2]
