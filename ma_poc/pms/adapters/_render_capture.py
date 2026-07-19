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
_SHADOW_SERIALIZE_JS = r"""
() => {
  try {
    const parts = [];
    const walk = (root) => {
      const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
      for (const el of all) {
        if (el.shadowRoot) {
          const tag = (el.tagName || 'shadow').toLowerCase();
          parts.push('<shadow-content data-shadow-host="' + tag + '">');
          parts.push(el.shadowRoot.innerHTML || '');
          walk(el.shadowRoot);
          parts.push('</shadow-content>');
        }
      }
    };
    walk(document);
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
