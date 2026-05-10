"""URL pattern normalization for replay matching.

The replay matcher's substring compare requires saved patterns to remain
substrings of incoming URLs across runs. Query-param drift (api_key
rotation, session tokens, timestamp params) breaks that. ``normalize_url_pattern``
collapses both sides to ``host/path``.

Tests cover:
- All the drift shapes the LLM extractor writes into
  ``LlmFieldMapping.api_url_pattern`` in production: scheme, host case,
  default vs explicit port, query-string with rotated api_keys / session
  tokens, fragments, path-only inputs, empty/None.
- ``save_llm_field_mapping`` writes the normalized form regardless of the
  scheme/query the LLM emitted.
- The Sweetwater-FL real-data shape (``?api_key=<rotated>``) survives
  drift after the fix.
- Migration safety: pre-existing entries with raw URLs collapse correctly
  during upsert.

Reference: ``ma_poc/docs/url_pattern_normalization.md``.
"""

from __future__ import annotations

import pytest

from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.services.profile_updater import (
    normalize_url_pattern,
    save_llm_field_mapping,
)

# ── normalize_url_pattern ──────────────────────────────────────────────────


class TestNormalizeUrlPattern:
    def test_strips_scheme(self) -> None:
        assert normalize_url_pattern("https://api.example.com/units") == "api.example.com/units"
        assert normalize_url_pattern("http://api.example.com/units") == "api.example.com/units"

    def test_strips_query(self) -> None:
        assert normalize_url_pattern("https://api.example.com/units?session=ABC") == "api.example.com/units"
        assert normalize_url_pattern("https://api.example.com/units?a=1&b=2") == "api.example.com/units"

    def test_strips_fragment(self) -> None:
        assert normalize_url_pattern("https://api.example.com/units#section") == "api.example.com/units"

    def test_strips_port(self) -> None:
        # Both default and non-default ports drop — replay treats them as
        # the same endpoint when path matches.
        assert normalize_url_pattern("https://api.example.com:443/units") == "api.example.com/units"
        assert normalize_url_pattern("https://api.example.com:8080/units") == "api.example.com/units"

    def test_lowercases_host(self) -> None:
        assert normalize_url_pattern("https://API.Example.COM/units") == "api.example.com/units"

    def test_preserves_path_case(self) -> None:
        # Paths can be case-sensitive (e.g. AWS S3 keys); don't lowercase.
        assert normalize_url_pattern("https://api.example.com/Units/ABC") == "api.example.com/Units/ABC"

    def test_handles_path_only_input(self) -> None:
        assert normalize_url_pattern("/api/v3/units") == "/api/v3/units"

    def test_handles_empty(self) -> None:
        assert normalize_url_pattern("") == ""
        assert normalize_url_pattern("   ") == ""

    def test_idempotent(self) -> None:
        once = normalize_url_pattern("https://api.example.com/units?x=1")
        twice = normalize_url_pattern(once)
        assert once == twice

    def test_sweetwater_real_data(self) -> None:
        """The actual Sweetwater FL pattern from production DB. Saved with
        api_key=2c704f12... in the URL; new run captures the same endpoint
        with a rotated key. After PR 5 normalisation both collapse to the
        same form."""
        saved_url = "https://www.liveatsweetwaterfl.com/api/v3/floorplans/all/?api_key=2c704f12feb4f8613760dda184bd08d7e37785e4"
        new_run_url = "https://www.liveatsweetwaterfl.com/api/v3/floorplans/all/?api_key=DIFFERENT_ROTATED_VALUE"
        assert normalize_url_pattern(saved_url) == "www.liveatsweetwaterfl.com/api/v3/floorplans/all/"
        assert normalize_url_pattern(new_run_url) == "www.liveatsweetwaterfl.com/api/v3/floorplans/all/"
        # And the substring match the matcher does now succeeds:
        norm_pat = normalize_url_pattern(saved_url)
        norm_new = normalize_url_pattern(new_run_url)
        assert norm_pat in norm_new


# ── Writer integration ─────────────────────────────────────────────────────


@pytest.fixture
def profile() -> ScrapeProfile:
    return ScrapeProfile(canonical_id="test-pr5")


