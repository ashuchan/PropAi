"""Deterministic offer-taxonomy extraction (2026-05-24).

Pulls structured offer fields out of raw concession text. Matches the
8-column reference schema:

  Concession (Banner)    — short offer-only phrase pulled from raw chrome
  Offer Type             — categorical (free_rent / dollar_off / waived_fee
                           / reduced_rate / look_and_lease / reduced_deposit
                           / percent_off)
  Offer Target           — what the offer applies to (rent / deposit /
                           amenity_fee / app_fee / admin_fee / move_in_cost)
  Offer Value            — formatted string with unit ("6 weeks" / "$400" /
                           "1 month" / "50%") or None for vague offers
  Offer Conditions       — semicolon-delimited key:value pairs
                           (deadline:May 31st; unit_scope:select;
                            lease_length:12+ months; apply_within:48h;
                            audience:military; restrictions)

Why deterministic regex (not LLM):
  * Concession-phrasing space is closed and stable (cohort + random-20/20
    probe 2026-05-19 confirmed). LLM would add cost without accuracy gain.
  * Auditable, free, regression-testable, no drift.
  * Pairs naturally with the existing ``concession_normalize.py`` which
    handles the structured RealPage shape.

This module is the FLAT-COLUMN companion to ``concession_normalize.py``:
the latter outputs ``{"obj": {"free": {...}}}`` for downstream RealPage
JSON consumers; this one outputs the categorical/Excel-friendly flat
fields for human review + analytics.

Calling contract:
  * ``extract_offer(raw_text)`` returns a dict with ALL 5 keys (None
    when no signal). Safe for direct unpacking into ``make_unit_dict``
    or schema_v2 output.
  * Individual ``extract_offer_*`` helpers are exposed for tests and
    targeted reuse.
"""

from __future__ import annotations

import re
from typing import Any

# ──────────────────────────────────────────────────────────────────────
# Offer Type taxonomy
# ──────────────────────────────────────────────────────────────────────

# Ordering matters — first match wins. More specific patterns first.
_OFFER_TYPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # waived/no fee comes BEFORE look_and_lease (which can mention "no app fee")
    ("waived_fee", re.compile(
        r"\b(?:waived|no|free|complimentary)\s+(?:app(?:lication)?|admin|"
        r"amenity|move[- ]in|hold|reservation)\s+fee\b",
        re.IGNORECASE,
    )),
    ("look_and_lease", re.compile(
        r"\blook[- ]?(?:and|&|n)[- ]?lease\b|\blook[- ]lease\s+special\b",
        re.IGNORECASE,
    )),
    ("reduced_deposit", re.compile(
        r"\b(?:reduced|lower(?:ed)?|low|\$\d+)\s+(?:security\s+)?deposit\b"
        r"|\bdeposit\s+(?:reduced|waived|special)\b",
        re.IGNORECASE,
    )),
    ("percent_off", re.compile(
        r"\b\d{1,3}\s*%\s*(?:off|discount|reduction)\b",
        re.IGNORECASE,
    )),
    ("dollar_off", re.compile(
        r"\$\s*\d[\d,]*\s*(?:off|discount|credit|back)\b"
        r"|\$\s*\d[\d,]*\s+(?:off|toward|credit)",
        re.IGNORECASE,
    )),
    # free_rent — "N weeks/months free" or "free rent for N months"
    # Anchored on the explicit "rent" qualifier OR a duration+free pair to
    # avoid grabbing "free WiFi" / "free parking". The duration→free
    # alternation tolerates a short connective ("of", "of free") and a
    # qualifier word ("base", "effective", "monthly") between the duration
    # and "rent free" so phrases like "10 Weeks Base Rent Free"
    # (theblakeoptimistpark.com 2026-05-24) match.
    ("free_rent", re.compile(
        r"(?:\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve)\s+(?:weeks?|months?)\s+(?:of\s+)?(?:free|"
        r"complimentary|on\s+us)\b"
        r"|\bfree\s+rent\b"
        r"|\b(?:rent[- ]?)?free\s+(?:for\s+)?(?:\d+\s+)?(?:weeks?|months?)\b"
        r"|\bfirst\s+(?:\d+\s+|full\s+)?months?\b[^.!?]{0,30}\bfree\b"
        r"|\b\d+\s+months?\s+free\s+(?:base\s+)?rent\b"
        # NEW 2026-05-24: "N weeks/months [base|effective|monthly|total|
        # select|premium|market] rent free|waived|complimentary"
        r"|\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve)\s+(?:weeks?|months?)\s+(?:of\s+)?"
        r"(?:base|effective|monthly|total|select|premium|market)\s+"
        r"rent\s+(?:free|waived|complimentary)\b)",
        re.IGNORECASE,
    )),
    # reduced_rate — special pricing on rent (low priority; catches anything
    # left like "reduced rent" / "rent special" without numeric value)
    ("reduced_rate", re.compile(
        r"\b(?:reduced|discounted|special|preferred)\s+(?:rent|rate|pricing)\b"
        r"|\brent\s+(?:reduced|discount|special)\b",
        re.IGNORECASE,
    )),
)


