"""Stealth identity pool — curated browser fingerprints for anti-bot evasion.

Only real Chrome/Firefox/Edge UA strings. No LLM-generated strings.
Sticky keys ensure the same property sees the same browser across runs.
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
    browser_major: int = 124
    # Sec-CH-UA* headers — None for Firefox/Safari (they don't send Client Hints).
    sec_ch_ua: str | None = None
    sec_ch_ua_mobile: str = "?0"
    sec_ch_ua_platform: str | None = None
    sec_ch_ua_platform_version: str | None = None


# Curated list of real browser identities — Chrome, Firefox, Edge on Windows/Mac/Linux
_IDENTITIES: list[Identity] = [
    # 0 — Chrome 124 on Windows 10 (1920×1080)
    Identity(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        accept_language="en-US,en;q=0.9",
        platform="Windows",
        viewport=(1920, 1080),
        browser_family="chrome",
        browser_major=124,
        sec_ch_ua='"Chromium";v="124", "Not-A.Brand";v="99", "Google Chrome";v="124"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"Windows"',
        sec_ch_ua_platform_version='"15.0.0"',
    ),
    # 1 — Chrome 124 on macOS (1440×900)
    Identity(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        accept_language="en-US,en;q=0.9",
        platform="macOS",
        viewport=(1440, 900),
        browser_family="chrome",
        browser_major=124,
        sec_ch_ua='"Chromium";v="124", "Not-A.Brand";v="99", "Google Chrome";v="124"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"macOS"',
        sec_ch_ua_platform_version='"14.4.1"',
    ),
    # 2 — Firefox 125 on Windows 10 (1920×1080)
    Identity(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
            "Gecko/20100101 Firefox/125.0"
        ),
        accept_language="en-US,en;q=0.5",
        platform="Windows",
        viewport=(1920, 1080),
        browser_family="firefox",
        browser_major=125,
        # Firefox does not send Sec-CH-UA headers (Mozilla opted out of Client Hints).
        sec_ch_ua=None,
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform=None,
        sec_ch_ua_platform_version=None,
    ),
    # 3 — Chrome 124 on Linux (1920×1080)
    Identity(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        accept_language="en-US,en;q=0.9",
        platform="Linux",
        viewport=(1920, 1080),
        browser_family="chrome",
        browser_major=124,
        sec_ch_ua='"Chromium";v="124", "Not-A.Brand";v="99", "Google Chrome";v="124"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"Linux"',
        sec_ch_ua_platform_version='""',
    ),
    # 4 — Edge 123 on Windows 10 (1920×1080)
    Identity(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0"
        ),
        accept_language="en-US,en;q=0.9",
        platform="Windows",
        viewport=(1920, 1080),
        browser_family="edge",
        browser_major=123,
        sec_ch_ua='"Microsoft Edge";v="123", "Not-A.Brand";v="99", "Chromium";v="123"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"Windows"',
        sec_ch_ua_platform_version='"15.0.0"',
    ),
    # 5 — Safari 17.4 on macOS (1440×900)
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
        # Safari does not send Sec-CH-UA headers.
        sec_ch_ua=None,
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform=None,
        sec_ch_ua_platform_version=None,
    ),
    # 6 — Chrome 122 on Windows 10 (1366×768)
    Identity(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        accept_language="en-US,en;q=0.9",
        platform="Windows",
        viewport=(1366, 768),
        browser_family="chrome",
        browser_major=122,
        sec_ch_ua='"Chromium";v="122", "Not-A.Brand";v="99", "Google Chrome";v="122"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"Windows"',
        sec_ch_ua_platform_version='"15.0.0"',
    ),
    # 7 — Chrome 123 on macOS (2560×1440)
    Identity(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ),
        accept_language="en-US,en;q=0.9",
        platform="macOS",
        viewport=(2560, 1440),
        browser_family="chrome",
        browser_major=123,
        sec_ch_ua='"Chromium";v="123", "Not-A.Brand";v="99", "Google Chrome";v="123"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"macOS"',
        sec_ch_ua_platform_version='"14.4.1"',
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
