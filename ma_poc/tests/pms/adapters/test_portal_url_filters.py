"""Tests for the 2026-05-17 RC4c/RC4d portal URL filters.

RC4c — additions to ``_PORTAL_INFRA_BLACKLIST`` that prevent the
iframe scanner from queuing analytics, feedback widgets, and webchat
clients as candidate "unknown portals". Each of these contributed
hundreds of wasted hop fetches on PIDs 6997 / 229374 / 231970 in the
May 17 cloud run.

RC4d — ``_is_portal_auth_path`` rejects sign-in / login / signout /
auth-callback paths even when the URL host matches an allow-listed
leasing portal. Without it, a property's "Resident Portal" CTA pointing
at ``myresman.com/Portal/Access/SignIn/WT`` gets queued at score 10_115
and the auth-wall body is fed to the LLM.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters._html_extract import (
    _PORTAL_INFRA_BLACKLIST,
    _is_portal_auth_path,
    _is_portal_infra_blacklisted,
    detect_embedded_portal_urls,
    sweep_raw_html_for_portal_urls,
)


# ── RC4c: portal-infra blacklist additions ──────────────────────────────────


class TestPortalInfraBlacklistRC4cAdditions:
    """Each host added on 2026-05-17 must blacklist correctly. If a
    future blacklist refactor accidentally drops one, this test fails
    with the specific PID that the host was wasting hops on."""

    @pytest.mark.parametrize(
        ("host_url", "regressed_pids"),
        [
            ("https://www.leavefeedback.app/survey/2n0zjWw", "PID 231970"),
            ("https://gum.criteo.com/sync?h=...", "PIDs 6997/229374"),
            ("https://connect.facebook.net/en_US/fbevents.js", "PID 229374"),
            ("https://www.analytics.sitewit.com/track", "general"),
            ("https://rp-webchat-client.netlify.app/widget.js", "general"),
            ("https://integrations.nestio.com/widget.js", "general"),
            ("https://chat.example.com/webchat-client/loader.js", "general"),
        ],
    )
    def test_host_is_blacklisted(self, host_url, regressed_pids):
        assert _is_portal_infra_blacklisted(host_url) is True, (
            f"{host_url!r} no longer on _PORTAL_INFRA_BLACKLIST — "
            f"re-opens the May 17 regression for {regressed_pids}."
        )

    def test_legitimate_leasing_portal_not_blacklisted(self):
        """Guard against over-blacklisting: real portals must still pass
        the gate so the link-hop can fetch them."""
        legitimate = [
            "https://propertyslug.appfolio.com/listings",
            "https://8929323.onlineleasing.realpage.com/floorplans",
            "https://my.hy.ly/api/properties/123/floorplans",
            "https://sightmap.com/embed/abc123",
            "https://propertyslug.securecafe.com/onlineleasing/floorplans",
        ]
        for url in legitimate:
            assert _is_portal_infra_blacklisted(url) is False, (
                f"{url!r} incorrectly blacklisted — would block "
                "legitimate inventory iframes from being fetched."
            )


# ── RC4c: portal_candidates URL dedup ───────────────────────────────────────


def test_detect_embedded_portal_urls_dedups_same_url():
    """When ``embedded_portal_hints`` contains the same URL multiple
    times (iframe scanner found it twice in the same HTML), the
    portal_candidates list must only queue it once."""
    blobs = [
        {
            "url": "https://example.com/",
            "body": {
                "first_iframe": "https://8929323.onlineleasing.realpage.com/",
                "second_iframe": "https://8929323.onlineleasing.realpage.com/",
                "nested": {
                    "duplicate_again": "https://8929323.onlineleasing.realpage.com/",
                },
            },
        }
    ]
    hints = detect_embedded_portal_urls(blobs)
    urls = [h[0] for h in hints]
    assert urls.count("https://8929323.onlineleasing.realpage.com/") == 1, (
        f"Same URL admitted {urls.count('https://8929323.onlineleasing.realpage.com/')} "
        "times — caller would queue it that many times before downstream "
        "URL-dedup removes the spares. Per-PID candidate-count telemetry "
        "becomes hard to read."
    )


# ── RC4d: portal auth-path filter ───────────────────────────────────────────


class TestIsPortalAuthPath:
    """``_is_portal_auth_path`` must reject auth/sign-in routes (path-
    segment match) while admitting legitimate inventory routes that
    share substring overlap with auth keywords."""

    @pytest.mark.parametrize(
        "auth_url",
        [
            "https://urbanresidential.myresman.com/Portal/Access/SignIn/WT",  # PID 6997
            "https://urbanresidential.myresman.com/Portal/Access/SignIn",
            "https://propertyslug.appfolio.com/connect/users/sign_in",
            "https://propertyslug.appfolio.com/connect/users/oauth/callback",
            "https://example.rentcafe.com/login",
            "https://example.rentcafe.com/auth/callback",
            "https://example.rentcafe.com/logout",
            "https://example.com/oauth2/authorize",
            "https://example.com/register",
            "https://example.com/forgot-password",
        ],
    )
    def test_auth_paths_rejected(self, auth_url):
        assert _is_portal_auth_path(auth_url) is True, (
            f"_is_portal_auth_path({auth_url!r}) returned False — auth-wall "
            "URLs on allow-listed portal hosts will be queued at score "
            "10_000+ and consume hop slots on 0-unit auth shells."
        )

    @pytest.mark.parametrize(
        "inventory_url",
        [
            # Legitimate inventory URLs whose path contains an auth
            # keyword as a SUBSTRING but not as a segment.
            "https://example.com/floorplans/the-signature-suite",   # `sign` substring
            "https://example.com/apartments/login-square-park",     # `login` substring
            "https://example.com/availability/the-register-room",   # `register` substring
            "https://example.com/floorplans/oauth-loft",            # `oauth` substring
            # Standard inventory routes
            "https://example.com/floorplans",
            "https://example.com/availability",
            "https://propertyslug.appfolio.com/listings",
            "https://8929323.onlineleasing.realpage.com/floorplans",
            "https://my.hy.ly/api/properties/123/floorplans",
        ],
    )
    def test_inventory_paths_admitted(self, inventory_url):
        assert _is_portal_auth_path(inventory_url) is False, (
            f"_is_portal_auth_path({inventory_url!r}) returned True — "
            "legitimate inventory URL incorrectly classified as auth. "
            "The path-segment boundary check is what distinguishes "
            "auth keywords from substrings inside slugs."
        )

    def test_empty_and_none_inputs_safe(self):
        assert _is_portal_auth_path("") is False
        assert _is_portal_auth_path(None) is False  # type: ignore[arg-type]


# ── RC4d: integration through detect_embedded_portal_urls ───────────────────


def test_detect_embedded_portal_urls_rejects_auth_path_on_allowed_host():
    """End-to-end: a SignIn URL on an allow-listed ResMan host must NOT
    appear in the output of ``detect_embedded_portal_urls``. Pre-RC4d
    the host match alone admitted it."""
    blobs = [
        {
            "url": "https://example.com/",
            "body": {
                "resident_portal_cta": (
                    "https://urbanresidential.myresman.com/Portal/Access/SignIn/WT"
                ),
                # A legitimate inventory URL on the same host — must be ADMITTED.
                "applicant_portal": (
                    "https://urbanresidential.myresman.com/portal/applicants"
                ),
            },
        }
    ]
    hints = detect_embedded_portal_urls(blobs)
    urls = [h[0] for h in hints]
    assert not any("SignIn" in u for u in urls), (
        f"SignIn URL admitted to portal candidates: {urls}. The auth-path "
        "filter must reject it even though the host matches the resman "
        "allow-list."
    )
    assert any("portal/applicants" in u for u in urls), (
        f"Legitimate applicant-portal URL was incorrectly filtered out: {urls}. "
        "The auth-path filter is over-rejecting."
    )


def test_sweep_raw_html_rejects_auth_path_on_allowed_host():
    """Symmetric to the embedded-JSON walker: the raw-HTML sweep must
    also apply the auth-path filter so the same URL is treated
    identically regardless of which discovery path found it."""
    html = """
    <html>
    <body>
    <a href="https://urbanresidential.myresman.com/Portal/Access/SignIn/WT">Resident Login</a>
    <a href="https://urbanresidential.myresman.com/portal/applicants">Apply Now</a>
    </body>
    </html>
    """
    hints = sweep_raw_html_for_portal_urls(html)
    urls = [h[0] for h in hints]
    assert not any("SignIn" in u for u in urls), (
        f"SignIn URL admitted via sweep_raw_html_for_portal_urls: {urls}. "
        "RC4d filter must be applied symmetrically across both portal-"
        "discovery entry points."
    )
    assert any("portal/applicants" in u for u in urls)