def classify_offer_type(text: str | None) -> str | None:
    """Return the offer-type label, or None when no pattern matches.

    First-match wins per the priority order in ``_OFFER_TYPE_PATTERNS``.
    More specific types (waived_fee, look_and_lease, reduced_deposit) are
    checked before generic ones (free_rent, reduced_rate) so e.g. "no
    app fee + 1 month free" classifies as ``waived_fee`` not ``free_rent``.
    """
    if not text or not isinstance(text, str):
        return None
    for label, pat in _OFFER_TYPE_PATTERNS:
        if pat.search(text):
            return label
    return None


# ──────────────────────────────────────────────────────────────────────
# Offer Target taxonomy
# ──────────────────────────────────────────────────────────────────────

_TARGET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("app_fee", re.compile(r"\bapp(?:lication)?\s+fee\b", re.IGNORECASE)),
    ("admin_fee", re.compile(r"\badmin(?:istrative)?\s+fee\b", re.IGNORECASE)),
    ("amenity_fee", re.compile(r"\b(?:amenity|amenities)\s+fee\b", re.IGNORECASE)),
    ("move_in_cost", re.compile(
        r"\bmove[- ]?in\s+(?:cost|special|fee|credit)\b", re.IGNORECASE,
    )),
    ("deposit", re.compile(r"\b(?:security\s+)?deposit\b", re.IGNORECASE)),
    # "rent" is the most generic — matched LAST so specific fees win.
    ("rent", re.compile(r"\brent\b|\bmonths?\b|\bweeks?\b", re.IGNORECASE)),
)


def classify_offer_target(text: str | None, offer_type: str | None = None) -> str | None:
    """Return the target the offer applies to.

    If ``offer_type`` is provided, the target defaults are pinned by type:
      * waived_fee → look for fee-specific match; fallback "app_fee"
      * reduced_deposit → "deposit"
      * free_rent / dollar_off / reduced_rate / percent_off / look_and_lease
        → look for target-specific match; fallback "rent"

    Returns None only when ``text`` is empty.
    """
    if not text or not isinstance(text, str):
        return None

    # Pinned defaults by offer_type — fewer false positives
    if offer_type == "reduced_deposit":
        return "deposit"

    for label, pat in _TARGET_PATTERNS:
        if pat.search(text):
            return label

    # Fallback by type when no explicit target word found
    if offer_type in ("waived_fee",):
        return "app_fee"
    if offer_type in ("free_rent", "dollar_off", "reduced_rate",
                      "percent_off", "look_and_lease"):
        return "rent"
    return None


# ──────────────────────────────────────────────────────────────────────
# Offer Value — formatted string with unit
# ──────────────────────────────────────────────────────────────────────

_WORD_NUMBER = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12",
}

# "$400" / "$1,500"
_VAL_DOLLAR_RE = re.compile(r"\$\s*(\d[\d,]*)")
# "6 weeks" / "two weeks"
_VAL_WEEKS_RE = re.compile(
    r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+weeks?\b",
    re.IGNORECASE,
)
# "1 month" / "2 months" / "first month"
_VAL_MONTHS_RE = re.compile(
    r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+months?\b",
    re.IGNORECASE,
)
_VAL_FIRST_MONTH_RE = re.compile(
    r"\bfirst\s+(?:full\s+)?months?\b", re.IGNORECASE,
)
_VAL_PERCENT_RE = re.compile(r"(\d{1,3})\s*%")


