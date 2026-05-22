"""Tests for pms.signal_engine.floor_plan_signals.

Coverage matrix
---------------
Threshold constants
  - SIGNAL_THRESHOLD_ANY == 1
  - SIGNAL_THRESHOLD_STRUCTURAL == 2
  - Constants are the sole source of threshold decisions used at call sites

has_floor_plan_signals
  - Default threshold is SIGNAL_THRESHOLD_STRUCTURAL (2)
  - Returns False for empty text at any threshold
  - Returns correct bool for marketing copy (score 0 → False at threshold 1)
  - Returns correct bool for single-element text (score 1 → True at threshold 1, False at 2)
  - Returns correct bool for rich unit card (score ≥ 2 → True at both thresholds)
  - Explicit threshold=SIGNAL_THRESHOLD_ANY works
  - Explicit threshold=SIGNAL_THRESHOLD_STRUCTURAL works
  - Call sites must use named constants (not magic integers 1 or 2)

count_floor_plan_signals
  - Returns 0 for empty / None-ish input
  - Bedroom patterns: BR, bed, beds, bedroom, bedrooms, R (room), Studio, Efficiency
  - Bathroom patterns: BA, bath, baths, bathroom, full bath, half bath, decimal (1.5)
  - Area patterns: sqft, sq ft, sq. ft., SF, sf, square feet, square foot
  - Floor-plan type: 1/1, 2/2, 1BR/1BA, 2Bed/2Bath, Studio, bare slash form
  - Scoring: each type counted once regardless of repetition
  - Marketing text scores 0 or 1 (no structural signals)
  - Rich unit card scores 3-4

normalize_field_key
  - Returns canonical key for all known aliases (beds, baths, sqft, floor_plan_name)
  - Handles camelCase → lowercase mapping
  - Handles hyphen → underscore conversion
  - Already-canonical keys pass through unchanged
  - Unknown keys returned lowercased / underscore-normalised

normalize_field_keys
  - frozenset input → frozenset output
  - All keys normalised
  - Duplicate after normalisation → deduplicated

normalize_field_dict
  - Keys remapped, values preserved
  - camelCase API dict → canonical keys
  - Entrata real-world fields: no_of_bedroom, no_of_bathroom, square_footage
  - Collision: two source keys → same canonical key (last-wins, not raised)

parse_floor_plan_label
  - Studio → beds=0, baths=None
  - "1BR/1BA" → beds=1, baths=1.0
  - "2Bed/2Bath" → beds=2, baths=2.0
  - "1/1" → beds=1, baths=1.0
  - "1 Bed / 1.5 Bath" → beds=1, baths=1.5
  - "3BR" → beds=3, baths=None
  - "2BR/2BA" → beds=2, baths=2.0
  - Empty string → both None

FieldCombination integration (normalised keys)
  - Entrata response with no_of_bedroom / no_of_bathroom / square_footage qualifies
  - Response with bathRooms (camelCase) qualifies after normalisation
  - Response with only beds + baths (missing area/name group) rejected
"""

from __future__ import annotations

import pytest

from ma_poc.pms.signal_engine.floor_plan_signals import (
    FIELD_ALIASES,
    SIGNAL_THRESHOLD_ANY,
    SIGNAL_THRESHOLD_STRUCTURAL,
    count_floor_plan_signals,
    has_floor_plan_signals,
    normalize_field_dict,
    normalize_field_key,
    normalize_field_keys,
    parse_floor_plan_label,
)


# ── Threshold constants ─────────────────────────────────────────────────────


class TestThresholdConstants:
    """Threshold values must be stable — call sites depend on exact values."""

    def test_any_threshold_is_one(self) -> None:
        assert SIGNAL_THRESHOLD_ANY == 1

    def test_structural_threshold_is_two(self) -> None:
        assert SIGNAL_THRESHOLD_STRUCTURAL == 2

    def test_structural_greater_than_any(self) -> None:
        assert SIGNAL_THRESHOLD_STRUCTURAL > SIGNAL_THRESHOLD_ANY


# ── has_floor_plan_signals ───────────────────────────────────────────────────


