"""Deterministic concession normalizer: raw banner/modal text -> RealPage
``concessions_json`` object.

Authority is the 20 worked examples (regression oracle in
``tests/test_concession_normalize.py``), NOT any prior code. The
concession-phrasing space was empirically characterized as closed and
stable (cohort + random-20/20 probe, 2026-05-19 — see
investigations/.../CONCESSION_PROBE_2026-05-19.md), so a fixed rule set
is preferred over an LLM: auditable, free, regression-testable, no drift.

Capture-first contract: the caller ALWAYS retains the raw ``concessions``
text. This returns ``None`` when nothing is confidently parseable — that
is correct behavior, not data loss (the raw string is still kept and any
genuinely-novel structure surfaces there as a future rule).

Output vocabulary is exactly what the oracle produces:
  {"obj": {"free": {"monthsDiscounted": <num>, "dollarsLow": <int>},
           "rr":   {"dollarsLow": <int>},
           "leaseTerm": <int>}}
Sub-objects/keys are omitted when absent. Weeks->months = weeks / 4
(2wk=0.5, 4wk=1, 6wk=1.5). ``$X off [the] first [full] month`` is a
one-time free-rent credit (``free.dollarsLow`` + ``monthsDiscounted:1``);
``$X off [monthly] rent`` is a recurring reduction (``rr.dollarsLow``).
"""

from __future__ import annotations

import re
from typing import Any

_WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_NUM = r"(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"


def _to_num(tok: str | None) -> float | None:
    if tok is None:
        return None
    t = tok.strip().lower()
    if t in _WORD_NUM:
        return float(_WORD_NUM[t])
    try:
        return float(t)
    except ValueError:
        return None


def _num_out(v: float) -> Any:
    """Whole numbers as int (matches oracle: 1, 2, 3); fractions as float
    (0.5, 1.5)."""
    return int(v) if v == int(v) else v


def _months_from(count: float, unit: str) -> float:
    return count / 4.0 if unit.startswith("w") else count


# $X off [your] [the] first [full] month['s [rent]] / $X off move-in
# -> one-time credit: free.dollarsLow + monthsDiscounted = 1
_DOLLAR_OFF_FIRST = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})+|\d{2,5})\s*(?:off|discount(?:ed)?)\s*"
    r"(?:your\s+|the\s+)*(?:first\s+(?:full\s+)?month|move[\s-]*in)",
    re.IGNORECASE,
)
# $X off [your] [the] [monthly] rent  (recurring reduction) — NOT "first month"
_DOLLAR_OFF_RENT = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})+|\d{2,4})\s*(?:off|discount(?:ed)?)\s*"
    r"(?:your\s+|the\s+|on\s+)*(?:monthly\s+)?rent\b",
    re.IGNORECASE,
)
# N (week|month)s ... free | of rent free | on us   (count before unit).
# `[\s-]*` covers hyphenated copy ("6-weeks-free"); `['’]s?` covers the
# possessive "two months' rent on us"; terminator includes "on us".
_FREE_COUNT_UNIT = re.compile(
    rf"({_NUM})[\s-]*(?:full[\s-]+)?(month|week)s?(?:['’]s?)?[\s-]*"
    r"(?:of[\s-]+)?(?:rent[\s-]+)?"
    r"(?:free|complimentary|on\s+us|on\s+the\s+house)\b",
    re.IGNORECASE,
)
# free before unit; count optional -> 1. Also "rent-free for N weeks"
# ("for" gap + unit after free).
_FREE_REV = re.compile(
    rf"\b(?:rent[\s-]?)?free\s+(?:for\s+)?(?:({_NUM})\s+)?(?:full\s+)?(month|week)s?\b",
    re.IGNORECASE,
)
# first [N] [full] month(s) ... free  ("1st TWO full months are free" -> 2;
# "first full month ... free" -> 1)
_FIRST_MONTHS_FREE = re.compile(
    rf"(?:first|1st)\s+(?:({_NUM})\s+)?(?:full\s+)?months?\b[^.!?]*?\bfree\b",
    re.IGNORECASE,
)
# N month(s) free base rent  ("3 Months Free Base Rent")
_N_FREE_BASE = re.compile(
    rf"({_NUM})\s*months?\s+free\s+base\s+rent", re.IGNORECASE,
)
# lease term tied to offer: "12 month lease", "12+ month", "12-14 month lease"
_LEASE_TERM = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?month\s+lease", re.IGNORECASE,
)


def _dollar(tok: str) -> int:
    return int(tok.replace(",", ""))


def normalize_concession(raw: Any) -> dict[str, Any] | None:
    """Raw concession text -> ``{"obj": {...}}`` or ``None`` if unparseable.

    Deterministic; never raises. ``None`` is valid (caller keeps raw text).
    """
    if not raw or not isinstance(raw, str):
        return None
    t = re.sub(r"\s+", " ", raw).strip()
    if not t:
        return None

    free: dict[str, Any] = {}
    rr: dict[str, Any] = {}

    # 1. $X off first month -> one-time credit (free.dollarsLow + 1 month)
    m = _DOLLAR_OFF_FIRST.search(t)
    if m:
        free["dollarsLow"] = _dollar(m.group(1))
        free["monthsDiscounted"] = 1

    # 2. recurring $ off rent -> rr.dollarsLow
    m = _DOLLAR_OFF_RENT.search(t)
    if m:
        rr["dollarsLow"] = _dollar(m.group(1))

    # 3. free-time months (only set monthsDiscounted if not already from $-off)
    if "monthsDiscounted" not in free:
        months: float | None = None
        for rx in (_N_FREE_BASE, _FREE_COUNT_UNIT, _FIRST_MONTHS_FREE, _FREE_REV):
            mm = rx.search(t)
            if not mm:
                continue
            if rx is _N_FREE_BASE:
                c = _to_num(mm.group(1))
                if c is not None:
                    months = c
            elif rx is _FIRST_MONTHS_FREE:
                c = _to_num(mm.group(1))
                months = c if c is not None else 1.0
            else:  # _FREE_COUNT_UNIT / _FREE_REV: (count, unit)
                c = _to_num(mm.group(1))
                unit = mm.group(2) or "month"
                if c is None:
                    c = 1.0  # bare "free month" => 1
                months = _months_from(c, unit.lower())
            if months is not None:
                break
        if months is not None and months > 0:
            free["monthsDiscounted"] = _num_out(months)

    obj: dict[str, Any] = {}
    if free:
        obj["free"] = free
    if rr:
        obj["rr"] = rr

    # 4. lease term — only meaningful alongside a detected concession
    if obj:
        lm = _LEASE_TERM.search(t)
        if lm:
            obj["leaseTerm"] = int(lm.group(1))

    return {"obj": obj} if obj else None
