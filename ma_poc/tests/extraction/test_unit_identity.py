"""Direct unit tests for ``ma_poc.extraction.unit_identity``.

The module centralises two contracts that ~13 merge call sites depend on:

  * ``canonical_unit_key`` — apartment-number-first identity resolution.
    Refuses inferred-only uids so callers can't silently bucket on a
    synthetic hash and destructively collapse distinct apartments.
  * ``merge_units`` — provenance-safe field merge. Never inherits the
    ``_inferred_id`` flag from secondary; promotes a real ``unit_id``
    onto an inferred-only primary when secondary carries one;
    reconciles ``_date_placeholder`` with the resulting date state.

These tests pin the contracts at the helper level so future refactors
of the call sites (post_process cross-page dedup, source_merger,
state_store cross-run dedup) can't silently regress them.
"""

from __future__ import annotations

from ma_poc.extraction.unit_identity import canonical_unit_key, merge_units


# ── canonical_unit_key ────────────────────────────────────────────────────────


class TestCanonicalUnitKey:
    def test_prefers_unit_number_over_real_uid(self):
        """Apartment number is the natural per-unit key — beats unit_id."""
        u = {"unit_number": "618", "unit_id": "abc-real-id"}
        key = canonical_unit_key(u)
        assert key == ("", "618")

    def test_normalises_unit_number_prefix(self):
        """``Apt 618`` and ``#618`` and ``618`` all collapse to the same key."""
        assert canonical_unit_key({"unit_number": "Apt 618"}) == ("", "618")
        assert canonical_unit_key({"unit_number": "#618"}) == ("", "618")
        assert canonical_unit_key({"unit_number": "618"}) == ("", "618")

    def test_falls_back_to_real_uid_when_no_unit_number(self):
        """No apartment number → use real (non-inferred) unit_id."""
        key = canonical_unit_key({"unit_id": "abc-real-id"})
        assert key == ("", "abc-real-id")

    def test_returns_none_for_inferred_only_uid(self):
        """Inferred ``unit_id`` is a synthetic hash — caller MUST NOT bucket on it."""
        assert canonical_unit_key({"unit_id": "inferred_abc123"}) is None

    def test_returns_none_for_explicit_inferred_flag(self):
        """``_inferred_id=True`` is the in-band marker for synthetic identity."""
        assert canonical_unit_key({"unit_id": "abc", "_inferred_id": True}) is None

    def test_returns_none_for_empty_row(self):
        assert canonical_unit_key({}) is None
        assert canonical_unit_key({"_inferred_id": True}) is None

    def test_uses_building_to_scope_identity(self):
        """Townhome complexes re-use unit numbers across buildings —
        building must be in the key so they don't collide."""
        a = canonical_unit_key({"unit_number": "1", "building": "A"})
        b = canonical_unit_key({"unit_number": "1", "building": "B"})
        assert a == ("a", "1")
        assert b == ("b", "1")
        assert a != b

    def test_pascalcase_building_alias_admitted(self):
        """``BuildingName`` (PascalCase) is admitted via the alias table."""
        key = canonical_unit_key({"unit_number": "1", "BuildingName": "Tower A"})
        assert key == ("tower a", "1")

    def test_pascalcase_unit_number_alias_admitted(self):
        """``UnitNumber`` (PascalCase) is admitted via the alias table."""
        # extract_unit_number builds a case-insensitive lc_view, so
        # PascalCase keys collapse to the same alias.
        key = canonical_unit_key({"UnitNumber": "618"})
        assert key == ("", "618")

    def test_building_context_arg_used_only_when_row_lacks_building(self):
        # Row has a building → ignore context.
        key1 = canonical_unit_key(
            {"unit_number": "1", "building": "A"}, building_context="B"
        )
        assert key1 == ("a", "1")
        # Row has no building → fall back to context.
        key2 = canonical_unit_key(
            {"unit_number": "1"}, building_context="ctx"
        )
        assert key2 == ("ctx", "1")

    def test_returns_none_for_non_dict(self):
        assert canonical_unit_key(None) is None  # type: ignore[arg-type]
        assert canonical_unit_key("string") is None  # type: ignore[arg-type]
        assert canonical_unit_key([]) is None  # type: ignore[arg-type]


# ── merge_units — provenance + identity promotion ─────────────────────────────


class TestMergeUnitsIdentityPromotion:
    def test_promotes_real_uid_over_inferred_primary(self):
        """Real uid from secondary replaces primary's inferred hash."""
        primary = {
            "unit_id": "inferred_abc123",
            "_inferred_id": True,
            "unit_number": "618",
            "beds": 1,
        }
        secondary = {"unit_id": "real-618", "rent_low": 1500}
        result = merge_units(primary, secondary)
        assert result["unit_id"] == "real-618"
        assert result["_inferred_id"] is False
        # Field-level fill still picked up rent_low from secondary.
        assert result["rent_low"] == 1500

    def test_real_primary_keeps_its_real_uid(self):
        """Primary with real uid is never overwritten by secondary."""
        primary = {"unit_id": "real-618", "rent_low": 1500}
        secondary = {"unit_id": "real-619", "beds": 2}
        result = merge_units(primary, secondary)
        # Primary wins — uid stays.
        assert result["unit_id"] == "real-618"
        # Field-level fill picks up missing keys.
        assert result["beds"] == 2

    def test_inferred_primary_with_inferred_secondary_no_promotion(self):
        """Both sides synthetic → no promotion, primary's uid stays."""
        primary = {"unit_id": "inferred_a", "_inferred_id": True}
        secondary = {"unit_id": "inferred_b", "_inferred_id": True}
        result = merge_units(primary, secondary)
        assert result["unit_id"] == "inferred_a"
        # _inferred_id NOT inherited from secondary even though both are True
        # — primary's value wins, never overwritten.
        assert result["_inferred_id"] is True