class TestHasFloorPlanSignals:
    """Single wrapper function used by ALL call sites across the codebase.

    Call sites import ``has_floor_plan_signals`` and one of the two threshold
    constants.  No raw integer thresholds (1 or 2) should appear at call sites.
    """

    # -- default threshold (SIGNAL_THRESHOLD_STRUCTURAL == 2) --

    def test_empty_returns_false_at_default(self) -> None:
        assert not has_floor_plan_signals("")

    def test_marketing_copy_returns_false_at_default(self) -> None:
        assert not has_floor_plan_signals("Luxury apartments from $1,200/month")

    def test_single_bedroom_label_returns_false_at_default(self) -> None:
        # Score = 1 (bedrooms only) < structural threshold (2)
        assert not has_floor_plan_signals("1 bedroom apartment")

    def test_two_signal_types_returns_true_at_default(self) -> None:
        # Score = 2 (bedrooms + bathrooms) == structural threshold
        assert has_floor_plan_signals("1 bedroom / 1 bathroom")

    def test_rich_unit_card_returns_true_at_default(self) -> None:
        # Score = 4
        assert has_floor_plan_signals("1BR/1BA — 750 sqft — $1,500/mo")

    # -- explicit SIGNAL_THRESHOLD_ANY --

    def test_empty_returns_false_at_any_threshold(self) -> None:
        assert not has_floor_plan_signals("", SIGNAL_THRESHOLD_ANY)

    def test_marketing_copy_false_at_any_threshold(self) -> None:
        # No structural signals at all
        assert not has_floor_plan_signals("Call us today!", SIGNAL_THRESHOLD_ANY)

    def test_single_bedroom_true_at_any_threshold(self) -> None:
        # Score = 1 >= ANY threshold (1)
        assert has_floor_plan_signals("1 bedroom apartment", SIGNAL_THRESHOLD_ANY)

    def test_studio_true_at_any_threshold(self) -> None:
        assert has_floor_plan_signals("Studio", SIGNAL_THRESHOLD_ANY)

    def test_two_types_true_at_any_threshold(self) -> None:
        assert has_floor_plan_signals("1BR/1BA", SIGNAL_THRESHOLD_ANY)

    # -- explicit SIGNAL_THRESHOLD_STRUCTURAL --

    def test_single_type_false_at_structural_threshold(self) -> None:
        assert not has_floor_plan_signals("1 bedroom", SIGNAL_THRESHOLD_STRUCTURAL)

    def test_two_types_true_at_structural_threshold(self) -> None:
        assert has_floor_plan_signals("2 bedrooms, 1.5 baths", SIGNAL_THRESHOLD_STRUCTURAL)

    def test_area_plus_bedroom_true_at_structural_threshold(self) -> None:
        assert has_floor_plan_signals("1 bed, 750 sqft", SIGNAL_THRESHOLD_STRUCTURAL)

    # -- named-constant usage pattern (documents intended call-site style) --

    def test_scroll_trigger_pattern(self) -> None:
        """Scroll fires when the page has NO floor-plan signals at all."""
        page_has_no_data = "Welcome to our luxury community"
        page_has_data    = "1BR/1BA 750 sqft $1,500/mo"
        # Scroll should fire (not suppress) when no signals
        assert not has_floor_plan_signals(page_has_no_data, SIGNAL_THRESHOLD_ANY)
        # Scroll should be suppressed when data is already present
        assert has_floor_plan_signals(page_has_data, SIGNAL_THRESHOLD_ANY)

    def test_portal_wait_suppress_pattern(self) -> None:
        """12s portal wait is skipped when structural signals already present."""
        thin_page  = "1 bedroom available"        # score=1 → still wait
        rich_page  = "1BR/1BA 750sqft $1,500/mo"  # score=4 → skip wait
        assert not has_floor_plan_signals(thin_page,  SIGNAL_THRESHOLD_STRUCTURAL)
        assert     has_floor_plan_signals(rich_page,   SIGNAL_THRESHOLD_STRUCTURAL)

    def test_page_has_content_signals_pattern(self) -> None:
        """RC3 deferral is suppressed when the page is structurally rich."""
        entrata_shell   = "Schedule a tour today"           # score=0
        entrata_content = "2 bedrooms / 2 bathrooms"        # score=2
        assert not has_floor_plan_signals(entrata_shell,   SIGNAL_THRESHOLD_STRUCTURAL)
        assert     has_floor_plan_signals(entrata_content,  SIGNAL_THRESHOLD_STRUCTURAL)


