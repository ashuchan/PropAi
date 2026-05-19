"""Generalization proof: 100 synthetic varied concessions whose expected
``concessions_json`` is derived from the GENERATOR's parameters (not the
parser). Parser must reproduce it exactly. Seeded -> reproducible.

Stays within the empirically-closed structure space (grind 2026-05-19):
free-time (digit/spelled, month/week), $-off-first-month (one-time),
$-off-rent (recurring), leaseTerm, combos, marketing-noise wrappers,
and true-negatives. No invented structures.
"""
import random

from ma_poc.core.concession_normalize import normalize_concession

_SPELL = {1: "one", 2: "two", 3: "three", 4: "four", 6: "six", 8: "eight"}


def _num(n, rng):
    return rng.choice([str(n), _SPELL[n]]) if n in _SPELL else str(n)


def _wrap(core, rng):
    """Marketing-noise wrappers that must NOT change the parsed result."""
    w = rng.choice([
        "{c}", "{c}", "LEASE TODAY & GET {c}!", "For a limited time: {c}",
        "Move in by 5/31 and {c}!", "{c} **Conditions apply",
        "{c} Restrictions apply, contact the leasing office for details.",
        "\U0001F389 Special: {c}", "Reduced Rent PLUS {c}",
        "{c} Offer subject to change without notice.",
    ]).format(c=core)
    return w.upper() if rng.random() < 0.3 else w


def _lease(rng):
    t = rng.choice([12, 13, 14, 15])
    phr = rng.choice([
        f" on a {t}-{t + 2} month lease", f" with {t} month lease",
        f" when you sign a {t}+ month lease", f" ({t} month lease required)",
    ])
    return phr, t


def _dollar(rng):
    x = rng.choice([250, 500, 750, 800, 1000, 1200, 1500, 2000])
    s = f"${x:,}" if (x >= 1000 and rng.random() < 0.5) else f"${x}"
    return s, x


def gen_case(rng):
    kind = rng.choice([
        "free_m", "free_w", "free_bare", "first_m",
        "off_first", "off_rent", "combo", "neg",
    ])
    if kind == "neg":
        return rng.choice([
            "Now offering FLEX - split your rent into smaller payments.",
            "Self-Guided Tours Available!", "Pet-friendly community with dog park.",
            "$99 application fee due at signing.", "$300 pet deposit required.",
            "Schedule a tour today! Top 10 of 2025 award winner.",
            "Resort-style pool and 24-hour fitness center.",
        ]), None

    obj = {}
    if kind == "free_m":
        n = rng.choice([1, 2, 3])
        core = rng.choice([
            f"{_num(n, rng)} month{'s' if n > 1 else ''} free",
            f"{_num(n, rng)} months free base rent",
            f"get {_num(n, rng)} month{'s' if n > 1 else ''} of free rent",
            f"{_num(n, rng)} months of complimentary rent",
        ])
        obj["free"] = {"monthsDiscounted": n}
    elif kind == "free_w":
        w = rng.choice([2, 4, 6, 8])
        core = rng.choice([
            f"{_num(w, rng)} weeks free", f"get {_num(w, rng)} weeks FREE rent",
            f"{_num(w, rng)} weeks of free rent",
        ])
        m = w / 4.0
        obj["free"] = {"monthsDiscounted": int(m) if m == int(m) else m}
    elif kind == "free_bare":
        core = rng.choice(["1 free month", "one month free", "free month"])
        obj["free"] = {"monthsDiscounted": 1}
    elif kind == "first_m":
        n = rng.choice([1, 2])
        if n == 1:
            core = rng.choice([
                "first full month free", "your first full month of rent free",
                "one month free (first full month)",
            ])
        else:
            core = rng.choice([
                f"first {_num(2, rng)} full months of rent free",
                f"1st {_num(2, rng)} full months are free",
            ])
        obj["free"] = {"monthsDiscounted": n}
    elif kind == "off_first":
        s, x = _dollar(rng)
        core = rng.choice([
            f"{s} off your first full month",
            f"{s} off first month's rent",
            f"{s} off move-in",
        ])
        obj["free"] = {"dollarsLow": x, "monthsDiscounted": 1}
    elif kind == "off_rent":
        s, x = _dollar(rng)
        core = rng.choice([
            f"{s} off rent", f"{s} off your monthly rent",
            f"{s} off monthly rent for a year",
        ])
        obj["rr"] = {"dollarsLow": x}
    else:  # combo: free weeks/months + recurring $ off rent
        w = rng.choice([4, 8])
        s, x = _dollar(rng)
        core = f"{_num(w, rng)} weeks free + reduced rate ({s} off rent)"
        m = w / 4.0
        obj["free"] = {"monthsDiscounted": int(m) if m == int(m) else m}
        obj["rr"] = {"dollarsLow": x}

    text = _wrap(core, rng)
    if rng.random() < 0.4:
        phr, t = _lease(rng)
        text += phr
        obj["leaseTerm"] = t
    return text, {"obj": obj}


def _generate(n=100, seed=424242):
    rng = random.Random(seed)
    return [gen_case(rng) for _ in range(n)]


def test_fuzz_100_generalization():
    cases = _generate(100)
    fails = []
    for text, expected in cases:
        got = normalize_concession(text)
        if got != expected:
            fails.append(f"\n  TXT: {text[:90]!r}\n  exp: {expected}\n  got: {got}")
    assert not fails, f"{len(fails)}/100 mismatches:" + "".join(fails[:25])


# Adversarial: realistic phrasings that do NOT mirror the parser regexes.
# In-contract ones must parse; genuinely out-of-contract ones MUST return
# None (capture-first keeps the raw text — None is correct, not a bug).
ADVERSARIAL = [
    ("Two months' rent on us!", {"obj": {"free": {"monthsDiscounted": 2}}}),
    ("Rent-free for 6 weeks", {"obj": {"free": {"monthsDiscounted": 1.5}}}),
    ("6-weeks-free!", {"obj": {"free": {"monthsDiscounted": 1.5}}}),
    ("FREE 2 MONTHS on select homes", {"obj": {"free": {"monthsDiscounted": 2}}}),
    ("receive 1.5 months free", {"obj": {"free": {"monthsDiscounted": 1.5}}}),
    ("two weeks free rent", {"obj": {"free": {"monthsDiscounted": 0.5}}}),
    ("Sign a 13 month lease and get one month free",
     {"obj": {"free": {"monthsDiscounted": 1}, "leaseTerm": 13}}),
    ("Look & lease for $750 off first full month",
     {"obj": {"free": {"dollarsLow": 750, "monthsDiscounted": 1}}}),
    ("8 weeks free + $99 admin fee", {"obj": {"free": {"monthsDiscounted": 2}}}),
    ("FIRST TWO MONTHS FREE!", {"obj": {"free": {"monthsDiscounted": 2}}}),
    ("$1,500 off your move-in costs",
     {"obj": {"free": {"dollarsLow": 1500, "monthsDiscounted": 1}}}),
    # Out-of-contract — None is the honest correct answer (raw retained):
    ("Ask about our current specials!", None),
    ("Contact us for move-in specials", None),
    ("Half off your first month", None),
    ("$50 off per month for 12 months", None),
    ("60 days free", None),
    ("$1,000 credit toward your first month rent", None),
]


def test_adversarial_divergent_phrasings():
    fails = []
    for text, expected in ADVERSARIAL:
        got = normalize_concession(text)
        if got != expected:
            fails.append(f"\n  TXT: {text!r}\n  exp: {expected}\n  got: {got}")
    assert not fails, f"{len(fails)} adversarial mismatches:" + "".join(fails)
