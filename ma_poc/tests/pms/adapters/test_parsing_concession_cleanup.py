"""Centralized concession cleanup in make_unit_dict (2026-05-24).

Pre-fix: 11 adapters (entrata, g5, sightmap, generic, leaseleads,
avalonbay, maac, cortland, irvine, essex, funnel) all called
``make_unit_dict(concession="raw text")`` and emitted only the legacy
``concession`` field. The cleanup (concession_clean / concession_normalize)
ran only at v2 output time — meaning:
  * Cross-tier merges in _merge_fns.py preserved
    ``concession_text``/``_value``/``_source`` but not the legacy
    ``concession``, silently dropping concessions on merge
  * Internal flows operated on dirty raw text (JS/CSS leak from the
    scraper's window-capture regex)
  * ``concession_value`` was rarely populated at unit level — only the
    property-level ``concessions_text`` ran through normalize_concession

Post-fix: ``make_unit_dict`` accepts legacy ``concession=`` AND
canonical ``concession_text=`` / ``concession_value=`` /
``concession_source=`` kwargs. Either text input flows through:
  * ``clean_concession_text`` → ``concession_text_clean``
  * ``classify_concession_quality`` → ``_concession_quality``
  * ``normalize_concession`` → derives ``concession_value`` when absent
Caller-supplied canonical values win over derived values (capture-first).
"""
from __future__ import annotations

from ma_poc.pms.adapters._parsing import make_unit_dict


# ── Back-compat: legacy ``concession=`` still works ──────────────────


def test_legacy_concession_kwarg_populates_all_canonical_fields() -> None:
    """An adapter calling the OLD signature (``concession=`` only) must
    automatically get every canonical field populated — that's the
    whole point of the centralisation: zero adapter changes required."""
    u = make_unit_dict(
        floor_plan_name="A1",
        bedrooms="1",
        bathrooms="1",
        sqft="700",
        rent_low=1500,
        rent_high=1500,
        concession="One month free on 12-month lease",
    )
    # Raw text on BOTH the legacy and canonical key
    assert u["concession"] == "One month free on 12-month lease"
    assert u["concession_text"] == "One month free on 12-month lease"
    # Cleanup ran
    assert u["concession_text_clean"]  # non-empty cleaned variant
    assert u["_concession_quality"]  # classifier label populated


def test_canonical_concession_text_kwarg_works() -> None:
    """Adapters that want to use the canonical surface can pass
    ``concession_text=`` directly. Legacy ``concession`` mirrors the
    canonical text for back-compat consumers."""
    u = make_unit_dict(
        floor_plan_name="B1",
        sqft="800",
        rent_low=1700,
        rent_high=1700,
        concession_text="$500 off first month",
    )
    assert u["concession_text"] == "$500 off first month"
    assert u["concession"] == "$500 off first month"  # back-compat mirror
    assert u["concession_text_clean"]


# ── Caller-supplied canonical values win (capture-first) ─────────────


def test_caller_supplied_concession_value_is_preserved() -> None:
    """When the adapter explicitly knows the numeric value (e.g. it
    parsed it from a structured API), make_unit_dict must NOT overwrite
    it with a derived value."""
    u = make_unit_dict(
        floor_plan_name="C1",
        concession_text="One month free",
        concession_value=1234.0,  # adapter explicitly set this
    )
    assert u["concession_value"] == 1234.0


def test_concession_value_derived_when_caller_omits() -> None:
    """When the caller doesn't pass concession_value but the text is
    parseable, normalize_concession derives the value automatically."""
    u = make_unit_dict(
        floor_plan_name="D1",
        concession_text="$500 off first month",
    )
    assert u["concession_value"] == 500.0  # derived from text


def test_concession_value_none_when_text_unparseable() -> None:
    """If normalize_concession can't extract a numeric value (e.g.
    'Call for current specials'), concession_value stays None — that's
    expected, not a bug. The raw text is still preserved."""
    u = make_unit_dict(
        floor_plan_name="E1",
        concession_text="Call for current specials",
    )
    assert u["concession_text"] == "Call for current specials"
    assert u["concession_value"] is None