# ── count_floor_plan_signals ────────────────────────────────────────────────


class TestCountFloorPlanSignals:
    """Each signal type contributes 1 point; max = 4."""

    # -- edge cases --

    def test_empty_string_returns_zero(self) -> None:
        assert count_floor_plan_signals("") == 0

    def test_whitespace_only_returns_zero(self) -> None:
        assert count_floor_plan_signals("   \n\t  ") == 0

    def test_generic_marketing_copy_returns_zero(self) -> None:
        text = "Experience luxury living at The Grand. Call us today."
        assert count_floor_plan_signals(text) == 0

    def test_rent_only_no_structure_returns_zero(self) -> None:
        # A marketing banner with only a price mention scores 0
        text = "Starting at $1,200 / month"
        assert count_floor_plan_signals(text) == 0

    # -- bedroom patterns --

    @pytest.mark.parametrize("text", [
        "1BR",
        "2 BR",
        "1 bed",
        "2 beds",
        "1 bedroom",
        "3 bedrooms",
        "2R",
        "3 Rooms",
        "Studio",
        "studio apartment",
        "Efficiency",
        "efficiency unit",
        "1BR/1BA",     # bedroom + bathroom → would score 2 but test just beds alone below
    ])
    def test_bedroom_pattern_detected(self, text: str) -> None:
        # At least 1 point for any bedroom-containing text
        assert count_floor_plan_signals(text) >= 1

    def test_bedroom_scores_exactly_one_point_regardless_of_count(self) -> None:
        # "1BR 2BR 3BR" in the same text is still 1 bedroom-type point
        assert count_floor_plan_signals("1 bed 2 beds 3 bedrooms") == 1

    # -- bathroom patterns --

    @pytest.mark.parametrize("text", [
        "1BA",
        "2 BA",
        "1 bath",
        "2 baths",
        "1 bathroom",
        "2 bathrooms",
        "1.5 bath",
        "1.5 baths",
        "full bath",
        "half bath",
    ])
    def test_bathroom_pattern_detected(self, text: str) -> None:
        assert count_floor_plan_signals(text) >= 1

    def test_bathroom_scores_one_point_only(self) -> None:
        assert count_floor_plan_signals("1 bath 2 baths 1.5 bathrooms") == 1

    # -- area patterns --

    @pytest.mark.parametrize("text", [
        "750 sqft",
        "750 sq ft",
        "750 sq. ft.",
        "750 SF",
        "750 sf",
        "750 square feet",
        "750 square foot",
        "1200sqft",
        "850 Sq Ft",
    ])
    def test_area_pattern_detected(self, text: str) -> None:
        assert count_floor_plan_signals(text) >= 1

    def test_small_number_not_area(self) -> None:
        # Two-digit numbers are not sqft (minimum is 100)
        assert count_floor_plan_signals("99 sqft") == 0

    def test_area_scores_one_point_only(self) -> None:
        assert count_floor_plan_signals("750 sqft 900 sq ft") == 1

    # -- floor-plan type patterns --

    @pytest.mark.parametrize("text", [
        "1/1",
        "2/2",
        "1BR/1BA",
        "2BR/1BA",
        "2Bed/2Bath",
        "1 Bed / 1 Bath",
        "Studio",
    ])
    def test_floor_plan_type_detected(self, text: str) -> None:
        assert count_floor_plan_signals(text) >= 1

    # -- combined scoring --

    def test_rich_unit_card_scores_four(self) -> None:
        text = "1 Bedroom | 1 Bathroom | 750 sq ft | Available"
        # beds=1, bath=1, area=1, type pattern not in this exact text
        score = count_floor_plan_signals(text)
        assert score >= 3

    def test_full_floor_plan_label_scores_four(self) -> None:
        # "1BR/1BA" triggers bedroom + bathroom + floor-plan-type
        # "750 sqft" adds area
        text = "1BR/1BA — 750 sqft starting at $1,500/mo"
        assert count_floor_plan_signals(text) == 4

    def test_studio_with_area_scores_three(self) -> None:
        # Studio = bedroom + floor-plan-type; + area = 3
        text = "Studio | 500 sq ft | $1,100/mo"
        assert count_floor_plan_signals(text) >= 3

    def test_beds_plus_baths_only_scores_two(self) -> None:
        text = "2 bedrooms / 1 bathroom"
        score = count_floor_plan_signals(text)
        # bedroom=1, bathroom=1, floor-plan-type from the "2/1" pattern = +1 → ≥2
        assert score >= 2

    def test_marketing_banner_scores_at_most_one(self) -> None:
        # Just a price — no structural floor plan signals
        text = "Special offer: $500 off first month! Call now."
        assert count_floor_plan_signals(text) <= 1

    def test_case_insensitive(self) -> None:
        assert count_floor_plan_signals("1 BEDROOM / 1 BATHROOM / 750 SQFT") >= 3
        assert count_floor_plan_signals("1 bedroom / 1 bathroom / 750 sqft") >= 3


