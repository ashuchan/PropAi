"""Operator-published "no availability" detector + placeholder synthesis.

Background — the 2026-05-22 grind flagged ~10 krcapartments.com property
pages (and similar properties on several smaller operators) as
FAILED_NO_DATA because no unit-level rows survived extraction. The
operator IS publishing data — they're publishing that ZERO units are
available right now. That's a successful extraction of an honest
business state, not a failure to extract.

The wild patterns we observed (case-insensitive):
  • "Sorry, there are no available units at this time."   (krcapartments)
  • "No units currently available"                          (assorted WP)
  • "Currently no vacancies"                                (custom CMS)
  • "Fully leased"                                          (a few small ops)
  • "Waitlist only" / "Wait list only"                      (lease-up sites)
  • "No apartments are currently available"                 (Squarespace tpls)

This module:
  1. ``detect_no_availability(html)`` — substring match against a
     curated phrase list. Cheap; safe to run on every page.
  2. ``build_no_availability_placeholder(...)`` — emit a single
     synthetic unit dict that carries
     ``data_quality_flag="NO_AVAILABILITY_NOW"`` and
     ``data_gaps=["rent","sqft","unit_number","availability_date"]``
     so downstream layers know this is a documented zero-availability
     state, not a missing-data failure.

The verdict layer recognizes ``NO_AVAILABILITY_NOW`` separately from
SUCCESS and SUCCESS_PLAN_LEVEL (see ``ma_poc.reporting.verdict``).
Counted toward the run-rate denominator (we did successfully scrape
the page) but excluded from the strict success bar (no rent+sqft).
"""

from __future__ import annotations

import re
from typing import Any

from ma_poc.pms.adapters._parsing import make_unit_dict

# ── Detection ───────────────────────────────────────────────────────────────

# Curated phrase list. Order matters only for performance — most common
# variant first. All matched case-insensitive and with arbitrary
# inter-word whitespace (the HTML often has tabs / newlines mid-phrase).
_NO_AVAILABILITY_PHRASES: tuple[str, ...] = (
    r"sorry,?\s+there\s+are\s+no\s+available\s+units?",
    r"no\s+available\s+units?\s+at\s+this\s+time",
    r"no\s+units?\s+currently\s+available",
    r"no\s+apartments?\s+are\s+currently\s+available",
    r"currently\s+no\s+vacancies",
    r"no\s+vacancies\s+at\s+this\s+time",
    r"fully\s+leased\s+at\s+this\s+time",
    r"100%\s+leased",
    r"wait\s*list\s+only",
    r"join\s+(?:our|the)\s+wait\s*list",
    # 2026-05-27 612-failure-grind cohort additions (supersedes the
    # RentCafe chip's three-phrase set — these regexes cover the same
    # livebrez/larsonapts/201walnut waitlist cohort PLUS the broader
    # AppFolio empty-embed + fully-leased + no-results cohorts found
    # in the detector_hits_v2 reprobe).
    r"no\s+available\s+properties",
    r"currently\s+no\s+available\s+(?:units?|apartments?|listings?)",
    r"no\s+listings\s+matching",
    r"add\s+to\s+(?:our\s+|the\s+)?wait\s*list",
    r"all\s+units\s+leased",
    r"fully\s+leased",
    # 2026-05-27 follow-up: detector_hits_v2 cohort found 3 RentCafe
    # empty-search pages publishing "no results found" / "no results match"
    # when the property has zero units to list. Same operator-transparency
    # signal — just a different phrase.
    r"no\s+results?\s+(?:found|match)",
)

# Pre-compile a single combined regex. ``re.IGNORECASE`` covers the
# Title-Case variants and ``re.DOTALL`` lets ``\s+`` match newlines.
_COMBINED_RE = re.compile(
    "|".join(f"(?:{p})" for p in _NO_AVAILABILITY_PHRASES),
    re.IGNORECASE | re.DOTALL,
)

# Anti-pattern guard. Some pages carry the phrase in a different,
# misleading context — e.g. "If there are no available units, join
# our waitlist" in marketing prose. To stay precise, we ONLY trigger
# when the matched phrase isn't preceded by a hypothetical keyword
# in the immediate ~50 chars.
_HYPOTHETICAL_PREFIX_RE = re.compile(
    r"\b(?:if|when|in\s+case|should|whether|unless|until)\b[^.!?<>]{0,40}$",
    re.IGNORECASE,
)


def detect_no_availability(html: str) -> bool:
    """True when the page carries a clear "operator says zero availability"
    statement that is NOT inside a hypothetical clause.

    Returns False on empty input. Tolerant of HTML tags between words —
    we strip tags before matching so ``<p>Sorry,</p> <span>there are no
    available units</span>`` still trips.
    """
    if not html:
        return False
    # Strip HTML tags so phrase matching sees the human-readable text
    # without the operator's per-word span/p wrappers.
    text = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace (incl. &nbsp; entities the WP templates use).
    text = text.replace(" ", " ").replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text)
    for m in _COMBINED_RE.finditer(text):
        prefix = text[max(0, m.start() - 60) : m.start()]
        if _HYPOTHETICAL_PREFIX_RE.search(prefix):
            continue
        return True
    return False


