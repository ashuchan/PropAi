"""Regression tests for the parsing-regex hardening pass (2026-05-25).

Bundles the four canary-1ef1060 user-flagged regressions:

  * regr #12  floor_plan_name empty/"~"/"Unknown" with no DOM signal but
              ``?floorplan=…`` slug — derive readable name from slug.
              User-flagged: https://www.lifeatalexis.com/floorplans/
              ?floorplan=1-bed-1-bath-1992 (emitted "~").
  * regr #13  bath count "1 Bathroom" / "2 Bathrooms" missed by the
              ``(?:bath|ba)\\b`` pattern because the trailing word
              boundary fails when "bath" is followed by "room".
              User-flagged: primeurbanproperties.com/unit/the-fitzgerald-unit-405/.
  * regr #16  sqft "1,200 ft²" / "950 ft2" / "1200 ft^2" / "1200 sf"
              missed by the older sqft regex — came through as -1.
              User-flagged: eaglepointestates.com/#floor-plans.
  * regr #17  unit_number leak from generic DOM scan: adjacent <td>
              cells collapse and "623 sq ft" lands in unit_number.
              User-flagged: spearheadproperties.com/property/oak-i/
              ?unit_gallery=1-bedroom-unit---a.

All tests run against the canonical helpers added in _parsing.py without
booting the full adapter package (the package __init__ imports
playwright / bs4 which aren't pinned for the test runner here). The
unit-under-test is _parsing.py in isolation.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PARSING_PATH = (
    Path(__file__).resolve().parents[3] / "pms" / "adapters" / "_parsing.py"
)
_spec = importlib.util.spec_from_file_location("_parsing_isolated", _PARSING_PATH)
assert _spec is not None and _spec.loader is not None
_parsing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parsing)

BATH_RE = _parsing.BATH_RE
SQFT_RE = _parsing.SQFT_RE
derive_plan_name_from_url = _parsing.derive_plan_name_from_url
clean_unit_number = _parsing.clean_unit_number
make_unit_dict = _parsing.make_unit_dict
infer_bed_bath_from_name = _parsing.infer_bed_bath_from_name


# ─────────────────────────────────────────────────────────────────────────
# regr #13 — BATH_RE accepts every casing/spelling variant
# ─────────────────────────────────────────────────────────────────────────
class TestBathRegex:
    """Covers ``Bath``, ``Baths``, ``Bathroom``, ``Bathrooms``, ``BA``.

    Production cohort: ~100+ properties using long-form "Bathroom"
    in the unit-detail HTML (primeurbanproperties.com user-flagged).
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1 Bath", "1"),
            ("1 Baths", "1"),
            ("1 Bathroom", "1"),
            ("2 Bathrooms", "2"),
            ("2.5 Baths", "2.5"),
            ("1.5 Bathrooms", "1.5"),
            ("1 BA", "1"),
            ("2 ba", "2"),
            ("10 Bathrooms", "10"),  # multi-digit (was lost by single-\d)
            ("1 bath", "1"),  # lowercase
            ("1 BATHROOM", "1"),  # uppercase
            ("Studio - 1 Bathroom", "1"),  # in context
            ("3 BR / 2 Bathrooms", "2"),  # bath-of-pair
        ],
    )
    def test_canonical_bath_variants_match(
        self, text: str, expected: str
    ) -> None:
        m = BATH_RE.search(text)
        assert m is not None, f"BATH_RE did not match {text!r}"
        assert m.group(1) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "Bath time",  # no number
            "Bather",  # plural noun, no digit
            "Battery 1",  # different word
            "1 bedroom",  # bed not bath
        ],
    )
    def test_canonical_bath_no_false_positives(self, text: str) -> None:
        assert BATH_RE.search(text) is None

    def test_infer_bed_bath_uses_canonical_bath(self) -> None:
        """The infer helper inherits the canonical regex via _BATH_ONLY_RE."""
        beds, baths = infer_bed_bath_from_name("1 Bedroom 1 Bathroom")
        assert beds == 1
        assert baths == 1.0

    def test_infer_bed_bath_picks_up_plural_bathrooms(self) -> None:
        beds, baths = infer_bed_bath_from_name("2 Bed 2 Bathrooms")
        assert beds == 2
        assert baths == 2.0