# ── normalize_field_key ─────────────────────────────────────────────────────


class TestNormalizeFieldKey:
    """Every known alias must map to its canonical key."""

    # -- bedroom aliases --
    @pytest.mark.parametrize("key,expected", [
        ("no_of_bedroom",   "bedrooms"),
        ("numberofbedrooms","bedrooms"),
        ("num_bedrooms",    "bedrooms"),
        ("bedcount",        "bedrooms"),
        ("bedroom_count",   "bedrooms"),
        ("bedroomcount",    "bedrooms"),
        ("br",              "bedrooms"),
        ("bed",             "bedrooms"),
    ])
    def test_bedroom_aliases(self, key: str, expected: str) -> None:
        assert normalize_field_key(key) == expected

    # -- bathroom aliases --
    @pytest.mark.parametrize("key,expected", [
        ("no_of_bathroom",   "bathrooms"),
        ("no_of_bath",       "bathrooms"),
        ("numberofbathrooms","bathrooms"),
        ("num_bathrooms",    "bathrooms"),
        ("bathcount",        "bathrooms"),
        ("bathroom_count",   "bathrooms"),
        ("bathroomcount",    "bathrooms"),
        # "baths" is NOT aliased — it is a canonical key used by RentCafe natively
        ("ba",               "bathrooms"),
    ])
    def test_bathroom_aliases(self, key: str, expected: str) -> None:
        assert normalize_field_key(key) == expected

    def test_baths_is_canonical_not_aliased(self) -> None:
        # "baths" must remain "baths" — aliasing it to "bathrooms" breaks
        # RentCafe-specific fingerprinting that checks for the native key.
        assert normalize_field_key("baths") == "baths"

    # -- area / sqft aliases --
    @pytest.mark.parametrize("key,expected", [
        ("square_footage",  "sqft"),
        ("squarefeet",      "sqft"),
        ("squarefootage",   "sqft"),
        ("sq_ft",           "sqft"),
        ("sf",              "sqft"),
        ("area",            "sqft"),
        ("minimumsquarefeet", "sqft"),
    ])
    def test_area_aliases(self, key: str, expected: str) -> None:
        assert normalize_field_key(key) == expected

    # -- floor plan name aliases --
    @pytest.mark.parametrize("key,expected", [
        ("floorplan_name",  "floor_plan_name"),
        ("floorplanname",   "floor_plan_name"),
        ("floor_plan",      "floor_plan_name"),
        ("plan_name",       "floor_plan_name"),
        ("unittype",        "floor_plan_name"),
        ("unit_type",       "floor_plan_name"),
        ("floorplan-name",  "floor_plan_name"),   # hyphen → underscore then alias
    ])
    def test_floor_plan_name_aliases(self, key: str, expected: str) -> None:
        assert normalize_field_key(key) == expected

    # -- rent aliases --
    @pytest.mark.parametrize("key,expected", [
        ("minrent",          "min_rent"),
        ("maxrent",          "max_rent"),
        ("minimumrent",      "min_rent"),
        ("maximumrent",      "max_rent"),
        ("askingrent",       "rent"),
        ("monthlyrent",      "rent"),
        ("baserent",         "rent"),
    ])
    def test_rent_aliases(self, key: str, expected: str) -> None:
        assert normalize_field_key(key) == expected

    # -- canonical keys pass through --
    @pytest.mark.parametrize("key", [
        "bedrooms", "bathrooms", "sqft", "floor_plan_name",
        "rent", "min_rent", "max_rent", "unit_number", "unit_id",
        "available_date",
    ])
    def test_canonical_keys_unchanged(self, key: str) -> None:
        assert normalize_field_key(key) == key

    # -- camelCase lowercasing --
    def test_camel_case_lowercased(self) -> None:
        # "bedRooms" → "bedrooms" (not in aliases) → already a known canonical key
        assert normalize_field_key("bedRooms") == "bedrooms"

    def test_camel_case_bathroom(self) -> None:
        assert normalize_field_key("bathRooms") == "bathrooms"

    def test_camel_case_sqft_variant(self) -> None:
        assert normalize_field_key("squareFeet") == "sqft"

    def test_camel_case_min_rent(self) -> None:
        assert normalize_field_key("minRent") == "min_rent"

    def test_camel_case_max_rent(self) -> None:
        assert normalize_field_key("maxRent") == "max_rent"

    # -- hyphen handling --
    def test_hyphen_converted_to_underscore_before_alias_lookup(self) -> None:
        # "floorplan-name" → "floorplan_name" → alias → "floor_plan_name"
        assert normalize_field_key("floorplan-name") == "floor_plan_name"

    def test_space_converted_to_underscore(self) -> None:
        assert normalize_field_key("floor plan name") == "floor_plan_name"

    # -- unknown key returned lowercase-underscored --
    def test_unknown_key_lowercased(self) -> None:
        assert normalize_field_key("MyCustomField") == "mycustomfield"

    def test_unknown_key_with_hyphen(self) -> None:
        assert normalize_field_key("custom-field") == "custom_field"

    # ── 2026-05-22 vendor variants observed in LLM_API fallbacks ────────
    # Brookfield WP middleware uses ``minimumSQFT`` / ``maximumSQFT`` (sqft
    # acronym instead of squareFeet). RentDynamics-style plansandpricing
    # feeds use ``MinSqFt`` / ``MaxSqFt`` (PascalCase). Without these
    # aliases the narrow qualifier missed the sqft signal and the row
    # failed the ≥2-canonical-signals gate. See cloud run 2026-05-22
    # API_ANALYSIS audit.
    @pytest.mark.parametrize("key,expected", [
        ("minimumsqft",  "sqft"),
        ("maximumsqft",  "sqft"),
        ("minsqft",      "sqft"),
        ("maxsqft",      "sqft"),
        # PascalCase / camelCase variants must also work via lowercase
        # normalisation before alias lookup.
        ("MinSqFt",      "sqft"),
        ("MaxSqFt",      "sqft"),
        ("minimumSQFT",  "sqft"),
        ("maximumSQFT",  "sqft"),
    ])
    def test_2026_05_22_sqft_acronym_aliases(self, key: str, expected: str) -> None:
        assert normalize_field_key(key) == expected

    @pytest.mark.parametrize("key,expected", [
        ("availableunitscount",    "unit_count"),
        ("unitsavailable",         "unit_count"),
        ("totalunitsavailable",    "unit_count"),
        ("earliestunitavailable",  "available_date"),
        ("date_available",         "available_date"),
        ("dateavailable",          "available_date"),
        # PascalCase / camelCase still works post-lowercase.
        ("AvailableUnitsCount",    "unit_count"),
        ("UnitsAvailable",         "unit_count"),
        ("EarliestUnitAvailable",  "available_date"),
        ("date_available",         "available_date"),
    ])
    def test_2026_05_22_availability_aliases(self, key: str, expected: str) -> None:
        assert normalize_field_key(key) == expected

    @pytest.mark.parametrize("key,expected", [
        ("unitminprice", "min_rent"),
        ("unitmaxprice", "max_rent"),
        # PascalCase variant from plansandpricing payloads.
        ("UnitMinPrice", "min_rent"),
        ("UnitMaxPrice", "max_rent"),
    ])
    def test_2026_05_22_per_unit_price_aliases(self, key: str, expected: str) -> None:
        assert normalize_field_key(key) == expected


