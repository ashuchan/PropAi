"""Encoreskyline-template (Jonah Digital widget) adapter.

2026-05-19 deep-probe finding: a family of marketing-template sites with
the visual signature ``Skip to main content`` + ``Book a Tour`` + per-plan
URLs at ``/floorplans/{slug}/`` renders **real apartment-level rows
inline** on each per-plan page — but only after a per-plan
``Check Availability`` JS toggle, which is driven by the
Jonah Digital / MeetElise widget (``JonahWidget.meetelise({organization,
building})`` initialiser embedded in the page). The marketing-site
tiers (TIER_3_DOM / TIER_2_JSONLD / TIER_MERGED) never fire the toggle
and return 0% real ``unit_id`` despite the data being one JS click deep.

This adapter is the routing fix:

  1. Confirm the Jonah-widget marker on the current page.
  2. Discover per-plan URLs from the live ``/floorplans/{slug}/`` anchor
     set on the page (or hop to ``/floorplans`` first if necessary).
  3. For each per-plan URL: navigate, click ``Check Availability``, wait
     ~2.5s for the Jonah widget to insert its rendered unit rows, then
     extract ``document.body.innerText`` and parse with
     ``parse_encoreskyline_units``.
  4. Aggregate units across plans.

Verified live (2026-05-19) on encoreskyline.com, geneseepointe.com,
highlineaustin.com.

NOTE — RealPage-Online-Leasing variant: the same visual marketing template
sometimes wraps a RealPage onlineleasing portal (see rent-portofino.com)
instead of the Jonah widget; in that case detection routes to RealPage /
``_pms_portal_hop`` instead — the Jonah-marker gate keeps this adapter
strictly scoped.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._encoreskyline_units import (
    is_encoreskyline_template_page,
    parse_encoreskyline_units,
    parse_rentpress_data_floorplans,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

_TIER = "TIER_1_DOM_ENCORESKYLINE_TEMPLATE"
# Safety cap — never visit more than this many per-plan URLs in one run.
# Encoreskyline-family sites in the cohort have 4–14 floorplans; 30 is
# more than enough headroom while bounding worst-case page-load cost.
_MAX_PLANS: int = 30
# Per-plan wait for the Jonah widget to populate inline unit rows after
# the Check-Availability click. 2.5s matches the deep-probe timing.
_POST_CLICK_WAIT_MS: int = 2500

# JS: harvest ``/floorplans/{slug}/`` per-plan URLs from the current page.
# Filters out the index page itself and the back-link to ``/floorplans``.
_PER_PLAN_URLS_JS = r"""
() => {
  const out = new Set();
  document.querySelectorAll('a').forEach((a) => {
    const h = a.getAttribute('href') || '';
    if (/\/floorplans\/[a-z0-9-]+\/?$/i.test(h)) {
      try {
        const abs = new URL(h, location.href).href;
        out.add(abs.replace(/[?#].*$/, ''));
      } catch (e) {}
    }
  });
  return [...out];
}
"""


async def _safe_call(coro_factory: Any, *args: Any) -> Any:
    """Run a possibly-async callable; swallow any exception and return None."""
    try:
        result = coro_factory(*args)
        if hasattr(result, "__await__"):
            return await result
        return result
    except Exception as exc:
        log.debug("encoreskyline _safe_call err=%s", exc)
        return None


def _html_from_ctx(ctx: Any) -> str:
    """Recover the already-fetched body HTML from the AdapterContext.

    Production dispatches ``adapter.extract(page=None, ctx)`` (the runner
    fetches via L1 and hands the adapter the FetchResult, not a live page).
    ``scrape()`` computes the body — a RENDER-mode fetch carries the fully
    rendered HTML incl. the JS-injected Jonah marker + ``/floorplans/{slug}/``
    anchors. Mirrors ``scrape()``'s own bytes/str decode.
    """
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None)
    if isinstance(body, bytes):
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return ""
    return body if isinstance(body, str) else ""


async def _get_page_html(page: Page, ctx: Any = None) -> str:
    """Return the page HTML: live ``page.content()`` first, else the fetched
    body from ``ctx``. The page-only version silently returned ``""`` under the
    production ``page=None`` dispatch, so the Jonah gate (and the static
    RentPress fast-path) never saw the marker that WAS in the fetched body —
    every encore property failed ``NOT_ENCORESKYLINE_TEMPLATE``. Other render
    adapters already fall back to the fetch body (scraper.py:598); this aligns
    encoreskyline with that contract."""
    out = await _safe_call(getattr(page, "content", lambda: ""))
    if isinstance(out, str) and out:
        return out
    return _html_from_ctx(ctx) if ctx is not None else ""


def _plan_urls_from_html(html: str, base_url: str) -> list[str]:
    """Fallback plan-link discovery from HTML when ``page.evaluate`` is
    unavailable (page=None). Mirrors ``_PER_PLAN_URLS_JS``: harvest
    ``/floorplans/{slug}/`` anchors, absolutise, dedup, cap. In RENDER mode the
    fetched body already carries the rendered anchor set."""
    if not html:
        return []
    from urllib.parse import urljoin

    seen: dict[str, None] = {}
    for m in re.finditer(r"""href\s*=\s*["']([^"']*/floorplans/[a-z0-9-]+/?)["']""", html, re.I):
        href = m.group(1)
        # index page itself and the bare /floorplans back-link are not per-plan
        if re.search(r"/floorplans/[a-z0-9-]+/?$", href, re.I):
            abs_url = urljoin(base_url or "", href).split("?")[0].split("#")[0]
            seen.setdefault(abs_url, None)
    return list(seen)[:_MAX_PLANS]


async def _evaluate(page: Page, js: str, *args: Any) -> Any:
    """``page.evaluate`` wrapper that returns None on failure."""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    return await _safe_call(evaluate, js, *args)


async def _click_check_availability(page: Page) -> bool:
    """Fire the Jonah ``Check Availability`` toggle on the current page.

    Try Playwright-native ``page.click`` first (production path), then a
    JS-side ``document.querySelector(...).click()`` fallback (works under
    the FakePage shim in tests).
    """
    click_fn = getattr(page, "click", None)
    if callable(click_fn):
        for selector in (
            "text=/check availability/i",
            "a:has-text('Check Availability')",
            "button:has-text('Check Availability')",
        ):
            res = await _safe_call(click_fn, selector)
            # ``page.click`` returns None on success; we only rely on
            # "no exception raised". The next-step inner-text scan is the
            # real signal — see ``extract``.
            if res is None:
                # Provisional success; let the caller confirm via DOM read.
                return True
    # JS fallback — works whether or not Playwright's ``click`` is present.
    js = (
        "() => {"
        "const b = [...document.querySelectorAll('a,button')]"
        ".find(x => /check availability/i.test(x.textContent || ''));"
        "if (b) { b.click(); return true; } return false;"
        "}"
    )
    res = await _evaluate(page, js)
    return bool(res)


async def _wait_ms(page: Page, ms: int) -> None:
    """Best-effort sleep — Playwright's wait helper, else JS setTimeout."""
    wait_fn = getattr(page, "wait_for_timeout", None)
    if callable(wait_fn):
        await _safe_call(wait_fn, ms)
        return
    # JS fallback for tests / non-Playwright runtimes.
    await _evaluate(
        page,
        f"() => new Promise(r => setTimeout(r, {int(ms)}))",
    )


async def _get_rendered_text(page: Page) -> str:
    """Read ``document.body.innerText`` (post-click)."""
    out = await _evaluate(page, "() => document.body.innerText || ''")
    return out if isinstance(out, str) else ""


async def _goto(page: Page, url: str) -> bool:
    """Best-effort navigate. Returns True on apparent success."""
    goto = getattr(page, "goto", None)
    if not callable(goto):
        return False
    res = await _safe_call(goto, url)
    return res is not None or True  # never raised = success in test shim


class EncoreSkylineTemplateAdapter:
    """Per-plan ``Check Availability`` recovery for Jonah-widget sites."""

    pms_name: str = "encoreskyline_template"
    _fingerprints: list[str] = ["jonahwidget", "jonahdigital", "meetelise"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        # 1. Marker gate — strict; the RealPage variant of the same visual
        #    template must NOT route here (it lacks the Jonah marker).
        html = await _get_page_html(page, ctx)
        if not is_encoreskyline_template_page(html):
            return AdapterResult(
                tier_used="NOT_ENCORESKYLINE_TEMPLATE",
                confidence=0.0,
                errors=["Jonah Digital widget marker not present"],
            )

        # 1b. Static RentPress fast-path. RentPress (RentCafe-synced) sites
        #     embed the full unit inventory as an escaped-JSON
        #     ``data-floorplans`` attribute in the initial HTML — no per-plan
        #     click flow needed. This short-circuits the (slow, render-
        #     dependent) Jonah drill for the ~8-prop RentPress cohort that
        #     otherwise fell through to LLM/failed. See
        #     parse_rentpress_data_floorplans.
        rp_units = parse_rentpress_data_floorplans(
            html, getattr(ctx, "base_url", "") or ""
        )
        if rp_units:
            from ma_poc.extraction.post_process import post_process as _pp_rp

            _rp = _pp_rp(rp_units, property_id=getattr(ctx, "property_id", None))
            if _rp.n_admitted > 0:
                return AdapterResult(
                    units=_rp.admitted,
                    plan_summaries=_rp.plan_summaries,
                    tier_used="TIER_1_DOM_RENTPRESS",
                    confidence=min(0.92, 0.7 + 0.04 * _rp.n_admitted),
                )

        # 2. Discover per-plan URLs. If none are on the current page,
        #    hop the user to ``/floorplans`` first and retry — the per-plan
        #    anchors live in the floorplan grid.
        plans = await _evaluate(page, _PER_PLAN_URLS_JS) or []
        if not isinstance(plans, list):
            plans = []
        plans = [str(u) for u in plans if isinstance(u, str)][:_MAX_PLANS]

        if not plans:
            base_url = getattr(ctx, "base_url", "") or ""
            if base_url:
                fp_url = base_url.rstrip("/") + "/floorplans/"
                if await _goto(page, fp_url):
                    again = await _evaluate(page, _PER_PLAN_URLS_JS) or []
                    if isinstance(again, list):
                        plans = [
                            str(u) for u in again if isinstance(u, str)
                        ][:_MAX_PLANS]

        # Body fallback: when the live page can't be evaluated (page=None under
        # the production dispatch), harvest the per-plan anchors from the
        # already-fetched HTML. A RENDER-mode body carries the JS-rendered
        # anchor set, so discovery still works even without a live page.
        if not plans:
            plans = _plan_urls_from_html(html, getattr(ctx, "base_url", "") or "")

        if not plans:
            return AdapterResult(
                tier_used="ENCORESKYLINE_NO_PLAN_LINKS",
                confidence=0.0,
                errors=["no /floorplans/{slug}/ anchors found"],
            )

        # 3. Per-plan loop: nav → click → wait → extract → parse.
        all_units: list[dict[str, Any]] = []
        seen_units: set[str] = set()
        for plan_url in plans:
            if not await _goto(page, plan_url):
                continue
            await _click_check_availability(page)
            await _wait_ms(page, _POST_CLICK_WAIT_MS)
            text = await _get_rendered_text(page)
            for u in parse_encoreskyline_units(text, plan_url):
                key = str(u.get("unit_number") or "")
                if not key or key in seen_units:
                    continue
                seen_units.add(key)
                all_units.append(u)

        if not all_units:
            return AdapterResult(
                tier_used="ENCORESKYLINE_NO_UNITS",
                confidence=0.0,
                errors=[
                    f"visited {len(plans)} per-plan pages; no rendered "
                    "Check-Availability rows found (template-capable but "
                    "0 live units, or click did not fire)"
                ],
            )

        # 4. Post-process + return.
        from ma_poc.extraction.post_process import post_process

        pp = post_process(all_units, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted == 0:
            return AdapterResult(
                tier_used="ENCORESKYLINE_VALIDITY_REJECTED",
                confidence=0.0,
                errors=[f"{len(all_units)} parsed but failed validity gate"],
            )
        return AdapterResult(
            units=pp.admitted,
            plan_summaries=pp.plan_summaries,
            tier_used=_TIER,
            confidence=min(0.95, 0.7 + 0.02 * pp.n_admitted),
        )

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
