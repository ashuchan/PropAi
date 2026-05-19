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
DATE_NOW_TOKENS: frozenset[str] = frozenset({
    "now", "today", "immediate", "immediately", "imm",
    "asap", "ready now", "ready", "current", "currently",
    "open now", "available now",
})

#: Placeholder strings that explicitly mean "no date available" — kept
#: distinct from format-mismatch so the producer's intent isn't lost in
#: logs. Both map to None (the v2 contract uses None for absent dates).
DATE_ABSENT_TOKENS: frozenset[str] = frozenset({
    "n/a", "na", "tbd", "tba", "call", "contact", "inquire",
    "unavailable", "unavail", "leased", "rented", "not available",
    "coming soon", "wait list", "waitlist", "-", "--",
})


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

    return None