def extract_offer_value(text: str | None, offer_type: str | None = None) -> str | None:
    """Return the offer value as a formatted string with unit.

    Examples:
      "6 weeks FREE rent"        → "6 weeks"
      "$400 off rent"            → "$400"
      "50% off"                  → "50%"
      "first month free"         → "first month"
      "1 month free"             → "1 month"
      "Reduced deposit"          → None  (no numeric anchor)

    Returns None when no numeric/word anchor present (vague offers like
    "Reduced rent" or "Look & lease special") — matches the reference
    xlsx behavior where Value is empty for these.
    """
    if not text or not isinstance(text, str):
        return None

    # Dollar values win when offer_type is dollar_off
    if offer_type == "dollar_off":
        m = _VAL_DOLLAR_RE.search(text)
        if m:
            raw = m.group(1).replace(",", "")
            # Re-format with comma if 4+ digits
            return f"${int(raw):,}" if len(raw) >= 4 else f"${raw}"
        return None

    if offer_type == "percent_off":
        m = _VAL_PERCENT_RE.search(text)
        if m:
            return f"{m.group(1)}%"
        return None

    if offer_type == "free_rent":
        # "first N (full) months" — count takes priority over the bare
        # "first month" literal (must run BEFORE _VAL_FIRST_MONTH_RE
        # which would otherwise short-circuit on the leading "first").
        m = re.search(
            r"\bfirst\s+(\d+|two|three|four|five|six|seven|eight|nine|ten|"
            r"eleven|twelve)\s+(?:full\s+)?months?\b",
            text, re.IGNORECASE,
        )
        if m:
            n = _WORD_NUMBER.get(m.group(1).lower(), m.group(1))
            unit = "month" if n == "1" else "months"
            return f"{n} {unit}"
        # Bare "first month" (no count) → literal
        if _VAL_FIRST_MONTH_RE.search(text):
            return "first month"
        # Weeks/months count + "free"
        for pat, unit_word in ((_VAL_WEEKS_RE, "week"), (_VAL_MONTHS_RE, "month")):
            m = pat.search(text)
            if m:
                n_raw = m.group(1).lower()
                n = _WORD_NUMBER.get(n_raw, n_raw)
                unit = f"{unit_word}s" if n != "1" else unit_word
                return f"{n} {unit}"
        return None

    # For waived_fee / reduced_rate / reduced_deposit / look_and_lease,
    # vague offers without a specific value → None (matches xlsx).
    # But if a $ amount IS present (rare), report it.
    m = _VAL_DOLLAR_RE.search(text)
    if m:
        raw = m.group(1).replace(",", "")
        return f"${int(raw):,}" if len(raw) >= 4 else f"${raw}"
    return None


# ──────────────────────────────────────────────────────────────────────
# Offer Conditions — semicolon-delimited key:value pairs
# ──────────────────────────────────────────────────────────────────────

# Deadlines — calendar dates in various formats
_COND_DEADLINE_RES: tuple[re.Pattern[str], ...] = (
    # "by May 25th" / "before May 31" / "ends June 30"
    re.compile(
        r"\b(?:by|before|ends?|expires?|through|until|deadline:?)\s+"
        r"(?:on\s+)?"
        r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?)\b",
        re.IGNORECASE,
    ),
    # "by 5/31/26" / "before 6/30/2026"
    re.compile(
        r"\b(?:by|before|ends?|expires?|through|until|deadline:?)\s+"
        r"(?:on\s+)?(\d{1,2}/\d{1,2}/\d{2,4})\b",
        re.IGNORECASE,
    ),
)

# Unit scope — which units the offer applies to
_COND_UNIT_SCOPE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("1-bedroom", re.compile(r"\b1\s*-?\s*bedrooms?\b|\bone[- ]bedrooms?\b", re.IGNORECASE)),
    ("2-bedroom", re.compile(r"\b2\s*-?\s*bedrooms?\b|\btwo[- ]bedrooms?\b", re.IGNORECASE)),
    ("3-bedroom", re.compile(r"\b3\s*-?\s*bedrooms?\b|\bthree[- ]bedrooms?\b", re.IGNORECASE)),
    ("select", re.compile(r"\bselect\s+(?:homes?|units?|apartments?|floor\s*plans?)\b", re.IGNORECASE)),
    ("all", re.compile(r"\ball\s+(?:homes?|units?|apartments?|floor\s*plans?)\b", re.IGNORECASE)),
)

