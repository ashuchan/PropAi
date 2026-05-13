"""Interactive-reveal pre-extraction step (Pattern B fix).

Some multifamily marketing sites render floor-plan cards in a collapsed
state and only reveal unit rows / rent prices when the user clicks a
per-card button ("View Availability", "Show Units", "See Pricing", …).
The post-click DOM is server-rendered or hydrated inline — no XHR fires
that we can intercept — so our normal pipeline sees the placeholder
cards but no rent text.

This module runs BEFORE the adapter dispatch in ``scraper.scrape()``:

  1. Looks at the rendered HTML.
  2. Skips if the page already has ≥ ``_MIN_RENT_SIGNALS`` dollar amounts
     (the existing extractors will succeed without our help).
  3. Skips if no Pattern-B-shaped trigger text is present on the page
     (so we don't waste clicks on every property).
  4. Otherwise: tags each candidate trigger element with a temporary
     ``data-jugnu-reveal-id`` attribute, clicks them one-by-one, waits
     for the DOM to settle, and returns telemetry.

The downstream adapter then reads ``page.content()`` — which reflects
the post-click DOM — and extracts the now-visible rents through its
normal Tier-1/2/3/4 chain. No adapter changes required.

Research log
------------
Pattern observed in the May-13 failed-properties cohort QC analysis
(``investigations/2026-05-13/manual_qc_analysis.md``, F1 bucket — 12
properties, ~3% of the manually QC'd 400). Typical signature:

  - One marketing page renders multiple floor-plan cards (no portal hop).
  - Each card has a "View Availability" / "Show Units" button or accordion
    chevron whose click handler injects HTML (or removes a ``hidden``
    class) revealing unit rows with rent amounts.
  - No XHR fires on click (the data was inline in a ``<script>`` blob
    keyed by plan-id, or in a hidden ``<template>`` element).

Gating heuristics tuned to avoid false-positives on pages that already
extract cleanly (≥ 3 rent signals is usually enough for downstream
adapters to succeed).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


# Regex anchored at word boundaries so we don't match button text like
# "see neighborhood gallery" or "view amenities".
_REVEAL_TEXT_RE = re.compile(
    r"\b("
    r"view\s+(?:availability|units|pricing|details|plans|apartments|floor[\s-]?plans?)"
    r"|see\s+(?:availability|units|pricing|details|apartments)"
    r"|show\s+(?:availability|units|pricing|me\s+units)"
    r"|check\s+availability"
    r"|available\s+units?"
    r"|view\s+all\s+units?"
    r"|select\s+(?:an?\s+)?(?:apartment|unit)"
    r"|get\s+pricing"
    r")\b",
    re.IGNORECASE,
)

# Page-level dollar-amount detector. Used as a cheap "is there rent
# content on the page already" gate. Two alternations:
#   - ``\d{1,3}(?:,\d{3})+`` matches comma-grouped amounts ($1,500, $12,000)
#   - ``\d{3,5}`` matches contiguous-digit amounts ($995, $1500)
# Two-digit amounts like "$50" (application fee) are intentionally
# excluded — too small to be rent.
_RENT_RE = re.compile(
    r"\$\s?(?:\d{1,3}(?:,\d{3})+|\d{3,5})(?:\.\d{1,2})?"
)

# When the page renders this many dollar amounts on initial load, assume
# the existing extractors will succeed and skip the reveal step.
_MIN_RENT_SIGNALS = 3

# Hard cap on click attempts per page. Even Pattern-B-shaped pages rarely
# have more than ~15 plan cards; cap protects against accidental matches
# on nav menus / footers.
_MAX_CLICKS = 30

# Time we let the DOM settle after each click. Most accordion-style
# reveals are synchronous; the small delay covers CSS transitions that
# gate ``offsetHeight`` reads.
_POST_CLICK_SETTLE_S = 0.15

# Time we let the page settle after the last click before reading the
# updated HTML. Some sites batch multiple reveals into a single
# requestAnimationFrame; 0.4s is conservative without being slow.
_FINAL_SETTLE_S = 0.4


def _count_rent_signals(html: str) -> int:
    """Return the number of ``$NNNN`` matches in *html*. Cheap heuristic."""
    if not html:
        return 0
    return len(_RENT_RE.findall(html))


def looks_like_pattern_b(html: str) -> bool:
    """Return True when the page HTML suggests collapsed-reveal cards.

    Used by the gate in :func:`maybe_reveal` and by unit tests that want
    to assert the classifier alone (without needing a real Playwright
    page).
    """
    if not html:
        return False
    if _count_rent_signals(html) >= _MIN_RENT_SIGNALS:
        return False
    return bool(_REVEAL_TEXT_RE.search(html))


# JavaScript executed inside the page: finds Pattern-B-shaped trigger
# elements and stamps them with ``data-jugnu-reveal-id="N"`` so we can
# address them deterministically from Python. Returns the list of stamped
# IDs. Anchors that navigate to a different path are skipped — clicking
# them would unload the page mid-extraction.
_FIND_TRIGGERS_JS = r"""
() => {
  // The reveal text regex is duplicated here from interactive_reveal.py
  // because Playwright evaluates this string inside the page context and
  // can't import Python. Keep the two in sync.
  const re = /\b(view\s+(?:availability|units|pricing|details|plans|apartments|floor[\s-]?plans?)|see\s+(?:availability|units|pricing|details|apartments)|show\s+(?:availability|units|pricing|me\s+units)|check\s+availability|available\s+units?|view\s+all\s+units?|select\s+(?:an?\s+)?(?:apartment|unit)|get\s+pricing)\b/i;
  const sel = 'button, a, [role="button"], [class*="expand"], [class*="toggle"], [class*="accordion"], [data-toggle], [aria-expanded]';
  const all = Array.from(document.querySelectorAll(sel));
  const here = location.pathname;
  const matches = [];
  for (const el of all) {
    const t = (el.innerText || el.textContent || '').trim();
    if (!t || t.length > 120) continue;
    if (!re.test(t)) continue;
    // Skip anchors that navigate to a different page — clicking would
    // unload the current page mid-extraction.
    if (el.tagName === 'A' && el.href) {
      try {
        const u = new URL(el.href, location.href);
        if (u.pathname && u.pathname !== here && u.pathname !== here + '/' && here !== u.pathname + '/') {
          // Allow same-page hash fragments (#avail-123) — they typically
          // trigger client-side reveal.
          if (!el.getAttribute('href') || !el.getAttribute('href').startsWith('#')) {
            continue;
          }
        }
      } catch (_) { /* malformed href — let it through */ }
    }
    // Don't double-stamp if a previous reveal pass already tagged this
    // element (e.g. on a retry).
    if (el.hasAttribute('data-jugnu-reveal-id')) continue;
    matches.push(el);
  }
  const ids = [];
  matches.slice(0, MAX_CLICKS_PLACEHOLDER).forEach((el, i) => {
    el.setAttribute('data-jugnu-reveal-id', String(i));
    ids.push(i);
  });
  return ids;
}
""".replace("MAX_CLICKS_PLACEHOLDER", str(_MAX_CLICKS))


# Click executed on each tagged element. We use ``.click()`` rather than
# ``page.click()`` so the event is dispatched even if the element is
# off-screen / hidden behind a sticky header — Pattern-B reveals don't
# require visibility, just a click on the element.
_CLICK_TRIGGER_JS = r"""
(id) => {
  const el = document.querySelector('[data-jugnu-reveal-id="' + id + '"]');
  if (!el) return false;
  try {
    el.click();
    return true;
  } catch (e) {
    return false;
  }
}
"""


async def maybe_reveal(
    page: Any,
    *,
    page_html: str | None = None,
) -> dict[str, Any]:
    """Trigger collapsed-reveal buttons if the page looks like Pattern B.

    Parameters
    ----------
    page : Any
        A Playwright Page (or test double exposing ``content()`` and
        ``evaluate()``). When ``None`` the function no-ops — the Jugnu
        fetch-only path has no DOM to mutate.
    page_html : str | None
        Optional pre-fetched HTML. When omitted, the function calls
        ``page.content()`` itself.

    Returns
    -------
    dict[str, Any]
        Telemetry with at least ``triggered`` (bool) and a ``reason``
        when ``triggered=False``. When ``triggered=True`` the dict also
        carries ``clicks`` (int), ``rent_signals_before`` (int),
        ``rent_signals_after`` (int), ``rent_delta`` (int), and an
        ``errors`` list of per-click failure strings.

    The function NEVER raises — Pattern B reveal is best-effort and any
    failure leaves the page state unchanged for downstream adapters.
    """
    if page is None:
        return {"triggered": False, "reason": "no_page"}

    if page_html is None:
        try:
            page_html = await page.content()
        except Exception as exc:  # pragma: no cover - defensive
            return {"triggered": False, "reason": f"content_failed: {exc}"}

    if not page_html:
        return {"triggered": False, "reason": "empty_html"}

    rent_before = _count_rent_signals(page_html)
    if rent_before >= _MIN_RENT_SIGNALS:
        # Page already has rent content; let normal extractors run.
        return {
            "triggered": False,
            "reason": "rents_present",
            "rent_signals_before": rent_before,
        }

    if not _REVEAL_TEXT_RE.search(page_html):
        return {
            "triggered": False,
            "reason": "no_reveal_text",
            "rent_signals_before": rent_before,
        }

    # ── Stamp candidate trigger elements ──────────────────────────────
    try:
        ids = await page.evaluate(_FIND_TRIGGERS_JS)
    except Exception as exc:
        log.debug("interactive_reveal: find_triggers failed: %s", exc)
        return {"triggered": False, "reason": f"find_failed: {exc}"}

    if not ids:
        return {
            "triggered": False,
            "reason": "no_triggers_found",
            "rent_signals_before": rent_before,
        }

    # ── Click each tagged element ─────────────────────────────────────
    clicks = 0
    errors: list[str] = []
    for trigger_id in ids:
        try:
            ok = await page.evaluate(_CLICK_TRIGGER_JS, trigger_id)
            if ok:
                clicks += 1
                await asyncio.sleep(_POST_CLICK_SETTLE_S)
        except Exception as exc:
            errors.append(f"click_{trigger_id}: {str(exc)[:80]}")
            continue

    # Final settle then re-read DOM for telemetry. The adapter that runs
    # next will re-call page.content() itself; we read here only to
    # compute the rent_delta we report.
    try:
        await asyncio.sleep(_FINAL_SETTLE_S)
        page_html_after = await page.content()
    except Exception as exc:
        log.debug("interactive_reveal: post-click content() failed: %s", exc)
        return {
            "triggered": True,
            "clicks": clicks,
            "rent_signals_before": rent_before,
            "errors": errors,
            "post_read_failed": True,
        }

    rent_after = _count_rent_signals(page_html_after)
    return {
        "triggered": True,
        "clicks": clicks,
        "rent_signals_before": rent_before,
        "rent_signals_after": rent_after,
        "rent_delta": rent_after - rent_before,
        "errors": errors,
    }
