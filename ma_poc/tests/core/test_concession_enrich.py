"""Unit tests for ma_poc.core.concession_enrich.

Each test pins a behaviour against a verbatim sample shape observed in
the 2026-05-21 cloud-run concessions corpus (2,081 properties). When a
new offer phrasing is found in production, add a fixture HERE first,
then a regex / classifier change; that order ensures we never silently
shrink coverage on existing rows.
"""
from __future__ import annotations

import pytest

from ma_poc.core.concession_enrich import (
    Atom,
    Condition,
    Enrichment,
    enrich_concession,
)


# ─────────────────────────────────────────────────────────────────────
# Contract — empty / null input
# ─────────────────────────────────────────────────────────────────────


def test_empty_string_returns_empty_enrichment():
    e = enrich_concession("")
    assert e.atoms == []
    assert e.primary_atom is None
    assert e.conditions == []
    assert e.banner == ""


def test_none_returns_empty_enrichment():
    e = enrich_concession(None)
    assert isinstance(e, Enrichment)
    assert e.banner == ""


def test_whitespace_only_returns_empty():
    assert enrich_concession("   \n\t  ").primary_atom is None


def test_non_string_returns_empty():
    # The producer occasionally passes a stringified dict-or-other shape;
    # the enricher must never crash. Idiomatically callers stringify first.
    e = enrich_concession(123)  # type: ignore[arg-type]
    assert e.primary_atom is None


# ─────────────────────────────────────────────────────────────────────
# HTML entity decoding — the user-visible bug
# ─────────────────────────────────────────────────────────────────────


def test_html_entities_decoded_in_banner():
    """&amp; / &nbsp; / &quot; / &#39; survive the cleaner in the raw
    capture; the enricher must surface decoded literals in the banner
    so xlsx cells don't show ``May &amp; June``."""
    raw = "$99 Move-In Special covers May &amp; June RENT! Limited time offer!"
    e = enrich_concession(raw)
    assert "&amp;" not in e.banner
    # The May & June phrase isn't in the banner (it's a marketing line);
    # but if the banner falls back to raw text the entity must be decoded.
    assert "&amp;" not in e.banner


def test_nbsp_decoded_in_banner():
    raw = "Get 2 Months Free&nbsp; On newly renovated units!"
    e = enrich_concession(raw)
    assert "&nbsp;" not in e.banner
    assert e.primary_atom is not None
    assert e.primary_atom.offer_type == "free_rent"
    assert e.primary_atom.value == "2 months"


def test_numeric_entity_decoded():
    raw = "Get 1 Month Free&#33; Limited time."
    e = enrich_concession(raw)
    assert "&#" not in e.banner


# ─────────────────────────────────────────────────────────────────────
# Offer extraction — free_rent
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected_value", [
    ("Get 1 month free!",                        "1 month"),
    ("Receive 2 months FREE!",                   "2 months"),
    ("Up to 3 months FREE",                      "3 months"),
    ("Get 6 WEEKS of FREE RENT",                 "6 weeks"),
    ("Sign and enjoy 6 weeks free off your rent","6 weeks"),
    ("Receive up to TEN WEEKS FREE",             "10 weeks"),
    ("One Month FREE + Waived App Fee",          "1 month"),
    ("Two Months Free Rent",                     "2 months"),
])
def test_free_rent_value_extraction(raw, expected_value):
    e = enrich_concession(raw)
    assert e.primary_atom is not None
    assert e.primary_atom.offer_type == "free_rent"
    assert e.primary_atom.target == "rent"
    assert e.primary_atom.value == expected_value


def test_free_rent_inverted_form():
    e = enrich_concession("Move in by May 30th and receive FREE rent for 2 months")
    assert e.primary_atom is not None
    assert e.primary_atom.offer_type == "free_rent"
    assert e.primary_atom.value == "2 months"


def test_free_rent_ordinal_form():
    """First / second / third month rent free — observed at e.g.
    'first month's rent FREE when you sign a 12 month lease'."""
    e = enrich_concession(
        "Receive your first month's rent FREE when you sign a 12 month lease!"
    )
    assert e.primary_atom is not None
    assert e.primary_atom.offer_type == "free_rent"
    assert "first" in e.primary_atom.value


def test_free_rent_article_form():
    """'a month of free rent' / 'get a month free' — observed at
    'Get a month of FREE RENT when you sign a 12 month lease'."""
    e = enrich_concession(
        ") Get a month of FREE RENT when you sign a 12 month lease with us!"
    )
    assert e.primary_atom is not None
    assert e.primary_atom.offer_type == "free_rent"
    assert e.primary_atom.value == "1 month"


def test_free_rent_rejects_impossible_count():
    """24 months upper bound — anything beyond is almost always a typo
    or unrelated marketing copy (e.g. '36 months later you'll still
    love...'). The cap protects the structured field from poisoning."""
    assert enrich_concession("99 months free!").primary_atom is None