# ── normalize_field_keys ────────────────────────────────────────────────────


class TestNormalizeFieldKeys:
    def test_frozenset_input_returns_frozenset(self) -> None:
        result = normalize_field_keys(frozenset({"no_of_bedroom", "square_footage"}))
        assert isinstance(result, frozenset)

    def test_set_input_accepted(self) -> None:
        result = normalize_field_keys({"no_of_bathroom", "sqft"})
        assert "bathrooms" in result
        assert "sqft" in result

    def test_all_keys_normalised(self) -> None:
        result = normalize_field_keys(frozenset({
            "no_of_bedroom", "no_of_bathroom", "square_footage",
        }))
        assert result == frozenset({"bedrooms", "bathrooms", "sqft"})

    def test_duplicate_after_normalisation_deduplicated(self) -> None:
        # "no_of_bathroom" and "bathrooms" both normalise to "bathrooms"
        result = normalize_field_keys(frozenset({"no_of_bathroom", "bathrooms"}))
        assert result == frozenset({"bathrooms"})

    def test_baths_stays_baths_not_deduplicated(self) -> None:
        # "baths" is canonical (not aliased), so {baths, bathrooms} → {baths, bathrooms}
        result = normalize_field_keys(frozenset({"baths", "bathrooms"}))
        assert result == frozenset({"baths", "bathrooms"})

    def test_canonical_keys_preserved(self) -> None:
        keys = frozenset({"rent", "bedrooms", "bathrooms", "sqft"})
        assert normalize_field_keys(keys) == keys


