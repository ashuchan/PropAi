"""
Shared parsing utilities for Tier 3 PMS templates.

Provides regex-based text extractors ported from the production entrata scraper
(scripts/entrata.py). These serve as fallbacks when CSS-selector-based extraction
fails, dramatically reducing the number of properties that fall through to Tier 4.

All helpers are pure functions operating on strings — no Playwright dependency.
"""
from __future__ import annotations

import re
from datetime import date

from bs4 import Tag

from models.unit_record import AvailabilityStatus

# ── Compiled regexes ──────────────────────────────────────────────────────────

RENT_RE = re.compile(r"\$\s*([\d,]+)(?:\.\d+)?")
SQFT_RE = re.compile(r"([\d,]+)\s*(?:sq\.?\s*ft|sqft|sf)\b", re.IGNORECASE)
BED_RE = re.compile(r"(\d+(?:\.\d)?)\s*(?:bed|bd|bedroom)s?", re.IGNORECASE)
BATH_RE = re.compile(r"(\d+(?:\.\d)?)\s*(?:bath|ba)s?", re.IGNORECASE)
STUDIO_RE = re.compile(r"\bstudio\b", re.IGNORECASE)
UNIT_NUM_RE = re.compile(
    r"(?:unit|apt\.?|suite|ste\.?|#)\s*#?\s*([A-Za-z]?\d{1,5}[A-Za-z]?)",
    re.IGNORECASE,
)
FLOOR_RE = re.compile(r"(?:floor|level)\s*:?\s*(\d{1,3})", re.IGNORECASE)
AVAIL_DATE_RE = re.compile(
    r"(?:available|avail\.?|move.in)\s*(?:date|now|:)?\s*"
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s*\d{0,4})",
    re.IGNORECASE,
)
AVAIL_DATE_NUMERIC_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")
CONCESSION_RE = re.compile(
    r"(\d+)\s+(?:week|month)s?\s+free", re.IGNORECASE
)


# ── Scalar parsers ────────────────────────────────────────────────────────────


def parse_rent(text: str | None) -> float | None:
    """Extract the first dollar amount from text. '$1,450' -> 1450.0"""
    if not text:
        return None
    m = RENT_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_int_from_text(text: str | None) -> int | None:
    """Extract the first integer-like number from text. '1,050 sqft' -> 1050"""
    if not text:
        return None
    m = re.search(r"[\d,]+", text)
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_sqft(text: str | None) -> int | None:
    """Extract sqft from text using the sqft-specific regex."""
    if not text:
        return None
    m = SQFT_RE.search(text)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return parse_int_from_text(text)


def parse_availability(text: str | None) -> AvailabilityStatus:
    """Determine availability status from free-form text."""
    if not text:
        return AvailabilityStatus.UNKNOWN
    t = text.lower()
    if "unavailable" in t or "leased" in t or "waitlist" in t or "not available" in t:
        return AvailabilityStatus.UNAVAILABLE
    if "available" in t or "now" in t or re.search(r"\d{1,2}/\d{1,2}", t):
        return AvailabilityStatus.AVAILABLE
    return AvailabilityStatus.UNKNOWN


def parse_availability_date(text: str | None) -> date | None:
    """
    Try to extract an availability date from text.
    Handles: 'Available 06/15/2026', 'Available Now', 'Jan 15, 2026'.
    Returns None if no date found or if 'Now' / immediate.
    """
    if not text:
        return None
    t = text.strip()
    if re.search(r"\bnow\b", t, re.IGNORECASE):
        return None

    # Try MM/DD/YYYY or MM/DD/YY
    m = AVAIL_DATE_NUMERIC_RE.search(t)
    if m:
        parts = m.group(1).split("/")
        try:
            month, day = int(parts[0]), int(parts[1])
            year = int(parts[2])
            if year < 100:
                year += 2000
            return date(year, month, day)
        except (ValueError, IndexError):
            return None

    # Try month-name formats: "Jun 15, 2026", "June 15 2026"
    m = re.search(
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s*(\d{4})?",
        t,
        re.IGNORECASE,
    )
    if m:
        month_names = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        month = month_names.get(m.group(1).lower()[:3], 0)
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else date.today().year
        if month:
            try:
                return date(year, month, day)
            except ValueError:
                return None
    return None


