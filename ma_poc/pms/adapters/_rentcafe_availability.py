"""Per-unit availability helpers shared by RentCafe DOM adapters.

RentCafe's public unit cards often publish the same source date twice: as
visible text (``Date Available: 9/5/2026``) and as ``MoveInDate`` on the
unit's Apply link.  Keep extraction scoped to the nearest unit row/card so a
date can never be borrowed from a neighbouring apartment.
"""

from __future__ import annotations

import html as _html
import re
from collections.abc import Callable
from urllib.parse import unquote_plus

from bs4 import BeautifulSoup, Tag

_NUMERIC_DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b"
)
_MONTH_DATE_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2}(?:,?\s+\d{4})?\b",
    re.IGNORECASE,
)
_AVAILABLE_NOW_RE = re.compile(r"\bavailable\s+now\b", re.IGNORECASE)
_LABELED_DATE_RE = re.compile(
    r"\b(?:date(?:\s+available)?|available)\s*:\s*"
    r"(?P<value>[^|\n\r]{1,40})",
    re.IGNORECASE,
)
_MOVE_IN_DATE_RE = re.compile(
    r"(?:[?&]|&amp;)MoveInDate=([^&#'\"<>\s]+)", re.IGNORECASE
)

_UNIT_SCOPE_CLASSES = frozenset(
    {
        "available-unit",
        "available-unit-card",
        "card",
        "card-body",
        "unit-card",
        "unit-container",
        "unit-row",
    }
)


def _date_token(text: str) -> str:
    """Return one source date token, without interpreting it."""
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return ""
    for pattern in (_NUMERIC_DATE_RE, _MONTH_DATE_RE):
        match = pattern.search(cleaned)
        if match:
            return match.group(0).strip()
    if _AVAILABLE_NOW_RE.search(cleaned):
        return "Available Now"
    return ""


def _unit_scope(apply_element: Tag) -> Tag | None:
    """Find the smallest bounded row/card containing one apartment."""
    row = apply_element.find_parent("tr")
    if isinstance(row, Tag):
        return row

    for depth, parent in enumerate(apply_element.parents):
        if not isinstance(parent, Tag):
            continue
        classes = {str(value).lower() for value in (parent.get("class") or [])}
        if classes & _UNIT_SCOPE_CLASSES:
            return parent
        # Do not walk into a page-wide wrapper: five levels covers the live
        # RentCafe card shapes while preserving per-unit isolation.
        if depth >= 4 or parent.name in {"main", "body"}:
            break
    return apply_element.parent if isinstance(apply_element.parent, Tag) else None


def _visible_date(scope: Tag) -> str:
    """Prefer the human-visible date inside a single unit scope."""
    candidates: list[Tag] = []
    for element in scope.find_all(True):
        classes = " ".join(str(v).lower() for v in (element.get("class") or []))
        data_label = str(element.get("data-label") or "").lower()
        selenium_id = str(element.get("data-selenium-id") or "").lower()
        if (
            "available" in classes
            or "availdate" in selenium_id
            or "date available" in data_label
        ):
            candidates.append(element)

    # Structured date cells/subtitles are strongest.  Numeric/month tokens
    # outrank a generic "Available" label; explicit "Available Now" remains
    # meaningful and is normalized to capture date by the canonical formatter.
    for element in candidates:
        token = _date_token(element.get_text(" ", strip=True))
        if token:
            return token

    scope_text = scope.get_text(" ", strip=True)
    for match in _LABELED_DATE_RE.finditer(scope_text):
        token = _date_token(match.group("value"))
        if token:
            return token
    return _date_token(scope_text) if _AVAILABLE_NOW_RE.search(scope_text) else ""


def _move_in_date(scope: Tag, apply_element: Tag) -> str:
    """Read the exact public ``MoveInDate`` from this unit's Apply action."""
    values: list[str] = []
    for element in (apply_element, *scope.find_all(True)):
        for attr in ("href", "onclick", "data-href", "data-url"):
            value = element.get(attr)
            if value:
                values.append(str(value))
    for value in values:
        match = _MOVE_IN_DATE_RE.search(_html.unescape(value))
        if match:
            return unquote_plus(match.group(1)).strip()
    return ""


def availability_by_applyga_unit(
    raw_html: str,
    *,
    unit_from_element: Callable[[str, Tag], str],
) -> dict[str, str]:
    """Map each ``applyGAClick`` unit to its own raw availability token.

    ``unit_from_element`` receives ``(onclick, element)`` and returns the
    adapter-specific unit number (LT stores it in arg6; Nestin also exposes
    it as the element id).
    """
    if not raw_html or "applyGAClick" not in raw_html:
        return {}
    try:
        soup = BeautifulSoup(raw_html, "html.parser")
    except Exception:
        return {}

    out: dict[str, str] = {}
    for element in soup.find_all(attrs={"onclick": True}):
        onclick = str(element.get("onclick") or "")
        if "applygaclick" not in onclick.lower():
            continue
        unit = str(unit_from_element(onclick, element) or "").strip()
        if not unit or unit.upper() in out:
            continue
        scope = _unit_scope(element)
        if scope is None:
            continue
        token = _visible_date(scope) or _move_in_date(scope, element)
        if token:
            out[unit.upper()] = token
    return out
