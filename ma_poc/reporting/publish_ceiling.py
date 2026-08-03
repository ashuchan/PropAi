"""Publish-ceiling verifier — grade a zero-unit result's "no data published"
claim with a machine-checkable evidence bundle.

Campaign context (2026-07-12): the user's GOLD definition counts a property
as gold when EITHER we extract Tier-1 unit-level data OR we can prove, at
100% confidence, that the operator publishes no unit-level data (plan-only /
no-availability / no-data). This module produces that proof — or refuses to.

THE CARDINAL GUARD (rentcafe audit, pid 18158 "madrid"): a page can carry an
explicit "No apartments available" MARKER *and still list real units*
(madrid had the marker string + 2 unit rows at $1,795). So a "no data"
verdict must NEVER rest on a marker string alone. If rent tokens are present
in the HTML but zero units were extracted, that is an EXTRACTION_MISS
(a bug to fix), not a publish-ceiling — it must not be graded as gold.

This is a PURE function over already-gathered signals (html characterization,
the tier-attempt trace, the fetched HTML). It performs no network I/O — the
deeper probing (floorplan detail pages, API calls) happens upstream in the
adapter/link-hop; here we only judge whether what was gathered constitutes
airtight evidence of a publish-ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ma_poc.pms.adapters._no_availability import (
    detect_no_availability,
    is_empty_inventory_page,
    is_pre_leasing_site,
)


class PublishCeiling(StrEnum):
    """Graded outcome of the publish-ceiling assessment."""

    CONFIRMED_NO_DATA = "CONFIRMED_NO_DATA"      # gold-eligible: proven no units published
    CONFIRMED_PLAN_ONLY = "CONFIRMED_PLAN_ONLY"  # gold-eligible: proven only plan-level published
    EXTRACTION_MISS = "EXTRACTION_MISS"          # NOT gold: data present, extraction failed (fix it)
    NEEDS_RENDER = "NEEDS_RENDER"                # NOT gold: SPA/embed shell, can't verify offline
    UNCERTAIN = "UNCERTAIN"                      # NOT gold: insufficient evidence either way


# Embeds that ALWAYS imply unit-level inventory exists somewhere — their
# presence forbids a no-data verdict (the units are behind the widget).
_UNIT_BEARING_EMBEDS: tuple[str, ...] = (
    "sightmap.com", "engrain", "sightmap/embed", "ysi.unitsList",
    "unitmap", "app/api/v1",
)

# STRUCTURAL DOM tokens that betray per-unit markup the extractor may have
# missed. If ANY appear with zero extracted units, we must not claim a
# publish-ceiling. Kept deliberately narrow to DOM/JSON markers — loose
# English like "available units" is EXCLUDED because it appears inside
# no-availability phrases ("no available units at this time") and would
# falsely fire the miss guard on a genuine empty-inventory marker.
_UNIT_VOCAB_TOKENS: tuple[str, ...] = (
    "availunitrow", "data-unit=", 'data-unit"', "unit-card", "unitcode",
    "apt #", "apartment #", "unit number",
)


@dataclass
class PublishCeilingResult:
    """Grade + auditable evidence for a publish-ceiling assessment."""

    verdict: PublishCeiling
    confidence: float                      # 0.0-1.0
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def gold_eligible(self) -> bool:
        """True only for evidence-backed publish-ceilings — never for a
        miss/render/uncertain grade. This is the gate the reporting layer
        reads to count a zero-unit property as gold."""
        return self.verdict in (
            PublishCeiling.CONFIRMED_NO_DATA,
            PublishCeiling.CONFIRMED_PLAN_ONLY,
        )


def _tiers_ran_and_empty(tier_trace: list[dict[str, Any]] | None) -> bool:
    """True when the extraction cascade actually RAN and each tier came back
    empty — as opposed to crashing, timing out, or being skipped. A
    publish-ceiling can only be asserted when the pipeline genuinely looked
    and found nothing, not when it never looked.
    """
    if not tier_trace:
        return False
    saw_a_real_attempt = False
    for t in tier_trace:
        outcome = str(t.get("outcome") or "").lower()
        # "ran_empty" / "skipped" are fine; a crash/timeout/error is not —
        # those mean we did not actually observe the page's contents.
        if outcome in ("error", "timeout", "crash", "exception", "bot_blocked"):
            return False
        if outcome in ("ran_empty", "ran_units", "ran"):
            saw_a_real_attempt = True
    return saw_a_real_attempt


def _has_any(haystack: str, needles: tuple[str, ...]) -> str | None:
    low = haystack.lower()
    for n in needles:
        if n in low:
            return n
    return None


def assess_publish_ceiling(
    *,
    units: list[Any] | None,
    plan_summaries: list[Any] | None,
    html_signals: dict[str, Any] | None,
    tier_trace: list[dict[str, Any]] | None,
    page_html: str | None,
    verified_plan_only_surface: bool = False,
) -> PublishCeilingResult:
    """Grade whether a zero-unit result is a genuine publish-ceiling.

    Args:
        units: final extracted unit list (should be empty/none for a ceiling).
        plan_summaries: plan-level rows extracted, if any.
        html_signals: the dict from ``_characterize_html`` (rent_signal_count,
            spa_confidence, framework_hints, ...). None → treated as unknown.
        tier_trace: list of ``{tier_key, outcome, reason}`` — the
            extract.tier_attempted events for this property.
        page_html: the fetched HTML (for marker / embed / unit-vocab checks).
        verified_plan_only_surface: True only when a bounded adapter proved it
            parsed the complete public plan-only surface represented by
            ``plan_summaries``.

    Returns:
        PublishCeilingResult with a grade, confidence, reason and an
        evidence bundle. Never raises.
    """
    units = units or []
    plan_summaries = plan_summaries or []
    sig = html_signals or {}
    html = page_html or ""
    rent_signals = int(sig.get("rent_signal_count", 0) or 0)
    spa_conf = float(sig.get("spa_confidence", 0.0) or 0.0)

    ev: dict[str, Any] = {
        "n_units": len(units),
        "n_plan_summaries": len(plan_summaries),
        "rent_signal_count": rent_signals,
        "spa_confidence": spa_conf,
        "tiers_ran_and_empty": _tiers_ran_and_empty(tier_trace),
        "verified_plan_only_surface": bool(verified_plan_only_surface),
    }

    # If units WERE extracted this is not a ceiling question at all.
    if units:
        return PublishCeilingResult(
            PublishCeiling.UNCERTAIN, 0.0,
            "units present — not a publish-ceiling case", ev
        )

    # A bounded adapter may opt in only after proving that it parsed the entire
    # public source and that the source is plan-only.  Rent tokens on that exact
    # surface describe the emitted plans, not missed apartments.  Unit-bearing
    # embeds/vocabulary still veto the proof; the unverified Madrid guard below
    # remains unchanged for every ordinary plan collection.
    embed = _has_any(html, _UNIT_BEARING_EMBEDS)
    vocab = _has_any(html, _UNIT_VOCAB_TOKENS)
    if verified_plan_only_surface and plan_summaries and not embed and not vocab:
        ev["guard"] = "verified_complete_plan_surface"
        return PublishCeilingResult(
            PublishCeiling.CONFIRMED_PLAN_ONLY,
            0.95,
            f"{len(plan_summaries)} plan rows from a verified complete plan-only "
            "surface; rent tokens are explained by those plans",
            ev,
        )

    # ── GUARD 1 (the madrid guard): rent tokens present but 0 units. ────────
    # The data is on the page; the extractor missed it. This is a fixable
    # miss, NOT a publish-ceiling — must never be graded gold.
    if rent_signals > 0:
        ev["guard"] = "rent_signals_present_zero_units"
        return PublishCeilingResult(
            PublishCeiling.EXTRACTION_MISS, 0.0,
            f"{rent_signals} rent tokens in HTML but 0 units extracted "
            "— extraction miss, not a publish-ceiling",
            ev,
        )

    # ── GUARD 2: a unit-bearing embed (SightMap/Engrain/ysi) is present. ────
    # Units exist behind the widget; a lazy-loaded embed that yielded nothing
    # is a render/probe miss, not proof of no data.
    if embed:
        ev["unit_bearing_embed"] = embed
        return PublishCeilingResult(
            PublishCeiling.NEEDS_RENDER, 0.0,
            f"unit-bearing embed present ({embed}) with 0 units — "
            "needs render/probe, cannot claim no-data",
            ev,
        )

    # ── GUARD 3: unit-vocab tokens in the DOM with 0 extracted units. ───────
    if vocab:
        ev["unit_vocab_token"] = vocab
        return PublishCeilingResult(
            PublishCeiling.EXTRACTION_MISS, 0.0,
            f"unit-vocab token '{vocab}' in DOM with 0 units — likely "
            "extraction miss, not a publish-ceiling",
            ev,
        )

    # ── GUARD 4: the cascade never actually ran the page (crash/timeout). ───
    if not ev["tiers_ran_and_empty"]:
        # Fall through to SPA/uncertain — we did not truly observe the page.
        if spa_conf >= 0.5:
            ev["guard"] = "spa_shell_unobserved"
            return PublishCeilingResult(
                PublishCeiling.NEEDS_RENDER, 0.0,
                f"SPA shell (spa_confidence={spa_conf}) and cascade did not "
                "fully run — needs render to verify",
                ev,
            )
        return PublishCeilingResult(
            PublishCeiling.UNCERTAIN, 0.0,
            "extraction cascade did not run to completion — cannot verify",
            ev,
        )

    # ── Positive evidence paths (cascade ran, no rent, no embed, no vocab) ──
    no_avail = detect_no_availability(html)
    empty_inv = is_empty_inventory_page(html) if not no_avail else False
    pre_leasing = is_pre_leasing_site(html)
    ev.update({
        "no_availability_marker": no_avail,
        "empty_inventory_page": empty_inv,
        "pre_leasing": pre_leasing,
    })

    # Plan-level published (plan rows exist, no units, and all the miss
    # guards passed) → confirmed plan-only.
    if plan_summaries:
        return PublishCeilingResult(
            PublishCeiling.CONFIRMED_PLAN_ONLY, 0.9,
            f"{len(plan_summaries)} plan rows, 0 units, no rent tokens / "
            "unit vocab / embed — operator publishes plan-level only",
            ev,
        )

    # No units, no plans, but an explicit operator signal that inventory is
    # genuinely empty / not-yet-leasing.
    if no_avail or empty_inv or pre_leasing:
        signal = (
            "no_availability_marker" if no_avail else
            "empty_inventory_page" if empty_inv else "pre_leasing"
        )
        return PublishCeilingResult(
            PublishCeiling.CONFIRMED_NO_DATA, 0.85,
            f"cascade ran empty; 0 rent tokens / unit vocab / embed; "
            f"operator signal '{signal}' — no unit-level data published",
            ev,
        )

    # Cascade ran, page is genuinely bare (no rent, no vocab, no embed, no
    # explicit marker). Weaker than an explicit marker → uncertain, not gold.
    return PublishCeilingResult(
        PublishCeiling.UNCERTAIN, 0.4,
        "cascade ran empty and page is bare, but no explicit no-availability "
        "signal — insufficient for a 100%-confidence no-data verdict",
        ev,
    )