def matched_phrase(html: str) -> str | None:
    """Return the first matched phrase (lower-cased, whitespace-collapsed),
    or None. Useful for telemetry / debugging."""
    if not html:
        return None
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace(" ", " ").replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text)
    for m in _COMBINED_RE.finditer(text):
        prefix = text[max(0, m.start() - 60) : m.start()]
        if _HYPOTHETICAL_PREFIX_RE.search(prefix):
            continue
        return m.group(0).lower().strip()
    return None


# ── Empty-inventory-page predicate (2026-05-27) ─────────────────────────────

# Signals that the URL we resolved to is a canonical inventory page —
# i.e. the operator's "here are the apartments" page, not a brochure
# splash. Found in the page head (first ~10KB) to avoid footer false
# positives.
_INVENTORY_PAGE_KEYWORDS = re.compile(
    # Word-bounded — "comm**unit**y" / "opport**unit**y" / "Sun**set**"
    # are false positives without \b around units/apartments etc.
    r"\bfloor\s*plans?\b|\bapartments?\b|\bavailab(?:le|ility)\b"
    r"|\bunits?\b|\brentals?\b|\bresidences?\b",
    re.IGNORECASE,
)

# Quantitative signals — if ANY of these fire >= the threshold the
# page is NOT empty (there's data we should've extracted, so the
# adapter has a bug; do NOT misclassify as no-inventory).
_RENT_SIGNAL_RE = re.compile(r"\$\s*\d{1,2}[, ]?\d{3}")
_BED_SIGNAL_RE = re.compile(r"\b\d\s*(?:BR|Bed|Bedroom)s?\b", re.IGNORECASE)
_SQFT_SIGNAL_RE = re.compile(
    r"\b\d{2,4}\s*(?:sq\.?\s*ft\.?|sqft|sf|square\s*feet)\b", re.IGNORECASE
)


def is_empty_inventory_page(
    html: str,
    *,
    min_html_bytes: int = 2500,
    max_bed_signals: int = 1,
    max_sqft_signals: int = 1,
) -> bool:
    """True when the page LOOKS like an inventory listing page but
    contains zero unit data — i.e. the operator is transparently
    publishing an empty schema.

    Discovered in the 2026-05-27 612-failure-grind reprobe: 75 of 80
    "operator-gap" candidates fell into this bucket — the floor-plans
    page exists, returns 200, mentions floor plans in the head, but
    has no rent / bed / sqft signals. Daily-re-scrape model means it's
    safe to call these SUCCESS_NO_AVAILABILITY: tomorrow's scrape will
    detect new inventory the moment the operator publishes it.

    Args:
      html: rendered HTML of the page the adapter ultimately landed on.
      min_html_bytes: page must be at least this large (filters 404
        fallbacks that masquerade as 200 with thin content).
      max_bed_signals: allow up to N "1 Bed" / "2 BR" hits (some pages
        carry "We offer 1-3 bedroom homes" marketing copy without any
        rent — that's still empty inventory).
      max_sqft_signals: same idea for sqft.

    Caller MUST already know:
      • Zero units were extracted across all tiers (the gate that
        triggers this check).
      • The resolved URL looks like an inventory page (e.g. matched
        /floor-plans or /availability via the adapter's path ladder).
        This function double-checks the page content as a safety net.

    Returns False on empty input or when ANY rent signal is present —
    a single "$1,250" anywhere on the page disqualifies the call.
    """
    if not html or len(html) < min_html_bytes:
        return False
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace(" ", " ").replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text)
    # Page must mention inventory keywords near the top — guards
    # against generic homepage misclassification.
    if not _INVENTORY_PAGE_KEYWORDS.search(text[:10_000]):
        return False
    # Any rent signal disqualifies — we either should have extracted
    # it (adapter bug) or the page is plan-with-rent (not empty).
    if _RENT_SIGNAL_RE.search(text):
        return False
    if len(_BED_SIGNAL_RE.findall(text)) > max_bed_signals:
        return False
    if len(_SQFT_SIGNAL_RE.findall(text)) > max_sqft_signals:
        return False
    return True


# ── Placeholder synthesis ───────────────────────────────────────────────────


def build_no_availability_placeholder(
    source_url: str = "",
    property_name: str = "",
    matched_text: str | None = None,
) -> dict[str, Any]:
    """Synthesize a single placeholder unit dict for the
    ``NO_AVAILABILITY_NOW`` state.

    The dict has no rent / sqft / unit_number — those are listed in
    ``data_gaps`` and flagged with ``data_quality_flag="NO_AVAILABILITY_NOW"``
    so downstream consumers can:
      • Count the property toward the scrape-rate denominator (we DID
        successfully extract the operator's stated state).
      • Exclude the property from the strict ≥1-unit-with-rent+sqft bar.
      • Distinguish from FAILED_NO_DATA (extraction error) in dashboards.

    The placeholder uses ``availability_status="UNAVAILABLE"`` to match
    the canonical no-availability marker for downstream pricing /
    occupancy logic.
    """
    return make_unit_dict(
        floor_plan_name=property_name or "No availability",
        unit_number="",
        availability_status="UNAVAILABLE",
        source_api_url=source_url,
        extraction_tier="TIER_1_DOM_NO_AVAILABILITY",
        source_ids={
            "operator_published_state": "no_availability_now",
            "matched_phrase": matched_text or "",
        },
        data_gaps=[
            "rent",
            "sqft",
            "unit_number",
            "availability_date",
        ],
        data_quality_flag="NO_AVAILABILITY_NOW",
    )