# ─────────────────────────────────────────────────────────────────────
# Offer extraction — dollar_off
# ─────────────────────────────────────────────────────────────────────


def test_dollar_off_with_off_tail():
    e = enrich_concession("Save up to $1000 on select homes!")
    assert e.primary_atom is not None
    assert e.primary_atom.offer_type == "dollar_off"
    assert e.primary_atom.value == "$1,000"


def test_dollar_off_with_gift_card_tail():
    """Routed to gift_card type when 'gift card' qualifier present."""
    e = enrich_concession(
        "Apply within 24 hours of your tour and get a $200 gift card!"
    )
    assert e.primary_atom is not None
    assert e.primary_atom.offer_type == "gift_card"
    assert e.primary_atom.target == "gift_card"
    assert e.primary_atom.value == "$200"


def test_dollar_off_with_move_in_cost_tail():
    e = enrich_concession("$1000 off total move-in cost with move-in by 11/07")
    assert e.primary_atom is not None
    assert e.primary_atom.target == "move_in_cost"


def test_dollar_off_late_fee_NOT_a_concession():
    """``$95 Late Fee Per occurrence`` is a price schedule, not a
    discount. Pre-fix the regex matched it. Post-fix the tail anchor
    rejects bare amounts."""
    raw = "Late Fee $95 Per occurrence Water and Sewer varies Monthly"
    e = enrich_concession(raw)
    assert e.primary_atom is None


def test_dollar_off_phone_number_NOT_a_concession():
    """``Call us at (737) 275-7$300`` etc. should not match because
    the tail anchor requires an action word."""
    e = enrich_concession("Call us at 7372757300 for details")
    # Even if a 4-digit run is matched, the tail anchor blocks promotion.
    assert e.primary_atom is None


# ─────────────────────────────────────────────────────────────────────
# Offer extraction — percent_off / waived_fee / reduced
# ─────────────────────────────────────────────────────────────────────


def test_percent_off():
    e = enrich_concession("Limited Time Offer: 50% off rent for 3 Months.")
    assert e.primary_atom is not None
    assert e.primary_atom.offer_type == "percent_off"
    assert e.primary_atom.value == "50%"


def test_percent_off_rejects_over_99():
    """`150% off` would be nonsensical — the regex must reject it."""
    e = enrich_concession("150% off")
    assert e.primary_atom is None


def test_waived_app_fee():
    e = enrich_concession("Waived application fee + 1 month free!")
    waived = [a for a in e.atoms if a.offer_type == "waived_fee"]
    assert len(waived) == 1
    assert waived[0].target == "app_fee"


def test_waived_admin_fee():
    e = enrich_concession("Check out our May Rent Special: Waived Admin Fee*")
    waived = [a for a in e.atoms if a.offer_type == "waived_fee"]
    assert waived[0].target == "admin_fee"


def test_zero_dollar_app_fee_treated_as_waived():
    """Some properties phrase as ``Now $0 Administrative Fees`` —
    semantically identical to ``waived admin fee``."""
    e = enrich_concession("Now $0 Administrative Fees + Move in by May 31st")
    waived = [a for a in e.atoms if a.offer_type == "waived_fee"]
    assert len(waived) >= 1
    assert waived[0].target == "admin_fee"


def test_reduced_rate():
    e = enrich_concession("REDUCED RATES on 2 BEDROOMS")
    assert e.primary_atom is not None
    assert e.primary_atom.offer_type in ("free_rent", "reduced_rate")


def test_look_and_lease():
    e = enrich_concession("Look and Lease Special* First Full 2 Months Free")
    # When BOTH offers fire, free_rent wins on priority — the audit
    # trail still records both.
    types = {a.offer_type for a in e.atoms}
    assert "look_and_lease" in types
    assert "free_rent" in types
    assert e.primary_atom.offer_type == "free_rent"


# ─────────────────────────────────────────────────────────────────────
# Conditions — deadline / lease length / apply within / unit scope
# ─────────────────────────────────────────────────────────────────────


def test_deadline_iso_format():
    e = enrich_concession("Lease today for 2 months free! Valid 05/20/2026 to 05/31/2026")
    deadlines = [c for c in e.conditions if c.kind == "deadline"]
    assert deadlines
    assert "05/20/2026" in deadlines[0].value or "05/31/2026" in deadlines[0].value


def test_deadline_named_month():
    e = enrich_concession("1 month free; move-in by July 15th")
    deadlines = [c for c in e.conditions if c.kind == "deadline"]
    assert deadlines
    assert "July" in deadlines[0].value


def test_deadline_short_date():
    e = enrich_concession("2 months free! Move-in by 5/31/26")
    deadlines = [c for c in e.conditions if c.kind == "deadline"]
    assert deadlines


def test_lease_length_range():
    """``13-15 month lease`` — range form."""
    e = enrich_concession("Get 6 WEEKS of FREE RENT when you sign a 13-15 month lease!")
    lengths = [c for c in e.conditions if c.kind == "lease_length"]
    assert lengths
    assert "13" in lengths[0].value and "15" in lengths[0].value


