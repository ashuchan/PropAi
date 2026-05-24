"""Generic plan-text adapter for bespoke custom-CMS multifamily sites.

A last-resort plan-level extractor for properties whose marketing site
uses no recognizable PMS framework but DOES embed plan info in body
text using common multifamily conventions.

Verified live 2026-05-21 on:
  - www.colonialcourtapts.com/floor-plans (Drupal blocks, plan-level)
  - stargatewest.com/tucson-rental-floor-plans/ (custom WP, plan-level)
  - www.countryvillageapthomes.com (slick carousel, plan-level)

Body-text patterns observed:
  * colonialcourt: "2 Bedroom / 1 Bath Apartment ... $1495"
  * stargatewest: "1 Bedroom / 1 Bathroom From $1275"
                  separately:  "1 BDRM / 1 BTH 675 Sq. Ft."
  * countryvillage: prices interleaved with $300 deposits in carousel

The adapter is gated VERY tightly to avoid false-firing across the
fleet:
  * Detector signal yields at LOW confidence (0.55) — every recognized
    PMS / CMS signal beats this. Only fires as a last resort when no
    stronger adapter detected.
  * Parser requires ≥2 distinct plan rows (avoids single-noise matches
    like "1 BDRM Studio for $500" in an amenity blurb).
  * Each row must have BOTH a bed count AND a $-prefixed price within
    proximity — prevents matching deposit-only lines.

Plan-level only by design. The 2 truly-empty bespoke sites (wildwoodmanor,
princetonmanagement) correctly emit nothing.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)


_GENERIC_DOM_JS = r"""
async () => {
  const T = (el) => (el ? el.innerText.replace(/\s+/g, ' ').trim() : '');
  return {ok: true, bodyText: T(document.body)};
}
"""


# Plan-line regex — accept both long-form ("Bedroom") and short-form
# ("BDRM") + both "Bath" and "BTH". The "rest" context (next ~150 chars
# after the bath token) is sliced from the source body string AFTER
# the match — NOT captured inside the regex — so that ``re.finditer``
# can find subsequent plan lines without the rest-window swallowing
# them.
_PLAN_LINE_RE = re.compile(
    r"(?P<beds>\d+|studio)\s*"
    r"(?:bedroom|bdrm|bd|br)s?\b"
    r"\s*[/|-]?\s*"
    r"(?P<baths>\d+(?:\.\d+)?)\s*"
    r"(?:bathroom|bath|bth|ba)s?\b",
    re.IGNORECASE,
)
# Lookahead window after a plan-line match. Sized to capture realistic
# "${rent}" placement (15-80 chars after baths) while rejecting nav-menu
# items whose closest $ is hundreds of chars away in the body.
_REST_WINDOW = 100
# Pattern that signals the start of the NEXT plan in the body — when
# this appears in the lookahead window BEFORE the $, we've crossed a
# plan boundary (the current "plan" was a nav-menu item paired with a
# later body plan's price). Drop the row.
_NEXT_PLAN_BOUNDARY_RE = re.compile(
    r"\d+\s*(?:bedroom|bdrm|bd|br)s?\b|\bstudio\b",
    re.IGNORECASE,
)
_SQFT_RE = re.compile(r"(\d[\d,]{2,4})\s*sq\.?\s*(?:ft|feet)", re.IGNORECASE)
# Match either a single price (From $X / $X) OR a range ($X - $X).
_PRICE_RE = re.compile(
    r"(?:from\s+)?\$\s*([\d,]+)(?:\s*-\s*\$?\s*([\d,]+))?",
    re.IGNORECASE,
)
# Distinguishing deposit-style amounts ($300, $500) from rent ($1,275)
# by magnitude. Rent floor: anything under $400 is unlikely to be a
# multifamily monthly rent in any US market.
_RENT_FLOOR = 400


def parse_generic_plan_text(body: str, url: str) -> list[dict]:
    """Extract plan-level rows from page body text. Returns [] when no
    rows pass the ≥2-plan / has-rent guards."""
    if not body:
        return []
    out: list[dict] = []
    seen_signatures: set[tuple] = set()
    for m in _PLAN_LINE_RE.finditer(body):
        bed_v = m.group("beds")
        beds = 0 if bed_v.lower() == "studio" else int(bed_v)
        baths = m.group("baths")
        # Pull the lookahead window from the source body — NOT a capture
        # group — so finditer can still find subsequent plan lines.
        rest = body[m.end():m.end() + _REST_WINDOW]
        # Sqft (optional — many bespoke sites omit it).
        sqft = ""
        sq_m = _SQFT_RE.search(rest)
        if sq_m:
            sqft = sq_m.group(1).replace(",", "")
        # Rent — first $-amount above RENT_FLOOR in the proximity window.
        # If the lookahead contains the start of ANOTHER plan-line BEFORE
        # the first $-amount, we've crossed a plan boundary (current was
        # a nav-menu item, not a real plan row). Drop the row.
        rent_low: int | None = None
        rent_high: int | None = None
        for p_m in _PRICE_RE.finditer(rest):
            low = money_to_int(p_m.group(1))
            high = money_to_int(p_m.group(2)) if p_m.group(2) else low
            if low is None or low < _RENT_FLOOR:
                continue
            # Boundary check — anything between match-start and the
            # current $-position that looks like another plan-line means
            # this $ doesn't belong to the current plan.
            preceding = rest[: p_m.start()]
            if _NEXT_PLAN_BOUNDARY_RE.search(preceding):
                continue
            rent_low = low
            rent_high = high if high is not None else low
            break
        # Skip rows without a real rent — defends against rows like
        # "1 Bedroom 1 Bath required deposit $300".
        if rent_low is None:
            continue
        sig = (beds, baths, sqft, rent_low, rent_high)
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)
        plan_name = f"{beds} Bedroom / {baths} Bath" if beds else f"Studio / {baths} Bath"
        out.append(
            make_unit_dict(
                floor_plan_name=plan_name,
                bed_label=bed_label_from(beds, plan_name),
                bedrooms=str(beds),
                bathrooms=baths,
                sqft=sqft,
                unit_number="",  # plan-level only
                rent_low=rent_low,
                rent_high=rent_high,
                rent_range=format_rent_range(rent_low, rent_high),
                availability_status="AVAILABLE",
                source_api_url=url,
                extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT",
            )
        )
    # Anti-noise: require ≥2 distinct plan rows. Single-match could be
    # an amenity blurb mentioning "1 bedroom from $500".
    if len(out) < 2:
        return []
    return out


class GenericPlanTextAdapter:
    """Last-resort plan-level text extractor for bespoke custom-CMS sites.

    Detector yields at 0.55 — every recognized PMS / CMS signal beats
    this. Only fires when nothing else routed.
    """

    pms_name: str = "generic_plan_text"
    _fingerprints: list[str] = []  # no host-specific fingerprints

    async def try_dom(self, page, html: str, ctx: AdapterContext):
        """2026-05-24 Phase 1 cascade hook — last-resort plan-text extractor.

        Wraps ``parse_generic_plan_text``. Returns LOWER confidence (0.6)
        because this adapter is by-design a fallback: the parser only
        looks at body text without HTML structure, so its precision is
        weaker than per-PMS adapters. The cascade should still try
        higher-fidelity options first.

        Empty-marker check: the parser requires ≥2 plan lines AND each
        line must have a real rent above $400. The cheap pre-check
        looks for at least one ``$`` followed by digits — pages without
        any dollar amount can't yield a plan-text row.
        """
        from ma_poc.pms.adapters.base import AdapterDomResult

        if not html or "$" not in html:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_GENERIC_PLAN_TEXT",
                reason="no_dollar_marker",
            )
        # The parser walks body TEXT — strip script/style up front to
        # mirror what page.evaluate(document.body.innerText) would give.
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            # Drop script/style blocks so their content doesn't pollute
            # the text walker (CMS-functions/PropLeadSource leak).
            for s in soup(["script", "style", "noscript"]):
                s.decompose()
            body_text = soup.get_text(" ", strip=True)
        except Exception:
            # Fallback: regex-strip script/style + tag-flatten — mirror
            # the playbook recipe in _capture_concession_from_html.
            import re as _re
            no_code = _re.sub(
                r"<(script|style|noscript)\b[^>]*>.*?</\1>",
                " ", html, flags=_re.IGNORECASE | _re.DOTALL,
            )
            body_text = _re.sub(r"\s+", " ", _re.sub(r"<[^>]+>", " ", no_code))
        if not body_text:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_GENERIC_PLAN_TEXT",
                reason="empty_text",
            )
        url = getattr(ctx, "base_url", "") or ""
        try:
            raw_units = parse_generic_plan_text(body_text, url)
        except Exception as e:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_GENERIC_PLAN_TEXT",
                reason=f"parse_exception:{type(e).__name__}",
            )
        if not raw_units:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_GENERIC_PLAN_TEXT",
                reason="parser_silent_empty",
            )
        try:
            from ma_poc.extraction.dq_guards import apply_unit_guards
            guarded = apply_unit_guards(
                raw_units,
                property_id=getattr(ctx, "property_id", ""),
                source_html=html,
                detect_same_rent=True,
            )
        except Exception:
            guarded = raw_units
        if not guarded:
            return AdapterDomResult.empty(
                tier="TIER_3_DOM_GENERIC_PLAN_TEXT",
                reason="dq_guards_rejected_all",
            )
        # Lower confidence than per-PMS adapters — this is a fallback.
        # ``is_high_confidence`` requires ≥0.7; returning 0.6 means the
        # cascade WILL still try other paths (LLM_DOM as backup).
        return AdapterDomResult(
            units=guarded,
            plan_summaries=[],
            tier_used="TIER_3_DOM_GENERIC_PLAN_TEXT",
            selector_signature="plan-text-line-regex",
            confidence=0.6,
            debug={"raw_count": len(raw_units), "guarded_count": len(guarded)},
        )

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used="TIER_1_DOM_GENERIC_PLAN_TEXT")

        # 2026-05-24: prefer page.evaluate(bodyText) when a live Playwright
        # page is available, but fall back to ``fetch_result.body`` when
        # the Jugnu pipeline dispatches with page=None (the L1 fetcher
        # already captured the HTML — re-rendering through patchright
        # would be wasted work). Pre-fix, the adapter bailed on every
        # Jugnu invocation with "no live page" — verified live on PID
        # 30350 stargatewest.com (canary 2026-05-24-new-adapters).
        body = ""
        evaluate = getattr(page, "evaluate", None) if page is not None else None
        if callable(evaluate):
            try:
                payload = await evaluate(_GENERIC_DOM_JS)
            except Exception as exc:
                log.debug("generic_plan_text evaluate failed err=%s", exc)
                payload = None
            if isinstance(payload, dict):
                body = str(payload.get("bodyText") or "")
        if not body:
            # Fall back to the L1 fetcher's already-captured body.
            fr = getattr(ctx, "fetch_result", None)
            raw = getattr(fr, "body", None) if fr is not None else None
            if isinstance(raw, bytes):
                body = raw.decode("utf-8", errors="replace")
            elif isinstance(raw, str):
                body = raw
        if not body:
            result.confidence = 0.0
            result.errors.append(
                "generic_plan_text: no body source — page.evaluate failed AND "
                "fetch_result.body empty"
            )
            return result
        winning = self._winning_url(page, ctx)
        rows = parse_generic_plan_text(body, winning)
        if not rows:
            result.confidence = 0.0
            result.errors.append(
                "generic_plan_text: <2 distinct plan rows found "
                "(anti-noise threshold; falls to LLM)"
            )
            return result
        from ma_poc.extraction.post_process import post_process

        pp = post_process(rows, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = winning
            # Cap confidence below other plan-level adapters so a real
            # PMS routing always wins when both apply.
            result.confidence = min(0.70, 0.55 + 0.03 * pp.n_admitted)
            return result
        result.confidence = 0.0
        result.errors.append(
            f"generic_plan_text: {len(rows)} rows failed unit_validity"
        )
        return result

    @staticmethod
    def _winning_url(page: Page | None, ctx: AdapterContext) -> str:
        # 2026-05-24: tolerate page=None (Jugnu pipeline dispatches with
        # the L1-fetched body on ctx.fetch_result instead of a live page).
        # Prefer fetch_result.final_url over ctx.base_url when available
        # because the L1 fetch may have followed redirects.
        if page is not None:
            try:
                u = getattr(page, "url", None)
                if u:
                    return u
            except Exception:
                pass
        fr = getattr(ctx, "fetch_result", None)
        if fr is not None:
            final = getattr(fr, "final_url", None)
            if final:
                return str(final)
        return getattr(ctx, "base_url", "") or ""

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