# Lease length minimums
_COND_LEASE_LENGTH = re.compile(
    r"\b(\d{1,2})(?:\s*\+|\s*or\s+(?:more|longer))?\s*[- ]?\s*months?\s+(?:lease|term)\b"
    r"|\b(?:lease|term)\s+of\s+(\d{1,2})(?:\s*\+|\s*or\s+(?:more|longer))?\s*months?\b",
    re.IGNORECASE,
)

# Apply-within window
_COND_APPLY_WITHIN = re.compile(
    r"\bapply\s+(?:within|in)\s+(\d{1,3})\s*(hours?|hrs?|days?)\b",
    re.IGNORECASE,
)

# Audience qualifiers
_COND_AUDIENCE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("military", re.compile(r"\bmilitary\b|\bveterans?\b|\bservicemembers?\b", re.IGNORECASE)),
    ("student", re.compile(r"\bstudents?\b|\bcollege\b", re.IGNORECASE)),
    ("senior", re.compile(r"\bseniors?\b|\b55\+\b", re.IGNORECASE)),
    ("first_responder", re.compile(r"\bfirst[- ]responders?\b|\bfirefighters?\b|\bpolice\b", re.IGNORECASE)),
)

# Catch-all restrictions
_COND_RESTRICTIONS_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\*[^.!?]*(?:apply|condition|restriction)", re.IGNORECASE),
    re.compile(r"\bconditions?\s+(?:apply|may\s+apply)\b", re.IGNORECASE),
    re.compile(r"\brestrictions?\s+(?:apply|may\s+apply)\b", re.IGNORECASE),
    re.compile(r"\bsubject\s+to\s+(?:change|availability|qualification|approval)\b", re.IGNORECASE),
    re.compile(r"\bsee\s+(?:office|leasing|staff)\s+for\s+details\b", re.IGNORECASE),
    re.compile(r"\bcall\s+for\s+details\b", re.IGNORECASE),
)