# ─────────────────────────────────────────────────────────────────────────
# regr #16 — SQFT_RE accepts ft²/ft2/ft^2/sf/square-* variants
# ─────────────────────────────────────────────────────────────────────────
class TestSqftRegex:
    """Covers the symbol and word forms that the older regex dropped.

    Production cohort: eaglepointestates.com used "1,200 ft²" and was
    coming through as sqft=-1 because neither ft² nor ft2 was matched.
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1200 sqft", "1200"),
            ("1,200 sq ft", "1,200"),
            ("950 sq.ft.", "950"),
            ("950 ft²", "950"),  # unicode superscript
            ("1,200 ft²", "1,200"),
            ("950 ft2", "950"),  # ASCII variant
            ("1200 ft^2", "1200"),  # caret variant
            ("950 square feet", "950"),
            ("950 square foot", "950"),
            ("950 square ft", "950"),
            ("1200 sf", "1200"),  # abbreviated
            ("950sqft", "950"),  # no whitespace
            ("1,200ft2", "1,200"),
            ("623 sq ft</td>", "623"),  # bleeds into HTML
        ],
    )
    def test_canonical_sqft_variants_match(
        self, text: str, expected: str
    ) -> None:
        m = SQFT_RE.search(text)
        assert m is not None, f"SQFT_RE did not match {text!r}"
        assert m.group(1) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "no sqft here",  # no digit anchor
            "$1,200",  # rent, not sqft
            "1 Bedroom",  # bed
        ],
    )
    def test_canonical_sqft_no_false_positives(self, text: str) -> None:
        assert SQFT_RE.search(text) is None

    def test_sqft_does_not_capture_sf_inside_word(self) -> None:
        """The lookahead on `s.?f.?` avoids matching tokens like sfgate."""
        # "1 sfgate" — `1 sf` would otherwise look like sqft.
        assert SQFT_RE.search("1 sfgate") is None


# ─────────────────────────────────────────────────────────────────────────
# regr #12 — floor_plan_name slug → titleized name fallback
# ─────────────────────────────────────────────────────────────────────────
class TestDerivePlanNameFromUrl:
    """Covers query-param and path-segment forms for slug derivation."""

    def test_alexis_user_flagged_url(self) -> None:
        """The exact URL from the chip — 1-bed-1-bath-1992 → '1 Bed 1 Bath'."""
        url = (
            "https://www.lifeatalexis.com/floorplans/"
            "?floorplan=1-bed-1-bath-1992"
        )
        assert derive_plan_name_from_url(url) == "1 Bed 1 Bath"

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            # Query-param variants
            ("https://x/foo?floorplan=studio", "Studio"),
            ("https://x/foo?floor_plan=2br-deluxe", "2br Deluxe"),
            ("https://x/foo?plan=loft-a3", "Loft A3"),
            ("https://x/foo?fp=garden", "Garden"),
            (
                "https://x/property/oak/?unit_gallery=1-bedroom-unit---a",
                "1 Bedroom Unit A",
            ),
            # Path-segment variants
            ("https://x/floorplans/the-aspen/", "The Aspen"),
            ("https://x/floor-plans/the-elm", "The Elm"),
            ("https://x/floorplan/loft/", "Loft"),
            # Trailing 3+ digit token is trimmed (it's a per-unit id)
            ("https://x/?floorplan=1-bed-1-bath-1992", "1 Bed 1 Bath"),
            # Trailing 1- or 2-digit token is KEPT (likely plan variant)
            ("https://x/?floorplan=plan-a-2", "Plan A 2"),
        ],
    )
    def test_slug_extraction_variants(self, url: str, expected: str) -> None:
        assert derive_plan_name_from_url(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "",
            None,
            "https://x/",  # no slug
            "https://x/about",  # unrelated path
            "https://x/?other_param=value",  # unrecognised key
        ],
    )
    def test_no_slug_returns_empty(self, url: str | None) -> None:
        assert derive_plan_name_from_url(url) == ""

    def test_malformed_url_returns_empty(self) -> None:
        """Bad URL never raises — returns empty string."""
        assert derive_plan_name_from_url("not://a real url at all") == ""

    def test_case_insensitive_query_key(self) -> None:
        """``?FloorPlan=…`` works just like ``?floorplan=…``."""
        assert (
            derive_plan_name_from_url("https://x/?FloorPlan=the-elm")
            == "The Elm"
        )


# ─────────────────────────────────────────────────────────────────────────
# regr #17 — unit_number sqft-leak guard
# ─────────────────────────────────────────────────────────────────────────
class TestCleanUnitNumber:
    """Strips sqft text that bleeds from adjacent DOM cells."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Pure leak: only sqft text → empty
            ("623 sq ft", ""),
            ("975 ft²", ""),
            ("1,200 sqft", ""),
            ("950 ft2", ""),
            # Mixed: real unit number with sqft suffix → keep the number
            ("623 sq ft 105", "105"),
            ("950 ft2 - 102", "102"),
            ("A301 - 750 sqft", "A301"),
            # Clean inputs pass through unchanged
            ("101", "101"),
            ("A-301", "A-301"),
            ("Loft-5", "Loft-5"),
            ("3B", "3B"),
            # Empty / whitespace handled gracefully
            ("", ""),
            ("   ", ""),
        ],
    )
    def test_strip_sqft_from_unit_number(
        self, raw: str, expected: str
    ) -> None:
        assert clean_unit_number(raw) == expected

    def test_non_string_passes_through_safely(self) -> None:
        """None or non-str inputs never raise."""
        # Type ignore — exercising defensive non-string handling.
        assert clean_unit_number(None) == ""  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────
