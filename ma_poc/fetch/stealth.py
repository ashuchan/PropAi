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
    """A browser identity used for stealth."""

    user_agent: str
    accept_language: str
    platform: str
    viewport: tuple[int, int]


# Curated list of real browser identities — Chrome, Firefox, Edge on Windows/Mac/Linux
_IDENTITIES: list[Identity] = [
    Identity(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        accept_language="en-US,en;q=0.9",
        platform="Windows",
        viewport=(1920, 1080),
    ),
    Identity(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        accept_language="en-US,en;q=0.9",
        platform="macOS",
        viewport=(1440, 900),
    ),
    Identity(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        accept_language="en-US,en;q=0.5",
        platform="Windows",
        viewport=(1920, 1080),
    ),
    Identity(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        accept_language="en-US,en;q=0.9",
        platform="Linux",
        viewport=(1920, 1080),
    ),
    Identity(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
        accept_language="en-US,en;q=0.9",
        platform="Windows",
        viewport=(1920, 1080),
    ),
    Identity(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        accept_language="en-US,en;q=0.9",
        platform="macOS",
        viewport=(1440, 900),
    ),
    Identity(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        accept_language="en-US,en;q=0.9",
        platform="Windows",
        viewport=(1366, 768),
    ),
    Identity(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        accept_language="en-US,en;q=0.9",
        platform="macOS",
        viewport=(2560, 1440),
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

    def rotate(self, sticky_key: str) -> None:
        """Rotate to a different identity for the given key.

        Args:
            sticky_key: The key (typically property_id) to rotate.
        """
        self._rotations[sticky_key] = self._rotations.get(sticky_key, 0) + 1
        log.info("Rotated identity for %s (rotation=%d)", sticky_key, self._rotations[sticky_key])

    def _hash_key(self, key: str) -> int:
        """Deterministic hash of a string key using SHA-256.

        Args:
            key: String to hash.

        Returns:
            Integer hash value.
        """
        digest = hashlib.sha256(key.encode()).hexdigest()
        return int(digest[:8], 16)
