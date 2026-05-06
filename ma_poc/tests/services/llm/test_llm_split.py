"""PR-6 — Tests for services/llm/ split and backward-compat shim.

These tests verify:
1. Each sub-module can be imported independently from ma_poc.services.llm.*
2. Backward compat: ma_poc.services.llm_extractor still exports all public names
3. apply_saved_mapping() works deterministically without any LLM call
"""

from __future__ import annotations

import importlib
import inspect
import pytest


# ── 1. Sub-module import tests ─────────────────────────────────────────────


def test_import_services_llm_package():
    """services.llm package is importable."""
    mod = importlib.import_module("ma_poc.services.llm")
    assert mod is not None


def test_import_services_llm_monolithic():
    """services.llm.monolithic is importable and exports extract_with_llm."""
    mod = importlib.import_module("ma_poc.services.llm.monolithic")
    assert hasattr(mod, "extract_with_llm")
    assert callable(mod.extract_with_llm)


def test_import_services_llm_api_analyzer():
    """services.llm.api_analyzer is importable and exports analyze_api_with_llm."""
    mod = importlib.import_module("ma_poc.services.llm.api_analyzer")
    assert hasattr(mod, "analyze_api_with_llm")
    assert callable(mod.analyze_api_with_llm)


def test_import_services_llm_dom_analyzer():
    """services.llm.dom_analyzer is importable and exports analyze_dom_with_llm."""
    mod = importlib.import_module("ma_poc.services.llm.dom_analyzer")
    assert hasattr(mod, "analyze_dom_with_llm")
    assert callable(mod.analyze_dom_with_llm)


def test_import_services_llm_mapping_replay():
    """services.llm.mapping_replay is importable and exports apply_saved_mapping."""
    mod = importlib.import_module("ma_poc.services.llm.mapping_replay")
    assert hasattr(mod, "apply_saved_mapping")
    assert callable(mod.apply_saved_mapping)


def test_import_services_llm_shared():
    """services.llm._shared is importable and exports _trim_html and _rank_api_responses."""
    mod = importlib.import_module("ma_poc.services.llm._shared")
    assert hasattr(mod, "_trim_html")
    assert hasattr(mod, "_rank_api_responses")


# ── 2. Backward compatibility via shim ────────────────────────────────────


def test_backward_compat_extract_with_llm():
    """from ma_poc.services.llm_extractor import extract_with_llm still works."""
    from ma_poc.services.llm_extractor import extract_with_llm  # noqa: F401

    assert callable(extract_with_llm)


def test_backward_compat_analyze_api_with_llm():
    """from ma_poc.services.llm_extractor import analyze_api_with_llm still works."""
    from ma_poc.services.llm_extractor import analyze_api_with_llm  # noqa: F401

    assert callable(analyze_api_with_llm)


def test_backward_compat_analyze_dom_with_llm():
    """from ma_poc.services.llm_extractor import analyze_dom_with_llm still works."""
    from ma_poc.services.llm_extractor import analyze_dom_with_llm  # noqa: F401

    assert callable(analyze_dom_with_llm)


def test_backward_compat_apply_saved_mapping():
    """from ma_poc.services.llm_extractor import apply_saved_mapping still works."""
    from ma_poc.services.llm_extractor import apply_saved_mapping  # noqa: F401

    assert callable(apply_saved_mapping)


def test_backward_compat_prepare_llm_input():
    """from ma_poc.services.llm_extractor import prepare_llm_input still works."""
    from ma_poc.services.llm_extractor import prepare_llm_input  # noqa: F401

    assert callable(prepare_llm_input)


def test_backward_compat_shim_module():
    """ma_poc.services.llm_extractor module itself is still importable."""
    mod = importlib.import_module("ma_poc.services.llm_extractor")
    # All key public names must be present
    for name in ("extract_with_llm", "analyze_api_with_llm", "analyze_dom_with_llm",
                  "apply_saved_mapping", "prepare_llm_input"):
        assert hasattr(mod, name), f"Missing {name} from llm_extractor shim"