# Integration through make_unit_dict — the entry every adapter funnels
# through. These guard against silent breakage of the wiring.
# ─────────────────────────────────────────────────────────────────────────
class TestMakeUnitDictIntegration:

    # #12: slug fallback
    def test_empty_plan_name_with_floorplan_query_gets_derived(self) -> None:
        out = make_unit_dict(
            floor_plan_name="~",
            unit_number="101",
            source_api_url=(
                "https://www.lifeatalexis.com/floorplans/"
                "?floorplan=1-bed-1-bath-1992"
            ),
        )
        assert out["floor_plan_name"] == "1 Bed 1 Bath"

    def test_empty_plan_name_with_path_slug_gets_derived(self) -> None:
        out = make_unit_dict(
            floor_plan_name="",
            unit_number="205",
            source_api_url="https://example.com/floorplans/the-aspen/",
        )
        assert out["floor_plan_name"] == "The Aspen"

    def test_real_plan_name_passes_through_when_slug_present(self) -> None:
        """A non-empty plan name MUST NOT be overwritten by the slug."""
        out = make_unit_dict(
            floor_plan_name="The Reserve",
            unit_number="100",
            source_api_url="https://example.com/?floorplan=studio",
        )
        assert out["floor_plan_name"] == "The Reserve"

    def test_unknown_token_triggers_slug_fallback(self) -> None:
        out = make_unit_dict(
            floor_plan_name="Unknown",
            unit_number="100",
            source_api_url="https://example.com/?floorplan=loft-deluxe",
        )
        assert out["floor_plan_name"] == "Loft Deluxe"

    def test_empty_plan_name_without_slug_stays_empty(self) -> None:
        """No slug → leave plan name as the empty token; do not invent."""
        out = make_unit_dict(
            floor_plan_name="~",
            unit_number="100",
            source_api_url="https://example.com/about",
        )
        assert out["floor_plan_name"] == "~"

    # #17: unit_number leak cleanup
    def test_unit_number_sqft_leak_is_cleared(self) -> None:
        """Spearhead-style leak: pure sqft text → empty unit_number."""
        out = make_unit_dict(
            floor_plan_name="1 Bed 1 Bath",
            unit_number="623 sq ft",
        )
        assert out["unit_number"] == ""

    def test_unit_number_mixed_leak_keeps_the_id(self) -> None:
        """When both id and sqft are present, keep the id."""
        out = make_unit_dict(
            floor_plan_name="1 Bed 1 Bath",
            unit_number="623 sq ft 105",
        )
        assert out["unit_number"] == "105"

    def test_clean_unit_number_passes_through(self) -> None:
        out = make_unit_dict(unit_number="A-301")
        assert out["unit_number"] == "A-301"

    # Cross-fix combo: both #12 and #17 firing on a single unit
    def test_combined_slug_fallback_and_unit_leak(self) -> None:
        out = make_unit_dict(
            floor_plan_name="",
            unit_number="975 ft²",
            source_api_url=(
                "https://www.spearheadproperties.com/property/oak-i/"
                "?unit_gallery=1-bedroom-unit---a"
            ),
        )
        assert out["floor_plan_name"] == "1 Bedroom Unit A"
        assert out["unit_number"] == ""
