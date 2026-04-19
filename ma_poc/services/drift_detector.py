"""
Drift detector — compares extraction results to profile expectations.

When drift is detected, demotes profile maturity so the next run triggers
a full extraction cascade rather than relying on learned shortcuts.

Phase: claude-scrapper-arch.md Step 3.2
"""
from __future__ import annotations

from models.scrape_profile import ProfileMaturity, ScrapeProfile


def detect_drift(
    profile: ScrapeProfile,
    units_extracted: int,
    scrape_result: dict,
) -> tuple[bool, list[str]]:
    """Compare extraction results to profile expectations.

    Returns:
        (drift_detected, reasons) tuple.
    """
    reasons: list[str] = []

    if profile.confidence.maturity == ProfileMaturity.COLD:
        return False, []  # No expectations to drift from

    expected = profile.confidence.last_unit_count

    # Unit count drop >30%
    if expected > 0 and units_extracted < expected * 0.7:
        reasons.append(f"unit_count_drop: expected ~{expected}, got {units_extracted}")

    # All rents null (extracted shells without data). Covers both v1
    # (market_rent_low/high, asking_rent, rent_range) and v2 (rent_low/high)
    # — a v2 success was otherwise spuriously demoted to COLD because the
    # updater promotes COLD→WARM and drift then saw zero recognized rents.
    units = scrape_result.get("units", [])
    _rent_keys = (
        "rent_range", "market_rent_low", "market_rent_high",
        "asking_rent", "rent_low", "rent_high",
    )
    if units_extracted > 0 and units:
        null_rents = sum(
            1 for u in units if not any(u.get(k) for k in _rent_keys)
        )
        if null_rents == len(units):
            reasons.append(
                f"all_rents_null: {null_rents}/{len(units)} units have no rent data"
            )

    # Scrape timeout pattern
    if scrape_result.get("_timeout"):
        if profile.confidence.consecutive_failures >= 2:
            reasons.append("timeout_pattern: 3+ consecutive timeouts")

    return len(reasons) > 0, reasons


def apply_drift_demotion(
    profile: ScrapeProfile, reasons: list[str]
) -> ScrapeProfile:
    """Demote profile maturity based on drift signals."""
    severe = any("all_rents_null" in r or "timeout_pattern" in r for r in reasons)

    if severe:
        profile.confidence.maturity = ProfileMaturity.COLD
        profile.confidence.consecutive_successes = 0
    elif profile.confidence.maturity == ProfileMaturity.HOT:
        profile.confidence.maturity = ProfileMaturity.WARM
        profile.confidence.consecutive_successes = 0

    return profile
