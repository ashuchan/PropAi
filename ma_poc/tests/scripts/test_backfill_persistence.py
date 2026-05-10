"""Tests for ``scripts/diagnostics/backfill_persistence.py``.

Exercises:
- URL extraction from API_ANALYSIS prompts (the `API URL: <url>` line).
- Mapping reconstruction from a synthetic llm_report payload (success +
  noise verdict + malformed JSON).
- Patch reconstruction from a synthetic field-recovery payload (high-conf
  + low-conf + invalid field_name + not_present source_path).
- End-to-end backfill with a synthetic shard directory written to tmp_path.
- Idempotency: running apply twice produces identical state.
- Tolerance: malformed file in the middle of a shard doesn't crash the run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Add scripts/diagnostics to sys.path so we can import the module under test.
_DIAG_DIR = Path(__file__).resolve().parents[2] / "scripts" / "diagnostics"
if str(_DIAG_DIR) not in sys.path:
    sys.path.insert(0, str(_DIAG_DIR))


@pytest.fixture
def bf():
    import backfill_persistence  # type: ignore[import-not-found]
    return backfill_persistence


# ── URL extraction ─────────────────────────────────────────────────────────


class TestExtractApiUrlFromPrompt:
    def test_clean_prompt(self, bf):
        prompt = "Some preamble\nAPI URL: https://api.example.com/v1/units?key=abc\nMore text"
        assert bf.extract_api_url_from_prompt(prompt) == "https://api.example.com/v1/units?key=abc"

    def test_lowercase_label(self, bf):
        prompt = "api url: https://example.com/x"
        assert bf.extract_api_url_from_prompt(prompt) == "https://example.com/x"

    def test_indented(self, bf):
        prompt = "  API URL:   https://example.com/x  "
        assert bf.extract_api_url_from_prompt(prompt) == "https://example.com/x"

    def test_missing_returns_none(self, bf):
        assert bf.extract_api_url_from_prompt("nothing relevant here") is None

    def test_empty_returns_none(self, bf):
        assert bf.extract_api_url_from_prompt("") is None
        assert bf.extract_api_url_from_prompt(None) is None  # type: ignore[arg-type]


# ── Mapping reconstruction ─────────────────────────────────────────────────


class TestReconstructMappingDicts:
    def test_full_mapping_reconstructed(self, bf):
        report = {
            "interactions": [
                {
                    "tier": "API_ANALYSIS",
                    "success": True,
                    "user_prompt": "Property: X\nAPI URL: https://api.example.com/units\nBody: …",
                    "raw_response": json.dumps({
                        "has_unit_data": True,
                        "units": [{"id": "U1"}],
                        "json_paths": {"rent": "data.rent", "id": "data.id"},
                        "response_envelope": "data.units",
                    }),
                },
            ],
        }
        mappings = bf.reconstruct_mapping_dicts(report)
        assert len(mappings) == 1
        m = mappings[0]
        assert m["api_url_pattern"] == "https://api.example.com/units"
        assert m["json_paths"] == {"rent": "data.rent", "id": "data.id"}
        assert m["response_envelope"] == "data.units"

    def test_degraded_mapping_reconstructed(self, bf):
        """PR 1's degraded persistence path: empty json_paths but non-empty
        envelope still produces a valid mapping_dict the writer accepts."""
        report = {
            "interactions": [
                {
                    "tier": "API_ANALYSIS",
                    "success": True,
                    "user_prompt": "API URL: https://api.example.com/u\n",
                    "raw_response": json.dumps({
                        "has_unit_data": True,
                        "units": [{"id": "U1"}],
                        "json_paths": {},
                        "response_envelope": "data.units",
                    }),
                },
            ],
        }
        mappings = bf.reconstruct_mapping_dicts(report)
        assert len(mappings) == 1
        assert mappings[0]["json_paths"] == {}
        assert mappings[0]["response_envelope"] == "data.units"

    def test_noise_verdict_skipped(self, bf):
        """has_unit_data=False is a noise verdict — handled by blocked_endpoints
        writer, not the mapping writer. Skip during mapping reconstruction."""
        report = {
            "interactions": [
                {
                    "tier": "API_ANALYSIS",
                    "success": True,
                    "user_prompt": "API URL: https://chat.example.com/config\n",
                    "raw_response": json.dumps({"has_unit_data": False, "noise_reason": "chatbot_config"}),
                },
            ],
        }
        assert bf.reconstruct_mapping_dicts(report) == []

    def test_failed_interaction_skipped(self, bf):
        report = {
            "interactions": [
                {"tier": "API_ANALYSIS", "success": False, "user_prompt": "API URL: x\n", "raw_response": ""},
            ],
        }
        assert bf.reconstruct_mapping_dicts(report) == []

    def test_non_api_analysis_tier_skipped(self, bf):
        report = {
            "interactions": [
                {"tier": "DOM_ANALYSIS", "success": True, "user_prompt": "API URL: x\n", "raw_response": "{}"},
            ],
        }
        assert bf.reconstruct_mapping_dicts(report) == []

    def test_malformed_raw_response_skipped(self, bf):
        report = {
            "interactions": [
                {"tier": "API_ANALYSIS", "success": True, "user_prompt": "API URL: x\n", "raw_response": "not json"},
            ],
        }
        assert bf.reconstruct_mapping_dicts(report) == []

    def test_url_missing_from_prompt_skipped(self, bf):
        report = {
            "interactions": [
                {
                    "tier": "API_ANALYSIS",
                    "success": True,
                    "user_prompt": "no URL line here",
                    "raw_response": json.dumps({"has_unit_data": True, "json_paths": {"r": "x"}}),
                },
            ],
        }
        assert bf.reconstruct_mapping_dicts(report) == []

    def test_both_paths_and_envelope_empty_skipped(self, bf):
        """Writer drops these as ``empty_paths_and_envelope``. Don't bother
        reconstructing — saves the writer a no-op call."""
        report = {
            "interactions": [
                {
                    "tier": "API_ANALYSIS",
                    "success": True,
                    "user_prompt": "API URL: https://x.example.com/u\n",
                    "raw_response": json.dumps({"has_unit_data": True, "json_paths": {}, "response_envelope": ""}),
                },
            ],
        }
        assert bf.reconstruct_mapping_dicts(report) == []

    def test_top_level_non_dict_tolerated(self, bf):
        """A malformed-but-valid JSON file that parses to a list/string/null
        at the top level must not crash the reconstruct walker — otherwise a
        single bad file aborts the entire shard run."""
        assert bf.reconstruct_mapping_dicts(["not", "a", "dict"]) == []
        assert bf.reconstruct_mapping_dicts("string at top level") == []
        assert bf.reconstruct_mapping_dicts(None) == []
        assert bf.reconstruct_mapping_dicts(42) == []

    def test_null_values_in_json_paths_filtered(self, bf):
        """The LLM occasionally emits ``"some_field": null`` for fields it
        couldn't map. The Pydantic ``LlmFieldMapping.json_paths: dict[str,str]``
        rejects None values, so the reconstructor must drop them rather than
        let the whole mapping die at writer-validation time. Discovered by
        smoke test against real cloud-run-2026-05-09."""
        report = {
            "interactions": [
                {
                    "tier": "API_ANALYSIS",
                    "success": True,
                    "user_prompt": "API URL: https://api.example.com/u\n",
                    "raw_response": json.dumps({
                        "has_unit_data": True,
                        "json_paths": {
                            "rent_low": "data.rent",
                            "move_in_date": None,        # filtered
                            "concession_value": None,    # filtered
                            "unit_id": "data.unit_id",
                            "garbage_path": "",          # filtered (empty)
                        },
                        "response_envelope": "data.units",
                    }),
                },
            ],
        }
        mappings = bf.reconstruct_mapping_dicts(report)
        assert len(mappings) == 1
        assert mappings[0]["json_paths"] == {"rent_low": "data.rent", "unit_id": "data.unit_id"}


# ── Patch reconstruction ───────────────────────────────────────────────────


class TestReconstructPatchDicts:
    def _make_recovery(self, fields: list[dict[str, Any]], url: str = "https://api.example.com/u") -> list[dict[str, Any]]:
        return [{
            "property_id": "P1",
            "tier_used": "TIER_1_API",
            "recovered_fields": fields,
            "_llm_interaction": {"user_prompt": f"API URL: {url}\n"},
        }]

    def test_high_confidence_patch_reconstructed(self, bf):
        recovery = self._make_recovery([
            {"field_name": "unit_id", "recovered_value": "U1", "confidence": 0.95, "source_path": "$.uuid", "parser_fix": "item.get('uuid')"},
        ])
        patches = bf.reconstruct_patch_dicts(recovery)
        assert len(patches) == 1
        p = patches[0]
        assert p["api_url_pattern"] == "https://api.example.com/u"
        assert p["field_name"] == "unit_id"
        assert p["json_path"] == "$.uuid"
        assert p["confidence"] == 0.95

    def test_low_confidence_skipped(self, bf):
        recovery = self._make_recovery([
            {"field_name": "rent_low", "recovered_value": 1500, "confidence": 0.5, "source_path": "$.rent"},
        ])
        assert bf.reconstruct_patch_dicts(recovery) == []

    def test_not_present_source_path_skipped(self, bf):
        recovery = self._make_recovery([
            {"field_name": "rent_low", "recovered_value": None, "confidence": 0.0, "source_path": "not_present"},
        ])
        assert bf.reconstruct_patch_dicts(recovery) == []

    def test_invalid_field_name_skipped(self, bf):
        """field_name not in FieldPatch's Literal — would fail Pydantic
        validation in the writer. Skip during reconstruction."""
        recovery = self._make_recovery([
            {"field_name": "totally_made_up_field", "recovered_value": "x", "confidence": 0.99, "source_path": "$.x"},
        ])
        assert bf.reconstruct_patch_dicts(recovery) == []

    def test_url_missing_skipped(self, bf):
        recovery = [{
            "property_id": "P1",
            "recovered_fields": [
                {"field_name": "unit_id", "confidence": 0.95, "source_path": "$.uuid"},
            ],
            "_llm_interaction": {"user_prompt": "no url line"},
        }]
        assert bf.reconstruct_patch_dicts(recovery) == []

    def test_multiple_fields_same_source(self, bf):
        recovery = self._make_recovery([
            {"field_name": "unit_id",       "confidence": 0.95, "source_path": "$.uuid"},
            {"field_name": "rent_low",      "confidence": 0.92, "source_path": "$.minRent"},
            {"field_name": "rent_high",     "confidence": 0.50, "source_path": "$.maxRent"},  # low conf — skipped
            {"field_name": "available_date","confidence": 0.0,  "source_path": "not_present"},  # skipped
        ])
        patches = bf.reconstruct_patch_dicts(recovery)
        assert len(patches) == 2
        assert {p["field_name"] for p in patches} == {"unit_id", "rent_low"}

    def test_malformed_payload_returns_empty(self, bf):
        assert bf.reconstruct_patch_dicts(None) == []
        assert bf.reconstruct_patch_dicts({"not": "a list"}) == []
        assert bf.reconstruct_patch_dicts([{"no_recovered_fields_key": True}]) == []


# ── End-to-end with synthetic shard layout ─────────────────────────────────


def _make_synthetic_shard(
    tmp_path: Path,
    pid: str,
    mapping_url: str | None,
    patch_url: str | None,
) -> Path:
    """Write one shard with ``llm_report/{pid}.json`` and
    ``llm_diagnostics/{pid}_field_recovery.json`` populated."""
    shard = tmp_path / "shard_0"
    (shard / "llm_report").mkdir(parents=True, exist_ok=True)
    (shard / "llm_diagnostics").mkdir(parents=True, exist_ok=True)
    if mapping_url:
        (shard / "llm_report" / f"{pid}.json").write_text(json.dumps({
            "interactions": [{
                "tier": "API_ANALYSIS",
                "success": True,
                "user_prompt": f"Property: X\nAPI URL: {mapping_url}\nBody: …",
                "raw_response": json.dumps({
                    "has_unit_data": True,
                    "units": [{"id": "U1"}],
                    "json_paths": {"rent": "data.rent"},
                    "response_envelope": "data.units",
                }),
            }],
        }), encoding="utf-8")
    if patch_url:
        (shard / "llm_diagnostics" / f"{pid}_field_recovery.json").write_text(json.dumps([{
            "property_id": pid,
            "tier_used": "TIER_1_API",
            "recovered_fields": [{
                "field_name": "unit_id",
                "recovered_value": "X1",
                "confidence": 0.95,
                "source_path": "$.uuid",
            }],
            "_llm_interaction": {"user_prompt": f"API URL: {patch_url}\n"},
        }]), encoding="utf-8")
    return shard


class TestRunBackfillEndToEnd:
    def test_dryrun_counts_without_writing(self, bf, tmp_path):
        run_dir = tmp_path / "run-2026-05-10"
        _make_synthetic_shard(
            run_dir, "P1",
            mapping_url="https://api.example.com/units",
            patch_url="https://api.example.com/units",
        )
        stats = bf.run_backfill(
            run_date="2026-05-10",
            local_mirror_root=tmp_path,
            apply_mode=False,
        )
        assert stats.shards_seen == ["shard_0"]
        assert stats.properties_visited == 1
        assert stats.mappings_attempted == 1
        assert stats.mappings_persisted == 1
        assert stats.patches_attempted == 1
        assert stats.patches_persisted == 1
        # Dry-run uses the NullStore — no writes happened, but counters
        # reflect what WOULD have written.

    def test_malformed_file_doesnt_crash(self, bf, tmp_path):
        run_dir = tmp_path / "run-2026-05-10"
        shard = _make_synthetic_shard(
            run_dir, "P1",
            mapping_url="https://api.example.com/u",
            patch_url=None,
        )
        # Inject a malformed file in the middle of the shard
        (shard / "llm_report" / "BAD.json").write_text("not json{", encoding="utf-8")

        stats = bf.run_backfill(
            run_date="2026-05-10",
            local_mirror_root=tmp_path,
            apply_mode=False,
        )
        assert stats.mapping_files_seen == 2
        assert stats.mapping_files_malformed == 1
        # The good file's mapping still made it through
        assert stats.mappings_persisted == 1

    def test_shard_filter(self, bf, tmp_path):
        run_dir = tmp_path / "run-2026-05-10"
        _make_synthetic_shard(run_dir, "P1", mapping_url="https://x/u", patch_url=None)
        # Add a second shard
        shard2 = run_dir / "shard_1"
        (shard2 / "llm_report").mkdir(parents=True)
        (shard2 / "llm_report" / "P2.json").write_text(json.dumps({
            "interactions": [{
                "tier": "API_ANALYSIS", "success": True,
                "user_prompt": "API URL: https://y/u\n",
                "raw_response": json.dumps({"has_unit_data": True, "json_paths": {"r": "x"}, "response_envelope": "u"}),
            }],
        }), encoding="utf-8")

        stats_all = bf.run_backfill(run_date="2026-05-10", local_mirror_root=tmp_path, apply_mode=False)
        assert stats_all.shards_seen == ["shard_0", "shard_1"]
        assert stats_all.properties_visited == 2

        stats_one = bf.run_backfill(run_date="2026-05-10", local_mirror_root=tmp_path, shard_filter=0, apply_mode=False)
        assert stats_one.shards_seen == ["shard_0"]
        assert stats_one.properties_visited == 1

    def test_missing_run_dir_raises(self, bf, tmp_path):
        with pytest.raises(FileNotFoundError):
            bf.run_backfill(run_date="2099-01-01", local_mirror_root=tmp_path, apply_mode=False)


# ── Report rendering ───────────────────────────────────────────────────────


class TestRenderReportMd:
    def test_dryrun_banner_present(self, bf):
        stats = bf.BackfillStats(run_date="2026-05-10", shards_seen=["shard_0"])
        out = bf.render_report_md(stats, apply_mode=False)
        assert "[DRY RUN]" in out

    def test_apply_no_dryrun_banner(self, bf):
        stats = bf.BackfillStats(run_date="2026-05-10", shards_seen=["shard_0"])
        out = bf.render_report_md(stats, apply_mode=True)
        assert "[DRY RUN]" not in out
        assert "APPLY (writes to DB)" in out

    def test_drop_reasons_table_renders(self, bf):
        from collections import Counter
        stats = bf.BackfillStats(
            run_date="2026-05-10",
            shards_seen=["shard_0"],
            mapping_drop_reasons=Counter({"writer_returned_false": 5}),
        )
        out = bf.render_report_md(stats, apply_mode=False)
        assert "writer_returned_false" in out
