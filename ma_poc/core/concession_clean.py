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
    # 2026-05-20 sanity-sweep additions: surfaced from the 4,407 dirty
    # canary rows where the offer-phrase extension consumed JS that
    # followed the offer. Most common shape:
    #   "...Move in Special! ... Open Banner const now = new Date();..."
    # These markers serve double duty — they classify a row as
    # script-leak AND act as truncation boundaries for the cleaner's
    # offer-phrase extension (stop before this token).
    "stickyheader",
    "const now ",
    "new date(",
    " = new date",
    "initmap(",
    "initsightmap",
    "open banner",
    # Template-literal start: backtick-paren pattern from the "shopify-
    # style" leak (``'off' : 'on'}`); stickyHeader(); });``).
    "`);",
)

# Markers for JSON-blob leaks. Distinct from script leaks because the
# upstream is a CMS payload (Spherexx/Duda/McKinley) rather than JS
# code. Real shape: ``,"promotionTitle":"Save up to $260/mo on..."``.
# Single quote-key signature is enough to identify — too specific to
# appear in legitimate marketing copy.
_JSON_BLOB_MARKERS: tuple[str, ...] = (
    '","',                           # adjacent string fields
    '":"',                           # key/value separator
    '":{',                           # nested object
    '":[',                           # nested array
    '":false',
    '":true',
    '":null',
    'promotiontitle',
    'promotiondescription',
    'mobileinfobarsettings',
    'announcementbarsettings',
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

# 2026-07-12 concession-quality sweep: cookie-consent UI chrome leaking
# into the captured banner text. The rendered-DOM banner walker queries
# ``[class*="banner"]``/``[class*="notice"]``-style selectors that also
# match OneTrust/CookieYes consent bars, and when the consent bar and the
# marketing banner render adjacently the captured window concatenates
# both. Live shapes (2026-07-11 canary + fresh repro):
#   "Privacy Policy Accept Deny Non-Essential Close Cookie Preferences X
#    2 Months Free Base Rent on 2-Bedroom Homes…"   (liveatleesquare)
#   "ACCEPT DECLINE Up to 10 weeks free on select…" (banyanonwashington)
# Pre-fix these classified "clean" (no code-leak markers) so
# clean_concession_text returned them UNCHANGED at step 1 — a false-clean.
# Markers are multi-word consent-specific vocabulary; single common words
# ("close", "accept") are deliberately NOT markers on their own — they
# participate only in the leading-run stripper below once a strong marker
# anchors the run.
_COOKIE_CHROME_MARKERS: tuple[str, ...] = (
    "cookie preferences",
    "cookie settings",
    "cookies policy",
    "cookie policy",
    "we use cookies",
    "use of cookies",
    "accept all cookies",
    "manage cookies",
    "consent preferences",
    "accept deny",
    "accept decline",
    "non-essential",
    "nonessential cookies",
    "reject all",
    "privacy policy accept",
)

# Leading consent-chrome run stripper. Engages only at the START of the
# text and only when the run OPENS with a strong consent token; it then
# keeps consuming the weak UI words that follow (Accept / Deny / Close /
# X / Preferences …) until real copy begins. Strong-anchor requirement
# keeps legitimate marketing text safe — "Accept our gift of 1 month
# free" never matches because bare "accept" is not a strong opener.
_COOKIE_CHROME_STRONG_RE = re.compile(
    r"^\s*(?:"
    r"privacy\s+policy"
    r"|cookies?\s+(?:preferences|settings|policy|notice|consent)"
    r"|we\s+use\s+cookies"
    r"|this\s+(?:web)?site\s+uses\s+cookies"
    r"|manage\s+cookies"
    r"|consent(?:\s+preferences)?"
    r"|accept\s+(?:all|deny|decline)"
    r"|reject\s+all"
    r"|non[-\s]essential"
    r")\b",
    re.IGNORECASE,
)
_COOKIE_CHROME_TOKEN_RE = re.compile(
    r"^\s*(?:"
    r"privacy\s+policy|cookies?(?:\s+(?:preferences|settings|policy|notice|consent))?"
    r"|we\s+use\s+cookies[^.!?]{0,80}[.!?]?"
    r"|this\s+(?:web)?site\s+uses\s+cookies[^.!?]{0,80}[.!?]?"
    r"|manage\s+cookies|consent(?:\s+preferences)?"
    r"|accept(?:\s+all)?|deny|decline|reject(?:\s+all)?"
    r"|non[-\s]essential|essential\s+only|opt[-\s]?out"
    r"|preferences|settings|close|got\s+it|i\s+agree|agree|ok(?:ay)?"
    r"|[x✕×]"
    r")\b[\s:|,·.!—–-]*",
    re.IGNORECASE,
)


def _strip_leading_cookie_chrome(text: str) -> str:
    """Remove a leading cookie-consent UI run from *text*.

    Anchored: only strips when the text OPENS with a strong consent token
    (see ``_COOKIE_CHROME_STRONG_RE``); then iteratively consumes the weak
    consent/UI tokens that follow. Returns the remainder (may be the
    original text when no strong anchor at position 0).
    """
    if not _COOKIE_CHROME_STRONG_RE.match(text):
        return text
    out = text
    for _ in range(40):  # bounded — each iteration must consume ≥1 char
        m = _COOKIE_CHROME_TOKEN_RE.match(out)
        if not m or m.end() == 0:
            break
        out = out[m.end():]
    return out.strip()


# Duda CMS — the signature is ``Functions["hash~N"] = function``.
_DMAPI_RE = re.compile(r"Functions\[[\"'][a-f0-9]+~\d+[\"']\]\s*=\s*function", re.I)

# Used to detect "starts with orphan punctuation" (truncated mid-statement).
_LEADING_ORPHAN_RE = re.compile(r"^\s*[)}\];,=+<>/]")
# Strips the full leading run of orphan punctuation (handles ``}); ``,
# ``) `` and similar HTML/JS-flatten artifacts, with intervening whitespace).
_LEADING_ORPHAN_RUN_RE = re.compile(r"^(?:\s*[)}\];,=+<>/]+\s*)+")

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
    r"|half\s+off"  # worded-fraction discount (greenarchtulsa 2026-07-12)
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


# 2026-07-12: the broadest offer-ish vocabulary — ANY of these words
# means the text at least talks about an offer (the specific-offer regex
# above then grades whether it carries actionable terms). Absence of ALL
# of them = amenity/nav noise captured as concession text
# ("no_offer_signal" label). Deliberately broad: this gate only has to
# separate offers from amenity lists, not grade offer quality.
_ANY_OFFER_SIGNAL_RE = re.compile(
    r"\bspecials?\b|\boffer(?:s|ing)?\b|\bfree\b|\bdiscount\w*\b"
    r"|\bwaived?\b|\bcredit\b|\bsav(?:e|ings)\b|\bdeal\b|\bpromo\w*\b"
    r"|\bconcession\w*\b|\bbonus\b|\breduced?\b|\boff\b"
    # flip-audit additions (2026-07-11 corpus): "6 Weeks ON US",
    # "NO RENT until Aug", "Flash SALE" are real offers with none of the
    # words above; look-lease must tolerate the HTML-entity ampersand.
    r"|\bon\s+us\b|\bno\s+rent\b|\bsale\b|\bincentives?\b"
    r"|look[\s\-]*(?:and|&(?:amp;)?|\+|n)?[\s\-]*lease"
    r"|move[\s\-]?in|\$\s*\d|\d+\s*%",
    re.IGNORECASE,
)


def classify_concession_quality(text: str | None) -> str:
    """Return a short quality label for *text*.

    Possible values:
      * ``"clean"``                 — no code-leak markers found.
      * ``"unclean_script_leak"``   — JS function bodies / DOM calls present.
      * ``"unclean_style_leak"``    — CSS rules / selectors present.
      * ``"unclean_dmapi"``         — Duda CMS function definition leaked.
      * ``"unclean_json_blob"``     — CMS JSON payload leaked
                                       (``"promotionTitle":...``, Spherexx /
                                       McKinley pattern).
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
    json_hit = any(m in sl for m in _JSON_BLOB_MARKERS)
    # JSON-blob is more specific than orphan-prefix (a leading ``,"``
    # would otherwise satisfy both); runs before the orphan check.
    # But after script/style — a row with both JS function bodies AND
    # JSON payload signals JS misclassification, prefer script_leak.
    if json_hit and not script_hit and not style_hit:
        return "unclean_json_blob"
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
    # 2026-07-12: cookie-consent chrome. Runs after the code-leak
    # categories (a row with JS AND consent text is a script leak first)
    # and before orphan-prefix. See _COOKIE_CHROME_MARKERS.
    if any(m in sl for m in _COOKIE_CHROME_MARKERS):
        return "unclean_cookie_chrome"
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
    # 2026-07-12 (no-concession decomposition): amenity/nav noise stored
    # as concession text — "Stackable Machines provided Stainless
    # Appliances…", nav menus, community-feature lists (~1,000+ canary
    # units). Per the module contract (preserve-and-flag, never discard)
    # the text ships unchanged; this label lets consumers filter rows
    # that carry NO offer-ish vocabulary at all. Runs last: any leak
    # class or header phrase above wins first.
    if not _ANY_OFFER_SIGNAL_RE.search(text):
        return "no_offer_signal"
    return "clean"


# ─────────────────────────────────────────────────────────────────────
# Best-effort cleaner
# ─────────────────────────────────────────────────────────────────────

# Real offer-text patterns. These are the signals we want to surface
# from a dirty string. List grown from the 49,677-row feature-canary
# 2026-05-20 sample.
_OFFER_PHRASES: tuple[str, ...] = (
    r"limited[\s\-]+time[\s\-]+(?:offer|special)",
    # 2026-05-20 sanity-sweep: the ``rent`` infix variants surfaced in
    # canary residuals — ``1 month rent free``, ``2 weeks rent free``,
    # ``1 Month of Rent Free``. Original regex required ``weeks free`` /
    # ``months free`` adjacency and missed these. Optional non-capturing
    # group preserves the simple form.
    r"\d{1,3}\s+weeks?\s+(?:(?:of\s+)?rent\s+)?free",
    r"\d{1,3}\s+months?\s+(?:(?:of\s+)?rent\s+)?free",
    r"\d{1,3}\s+days?\s+(?:(?:of\s+)?rent\s+)?free",
    r"free\s+rent\b",
    r"month\s+(?:of\s+)?(?:rent\s+)?free\b",
    r"week\s+(?:of\s+)?(?:rent\s+)?free\b",
    r"up\s+to\s+\d{1,3}\s+(?:weeks?|months?)\s+(?:(?:of\s+)?rent\s+)?free",
    r"\$\d{1,4}\s+off",
    r"half\s+off(?:\s+(?:your\s+)?first\s+month(?:['\u2019]?s)?\s+rent)?",
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
# 2026-05-20 sanity-sweep refinement: the trailing 120-char window
# excludes ``<>{}`` AND backtick — the canary surfaced ``…Move in
# Special! … Open Banner const now = new Date()…`` shapes where a
# template literal trailed the offer, and the 120-char extension
# pulled it in. Backtick stops the capture before the template
# literal opens.
_OFFER_RE = re.compile(
    "(?:" + "|".join(_OFFER_PHRASES) + r")[^<>{}`]{0,120}",
    re.IGNORECASE,
)

# 2026-05-20 sanity-sweep: after offer-phrase extraction, trim the
# snippet at the first script-leak token. Real residual after Fix A:
# offer phrase matches, 120-char extension stays under the
# ``[^<>{}\\`]`` constraints, then ``const now = new Date()`` or
# ``initSightMap`` appears at the boundary. We truncate AT those
# tokens (case-insensitive) so the cleaner doesn't carry the JS
# downstream.
_CLEANER_BOUNDARY_TOKENS_RE = re.compile(
    r"(?i)\b(?:"
    r"const\s+\w+\s*=|"
    r"var\s+\w+\s*=|"
    r"let\s+\w+\s*=|"
    r"function\s*\(|"
    r"new\s+date\s*\(|"
    r"stickyheader|"
    r"initmap|"
    r"initsightmap|"
    r"addeventlistener|"
    r"document\.|"
    r"window\.|"
    r"open\s+banner"
    r")"
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
    if quality == "unclean_cookie_chrome":
        # 2026-07-12: strip the leading consent-UI run, then re-clean the
        # remainder (it may still carry other leak classes, or be clean).
        # A run that consumes the ENTIRE text means the capture was pure
        # consent UI with no offer at all — the honest clean value is ""
        # (the quality flag preserves the why). When the chrome is NOT a
        # leading run (marker mid-text), fall through to the offer-phrase
        # extraction below, which truncates at the first chrome marker.
        stripped = _strip_leading_cookie_chrome(text)
        if not stripped:
            return ""
        if stripped != text.strip():
            return clean_concession_text(stripped)
        # Offer-BEFORE-chrome shape ("…$99.00 + FREE RENT! X How we use
        # cookies…"): no leading run to strip, but the copy ahead of the
        # first consent marker carries the offer. Cut there and re-clean
        # the head (which by construction contains no cookie markers, so
        # the recursion terminates). Trailing solitary close-button
        # tokens ("… X") are trimmed off the result.
        _tl = text.lower()
        _cut = min(
            (_tl.find(m) for m in _COOKIE_CHROME_MARKERS if m in _tl),
            default=-1,
        )
        if _cut > 0:
            _head = text[:_cut]
            if _OFFER_RE.search(_head):
                _head_clean = clean_concession_text(_head)
                if _head_clean:
                    return re.sub(
                        r"(?:\s+(?:[x✕×]|how))+\s*$", "", _head_clean,
                        flags=re.IGNORECASE,
                    ).rstrip(" ,;:-—–")
    if quality == "unclean_orphan_prefix":
        # 2026-07-16: strip the leading orphan-punctuation run (a ``) `` /
        # ``}); `` artifact from HTML/JS flattening) and re-clean the
        # remainder — mirrors the cookie_chrome path. The offer copy AFTER
        # the orphan is intact, so re-cleaning the stripped text returns the
        # whole offer (when it re-classifies as clean) instead of the lossy
        # offer-window crop in Pass 1 below, which dropped a leading header
        # offer (e.g. "Free AC Unit") whenever ``_OFFER_RE`` first matched a
        # LATER phrase ("look and lease special"). Recursion terminates: the
        # stripped text no longer opens with orphan punctuation, so its
        # quality is never ``unclean_orphan_prefix`` again.
        stripped = _LEADING_ORPHAN_RUN_RE.sub("", text).strip()
        if stripped and stripped != text.strip():
            return clean_concession_text(stripped)
    if quality in ("unclean_header_only", "no_offer_signal"):
        # Nothing to extract — either the text IS the banner header
        # (no body), or it carries no offer vocabulary at all
        # (amenity/nav noise; preserve-and-flag contract). Return
        # whitespace-normalized so it's safe in xlsx cells; the
        # quality flag tells reporting to display/filter accordingly.
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
        # Strip leading JSON-key fragments that precede the offer —
        # ``"promotionTitle":"Save up to $158…"`` → ``Save up to $158…``.
        # Anchor: a quoted-key followed by ``":"``. Re-running the
        # offer-phrase regex tells us where the offer starts inside
        # the snippet; everything before it that looks like JSON keys
        # is junk.
        offer_in_snippet = _OFFER_RE.search(snippet)
        if offer_in_snippet:
            head = snippet[:offer_in_snippet.start()]
            # Match a key-value head like ``…"<key>":"`` and strip it
            # (but only if it covers most of the head — avoids
            # stripping a legitimate prefix that happens to end with
            # ``":"``).
            if re.search(r'"\s*:\s*"\s*$', head):
                snippet = snippet[offer_in_snippet.start():]
                offer_in_snippet = _OFFER_RE.search(snippet)
        # 2026-05-20 sanity-sweep: truncate at the first script/template
        # token in the snippet. Canary residual showed offer phrases
        # trailing ``Open Banner const now = new Date()`` and similar
        # — the 120-char window stops at backtick now, but a
        # no-backtick template (``const x = ...``) still leaks through
        # unless we explicitly trim. The leading-head strip above has
        # already removed any JSON-key prefix that precedes the offer,
        # so the boundary tokens found here are guaranteed to come
        # AFTER the offer text (the offer phrases themselves don't
        # contain ``const``/``function``/``new Date``/etc.).
        boundary_match = _CLEANER_BOUNDARY_TOKENS_RE.search(snippet)
        if boundary_match:
            snippet = snippet[:boundary_match.start()]
        # Strip a TRAILING CMS JSON-key fragment like ``…select units
        # ","promotionDescription":…``. After the leading-head strip,
        # the FIRST ``","`` in the remainder is the trailing JSON
        # boundary — truncate there. (The offer-phrase regex's
        # 120-char extension consumes past the trail, so we can't
        # use offer_match.end() as a fence — must scan from snippet
        # start.)
        tail_match = re.search(r'"\s*,\s*"', snippet)
        if tail_match:
            snippet = snippet[:tail_match.start()]
        # 2026-07-12: truncate at the first cookie-consent marker — the
        # sentence-terminator extension above can pull a trailing consent
        # bar into the window ("…Restrictions apply. Cookie Preferences").
        _snip_l = snippet.lower()
        _cookie_cut = min(
            (_snip_l.find(m) for m in _COOKIE_CHROME_MARKERS if m in _snip_l),
            default=-1,
        )
        if _cookie_cut > 0:
            snippet = snippet[:_cookie_cut]
        return snippet.rstrip(' ,;:.-—–"').strip()

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