# ── 3. apply_saved_mapping() deterministic tests (no LLM) ─────────────────


def test_apply_saved_mapping_basic():
    """apply_saved_mapping returns units from a well-formed mapping."""
    from ma_poc.services.llm.mapping_replay import apply_saved_mapping

    api_body = {
        "units": [
            {"unitId": "101", "planName": "Studio", "rentLow": 1500, "rentHigh": 1700, "beds": 0, "baths": 1.0},
            {"unitId": "202", "planName": "1BD", "rentLow": 1900, "rentHigh": 2100, "beds": 1, "baths": 1.0},
        ]
    }
    mapping = {
        "response_envelope": "units",
        "json_paths": {
            "unit_id": "unitId",
            "floor_plan_name": "planName",
            "rent_low": "rentLow",
            "rent_high": "rentHigh",
            "bedrooms": "beds",
            "bathrooms": "baths",
        },
    }

    result = apply_saved_mapping(api_body, mapping)
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["unit_id"] == "101"
    assert result[0]["market_rent_low"] == 1500.0
    assert result[1]["unit_id"] == "202"
    assert result[1]["bedrooms"] == 1


def test_apply_saved_mapping_empty_body():
    """apply_saved_mapping returns [] when body is empty."""
    from ma_poc.services.llm.mapping_replay import apply_saved_mapping

    result = apply_saved_mapping({}, {"response_envelope": "units", "json_paths": {"unit_id": "id"}})
    assert result == []


def test_apply_saved_mapping_no_json_paths():
    """apply_saved_mapping returns [] when json_paths is empty."""
    from ma_poc.services.llm.mapping_replay import apply_saved_mapping

    result = apply_saved_mapping({"units": [{"id": "1"}]}, {"response_envelope": "", "json_paths": {}})
    assert result == []


def test_apply_saved_mapping_bad_envelope():
    """apply_saved_mapping returns [] when envelope path doesn't exist."""
    from ma_poc.services.llm.mapping_replay import apply_saved_mapping

    result = apply_saved_mapping(
        {"data": []},
        {"response_envelope": "nonexistent.path", "json_paths": {"unit_id": "id"}},
    )
    assert result == []


def test_apply_saved_mapping_nested_envelope():
    """apply_saved_mapping follows dot-separated envelope paths."""
    from ma_poc.services.llm.mapping_replay import apply_saved_mapping

    api_body = {
        "response": {
            "data": {
                "apartments": [
                    {"id": "A1", "name": "1BR", "rent": 1800},
                ]
            }
        }
    }
    mapping = {
        "response_envelope": "response.data.apartments",
        "json_paths": {
            "unit_id": "id",
            "floor_plan_name": "name",
            "rent_low": "rent",
        },
    }
    result = apply_saved_mapping(api_body, mapping)
    assert len(result) == 1
    assert result[0]["unit_id"] == "A1"
    assert result[0]["market_rent_low"] == 1800.0


def test_apply_saved_mapping_via_shim():
    """apply_saved_mapping imported via the shim also works correctly."""
    from ma_poc.services.llm_extractor import apply_saved_mapping

    api_body = {"units": [{"unitId": "303", "rent": 2000}]}
    mapping = {
        "response_envelope": "units",
        "json_paths": {"unit_id": "unitId", "rent_low": "rent"},
    }
    result = apply_saved_mapping(api_body, mapping)
    assert len(result) == 1
    assert result[0]["unit_id"] == "303"


# ── 4. Module size guard ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "module_name",
    [
        "ma_poc.services.llm.monolithic",
        "ma_poc.services.llm.api_analyzer",
        "ma_poc.services.llm.dom_analyzer",
        "ma_poc.services.llm.mapping_replay",
        "ma_poc.services.llm._shared",
    ],
)
def test_module_under_400_lines(module_name: str):
    """Each sub-module must be under 400 lines."""
    mod = importlib.import_module(module_name)
    source_file = inspect.getfile(mod)
    with open(source_file) as f:
        line_count = sum(1 for _ in f)
    assert line_count < 400, f"{module_name} has {line_count} lines (max 400)"
