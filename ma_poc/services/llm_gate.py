"""
LLM escalation gate — Change 5.

Decides whether a Tier-4 LLM extraction call is worth the spend. Pre-Change-5
the generic adapter always ran the LLM when deterministic tiers failed, which
burned $0.94 across 13 TIER_1_API properties on the 04-20 run whose bodies
carried nothing the LLM could rescue.

Policy (documented in claude_adapter_fixes.md §Change 5):
- ``LLM_GATE_NO_BODY`` — page body too short or missing rent tokens → skip.
- ``LLM_GATE_TIER1_SHAPE_MATCHED`` — Tier-1 adapter captured a shape-matched
  response but parsed to zero units → escalate (LLM can sometimes salvage).
- ``LLM_GATE_DETERMINISTIC_TIERS_SUFFICED`` — Tier-2 (JSON-LD) or Tier-3
  (DOM) already produced units → skip (LLM not needed).
- ``LLM_GATE_FALLBACK_JUSTIFIED`` — meaningful body, no deterministic tier
  found anything → escalate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# --- Public reason codes ----------------------------------------------------

LLM_GATE_NO_BODY = "LLM_GATE_NO_BODY"
LLM_GATE_TIER1_SHAPE_MATCHED = "LLM_GATE_TIER1_SHAPE_MATCHED"
LLM_GATE_DETERMINISTIC_TIERS_SUFFICED = "LLM_GATE_DETERMINISTIC_TIERS_SUFFICED"
LLM_GATE_FALLBACK_JUSTIFIED = "LLM_GATE_FALLBACK_JUSTIFIED"


# --- Tuning constants -------------------------------------------------------

# Body-size threshold below which the page is presumed too thin for the LLM
# to recover anything. 1KB chosen because the 04-20 analysis of wasted calls
# showed all 13 skipped-candidates had bodies well under 1KB after script
# stripping (e.g. plain "site temporarily unavailable" stubs).
_MIN_BODY_BYTES = 1024

# Tokens that indicate the page is at least about rentals. At least one must
# appear (case-insensitive) for the body to count as "meaningful".
_RENT_TOKENS: tuple[str, ...] = (
    "rent",
    "apartment",
    "floor",
    "bed",
    "studio",
    "lease",
    "available",
)

_RENT_TOKEN_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _RENT_TOKENS) + r")\b",
    re.IGNORECASE,
)


# --- Duck-typed inputs ------------------------------------------------------


def _api_responses_of(tier1_result: Any) -> list[dict[str, Any]]:
    """Return the ``api_responses`` list from *tier1_result* regardless of
    whether it's an ``AdapterResult`` dataclass, a dict, or None.

    Duck-typing lets callers from the orchestrator (which holds the real
    dataclass) and the generic adapter's Tier-4 entry point (which holds a
    proxy dict) share the same gate without an adapter import cycle.
    """
    if tier1_result is None:
        return []
    if isinstance(tier1_result, dict):
        resp = tier1_result.get("api_responses")
    else:
        resp = getattr(tier1_result, "api_responses", None)
    if not isinstance(resp, list):
        return []
    return resp


# --- Body helper ------------------------------------------------------------


def page_has_meaningful_body(html: str | None) -> bool:
    """True if *html* is at least 1KB and contains at least one rent token.

    Intentionally cheap — no lxml, no BeautifulSoup. The LLM gate runs on the
    happy-path of every extract() so anything expensive here would be paid
    on every property.
    """
    if not html or not isinstance(html, str):
        return False
    try:
        byte_len = len(html.encode("utf-8", errors="ignore"))
    except Exception:
        return False
    if byte_len < _MIN_BODY_BYTES:
        return False
    return bool(_RENT_TOKEN_RE.search(html))


# --- Return type ------------------------------------------------------------


@dataclass
class EscalationDecision:
    """Result of ``should_escalate_to_llm``.

    ``escalate`` drives the actual branch; ``reason`` is the machine-readable
    code stamped on ``AdapterResult.tier_used`` or appended to ``errors``.
    """

    escalate: bool
    reason: str


def should_escalate_to_llm(
    *,
    html: str | None,
    tier1_result: Any,
    tier2_units: list[Any] | None = None,
    tier3_units: list[Any] | None = None,
) -> EscalationDecision:
    """Gate the Tier-4 LLM call. See module docstring for policy.

    ``tier1_result`` may be an ``AdapterResult`` dataclass or a dict (duck
    typing). Only its ``api_responses`` field is consulted — the gate
    intentionally does not care whether the Tier-1 adapter parsed anything;
    Tier-1 shape-matched bodies earn an LLM retry even when units is empty.
    """
    if not page_has_meaningful_body(html):
        return EscalationDecision(
            escalate=False,
            reason=f"{LLM_GATE_NO_BODY}: page body too short or no rent tokens",
        )

    if _api_responses_of(tier1_result):
        return EscalationDecision(
            escalate=True,
            reason=(
                f"{LLM_GATE_TIER1_SHAPE_MATCHED}: tier-1 captured ≥1 "
                "shape-matched response; LLM may recover units"
            ),
        )

    if tier2_units or tier3_units:
        return EscalationDecision(
            escalate=False,
            reason=(f"{LLM_GATE_DETERMINISTIC_TIERS_SUFFICED}: tier-2/3 produced units; LLM not needed"),
        )

    return EscalationDecision(
        escalate=True,
        reason=(f"{LLM_GATE_FALLBACK_JUSTIFIED}: meaningful body, no deterministic tier produced units"),
    )