# ── normalize_field_dict ────────────────────────────────────────────────────


class TestNormalizeFieldDict:
    def test_keys_remapped_values_preserved(self) -> None:
        d = {"no_of_bedroom": 1, "no_of_bathroom": 1, "square_footage": 750}
        result = normalize_field_dict(d)
        assert result["bedrooms"] == 1
        assert result["bathrooms"] == 1
        assert result["sqft"] == 750

    def test_entrata_real_world_fields(self) -> None:
        # Mirrors actual fixture data from tests/pms/adapters/fixtures/entrata/
        entrata_item = {
            "id":              "fp-001",
            "floorplan-name":  "A1",
            "no_of_bedroom":   1,
            "no_of_bathroom":  1,
            "square_footage":  750,
            "min_rent":        "$1,565",
            "max_rent":        "$3,110",
        }
        result = normalize_field_dict(entrata_item)
        assert result["floor_plan_name"] == "A1"
        assert result["bedrooms"] == 1
        assert result["bathrooms"] == 1
        assert result["sqft"] == 750
        assert result["min_rent"] == "$1,565"

    def test_camel_case_api_response(self) -> None:
        item = {
            "unitNumber": "101",
            "bedRooms": 2,
            "bathRooms": 1,
            "squareFeet": 900,
            "minRent": 1800,
        }
        result = normalize_field_dict(item)
        assert "unit_number" in result or "unitnumber" in result  # lowercased
        assert result.get("bedrooms") == 2
        assert result.get("bathrooms") == 1
        assert result.get("sqft") == 900
        assert result.get("min_rent") == 1800

    def test_values_not_modified(self) -> None:
        d = {"no_of_bedroom": None, "square_footage": 0, "min_rent": ""}
        result = normalize_field_dict(d)
        assert result["bedrooms"] is None
        assert result["sqft"] == 0
        assert result["min_rent"] == ""

    def test_canonical_keys_pass_through(self) -> None:
        d = {"bedrooms": 2, "bathrooms": 2, "sqft": 1000, "rent": 2500}
        result = normalize_field_dict(d)
        assert result == d

    def test_unknown_keys_lowercased(self) -> None:
        d = {"CustomField": "value", "AnotherField": 42}
        result = normalize_field_dict(d)
        assert "customfield" in result
        assert "anotherfield" in result

    def test_collision_last_wins(self) -> None:
        # "no_of_bathroom" and "bathrooms" both → "bathrooms"; last wins
        d = {"no_of_bathroom": 1, "bathrooms": 2}
        result = normalize_field_dict(d)
        assert result["bathrooms"] == 2

    def test_baths_and_bathrooms_both_preserved(self) -> None:
        # "baths" is canonical and NOT aliased to "bathrooms", so both coexist
        d = {"baths": 1, "bathrooms": 2}
        result = normalize_field_dict(d)
        assert result["baths"] == 1
        assert result["bathrooms"] == 2


