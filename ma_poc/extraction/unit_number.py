"""Apartment-number normalisation for cross-page identity matching.

A unit's apartment number is the property's natural key for that unit — but
the raw value arrives in many shapes across producers:

  * ``"618"``           — plain numeric
  * ``"Apt 618"``       — verbose prefix
  * ``"#618"``          — hash prefix
  * ``"Unit 618"``      — explicit prefix
  * ``"Suite 618"``     — alternate prefix
  * ``"Ste 618"``       — abbreviation
  * ``"618 "``          — trailing whitespace
  * ``"618.0"``         — numeric coerced through float
  * ``"618A"``          — alphanumeric suffix (must NOT collapse with ``618B``)

This module exposes a single helper, :func:`normalize_unit_number`, which
collapses these variants to a single canonical key while preserving the
alphanumeric suffix that genuinely distinguishes two physical units (101A
and 101B are different apartments).

See D16 plan, item 6: "Add a post-partition cross-page unit-number dedup
pass" — this normaliser is the keying primitive for that pass.

This module is **pure**: deterministic, no side effects, never raises.
"""

from __future__ import annotations

import re
from typing import Any, Final

from ma_poc.extraction.canonical import is_present

#: D16 — alias keys for reading the raw ``unit_number`` value off a unit
#: dict. Used by classify (Path 2 promotion), infer (item 7 short-circuit),
#: and post_process (cross-page dedup + inverse-B4 bucket keying).
#:
#: This is the SINGLE source of truth for unit-number aliases. Adding a
#: new alias here is a deliberate append; never inline the list at a call
#: site (drift risk — three call sites previously fell out of sync).
#:
#: Distinct from ``UID_KEYS`` in ``canonical.py``: ``UID_KEYS`` is the
#: unit-id-FIRST resolution list (returns ``unit_id`` even when it's
#: ``"inferred_<hash>"``). This tuple omits ``unit_id`` so callers can
#: ask "do you have a real apartment number?" without the inferred
#: fallback short-circuiting them.
UNIT_NUMBER_ALIASES: Final[tuple[str, ...]] = (
    "unit_number",
    "_unit_number",
    "unitnumber",
    "apartmentname",
    "apartment_name",
    "displayunitnumber",
    "display_unit_number",
)


def extract_unit_number(unit: dict[str, Any]) -> str | None:
    """Return the first present unit-number alias as a stripped string.

    Walks ``UNIT_NUMBER_ALIASES`` in order, unwraps ``FieldValue`` wrappers
    from the cross-source merger, and returns the first value that
    ``is_present`` admits. Returns ``None`` if no alias is present.

    Case-insensitive lookup — mirrors ``canonical._build_lower_view`` so
    PascalCase / camelCase / snake_case producer keys all hit the same
    alias.

    This is the **read-side primitive** for the unit-number identity
    pipeline. Combine with :func:`normalize_unit_number` to get a
    canonical key suitable for cross-page dedup buckets:

        raw = extract_unit_number(unit)
        key = normalize_unit_number(raw)

    Note: returns the raw string (whitespace-trimmed) without
    normalisation. Callers that want a comparable key must pipe through
    ``normalize_unit_number``.
    """
    if not isinstance(unit, dict):
        return None
    lc_view = {k.lower(): v for k, v in unit.items() if isinstance(k, str)}
    for key in UNIT_NUMBER_ALIASES:
        v = lc_view.get(key)
        # Unwrap FieldValue (provenance wrapper from the cross-source merger).
        if hasattr(v, "value") and hasattr(v, "source"):
            v = v.value
        if is_present(v):
            s = str(v).strip()
            if s:
                return s
    return None


#: Prefixes stripped from the raw unit-number string. Order-insensitive
#: (regex matches any of them). Case-insensitive at apply time.
#:
#: ``Suite`` / ``Ste`` come before ``Unit`` / ``Apt`` only for readability —
#: the regex alternation does not rank, it matches any.
_PREFIX_RE: Final = re.compile(
    r"^(?:apt|apartment|unit|suite|ste|no|number)\b\.?\s*",
    re.IGNORECASE,
)

#: Leading punctuation: ``#`` or whitespace. Stripped after prefix removal so
#: ``"# 618"`` and ``"#618"`` both collapse cleanly.
_LEADING_PUNCT_RE: Final = re.compile(r"^[#\s]+")

#: Trailing ``.0`` on numeric strings (the result of ``str(float_value)``
#: when an upstream producer coerced an int through float). ``"618.0"`` →
#: ``"618"``. Does NOT strip ``.0`` from non-numeric strings — ``"618A.0"``
#: is left as-is (highly improbable but defensive).
_TRAILING_DOT_ZERO_RE: Final = re.compile(r"^(\d+)\.0+$")


def normalize_unit_number(raw: Any) -> str | None:
    """Canonicalise a raw apartment-number value for cross-page identity.

    Returns ``None`` for inputs that cannot represent an apartment number:
    ``None``, empty / whitespace-only strings, non-string-coercible types,
    and the absence sentinel ``"-1"`` / ``-1``.

    Returns a lowercased, trimmed string for everything else. Preserves
    alphanumeric suffixes (``"101A"`` ≠ ``"101B"``).

    Args:
        raw: Raw unit-number value from any producer shape. Accepts ``str``,
            ``int``, ``float``, or anything ``str()``-coercible.

    Returns:
        Canonical key for use in cross-page dedup, or ``None`` when the
        input does not carry an apartment number.

    Examples:
        >>> normalize_unit_number("Apt 101")
        '101'
        >>> normalize_unit_number("#101")
        '101'
        >>> normalize_unit_number("101")
        '101'
        >>> normalize_unit_number("101 ")
        '101'
        >>> normalize_unit_number("101.0")
        '101'
        >>> normalize_unit_number("101A") == normalize_unit_number("101a")
        True
        >>> normalize_unit_number("101A") == normalize_unit_number("101B")
        False
        >>> normalize_unit_number(None) is None
        True
        >>> normalize_unit_number("") is None
        True
        >>> normalize_unit_number("-1") is None
        True
    """
    if raw is None:
        return None

    # Absence sentinel: distinguish from a real "-1" apartment number
    # (no such apartment exists) and from missing data.
    if isinstance(raw, int) and raw == -1:
        return None

    try:
        s = str(raw).strip()
    except Exception:
        return None

    if not s:
        return None

    # Treat the absence sentinel in string form as absent too.
    if s == "-1":
        return None

    # Strip recognised prefixes (case-insensitive). Run the regex once —
    # only the first prefix is stripped because chained prefixes
    # ("Apt Suite 101") are not a real shape.
    s = _PREFIX_RE.sub("", s)

    # Strip leading ``#`` and any whitespace surfaced by prefix removal.
    s = _LEADING_PUNCT_RE.sub("", s)

    s = s.strip()
    if not s:
        return None

    # Trailing ``.0`` on a purely-numeric string: the result of producers
    # that coerced an int through float (``str(618.0)`` → ``"618.0"``).
    m = _TRAILING_DOT_ZERO_RE.match(s)
    if m:
        s = m.group(1)

    # Lowercase to collapse case-variant alphanumeric suffixes
    # (``"101A"`` and ``"101a"`` are the same apartment).
    return s.lower()
