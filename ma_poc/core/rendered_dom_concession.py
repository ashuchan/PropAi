"""Step 3c — rendered-DOM concession rescan.

Steps 3 (HTML banner regex) and 3b (API-response JSON) operate on static
HTML / pre-captured API responses. The 100-prop vision audit at
``investigations/2026-05-24-cascade-fixes-grind/CONCESSION_100PROP_VISION_VERIFICATION.md``
found ~9% of properties carry concession banners that are physically
absent from static HTML — React/Vue/Angular-hydrated popups,
``[role="dialog"]`` modals that fire on a JS timeout, banner divs that
mount only after the bundle's ``fetch()`` resolves.

When a Playwright session is already open AND Steps 3/3b returned
nothing, this module queries the popup/modal/banner element set on the
rendered page and re-runs the same ``_PROPERTY_CONCESSION_RE`` against
each visible block's innerText. Reuses the open browser context — no
extra navigation cost on routes that already render.

Confirmed false-negatives this closes (from the 100-prop audit):

  * Cortland Brier Creek      "up to 2 months free…"             (modal)
  * Blossoms at Brentwood     "Now offering up to six weeks free" (hydrated banner)
  * Austin Midtown            "LEASE TODAY & EARN UP TO 4 WEEKS* FREE!" (modal)
  * Colina Ranch Hill         "Up to 2 Months Free + Look & Lease 50% off" (banner)
  * Prose Riviana             "8-Weeks Free Base Rent + $1,500 Gift Card" (banner)
  * Quarry Alamo Heights      "6 Weeks Free Base Rent + Waived App/Admin Fees" (modal)
  * Museum Terrace            "$1,500 Off Base Rent — Look & Lease" (modal)
  * Jefferson Place           "up to $1000 Off" (banner)
  * 42 West Apartments        "$300 One-Time Rent Concession" (banner)
"""

from __future__ import annotations

import re
from typing import Any

# Query a wide net of popup/modal/banner/announcement/special/promo/offer
# classes and IDs, plus role="dialog". The corresponding ``[role="alert"]``
# is intentionally excluded — it's mostly cookie/GDPR consent banners which
# the static pipeline already filters out via ``extract_api_concession``'s
# junk filters. We re-apply concession-regex matching, so even if cookie
# banners slip through they will not match the offer-keyword set.
#
# We return up to 25 distinct blocks, each capped at 800 chars; the caller
# regex-scans each. A separate ``body_text`` field carries the full
# ``document.body.innerText`` (capped 60 KB) as a recall fallback for
# sites whose banner doesn't carry any of the popup-ish class names — e.g.
# Cortland uses ``apartments__notice`` for the "2 months free" copy and
# Blossoms uses a plain ``<section>`` with no offer-related class.
RENDERED_DOM_PROBE_JS = r"""
() => {
  try {
    const selectors = [
      '[role="dialog"]',
      '[class*="popup"]',
      '[class*="Popup"]',
      '[class*="modal"]',
      '[class*="Modal"]',
      '[class*="banner"]',
      '[class*="Banner"]',
      '[class*="announcement"]',
      '[class*="Announcement"]',
      '[class*="special"]',
      '[class*="Special"]',
      '[class*="promo"]',
      '[class*="Promo"]',
      '[class*="offer"]',
      '[class*="Offer"]',
      '[class*="notice"]',
      '[class*="Notice"]',
      '[id*="popup"]',
      '[id*="modal"]',
      '[id*="banner"]',
      '[id*="announcement"]',
      '[id*="special"]',
      '[id*="promo"]',
      '[id*="offer"]'
    ];
    const seen = new Set();
    const blocks = [];
    document.querySelectorAll(selectors.join(',')).forEach((el) => {
      let t = (el.innerText || '').trim();
      if (t.length < 8 || t.length > 800) return;
      t = t.replace(/\s+/g, ' ');
      if (seen.has(t)) return;
      seen.add(t);
      // visibility — skip elements with 0 area (e.g. display:none).
      const r = el.getBoundingClientRect();
      if (r.width < 30 || r.height < 15) return;
      blocks.push(t);
      if (blocks.length >= 25) return;
    });
    const body = (document.body && document.body.innerText) || '';
    return {
      blocks: blocks.slice(0, 25),
      body_text: body.slice(0, 60000)
    };
  } catch (e) {
    return { error: String(e) };
  }
}
"""


