"""Concession-text quality classifier + best-effort cleaner.

Two collaborating helpers consumed by the v2 schema emitter and the xlsx
exporter:

* :func:`classify_concession_quality` — returns a short label that
  reporting / downstream filters can pivot on.
* :func:`clean_concession_text` — returns a best-effort de-leaked
  variant. **Never empty when the input had any visible-text content.**

Design invariant — *preserve and flag, never discard*:

    The caller (``schema_v2._format_v2_unit`` and the property-level
    emitter) always retains the raw text in ``concession_text`` /
    ``concessions``. The cleaned variant is an additive sibling and the
    quality label tells downstream code whether the raw is safe to
    surface as-is.

Background — the 2026-05-19 canary's concession output had a ~50/50
clean/dirty split: ~50% of captured concessions contained JS-function
bodies, CSS rules, or Duda-CMS function definitions ahead of the real
offer text. Most dirty rows still carried the real offer signal
(89.5% have recognisable patterns like ``weeks free``, ``month free``,
``$X off``) but the JS/CSS prefix consumed the upstream 300-char cap,
often truncating the actual offer entirely. A subsequent finding showed
that ~46 rows for one canary property captured **just the banner header**
(``Limited Time Offer!``) because the sentence-split discarded the body.
This module classifies both leak patterns AND header-only orphans so
reporting can count them and the cleaner can still emit something usable.
"""

from __future__ import annotations

import re

# ─────────────────────────────────────────────────────────────────────
# Quality classification
# ─────────────────────────────────────────────────────────────────────

# Substrings that almost never appear in clean concession text but DO
# appear in JS code / CSS rules / Duda CMS function definitions.
_SCRIPT_LEAK_MARKERS: tuple[str, ...] = (
    "href.indexof",
    "el.setattribute",
    "setattribute(",
    "document.",
    "window.",
    "function(",
    "function (",
    "});",
    "} else {",
    "== -1)",
    "=== -1)",
    "onclick=",
    ".click()",
    "addeventlistener",
    "queryselector",
    "getelementbyid",
    "createelement",
    "innerhtml",
    "console.",
    "propleadsource",
)

_STYLE_LEAK_MARKERS: tuple[str, ...] = (
    "@media ",
    "!important",
    "padding:",
    "margin:",
    "z-index:",
    "display: none",
    "display:none",
    "position: ",
    "position:fixed",
    "border-radius",
    "background-color:",
    "flex-direction",
)

# Duda CMS — the signature is ``Functions["hash~N"] = function``.
_DMAPI_RE = re.compile(r"Functions\[[\"'][a-f0-9]+~\d+[\"']\]\s*=\s*function", re.I)

# Used to detect "starts with orphan punctuation" (truncated mid-statement).
_LEADING_ORPHAN_RE = re.compile(r"^\s*[)}\];,=+<>/]")

# Banner-header phrases that often appear ABOVE the actual offer body.
# ``Limited Time Offer!`` and ``Move-in Special!`` alone don't tell you
# the dollar amount, weeks/months free, or move-in deadline. Source-side
# window extension (pms/scraper.py) usually pulls the body in alongside,
# but for rows captured BEFORE the sentence-extend fix the body was
# dropped — we surface a distinct quality flag so reporting can count
# header-only orphans.
_BANNER_HEADER_RE = re.compile(
    r"^\s*(?:"
    r"limited[\s\-]+time[\s\-]+(?:offer|special|savings|deal)s?"
    r"|move[\s\-]?in\s+special"
    r"|special\s+offer"
    r"|don['’]?t\s+miss\s+out"
    r"|act\s+(?:now|fast)"
    r"|hurry"
    r"|exclusive\s+(?:offer|deal)"
    r"|new\s+resident\s+(?:offer|special)"
    r")[\s!.\-—–]*$",
    re.IGNORECASE,
)

# Specific-offer tokens — the things that, if present, mean the row
# carries actionable terms (dollar amount / duration / move-in date /
# percentage). The classifier uses ABSENCE of these alongside a banner-
# phrase match to identify header-only rows. The cleaner uses PRESENCE
# of these to locate the 120-char window in dirty rows.
_SPECIFIC_OFFER_RE = re.compile(
    r"\$\s*\d"  # dollar amount
    r"|\d+\s*%"  # percentage off
    r"|\d+\s+(?:weeks?|months?|days?)\s+(?:free|of\s+free|on\s+us|complimentary)"
    r"|free\s+rent"
    r"|free\s+\w+\s+for"
    r"|months?\s+free"
    r"|weeks?\s+free"
    r"|reduced\s+(?:rent|deposit|fees?)"
    r"|waived\s+(?:application|admin|deposit|fee)"
    r"|move[\s\-]?in\s+by\s+\d"
    r"|lease\s+by\s+\d"
    r"|move[\s\-]?in\s+(?:bonus|credit)"
    r"|save\s+\$?\d"
    r"|look[\s\-]+(?:and|&|n)[\s\-]+lease",
    re.IGNORECASE,
)


