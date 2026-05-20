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
    "count_listing_structural_signals",
    "has_listing_structure",
    "normalize_field_key",
    "normalize_field_keys",
    "normalize_field_dict",
    "parse_floor_plan_label",
    "FIELD_ALIASES",
    "SIGNAL_THRESHOLD_ANY",
    "SIGNAL_THRESHOLD_STRUCTURAL",
    "SIGNAL_THRESHOLD_HIGH",
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

SIGNAL_THRESHOLD_HIGH: int = 4
"""Four or more distinct structural signal types — a saturated unit page.

Four signal types out of the five-ish we recognise (bedrooms, bathrooms,
area, studio/efficiency, rent token) means the page is essentially
guaranteed to be a real listing page rather than marketing copy. Used to
grant ONE free LLM call per property when extraction lands on such a page
(see Bug #3, 2026-05-18): the page is so signal-rich that the LLM cost is
worth absorbing even when the per-property budget is otherwise exhausted.
The free-call grant is one-shot per property to bound worst-case cost.

Call sites:
- Free LLM-monolithic call gate in ``generic.py`` (Bug #3 fix)
- Free LLM-DOM-targeted call gate in ``generic.py`` (Bug #3 fix)
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
    "numbedrooms":        "bedrooms",      # Wix wix-data collection (no underscore)
    "bedroomsnumber":     "bedrooms",      # Wix Repeater itemData
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
    "numberofbathroomstotal": "bathrooms",  # Schema.org
    "num_bathrooms":      "bathrooms",
    "numbathrooms":       "bathrooms",      # Wix wix-data collection
    "bathroomsnumber":    "bathrooms",      # Wix Repeater itemData
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
    # Schema.org Apartment.floorSize (after lowercase normalisation).
    "floorsize":          "sqft",
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
    "pricetext":          "rent",        # Wix display string ("$1,450")
    "pricepermonth":      "rent",        # Wix monthly rent
    "rentpermonth":       "rent",        # Wix variant
    "priceamount":        "rent",        # Wix Studio numeric
    # F6 (2026-05-20 PID 218985 cohort): MAA / Cortland / Bell embedded
    # JSON payloads observed in the avail-gap audit. The walker found the
    # unit lists but skipped the rent because these key spellings weren't
    # in the alias table. Best-effort — needs canary validation against
    # a live MAA tenant page since the maac.com host CF-blocks ad-hoc
    # requests. If a key here doesn't match the live payload it costs
    # nothing (the alias table is read-only); if a key matches it
    # recovers rent on ~23 properties / day.
    "loweffectiverent":   "min_rent",    # MAA
    "lowestrent":         "min_rent",    # MAA / Cortland fallback name
    "lowmarketrent":      "min_rent",    # MAA variant
    "highestrent":        "max_rent",    # MAA / Cortland
    "highmarketrent":     "max_rent",    # MAA variant
    "higheffectiverent":  "max_rent",    # MAA
    "starting_rent":      "min_rent",    # marketing-CMS shape
    "startingat":         "min_rent",    # display variant
    "starting_at":        "min_rent",
    "effectiverent":      "rent",        # MAA per-unit
    "netrent":            "rent",        # Bell / Equity variant
    "marketrent":         "rent",        # also covers MAA per-unit lower-case
    # ── Unit / availability ───────────────────────────────────────────────
    "unitnumber":         "unit_number",
    "unitid":             "unit_id",
    "availabledate":      "available_date",
    "availableon":        "available_date",
    "availablecount":     "unit_count",
}


# ---------------------------------------------------------------------------
# Fuzzy normalisation for camelCase / snake_case / vendor-prefix variants
# ---------------------------------------------------------------------------
# When the exact-match table above misses, try a small set of structural
# rewrites BEFORE returning the unchanged key. Each rewrite is conservative
# (matches a small fixed set of patterns) so we don't accidentally map
# unrelated keys to canonical names.
#
# Common pattern families:
#   num<X>         → <X>s         (numBedrooms → bedrooms)
#   number<X>      → <X>s         (numberBedrooms → bedrooms; handled separately for "of")
#   <X>Number      → <X>s         (bedroomsNumber → bedrooms)
#   <X>Count       → <X>s         (bedroomCount → bedrooms; already aliased)
#   total<X>       → <X>s         (totalBedrooms → bedrooms)
#
# The rewrites run in fixed order; first hit wins.

_FUZZY_STEM_CANONICAL: dict[str, str] = {
    # stem (after pluralisation) → canonical key
    "bedroom":  "bedrooms",
    "bedrooms": "bedrooms",
    "bed":      "bedrooms",
    "beds":     "bedrooms",
    "bathroom": "bathrooms",
    "bathrooms":"bathrooms",
    "bath":     "bathrooms",
    "baths":    "bathrooms",
    "sqft":     "sqft",
    "sqfeet":   "sqft",
    "squarefoot": "sqft",
    "squarefootage": "sqft",
    "squarefeet": "sqft",
    "floorsize": "sqft",
    "rent":     "rent",
}

# Prefix rewrites: only "num/number" and "total" variants. We deliberately
# do NOT include "min" / "max" prefixes because min_rent / max_rent ARE
# distinct canonical keys (the rent range) and must survive normalization
# unchanged. Quantity-of-units prefixes don't apply to rent.
_FUZZY_PREFIX_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^num(?:ber)?(?:of)?_?([a-z]+)$"),   # numBedrooms, numberOfBedrooms, num_bedrooms
    re.compile(r"^total_?([a-z]+)$"),                 # totalBedrooms
)

_FUZZY_SUFFIX_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^([a-z]+)_?count$"),                 # bedroomCount, bedroom_count
    re.compile(r"^([a-z]+)_?number$"),                # bedroomsNumber
    re.compile(r"^([a-z]+)_?total$"),                 # bedroomsTotal
)

# Protected canonical keys: never let the fuzzy rewriter touch these. They're
# the OUTPUT names that downstream code (BEDS_KEYS, FieldCombination,
# has_unit_signals) reads. min_rent / max_rent are particularly important —
# the "min" / "max" prefixes there are SEMANTIC, not vendor decoration.
_PROTECTED_CANONICAL_KEYS: frozenset[str] = frozenset({
    "rent", "min_rent", "max_rent", "asking_rent", "market_rent",
    "sqft", "area",
    "bedrooms", "bathrooms", "beds", "baths",
    "floor_plan_name", "floor_plan_id",
    "unit_number", "unit_id", "unit_count",
    "available_date",
})


def _fuzzy_normalize(key: str) -> str | None:
    """Try a small fixed set of camelCase / vendor-prefix structural rewrites.

    Returns the canonical key when a rewrite matches and the stem is in
    ``_FUZZY_STEM_CANONICAL``. Returns None when no rewrite applies — the
    caller should fall back to the unchanged key.

    Conservative on purpose:
      * Each rewrite produces a stem that must be in the canonical-stem
        table to be accepted. ``foo`` → returns None.
      * Protected canonical keys (``min_rent``, ``max_rent``, etc.) are
        returned unchanged so semantic prefixes survive.

    Examples (after the caller lowercases + replaces hyphens with underscores):
        numbedrooms       → bedrooms   (prefix rewrite, stem "bedrooms")
        bedroomsnumber    → bedrooms   (suffix rewrite, stem "bedrooms")
        totalbathrooms    → bathrooms  (prefix rewrite, stem "bathrooms")
        min_rent          → None       (protected canonical — caller preserves)
        squarefootagemin  → None       (no rewrite + stem isn't in canon)
    """
    if key in _PROTECTED_CANONICAL_KEYS:
        return None
    for pat in _FUZZY_PREFIX_PATTERNS:
        m = pat.match(key)
        if m:
            stem = m.group(1)
            canon = _FUZZY_STEM_CANONICAL.get(stem)
            if canon:
                return canon
    for pat in _FUZZY_SUFFIX_PATTERNS:
        m = pat.match(key)
        if m:
            stem = m.group(1)
            canon = _FUZZY_STEM_CANONICAL.get(stem)
            if canon:
                return canon
    return None


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
    # Exact-match alias table first — fastest and most precise.
    canon = FIELD_ALIASES.get(normalised)
    if canon is not None:
        return canon
    # Fuzzy structural rewrite for camelCase / vendor-prefix near-misses.
    # Only kicks in for the exact-match misses, and only succeeds when the
    # rewritten stem is in the canonical-stem table. Conservative.
    fuzzy = _fuzzy_normalize(normalised)
    if fuzzy is not None:
        return fuzzy
    return normalised


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


# Listing-structure heuristics — distinguish "page lists individual units"
# from "page has marketing copy mentioning units".
#
# PID 119144 windsorburnet.com 2026-05-15 surfaced the false-positive case:
# count_floor_plan_signals returned 3 from `priceRange: "$1490 - $3824"` (a
# LocalBusiness JSON-LD aggregate field), "Choose from 1, 2, 3-bedroom"
# marketing copy, and "793 square feet fitness center" amenity description.
# All three are pattern matches; none are unit listings.
#
# A real unit listing has REPEATING containers (table rows, list items,
# repeater nodes, schema.org Offer arrays). Marketing aggregates have NONE.
#
# _RE_LISTING_REPEAT_SQL_HTML: 2+ <tr> rows with at least one rent-shaped
#   cell — covers entrata/rentcafe pricing tables.
# _RE_LISTING_REPEAT_CARDS: 2+ DOM nodes whose class/id contains a unit-card
#   marker (unit-card, fp-card, floorplan-card, unit-row, listing-card).
# _RE_LISTING_REPEAT_JSONLD: schema.org Offer/Apartment array with 2+ items.
# _RE_LISTING_REPEAT_PRODUCT: schema.org Product with additionalProperty
#   carrying bed/bath/sqft PropertyValue entries — covers Squarespace 250high.

_RE_LISTING_REPEAT_TABLE = re.compile(
    r"<tr[\s>][\s\S]{1,1500}?\$\s?\d{2,5}",
    re.IGNORECASE,
)
#: PMS-listing nouns — what producers call a per-apartment row.
#:
#: Expansion path: when a new vendor surfaces with a different noun (e.g.
#: ``"home_card"`` / ``"residence-row"`` / ``"model-item"``), append the
#: noun stem here rather than adding a fully-specified pattern. The regex
#: below builds the cross-product noun × separator × suffix.
_LISTING_NOUNS: tuple[str, ...] = (
    "unit",
    "floorplan",
    "floor",           # Wordpress/Elementor producers: floor_title / floor_details
    "fp",
    "listing",
    "availability",
    "apartment",
    "plan",
    "home",
    "residence",
    "model",
    "rental",
    "vacancy",
)

#: Suffixes that, paired with a listing noun, indicate a per-listing DOM
#: container or a per-listing detail node. Split into two groups for
#: readability — both contribute to the same regex.
_LISTING_CONTAINER_SUFFIXES: tuple[str, ...] = (
    # repeating containers
    "card", "row", "item", "list", "tile", "block",
    "wrapper", "container", "section", "panel",
)
_LISTING_DETAIL_SUFFIXES: tuple[str, ...] = (
    # per-listing detail nodes (the princetonmanagement.com case:
    # ``class="floor_title"``, ``class="floor_details"``,
    # ``class="floor_price_details"``). When 2+ of these repeat in the
    # markup the page IS listing real units even when no $NNN appears in
    # the static HTML (prices rendered client-side).
    "title", "name", "details", "info", "price", "pricing",
    "description", "features", "status", "header", "summary",
)


def _build_listing_card_regex() -> re.Pattern[str]:
    """Cross-product of listing nouns × separator × suffixes.

    Generic so that adding a new vendor noun in :data:`_LISTING_NOUNS`
    automatically extends the regex. Match shape::

        class="…floor_title…"
        id   ='unit-row-12'
        class="apartment_card primary"

    Anchored on ``class=`` / ``id=`` attribute openings so we don't
    accidentally match noun/suffix combinations that happen to appear
    inside text content.
    """
    nouns = "|".join(re.escape(n) for n in _LISTING_NOUNS)
    suffixes = "|".join(
        re.escape(s) for s in (_LISTING_CONTAINER_SUFFIXES + _LISTING_DETAIL_SUFFIXES)
    )
    pattern = (
        r"(?:class|id)\s*=\s*[\"'][^\"']*?"
        r"(?:" + nouns + r")"
        r"[-_]"
        r"(?:" + suffixes + r")"
        r"[^\"']*[\"']"
    )
    return re.compile(pattern, re.IGNORECASE)


_RE_LISTING_REPEAT_CARDS = _build_listing_card_regex()
_RE_LISTING_REPEAT_OFFER_ARRAY = re.compile(
    r'"@type"\s*:\s*"(?:Apartment|FloorPlan|Offer|Product)"',
    re.IGNORECASE,
)
_RE_LISTING_PROPERTY_VALUE_BEDS = re.compile(
    r'"name"\s*:\s*"(?:Bedrooms?|Beds?|Bathrooms?|Baths?|Square\s*F(?:ootage|eet|t))"',
    re.IGNORECASE,
)


def count_listing_structural_signals(html: str) -> int:
    """Count how many distinct LISTING-STRUCTURE signals are present.

    Unlike :func:`count_floor_plan_signals` which counts text-pattern matches
    (and is fooled by marketing aggregate copy), this scans the HTML for
    structural evidence of REPEATING per-unit containers.

    Returns 0-4:
    - 1 point: 2+ ``<tr>`` rows where a row carries a rent shape — classic
      Entrata/RentCafe/RealPage pricing tables.
    - 1 point: 2+ DOM nodes with a unit-card / floorplan-card / listing-card
      class or id marker.
    - 1 point: 2+ JSON-LD blocks declaring ``@type: Apartment|FloorPlan|
      Offer|Product`` — the listing is structured data.
    - 1 point: 2+ JSON-LD ``PropertyValue`` entries naming a dimension
      (Bedrooms/Bathrooms/Square Footage) — covers Squarespace's
      Product + additionalProperty shape.

    Args:
        html: Full HTML body. Pattern matches run directly against the markup,
              not the stripped-text. Marketing aggregate copy lacks these
              repeating-container patterns.

    Returns:
        Integer 0-4.
    """
    if not html:
        return 0
    score = 0
    if len(_RE_LISTING_REPEAT_TABLE.findall(html)) >= 2:
        score += 1
    if len(_RE_LISTING_REPEAT_CARDS.findall(html)) >= 2:
        score += 1
    if len(_RE_LISTING_REPEAT_OFFER_ARRAY.findall(html)) >= 2:
        score += 1
    if len(_RE_LISTING_PROPERTY_VALUE_BEDS.findall(html)) >= 2:
        score += 1
    return score


def has_listing_structure(html: str, threshold: int = 1) -> bool:
    """True when *html* has at least *threshold* listing-structure signals.

    Use ALONGSIDE ``has_floor_plan_signals`` to discriminate:

    - ``fp_signals >= 2 AND listing_structure >= 1``  → real unit listing
    - ``fp_signals >= 2 AND listing_structure == 0``  → marketing aggregate copy
                                                        (STUB_AGGREGATE_COPY)
    - ``fp_signals == 0``                              → genuine stub
    """
    return count_listing_structural_signals(html) >= threshold


# ---------------------------------------------------------------------------
# Cardinality metrics — uncapped volume signals for source ranking
# ---------------------------------------------------------------------------
#
# RC-CARD (2026-05-15 PM): ``count_floor_plan_signals`` returns 0-4 (one
# point per signal TYPE — bedroom/bathroom/area/floor-plan-type). That
# scoring is by-design for the boolean ``has_floor_plan_signals`` gate
# but it destroys volume information — a 25-unit listings page is
# indistinguishable from a 5-signal stub. Source ranking would benefit
# from knowing whether a page has 1 rent value vs 50.
#
# These additional metrics live alongside the type-score (preserved
# for backward compat with the `floor_plan_signal_count` field name
# in `extract.html_characterized` events) and add cardinality:
#
#   - unique_rents              — distinct $NNN dollar values
#   - unique_bed_bath_pairs     — distinct (beds, baths) tuples
#   - listing_rows_with_rent    — `<tr>` rows containing a $NNN cell
#   - jsonld_offer_count        — schema.org Offer/Apartment/FloorPlan
#   - property_value_bed_bath   — JSON-LD PropertyValue dimension entries
#   - matched_signal_bytes      — total bytes of matched signal substrings

from dataclasses import dataclass as _dc

_RE_DOLLAR = re.compile(r"\$\s?(\d{2,5}(?:,\d{3})?(?:\.\d+)?)")
_RE_BED_BATH_PAIR = re.compile(
    r"(\d)\s*(?:br|bed|bedroom)s?\b[^\n.;]{0,40}?(\d(?:\.5)?)\s*(?:ba|bath)s?\b",
    re.IGNORECASE,
)


@_dc(frozen=True)
class FloorPlanSignalCardinality:
    """Volume metrics for floor-plan / listing signals on a page.

    ``total_count`` preserves the existing 0-4 type-score for backward
    compatibility with ``floor_plan_signal_count``. The remaining fields
    are uncapped volume metrics used by the source ranker to discriminate
    a 50-unit listings page from a 1-rent marketing aggregate.
    """

    total_count: int = 0
    unique_rents: int = 0
    unique_bed_bath_pairs: int = 0
    listing_rows_with_rent: int = 0
    jsonld_offer_count: int = 0
    jsonld_apartment_count: int = 0
    property_value_bed_bath: int = 0
    matched_signal_bytes: int = 0


def count_floor_plan_signal_cardinality(
    text: str, html: str
) -> FloorPlanSignalCardinality:
    """Compute uncapped volume metrics.

    Args:
        text: HTML-stripped visible-text version (used for rent / bed-bath
              pattern matching to avoid script/style contamination).
        html: Raw HTML body (used for the structural / JSON-LD counts which
              need the tags + attributes intact).

    Returns:
        A frozen ``FloorPlanSignalCardinality`` snapshot.
    """
    if not text and not html:
        return FloorPlanSignalCardinality()

    total_count = count_floor_plan_signals(text or "")

    # Unique rent values — extract numeric, dedup.
    unique_rents: set[int] = set()
    matched_bytes = 0
    if text:
        for m in _RE_DOLLAR.finditer(text):
            raw = m.group(1)
            matched_bytes += len(m.group(0))
            try:
                num = int(float(raw.replace(",", "")))
                # Filter implausible dollar amounts (deposits like $50,
                # phone numbers, etc. — keep only $200-$50,000 range that
                # plausibly represents rent).
                if 200 <= num <= 50_000:
                    unique_rents.add(num)
            except (TypeError, ValueError):
                continue

    # Unique (beds, baths) pairs.
    bed_bath_pairs: set[tuple[str, str]] = set()
    if text:
        for m in _RE_BED_BATH_PAIR.finditer(text):
            bed_bath_pairs.add((m.group(1), m.group(2)))
            matched_bytes += len(m.group(0))

    # Listing rows (uncapped count of `<tr>` rows with a rent value).
    listing_rows_with_rent = (
        len(_RE_LISTING_REPEAT_TABLE.findall(html)) if html else 0
    )

    # JSON-LD type counts — break apart Apartment/FloorPlan from Offer/Product
    # so the cardinality signal can distinguish "5 floor-plan stubs" from
    # "5 priced offers".
    jsonld_apartment_count = 0
    jsonld_offer_count = 0
    if html:
        for m in _RE_LISTING_REPEAT_OFFER_ARRAY.finditer(html):
            matched_bytes += len(m.group(0))
            type_str = m.group(0).lower()
            if "apartment" in type_str or "floorplan" in type_str:
                jsonld_apartment_count += 1
            else:
                jsonld_offer_count += 1
    property_value_bed_bath = (
        len(_RE_LISTING_PROPERTY_VALUE_BEDS.findall(html)) if html else 0
    )

    return FloorPlanSignalCardinality(
        total_count=total_count,
        unique_rents=len(unique_rents),
        unique_bed_bath_pairs=len(bed_bath_pairs),
        listing_rows_with_rent=listing_rows_with_rent,
        jsonld_offer_count=jsonld_offer_count,
        jsonld_apartment_count=jsonld_apartment_count,
        property_value_bed_bath=property_value_bed_bath,
        matched_signal_bytes=matched_bytes,
    )


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
