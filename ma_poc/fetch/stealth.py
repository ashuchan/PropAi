"""Stealth identity pool — curated browser fingerprints for anti-bot evasion.

Only real Chrome/Firefox/Edge/Safari UA strings. No LLM-generated strings.
Sticky keys ensure the same property sees the same browser across runs.

2026-05-12: Updated Chrome identities from 122-124 to 134-136 range.
Chrome 124 (April 2024) was ~2 years stale; modern bot-detection checks
UA recency. Edge updated from 123→136. Firefox updated from 125→137.
Safari 17.4 kept (Safari releases are much slower).

Also added `timezone_id` field so BrowserContextPool can inject a plausible
US timezone into each page context — prevents the server UTC timezone from
leaking via JS `Intl.DateTimeFormat().resolvedOptions().timeZone`.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Identity:
    """A browser identity used for stealth.

    Existing fields are unchanged (H7). New fields (S3) populate client-hint
    headers consistent with the browser family and major version.
    """

    user_agent: str
    accept_language: str
    platform: str
    viewport: tuple[int, int]
    # S3 additions — additive only; existing field names/positions are not modified.
    browser_family: str = "chrome"   # "chrome" | "firefox" | "edge" | "safari"
    browser_major: int = 136
    # Sec-CH-UA* headers — None for Firefox/Safari (they don't send Client Hints).
    sec_ch_ua: str | None = None
    sec_ch_ua_mobile: str = "?0"
    sec_ch_ua_platform: str | None = None
    sec_ch_ua_platform_version: str | None = None
    # Timezone for Playwright context injection. Prevents server UTC from leaking
    # via navigator.languages / Intl API when Playwright runs on a UTC host.
    # US Eastern/Central covers >60% of apartment properties by volume.
    timezone_id: str = "America/New_York"


# Curated list of real browser identities — Chrome, Firefox, Edge on Windows/Mac/Linux.
# Chrome 136 (April 2026 stable), Edge 136, Firefox 137, Safari 17.4.
# Sec-CH-UA format for Chrome 126+: "Google Chrome";v="N", "Chromium";v="N", "Not A;Brand";v="8"
# Note: "Not-A.Brand" changed to "Not A;Brand" and v changed from "99" to "8" in Chrome 126+.
_IDENTITIES: list[Identity] = [
    # 0 — Chrome 136 on Windows 10 (1920×1080) — Eastern US
    Identity(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        ),
        accept_language="en-US,en;q=0.9",
        platform="Windows",
        viewport=(1920, 1080),
        browser_family="chrome",
        browser_major=136,
        sec_ch_ua='"Google Chrome";v="136", "Chromium";v="136", "Not A;Brand";v="8"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"Windows"',
        sec_ch_ua_platform_version='"15.0.0"',
        timezone_id="America/New_York",
    ),
    # 1 — Chrome 136 on macOS (1440×900) — Pacific US
    Identity(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        ),
        accept_language="en-US,en;q=0.9",
        platform="macOS",
        viewport=(1440, 900),
        browser_family="chrome",
        browser_major=136,
        sec_ch_ua='"Google Chrome";v="136", "Chromium";v="136", "Not A;Brand";v="8"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"macOS"',
        sec_ch_ua_platform_version='"14.4.1"',
        timezone_id="America/Los_Angeles",
    ),
    # 2 — Firefox 137 on Windows 10 (1920×1080) — Central US
    # Firefox does not send Sec-CH-UA headers (Mozilla opted out of Client Hints).
    Identity(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) "
            "Gecko/20100101 Firefox/137.0"
        ),
        accept_language="en-US,en;q=0.5",
        platform="Windows",
        viewport=(1920, 1080),
        browser_family="firefox",
        browser_major=137,
        sec_ch_ua=None,
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform=None,
        sec_ch_ua_platform_version=None,
        timezone_id="America/Chicago",
    ),
    # 3 — Chrome 135 on Linux (1920×1080) — Central US
    # One version behind for diversity; still recent enough not to flag.
    Identity(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        ),
        accept_language="en-US,en;q=0.9",
        platform="Linux",
        viewport=(1920, 1080),
        browser_family="chrome",
        browser_major=135,
        sec_ch_ua='"Google Chrome";v="135", "Chromium";v="135", "Not A;Brand";v="8"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"Linux"',
        # Linux Chrome sends empty platform version — this is correct.
        sec_ch_ua_platform_version='""',
        timezone_id="America/Chicago",
    ),
    # 4 — Edge 136 on Windows 10 (1920×1080) — Eastern US
    Identity(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0"
        ),
        accept_language="en-US,en;q=0.9",
        platform="Windows",
        viewport=(1920, 1080),
        browser_family="edge",
        browser_major=136,
        sec_ch_ua='"Microsoft Edge";v="136", "Chromium";v="136", "Not A;Brand";v="8"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"Windows"',
        sec_ch_ua_platform_version='"15.0.0"',
        timezone_id="America/New_York",
    ),
    # 5 — Safari 17.4 on macOS (1440×900) — Pacific US
    # Safari does not send Sec-CH-UA headers.
    Identity(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
        ),
        accept_language="en-US,en;q=0.9",
        platform="macOS",
        viewport=(1440, 900),
        browser_family="safari",
        browser_major=17,
        sec_ch_ua=None,
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform=None,
        sec_ch_ua_platform_version=None,
        timezone_id="America/Los_Angeles",
    ),
    # 6 — Chrome 134 on Windows 10 (1366×768) — Mountain US
    # Two versions behind for diversity; low-end viewport for variety.
    Identity(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
        ),
        accept_language="en-US,en;q=0.9",
        platform="Windows",
        viewport=(1366, 768),
        browser_family="chrome",
        browser_major=134,
        sec_ch_ua='"Google Chrome";v="134", "Chromium";v="134", "Not A;Brand";v="8"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"Windows"',
        sec_ch_ua_platform_version='"15.0.0"',
        timezone_id="America/Denver",
    ),
    # 7 — Chrome 135 on macOS (2560×1440) — Pacific US
    # One version behind to ensure identity 7 has a distinct UA from identity 1.
    # Chrome on macOS always reports 10_15_7 in the UA regardless of actual OS
    # version — using a different Chrome version gives us UA uniqueness.
    Identity(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        ),
        accept_language="en-US,en;q=0.9",
        platform="macOS",
        viewport=(2560, 1440),
        browser_family="chrome",
        browser_major=135,
        sec_ch_ua='"Google Chrome";v="135", "Chromium";v="135", "Not A;Brand";v="8"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"macOS"',
        sec_ch_ua_platform_version='"14.4.1"',
        timezone_id="America/Los_Angeles",
    ),
]


class IdentityPool:
    """Rotates through curated browser identities.

    Uses deterministic hashing for sticky key support so the same property
    sees the same browser identity across runs.
    """

    def __init__(self, identities: list[Identity] | None = None) -> None:
        """Initialise the identity pool.

        Args:
            identities: Custom identity list. Uses built-in list if None.
        """
        self._identities = identities or list(_IDENTITIES)
        self._rotations: dict[str, int] = {}

    def pick(self, sticky_key: str | None = None) -> Identity:
        """Select an identity, deterministically if a sticky key is provided.

        Args:
            sticky_key: Typically property_id. Same key always returns the same identity.

        Returns:
            An Identity to use for the request.
        """
        if sticky_key is None:
            return self._identities[0]
        rotation = self._rotations.get(sticky_key, 0)
        idx = (self._hash_key(sticky_key) + rotation) % len(self._identities)
        return self._identities[idx]

    def pick_chrome_only(self, sticky_key: str) -> Identity:
        """Like pick(), but restricted to Chrome/Edge family for proxy-tier
        use where curl_cffi ships a Chrome JA3 fingerprint.

        Edge is included because Edge is Chromium-based and shares the JA3.
        """
        chrome_ids = [i for i in self._identities if i.browser_family in ("chrome", "edge")]
        if not chrome_ids:
            return self._identities[0]
        rotation = self._rotations.get(sticky_key, 0)
        idx = (self._hash_key(sticky_key) + rotation) % len(chrome_ids)
        return chrome_ids[idx]

    def rotate(self, sticky_key: str) -> None:
        """Rotate to a different identity for the given key.

        Args:
            sticky_key: The key (typically property_id) to rotate.
        """
        self._rotations[sticky_key] = self._rotations.get(sticky_key, 0) + 1
        log.info("Rotated identity for %s (rotation=%d)", sticky_key, self._rotations[sticky_key])

    def current_slot(self, sticky_key: str) -> int:
        """Index of the identity currently selected for this key (post-rotation).

        Args:
            sticky_key: The key (typically property_id).

        Returns:
            Integer slot index in [0, len(identities)).
        """
        rotation = self._rotations.get(sticky_key, 0)
        return (self._hash_key(sticky_key) + rotation) % len(self._identities)

    def _hash_key(self, key: str) -> int:
        """Deterministic hash of a string key using SHA-256.

        Args:
            key: String to hash.

        Returns:
            Integer hash value.
        """
        digest = hashlib.sha256(key.encode()).hexdigest()
        return int(digest[:8], 16)
