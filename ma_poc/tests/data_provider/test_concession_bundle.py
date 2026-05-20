"""Unit tests for SqlUnitStateStore._bundle_unit_concessions.

Verifies the DB-write-side bundle that lands in ``units.concessions``
(JSON column):

  * When v2 fields are emitted → bundled dict with full provenance.
  * When only legacy text key is emitted → still bundles (no data loss).
  * When NOTHING was captured → None (matches prior null contract).
  * Pre-bundled dict (carry-forward path) → passed through unchanged.

The bundle preserves raw + clean + structured + value + source — so a
re-read of the JSON column carries the full concession provenance
without re-scraping.
"""

from __future__ import annotations

from typing import Any

from ma_poc.data_provider.sql.stores import SqlUnitStateStore


class TestBundleUnitConcessions:
    def test_full_v2_unit_bundled(self) -> None:
        unit: dict[str, Any] = {
            "concession_text": "2 months free rent",
            "concession_text_clean": "2 months free rent",
            "_concession_quality": "clean",
            "concession_structured": {
                "type": "free_rent",
                "free_period": {"value": 2, "unit": "months"},
                "source": "API",
                "text": "2 months free rent",
            },
            "concession_value": 1500.0,
            "concession_source": "API",
        }
        bundle = SqlUnitStateStore._bundle_unit_concessions(unit)
        assert isinstance(bundle, dict)
        assert bundle["text"] == "2 months free rent"
        assert bundle["text_clean"] == "2 months free rent"
        assert bundle["quality"] == "clean"
        assert bundle["structured"]["type"] == "free_rent"
        assert bundle["value"] == 1500.0
        assert bundle["source"] == "API"

    def test_legacy_text_only_unit_bundled(self) -> None:
        """A unit emitting only the legacy ``concession`` string still
        gets a bundled dict with raw preserved."""
        unit = {"concession": "Some banner text"}
        bundle = SqlUnitStateStore._bundle_unit_concessions(unit)
        assert isinstance(bundle, dict)
        assert bundle["text"] == "Some banner text"
        assert bundle["text_clean"] is None
        assert bundle["quality"] is None
        assert bundle["structured"] is None

    def test_legacy_plural_concessions_key(self) -> None:
        unit = {"concessions": "Older banner"}
        bundle = SqlUnitStateStore._bundle_unit_concessions(unit)
        assert isinstance(bundle, dict)
        assert bundle["text"] == "Older banner"

    def test_no_concession_returns_none(self) -> None:
        unit = {"unit_id": "101", "rent_low": 1500}
        assert SqlUnitStateStore._bundle_unit_concessions(unit) is None

    def test_carry_forward_dict_passthrough(self) -> None:
        """A unit that already has a bundled dict (carry-forward path
        re-feeds the prior JSON column value) must not be double-wrapped."""
        prior_bundle = {
            "text": "1 month free",
            "text_clean": "1 month free",
            "quality": "clean",
            "structured": {"type": "free_rent"},
            "value": None,
            "source": "API",
        }
        unit = {"concession_text": prior_bundle}
        bundle = SqlUnitStateStore._bundle_unit_concessions(unit)
        # Passed through verbatim — same identity.
        assert bundle is prior_bundle

    def test_structured_none_but_text_present(self) -> None:
        """Raw-fallback invariant: when normalization fails, raw text
        is still bundled. Confirms the user-stated guarantee."""
        unit = {
            "concession_text": "Welcome to our community",
            "concession_text_clean": "Welcome to our community",
            "_concession_quality": "clean",
            "concession_structured": None,
        }
        bundle = SqlUnitStateStore._bundle_unit_concessions(unit)
        assert isinstance(bundle, dict)
        assert bundle["text"] == "Welcome to our community"
        assert bundle["structured"] is None

    def test_only_value_and_source(self) -> None:
        """Edge case: adapter emits value + source but no text. Bundle
        still populates (no data loss)."""
        unit = {"concession_value": 500.0, "concession_source": "DOM"}
        bundle = SqlUnitStateStore._bundle_unit_concessions(unit)
        assert isinstance(bundle, dict)
        assert bundle["text"] is None
        assert bundle["value"] == 500.0
        assert bundle["source"] == "DOM"
