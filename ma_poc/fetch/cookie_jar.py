"""Per-property cookie persistence.

Cookies live in ``data/cache/cookies/{property_id}_{slot}.json``. The slot is
the identity slot index (0..7); separating slots prevents cross-identity
cookie contamination — when retry rotates identity, the new identity gets
its own fresh jar so a banned identity's cookies don't follow.

Storage format::

    {
        "host_or_domain": {"cookie_name": "cookie_value", ...},
        ...
    }

Best-effort writes: a failed save logs and proceeds. The next fetch will
just look like a cold visit, which is correct fail-soft behaviour.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Module-level lock per-jar-path; avoids two parallel fetches racing the file.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _get_lock(path: str) -> threading.Lock:
    with _locks_guard:
        if path not in _locks:
            _locks[path] = threading.Lock()
        return _locks[path]


class PropertyCookieJar:
    """Per-(property_id, identity_slot) cookie jar.

    Args:
        property_id: Stable property identifier.
        identity_slot: Identity index in the pool (0..N-1). Determines the
            jar file so identity rotation doesn't share cookies.
        base_dir: Root storage directory. Defaults to ``data/cache/cookies/``.
    """

    def __init__(
        self,
        property_id: str,
        identity_slot: int,
        base_dir: Path | None = None,
    ) -> None:
        self._property_id = property_id
        self._slot = identity_slot
        self._base_dir = base_dir or Path(
            os.getenv("DATA_DIR", "ma_poc/data")
        ) / "cache" / "cookies"
        self._path = self._base_dir / f"{property_id}_{identity_slot}.json"
        self._cache: dict[str, dict[str, str]] | None = None

    def load(self) -> dict[str, dict[str, str]]:
        """Load all cookies. Returns empty dict on miss/error (fail-soft)."""
        if self._cache is not None:
            return self._cache
        try:
            text = self._path.read_text(encoding="utf-8")
            self._cache = json.loads(text)
        except (FileNotFoundError, json.JSONDecodeError):
            self._cache = {}
        except Exception as exc:
            log.debug("cookie jar load failed for %s: %s", self._path, exc)
            self._cache = {}
        return self._cache

    def cookies_for_host(self, host: str) -> dict[str, str]:
        """Return cookies applicable to the given host.

        Matches both exact host and parent-domain entries (basic — no
        full RFC 6265 path matching; sufficient for our use case).
        """
        all_cookies = self.load()
        result: dict[str, str] = {}
        for stored_host, jar in all_cookies.items():
            if host == stored_host or host.endswith("." + stored_host):
                result.update(jar)
        return result

    def update_from_response(self, host: str, set_cookie_pairs: dict[str, str]) -> None:
        """Merge new cookies from a response into the jar.

        Args:
            host: The host the response came from.
            set_cookie_pairs: Cookies parsed from Set-Cookie headers as
                ``{name: value}``. Domain/path attributes are not parsed —
                we use the originating host as the key.
        """
        if not set_cookie_pairs:
            return
        all_cookies = self.load()
        existing = all_cookies.setdefault(host, {})
        existing.update(set_cookie_pairs)
        self._cache = all_cookies
        self._save()

    def _save(self) -> None:
        """Write to disk under a per-path lock. Best-effort."""
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            with _get_lock(str(self._path)):
                self._path.write_text(
                    json.dumps(self._cache or {}, sort_keys=True),
                    encoding="utf-8",
                )
        except Exception as exc:
            log.debug("cookie jar save failed for %s: %s", self._path, exc)