def classify_concession_quality(text: str | None) -> str:
    """Return a short quality label for *text*.

    Possible values:
      * ``"clean"``                 — no code-leak markers found.
      * ``"unclean_script_leak"``   — JS function bodies / DOM calls present.
      * ``"unclean_style_leak"``    — CSS rules / selectors present.
      * ``"unclean_dmapi"``         — Duda CMS function definition leaked.
      * ``"unclean_orphan_prefix"`` — starts with orphan ``}``, ``);``, etc.
      * ``"unclean_header_only"``   — banner header phrase only ("Limited
                                       Time Offer!") with no specific
                                       terms — dollar amount, weeks/
                                       months free, move-in deadline.
                                       Indicates the body was dropped
                                       by upstream sentence-split.
      * ``"empty"``                 — None / empty / whitespace-only input.
    """
    if not text or not isinstance(text, str) or not text.strip():
        return "empty"

    low = text.lower()

    # Order matters: pick the first-marker location as the primary
    # leak signal so a row with both script AND style markers is
    # classified by whichever appears first (the truncating prefix).
    script_idx = min((low.find(m) for m in _SCRIPT_LEAK_MARKERS if m in low), default=-1)
    style_idx = min((low.find(m) for m in _STYLE_LEAK_MARKERS if m in low), default=-1)

    if _DMAPI_RE.search(text):
        return "unclean_dmapi"
    if script_idx >= 0 and (style_idx < 0 or script_idx <= style_idx):
        return "unclean_script_leak"
    if style_idx >= 0:
        return "unclean_style_leak"
    if _LEADING_ORPHAN_RE.match(text):
        return "unclean_orphan_prefix"
    # Header-only check runs LAST so it doesn't shadow the leak
    # categories. A row that is both banner AND has JS leak still
    # classifies as unclean_script_leak — more actionable for upstream
    # debugging.
    if _BANNER_HEADER_RE.match(text.strip()) and not _SPECIFIC_OFFER_RE.search(text):
        return "unclean_header_only"
    return "clean"


# ─────────────────────────────────────────────────────────────────────
# Best-effort cleaner
# ─────────────────────────────────────────────────────────────────────

# Offer phrases that anchor the 120-char window around the real offer
# text when the row is leak-prefixed.
_OFFER_RE = re.compile(
    r"(?:"
    r"\d+\s*(?:weeks?|months?|days?)\s+(?:free|of\s+free|on\s+us|complimentary)"
    r"|\$\s*\d{2,5}(?:,\d{3})*\s*(?:off|gift\s*card|credit|cash|savings|welcome\s+bonus)?"
    r"|\d+\s*%\s*off"
    r"|free\s+rent"
    r"|months?\s+free"
    r"|weeks?\s+free"
    r"|reduced\s+(?:rent|deposit|fees?)"
    r"|waived\s+(?:application|admin|deposit)\s*fees?"
    r"|move[\s\-]?in\s+by\s+\w+\s*\d"
    r"|lease\s+by\s+\w+\s*\d"
    r"|limited[\s\-]+time\s+(?:offer|special|savings|deal)"
    r"|move[\s\-]?in\s+special"
    r"|look[\s\-]+(?:and|&|n)[\s\-]+lease"
    r")",
    re.IGNORECASE,
)

# Boundary tokens that mark the END of a JS/CSS prefix and the START of
# the real text. Used as a fallback when no offer phrase is recognised.
_BOUNDARY_RE = re.compile(r"(?:[)}\]]\s*[;,]?\s*|>)\s*(?=[A-Z\$\d])")


def clean_concession_text(text: str | None) -> str:
    """Return a best-effort de-leaked variant of *text*.

    Two strategies, first match wins:

    1. **Offer-phrase window** — extract a 120-char window around the
       first recognised offer phrase (``weeks free``, ``$X off``,
       ``months free``, ``limited time offer``, etc.). Covers ~89.5% of
       dirty rows where the real offer is buried after the JS/CSS prefix.

    2. **Boundary split** — when no offer phrase is recognised, split
       at the last ``})`` / ``};`` / ``>`` followed by capital letter or
       digit and return everything after it.

    Special cases:

    * ``empty`` → ``""``
    * ``clean`` → input with whitespace normalised
    * ``unclean_header_only`` → whitespace-normalised banner (nothing
      to mine — the text IS the banner; the quality flag tells
      reporting to display with caution).

    The function never returns an empty string when the input had any
    visible-text content — falls back to the whitespace-normalised
    original if no strategy yields a usable result.
    """
    if not text or not isinstance(text, str) or not text.strip():
        return ""

    quality = classify_concession_quality(text)
    if quality in ("clean", "empty"):
        return _whitespace_normalize(text)
    if quality == "unclean_header_only":
        # Nothing to extract — the text IS the banner header. Return
        # whitespace-normalised so it's safe in xlsx cells.
        return _whitespace_normalize(text)

    # Strategy 1 — offer-phrase window. Most reliable.
    m = _OFFER_RE.search(text)
    if m:
        start = max(0, m.start() - 30)
        end = min(len(text), m.end() + 90)
        window = text[start:end]
        return _strip_orphan_punctuation(_whitespace_normalize(window))

    # Strategy 2 — boundary split. Find the last JS/CSS terminator
    # followed by a capital letter or digit (the start of real prose
    # or a price).
    last_match = None
    for m2 in _BOUNDARY_RE.finditer(text):
        last_match = m2
    if last_match is not None:
        tail = text[last_match.end():]
        tail = _strip_orphan_punctuation(_whitespace_normalize(tail))
        if tail:
            return tail

    # Final fallback — whitespace-normalised original. Never empty
    # so the xlsx cell isn't blank when raw had content.
    return _strip_orphan_punctuation(_whitespace_normalize(text))


def _whitespace_normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strip_orphan_punctuation(text: str) -> str:
    """Strip leading orphan punctuation (``);``, ``},``, ``>``, etc).

    Applied after window extraction since a window can start mid-token.
    Bounded to keep removal predictable — only the leading run of
    syntactic-noise characters is dropped.
    """
    return re.sub(r"^\s*[)}\];,=+<>/]+\s*", "", text).strip()
