"""Audited property-boundary and current-marketing authority rules.

These rules are deliberately property-scoped.  A portfolio API can be valid,
well-formed and correctly bound to a property while still exposing a broader
catalogue than the units the operator currently publishes on its marketing
site.  We must not generalise that temporal policy to every Knock property.

The entries below are the multi-source exceptions confirmed in the completed
2026-08-03 4,982-property run and its fresh live-site verification.  They make
the normal marketing-page recovery path win; no unit IDs or expected counts are
hard-coded, so every run still reads the current public surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MarketingAuthorityRule:
    property_id: str
    property_name: str
    authority_provider: str
    reason: str = "backend_roster_broader_than_current_marketing_surface"


_RULES: dict[str, MarketingAuthorityRule] = {
    rule.property_id: rule
    for rule in (
        MarketingAuthorityRule("292744", "Chartwell Commons at Green Lea", "rentcafe"),
        MarketingAuthorityRule("285567", "SW 38th Avenue I", "rentcafe"),
        MarketingAuthorityRule("24584", "Westchase Apartments", "rentcafe"),
        MarketingAuthorityRule("10590", "Tides on Park Lane", "rentcafe"),
        MarketingAuthorityRule("34303", "Duke Manor", "sightmap"),
        MarketingAuthorityRule("74488", "St. Johns Wood", "sightmap"),
    )
}


def marketing_authority_rule(property_id: Any) -> MarketingAuthorityRule | None:
    """Return the audited current-marketing rule for one property, if any."""

    return _RULES.get(str(property_id or "").strip())


def knock_must_defer_to_current_marketing(property_id: Any) -> bool:
    """Whether a Knock roster must not terminate extraction for this property."""

    return marketing_authority_rule(property_id) is not None


def marketing_authority_error(property_id: Any) -> str:
    """Stable observable reason stamped when a broader roster is declined."""

    rule = marketing_authority_rule(property_id)
    if rule is None:
        return ""
    return (
        "CURRENT_MARKETING_AUTHORITY: "
        f"property_id={rule.property_id} provider={rule.authority_provider} "
        f"reason={rule.reason}"
    )
