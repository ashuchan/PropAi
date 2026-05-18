"""Detect vendor-lockout / parked-domain HTTP-200 stub pages.

Some properties' websites are administratively shut down (non-payment, domain
expiration, account suspension, parking) but still return HTTP 200 with a
small landlord-vendor stub page. Today these flow through L1 fetch as
``outcome=OK``, hit the LLM_GATE_NO_BODY gate (body < 1024 bytes or no rent
tokens), and emit ``FAILED_NO_DATA`` — the wrong terminal verdict. They
should be classified as ``DEAD_URL`` so they're excluded from the success-
rate denominator and routed to the re-discovery queue instead of the
extraction-bug triage queue.

Canonical case (2026-05-18): PID 33993 cityclubapartments.com redirects to
``https://nonpayment.spherexx.com/`` (962-byte HTML titled "Site Taken Down
(Non-Payment) - Web hosting provided by Spherexx.com"). 8 such properties
on the 2026-05-18 cloud run; cumulative across runs is much higher.

The patterns below cover the most common vendor-lockout & parking signals
observed in production. Add new patterns when triage surfaces them.
"""

from __future__ import annotations

import re
from typing import Final


# Substrings checked case-insensitively against the *body* of the entry
# fetch. Each entry is paired with a short label that ends up in the
# verdict.reason field — keep them human-skim-friendly.
#
# The list is intentionally specific. We avoid bare words like "suspended"
# alone because real apartment marketing copy frequently contains them
# ("services suspended on holidays", "rent suspended during construction").
# Patterns that DO appear here are either domains (no false-positive risk)
# or multi-word phrases that don't naturally occur in apartment marketing.
_LOCKOUT_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    # Spherexx — multifamily-marketing vendor; redirects offboarded clients
    # to nonpayment.spherexx.com. Canonical pattern (PID 33993 2026-05-18).
    ("nonpayment.spherexx.com", "spherexx_nonpayment"),
    ("site taken down (non-payment)", "spherexx_nonpayment"),
    # Bluehost shared hosting — domains parked for non-payment land here.
    ("account-suspended.bluehost.com", "bluehost_account_suspended"),
    # Common generic vendor takedown phrases (multi-word so they don't
    # collide with normal marketing copy).
    ("this account has been suspended", "hosting_account_suspended"),
    ("this site has been suspended", "hosting_account_suspended"),
    ("website has been disabled", "hosting_site_disabled"),
    ("website is temporarily unavailable due to non-payment", "hosting_nonpayment"),
    ("domain has expired", "domain_expired"),
    ("this domain is for sale", "domain_for_sale"),
    # Parking pages from common registrars — keep the FQDN to avoid matching
    # marketing copy that happens to mention 'godaddy' / 'sedo' generically.
    ("parking.godaddy.com", "godaddy_parked"),
    ("park.gandi.net", "gandi_parked"),
    ("sedoparking.com", "sedo_parked"),
    ("afternic.com/forsale", "afternic_parked"),
    ("hugedomains.com/domain_profile", "hugedomains_parked"),
)


def detect_vendor_lockout(
    body: bytes | str | None,
    *,
    final_url: str | None = None,
) -> tuple[bool, str | None]:
    """Return ``(is_lockout, label)`` for an entry-page body.

    Cheap pure function — caller passes the bytes already fetched by L1.
    Returns ``(False, None)`` for any non-matching body. Tolerates ``None``
    and decode errors so callers don't have to guard.

    Args:
        body: Raw response body (bytes preferred; str also accepted).
        final_url: Post-redirect URL when known. Some lockout vendors are
            recognisable by the redirect target alone (the body might be
            zero bytes if the vendor responded with a bare redirect). Pass
            ``fetch_result.final_url`` here when available; pass ``None``
            otherwise.

    Returns:
        ``(True, "<label>")`` when a known vendor-lockout pattern matches.
        Use the label as ``VerdictResult.reason`` to keep triage greppable.
        ``(False, None)`` otherwise.
    """
    # First-pass: check the final_url itself (cheap, catches redirect-to-
    # vendor cases regardless of body content).
    if final_url:
        url_lower = final_url.lower()
        for pat, label in _LOCKOUT_PATTERNS:
            # Only URL-shape patterns (contain '.' or '/') make sense here;
            # phrase patterns would never match a URL.
            if ("." in pat or "/" in pat) and pat in url_lower:
                return True, f"vendor_lockout:{label}"

    # Second-pass: scan the body. Decode best-effort.
    if body is None:
        return False, None
    if isinstance(body, bytes):
        try:
            text = body.decode("utf-8", errors="ignore")
        except Exception:
            return False, None
    else:
        text = body
    body_lower = text.lower()
    for pat, label in _LOCKOUT_PATTERNS:
        if pat in body_lower:
            return True, f"vendor_lockout:{label}"
    return False, None


# Compiled-once form for callers that want to do their own scanning
# without re-walking the pattern list — e.g. a downstream observer that
# logs all matches.
_LOCKOUT_OR_PATTERN: Final[re.Pattern[str]] = re.compile(
    "|".join(re.escape(p) for p, _ in _LOCKOUT_PATTERNS),
    re.IGNORECASE,
)


def lockout_pattern() -> re.Pattern[str]:
    """Compiled OR-regex covering every vendor-lockout pattern. Exposed for
    observers / analytics paths that want to scan bodies independently."""
    return _LOCKOUT_OR_PATTERN
