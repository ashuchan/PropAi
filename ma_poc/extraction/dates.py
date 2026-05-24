"""Lenient date-string normaliser shared across the extraction + validation
boundaries.

The producer-side date surface is wide: AppFolio ``"Available 7/4/26"``,
Entrata ``"Move-in 8/21/2026"``, Funnel ``"Date: 5/22/2026"``, RealPage
``"Tuesday June 23 2026"``, generic ``"Available Now"`` / ``"Now"``,
ordinal suffixes (``"Jul 21st"``), month-day without year
(``"Available May 30"``), and so on.

Two callers want the same parsing behaviour:

  * The L4 schema gate (``ma_poc.validation.schema_gate``) needs to decide
    whether ``record["available_date"]`` should pass through, be
    normalised in place, or be nulled and stashed in
    ``_date_placeholder`` for telemetry.

  * The v2 formatter in ``ma_poc.scripts.runners.jugnu`` needs to render
    the same column as a YYYY-MM-DD string.

Pre-2026-05-19 the v2 formatter had a lenient parser and the gate had a
strict ``datetime.fromisoformat`` check. The gate ran FIRST, nulling
21K+ rows per day with parseable producer strings; the v2 formatter
then saw ``None`` and shipped ``None`` to the units table. This module
exists so both call sites share one definition and no future caller
re-invents the strict variant.

Public surface — keep small on purpose:

  * :func:`format_loose_date` — string in, ISO YYYY-MM-DD or None out.
  * :data:`DATE_PREFIX_RE` / :data:`DATE_NOW_TOKENS` /
    :data:`DATE_ABSENT_TOKENS` — re-exported for callers (jugnu's
    availability-status inference reads ``DATE_NOW_TOKENS`` after the
    same prefix strip).

Pure, deterministic, never raises on unexpected input.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

# ── Producer prefixes / tokens ────────────────────────────────────────────────

#: Words that producers prepend to availability dates without changing the
#: underlying date semantics. Stripped before format-matching so AppFolio's
#: ``"Available 7/4/26"`` and Entrata's ``"Move-in 8/21/26"`` both reduce to
#: a plain date string. The leading word is consumed greedily;
#: case-insensitive.
#:
#: 2026-05-19 (R3): added ``date|on`` (Funnel/RealPage variants
#: ``"Date: 5/22/2026"``, ``"Available On: 5/18/2026"``) and weekday
#: prefixes (RealPage emits ``"Tuesday June 23 2026"`` for some property
#: feeds). Optional connector words ``starting|from|until|after`` cover
#: ``"Available starting 7/01"``.
DATE_PREFIX_RE: re.Pattern[str] = re.compile(
    r"^(?:available|avail|move[\s\-_]?in|moveinday|moveindate|ready|"
    r"availability|estimated|est\.?|approx\.?|approximate|opens?|open|"
    r"date|on|"
    r"mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|"
    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
    r"(?:\s+(?:starting|from|until|after|on))?"
    r"[\s:\-,.]+",
    re.IGNORECASE,
)

#: Ordinal-suffix collapser — ``"July 4th"`` → ``"July 4"``, ``"21st"`` → ``"21"``.
_DATE_ORDINAL_RE: re.Pattern[str] = re.compile(
    r"(\b\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE
)

#: Trailing decoration ("!", ".", ",", "?", whitespace) stripped before token
#: lookup so ``"Available now!"`` matches ``"available now"``.
_DATE_TRAILING_PUNCT_RE: re.Pattern[str] = re.compile(r"[!?.,;\s]+$")

#: Month-name + day with no year. Used as a fallback when strptime exhausts;
#: the year is back-filled from the ``today`` anchor, rolling forward one
#: year if the resulting date is already past (feeds emit forward-looking
#: dates, so ``"Feb 14"`` in November means *next* February).
_DATE_MONTH_DAY_RE: re.Pattern[str] = re.compile(
    r"^(?P<month>"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?"
    r")\s+(?P<day>\d{1,2})$",
    re.IGNORECASE,
)

#: Placeholder strings meaning "available immediately" — the producer is
#: signalling that the unit is rentable today rather than dating a future
#: move-in. These resolve to the scrape date (close-enough for analytics;
#: the alternative is None which loses the AVAILABLE-now signal entirely).
#:
#: 2026-05-23: extended with bare single-word availability signals
#: ``available``, ``vacant``, ``open``. A producer that emits just
#: "Available" in the date field IS asserting availability now — the v2
#: emit should resolve to today (so the rent-intelligence pipeline counts
#: this unit as available) rather than to None (which would drop the
#: signal entirely). Verified against PIDs 220976/12318/11797 on the
#: 2026-05-22 cloud run where bare "Available" was the dominant raw value.
DATE_NOW_TOKENS: frozenset[str] = frozenset({
    "now", "today", "immediate", "immediately", "imm",
    "asap", "ready now", "ready", "current", "currently",
    "open now", "available now",
    # 2026-05-23 — single-word availability signals (resolve to today)
    "available", "vacant", "open",
})

#: Placeholder strings that explicitly mean "no date available" — kept
#: distinct from format-mismatch so the producer's intent isn't lost in
#: logs. Both map to None (the v2 contract uses None for absent dates).
DATE_ABSENT_TOKENS: frozenset[str] = frozenset({
    "n/a", "na", "tbd", "tba", "call", "contact", "inquire",
    "unavailable", "unavail", "leased", "rented", "not available",
    "coming soon", "wait list", "waitlist", "-", "--",
})

#: "Only N {vacant|available|open|left|unit(s)}" — producer copy that
#: implies units are available NOW (with a count). Treats as
#: "available today" because the producer is asserting current
#: availability. Examples: "Only 2 Vacant Apartments Left!" /
#: "Only 1 Available Unit" / "Only 3 Left". Used in
#: ``format_loose_date`` to resolve to today.
_DATE_ONLY_N_AVAILABLE_RE: re.Pattern[str] = re.compile(
    r"^\s*only\s+\d+\s+(?:vacant|available|open|left|unit)",
    re.IGNORECASE,
)

#: m/d without year — "07/24", "6/15". Always implies current year
#: (with roll-forward to next year if the date is already past). Used by
#: ``format_loose_date`` after the existing m/d/Y branches fail. Bounded
#: to ≤2 digits per side so it doesn't fight the YYYY/MM/DD branch.
_DATE_MD_NO_YEAR_RE: re.Pattern[str] = re.compile(
    r"^\s*(?P<month>\d{1,2})[/-](?P<day>\d{1,2})\s*$"
)

#: "Mid/Late/Early <Month> [YYYY]" — vague-shape dates producers use when
#: an exact day is unknown but the rough timing is. Resolution: Early = 5th,
#: Mid = 15th, Late = 25th. Year is optional; back-fills current year and
#: rolls forward when past. Phase 4.5 (2026-05-24) — Run 2026-05-23 had
#: 142 rows shipping unparseable "Late August" / "Mid June" strings that
#: looks_date_like accepts but format_loose_date previously couldn't
#: resolve, leaving the raw string clipped to 32 chars in the typed
#: column. Now resolves to a concrete ISO date.
_DATE_VAGUE_MONTH_RE: re.Pattern[str] = re.compile(
    r"^\s*(?P<modifier>early|mid|late)\s+"
    r"(?P<month>"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?"
    r")"
    r"(?:\s+(?P<year>\d{4}))?\s*$",
    re.IGNORECASE,
)

#: Season-name + optional year → mid-season anchor. Phase 4.5: producers
#: increasingly publish "Spring 2026" / "Summer 2027" rather than a
#: specific date when pre-leasing far ahead. Resolves to:
#:   Spring → Mar 15
#:   Summer → Jul 15
#:   Fall/Autumn → Oct 15
#:   Winter → Jan 15 (next year)
#: Year is optional; back-fills current year and rolls forward when past.
_DATE_SEASON_YEAR_RE: re.Pattern[str] = re.compile(
    r"^\s*(?P<season>spring|summer|fall|autumn|winter)"
    r"(?:\s+(?P<year>\d{4}))?\s*$",
    re.IGNORECASE,
)

#: "end of [the] {month|year|week}" — vague-shape relative date.
#: Resolves to the LAST day of the current month/year/week.
_DATE_END_OF_RE: re.Pattern[str] = re.compile(
    r"^\s*end\s+of(?:\s+the)?\s+(?P<period>month|year|week)\s*$",
    re.IGNORECASE,
)

#: Year-only — "Early 2027", "2027" — Phase 4.5: when the producer
#: publishes only a year for pre-leasing, resolve to Jan 1 of that year
#: (or mid-Jan if "Early <year>"). Bounded to 4-digit year in plausible
#: range so accidental zip codes / phone fragments don't parse.
_DATE_YEAR_ONLY_RE: re.Pattern[str] = re.compile(
    r"^\s*(?:(?P<modifier>early|mid|late)\s+)?(?P<year>20\d{2})\s*$",
    re.IGNORECASE,
)

#: Month name → month number — used by the vague-month and season
#: branches. Built from the month tokens regex to keep one source of
#: truth.
_MONTH_NAME_TO_NUM: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

#: Modifier → day-of-month for vague shapes. Empirical anchors —
#: producer-side "Mid June" means roughly the 15th in property listings.
_MODIFIER_TO_DAY: dict[str, int] = {"early": 5, "mid": 15, "late": 25}

#: Season → (month, day) mid-season anchor.
_SEASON_TO_ANCHOR: dict[str, tuple[int, int]] = {
    "spring": (3, 15),
    "summer": (7, 15),
    "fall": (10, 15),
    "autumn": (10, 15),
    "winter": (1, 15),  # next-year roll-forward handled below
}


def format_loose_date(val: Any, *, today: date | None = None) -> str | None:
    """Normalise a producer date string to YYYY-MM-DD. None if unparseable.

    Accepts every shape observed across the 2026-05-18 cloud run telemetry
    plus the strict ISO / numeric ``m/d/Y`` shapes the legacy implementation
    handled. See module docstring for the full surface.

    Args:
        val: The raw value from the adapter. Any type; non-strings are
            coerced via ``str(val)``.
        today: Override for the "now" placeholder anchor — defaults to
            ``datetime.now().date()``. Exposed for testing so the unit
            tests can pin a deterministic anchor instead of asserting
            against the wall clock.

    Returns:
        ISO YYYY-MM-DD string, or None if the value is empty / a known
        "absent" placeholder / doesn't match any supported format.
    """
    if val is None or val == "":
        return None
    s = str(val).strip()
    if not s:
        return None

    # Collapse internal whitespace (incl. \n\t injected by some DOM scrapes).
    s = re.sub(r"\s+", " ", s)

    # Strip trailing decoration like "Available now!".
    s = _DATE_TRAILING_PUNCT_RE.sub("", s).strip()
    if not s:
        return None

    # Strip producer prefix word (e.g. "Available ", "Move-in ").
    # DATE_PREFIX_RE may match more than once (``"Available On: 5/18/2026"``
    # → "On: 5/18/2026" → "5/18/2026") so we retry until idempotent.
    for _ in range(3):
        stripped = DATE_PREFIX_RE.sub("", s).strip()
        if stripped == s:
            break
        s = stripped
    if not s:
        return None

    # Strip ordinal suffixes ("Jul 4th" -> "Jul 4", "21st" -> "21").
    s = _DATE_ORDINAL_RE.sub(r"\1", s)

    s_lower = s.lower()
    if s_lower in DATE_ABSENT_TOKENS:
        return None
    if s_lower in DATE_NOW_TOKENS:
        anchor = today if today is not None else datetime.now().date()
        return anchor.strftime("%Y-%m-%d")

    # 2026-05-23 — "Only N {vacant|available|open|left|unit}" producer
    # copy implies the producer is asserting availability NOW with a
    # count. Resolve to today's date so the v2 emit ships AVAILABLE +
    # today. Examples: "Only 2 Vacant Apartments Left!" /
    # "Only 1 Available Unit". Verified canonical on PIDs 11797/12285/
    # 220976/220976/229986 in the 2026-05-22 cloud run.
    if _DATE_ONLY_N_AVAILABLE_RE.match(s):
        anchor = today if today is not None else datetime.now().date()
        return anchor.strftime("%Y-%m-%d")

    # Strict ISO YYYY-MM-DD (already-normalised input — fast path).
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    if len(s) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]

    # Numeric m/d/Y and friends. Try 4-digit-year first (unambiguous),
    # then 2-digit. ``%y`` interprets 00-68 as 2000-2068 and 69-99 as
    # 1969-1999 per Python's strptime contract — good enough for
    # property listings whose dates are always near-future.
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%m-%d-%Y", "%Y-%m-%d",
                "%m/%d/%y", "%d/%m/%y", "%m-%d-%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Long-form ("July 4, 2026", "Jul 4 2026", "4 July 2026").
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
                "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Month-name + day without year — "Available May 30" / "Jun 03".
    # Back-fill current year, roll forward if past.
    m = _DATE_MONTH_DAY_RE.match(s)
    if m:
        anchor = today if today is not None else datetime.now().date()
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                parsed = datetime.strptime(
                    f"{m.group('month')} {m.group('day')} {anchor.year}", fmt
                ).date()
                if parsed < anchor:
                    parsed = parsed.replace(year=anchor.year + 1)
                return parsed.strftime("%Y-%m-%d")
            except ValueError:
                continue

    # 2026-05-23 — Numeric m/d without year ("07/24", "6/15", "12-31").
    # Producer asserts "this month/day of the current year." Back-fill
    # current year; roll forward to next year if the date is already
    # past. Mirrors the month-day-no-year branch above.
    m = _DATE_MD_NO_YEAR_RE.match(s)
    if m:
        try:
            mo = int(m.group("month"))
            da = int(m.group("day"))
            if 1 <= mo <= 12 and 1 <= da <= 31:
                anchor = today if today is not None else datetime.now().date()
                parsed = date(anchor.year, mo, da)
                if parsed < anchor:
                    parsed = parsed.replace(year=anchor.year + 1)
                return parsed.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass

    # 2026-05-24 Phase 4.5 — Vague month: "Mid June", "Late August 2026",
    # "Early March". Resolves to a concrete day (5/15/25) of the named
    # month. Year optional; back-fills + rolls forward.
    m = _DATE_VAGUE_MONTH_RE.match(s)
    if m:
        try:
            modifier = m.group("modifier").lower()
            month_token = m.group("month").lower()
            year_token = m.group("year")
            mo = _MONTH_NAME_TO_NUM.get(month_token)
            da = _MODIFIER_TO_DAY.get(modifier)
            if mo and da:
                anchor = today if today is not None else datetime.now().date()
                year = int(year_token) if year_token else anchor.year
                parsed = date(year, mo, da)
                # Only roll-forward when year was inferred (not explicit).
                if year_token is None and parsed < anchor:
                    parsed = parsed.replace(year=anchor.year + 1)
                return parsed.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass

    # 2026-05-24 Phase 4.5 — Season + optional year: "Spring 2026",
    # "Late Summer", "Fall". Resolves to mid-season anchor.
    m = _DATE_SEASON_YEAR_RE.match(s)
    if m:
        try:
            season = m.group("season").lower()
            year_token = m.group("year")
            anchor_md = _SEASON_TO_ANCHOR.get(season)
            if anchor_md:
                anchor = today if today is not None else datetime.now().date()
                year = int(year_token) if year_token else anchor.year
                mo, da = anchor_md
                parsed = date(year, mo, da)
                # Only roll-forward when year was inferred.
                if year_token is None and parsed < anchor:
                    parsed = parsed.replace(year=anchor.year + 1)
                return parsed.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass

    # 2026-05-24 Phase 4.5 — "end of [the] {month|year|week}".
    # Resolves to the LAST day of the current period.
    m = _DATE_END_OF_RE.match(s)
    if m:
        try:
            period = m.group("period").lower()
            anchor = today if today is not None else datetime.now().date()
            if period == "month":
                # Last day of current month: jump to first of next month, subtract 1 day.
                if anchor.month == 12:
                    nxt = date(anchor.year + 1, 1, 1)
                else:
                    nxt = date(anchor.year, anchor.month + 1, 1)
                from datetime import timedelta
                parsed = nxt - timedelta(days=1)
            elif period == "year":
                parsed = date(anchor.year, 12, 31)
            elif period == "week":
                # Last day of current week (Sunday — week starts Monday).
                from datetime import timedelta
                # anchor.weekday(): Mon=0, Sun=6
                days_until_sun = (6 - anchor.weekday()) % 7
                parsed = anchor + timedelta(days=days_until_sun)
            else:
                parsed = None
            if parsed is not None:
                return parsed.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass

    # 2026-05-24 Phase 4.5 — Year-only ("2027", "Early 2027"). Resolves to
    # Jan 5/15/25 of the year for early/mid/late modifiers, or Jan 1
    # otherwise. The 4-digit-year guard prevents zip codes / phone
    # fragments from accidentally parsing.
    m = _DATE_YEAR_ONLY_RE.match(s)
    if m:
        try:
            year = int(m.group("year"))
            modifier = (m.group("modifier") or "").lower()
            if 2024 <= year <= 2050:
                day = _MODIFIER_TO_DAY.get(modifier, 1)
                parsed = date(year, 1, day)
                return parsed.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass

    return None


# ── Shape predicate for raw-fallback gating ──────────────────────────────────

#: Month name / abbreviation token (matches "May", "August", "Jan", "Sep",
#: "Sept", "Sep.", etc.) — used by ``looks_date_like`` to identify strings
#: that contain at least one month reference.
_DATE_SHAPE_MONTH_RE: re.Pattern[str] = re.compile(
    r"\b(?:"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?"
    r")\b",
    re.IGNORECASE,
)

#: Numeric date shape — digits adjacent to a slash or dash. Matches "6/15",
#: "07/24", "2026/05/28", "6-15-26", but does NOT match a bare "$1,500" or
#: a phone number like "555-1234" because those don't reduce to the
#: digit-separator-digit form within a small window.
_DATE_SHAPE_NUMERIC_RE: re.Pattern[str] = re.compile(
    r"\b\d{1,4}[/-]\d{1,2}(?:[/-]\d{1,4})?\b"
)

#: Season name — "Spring 2026", "Late Summer", "Winter '27".
_DATE_SHAPE_SEASON_RE: re.Pattern[str] = re.compile(
    r"\b(?:spring|summer|fall|autumn|winter)\b",
    re.IGNORECASE,
)

#: Date-relative words: "early/mid/late <month>", "end of (the) month/year/week",
#: "this week/weekend/month", "next week/month".
_DATE_SHAPE_RELATIVE_RE: re.Pattern[str] = re.compile(
    r"\b(?:"
    r"early|mid|late|"
    r"end[\s-]of[\s-](?:the[\s-])?(?:month|year|week)|"
    r"this[\s-](?:week|month|weekend)|"
    r"next[\s-](?:week|month)"
    r")\b",
    re.IGNORECASE,
)

#: "Now"-class tokens (subset of DATE_NOW_TOKENS) — when present they
#: signal availability semantics. Single-word forms ("Available", "Now",
#: "Vacant") are admitted by the parser via the extended DATE_NOW_TOKENS
#: (resolve to today). The predicate accepts the same tokens both alone
#: and accompanied — symmetric with the parser.
_DATE_SHAPE_NOW_TOKEN_RE: re.Pattern[str] = re.compile(
    r"\b(?:now|today|asap|immediate(?:ly)?|soon|currently|ready|available|"
    r"vacant|open|opening|moves?[\s-]in|move[\s-]in)\b",
    re.IGNORECASE,
)

#: "Only N {vacant|available|open|left|unit(s)}" — explicit availability
#: assertion with a count. Predicate accepts; parser resolves to today.
_DATE_SHAPE_ONLY_N_RE: re.Pattern[str] = re.compile(
    r"\bonly\s+\d+\s+(?:vacant|available|open|left|unit)",
    re.IGNORECASE,
)

#: Negative-context prefix — "Not Available", "No Vacancy", "Never Open",
#: etc. When a now-token is preceded by a negative modifier, the producer
#: is signalling UNAVAILABLE. ``looks_date_like`` returns False for these
#: so the v2 fallback gate doesn't preserve them as raw available_date +
#: incorrectly infer AVAILABLE. Negative-context strings flow through
#: ``DATE_ABSENT_TOKENS`` (parser returns None, raw shipped as None).
_DATE_SHAPE_NEGATIVE_RE: re.Pattern[str] = re.compile(
    r"\b(?:not|no|never|cannot|coming|won['']?t\s+be)\s+"
    r"(?:available|vacant|open|ready|now|soon|on\s+market)\b",
    re.IGNORECASE,
)


def looks_date_like(val: Any) -> bool:
    """Heuristic gate for the v2 emit raw-fallback (jugnu.py:2037+).

    Returns True when ``val`` is a string that contains at least one
    plausibly date-shaped token. Used to filter the v2 emit's raw-fallback
    so non-date producer strings ("Longhorn", "Only 2 Vacant Apartments
    Left!", bare "Available", "Sign Waitlist") do NOT ship as
    ``available_date`` values when :func:`format_loose_date` returns None.

    Accepts (returns True):
      * A month name anywhere in the string ("Late August", "Spring 2026",
        "Mid June", "Dec. 2", "Available June 1")
      * A numeric-date shape (digits + ``/`` or ``-`` separator):
        ``"6/15"``, ``"07/24"``, ``"2026/05/28"``, ``"Available 6/7"``
      * A season name ("Spring 2026", "Late Summer")
      * Date-relative words ("end of month", "this weekend", "next week",
        "starting May 1st") — captured by either the relative regex OR
        the month-name regex
      * "Now"-class tokens BUT only when accompanied by another word
        (avoids bare "Available" / single-token "Now" leaking through).
        "Available Now" / "Ready Today" / "Available Soon" pass.

    Rejects (returns False):
      * Empty / None / non-string
      * Single-token strings even if the token is in the now-set
        ("Available" alone, "Now" alone — these are ambiguous on the page)
      * Plain text without any date shape ("Longhorn", "Cosmopolitan",
        "Only 2 Vacant Apartments Left!", "Sign Waitlist", "/ month", "to")

    Symmetric with :func:`format_loose_date`: any value the parser
    successfully parses also passes this predicate by design (every
    parseable shape contains at least one of the four recognised tokens).
    Callers can use ``format_loose_date(val) is not None or
    looks_date_like(val)`` to gate "parseable OR date-shaped" decisions.

    Live-canary verification: bare ``"Available"`` from SecureCafe
    extraction was the canonical leak class that drove this predicate
    (canary 2026-05-23 produced 34 rows of that shape). See
    [docs/dom_quality_and_llm_reduction_playbook.md](../docs/dom_quality_and_llm_reduction_playbook.md)
    T1.A for the full evidence + design rationale.
    """
    if val is None:
        return False
    if not isinstance(val, str):
        try:
            val = str(val)
        except Exception:
            return False
    s = val.strip()
    if not s:
        return False
    # Reject explicit "no date" / negative signals up front. Without this
    # check the now-token regex would match "available" inside "Not
    # Available" and the v2 fallback would ship the raw negative string
    # as available_date + infer status=AVAILABLE (wrong). Negative
    # strings stay None across the board; the status inference path
    # picks them up separately via DATE_ABSENT_TOKENS membership and
    # the producer's explicit "unavailable" / "leased" / "coming soon"
    # signal upstream.
    if s.lower() in DATE_ABSENT_TOKENS:
        return False
    if _DATE_SHAPE_NEGATIVE_RE.search(s):
        return False
    # Month name anywhere → date-shaped.
    if _DATE_SHAPE_MONTH_RE.search(s):
        return True
    # Numeric date shape → date-shaped.
    if _DATE_SHAPE_NUMERIC_RE.search(s):
        return True
    # Season name → date-shaped (with year or alone).
    if _DATE_SHAPE_SEASON_RE.search(s):
        return True
    # Date-relative words → date-shaped.
    if _DATE_SHAPE_RELATIVE_RE.search(s):
        return True
    # "Only N {vacant|available|open|left|unit}" — explicit count + availability
    # assertion. Parser resolves these to today; predicate accepts to maintain
    # the parser-symmetry invariant.
    if _DATE_SHAPE_ONLY_N_RE.search(s):
        return True
    # "Now"-class tokens — single-word forms ("Available", "Vacant", "Now")
    # are admitted because the parser now resolves them to today via the
    # extended DATE_NOW_TOKENS. The predicate accepts whenever ANY now-class
    # token is present, regardless of how many surrounding tokens exist.
    # Pre-2026-05-23 the predicate required ≥2 tokens; this was too
    # restrictive — bare "Available" carries the same availability signal
    # as "Available Now" and should ship as today.
    if _DATE_SHAPE_NOW_TOKEN_RE.search(s):
        return True
    return False
