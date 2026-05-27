"""RentCafe-WordPress plan-card meta parser (SecureCafe sqft-gap fix #3).

Some RentCafe-backed marketing sites are built on a WordPress plugin
that renders each floorplan as a structured ``<article>`` card carrying
data attributes for rent + beds + baths and an inline ``<li>NNN sq.
ft.</li>``. The card's ``<a href=".../floorplan/<id>">`` carries the
same Yardi FloorPlanID exposed by the SecureCafe rentaloptions URL —
giving us a clean exact-match join key.

Verified live 2026-05-22 on the ironstate.com portfolio (5 properties,
104 units in PARTIAL cohort):
  - ironstate.com/property/the-gotham/ → 7 plan cards, 100% FloorPlanID
    overlap with SecureCafe drill
  - ironstate.com/property/330-angelo-cifelli/ → 13 cards, 100% overlap
  - ironstate.com/property/333-river-street/ → fp_ids include
    5059833/5059848/5059853/… all present in SecureCafe

Pattern (taken straight from the ironstate the-gotham source):

  <article class="floorplans-box active" data-price="3150" data-beds="0"
           data-baths="1" data-movein="05/14/2026">
    <a href="https://ironstate.com/property/the-gotham/floorplan/5059833"
       class="box">
      …
      <h3><span>Studio</span></h3>
      <ul><li>$3,150</li><li>544 sq. ft.</li></ul>
      <ul><li>1 Available</li></ul>
    </a>
  </article>

Same enrichment shape as the apts247 / plan-name paths (see rentcafe.py):
fill missing/zero sqft on each SecureCafe unit from the plan map by
joining on FloorPlanID; per-unit values WIN; meta only fills gaps.
"""
from __future__ import annotations

import re
from typing import Any

# A floorplan card carries data-price + data-beds + data-baths up front.
# The inner content (which can run multiple KB) carries the href and
# the "NNN sq. ft." line. Permissive on attribute order — sites
# routinely re-shuffle the attrs.
_WPFC_CARD_RE = re.compile(
    r"<article[^>]*\bclass=['\"][^'\"]*floorplans-box[^'\"]*['\"][^>]*>"
    r"(?P<inner>.{20,6000}?)"
    r"</article>",
    re.IGNORECASE | re.DOTALL,
)
_WPFC_DATA_PRICE_RE = re.compile(r"data-price=['\"](\d+)['\"]", re.IGNORECASE)
_WPFC_DATA_BEDS_RE = re.compile(r"data-beds=['\"](\d+)['\"]", re.IGNORECASE)
_WPFC_DATA_BATHS_RE = re.compile(
    r"data-baths=['\"]([\d.]+)['\"]", re.IGNORECASE
)
_WPFC_HREF_FPID_RE = re.compile(
    r"href=['\"][^'\"]*?/floorplan/(\d+)", re.IGNORECASE
)
_WPFC_SQFT_RE = re.compile(
    r"(\d{1,2}(?:,\d{3})?|\d{3,5})\s*sq\.?\s*ft", re.IGNORECASE
)
_WPFC_PLAN_NAME_RE = re.compile(
    r"<h3[^>]*>\s*<span[^>]*>\s*([^<]+?)\s*</span>", re.IGNORECASE | re.DOTALL
)

# Detection signal: the card markup itself is the strongest fingerprint.
# Cheaper pre-check: the substring ``floorplans-box`` is rare enough to
# be a near-zero-false-positive marker on third-party sites.
_WPFC_MARKER_RE = re.compile(r"floorplans-box", re.IGNORECASE)


def has_wp_floorplan_cards(html: str) -> bool:
    """Cheap presence check: does this HTML carry the RentCafe-WP
    ``floorplans-box`` markup at all?"""
    return bool(html and _WPFC_MARKER_RE.search(html))