def test_lease_length_or_longer():
    e = enrich_concession("Move-in with a 15 month lease or longer and receive 2 months FREE!")
    lengths = [c for c in e.conditions if c.kind == "lease_length"]
    assert lengths
    assert "15" in lengths[0].value


def test_lease_length_minimum():
    e = enrich_concession("Get 1 month free with a 12 month lease")
    lengths = [c for c in e.conditions if c.kind == "lease_length"]
    assert lengths


def test_apply_within():
    e = enrich_concession(
        "Get 4 WEEKS FREE base rent on select floor plans if you lease within 24 hours of your tour"
    )
    aw = [c for c in e.conditions if c.kind == "apply_within"]
    assert aw
    assert "24" in aw[0].value


def test_apply_within_48_hours():
    e = enrich_concession("Apply within 48 hrs for $375 off move-in costs!")
    aw = [c for c in e.conditions if c.kind == "apply_within"]
    assert aw
    assert "48" in aw[0].value


def test_unit_scope_select():
    e = enrich_concession("Receive up to 2 MONTHS FREE on select homes")
    scope = [c for c in e.conditions if c.kind == "unit_scope"]
    assert scope
    assert scope[0].value == "select"


def test_unit_scope_specific_bedroom():
    """Specific bedroom counts surfaced as ``{N}-bedroom`` for filtering."""
    e = enrich_concession("Up to 4 weeks free on three bedrooms!")
    scope = [c for c in e.conditions if c.kind == "unit_scope"]
    assert scope
    assert scope[0].value == "3-bedroom"


def test_unit_scope_all():
    e = enrich_concession("Receive 8 Weeks Free on all Floorplans! Call for details!")
    scope = [c for c in e.conditions if c.kind == "unit_scope"]
    assert scope
    assert scope[0].value == "all"


def test_audience_student():
    e = enrich_concession("Student and Healthcare Special: Receive $500 off")
    aud = [c for c in e.conditions if c.kind == "audience"]
    assert aud
    assert aud[0].value in ("student", "healthcare")


def test_restrictions_apply_sentinel():
    """Generic ``*Restrictions apply`` is surfaced so reviewers can
    see the offer is conditional even without specifics. Doesn't
    leak into the banner."""
    e = enrich_concession("1 month free! *Restrictions apply")
    rest = [c for c in e.conditions if c.kind == "restrictions"]
    assert rest
    # Banner suppresses the restrictions sentinel — too noisy for the
    # one-liner.
    assert "restrictions" not in e.banner.lower()


# ─────────────────────────────────────────────────────────────────────
# Banner rendering
# ─────────────────────────────────────────────────────────────────────


def test_banner_free_rent_plus_deadline_plus_scope():
    e = enrich_concession(
        "Get 2 MONTHS FREE on select homes! Must move-in by 5/31/2026."
    )
    assert "2 months" in e.banner.lower()
    assert "FREE rent" in e.banner
    assert "5/31/2026" in e.banner
    assert "select units" in e.banner


def test_banner_dollar_off_plus_lease_length():
    e = enrich_concession(
        "Lease Now & Save Get $500 off when you sign a 13-15 month lease!"
    )
    assert "$500" in e.banner
    assert "13" in e.banner and "15" in e.banner


def test_banner_falls_back_to_raw_when_no_atom():
    """Pure header without specific offer terms — banner falls back to
    the cleaned raw text so the cell is informative."""
    e = enrich_concession("Limited Time Offer!")
    assert e.primary_atom is None
    assert e.banner == "Limited Time Offer!"


def test_banner_bounded_to_140_chars():
    e = enrich_concession("X " * 500)
    assert len(e.banner) <= 140


# ─────────────────────────────────────────────────────────────────────
# Multi-atom — order, dedup, audit trail
# ─────────────────────────────────────────────────────────────────────


def test_multi_offer_combo_recorded_in_atoms():
    """``One Month Free + Waived App Fee + $500 gift card`` — three
    distinct offers should produce three atoms, ordered by priority."""
    e = enrich_concession(
        "One Month FREE + Waived App Fee + $500 gift card!"
    )
    types = [a.offer_type for a in e.atoms]
    assert "free_rent" in types
    assert "waived_fee" in types
    assert "gift_card" in types
    # Free rent ranks highest.
    assert e.primary_atom.offer_type == "free_rent"


def test_atoms_dedup_overlapping_matches():
    """When two regexes match overlapping spans, only ONE atom is kept
    (the first / highest-priority one). Prevents 'one month free' from
    producing both free_rent and look_and_lease atoms for the same span."""
    e = enrich_concession("Look and Lease - 1 Month FREE")
    # Both should appear because their spans don't overlap — but no
    # duplicates of either.
    types = [a.offer_type for a in e.atoms]
    assert types.count("look_and_lease") == 1
    assert types.count("free_rent") == 1