class TestMergeUnitsProvenanceProtection:
    def test_never_inherits_inferred_id_flag(self):
        """Primary with real uid must not inherit _inferred_id=True from secondary."""
        primary = {"unit_id": "real-618", "rent_low": 1500}
        secondary = {"_inferred_id": True, "beds": 1}
        result = merge_units(primary, secondary)
        assert result.get("_inferred_id") is not True
        # Field-level fill still picks up legitimate fields.
        assert result["beds"] == 1

    def test_skips_unit_id_in_field_level_loop(self):
        """unit_id is handled by identity-promotion; field loop ignores it."""
        # Primary has real uid; secondary has different real uid.
        primary = {"unit_id": "real-618"}
        secondary = {"unit_id": "real-999", "rent_low": 1500}
        result = merge_units(primary, secondary)
        # Field loop did NOT copy secondary's unit_id over primary's.
        assert result["unit_id"] == "real-618"

    def test_returns_primary_object(self):
        """merge_units returns the mutated primary, not a new dict."""
        primary = {"unit_number": "618"}
        secondary = {"beds": 1}
        result = merge_units(primary, secondary)
        assert result is primary

    def test_does_not_mutate_secondary(self):
        secondary = {"unit_id": "real-id", "beds": 1, "_inferred_id": True}
        secondary_copy = dict(secondary)
        merge_units({"unit_number": "1"}, secondary)
        assert secondary == secondary_copy


class TestMergeUnitsFieldFill:
    def test_primary_wins_on_present_fields(self):
        primary = {"rent_low": 1500, "beds": 1}
        secondary = {"rent_low": 1700, "beds": 2, "baths": 1}
        result = merge_units(primary, secondary)
        assert result["rent_low"] == 1500
        assert result["beds"] == 1
        # Absent fields filled from secondary.
        assert result["baths"] == 1

    def test_absent_fields_filled_from_secondary(self):
        primary = {"unit_number": "618"}
        secondary = {"beds": 2, "baths": 1, "area": 800, "rent_low": 1500}
        result = merge_units(primary, secondary)
        assert result["beds"] == 2
        assert result["baths"] == 1
        assert result["area"] == 800
        assert result["rent_low"] == 1500

    def test_handles_non_dict_secondary_gracefully(self):
        primary = {"unit_number": "618"}
        result = merge_units(primary, None)  # type: ignore[arg-type]
        assert result == {"unit_number": "618"}


# ── merge_units — _date_placeholder coupling ──────────────────────────────────


class TestMergeUnitsDatePlaceholder:
    def test_real_date_drops_stale_placeholder(self):
        """Primary has placeholder, secondary has real date → placeholder cleared.

        Pre-fix bug: placeholder excluded from field-level loop survived
        even after the real date was inherited from secondary, producing
        a contradictory row.
        """
        primary = {
            "available_date": None,
            "_date_placeholder": "Spring 2026",
        }
        secondary = {"available_date": "2026-06-01"}
        result = merge_units(primary, secondary)
        assert result["available_date"] == "2026-06-01"
        assert "_date_placeholder" not in result

    def test_primary_with_real_date_strips_secondary_placeholder(self):
        """Primary has real date, secondary has placeholder → no inheritance."""
        primary = {"available_date": "2026-06-01"}
        secondary = {
            "available_date": None,
            "_date_placeholder": "Coming Soon",
        }
        result = merge_units(primary, secondary)
        assert result["available_date"] == "2026-06-01"
        assert "_date_placeholder" not in result

    def test_inherits_secondary_placeholder_when_neither_has_real_date(self):
        """Primary has no date info; secondary has placeholder → inherit.

        The placeholder is the only signal that the row's missing date
        was synthesised — dropping it erases provenance.
        """
        primary = {"unit_number": "618"}
        secondary = {
            "available_date": None,
            "_date_placeholder": "TBD",
        }
        result = merge_units(primary, secondary)
        assert result.get("available_date") is None
        assert result["_date_placeholder"] == "TBD"

    def test_primary_placeholder_wins_over_secondary_placeholder(self):
        """Both sides have placeholder, neither has real date — primary wins."""
        primary = {"_date_placeholder": "Spring 2026"}
        secondary = {"_date_placeholder": "Coming Soon"}
        result = merge_units(primary, secondary)
        assert result["_date_placeholder"] == "Spring 2026"

    def test_availability_date_alias_also_triggers_placeholder_drop(self):
        """The coupling tracks both ``available_date`` AND ``availability_date``."""
        primary = {
            "availability_date": None,
            "_date_placeholder": "Spring 2026",
        }
        secondary = {"availability_date": "2026-06-01"}
        result = merge_units(primary, secondary)
        assert "_date_placeholder" not in result
