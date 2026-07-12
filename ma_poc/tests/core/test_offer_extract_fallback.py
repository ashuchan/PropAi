"""Offer-extract normalisation + fallback taxonomy (2026-07-12).

The no-offer_type decomposition on the 2026-07-11 canary found 10,838
texted units with no structured type. The mechanical classes and their
fixes, all STRICTLY ADDITIVE (raw text classifies first; already-typed
texts keep their exact pre-fix labels/values — replay measured 0/38,537
changed):

- HTML entities: ``Look &amp; Lease`` / ``$250&nbsp;off``  → unescape
- hyphenated durations: ``Up to 1-Month Free``             → hyphen→space
- possessive durations: ``3 Month's Base Rent FREE``       → strip 's
- flat move-in price: ``$99 MOVE IN SPECIAL``              → new fallback
  type ``move_in_special`` (fallback-only: inline it re-labels
  multi-offer texts — 2,106-unit churn measured)
- worded fraction: ``half off``                            → percent_off
  50% (fallback-only for the same reason)

Replay on the canary: 2,266/10,838 no-type units now classify
(free_rent 1150, look_and_lease 547, move_in_special 508, dollar_off 34,
percent_off 27).
"""
from __future__ import annotations

from ma_poc.core.offer_extract import extract_offer


def test_html_entity_look_and_lease():
    o = extract_offer("Look &amp; Lease Special — tour today!")
    assert o["offer_type"] == "look_and_lease"


def test_nbsp_dollar_off():
    o = extract_offer("Leasing Special! $250&nbsp;off the first three months")
    assert o["offer_type"] == "dollar_off"
    assert o["offer_value"] == "$250"


def test_hyphenated_month_free():
    o = extract_offer("Get Up To 1-Month Free on a 12-Month Lease")
    assert o["offer_type"] == "free_rent"
    assert o["offer_value"] == "1 month"


def test_hyphenated_weeks_free():
    o = extract_offer("Enjoy Up to 8-Weeks Free Off Base Rent!*")
    assert o["offer_type"] == "free_rent"
    assert o["offer_value"] == "8 weeks"


def test_possessive_months_base_rent_free():
    o = extract_offer("Lease Today & Receive 3 Month's Base Rent FREE!")
    assert o["offer_type"] == "free_rent"
    assert o["offer_value"] == "3 months"


def test_move_in_special_flat_price():
    o = extract_offer("$99 MOVE IN SPECIAL GOING ON NOW!!!")
    assert o["offer_type"] == "move_in_special"
    assert o["offer_value"] == "$99"


def test_move_in_for_only():
    o = extract_offer("Move In for Only $500 on select homes")
    assert o["offer_type"] == "move_in_special"
    assert o["offer_value"] == "$500"


def test_half_off_maps_percent_50():
    o = extract_offer("Half off first month rent when you lease our Greenwood unit!")
    assert o["offer_type"] == "percent_off"
    assert o["offer_value"] == "50%"


def test_multi_offer_priority_unchanged():
    # STRICT additivity: texts that already classified keep their label —
    # "half off" must NOT hijack a text that classifies free_rent on raw.
    o = extract_offer("One Month Free and Half off Administrative Fees!")
    assert o["offer_type"] == "free_rent"


def test_plain_free_rent_unchanged():
    o = extract_offer("6 weeks FREE rent on select floorplans")
    assert o["offer_type"] == "free_rent"
    assert o["offer_value"] == "6 weeks"


def test_no_offer_still_none():
    o = extract_offer("Stainless appliances, in-unit laundry, pet friendly")
    assert o["offer_type"] is None
    assert o["offer_value"] is None