class TestWriterNormalisation:
    def test_save_strips_scheme(self, profile: ScrapeProfile) -> None:
        ok = save_llm_field_mapping(profile, {
            "api_url_pattern": "https://api.example.com/units",
            "json_paths": {"rent": "data.rent"},
            "response_envelope": "data",
        })
        assert ok
        assert profile.api_hints.llm_field_mappings[0].api_url_pattern == "api.example.com/units"

    def test_save_strips_query(self, profile: ScrapeProfile) -> None:
        ok = save_llm_field_mapping(profile, {
            "api_url_pattern": "https://api.example.com/units?session=ABC&t=12345",
            "json_paths": {"rent": "data.rent"},
            "response_envelope": "data",
        })
        assert ok
        m = profile.api_hints.llm_field_mappings[0]
        assert m.api_url_pattern == "api.example.com/units"
        assert "?" not in m.api_url_pattern
        assert "session" not in m.api_url_pattern

    def test_upsert_collapses_drifted_query_strings(self, profile: ScrapeProfile) -> None:
        """Two saves with the same logical endpoint but different api_keys
        must upsert to ONE entry, not append two. Pre-PR-5 the writer's
        equality check ``existing.api_url_pattern == url_pattern`` was on
        the raw URLs which differed by query string → two entries → cache
        bloat. Post-PR-5 both normalize to the same key → one entry."""
        save_llm_field_mapping(profile, {
            "api_url_pattern": "https://api.example.com/units?api_key=OLD",
            "json_paths": {"rent": "data.rent"},
            "response_envelope": "data",
        })
        save_llm_field_mapping(profile, {
            "api_url_pattern": "https://api.example.com/units?api_key=NEW",
            "json_paths": {"rent": "data.new_rent"},
            "response_envelope": "data",
        })
        # Single entry — the second save upserted via the (now-canonical) key.
        assert len(profile.api_hints.llm_field_mappings) == 1
        # Latest save's json_paths wins.
        assert profile.api_hints.llm_field_mappings[0].json_paths == {"rent": "data.new_rent"}

    def test_upsert_against_pre_pr5_raw_url_entry(self, profile: ScrapeProfile) -> None:
        """Migration safety: a pre-PR-5 entry has the raw URL stored. A
        post-PR-5 save for the same logical endpoint must collapse to ONE
        entry (upsert), not append a new one. Without normalising
        ``existing.api_url_pattern`` in the equality check, the profile
        would gradually accumulate duplicate entries until the cache rotates.

        Also asserts the existing entry's ``api_url_pattern`` is rewritten
        to the canonical form so all downstream readers see one shape.
        """
        from ma_poc.models.scrape_profile import LlmFieldMapping

        # Inject a pre-PR-5-shaped entry directly (simulates DB state before
        # the writer normalised at write time).
        profile.api_hints.llm_field_mappings.append(LlmFieldMapping(
            api_url_pattern="https://api.example.com/units?api_key=OLD_RAW_URL",
            json_paths={"rent": "old.path"},
            response_envelope="data",
        ))
        assert len(profile.api_hints.llm_field_mappings) == 1

        # New save for the same logical endpoint, post-PR-5 (normalised input).
        ok = save_llm_field_mapping(profile, {
            "api_url_pattern": "https://api.example.com/units?api_key=NEW_VALUE",
            "json_paths": {"rent": "new.path"},
            "response_envelope": "data",
        })
        assert ok
        # Single entry — old one upserted in place.
        assert len(profile.api_hints.llm_field_mappings) == 1
        # Pattern rewritten to canonical form.
        assert profile.api_hints.llm_field_mappings[0].api_url_pattern == "api.example.com/units"
        # New json_paths win.
        assert profile.api_hints.llm_field_mappings[0].json_paths == {"rent": "new.path"}

    def test_save_drops_pure_garbage(self, profile: ScrapeProfile) -> None:
        """Empty URL still drops — normalisation produces empty, writer
        drops with reason ``empty_pattern``."""
        ok = save_llm_field_mapping(profile, {
            "api_url_pattern": "",
            "json_paths": {"rent": "data.rent"},
        })
        assert ok is False