# ── parse_floor_plan_label ──────────────────────────────────────────────────


class TestParseFloorPlanLabel:
    def test_studio(self) -> None:
        r = parse_floor_plan_label("Studio")
        assert r["beds"] == 0
        assert r["baths"] is None

    def test_studio_lowercase(self) -> None:
        r = parse_floor_plan_label("studio")
        assert r["beds"] == 0

    def test_efficiency(self) -> None:
        r = parse_floor_plan_label("Efficiency")
        assert r["beds"] == 0

    def test_eff_abbreviation(self) -> None:
        r = parse_floor_plan_label("Eff")
        assert r["beds"] == 0

    def test_1br_1ba(self) -> None:
        r = parse_floor_plan_label("1BR/1BA")
        assert r["beds"] == 1
        assert r["baths"] == 1.0

    def test_2br_2ba(self) -> None:
        r = parse_floor_plan_label("2BR/2BA")
        assert r["beds"] == 2
        assert r["baths"] == 2.0

    def test_2br_1ba(self) -> None:
        r = parse_floor_plan_label("2BR/1BA")
        assert r["beds"] == 2
        assert r["baths"] == 1.0

    def test_2bed_2bath(self) -> None:
        r = parse_floor_plan_label("2Bed/2Bath")
        assert r["beds"] == 2
        assert r["baths"] == 2.0

    def test_bare_slash_form(self) -> None:
        r = parse_floor_plan_label("1/1")
        assert r["beds"] == 1
        assert r["baths"] == 1.0

    def test_bare_slash_form_2_2(self) -> None:
        r = parse_floor_plan_label("2/2")
        assert r["beds"] == 2
        assert r["baths"] == 2.0

    def test_spaced_slash(self) -> None:
        r = parse_floor_plan_label("1 Bed / 1 Bath")
        assert r["beds"] == 1
        assert r["baths"] == 1.0

    def test_decimal_baths(self) -> None:
        r = parse_floor_plan_label("2BR/1.5BA")
        assert r["beds"] == 2
        assert r["baths"] == 1.5

    def test_decimal_baths_spaced(self) -> None:
        r = parse_floor_plan_label("1 Bed / 1.5 Bath")
        assert r["beds"] == 1
        assert r["baths"] == 1.5

    def test_beds_only_no_slash(self) -> None:
        r = parse_floor_plan_label("3BR")
        assert r["beds"] == 3
        assert r["baths"] is None

    def test_empty_string(self) -> None:
        r = parse_floor_plan_label("")
        assert r["beds"] is None
        assert r["baths"] is None

    def test_returns_dict_always(self) -> None:
        r = parse_floor_plan_label("garbage input ???")
        assert isinstance(r, dict)
        assert "beds" in r
        assert "baths" in r


# ── Integration: normalised keys feed FieldCombination ──────────────────────


