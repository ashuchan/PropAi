"""Wix-hosted multifamily site with embedded plan-level data.

Wix property sites use per-instance ``comp-{hash}`` class names that
vary per-site, so selector-based detection isn't portable. But many
Wix multifamily templates emit a STABLE inline text pattern for plan
cards:

  "{Plan Title} {Plan Code} {Beds} | {Baths} | {Sqft} Sq. Ft.
   ${deposit} Starting at ${rent} Request More Info"

Verified live 2026-05-21 on
www.liveatarcos.com/phoenix-apartment-floor-plans — 3 plan cards
(Studio S1, One Bedroom A1, One Bedroom A2) all matching this pattern.

Adapter walks the DOM, finds elements matching the "Starting at $X"
text marker, then walks up to a container holding the plan title +
specs + rent. Plan-level only — Wix marketing sites don't typically
publish per-unit roster.

The detection signal is intentionally narrow: Wix host marker AND
"Starting at $" pattern in HTML body. WixNoPmsAdapter handles other
Wix sites that lack this pattern (e.g. truly plan-less brochures).
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


_WIX_DOM_JS = r"""
async () => {
  // Find every element whose innerText contains the "Starting at $X"
  // marker — these are plan card root candidates. Walk up to the
  // smallest container that also contains the plan code, beds, baths
  // and sqft.
  const T = (el) => (el ? el.innerText.replace(/\s+/g, ' ').trim() : '');
  const allEls = Array.from(document.querySelectorAll('div, section, article, li'));
  const starterRe = /Starting\s+at\s*\$\s*[\d,]+/i;
  const fullPlanRe = /(?:bed|studio).{0,80}\|.{0,40}bath.{0,80}\|.{0,40}sq/i;
  const seen = new Set();
  const cards = [];
  for (const el of allEls) {
    const text = T(el);
    if (text.length < 30 || text.length > 1200) continue;
    if (!starterRe.test(text)) continue;
    if (!fullPlanRe.test(text)) continue;
    // Walk up to the SMALLEST container with the complete pattern (to
    // avoid grabbing the entire page-section as one card). The current
    // element should already be the smallest one matching since we
    // bound text length.
    const key = text;
    if (seen.has(key)) continue;
    seen.add(key);
    cards.push({
      tag: el.tagName,
      id: el.id || '',
      classes: (el.className || '').toString().slice(0, 120),
      text: text,
    });
  }
  return {ok: cards.length > 0, cards: cards};
}
"""


# Parse pattern. Two card flavors observed on the live page:
#   numbered: "1 Bed | 1 Bath | 534 Sq. Ft."   (bed count + "Bed" word)
#   studio:   "Studio | 1 Bath | 424 Sq. Ft."  (just "Studio", no "Bed" word)
# Both lead into the next field with " | " — the "bed" suffix is OPTIONAL.
_SPECS_RE = re.compile(
    r"(?P<beds>studio|\d+)(?:\s*(?:bed|bedroom)s?)?\s*\|\s*"
    r"(?P<baths>\d+(?:\.\d+)?)\s*bath\w*\s*\|\s*"
    r"(?P<sqft>\d[\d,]*)\s*(?:sq\.?\s*ft|square\s*feet)",
    re.IGNORECASE,
)
_STARTING_AT_RE = re.compile(r"Starting\s+at\s*\$\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_DEPOSIT_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s+Starting\s+at", re.IGNORECASE)


def _split_plan_title_and_code(prefix: str) -> tuple[str, str]:
    """The text before the specs block is "{Plan Title} {Plan Code}"
    where Plan Code is typically a short alphanumeric like S1/A1/B2.
    If the last token of the prefix is 1-4 chars and alphanumeric, it's
    the plan code; otherwise the whole prefix is the title."""
    prefix = prefix.strip()
    if not prefix:
        return "", ""
    parts = prefix.rsplit(maxsplit=1)
    if len(parts) == 2 and re.fullmatch(r"[A-Z]\d{0,2}|\d[A-Z]{0,2}", parts[1].upper()):
        return parts[0].strip(), parts[1].strip()
    return prefix, ""


def _parse_card_text(text: str) -> dict | None:
    """Parse one Wix plan-card text. Returns dict on success, None on failure."""
    m = _SPECS_RE.search(text)
    if not m:
        return None
    bed_str = m.group("beds")
    beds = 0 if bed_str.lower() == "studio" else int(bed_str)
    baths = m.group("baths")
    sqft = m.group("sqft").replace(",", "")
    sa = _STARTING_AT_RE.search(text)
    if not sa:
        return None
    rent = money_to_int(sa.group(1))
    # Prefix (before the specs block) carries the title + optional code.
    prefix = text[: m.start()].rstrip(" ,;")
    title, code = _split_plan_title_and_code(prefix)
    dep_match = _DEPOSIT_RE.search(text)
    deposit = dep_match.group(1).replace(",", "") if dep_match else ""
    return {
        "title": title,
        "code": code,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
        "rent": rent,
        "deposit": deposit,
    }


def parse_wix_floor_plans_html(html: str, url: str) -> list[dict]:
    """Body-fallback parser for Jugnu (page=None). Walks div/section/article/li
    in the snapshot HTML mirroring ``_WIX_DOM_JS``, extracts text via
    BeautifulSoup, and feeds matching cards through ``parse_wix_floor_plans``.
    Returns ``[]`` when BeautifulSoup is unavailable or no matching cards
    exist in the snapshot."""
    if not html:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []
    starter_re = re.compile(r"Starting\s+at\s*\$\s*[\d,]+", re.IGNORECASE)
    full_plan_re = re.compile(r"(?:bed|studio).{0,80}\|.{0,40}bath.{0,80}\|.{0,40}sq", re.IGNORECASE)
    seen: set[str] = set()
    cards: list[dict] = []
    for el in soup.find_all(["div", "section", "article", "li"]):
        text = el.get_text(" ", strip=True)
        if not text or len(text) < 30 or len(text) > 1200:
            continue
        if not starter_re.search(text):
            continue
        if not full_plan_re.search(text):
            continue
        if text in seen:
            continue
        seen.add(text)
        cards.append({
            "tag": (el.name or "").upper(),
            "id": el.get("id", "") or "",
            "classes": " ".join(el.get("class") or [])[:120],
            "text": text,
        })
    return parse_wix_floor_plans(cards, url)


def parse_wix_floor_plans(cards: list[dict], url: str) -> list[dict]:
    out: list[dict] = []
    seen_codes: set[str] = set()
    for c in cards:
        if not isinstance(c, dict):
            continue
        text = str(c.get("text") or "")
        parsed = _parse_card_text(text)
        if not parsed:
            continue
        # Dedupe by (title, code, rent) — Wix sites sometimes render the
        # same card multiple times under different parent containers.
        key = f"{parsed['title']}|{parsed['code']}|{parsed['rent']}"
        if key in seen_codes:
            continue
        seen_codes.add(key)
        plan_name = parsed["code"] or parsed["title"]
        out.append(
            make_unit_dict(
                floor_plan_name=plan_name,
                bed_label=bed_label_from(parsed["beds"], parsed["title"]),
                bedrooms=str(parsed["beds"]),
                bathrooms=parsed["baths"],
                sqft=parsed["sqft"],
                unit_number="",
                rent_low=parsed["rent"],
                rent_high=parsed["rent"],
                rent_range=format_rent_range(parsed["rent"], parsed["rent"]),
                deposit=parsed["deposit"],
                availability_status="AVAILABLE",
                source_api_url=url,
                extraction_tier="TIER_1_DOM_WIX_FLOOR_PLANS",
            )
        )
    return out


class WixFloorPlansAdapter:
    """Wix-hosted multifamily site with the "Starting at $X" plan-card
    text pattern. Plan-level only."""

    pms_name: str = "wix_floor_plans"
    _fingerprints: list[str] = [
        "static.parastorage.com",
        "wix.com",
        "wixstatic.com",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used="TIER_1_DOM_WIX_FLOOR_PLANS")
        # 2026-05-24: page.evaluate when a live page is available; fall
        # back to BeautifulSoup body-walk when Jugnu dispatches with
        # page=None (snapshot on ctx.fetch_result.body).
        winning = self._winning_url(page, ctx)
        rows: list[dict] = []
        evaluate = getattr(page, "evaluate", None) if page is not None else None
        if callable(evaluate):
            try:
                payload = await evaluate(_WIX_DOM_JS)
            except Exception as exc:
                log.debug("wix_floor_plans evaluate failed err=%s", exc)
                payload = None
            if isinstance(payload, dict) and payload.get("ok"):
                cards = payload.get("cards") or []
                if isinstance(cards, list) and cards:
                    rows = parse_wix_floor_plans(cards, winning)
        if not rows:
            fr = getattr(ctx, "fetch_result", None)
            raw = getattr(fr, "body", None) if fr is not None else None
            body_str = (
                raw.decode("utf-8", errors="replace") if isinstance(raw, bytes)
                else raw if isinstance(raw, str) else ""
            )
            if body_str:
                rows = parse_wix_floor_plans_html(body_str, winning)
        if not rows:
            result.confidence = 0.0
            result.errors.append(
                f"wix_floor_plans: parser produced no rows from {len(cards)} cards"
            )
            return result
        from ma_poc.extraction.post_process import post_process

        pp = post_process(rows, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = winning
            result.confidence = min(0.82, 0.60 + 0.05 * pp.n_admitted)
            return result
        result.confidence = 0.0
        result.errors.append(
            f"wix_floor_plans: {len(rows)} rows failed unit_validity post-process"
        )
        return result

    @staticmethod
    def _winning_url(page: Page | None, ctx: AdapterContext) -> str:
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
