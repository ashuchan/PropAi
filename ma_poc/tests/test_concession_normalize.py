"""Regression oracle = the 20 worked examples (authoritative).

If concession_normalize disagrees with any row, the normalizer is wrong.
"""
from ma_poc.core.concession_normalize import normalize_concession

# (raw_text, expected concessions_json) — verbatim from the spec table.
CASES = [
    ("Move in by 4/30 and get $800 off your first full month!",
     {"obj": {"free": {"dollarsLow": 800, "monthsDiscounted": 1}}}),
    ("ONE MONTH FREE RENT!! Contact the leasing office for more details.",
     {"obj": {"free": {"monthsDiscounted": 1}}}),
    ("For a limited time: ONE MONTH FREE RENT!! Contact the leasing office for more details.",
     {"obj": {"free": {"monthsDiscounted": 1}}}),
    ("Move in now and receive your first full month of rent free on a 12-14 month lease!",
     {"obj": {"free": {"monthsDiscounted": 1}, "leaseTerm": 12}}),
    ("Move in now and receive your first two full months of rent free on a 12-14 month lease!",
     {"obj": {"free": {"monthsDiscounted": 2}, "leaseTerm": 12}}),
    ("3 Months Free Base Rent",
     {"obj": {"free": {"monthsDiscounted": 3}}}),
    ("Receive $100 off your monthly rent for a year when you sign a 12+ month lease. "
     "Heat, water, and trash included in rent. Must move in by 5.31.26. Offer subject "
     "to termination or change without notice.",
     {"obj": {"rr": {"dollarsLow": 100}, "leaseTerm": 12}}),
    ("Receive $100 off your monthly rent for a year when you sign a 12+ month lease.",
     {"obj": {"rr": {"dollarsLow": 100}, "leaseTerm": 12}}),
    ("Get up to 2 weeks FREE when you move in by 5/31",
     {"obj": {"free": {"monthsDiscounted": 0.5}}}),
    ("1st TWO full months are free",
     {"obj": {"free": {"monthsDiscounted": 2}}}),
    ("Reduced Rent PLUS 6 Weeks FREE!",
     {"obj": {"free": {"monthsDiscounted": 1.5}}}),
    ("1 FREE MONTH!",
     {"obj": {"free": {"monthsDiscounted": 1}}}),
    ("LEASE TODAY & GET 1 FREE MONTH!",
     {"obj": {"free": {"monthsDiscounted": 1}}}),
    ("Lease today and get 4 weeks Free **Conditions apply",
     {"obj": {"free": {"monthsDiscounted": 1}}}),
    ("LEASE TODAY & GET 1 FREE MONTH! Conditions Apply, call leasing office for details.",
     {"obj": {"free": {"monthsDiscounted": 1}}}),
    ("4 Weeks Free + Reduced Rate ($25 Off Rent)!",
     {"obj": {"rr": {"dollarsLow": 25}, "free": {"monthsDiscounted": 1}}}),
    ("$1,000 Look and Lease Special! Tour, lease within 24 hours, and move in by "
     "April 30th to receive $1,000 off the first full month's rent. Restrictions "
     "apply. Contact leasing for details.",
     {"obj": {"free": {"dollarsLow": 1000, "monthsDiscounted": 1}}}),
    ("One Month Free with 13 month lease!",
     {"obj": {"free": {"monthsDiscounted": 1}, "leaseTerm": 13}}),
    ("Get up to 2 MONTH FREE, plus only $99 for application and admin fees when you "
     "look and lease! You must tour and apply within 48 hours for this special to apply.",
     {"obj": {"free": {"monthsDiscounted": 2}}}),
    ("One Month Free! (First Full Month)",
     {"obj": {"free": {"monthsDiscounted": 1}}}),
]

# Must NOT produce a concession (true negatives from the live grind).
TRUE_NEGATIVES = [
    "Now offering FLEX - A smarter way to pay rent! Split your rent into smaller payments.",
    "Self-Guided Tours Available!",
    "TOP 10 OF 2025 AWARD WINNER",
    "$99 application fee due at signing",
]


def test_twenty_oracle_examples_exact():
    failures = []
    for raw, expected in CASES:
        got = normalize_concession(raw)
        if got != expected:
            failures.append(f"\n  RAW: {raw[:70]!r}\n  exp: {expected}\n  got: {got}")
    assert not failures, "Oracle mismatches:" + "".join(failures)


def test_true_negatives_return_none():
    for raw in TRUE_NEGATIVES:
        assert normalize_concession(raw) is None, f"false positive: {raw!r} -> {normalize_concession(raw)}"


def test_empty_and_garbage_safe():
    for raw in (None, "", "   ", 123, "nice pool and gym"):
        assert normalize_concession(raw) is None
