"""WAF clearance cookie persistence.

Stores clearance cookies (``cf_clearance``, ``__cf_bm``, ``srcfh-cookie``,
``_pxhd``, etc.) obtained after a WAF challenge is solved, keyed by
``(host, proxy_ip, ua_hash)`` so the right set of cookies is reinjected on
the next request to the same host through the same proxy and user-agent.

The store is an SQLite database in WAL mode so concurrent shard readers
never block each other. The default location mirrors the existing
``data/state/`` layout used by the frontier and conditional-GET cache.

Typical TTLs:
  - Cloudflare ``cf_clearance``/``__cf_bm``: 30 minutes
  - SiteGround ``srcfh-cookie``/``sg-cookies``: 12 hours
  - PerimeterX ``_pxhd``: 1 hour

Public API (consumed by ``fetch/fetcher.py``)::

    jar = ClearanceJar(db_path)
    # Inject before request
    cookies = jar.lookup(host, proxy_ip, ua_hash)
    # Store after challenge solve (or opportunistic capture from Set-Cookie)
    jar.store(host, proxy_ip, ua_hash, provider, {"cf_clearance": "..."}, 1800)
    # Scheduled cleanup at run boundary
    dropped = jar.purge_expired()
    jar.close()
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# Clearance cookie names we recognise and store opportunistically.  A cookie
# returned in a ``Set-Cookie`` header is only persisted when its name appears
# in this set, preventing general session cookies from polluting the jar.
CLEARANCE_COOKIE_NAMES: frozenset[str] = frozenset({
    "cf_clearance",     # Cloudflare managed-challenge clearance
    "__cf_bm",          # Cloudflare Bot Manager
    "srcfh-cookie",     # SiteGround SGCAPTCHA
    "sg-cookies",       # SiteGround alternative name
    "_pxhd",            # PerimeterX
    "_pxde",            # PerimeterX device token
    "__hssc",           # HubSpot bot guard (session)
    "__hstc",           # HubSpot bot guard (persistent)
})

# Provider → default TTL in seconds when the cookie doesn't carry its own
# ``Expires`` or ``Max-Age`` attribute.
_PROVIDER_DEFAULT_TTL: dict[str, int] = {
    "cloudflare": 1800,     # 30 min
    "sgcaptcha":  43200,    # 12 hours
    "perimeterx": 3600,     # 1 hour
    "hcaptcha":   3600,
    "recaptcha":  3600,
}
_FALLBACK_TTL: int = 1800  # 30 min when provider unknown

_DDL = """
CREATE TABLE IF NOT EXISTS clearance_cookies (
    host          TEXT NOT NULL,
    proxy_ip      TEXT NOT NULL,
    ua_hash       TEXT NOT NULL,
    provider      TEXT NOT NULL,
    cookie_name   TEXT NOT NULL,
    cookie_value  TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    acquired_at   TEXT NOT NULL,
    PRIMARY KEY (host, proxy_ip, ua_hash, cookie_name)
);
CREATE INDEX IF NOT EXISTS ix_jar_lookup
    ON clearance_cookies (host, expires_at);
