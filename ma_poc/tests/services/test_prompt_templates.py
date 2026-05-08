"""Phase 2 gate: shape and content invariants on the four prompt templates.

These tests assert the structural promises the templates make to the rest
of the system — known-floor-plan injection, amenities/concession blocks,
per-field instruction discipline — without coupling to the exact
wording. Update both the template and the assertion list together when
adding a new field.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "prompts"

# Files that use the single-brace placeholder convention ({key}).
_SINGLE_BRACE_PROMPTS: tuple[str, ...] = (
    "tier4_extraction.txt",
    "api_analysis.txt",
    "dom_analysis.txt",
)
# Files that use the double-brace placeholder convention ({{key}}).
_DOUBLE_BRACE_PROMPTS: tuple[str, ...] = ("api_rescue.txt",)
ALL_PROMPTS: tuple[str, ...] = _SINGLE_BRACE_PROMPTS + _DOUBLE_BRACE_PROMPTS


def _read(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


# ── Phase 2 structural assertions ────────────────────────────────────────────


@pytest.mark.parametrize("prompt", _SINGLE_BRACE_PROMPTS)
def test_single_brace_prompts_have_known_plans_placeholder(prompt: str) -> None:
    assert "{known_floor_plans}" in _read(prompt), (
        f"{prompt} must include the {{known_floor_plans}} placeholder so the "
        "FloorplanCatalog block can be substituted at runtime."
    )


def test_rescue_prompt_has_known_plans_placeholder() -> None:
    text = _read("api_rescue.txt")
    assert "{{known_floor_plans}}" in text, (
        "api_rescue.txt uses {{double-brace}} placeholders; the known-plans "
        "block must follow the same convention."
    )


@pytest.mark.parametrize("prompt", ALL_PROMPTS)
def test_all_prompts_have_amenities_block(prompt: str) -> None:
    text = _read(prompt)
    assert "AMENITIES" in text, f"{prompt} must include an AMENITIES block."
    # Sanity: the block must mention the array output and the lowercase rule.
    assert "lowercase" in text.lower(), f"{prompt} amenities block must reference lowercase normalisation."


@pytest.mark.parametrize("prompt", ALL_PROMPTS)
def test_all_prompts_have_concessions_block(prompt: str) -> None:
    text = _read(prompt)
    assert "CONCESSIONS" in text, f"{prompt} must include a CONCESSIONS block."
    assert "concession_text" in text and "concession_value" in text, (
        f"{prompt} concessions block must reference both concession_text and "
        "concession_value output fields."
    )
    assert "concession_source" in text, (
        f"{prompt} concessions block must reference concession_source so the "
        "report can break extractions down by where they came from."
    )


@pytest.mark.parametrize("prompt", ALL_PROMPTS)
def test_all_prompts_emit_availability_count_field(prompt: str) -> None:
    """availability_count is the floor-plan-grain marker used by the merge cascade."""
    assert "availability_count" in _read(prompt)


@pytest.mark.parametrize("prompt", ALL_PROMPTS)
def test_all_prompts_emit_floor_plan_source_field(prompt: str) -> None:
    """floor_plan_source is the audit signal showing how the name was derived."""
    assert "floor_plan_source" in _read(prompt)


@pytest.mark.parametrize("prompt", _SINGLE_BRACE_PROMPTS)
def test_per_field_instruction_blocks_present(prompt: str) -> None:
    """Per-field instructions must call out each high-stakes field by name."""
    text = _read(prompt)
    required_field_callouts = (
        "floor_plan_name",
        "bedrooms",
        "bathrooms",
        "sqft",
        "market_rent_low",
        "availability_status",
        "availability_count",
    )
    for field in required_field_callouts:
        assert text.count(field) >= 2, (
            f"{prompt} should reference {field} at least twice (output schema "
            "and per-field instruction). Update the per-field block when a "
            "new field is added."
        )


# ── _resolve_known_plans_block plumbing ─────────────────────────────────────


def test_known_plans_renders_when_catalog_has_property(tmp_path: Path) -> None:
    """The llm_extractor helper substitutes the block when a catalog is loaded."""
    from ma_poc.services import floorplan_catalog as fc
    from ma_poc.services.llm_extractor import _resolve_known_plans_block

    csv_path = tmp_path / "Floorplan-comparisons.csv"
    csv_path.write_text(
        "apartmentid,floorplannumber,description,bed,bath,area\n"
        "P1,1,Aspen,1,1,750\n",
        encoding="utf-8",
    )
    map_path = tmp_path / "csv_floorplan_mapping.json"
    map_path.write_text(json.dumps({"chosen_field": "apartmentid"}), encoding="utf-8")

    fc.reset_default_catalog()
    fc._default_catalog = fc.FloorplanCatalog(csv_path=csv_path, mapping_path=map_path)
    try:
        text = _resolve_known_plans_block({"property_id": "P1"})
        assert "KNOWN FLOOR PLANS" in text
        assert "Aspen" in text
    finally:
        fc.reset_default_catalog()


def test_known_plans_empty_when_property_unknown(tmp_path: Path) -> None:
    from ma_poc.services import floorplan_catalog as fc
    from ma_poc.services.llm_extractor import _resolve_known_plans_block

    csv_path = tmp_path / "Floorplan-comparisons.csv"
    csv_path.write_text(
        "apartmentid,floorplannumber,description,bed,bath,area\n"
        "P1,1,Aspen,1,1,750\n",
        encoding="utf-8",
    )
    map_path = tmp_path / "csv_floorplan_mapping.json"
    map_path.write_text(json.dumps({"chosen_field": "apartmentid"}), encoding="utf-8")

    fc.reset_default_catalog()
    fc._default_catalog = fc.FloorplanCatalog(csv_path=csv_path, mapping_path=map_path)
    try:
        # Property not in catalog → empty block (H6/H11 corner case).
        assert _resolve_known_plans_block({"property_id": "OTHER"}) == ""
    finally:
        fc.reset_default_catalog()


def test_known_plans_size_capped(tmp_path: Path) -> None:
    """H11: the rendered block stays within the prompt size budget."""
    from ma_poc.services.floorplan_catalog import (
        KNOWN_PLANS_PROMPT_LIMIT,
        FloorplanCatalog,
    )

    csv_path = tmp_path / "Floorplan-comparisons.csv"
    rows = ["apartmentid,floorplannumber,description,bed,bath,area"]
    for i in range(192):  # the spec calls out 192 as the CSV maximum
        rows.append(f"P9,{i},Plan{i},{i % 4},{1 + (i % 2) * 0.5},{600 + i}")
    csv_path.write_text("\n".join(rows), encoding="utf-8")
    map_path = tmp_path / "csv_floorplan_mapping.json"
    map_path.write_text(json.dumps({"chosen_field": "apartmentid"}), encoding="utf-8")

    cat = FloorplanCatalog(csv_path=csv_path, mapping_path=map_path)
    text, meta = cat.known_plans_block("P9")
    assert meta["plan_count_emitted"] == KNOWN_PLANS_PROMPT_LIMIT
    assert len(text.encode("utf-8")) <= 4096, (
        f"Known-plans block exceeded 4 KB (got {len(text.encode('utf-8'))} bytes); "
        "tighten KNOWN_PLANS_PROMPT_LIMIT or shorten the descriptor format."
    )


def test_known_plans_truncation_logged(tmp_path: Path, caplog) -> None:
    import logging

    from ma_poc.services.floorplan_catalog import (
        KNOWN_PLANS_PROMPT_LIMIT,
        FloorplanCatalog,
    )

    csv_path = tmp_path / "Floorplan-comparisons.csv"
    rows = ["apartmentid,floorplannumber,description,bed,bath,area"]
    for i in range(KNOWN_PLANS_PROMPT_LIMIT + 3):
        rows.append(f"P9,{i},Plan{i},{i % 4},{1 + (i % 2) * 0.5},{600 + i}")
    csv_path.write_text("\n".join(rows), encoding="utf-8")
    map_path = tmp_path / "csv_floorplan_mapping.json"
    map_path.write_text(json.dumps({"chosen_field": "apartmentid"}), encoding="utf-8")

    cat = FloorplanCatalog(csv_path=csv_path, mapping_path=map_path)
    with caplog.at_level(logging.INFO, logger="ma_poc.services.floorplan_catalog"):
        cat.known_plans_block("P9")
    assert any("truncated known-plans" in rec.message for rec in caplog.records)
