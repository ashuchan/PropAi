"""Decides when to escalate a property's tier after a fetch result.

Rules:
  - 403 Forbidden                → escalate once
  - 429 Too Many Requests        → escalate once
  - 407 Proxy Authentication     → do NOT escalate (our problem)
  - CAPTCHA / Cloudflare signal  → escalate once
  - Timeout                      → accumulate; escalate at 3 consecutive
  - 2xx / 3xx                    → reset fail streak; no escalation
  - After 30 consecutive successes, try one tier lower
"""

from __future__ import annotations

from dataclasses import dataclass

from ma_poc.fetch.proxy.base import ProxyTier

ESCALATE_IMMEDIATELY_ON_STATUS: frozenset[int] = frozenset({403, 429})
ESCALATE_IMMEDIATELY_ON_SIGNAL: frozenset[str] = frozenset(
    {"captcha", "cloudflare_challenge"}
)
TIMEOUT_FAIL_STREAK_THRESHOLD: int = 3
DEESCALATE_SUCCESS_STREAK: int = 30


@dataclass(frozen=True)
class EscalationDecision:
    new_tier: ProxyTier
    new_fail_count: int
    new_success_count: int
    reason: str


_TIER_ORDER: tuple[ProxyTier, ...] = (
    ProxyTier.DIRECT,
    ProxyTier.DATACENTER,
    ProxyTier.RESIDENTIAL,
    ProxyTier.UNBLOCKER,
)


def _tier_below(tier: ProxyTier) -> ProxyTier | None:
    idx = _TIER_ORDER.index(tier)
    return _TIER_ORDER[idx - 1] if idx > 0 else None


def decide(
    *,
    current_tier: ProxyTier,
    fail_count: int,
    success_count: int,
    fetch_status: int | None,
    fetch_signal: str | None,
    is_timeout: bool,
) -> EscalationDecision:
    """Compute the next tier and counters given a fetch outcome.

    Success is ``2xx/3xx`` AND not a timeout. Anything else is a failure of
    some stripe; the status/signal/timeout flags disambiguate the kind.
    """
    # Success path
    if (
        fetch_status is not None
        and 200 <= fetch_status < 400
        and not is_timeout
    ):
        new_success = success_count + 1
        if new_success >= DEESCALATE_SUCCESS_STREAK:
            lower = _tier_below(current_tier)
            if lower is not None:
                return EscalationDecision(
                    new_tier=lower,
                    new_fail_count=0,
                    new_success_count=0,
                    reason=(
                        f"deescalated after {new_success} successes "
                        f"at {current_tier.value}"
                    ),
                )
        return EscalationDecision(
            new_tier=current_tier,
            new_fail_count=0,
            new_success_count=new_success,
            reason="success — counter incremented",
        )

    # Immediate-escalation signals
    if (
        fetch_status in ESCALATE_IMMEDIATELY_ON_STATUS
        or (fetch_signal or "") in ESCALATE_IMMEDIATELY_ON_SIGNAL
    ):
        higher = current_tier.next_tier()
        if higher is None:
            return EscalationDecision(
                new_tier=current_tier,
                new_fail_count=fail_count + 1,
                new_success_count=0,
                reason=f"failure at top tier {current_tier.value}; sticking",
            )
        trigger = fetch_status if fetch_status is not None else fetch_signal
        return EscalationDecision(
            new_tier=higher,
            new_fail_count=0,
            new_success_count=0,
            reason=(
                f"escalated from {current_tier.value} to {higher.value} "
                f"on {trigger}"
            ),
        )

    # Timeout — accumulate, escalate at threshold
    if is_timeout:
        new_fail = fail_count + 1
        if new_fail >= TIMEOUT_FAIL_STREAK_THRESHOLD:
            higher = current_tier.next_tier()
            if higher is not None:
                return EscalationDecision(
                    new_tier=higher,
                    new_fail_count=0,
                    new_success_count=0,
                    reason=f"escalated on timeout streak ({new_fail})",
                )
        return EscalationDecision(
            new_tier=current_tier,
            new_fail_count=new_fail,
            new_success_count=0,
            reason=f"timeout — fail streak {new_fail}",
        )

    # Other 4xx/5xx — increment but don't escalate yet
    return EscalationDecision(
        new_tier=current_tier,
        new_fail_count=fail_count + 1,
        new_success_count=0,
        reason=f"failure — status {fetch_status}",
    )