def extract_offer_conditions(text: str | None) -> str | None:
    """Return a semicolon-delimited string of key:value condition pairs.

    Conditions surfaced:
      * deadline:<date>     — calendar deadline (parsed in various formats)
      * unit_scope:<scope>  — which units (1/2/3-bedroom, select, all)
      * lease_length:<N>+ months — minimum lease term
      * apply_within:<window> — application time-pressure (24h / 48h / 7days)
      * audience:<group>    — military / student / senior / first_responder
      * restrictions        — generic "conditions apply" / "subject to" caveat

    Returns None when no condition signal is present.
    """
    if not text or not isinstance(text, str):
        return None

    parts: list[str] = []

    # Deadline (first match wins)
    for pat in _COND_DEADLINE_RES:
        m = pat.search(text)
        if m:
            parts.append(f"deadline:{m.group(1).strip()}")
            break

    # Unit scope
    for label, pat in _COND_UNIT_SCOPE:
        if pat.search(text):
            parts.append(f"unit_scope:{label}")
            break

    # Lease length
    m = _COND_LEASE_LENGTH.search(text)
    if m:
        months = m.group(1) or m.group(2)
        if months:
            parts.append(f"lease_length:{months}+ months")

    # Apply-within window — normalize to canonical units
    m = _COND_APPLY_WITHIN.search(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower().rstrip("s")
        if unit in ("hour", "hr"):
            parts.append(f"apply_within:{n}h")
        else:  # day
            parts.append(f"apply_within:{n}d" if n != 1 else "apply_within:24h")

    # Audience qualifier
    for label, pat in _COND_AUDIENCE:
        if pat.search(text):
            parts.append(f"audience:{label}")
            break

    # Generic restrictions catchall (only when nothing more specific matched
    # OR when the text explicitly says "restrictions apply" alongside specifics)
    for pat in _COND_RESTRICTIONS_RES:
        if pat.search(text):
            parts.append("restrictions")
            break

    if not parts:
        return None
    return "; ".join(parts)


# ──────────────────────────────────────────────────────────────────────
# Banner extraction — short offer-only phrase
# ──────────────────────────────────────────────────────────────────────

# Patterns that anchor a real offer phrase. Captured window around the
# match is the banner. Tighter than the page-window-capture regex because
# the input here is already cleaned text.
_BANNER_ANCHORS: tuple[re.Pattern[str], ...] = (
    # "N weeks/months FREE rent"
    re.compile(
        r"\b\d+\s+(?:weeks?|months?)\s+(?:free|complimentary)\s+rent\b",
        re.IGNORECASE,
    ),
    # "$X off rent" / "$X off"
    re.compile(r"\$\d[\d,]*\s+off(?:\s+rent)?\b", re.IGNORECASE),
    # "free rent for N weeks/months"
    re.compile(r"\bfree\s+rent\s+for\s+\d+\s+(?:weeks?|months?)\b", re.IGNORECASE),
    # "N% off"
    re.compile(r"\b\d{1,3}\s*%\s*off\b", re.IGNORECASE),
    # "Reduced rent" / "Reduced deposit" / "Waived fee" / "Look & lease"
    re.compile(
        r"\b(?:reduced|waived|complimentary|low(?:er(?:ed)?)?)\s+"
        r"(?:rent|deposit|app(?:lication)?\s+fee|admin\s+fee|amenity\s+fee)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\blook[- ]?(?:and|&|n)[- ]?lease\b", re.IGNORECASE),
    re.compile(r"\bmove[- ]in\s+special\b", re.IGNORECASE),
    re.compile(r"\bfirst\s+months?\s+free\b", re.IGNORECASE),
)

# Right-side decoration to trim
_BANNER_TRIM_RIGHT = re.compile(
    r"\s*(?:\*|\(|\[|on\s+select|see\s+office|call\s+for\s+details|"
    r"conditions?\s+apply|restrictions?\s+apply|subject\s+to)\b.*$",
    re.IGNORECASE,
)


def extract_offer_banner(text: str | None) -> str | None:
    """Return a short ~20-100 char banner extracted from the longer raw text.

    Strategy: find the first offer anchor, expand a small window around it
    (up to natural sentence boundary or 80 chars), then trim disclaimer
    tails ("*conditions apply", "see office for details"). Returns None
    when no anchor matches.
    """
    if not text or not isinstance(text, str):
        return None
    norm = re.sub(r"\s+", " ", text).strip()
    if not norm:
        return None

    for pat in _BANNER_ANCHORS:
        m = pat.search(norm)
        if not m:
            continue
        start, end = m.span()
        # Expand left to the last sentence-end or capital-letter start
        left = max(0, start - 50)
        # Find nearest sentence boundary in the left window
        left_chunk = norm[left:start]
        # Take everything after the last "." "!" "?" or "·" or "—"
        sep_m = list(re.finditer(r"[.!?·—]\s+", left_chunk))
        if sep_m:
            left = left + sep_m[-1].end()
        # Expand right to next sentence boundary or 60 chars
        right_max = min(len(norm), end + 60)
        right_chunk = norm[end:right_max]
        right_sep = re.search(r"[.!?·—]\s+", right_chunk)
        if right_sep:
            right = end + right_sep.start()
        else:
            right = right_max

        banner = norm[left:right].strip(" \t·—-—,;:")
        # Trim disclaimer tails
        banner = _BANNER_TRIM_RIGHT.sub("", banner).strip(" \t·—-—,;:")
        if not banner:
            continue
        # Reject if it's just a single $ token or a fragment <8 chars
        if len(banner) < 8:
            continue
        return banner

    return None


# ──────────────────────────────────────────────────────────────────────
# Top-level orchestrator — returns all 5 fields
# ──────────────────────────────────────────────────────────────────────


def extract_offer(text: str | None) -> dict[str, Any]:
    """Return all 5 offer fields as a flat dict, suitable for direct
    inclusion in a unit dict or v2 output row.

    Always returns the same key set (None when no signal) so downstream
    code doesn't need ``.get()`` guards. Empty input → all keys are None.
    """
    out: dict[str, Any] = {
        "offer_banner": None,
        "offer_type": None,
        "offer_target": None,
        "offer_value": None,
        "offer_conditions": None,
    }
    if not text or not isinstance(text, str) or not text.strip():
        return out

    out["offer_type"] = classify_offer_type(text)
    out["offer_target"] = classify_offer_target(text, out["offer_type"])
    out["offer_value"] = extract_offer_value(text, out["offer_type"])
    out["offer_conditions"] = extract_offer_conditions(text)
    out["offer_banner"] = extract_offer_banner(text)
    return out
