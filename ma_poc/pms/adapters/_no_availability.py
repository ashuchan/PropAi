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
    # 2026-05-27 RentCafe waitlist cohort (612-failure-grind row 2). Three
    # additional phrases observed on livebrez / larsonapts / 201walnut and
    # ~31 sibling RentCafe properties where the operator publishes a true
    # zero-inventory state via a CTA or empty-search banner.
    r"add\s+to\s+wait\s*list",
    r"no\s+listings?\s+matching",
    r"currently\s+no\s+available",
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