# Junk filters re-applied client-side. Cookie/GDPR banners frequently
# contain the word "offer" or "promo" in unrelated marketing-tracking
# copy ("…to enhance site navigation and assist in our marketing
# efforts"). The concession regex itself excludes these; this filter
# is a belt-and-suspenders precaution to avoid even attempting the
# regex on obvious cookie-consent blocks.
_COOKIE_JUNK_RE = re.compile(
    r"\b(?:cookies?|gdpr|consent|privacy\s+policy|accept\s+all|reject\s+all|"
    r"manage\s+preferences|tracking|data\s+collection)\b",
    re.IGNORECASE,
)


def _looks_like_cookie_banner(text: str) -> bool:
    """True when a block reads like cookie/GDPR consent UI rather than
    a concession banner. Anchored on multiple consent-y phrases —
    a single 'cookies' mention isn't enough since real banner copy
    occasionally references cookies in fine-print disclaimers."""
    if not text:
        return False
    hits = len(_COOKIE_JUNK_RE.findall(text))
    return hits >= 2


def extract_concession_window(text: str, match: re.Match[str]) -> str:
    """Build a 300-char sentence-window around a concession-regex match.

    Mirrors the in-line logic at scraper.py Step 3 (lines 550–589) so
    Step 3c yields ``concessions_text`` in the same shape downstream
    cleanup code expects. Extracted here so the rendered-DOM path and
    the static-HTML path can't drift.
    """
    start, end = match.span()
    win = text[max(0, start - 200): end + 200]
    off = start - max(0, start - 200)
    # Sentence split — keep the matched sentence plus up to 2 forward
    # sentences while the running total stays under 300 chars. Same
    # forward-walk strategy as scraper.py.
    parts = re.split(r"(?<=[.!?|•·])\s+", win)
    idx, acc = -1, 0
    for i, p in enumerate(parts):
        if acc <= off < acc + len(p) + 1:
            idx = i
            break
        acc += len(p) + 1
    if idx >= 0:
        seg = parts[idx]
        for nxt in parts[idx + 1: idx + 3]:
            candidate = (seg + " " + nxt).strip()
            if len(candidate) > 300:
                break
            seg = candidate
    else:
        seg = win
    return seg.strip()[:300]


def find_concession_in_blocks(
    blocks: list[str],
    body_text: str,
    concession_re: re.Pattern[str],
) -> str | None:
    """Return the first 300-char concession window found in the
    rendered-DOM probe output, or ``None`` if no block matches.

    Strategy:
    1. Scan popup/banner ``blocks`` first (highest precision — these
       elements are scoped to promo content).
    2. Fall back to ``body_text`` (full ``document.body.innerText``) for
       cases where the operator's banner uses a non-standard class
       (Cortland's ``apartments__notice`` etc.). Lower precision but
       protected by the concession regex itself rejecting unrelated text.

    Cookie/GDPR consent blocks are skipped in step 1 even if they
    accidentally contain a regex-matching phrase.
    """
    for block in blocks or []:
        if not isinstance(block, str) or not block.strip():
            continue
        if _looks_like_cookie_banner(block):
            continue
        m = concession_re.search(block)
        if m:
            return extract_concession_window(block, m)

    if body_text and isinstance(body_text, str):
        m = concession_re.search(body_text)
        if m:
            return extract_concession_window(body_text, m)
    return None


async def scan_rendered_dom_for_concession(
    page: Any,
    concession_re: re.Pattern[str],
) -> str | None:
    """Run ``RENDERED_DOM_PROBE_JS`` on the already-open Playwright page
    and return the first 300-char concession window found, or ``None``.

    The caller is responsible for gating: only invoke when a Playwright
    page is open AND Steps 3/3b returned no concession. This function
    does no work on its own.

    Errors (page closed, evaluation failure, malformed response) are
    swallowed and return ``None`` — Step 3c is a best-effort enrichment,
    never a failure mode for the overall scrape.
    """
    if page is None:
        return None
    try:
        data = await page.evaluate(RENDERED_DOM_PROBE_JS)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("error"):
        return None
    blocks = data.get("blocks") or []
    body_text = data.get("body_text") or ""
    if not isinstance(blocks, list):
        blocks = []
    if not isinstance(body_text, str):
        body_text = ""
    return find_concession_in_blocks(blocks, body_text, concession_re)