def parse_floor_plan_type(text: str | None) -> str | None:
    """
    Infer floor plan type label from text containing bed/bath info.
    '2 Bed / 2 Bath' -> '2/2', 'Studio' -> 'Studio'.
    """
    if not text:
        return None
    if STUDIO_RE.search(text):
        return "Studio"
    bed_m = BED_RE.search(text)
    bath_m = BATH_RE.search(text)
    if bed_m and bath_m:
        return f"{bed_m.group(1)}/{bath_m.group(1)}"
    if bed_m:
        return f"{bed_m.group(1)} Bed"
    # Check for compact "1/1", "2/2" patterns
    compact = re.search(r"\b(\d)/(\d)\b", text)
    if compact:
        return f"{compact.group(1)}/{compact.group(2)}"
    return text.strip() if text.strip() else None


def parse_floor(text: str | None) -> int | None:
    """Extract floor number from text. 'Floor 3' -> 3."""
    if not text:
        return None
    m = FLOOR_RE.search(text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def parse_concession(text: str | None) -> str | None:
    """Extract concession text like '2 months free'."""
    if not text:
        return None
    m = CONCESSION_RE.search(text)
    return m.group(0) if m else None


# ── DOM helpers ───────────────────────────────────────────────────────────────


def text_of(node: Tag | None, selector: str) -> str | None:
    """Get text content of the first element matching selector within node."""
    if node is None:
        return None
    el = node.select_one(selector)
    return el.get_text(" ", strip=True) if el else None


def attr_of(node: Tag | None, selector: str, attr: str) -> str | None:
    """Get an attribute value from the first matching element."""
    if node is None:
        return None
    el = node.select_one(selector)
    if el is None:
        return None
    val = el.get(attr)
    if isinstance(val, list):
        return " ".join(val)
    return str(val) if val else None


def full_text(node: Tag | None) -> str:
    """Get all text from a node for regex-based extraction."""
    if node is None:
        return ""
    return node.get_text(" ", strip=True)


# ── Regex fallback extractor ──────────────────────────────────────────────────


def regex_extract_from_text(text: str) -> dict[str, object]:
    """
    Extract unit fields from raw text using regex patterns.
    Used as a fallback when CSS selectors fail to find specific elements.
    Returns a dict with keys matching UnitRecord field names.
    """
    result: dict[str, object] = {}

    # Unit number
    m = UNIT_NUM_RE.search(text)
    if m:
        result["unit_number"] = m.group(1)

    # Rent
    rent = parse_rent(text)
    if rent:
        result["asking_rent"] = rent

    # Sqft
    m = SQFT_RE.search(text)
    if m:
        try:
            result["sqft"] = int(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Floor plan type from bed/bath
    bed_m = BED_RE.search(text)
    bath_m = BATH_RE.search(text)
    if STUDIO_RE.search(text):
        result["floor_plan_type"] = "Studio"
    elif bed_m and bath_m:
        result["floor_plan_type"] = f"{bed_m.group(1)}/{bath_m.group(1)}"
    elif bed_m:
        result["floor_plan_type"] = f"{bed_m.group(1)} Bed"

    # Floor
    floor = parse_floor(text)
    if floor is not None:
        result["floor"] = floor

    # Availability status
    result["availability_status"] = parse_availability(text)

    # Availability date
    avail_date = parse_availability_date(text)
    if avail_date is not None:
        result["availability_date"] = avail_date

    # Concession
    concession = parse_concession(text)
    if concession:
        result["concession_text"] = concession

    return result