def test_concession_source_passes_through() -> None:
    u = make_unit_dict(
        floor_plan_name="F1",
        concession_text="2 weeks free",
        concession_source="banner",
    )
    assert u["concession_source"] == "banner"


# ── Empty / missing concession is a clean no-op ──────────────────────


def test_no_concession_text_leaves_canonical_fields_none() -> None:
    """A unit with no concession data — the canonical fields are
    explicitly None (not missing), so downstream consumers see a
    consistent shape every time."""
    u = make_unit_dict(
        floor_plan_name="G1",
        rent_low=1500,
        rent_high=1500,
    )
    assert u["concession"] == ""  # legacy default
    assert u["concession_text"] is None
    assert u["concession_text_clean"] is None
    assert u["_concession_quality"] is None
    assert u["concession_value"] is None
    assert u["concession_source"] is None


def test_whitespace_only_concession_treated_as_no_data() -> None:
    """Whitespace-only strings are equivalent to no data — canonical
    fields stay None. (The legacy ``concession`` field preserves the
    raw empty/whitespace input for diagnostic purposes.)"""
    u = make_unit_dict(
        floor_plan_name="H1",
        concession="   ",
    )
    assert u["concession_text"] is None
    assert u["concession_text_clean"] is None


def test_canonical_field_wins_when_both_provided() -> None:
    """If a caller passes BOTH the canonical and legacy forms (rare),
    the canonical value is the authoritative source."""
    u = make_unit_dict(
        floor_plan_name="I1",
        concession="legacy text",
        concession_text="canonical text",
    )
    assert u["concession_text"] == "canonical text"
    assert u["concession"] == "canonical text"  # mirrors canonical


# ── Dirty input flows through the de-leaker ──────────────────────────


def test_dirty_concession_gets_cleaned_text_clean_field() -> None:
    """Real-world: a Duda-CMS scrape pulls JS function body before the
    real offer. The raw is preserved unchanged; the de-leaked variant
    surfaces in concession_text_clean for display."""
    dirty = (
        "function() { if (href.indexOf('//') === 0) return; "
        "} One month free with 12-month lease"
    )
    u = make_unit_dict(
        floor_plan_name="J1",
        concession=dirty,
    )
    # Raw preserved verbatim
    assert u["concession"] == dirty
    assert u["concession_text"] == dirty
    # Cleaned variant is shorter (JS prefix stripped) and non-empty
    cleaned = u["concession_text_clean"]
    assert isinstance(cleaned, str)
    assert len(cleaned) > 0
    # Quality label flags the leak — actual labels from concession_clean
    assert u["_concession_quality"] in (
        "clean", "empty", "unclean_script_leak", "unclean_style_leak",
        "unclean_dmapi", "unclean_json_blob", "unclean_orphan_prefix",
        "unclean_header_only",
    )
    # For this specific input, the quality must NOT be "clean" — there's
    # an obvious JS prefix to flag
    assert u["_concession_quality"] != "clean"


# ── Smoke: every adapter's emission shape stays consistent ───────────


def test_all_canonical_concession_keys_always_present() -> None:
    """Schema stability: the 5 canonical concession keys are ALWAYS in
    the output dict (None when no source data). Downstream code can
    rely on key presence without ``.get()`` guards."""
    u = make_unit_dict(floor_plan_name="K1")
    required_keys = {
        "concession",
        "concession_text",
        "concession_text_clean",
        "_concession_quality",
        "concession_value",
        "concession_source",
    }
    assert required_keys.issubset(set(u.keys()))


# ── Cleanup never throws (defensive) ─────────────────────────────────


def test_concession_cleanup_does_not_crash_on_extreme_input() -> None:
    """Adversarial inputs: very long text, unicode, mixed scripts.
    make_unit_dict must always return a dict, never raise."""
    weird = "Special: 😀 ½ month free " * 200  # very long, unicode
    u = make_unit_dict(floor_plan_name="L1", concession=weird)
    assert "concession_text" in u
    # Raw preserved (no truncation here — caller is responsible for caps)
    assert u["concession_text"] == weird
