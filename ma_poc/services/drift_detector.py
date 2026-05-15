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

    # All rents null (extracted units with no rent data). Covers both v1
    # (market_rent_low/high, asking_rent, rent_range) and v2 (rent_low/high)
    # — a v2 success was otherwise spuriously demoted to COLD because the
    # updater promotes COLD→WARM and drift then saw zero recognized rents.
    #
    # 2026-05-16: relaxed from "severe" → "soft". A property whose floor-
    # plans carry beds/baths/sqft but no rent (LEASE_UP, "Call for
    # Pricing", syndication-only sites, JSON-LD ApartmentComplex with no
    # Offer arrays, AMLI-style index pages where pricing lives on per-
    # plan sub-pages) was being demoted to COLD on every run despite
    # legitimately extracted floor-plan data. The demotion wiped out
    # winning_page_url + dom_hints + llm_field_mappings, forcing a full
    # rediscovery on the next run — exactly the opposite of what the
    # self-learning loop is supposed to do for stable properties. The
    # reason is still recorded for telemetry but `severe` no longer
    # triggers on it (see apply_drift_demotion).
    units = scrape_result.get("units", [])
    _rent_keys = (
        "rent_range",
        "market_rent_low",
        "market_rent_high",
        "asking_rent",
        "rent_low",
        "rent_high",
    )
    if units_extracted > 0 and units:
        null_rents = sum(1 for u in units if not any(u.get(k) for k in _rent_keys))
        if null_rents == len(units):
            reasons.append(f"all_rents_null: {null_rents}/{len(units)} units have no rent data")

    # Scrape timeout pattern
    if scrape_result.get("_timeout"):
        if profile.confidence.consecutive_failures >= 2:
            reasons.append("timeout_pattern: 3+ consecutive timeouts")

    return len(reasons) > 0, reasons


def apply_drift_demotion(profile: ScrapeProfile, reasons: list[str]) -> ScrapeProfile:
    """Demote profile maturity based on drift signals.

    2026-05-16: `all_rents_null` removed from the severe-demotion list
    (it now drops one step HOT→WARM via the soft branch below, instead
    of slamming straight to COLD). The reason is still surfaced for
    telemetry but no longer wipes out winning_page_url / dom_hints /
    llm_field_mappings on properties that legitimately have floor-plan
    data without rent. Only `timeout_pattern` (3+ consecutive timeouts)
    remains severe — that signal is unambiguous: the property isn't
    extracting, period.
    """
    severe = any("timeout_pattern" in r for r in reasons)

    # Resolve the enum class from the instance's own schema rather than the
    # module-level ``ProfileMaturity`` import — the codebase has both
    # ``models.scrape_profile`` and ``ma_poc.models.scrape_profile`` reachable
    # on sys.path, each with its own ``ProfileMaturity`` class object.
    # Assigning the "wrong" class trips ``PydanticSerializationUnexpectedValue``
    # warnings on the next profile save; using the instance-bound class
    # guarantees serializer/value class agreement. Comparison (``==``) is
    # value-based on StrEnum so existing == checks remain correct unchanged.
    _Maturity = type(profile.confidence.maturity)
    if severe:
        profile.confidence.maturity = _Maturity.COLD
        profile.confidence.consecutive_successes = 0
    elif profile.confidence.maturity == ProfileMaturity.HOT:
        profile.confidence.maturity = _Maturity.WARM
        profile.confidence.consecutive_successes = 0

    return profile