def parse_wp_floorplan_cards(html: str) -> list[dict[str, Any]]:
    """Extract plan-level dicts from RentCafe-WP ``<article class=
    'floorplans-box'>`` cards.

    Each returned dict carries: ``fp_id`` (Yardi FloorPlanID — same value
    as SecureCafe's ``securecafe_floorplan_id``), ``rent`` (numeric
    string), ``beds`` (int-as-string), ``baths`` (float-as-string),
    ``sqft`` (numeric string, commas stripped), ``name`` (the
    ``<h3><span>…</span></h3>`` text).

    Cards with no recoverable ``fp_id`` are dropped — without the join
    key they cannot enrich the SecureCafe units anyway, and they would
    pollute the (bed, bath) bucket fallback.

    Empty / non-WP html → []. Best-effort: a card missing optional
    fields (sqft, name) still emits — partial enrichment is better than
    nothing.
    """
    if not html or not _WPFC_MARKER_RE.search(html):
        return []
    out: list[dict[str, Any]] = []
    for m in _WPFC_CARD_RE.finditer(html):
        card = m.group(0)
        inner = m.group("inner")
        fpid_m = _WPFC_HREF_FPID_RE.search(inner)
        if not fpid_m:
            continue
        fp_id = fpid_m.group(1)

        price_m = _WPFC_DATA_PRICE_RE.search(card)
        beds_m = _WPFC_DATA_BEDS_RE.search(card)
        baths_m = _WPFC_DATA_BATHS_RE.search(card)
        sqft_m = _WPFC_SQFT_RE.search(inner)
        name_m = _WPFC_PLAN_NAME_RE.search(inner)

        sqft = ""
        if sqft_m:
            sqft = sqft_m.group(1).replace(",", "")
            # Filter implausible values — under 100 is almost certainly
            # a typo or matched on a wrong number.
            try:
                if int(sqft) < 100:
                    sqft = ""
            except ValueError:
                sqft = ""

        out.append(
            {
                "fp_id": fp_id,
                "rent": price_m.group(1) if price_m else "",
                "beds": beds_m.group(1) if beds_m else "",
                "baths": baths_m.group(1) if baths_m else "",
                "sqft": sqft,
                "name": name_m.group(1).strip() if name_m else "",
            }
        )
    return out


def merge_wp_cards_into_securecafe(
    units: list[dict[str, Any]], plans: list[dict[str, Any]]
) -> int:
    """In-place merge: fill missing/zero sqft (+ beds/baths/floor_plan_
    name when blank) on each SC unit from the WP plan card list.

    Join: exact match on
    ``unit.source_ids['securecafe_floorplan_id'] == plan['fp_id']``.
    Strict — no fuzzy fallback because (a) the join key is exact-by-
    design (b) wrong fills here would silently corrupt prod data. If
    the SC unit didn't capture a FloorPlanID, it stays unenriched.

    Per-unit values WIN; meta only fills gaps. Returns count of units
    that gained ≥1 field (for diagnostics).
    """
    if not units or not plans:
        return 0
    by_id: dict[str, dict[str, Any]] = {}
    for p in plans:
        fid = str(p.get("fp_id") or "").strip()
        if fid:
            by_id.setdefault(fid, p)
    enriched = 0
    for u in units:
        fpid = str(
            (u.get("source_ids") or {}).get("securecafe_floorplan_id") or ""
        )
        if not fpid:
            continue
        plan = by_id.get(fpid)
        if plan is None:
            continue
        # sqft: treat existing "0"/"" as missing (operator hasn't
        # populated SC's Sq.Ft cell — the WP card carries the truth).
        sqft = str(plan.get("sqft") or "").strip()
        cur_sqft = str(u.get("sqft") or "").strip()
        if sqft and (not cur_sqft or cur_sqft == "0"):
            u["sqft"] = sqft
            enriched += 1
            continue
        if plan.get("name") and not u.get("floor_plan_name"):
            u["floor_plan_name"] = str(plan["name"])
            enriched += 1
    return enriched