class TestNormalisedFieldCombinationIntegration:
    """Verify that normalize_field_dict output satisfies FieldCombination rules."""

    @pytest.fixture(scope="class")
    def qualifier(self):
        from ma_poc.pms.signal_engine.defaults import create_default_qualifier
        return create_default_qualifier()

    def _signal(self, keys: set[str]):
        from ma_poc.pms.signal_engine.models import SourceKind, SourceSignal
        return SourceSignal(
            kind=SourceKind.API_RESPONSE,
            field_keys=frozenset(keys),
        )

    def test_entrata_fields_qualify_after_normalisation(self, qualifier) -> None:
        """Entrata's no_of_bedroom / no_of_bathroom / square_footage qualifies."""
        raw_keys = {"no_of_bedroom", "no_of_bathroom", "square_footage", "floorplan-name", "min_rent"}
        normalised = normalize_field_keys(frozenset(raw_keys))
        sig = self._signal(normalised)
        result = qualifier.qualify(sig)
        assert result.qualifies, f"expected qualify, got: {result.reason}"

    def test_camel_case_api_qualifies_after_normalisation(self, qualifier) -> None:
        """squareFeet + bedRooms + bathRooms qualifies as floor_plan_bed_bath_area."""
        raw_keys = {"squareFeet", "bedRooms", "bathRooms", "unitNumber"}
        normalised = normalize_field_keys(frozenset(raw_keys))
        sig = self._signal(normalised)
        result = qualifier.qualify(sig)
        assert result.qualifies, f"expected qualify, got: {result.reason}"

    def test_beds_plus_baths_only_rejected(self, qualifier) -> None:
        """beds + bathrooms alone (no area/name group) must be rejected."""
        sig = self._signal({"bedrooms", "bathrooms"})
        result = qualifier.qualify(sig)
        assert not result.qualifies

    def test_beds_area_no_baths_rejected(self, qualifier) -> None:
        """beds + sqft but no bath group must be rejected."""
        sig = self._signal({"bedrooms", "sqft"})
        result = qualifier.qualify(sig)
        assert not result.qualifies

    def test_full_canonical_keys_qualify(self, qualifier) -> None:
        """Standard keys that were always canonical still qualify."""
        sig = self._signal({"bedrooms", "bathrooms", "sqft", "floor_plan_name"})
        result = qualifier.qualify(sig)
        assert result.qualifies

    def test_null_value_items_still_pass_key_check(self, qualifier) -> None:
        """Key presence is what matters for FieldCombination; value nulls are
        handled by has_unit_signals separately."""
        # All three groups covered → qualifies at key level
        sig = self._signal({"bedrooms", "bathrooms", "sqft"})
        result = qualifier.qualify(sig)
        assert result.qualifies

    def test_floor_plan_name_group_alternative(self, qualifier) -> None:
        """bed + bath + floor_plan_name qualifies via the second combination."""
        normalised = normalize_field_keys(frozenset({"no_of_bedroom", "no_of_bathroom", "floorplan-name"}))
        sig = self._signal(normalised)
        result = qualifier.qualify(sig)
        assert result.qualifies, f"expected qualify, got: {result.reason}"


# ── FIELD_ALIASES completeness ───────────────────────────────────────────────


class TestFieldAliasesTable:
    """Sanity-check the FIELD_ALIASES dict itself."""

    def test_all_values_are_canonical(self) -> None:
        """Every alias target must itself be canonical (not an alias)."""
        for alias, canonical in FIELD_ALIASES.items():
            # A canonical key should not itself be in FIELD_ALIASES as a key
            # (that would be a chain: alias → canonical → canonical2)
            assert canonical not in FIELD_ALIASES, (
                f"FIELD_ALIASES[{alias!r}] = {canonical!r}, but {canonical!r} "
                f"is also in FIELD_ALIASES (chain detected)"
            )

    def test_no_self_mapping(self) -> None:
        for alias, canonical in FIELD_ALIASES.items():
            assert alias != canonical, f"Self-mapping: {alias!r} → {canonical!r}"

    def test_all_aliases_are_lowercase_underscored(self) -> None:
        """All alias keys must already be lowercased + underscored (normalisation
        applied before lookup, so keys must match post-normalisation form)."""
        for alias in FIELD_ALIASES:
            assert alias == alias.lower().replace("-", "_").replace(" ", "_"), (
                f"Alias key {alias!r} is not pre-normalised"
            )
