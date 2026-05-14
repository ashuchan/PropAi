"""Floor-plan structural signal detection and field-name normalisation.

All regex patterns, field-name aliases, signal-count logic, and threshold
decisions for identifying floor-plan data live here — single source of
truth for the signal engine.  Nothing outside this module should define its
own bed/bath/area patterns or duplicate the threshold constants.

Public API
----------
count_floor_plan_signals(text)              → int  (0-4, one point per signal type)
has_floor_plan_signals(text, threshold?)    → bool (True when score ≥ threshold)
normalize_field_key(key)                    → str  (canonical lowercase key)
normalize_field_keys(keys)                  → frozenset[str]
normalize_field_dict(d)                     → dict (keys remapped, values unchanged)
parse_floor_plan_label(label)               → dict (beds/baths extracted from "1BR/1BA")

Threshold constants (use these — never hardcode 1 or 2 at call sites)
----------------------------------------------------------------------
SIGNAL_THRESHOLD_ANY        = 1   — at least one structural type present
                                    (post-scroll check, LLM gate relaxation)
SIGNAL_THRESHOLD_STRUCTURAL = 2   — two or more distinct structural types
                                    (scroll suppress, portal-wait suppress,
                                     page_has_content_signals, hop richness,
                                     DOM section selection)
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "count_floor_plan_signals",
    "has_floor_plan_signals",
    "normalize_field_key",
    "normalize_field_keys",
    "normalize_field_dict",
    "parse_floor_plan_label",
    "FIELD_ALIASES",
    "SIGNAL_THRESHOLD_ANY",
    "SIGNAL_THRESHOLD_STRUCTURAL",
]

# ---------------------------------------------------------------------------
# Threshold constants — single source of truth for the signal level required
# at each call site.  Import and use these; never write a bare integer.
# ---------------------------------------------------------------------------

SIGNAL_THRESHOLD_ANY: int = 1
"""At least one structural floor-plan signal type is present.

Use when a single structural element (e.g. a bedroom label) is sufficient
evidence that the page *might* have unit data worth processing.

Call sites:
- Post-scroll ``_fp_appeared`` check in fetcher.py
- LLM gate relaxation ``strict_match`` in generic.py
"""

SIGNAL_THRESHOLD_STRUCTURAL: int = 2
"""Two or more distinct structural floor-plan signal types are present.

Two types (e.g. bedroom + area, or bedroom + bathroom) is the threshold
for "genuine unit data" vs marketing copy with a single bedroom label or
price mention.

