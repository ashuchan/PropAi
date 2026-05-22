"""Deterministic concession enrichment — offer type, target, conditions, banner.

Where :mod:`ma_poc.core.concession_normalize` returns ONE structured dict
keyed on the first-matching shape, this module emits a richer record:

* ``atoms`` — every recognised offer the text mentions (so a ``"1 month
  free + waived app fee"`` row produces TWO atoms, not one).
* ``primary_atom`` — the highest-priority atom for short-form display.
* ``conditions`` — ordered list of structured constraints (deadline,
  lease length, apply-within window, unit scope, audience, promo code,
  generic "restrictions apply" sentinel).
* ``banner`` — a short, human-readable single-line render of
  ``primary_atom`` + the most salient conditions. Bounded to ~140 chars.

Why a new module instead of extending ``normalize_concession``?

* :func:`normalize_concession` is pinned by ~27 tests against an
  intentionally narrow shape. Extending it would either bloat the dict
  every existing consumer reads, or break those consumers entirely.
* The new structure is consumed by xlsx export + (eventually) the
  property-detail UI. Both want the full breakdown, not just "the
  highest-priority shape." Keeping the two modules separate means each
  caller picks the shape that fits.

Empirical pattern coverage on the 2026-05-21 cloud run (2,081 captured
concessions): see ``tests/core/test_concession_enrich.py`` for the
real-text fixtures the regex library was tuned against.

Source-of-truth invariant — *raw is never replaced*: ``enrich_concession``
NEVER mutates the input. The caller's ``concessions`` / ``concession_text``
field remains the system of record. The enrichment is a derivative.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ma_poc.core.concession_clean import _decode_html_entities

# ─────────────────────────────────────────────────────────────────────
# Number-word helper (mirrors normalize_concession so phrasing is
# identical across modules — "two months free" parses the same as
# "2 months free").
# ─────────────────────────────────────────────────────────────────────

_NUM_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}


def _to_int(token: str) -> int | None:
    t = token.strip().lower()
    if t.isdigit():
        try:
            return int(t)
        except ValueError:
            return None
    return _NUM_WORDS.get(t)


def _amount_to_int(token: str) -> int | None:
    digits = re.sub(r"[^\d]", "", token)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# ─────────────────────────────────────────────────────────────────────
# Output dataclasses
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Atom:
    """A single recognised offer inside the concession text.

    ``offer_type`` — canonical taxonomy:
        free_rent | dollar_off | percent_off | waived_fee
        | reduced_rate | reduced_deposit | gift_card | look_and_lease

    ``target`` — what the discount is APPLIED to:
        rent | deposit | app_fee | admin_fee | move_in_cost
        | gift_card | utilities | other

    ``value`` — short display string for the offer magnitude
        ("2 months", "$500", "10%"). May be empty for waived_fee /
        look_and_lease where the offer is qualitative.

    ``raw`` — verbatim substring matched (whitespace-normalised), so
    downstream callers can audit the producer text.
    """
    offer_type: str
    target: str
    value: str
    raw: str
    priority: int


@dataclass(frozen=True)
class Condition:
    """A structured constraint extracted alongside an offer.

    ``kind`` — taxonomy:
        deadline       — must move-in / lease by date X
        lease_length   — minimum / maximum lease term
        apply_within   — speed bonus (apply within N hours of tour)
        unit_scope     — applies to "select" vs "all" units
        audience       — student / healthcare / military / new resident
        promo_code     — explicit code or "mention X" instruction
        restrictions   — generic "restrictions apply" sentinel; surface
                         so reviewers know the offer is conditional even
                         when the text doesn't name the specifics

    ``value`` — canonical short form. None for purely qualitative
    sentinels (e.g. ``restrictions`` carries no value).

    ``raw`` — verbatim source phrase for audit.
    """
    kind: str
    value: str | None
    raw: str


@dataclass
class Enrichment:
    """Top-level enrichment record returned by :func:`enrich_concession`."""
    atoms: list[Atom] = field(default_factory=list)
    primary_atom: Atom | None = None
    conditions: list[Condition] = field(default_factory=list)
    banner: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "atoms": [asdict(a) for a in self.atoms],
            "primary_atom": asdict(self.primary_atom) if self.primary_atom else None,
            "conditions": [asdict(c) for c in self.conditions],
            "banner": self.banner,
        }


# ─────────────────────────────────────────────────────────────────────
# Offer extraction
# ─────────────────────────────────────────────────────────────────────
#
# Priority drives BOTH ordering inside ``atoms`` AND selection of
# ``primary_atom`` for the banner. Higher = more salient to a reviewer
# scanning a one-line summary.
#
#   100  free_rent             "2 months free rent" — anchored time
#    90  dollar_off > $200     "$500 off" — concrete dollar value
#    85  percent_off           "50% off rent"
#    80  gift_card             "$200 gift card"
#    70  waived_fee            "waived app fee"
#    65  reduced_rate          "reduced rates" — magnitude unspecified
#    60  reduced_deposit
#    55  look_and_lease        "Look & lease special" — qualitative
#    50  dollar_off  <= $200   small "$99 special" — usually move-in
#                              cost rather than rent value
#
# The ``raw`` substring is captured so the xlsx audit cell shows
# exactly what we matched.

# Free rent — N weeks/months/days free [rent]
_FREE_PERIOD_RE = re.compile(
    r"\b(?:up\s+to\s+|receive\s+(?:up\s+to\s+)?)?"
    r"(?P<n>\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"[\s\-]+(?:full[\s\-]+)?"
    r"(?P<unit>weeks?|months?|days?)"
    r"(?:[\s\-]+of)?[\s\-]+"
    r"(?:rent[\s\-]+|base[\s\-]+rent[\s\-]+)?"
    r"(?:free|of[\s\-]+free|on[\s\-]+us|complimentary)",
    re.IGNORECASE,
)
# Inverted: "free rent for 2 months" / "FREE on select homes"
_FREE_PERIOD_INVERTED_RE = re.compile(
    r"\b(?:rent[\s\-]+)?free\s+(?:rent\s+)?(?:for\s+)?"
    r"(?P<n>\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"[\s\-]+(?:full[\s\-]+)?(?P<unit>weeks?|months?|days?)",
    re.IGNORECASE,
)

# Dollar amount. The trailing capture lets us guess the TARGET:
#   "$500 off [your rent]"     → rent / move-in cost (default)
#   "$200 gift card"           → gift_card
#   "$1000 off move-in cost"   → move_in_cost
#   "$X free rent"             → rent (but ``rent`` should be free_rent)
_DOLLAR_OFF_RE = re.compile(
    # The amount must be preceded by a "save / save up to / up to"
    # concession cue OR followed by a concession-action tail. This
    # prevents Late-Fee schedules, phone-number tails, and bare price
    # points from registering as discounts.
    r"(?:"
    # Cue-prefix branch — anchor on save / up to / get / receive / enjoy.
    r"(?:save\s+(?:up\s+to\s+)?|up\s+to\s+|get\s+|receive\s+|enjoy\s+|score\s+|grab\s+|claim\s+)"
    r"\$\s*(?P<amount_cue>\d{2,3}(?:,\d{3})+|\d{2,6})"
    r"(?:\s+(?P<tail_cue>off(?:\s+your)?(?:\s+[a-z\-]+){0,4}"
    r"|in\s+free\s+rent"
    r"|free\s+rent"
    r"|gift\s*card"
    r"|credit"
    r"|cash\s+(?:back|bonus)?"
    r"|savings"
    r"|welcome\s+bonus"
    r"|move[\s\-]?in\s+(?:cost|credit|bonus|special)"
    r"|(?:first|second|third)?\s*(?:full\s+)?months?(?:'s)?\s+rent))?"
    r"|"
    # Tail-only branch — bare amount must be followed by an action.
    r"\$\s*(?P<amount>\d{2,3}(?:,\d{3})+|\d{2,6})"
    r"\s+(?P<tail>off(?:\s+your)?(?:\s+[a-z\-]+){0,4}"
    r"|in\s+free\s+rent"
    r"|free\s+rent"
    r"|gift\s*card"
    r"|credit"
    r"|cash\s+(?:back|bonus)?"
    r"|savings"
    r"|welcome\s+bonus"
    r"|move[\s\-]?in\s+(?:cost|credit|bonus|special)"
    r"|(?:first|second|third)?\s*(?:full\s+)?months?(?:'s)?\s+rent)"
    r")",
    re.IGNORECASE,
)

# "first/second month's rent free" — counts as 1 month FREE without a
# leading digit. Restricted to "rent" target so phone numbers / dates
# don't trigger.
_ORDINAL_MONTH_FREE_RE = re.compile(
    r"\b(?P<which>first|second|third|1st|2nd|3rd)\s+(?:full\s+)?months?(?:'s)?\s+(?:rent\s+)?free",
    re.IGNORECASE,
)

# "a month free" / "a week free" — qualitative form that the digit-
# anchored ``_FREE_PERIOD_RE`` skips. Treated as 1 unit.
_ARTICLE_PERIOD_FREE_RE = re.compile(
    r"\b(?:a|an|one)\s+(?:full\s+)?(?P<unit>weeks?|months?|days?)"
    r"(?:\s+of)?\s+(?:rent\s+|base\s+rent\s+)?free\s+rent\b|"
    r"\b(?:get|receive|enjoy|score|grab|claim)\s+(?:a|an|one)\s+(?:full\s+)?"
    r"(?P<unit2>weeks?|months?|days?)\s+(?:of\s+)?(?:rent\s+)?free",
    re.IGNORECASE,
)

# Percent. Word boundary on the left so "150% off" doesn't match as "50% off".
_PERCENT_OFF_RE = re.compile(
    r"(?<!\d)(?P<percent>\d{1,2}(?:\.\d+)?)\s*%\s*"
    r"(?P<tail>off(?:\s+[a-z\-]+){0,4}|reduction)",
    re.IGNORECASE,
)

# Waived fee. ``kind`` distinguishes app vs admin vs amenity vs deposit
# so the TARGET column is precise (admin_fee vs app_fee — operators
# triage them separately).
_WAIVED_FEE_RE = re.compile(
    r"(?:waived|no|\$0|\$\s*0)\s+"
    r"(?P<kind>app(?:lication)?|admin(?:istration|istrative)?|amenity|move[\s\-]?in|deposit|security)"
    r"\s*fees?",
    re.IGNORECASE,
)

# Generic reduced-rate phrases — "REDUCED RATES" / "reduced rent" — qualitative.
_REDUCED_RATE_RE = re.compile(r"\breduced\s+(?:rates?|rent)\b", re.IGNORECASE)
_REDUCED_DEPOSIT_RE = re.compile(r"\breduced\s+deposit\b|\bno\s+(?:security\s+)?deposit\b", re.IGNORECASE)
_LOOK_AND_LEASE_RE = re.compile(r"look[\s\-]+(?:and|&|n|\+)[\s\-]+lease", re.IGNORECASE)


def _gift_card_target_from_tail(tail: str) -> str:
    t = tail.lower()
    if "gift" in t:
        return "gift_card"
    if "move" in t and "in" in t:
        return "move_in_cost"
    if "rent" in t or "first" in t or "second" in t:
        return "rent"
    if "credit" in t or "cash" in t or "bonus" in t or "savings" in t:
        return "move_in_cost"
    if "off" in t:
        # bare "off" — most often rent or move-in cost
        return "rent"
    return "rent"


def _classify_waived_kind(kind_raw: str) -> str:
    k = kind_raw.lower()
    if "app" in k:
        return "app_fee"
    if "admin" in k:
        return "admin_fee"
    if "amenity" in k:
        return "amenity_fee"
    if "move" in k and "in" in k:
        return "move_in_cost"
    if "deposit" in k or "security" in k:
        return "deposit"
    return "other"


def _extract_atoms(text: str) -> list[Atom]:
    atoms: list[Atom] = []
    seen_spans: list[tuple[int, int]] = []

    def _overlap(start: int, end: int) -> bool:
        for s, e in seen_spans:
            if start < e and end > s:
                return True
        return False

    # Free-rent: most salient, scan first
    for pattern in (_FREE_PERIOD_RE, _FREE_PERIOD_INVERTED_RE):
        for m in pattern.finditer(text):
            if _overlap(m.start(), m.end()):
                continue
            n = _to_int(m.group("n"))
            if n is None or n <= 0 or n > 24:
                continue
            unit = m.group("unit").lower().rstrip("s")
            atoms.append(Atom(
                offer_type="free_rent",
                target="rent",
                value=f"{n} {unit}{'s' if n != 1 else ''}",
                raw=_whitespace(m.group(0)),
                priority=100,
            ))
            seen_spans.append((m.start(), m.end()))

    # "First / second month's rent free" — ordinal form.
    for m in _ORDINAL_MONTH_FREE_RE.finditer(text):
        if _overlap(m.start(), m.end()):
            continue
        which = m.group("which").lower()
        atoms.append(Atom(
            offer_type="free_rent",
            target="rent",
            value=f"{which} month",
            raw=_whitespace(m.group(0)),
            priority=100,
        ))
        seen_spans.append((m.start(), m.end()))

    # "A month / a week free" — article form ("Get a month of FREE RENT").
    for m in _ARTICLE_PERIOD_FREE_RE.finditer(text):
        if _overlap(m.start(), m.end()):
            continue
        unit = (m.group("unit") or m.group("unit2") or "").lower().rstrip("s")
        if not unit:
            continue
        atoms.append(Atom(
            offer_type="free_rent",
            target="rent",
            value=f"1 {unit}",
            raw=_whitespace(m.group(0)),
            priority=100,
        ))
        seen_spans.append((m.start(), m.end()))

    # Percent-off
    for m in _PERCENT_OFF_RE.finditer(text):
        if _overlap(m.start(), m.end()):
            continue
        try:
            pct = float(m.group("percent"))
        except (ValueError, TypeError):
            continue
        if pct <= 0 or pct > 99:
            continue
        tail = (m.group("tail") or "").lower()
        target = "rent" if "rent" in tail else "rent"
        atoms.append(Atom(
            offer_type="percent_off",
            target=target,
            value=f"{int(pct) if pct.is_integer() else pct}%",
            raw=_whitespace(m.group(0)),
            priority=85,
        ))
        seen_spans.append((m.start(), m.end()))

    # Dollar-off / gift-card. The regex has two alternatives — cue
    # prefix (``save $N``) or tail action (``$N off``); read whichever
    # named group fired.
    for m in _DOLLAR_OFF_RE.finditer(text):
        if _overlap(m.start(), m.end()):
            continue
        amt_raw = m.group("amount_cue") or m.group("amount")
        amt = _amount_to_int(amt_raw) if amt_raw else None
        if amt is None or amt < 20 or amt > 50_000:
            continue
        tail = (m.group("tail_cue") or m.group("tail") or "").strip()
        if "gift" in tail.lower():
            offer_type = "gift_card"
            target = "gift_card"
            priority = 80
        else:
            offer_type = "dollar_off"
            target = _gift_card_target_from_tail(tail)
            priority = 90 if amt > 200 else 50
        atoms.append(Atom(
            offer_type=offer_type,
            target=target,
            value=f"${amt:,}",
            raw=_whitespace(m.group(0)),
            priority=priority,
        ))
        seen_spans.append((m.start(), m.end()))

    # Waived fees
    for m in _WAIVED_FEE_RE.finditer(text):
        if _overlap(m.start(), m.end()):
            continue
        target = _classify_waived_kind(m.group("kind"))
        atoms.append(Atom(
            offer_type="waived_fee",
            target=target,
            value="",
            raw=_whitespace(m.group(0)),
            priority=70,
        ))
        seen_spans.append((m.start(), m.end()))

    # Reduced rate / deposit
    for m in _REDUCED_RATE_RE.finditer(text):
        if _overlap(m.start(), m.end()):
            continue
        atoms.append(Atom(
            offer_type="reduced_rate", target="rent", value="",
            raw=_whitespace(m.group(0)), priority=65,
        ))
        seen_spans.append((m.start(), m.end()))
    for m in _REDUCED_DEPOSIT_RE.finditer(text):
        if _overlap(m.start(), m.end()):
            continue
        atoms.append(Atom(
            offer_type="reduced_deposit", target="deposit", value="",
            raw=_whitespace(m.group(0)), priority=60,
        ))
        seen_spans.append((m.start(), m.end()))

    # Look & lease — qualitative only when no other concrete offer fired.
    # Some properties (e.g. "Look & Lease - $1000 off") have BOTH; we add
    # the look_and_lease atom so the audit trail is complete but priority
    # 55 keeps it below concrete offers in the banner.
    for m in _LOOK_AND_LEASE_RE.finditer(text):
        if _overlap(m.start(), m.end()):
            continue
        atoms.append(Atom(
            offer_type="look_and_lease", target="rent", value="",
            raw=_whitespace(m.group(0)), priority=55,
        ))
        seen_spans.append((m.start(), m.end()))

    atoms.sort(key=lambda a: -a.priority)
    return atoms


# ─────────────────────────────────────────────────────────────────────
# Condition extraction
# ─────────────────────────────────────────────────────────────────────

_DEADLINE_RE = re.compile(
    r"\b(?:"
    r"move[\s\-]?in\s+by|lease\s+by|sign(?:ed)?\s+(?:up\s+|in\s+|a\s+lease\s+)?by"
    r"|apply\s+(?:in\s+person\s+)?by"
    # ``valid 05/20/2026 to 05/31/2026`` — bare "valid" is the common
    # short form. Also handle "valid through/until/thru" and "good
    # through" / "ends".
    r"|valid(?:\s+(?:through|until|thru))?"
    r"|good\s+(?:through|until|thru)"
    r"|expires?(?:\s+on)?|ends?(?:\s+on)?"
    r")\s+"
    r"(?P<deadline>"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{2,4})?"
    r"|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?(?:\s+\d{2,4})?"
    r"|\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?"
    r")",
    re.IGNORECASE,
)

# Lease term — "13-15 month lease", "minimum 12 month", "15 month lease or longer", "12+ month".
_LEASE_LENGTH_RE = re.compile(
    r"\b(?:"
    r"(?P<lo>\d{1,2})\s*[\-–]\s*(?P<hi>\d{1,2})\s+month(?:s)?\s+lease"
    r"|(?P<mins>minimum\s+lease\s+(?:term\s+)?(?:of\s+)?(?P<minv>\d{1,2})\s+months?)"
    r"|(?P<plus>\d{1,2})\+?\s*month\s+lease(?:\s+or\s+(?:longer|more|greater))?"
    r"|(?P<orlonger>\d{1,2})\s+month\s+lease\s+or\s+(?:longer|more|greater)"
    r")",
    re.IGNORECASE,
)

# Speed bonus — "apply within 24 hours of tour", "within 48 hrs"
_APPLY_WITHIN_RE = re.compile(
    r"\b(?:apply|tour|lease)\s+within\s+(?P<n>\d{1,3})\s*(?P<unit>hours?|hrs?|days?)"
    r"|within\s+(?P<n2>\d{1,3})\s*(?P<unit2>hours?|hrs?|days?)\s+of\s+(?:your\s+)?(?:tour|visit)",
    re.IGNORECASE,
)

# Unit scope. "select" wins over "all" when both are present (operators
# usually want to know about the restriction).
_UNIT_SCOPE_SELECT_RE = re.compile(
    r"\b(?:on\s+|for\s+)?select\s+(?:units?|floor\s*plans?|homes?|apartments?|layouts?|bedrooms?|townhomes?)",
    re.IGNORECASE,
)
_UNIT_SCOPE_ALL_RE = re.compile(
    r"\b(?:on\s+)?all\s+(?:units?|floor\s*plans?|homes?|apartments?|layouts?|bedrooms?)",
    re.IGNORECASE,
)
_UNIT_SCOPE_SPECIFIC_RE = re.compile(
    r"\b(?:on\s+)?(?P<beds>\d+|two|three|four|five)\s*-?\s*bedroom(?:s)?",
    re.IGNORECASE,
)

# Audience. Keyword presence is enough — we surface the matched phrase
# so a reviewer can see who the offer targets.
_AUDIENCE_RE = re.compile(
    r"\b(?P<aud>student|healthcare|nurses?|first[\s\-]+responder"
    r"|military|veteran|teacher|educator|new[\s\-]+resident|senior)s?\b",
    re.IGNORECASE,
)

# Promo code — "mention FreeMonth", "use code XYZ"
_PROMO_CODE_RE = re.compile(
    r"\b(?:mention\s+(?P<code1>[A-Z][A-Za-z0-9]{2,20})"
    r"|use\s+(?:promo\s+)?code\s+(?P<code2>[A-Z0-9]{3,20})"
    r"|promo\s+code\s*:?\s*(?P<code3>[A-Z0-9]{3,20}))",
)

# Generic "restrictions apply" sentinel — surfaces that the offer is
# conditional even when no specific terms are extracted.
_RESTRICTIONS_RE = re.compile(
    r"(?:restrictions|terms)\s+(?:may\s+)?apply|other\s+costs?\s+and\s+fees?\s+excluded",
    re.IGNORECASE,
)


def _extract_conditions(text: str) -> list[Condition]:
    conds: list[Condition] = []
    seen_kinds: set[str] = set()

    def _push(kind: str, value: str | None, raw: str) -> None:
        # One canonical entry per kind; surface only the first hit
        # so the conditions array stays scannable.
        if kind in seen_kinds:
            return
        conds.append(Condition(kind=kind, value=value, raw=_whitespace(raw)[:140]))
        seen_kinds.add(kind)

    m = _DEADLINE_RE.search(text)
    if m:
        _push("deadline", _whitespace(m.group("deadline")), m.group(0))

    m = _LEASE_LENGTH_RE.search(text)
    if m:
        if m.group("lo") and m.group("hi"):
            val = f"{m.group('lo')}-{m.group('hi')} months"
        elif m.group("minv"):
            val = f"{m.group('minv')}+ months"
        elif m.group("plus"):
            val = f"{m.group('plus')}+ months"
        elif m.group("orlonger"):
            val = f"{m.group('orlonger')}+ months"
        else:
            val = None
        _push("lease_length", val, m.group(0))

    m = _APPLY_WITHIN_RE.search(text)
    if m:
        n = m.group("n") or m.group("n2")
        unit = (m.group("unit") or m.group("unit2") or "").lower()
        unit_short = "h" if unit.startswith("h") else "d"
        _push("apply_within", f"{n}{unit_short}", m.group(0))

    if _UNIT_SCOPE_SELECT_RE.search(text):
        _push("unit_scope", "select", _UNIT_SCOPE_SELECT_RE.search(text).group(0))
    elif _UNIT_SCOPE_SPECIFIC_RE.search(text):
        m = _UNIT_SCOPE_SPECIFIC_RE.search(text)
        beds_raw = m.group("beds").lower()
        beds = _to_int(beds_raw) or beds_raw
        _push("unit_scope", f"{beds}-bedroom", m.group(0))
    elif _UNIT_SCOPE_ALL_RE.search(text):
        _push("unit_scope", "all", _UNIT_SCOPE_ALL_RE.search(text).group(0))

    m = _AUDIENCE_RE.search(text)
    if m:
        aud = m.group("aud").lower()
        aud = re.sub(r"[\s\-]+", "_", aud)
        _push("audience", aud, m.group(0))

    m = _PROMO_CODE_RE.search(text)
    if m:
        code = m.group("code1") or m.group("code2") or m.group("code3")
        _push("promo_code", code, m.group(0))

    if _RESTRICTIONS_RE.search(text):
        _push("restrictions", None, _RESTRICTIONS_RE.search(text).group(0))

    return conds


# ─────────────────────────────────────────────────────────────────────
# Banner renderer
# ─────────────────────────────────────────────────────────────────────

_BANNER_OFFER_DISPLAY: dict[str, str] = {
    "free_rent":      "{value} FREE rent",
    "dollar_off":     "{value} off {target_pretty}",
    "percent_off":    "{value} off {target_pretty}",
    "gift_card":      "{value} gift card",
    "waived_fee":     "Waived {target_pretty}",
    "reduced_rate":   "Reduced rent",
    "reduced_deposit":"Reduced deposit",
    "look_and_lease": "Look & lease special",
}

_TARGET_PRETTY: dict[str, str] = {
    "rent":         "rent",
    "deposit":      "deposit",
    "app_fee":      "app fee",
    "admin_fee":    "admin fee",
    "amenity_fee":  "amenity fee",
    "move_in_cost": "move-in cost",
    "gift_card":    "",
    "utilities":    "utilities",
    "other":        "",
}


def _render_atom(atom: Atom) -> str:
    template = _BANNER_OFFER_DISPLAY.get(atom.offer_type, "{value}")
    target_pretty = _TARGET_PRETTY.get(atom.target, atom.target)
    out = template.format(value=atom.value or "", target_pretty=target_pretty)
    return _whitespace(out.replace("  ", " "))


def _render_condition(c: Condition) -> str | None:
    if c.kind == "deadline":
        return f"by {c.value}"
    if c.kind == "lease_length":
        return f"{c.value} lease"
    if c.kind == "apply_within":
        return f"apply within {c.value}"
    if c.kind == "unit_scope":
        if c.value == "select":
            return "select units"
        if c.value == "all":
            return "all units"
        if c.value and c.value.endswith("-bedroom"):
            return c.value
        return None
    if c.kind == "audience":
        return c.value.replace("_", " ") if c.value else None
    if c.kind == "promo_code":
        return f"code {c.value}"
    if c.kind == "restrictions":
        # Don't surface the generic sentinel in the banner — it's
        # noise. Keep it in the structured ``conditions`` array.
        return None
    return None


def _build_banner(primary: Atom | None, conditions: list[Condition], raw_clean: str) -> str:
    """Assemble the short single-line summary.

    Strategy:
      * Lead with the primary atom rendered.
      * Append up to 3 high-signal conditions (deadline, lease length,
        unit scope, apply-within, audience, promo code — in that order).
      * Bounded at ~140 chars.
      * Falls back to the first 120 chars of the cleaned raw text when
        no primary atom was extracted — keeps the cell informative for
        header-only / qualitative-only inputs.
    """
    if primary is None:
        return _whitespace(raw_clean)[:140]
    parts: list[str] = [_render_atom(primary)]
    # Render order — most actionable first.
    order = (
        "deadline", "apply_within", "lease_length",
        "unit_scope", "audience", "promo_code",
    )
    cond_by_kind = {c.kind: c for c in conditions}
    for kind in order:
        if kind not in cond_by_kind:
            continue
        rendered = _render_condition(cond_by_kind[kind])
        if rendered:
            parts.append(rendered)
        if len(parts) >= 4:  # primary + 3 conditions
            break
    banner = " · ".join(parts)
    return banner[:140]


# ─────────────────────────────────────────────────────────────────────
# Public entrypoint
# ─────────────────────────────────────────────────────────────────────


def enrich_concession(raw_text: str | None) -> Enrichment:
    """Enrich a raw concession string into a structured display record.

    Always returns an :class:`Enrichment` (possibly empty) — never raises
    and never returns None. Empty inputs produce ``Enrichment()`` with
    no atoms, no conditions, and an empty banner.

    The function is idempotent and pure — given the same input it always
    returns the same record. Safe to call on the property-level OR the
    unit-level concession field, and to call multiple times.
    """
    if not raw_text or not isinstance(raw_text, str) or not raw_text.strip():
        return Enrichment()

    # Decode entities FIRST so regex sees unescaped literals (``&amp;``
    # → ``&``, ``&nbsp;`` → space).
    decoded = _decode_html_entities(raw_text)
    normalised = _whitespace(decoded)

    atoms = _extract_atoms(normalised)
    conditions = _extract_conditions(normalised)
    primary = atoms[0] if atoms else None
    banner = _build_banner(primary, conditions, normalised)

    return Enrichment(
        atoms=atoms,
        primary_atom=primary,
        conditions=conditions,
        banner=banner,
    )