"""


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _from_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def ua_hash(user_agent: str, accept_language: str = "") -> str:
    """Compute the UA hash key used as part of the clearance-jar primary key.

    The hash covers the User-Agent + Accept-Language pair because Cloudflare
    ties its clearance cookie to that combination (TLS JA3 fingerprint is a
    third factor handled at the proxy tier, not here).

    Args:
        user_agent: The ``User-Agent`` header value.
        accept_language: The ``Accept-Language`` header value (optional).

    Returns:
        A 16-character hex string (first 8 bytes of SHA-256).
    """
    raw = f"{user_agent}|{accept_language}"
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


class ClearanceJar:
    """SQLite-backed WAF clearance cookie store.

    Thread-safe via a per-instance lock.  Each public method acquires the
    lock for the full duration of its SQLite transaction so concurrent
    readers/writers in the same process never see partial state.

    Args:
        db_path: Path to the SQLite database file.  Created (with parents)
            on first use if it does not exist.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._ensure_open()

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _ensure_open(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_DDL)
            conn.commit()
            self._conn = conn
        return self._conn

    # ── Public API ───────────────────────────────────────────────────────────

    def lookup(self, host: str, proxy_ip: str, ua_hash_val: str) -> dict[str, str]:
        """Return active clearance cookies for the (host, proxy, UA) triple.

        Expired rows are deleted lazily on each lookup miss so the table
        doesn't grow unboundedly without needing a separate cron job.

        Args:
            host: Hostname (e.g. ``"example.com"`` — no scheme/port).
            proxy_ip: Exit IP of the residential proxy used to acquire the
                clearance.  Empty string ``""`` for direct connections.
            ua_hash_val: Output of :func:`ua_hash` for the identity used.

        Returns:
            Mapping ``{cookie_name: cookie_value}`` for unexpired rows.
            Empty dict when nothing is cached.
        """
        now_iso = _iso(_now_utc())
        with self._lock:
            conn = self._ensure_open()
            try:
                cursor = conn.execute(
                    """
                    SELECT cookie_name, cookie_value
                    FROM   clearance_cookies
                    WHERE  host = ? AND proxy_ip = ? AND ua_hash = ?
                      AND  expires_at > ?
                    """,
                    (host, proxy_ip, ua_hash_val, now_iso),
                )
                rows = cursor.fetchall()
                # Opportunistic purge: remove expired rows for this key in the
                # same transaction so the table stays tidy without extra round-trips.
                conn.execute(
                    """
                    DELETE FROM clearance_cookies
                    WHERE  host = ? AND proxy_ip = ? AND ua_hash = ?
                      AND  expires_at <= ?
                    """,
                    (host, proxy_ip, ua_hash_val, now_iso),
                )
                conn.commit()
            except sqlite3.Error as exc:
                log.warning("clearance_jar.lookup failed for %s: %s", host, exc)
                return {}
        return {name: value for name, value in rows}

    def store(
        self,
        host: str,
        proxy_ip: str,
        ua_hash_val: str,
        provider: str,
        cookies: dict[str, str],
        ttl_seconds: int,
    ) -> None:
        """Persist freshly-acquired clearance cookies.

        Upserts each cookie individually so callers can store one name at a
        time (e.g. ``cf_clearance`` separately from ``__cf_bm``) without
        clobbering other names for the same key triple.

        Args:
            host: Hostname.
            proxy_ip: Proxy exit IP.
            ua_hash_val: UA hash from :func:`ua_hash`.
            provider: WAF provider name (``"cloudflare"``, ``"sgcaptcha"``, …).
            cookies: ``{name: value}`` mapping of cookies to persist.
            ttl_seconds: Lifetime in seconds from now.
        """
        if not cookies:
            return
        now = _now_utc()
        expires = now + timedelta(seconds=max(ttl_seconds, 1))
        now_iso = _iso(now)
        exp_iso = _iso(expires)
        with self._lock:
            conn = self._ensure_open()
            try:
                conn.executemany(
                    """
                    INSERT INTO clearance_cookies
                        (host, proxy_ip, ua_hash, provider,
                         cookie_name, cookie_value, expires_at, acquired_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(host, proxy_ip, ua_hash, cookie_name)
                    DO UPDATE SET
                        cookie_value = excluded.cookie_value,
                        provider     = excluded.provider,
                        expires_at   = excluded.expires_at,
                        acquired_at  = excluded.acquired_at
                    """,
                    [
                        (host, proxy_ip, ua_hash_val, provider,
                         name, value, exp_iso, now_iso)
                        for name, value in cookies.items()
                    ],
                )
                conn.commit()
                log.debug(
                    "clearance_jar: stored %d cookie(s) for host=%s provider=%s ttl=%ds",
                    len(cookies), host, provider, ttl_seconds,
                )
            except sqlite3.Error as exc:
                log.warning(
                    "clearance_jar.store failed for %s: %s", host, exc
                )

    def purge_expired(self) -> int:
        """Delete all expired rows.

        Intended to be called at run boundaries (start or end) to prevent
        unbounded table growth.  The per-lookup opportunistic delete handles
        the common case; this method is the belt-and-suspenders sweep.

        Returns:
            Number of rows deleted.
        """
        now_iso = _iso(_now_utc())
        with self._lock:
            conn = self._ensure_open()
            try:
                cursor = conn.execute(
                    "DELETE FROM clearance_cookies WHERE expires_at <= ?",
                    (now_iso,),
                )
                conn.commit()
                deleted = cursor.rowcount
            except sqlite3.Error as exc:
                log.warning("clearance_jar.purge_expired failed: %s", exc)
                return 0
        if deleted:
            log.info("clearance_jar: purged %d expired row(s)", deleted)
        return deleted

    def close(self) -> None:
        """Close the database connection.  Idempotent."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    def __enter__(self) -> "ClearanceJar":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ── Utility: parse clearance cookies from HTTP Set-Cookie headers ─────────────


def _extract_clearance_from_set_cookie(
    set_cookie_header: str,
    all_headers: dict[str, str],
) -> dict[str, str]:
    """Parse WAF clearance cookie names/values from Set-Cookie response headers.

    Only cookies whose names appear in ``CLEARANCE_COOKIE_NAMES`` are returned;
    general session cookies are silently ignored.

    Handles both a single ``set-cookie`` header value string (passed directly)
    and a full response-headers dict (some HTTP clients merge multiple
    Set-Cookie lines).

    Args:
        set_cookie_header: The raw ``Set-Cookie`` header value, or ``""`` if
            not available as a scalar.
        all_headers: The complete response headers dict (lowercased keys).
            Any key that starts with ``"set-cookie"`` is included in the scan.

    Returns:
        ``{cookie_name: cookie_value}`` for matched clearance cookies.
        Empty dict when none found.
    """
    result: dict[str, str] = {}
    sources: list[str] = []
    if set_cookie_header:
        sources.append(set_cookie_header)
    for k, v in all_headers.items():
        k_lower = k.lower()
        if k_lower.startswith("set-cookie") and v not in sources:
            sources.append(v)

    for raw in sources:
        # Each semicolon-delimited segment: first is ``name=value``, rest are
        # directives (Expires, Path, Domain, Secure, HttpOnly, SameSite, …).
        for segment in raw.split(";"):
            segment = segment.strip()
            if "=" not in segment:
                continue
            name, _, value = segment.partition("=")
            name = name.strip()
            if name in CLEARANCE_COOKIE_NAMES:
                result[name] = value.strip()
                break  # matched — advance to next source header

    return result
