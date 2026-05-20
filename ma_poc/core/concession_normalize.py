"""Lightweight structured-concession parser.

Companion to :mod:`ma_poc.core.concession_clean`. Where ``concession_clean``
emits a *display-ready string* alongside a quality label, this module
attempts to emit a *structured dict* downstream code can pivot on
without re-running the regex sweep.

Contract — *raw is the system of record; structured is a derivative*:

    :func:`normalize_concession` returns ``None`` whenever the input
    cannot be confidently parsed into one of the supported shapes. The
    caller MUST retain the raw text in a sibling field
    (``concession_text`` / ``concessions``) so downstream consumers can
    fall back to it when ``concessions_json`` is ``None``.

Supported shapes (best-effort regex extraction — no LLM, no external
calls, zero $0 cost):

    * ``{"type": "free_rent", "free_period": {"value": 2, "unit": "months"}, ...}``
    * ``{"type": "discount", "amount": {"value": 500, "currency": "USD"}, ...}``
    * ``{"type": "percent_off", "percent": 10, ...}``
    * ``{"type": "waived_fee", "fee_kind": "application", ...}``
    * ``{"type": "reduced_deposit", ...}``
    * ``{"type": "look_and_lease", ...}``

Each output dict additionally carries:

    * ``source: "TEXT"`` — capture provenance label (vision banner
      callers override to ``"IMAGE_BANNER"``).
    * ``deadline: str | None`` — best-effort move-in / lease-by date
      string (raw, not date-parsed; downstream date pipeline owns
      format normalisation).
    * ``conditions: str | None`` — short trailing copy that follows
      the offer phrase, capped to 80 chars.
    * ``text: str`` — the source string the dict was derived from
      (whitespace-normalised) so consumers don't have to thread the
      raw alongside.

Anything not matched returns ``None``. Adding a new offer shape is
additive: add a regex + a builder + append to ``_RULES``.
"""

from __future__ import annotations

import re
from typing import Any

# ─────────────────────────────────────────────────────────────────────
# Number-word helper (so "two months free" parses alongside "2 months free")
# ─────────────────────────────────────────────────────────────────────

_NUM_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}


