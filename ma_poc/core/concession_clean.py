"""Concession-text quality classifier + best-effort cleaner.

2026-05-20 (post-canary inspection): the feature canary's concession
output had a ~50/50 clean/dirty split — 24,775 of 49,677 non-blank
rows contained JS-function bodies, CSS rules, or Duda-CMS function
definitions ahead of the real offer text. Most dirty rows still
carried the real offer signal (89.5% have recognizable patterns like
"weeks free", "month free", "$X off") but the JS/CSS prefix consumed
the upstream 300-char cap, often truncating the actual offer entirely.

Root cause (fixed at source in ``ma_poc/pms/scraper.py``): the
window-capture regex flattens HTML by stripping ``<tag>`` markers but
keeps the body of ``<script>...</script>`` and ``<style>...</style>``
blocks. Adjacent JS/CSS leaked into the ±200-char window around the
concession-pattern match.

This module is the **preserve-and-flag safety net** matching the user
constraint *"error on side of unclean rather than discard"*:

  * ``classify_concession_quality(text)`` returns a short label
    consumed by reporting/downstream filters.
  * ``clean_concession_text(text)`` returns a best-effort de-leaked
    version — never empty when the input had any visible-text content.
  * The original text is **always preserved** by the caller in the
    canonical ``concession_text`` field; the clean variant is an
    additive sibling.
"""

from __future__ import annotations

import re

# ─────────────────────────────────────────────────────────────────────
# Quality classification
# ─────────────────────────────────────────────────────────────────────

# Substrings that almost-never appear in clean concession text but DO
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

