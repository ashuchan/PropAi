"""Rendered-DOM capture that pierces open shadow roots (web components).

``page.content()`` serializes only the light DOM, so web-component unit markup
— ``<entrata-pp-unit-cards>``, RentCafe/Funnel widgets — never reaches the DOM
parsers (BeautifulSoup has no shadow concept). This captures ``page.content()``
PLUS a recursive walk of every OPEN shadow root, appended so the parsers see the
web-component unit rows as ordinary nested markup. Closed shadow roots
(``attachShadow({mode:"closed"})``) are unreachable from JS and remain the
API-interception tier's job.
"""
from __future__ import annotations

from typing import Any

# Emits ONLY the open-shadow-root subtrees (each wrapped in a marker element),
# newline-joined — empty string when the page has no shadow DOM. Uses
# ``innerHTML`` so text nodes (unit numbers, rents) are preserved.
# SELF-TERMINATING (2026-07-19 render-hang fix). The original walk had NO bounds
# — on a huge DOM or a shadow-root reference cycle it could run away, and because
# a hung/slow ``page.evaluate`` is NOT interruptible by the caller's
# ``asyncio.wait_for`` (JS execution is not bound by page.set_default_timeout),
# that manifested as a full 600s per-property timeout (test100: 27/33 timeouts
# were fetches that started and never completed). These hard caps make the JS
# ALWAYS return within ~1.5s regardless of the DOM: a wall-clock budget, a total
# node cap, a recursion-depth cap, an output-size cap, and a visited-Set cycle
# guard. The outer wait_for stays as a belt-and-suspenders backstop.
_SHADOW_SERIALIZE_JS = r"""
() => {
  try {
    const T0 = Date.now();
    const TIME_MS = 1500, MAX_NODES = 30000, MAX_DEPTH = 15, MAX_OUT = 3000000;
    const parts = [];
    const seen = new Set();
    let nodes = 0, outLen = 0;
    const budgetHit = () =>
      (Date.now() - T0) > TIME_MS || nodes > MAX_NODES || outLen > MAX_OUT;
    const walk = (root, depth) => {
      if (depth > MAX_DEPTH || budgetHit()) return;
      const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
      for (const el of all) {
        nodes++;
        if (budgetHit()) return;
        const sr = el.shadowRoot;
        if (sr && !seen.has(sr)) {
          seen.add(sr);  // cycle guard — never revisit a shadow root
          const tag = (el.tagName || 'shadow').toLowerCase();
          const html = sr.innerHTML || '';
          parts.push('<shadow-content data-shadow-host="' + tag + '">');
          parts.push(html);
          parts.push('</shadow-content>');
          outLen += html.length + 60;
          walk(sr, depth + 1);
        }
      }
    };
    walk(document, 0);
    return parts.join('\n');
  } catch (e) { return ''; }
}
"""


async def capture_rendered_dom(page: Any, fallback: str | None = None) -> str | None:
    """Return the page's rendered HTML with open shadow-root content appended.

    Prefers ``await page.content()`` (battle-tested light-DOM serialization) and
    additively appends a shadow-root walk only when the page actually has shadow
    DOM, so light-DOM-only pages are never regressed. Returns ``fallback`` when
    ``page`` is None / a non-Playwright stub / errors — so callers keep whatever
    HTML they already had (fetch-body or stale landing HTML).

    :param page: a live Playwright ``Page`` (or None / stub).
    :param fallback: HTML to return when the page can't be captured.
    :returns: rendered HTML string, or ``fallback``.
    """
    if page is None or not hasattr(page, "content"):
        return fallback
    try:
        content = await page.content()
    except Exception:
        return fallback
    if not isinstance(content, str) or not content:
        return fallback
    try:
        shadow = await page.evaluate(_SHADOW_SERIALIZE_JS)
    except Exception:
        shadow = None
    if isinstance(shadow, str) and shadow.strip():
        return content + "\n<!--shadow-dom-->\n" + shadow
    return content