Call sites:
- Scroll trigger suppression in fetcher.py     (don't scroll if already ≥2)
- Portal late-render suppression in fetcher.py (skip 12s wait if already ≥2)
- ``page_has_content_signals`` in generic.py    (suppress RC3 deferral)
- ``_link_hop_is_rich`` in scraper.py           (grant LLM budget refresh)
- DOM section selection in ``_extract_rent_dom_section`` (generic.py)
"""

# ---------------------------------------------------------------------------
# Structural floor-plan regex patterns
# ---------------------------------------------------------------------------

# Bedrooms: "1BR", "2 BR", "1 bed", "3 beds", "2 bedroom", "4 bedrooms",
#           "2R", "3 Rooms", "Studio" (→ 0), "Eff" / "Efficiency" (→ 0)
_RE_BEDROOMS = re.compile(
    r"""
    (?:
        \b(?P<studio>studio|efficiency|eff)\b          # Studio / Efficiency (0 BR)
        |
        \b(?P<n>\d+)\s*                                # leading digit
        (?:br|bd|bed(?:room)?s?|r(?:oom)?s?)          # br / bd / bed / room
        \b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Bathrooms: "1BA", "1 BA", "1 bath", "2 baths", "1.5 bath", "2 bathrooms",
#            "full bath", "half bath"
_RE_BATHROOMS = re.compile(
    r"""
    (?:
        \b(?:full|half)\s+bath(?:room)?s?\b            # "full bath" / "half bath"
        |
        \b(?P<n>\d+(?:\.\d+)?)\s*                      # "1", "1.5"
        (?:ba|bath(?:room)?s?)                         # ba / bath / bathroom
        \b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Area / square footage: "750 sqft", "750 sq ft", "750 SF", "750 sq. ft.",
#                        "750 square feet", "750 square foot", "750sf"
_RE_AREA = re.compile(
    r"""
    \b(?P<n>\d{3,5})           # 3-5 digit number (100 – 99999)
    \s*                        # optional whitespace
    (?:
        sq\.?\s*ft\.?          # sq ft / sq. ft.
        | sqft                 # sqft
        | sf                   # sf
        | square\s*f(?:eet|oot|t\.?)  # square feet / foot / ft.
    )
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Floor-plan type shorthands: "1/1", "2/2", "1BR/1BA", "2Bed/2Bath",
#                              "1 Bed / 1 Bath", "Studio"
_RE_FLOOR_PLAN_TYPE = re.compile(
    r"""
    (?:
        \b(?:studio|efficiency)\b                           # Studio / Eff
        |
        \b\d+\s*(?:br?|bed(?:room)?s?)                     # leading beds part
        \s*/\s*                                             # separator
        \d+(?:\.\d+)?\s*(?:ba?|bath(?:room)?s?)            # baths part
        \b
        |
        \b\d+\s*/\s*\d+\b                                   # bare "1/1", "2/2"
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# ---------------------------------------------------------------------------
# Field-name alias table
# ---------------------------------------------------------------------------
# Maps non-canonical field names → canonical signal-engine key (lowercase).
# All lookups are done on the lowercased, hyphen-to-underscore normalised key.
# Add entries here whenever a new PMS API uses an unknown field name.

FIELD_ALIASES: dict[str, str] = {
    # ── Bedrooms ──────────────────────────────────────────────────────────
    "no_of_bedroom":      "bedrooms",
    "numberofbedrooms":   "bedrooms",
    "num_bedrooms":       "bedrooms",
    "bedcount":           "bedrooms",
    "bedsnumber":         "bedrooms",
    "bedroom_count":      "bedrooms",
    "bedroomcount":       "bedrooms",
    "bedrooms_count":     "bedrooms",
    "br":                 "bedrooms",
    "bed":                "bedrooms",
    "bedscount":          "bedrooms",
    # ── Bathrooms ─────────────────────────────────────────────────────────
    "no_of_bathroom":     "bathrooms",
    "no_of_bath":         "bathrooms",
    "numberofbathrooms":  "bathrooms",
    "num_bathrooms":      "bathrooms",
    "bathcount":          "bathrooms",
    "bathroom_count":     "bathrooms",
    "bathroomcount":      "bathrooms",
    "bathrooms_count":    "bathrooms",
    # "baths" is NOT aliased — canonical key used natively by RentCafe; do not map to "bathrooms"
    "ba":                 "bathrooms",
    # ── Area / sqft ───────────────────────────────────────────────────────
    "square_footage":     "sqft",
    "squarefeet":         "sqft",
    "squarefootage":      "sqft",
    "square_feet":        "sqft",
    "sq_ft":              "sqft",
    "sf":                 "sqft",
    "area":               "sqft",
    "minimumsquarefeet":  "sqft",
    "squarefeet_min":     "sqft",
    # ── Floor-plan name / ID ──────────────────────────────────────────────
    "floorplan_name":     "floor_plan_name",
    "floorplanname":      "floor_plan_name",
    "floor_plan":         "floor_plan_name",
    "plan_name":          "floor_plan_name",
    "planname":           "floor_plan_name",
    "unittype":           "floor_plan_name",
    "unit_type":          "floor_plan_name",
    # Note: "floorplan-name" (with hyphen) is NOT a dict key here because
    # normalize_field_key() converts hyphens to underscores before the lookup,
    # so it arrives as "floorplan_name" → already handled by the entry above.
    # ── Rent ──────────────────────────────────────────────────────────────
    "minrent":            "min_rent",
    "maxrent":            "max_rent",
    "minimumrent":        "min_rent",
    "maximumrent":        "max_rent",
    "minimummarketrent":  "min_rent",
    "maximummarketrent":  "max_rent",
    "askingrent":         "rent",
    "monthlyrent":        "rent",
    "baserent":           "rent",
    "market_rent":        "rent",
    # ── Unit / availability ───────────────────────────────────────────────
    "unitnumber":         "unit_number",
    "unitid":             "unit_id",
    "availabledate":      "available_date",
    "availableon":        "available_date",
    "availablecount":     "unit_count",
}


def normalize_field_key(key: str) -> str:
    """Return the canonical signal-engine key for *key*.

    Steps:
    1. Lowercase + replace hyphens and spaces with underscores.
    2. Look up in ``FIELD_ALIASES``; return the alias if found.
    3. Otherwise return the normalised key unchanged.

    This is O(1) — exact dict lookup only, no fuzzy matching.
    Fuzzy matching is reserved for the ranker layer; use this function
    before feeding keys into FieldCombination / has_unit_signals.

    Examples
    --------
    >>> normalize_field_key("no_of_bedroom")
    'bedrooms'
    >>> normalize_field_key("squareFeet")
    'sqft'
    >>> normalize_field_key("floorplan-name")
    'floor_plan_name'
    >>> normalize_field_key("rent")     # already canonical
    'rent'
    """
    normalised = key.lower().replace("-", "_").replace(" ", "_")
    return FIELD_ALIASES.get(normalised, normalised)


def normalize_field_keys(keys: frozenset[str] | set[str]) -> frozenset[str]:
    """Return a new frozenset with every key passed through ``normalize_field_key``."""
    return frozenset(normalize_field_key(k) for k in keys)


def normalize_field_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *d* with all keys normalised; values are unchanged.

    Useful before feeding an API response item into ``has_unit_signals`` or
    a FieldCombination check so both camelCase and snake_case variants map
    to the same canonical key.

    When two source keys normalise to the same canonical key the last one
    in iteration order wins (dict ordering is insertion-order in CPython 3.7+).
    """
    return {normalize_field_key(k): v for k, v in d.items()}


# ---------------------------------------------------------------------------
# Floor-plan label parser   "1BR/1BA"  →  {"beds": 1, "baths": 1.0}
# ---------------------------------------------------------------------------

# Matches the full "1BR/1BA" / "2Bed/2Bath" / "1/1" / "Studio" shorthand
_RE_LABEL_BEDS = re.compile(
    r"^(?P<studio>studio|eff(?:iciency)?)"
    r"|^(?P<n>\d+)\s*(?:br?|bed(?:room)?s?|r(?:oom)?s?)?",
    re.IGNORECASE,
)
_RE_LABEL_BATHS = re.compile(
    r"(?P<n>\d+(?:\.\d+)?)\s*(?:ba?|bath(?:room)?s?)?$",
    re.IGNORECASE,
)


def parse_floor_plan_label(label: str) -> dict[str, int | float | None]:
    """Extract beds and baths from a compact floor-plan label string.

    Handles:
    - "Studio"           → {"beds": 0, "baths": None}
    - "1BR/1BA"          → {"beds": 1, "baths": 1.0}
    - "2Bed/2Bath"       → {"beds": 2, "baths": 2.0}
    - "1/1"              → {"beds": 1, "baths": 1.0}
    - "1 Bed / 1.5 Bath" → {"beds": 1, "baths": 1.5}
    - "3BR"              → {"beds": 3, "baths": None}
    - ""                 → {"beds": None, "baths": None}

    Returns a dict with keys ``"beds"`` (int | None) and ``"baths"``
    (float | None).  Missing values are ``None``, never raised.
    """
    result: dict[str, int | float | None] = {"beds": None, "baths": None}
    if not label:
        return result

    label = label.strip()
    # Split on "/" separating beds from baths
    parts = re.split(r"\s*/\s*", label, maxsplit=1)

    # --- beds part ---
    m = _RE_LABEL_BEDS.match(parts[0].strip())
    if m:
        if m.group("studio"):
            result["beds"] = 0
        elif m.group("n") is not None:
            result["beds"] = int(m.group("n"))

    # --- baths part ---
    if len(parts) == 2:
        m2 = _RE_LABEL_BATHS.search(parts[1].strip())
        if m2 and m2.group("n") is not None:
            result["baths"] = float(m2.group("n"))

    return result


# ---------------------------------------------------------------------------
# Floor-plan signal counter
# ---------------------------------------------------------------------------

def count_floor_plan_signals(text: str) -> int:
    """Count how many distinct structural floor-plan signal types are present.

    Scores 0–4, one point per signal type found anywhere in *text*:
    - 1 point: bedroom mention  (1BR / 1 bed / Studio …)
    - 1 point: bathroom mention (1BA / 1 bath / 1.5 bath …)
    - 1 point: area mention     (750 sqft / 750 sq ft …)
    - 1 point: floor-plan type  (1/1 / 2BR/1BA / Studio …)

    A score ≥ 2 indicates the text contains genuine floor-plan structure
    (not just a marketing banner with one price mention).
    A score of 0 or 1 against text that already has rent signals suggests
    marketing copy rather than unit data.

    Args:
        text: Plain text or HTML-stripped text from a DOM element or page body.

    Returns:
        Integer 0–4.
    """
    if not text:
        return 0
    score = 0
    if _RE_BEDROOMS.search(text):
        score += 1
    if _RE_BATHROOMS.search(text):
        score += 1
    if _RE_AREA.search(text):
        score += 1
    if _RE_FLOOR_PLAN_TYPE.search(text):
        score += 1
    return score


def has_floor_plan_signals(
    text: str,
    threshold: int = SIGNAL_THRESHOLD_STRUCTURAL,
) -> bool:
    """Return True when *text* has at least *threshold* structural signal types.

    This is the single call-site function for all floor-plan signal checks
    across the codebase.  Use the named threshold constants instead of bare
    integers so the threshold decision is visible at the definition site:

        from ma_poc.pms.signal_engine.floor_plan_signals import (
            has_floor_plan_signals,
            SIGNAL_THRESHOLD_ANY,
            SIGNAL_THRESHOLD_STRUCTURAL,
        )

        # Scroll trigger: fire when NO signals at all
        if not has_floor_plan_signals(body, SIGNAL_THRESHOLD_ANY):
            scroll()

        # Suppress portal wait: skip if already structurally rich
        if has_floor_plan_signals(body, SIGNAL_THRESHOLD_STRUCTURAL):
            skip_late_render_wait()

    Args:
        text:      Plain text or HTML-stripped text to inspect.
        threshold: Minimum score required.  Defaults to
                   ``SIGNAL_THRESHOLD_STRUCTURAL`` (2) — "genuine unit data."

    Returns:
        ``True`` when ``count_floor_plan_signals(text) >= threshold``.
    """
    return count_floor_plan_signals(text) >= threshold