# 2026-05-20 (header-only): phrases that signal a concession exists but
# carry no actionable terms on their own — banners typically render
# above the actual offer body. "Limited Time Offer!" + "Special!" alone
# don't tell you the dollar amount, weeks/months free, or move-in
# deadline. Source-side window-extension (pms/scraper.py) usually pulls
# the body in alongside, but for rows captured BEFORE the
# 2026-05-20 scraper fix the body was sentence-dropped — we surface a
# distinct quality flag so reporting can count header-only orphans.
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
# phrase match to identify header-only rows.
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
                                       by upstream sentence-split (pre-
                                       2026-05-20 scraper).
      * ``"empty"``                 — None / empty / whitespace-only input.
    """
    if not text or not isinstance(text, str) or not text.strip():
        return "empty"
    sl = text.lower()
    if _DMAPI_RE.search(text):
        return "unclean_dmapi"
    script_hit = any(m in sl for m in _SCRIPT_LEAK_MARKERS)
    style_hit = any(m in sl for m in _STYLE_LEAK_MARKERS)
    if script_hit and style_hit:
        # Disambiguate by which appears first.
        first_script = min(
            (sl.find(m) for m in _SCRIPT_LEAK_MARKERS if m in sl), default=10**9
        )
        first_style = min(
            (sl.find(m) for m in _STYLE_LEAK_MARKERS if m in sl), default=10**9
        )
        return "unclean_script_leak" if first_script <= first_style else "unclean_style_leak"
    if script_hit:
        return "unclean_script_leak"
    if style_hit:
        return "unclean_style_leak"
    if _LEADING_ORPHAN_RE.match(text):
        return "unclean_orphan_prefix"
    # Header-only check runs LAST so it doesn't shadow the leak
    # categories. "Limited Time Offer!" alone, with no $X / weeks /
    # months / move-in deadline anywhere in the string, is a banner
    # without an offer body. We flag it so reporting can count the
    # 2026-05-19-canary residual (~46 rows for Woodland Creek; many
    # more across the 49,677 non-blank sample where the body
    # sentence-split orphaned it).
    if _BANNER_HEADER_RE.match(text.strip()) and not _SPECIFIC_OFFER_RE.search(text):
        return "unclean_header_only"
    return "clean"


# ─────────────────────────────────────────────────────────────────────
# Best-effort cleaner
# ─────────────────────────────────────────────────────────────────────

# Real offer-text patterns. These are the signals we want to surface
# from a dirty string. List grown from the 49,677-row feature-canary
# 2026-05-20 sample.
_OFFER_PHRASES: tuple[str, ...] = (
    r"limited[\s\-]+time[\s\-]+(?:offer|special)",
    r"\d{1,3}\s+weeks?\s+free",
    r"\d{1,3}\s+months?\s+free",
    r"\d{1,3}\s+days?\s+free",
    r"free\s+rent\b",
    r"month\s+free\b",
    r"week\s+free\b",
    r"\$\d{1,4}\s+off",
    r"\d{1,3}%\s+off",
    r"save\s+up\s+to\s+\$?\d{1,4}",
    r"save\s+\$\d{1,4}",
    r"move[\s\-]?in\s+(?:special|bonus)",
    r"application\s+fee\s+waived",
    r"deposit\s+waived",
    r"reduced\s+(?:rent|deposit)",
    r"look[\s\-]+(?:&|and)[\s\-]+lease",
    r"lease\s+by\s+\d",
    r"move[\s\-]?in\s+by\s+\d",
    r"receive\s+(?:up\s+to\s+)?\$\d{1,4}",
    r"\d{1,3}\s+months?\s+(?:of\s+)?free\s+\w+",
)
_OFFER_RE = re.compile(
    "(?:" + "|".join(_OFFER_PHRASES) + r")[^<>{}]{0,120}",
    re.IGNORECASE,
)

# Generic "end-of-code → start-of-visible-text" boundary.
# Matches the LAST occurrence of a closing JS/CSS char (``}``, ``);``)
# followed by whitespace and then visible text.
_END_OF_CODE_BOUNDARY_RE = re.compile(
    r"^.*?(?:[)};]\s*[)};]?\s+|>\s+)(?=[A-Z\$\d])",
    re.DOTALL,
)


def clean_concession_text(text: str | None) -> str:
    """Return a best-effort cleaned version of *text*.

    Strategy (first match wins):
      1. If the text is already classified ``"clean"`` or ``"empty"``,
         return it unchanged.
      2. If one or more offer phrases (``"6 weeks free"``,
         ``"$200 off"``, etc.) are present, return the first match's
         enclosing 120-char window — keeps adjacent context like the
         dollar amount, duration, or deadline.
      3. Else split on the last ``})`` / ``};`` / ``>`` followed by
         capital-letter visible text, and return the tail.
      4. Else return the original (caller still has the raw via
         ``concession_text``; this signals "we tried, no clean text
         found" — quality flag tells reporting to display with caution).

    Never returns ``None``. Never raises. Empty input returns ``""``.
    """
    if not text or not isinstance(text, str):
        return ""
    quality = classify_concession_quality(text)
    if quality in ("clean", "empty"):
        return text.strip()
    if quality == "unclean_header_only":
        # Nothing to extract — the text IS the banner header, there
        # is no body to mine. Return whitespace-normalized so it's
        # safe in xlsx cells; quality flag tells reporting to display
        # with caution.
        return re.sub(r"\s+", " ", text).strip()

    # Pass 1 — offer-phrase extraction. Most reliable.
    m = _OFFER_RE.search(text)
    if m:
        # Expand a little before the match to include modifiers like
        # "Up to 6 weeks free" / "Receive $200 off" where the offer
        # phrase starts mid-clause.
        start = max(0, m.start() - 30)
        # Walk back to the previous whitespace boundary so we don't cut
        # mid-word.
        if start > 0:
            ws = text.rfind(" ", 0, m.start())
            if ws != -1 and ws >= start:
                start = ws + 1
        end = m.end()
        # Extend forward to the next sentence terminator if close.
        tail = text[end:end + 80]
        term = re.search(r"[.!?]", tail)
        if term:
            end = end + term.end()
        snippet = text[start:end].strip()
        # Collapse whitespace.
        snippet = re.sub(r"\s+", " ", snippet)
        # Strip ALL leading orphan-punctuation groups (handles multiple
        # ``}); });`` sequences with intervening whitespace).
        snippet = re.sub(r"^(?:[)}\];,=+<>/]+\s*)+", "", snippet)
        return snippet

    # Pass 2 — code/text boundary split.
    boundary = _END_OF_CODE_BOUNDARY_RE.match(text)
    if boundary:
        tail = text[boundary.end() - 1:]  # -1 to include the first visible char
        tail = re.sub(r"\s+", " ", tail).strip()
        if tail:
            return tail

    # Pass 3 — nothing recoverable; return original with whitespace
    # normalized so consumers don't crash on multi-line values.
    return re.sub(r"\s+", " ", text).strip()


__all__ = ["classify_concession_quality", "clean_concession_text"]