def _to_int(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        try:
            return int(token)
        except ValueError:
            return None
    return _NUM_WORDS.get(token)


def _amount_to_int(token: str) -> int | None:
    """Parse a comma-grouped dollar amount string into an int."""
    digits = re.sub(r"[^\d]", "", token)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────
# Deadline + conditions extraction (best-effort — None when absent)
# ─────────────────────────────────────────────────────────────────────

_DEADLINE_RE = re.compile(
    r"\b(?:"
    r"move[\s\-]?in\s+by|lease\s+by|sign\s+by|apply\s+by|"
    r"valid\s+(?:through|until|thru)|expires?(?:\s+on)?|"
    r"by|before|until|thru"
    r")\s+"
    r"(?P<deadline>"
    # Mon DD or Mon DDth / Month DD, YYYY
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?"
    # DD Mon / DD Mon YYYY
    r"|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?(?:\s+\d{4})?"
    # MM/DD or MM/DD/YYYY or MM-DD or MM-DD-YYYY
    r"|\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?"
    r")",
    re.IGNORECASE,
)

_CONDITION_TRIM_RE = re.compile(r"\s+")


def _extract_deadline(text: str) -> str | None:
    m = _DEADLINE_RE.search(text)
    if not m:
        return None
    return _CONDITION_TRIM_RE.sub(" ", m.group("deadline")).strip()


def _extract_conditions(text: str, anchor_end: int) -> str | None:
    """Capture up to 80 chars of qualifier copy after the matched offer.

    Trimmed to a comma, period, or end-of-string boundary so we don't
    leak unrelated marketing copy.
    """
    tail = text[anchor_end:anchor_end + 120].strip()
    if not tail:
        return None
    # Stop at the next sentence boundary so we don't pull in unrelated
    # marketing copy.
    cut = re.search(r"[.!|•·]", tail)
    if cut:
        tail = tail[:cut.start()]
    tail = _CONDITION_TRIM_RE.sub(" ", tail).strip(" ,:-")
    if not tail or len(tail) < 4:
        return None
    return tail[:80]


# ─────────────────────────────────────────────────────────────────────
# Offer-shape rules
# ─────────────────────────────────────────────────────────────────────

_FREE_PERIOD_RE = re.compile(
    r"(?P<n>\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"[\s\-]+"
    r"(?:full[\s\-]+)?"
    r"(?P<unit>weeks?|months?|days?)"
    r"(?:[\s\-]+of)?"
    r"[\s\-]+"
    r"(?:rent[\s\-]+)?"
    r"(?:free|of[\s\-]+free|on[\s\-]+us|complimentary)",
    re.IGNORECASE,
)

# Inverted form: "free rent for 2 months" / "free for one month"
_FREE_PERIOD_INVERTED_RE = re.compile(
    r"(?:rent[\s\-]+)?free\s+(?:rent\s+)?(?:for\s+)?"
    r"(?P<n>\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"[\s\-]+"
    r"(?:full[\s\-]+)?"
    r"(?P<unit>weeks?|months?|days?)",
    re.IGNORECASE,
)

_DOLLAR_OFF_RE = re.compile(
    r"\$\s*(?P<amount>\d{1,3}(?:,\d{3})*|\d+)"
    r"\s*(?:off|gift\s*card|credit|cash|savings|welcome\s+bonus)?",
    re.IGNORECASE,
)

_SAVE_RE = re.compile(
    r"save\s+(?:up\s+to\s+)?\$\s*(?P<amount>\d{1,3}(?:,\d{3})*|\d+)",
    re.IGNORECASE,
)

_PERCENT_OFF_RE = re.compile(
    # Word boundary on the left so ``150% off`` doesn't match as
    # ``50% off`` — out-of-range percentages must NOT register.
    r"(?<!\d)(?P<percent>\d{1,2}(?:\.\d+)?)\s*%\s*off",
    re.IGNORECASE,
)

_WAIVED_FEE_RE = re.compile(
    r"waived\s+(?P<kind>application|admin(?:istration)?|amenity|move[\s\-]?in|deposit)"
    r"\s*fees?",
    re.IGNORECASE,
)

_REDUCED_DEPOSIT_RE = re.compile(r"reduced\s+deposit", re.IGNORECASE)
_LOOK_AND_LEASE_RE = re.compile(r"look[\s\-]+(?:and|&|n|\+)[\s\-]+lease", re.IGNORECASE)


def _build_free_rent(m: re.Match[str], text: str) -> dict[str, Any] | None:
    n = _to_int(m.group("n"))
    unit = m.group("unit").lower().rstrip("s")
    if n is None or n <= 0 or n > 24:
        return None
    return {
        "type": "free_rent",
        "free_period": {"value": n, "unit": unit + "s"},
        "deadline": _extract_deadline(text),
        "conditions": _extract_conditions(text, m.end()),
    }


def _build_dollar_off(m: re.Match[str], text: str) -> dict[str, Any] | None:
    amt = _amount_to_int(m.group("amount"))
    if amt is None or amt <= 0:
        return None
    return {
        "type": "discount",
        "amount": {"value": amt, "currency": "USD"},
        "deadline": _extract_deadline(text),
        "conditions": _extract_conditions(text, m.end()),
    }


def _build_save(m: re.Match[str], text: str) -> dict[str, Any] | None:
    amt = _amount_to_int(m.group("amount"))
    if amt is None or amt <= 0:
        return None
    return {
        "type": "discount",
        "amount": {"value": amt, "currency": "USD"},
        "deadline": _extract_deadline(text),
        "conditions": _extract_conditions(text, m.end()),
    }


def _build_percent_off(m: re.Match[str], text: str) -> dict[str, Any] | None:
    try:
        pct = float(m.group("percent"))
    except (ValueError, TypeError):
        return None
    if pct <= 0 or pct > 100:
        return None
    return {
        "type": "percent_off",
        "percent": pct,
        "deadline": _extract_deadline(text),
        "conditions": _extract_conditions(text, m.end()),
    }


def _build_waived_fee(m: re.Match[str], text: str) -> dict[str, Any] | None:
    kind = m.group("kind").lower().replace(" ", "_").replace("-", "_")
    return {
        "type": "waived_fee",
        "fee_kind": kind,
        "deadline": _extract_deadline(text),
        "conditions": _extract_conditions(text, m.end()),
    }


def _build_reduced_deposit(m: re.Match[str], text: str) -> dict[str, Any] | None:
    return {
        "type": "reduced_deposit",
        "deadline": _extract_deadline(text),
        "conditions": _extract_conditions(text, m.end()),
    }


def _build_look_and_lease(m: re.Match[str], text: str) -> dict[str, Any] | None:
    return {
        "type": "look_and_lease",
        "deadline": _extract_deadline(text),
        "conditions": _extract_conditions(text, m.end()),
    }


# Ordered list — first match wins, so the most specific shapes are
# placed before the more permissive ones. ``$X off`` runs last among
# the dollar/percent siblings because ``save $X`` is more specific.
_RULES: tuple[tuple[re.Pattern[str], Any], ...] = (
    (_FREE_PERIOD_RE, _build_free_rent),
    (_FREE_PERIOD_INVERTED_RE, _build_free_rent),
    (_SAVE_RE, _build_save),
    (_PERCENT_OFF_RE, _build_percent_off),
    (_WAIVED_FEE_RE, _build_waived_fee),
    (_REDUCED_DEPOSIT_RE, _build_reduced_deposit),
    (_LOOK_AND_LEASE_RE, _build_look_and_lease),
    (_DOLLAR_OFF_RE, _build_dollar_off),
)


def normalize_concession(text: str | None, source: str = "TEXT") -> dict[str, Any] | None:
    """Parse *text* into a structured concession dict.

    Returns ``None`` when no rule matches — the caller's raw-text field
    remains the source of truth.

    ``source`` labels the capture provenance (``"TEXT"`` for the page-
    HTML scrape, ``"IMAGE_BANNER"`` for vision-LLM captures, ``"API"``
    for adapter-emitted concession fields, etc.). The label is opaque
    to this module — pass whatever your caller wants downstream
    consumers to see.
    """
    if not text or not isinstance(text, str) or not text.strip():
        return None

    norm_text = _CONDITION_TRIM_RE.sub(" ", text).strip()

    for pattern, builder in _RULES:
        m = pattern.search(norm_text)
        if not m:
            continue
        result = builder(m, norm_text)
        if result is None:
            continue
        result["source"] = source
        result["text"] = norm_text[:300]
        return result

    return None
