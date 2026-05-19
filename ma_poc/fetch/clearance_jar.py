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
from dataclasses import dataclass
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
})

# Provider → default TTL in seconds when the cookie doesn't carry its own
# ``Expires`` or ``Max-Age`` attribute.
PROVIDER_DEFAULT_TTL: dict[str, int] = {
    "cloudflare": 1800,     # 30 min
    "sgcaptcha":  43200,    # 12 hours
    "perimeterx": 3600,     # 1 hour
    "hcaptcha":   3600,
    "recaptcha":  3600,
}
FALLBACK_TTL: int = 1800  # 30 min when provider unknown

# Cookie name → WAF provider — used to infer TTL when the captcha detector
# hasn't fired (opportunistic Set-Cookie capture on successful responses).
_COOKIE_NAME_TO_PROVIDER: dict[str, str] = {
    "cf_clearance":  "cloudflare",
    "__cf_bm":       "cloudflare",
    "srcfh-cookie":  "sgcaptcha",
    "sg-cookies":    "sgcaptcha",
    "_pxhd":         "perimeterx",
    "_pxde":         "perimeterx",
}


def provider_from_cookie_name(cookie_name: str) -> str:
    """Infer the WAF provider from a clearance cookie name.

    Returns the provider string (e.g. ``"cloudflare"``) when the name is
    recognised, or ``"unknown"`` otherwise.  Used by the fetcher to
    select the correct default TTL when the captcha detector hasn't run
    (e.g. on a successful 200 response that opportunistically carries a
    clearance cookie in its Set-Cookie header).
    """
    return _COOKIE_NAME_TO_PROVIDER.get(cookie_name, "unknown")

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


def candidate_lookup_hosts(host: str) -> list[str]:
    """Return the request host plus its parent domains, in lookup priority order.

    Cookies set with a ``Domain=`` directive (e.g. ``Domain=.example.com``)
    are stored under the bare domain (``example.com``), but apply to every
    subdomain of that domain. When the next request goes to
    ``app.example.com`` the lookup must still find the cookie. This helper
    enumerates the candidate hosts to query for, walking up the dot chain:

    * ``app.www.example.com`` → ``["app.www.example.com", "www.example.com", "example.com"]``
    * ``example.com``         → ``["example.com"]``
    * ``co.uk``               → ``["co.uk"]``  (we stop before reducing to a single label)

    No eTLD+1 awareness — that needs a public-suffix list which is overkill
    for this codebase's WAF-clearance use case. The 2-label floor prevents
    walking into bare TLD space (``com``) where any cookie match would be
    spurious.
    """
    if not host:
        return [""]
    parts = host.split(".")
    if len(parts) <= 2:
        return [host]
    # Walk parents one label at a time, stopping when only two labels remain.
    return [host, *[".".join(parts[i:]) for i in range(1, len(parts) - 1)]]


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

        Cookies set with a ``Domain=`` directive (e.g. ``Domain=.example.com``)
        apply to every subdomain. The lookup walks the request host's dot
        chain via :func:`candidate_lookup_hosts` so a cookie stored under
        ``example.com`` is found for requests to ``app.example.com``.

        When multiple candidate hosts have an entry for the same cookie
        name, the most-specific match wins (``app.example.com`` over
        ``example.com``). This mirrors RFC 6265 cookie precedence.

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
        hosts = candidate_lookup_hosts(host)
        if not hosts:
            return {}
        now_iso = _iso(_now_utc())
        placeholders = ",".join("?" * len(hosts))
        with self._lock:
            conn = self._ensure_open()
            try:
                cursor = conn.execute(
                    f"""
                    SELECT host, cookie_name, cookie_value
                    FROM   clearance_cookies
                    WHERE  host IN ({placeholders})
                      AND  proxy_ip = ? AND ua_hash = ?
                      AND  expires_at > ?
                    """,
                    (*hosts, proxy_ip, ua_hash_val, now_iso),
                )
                rows = cursor.fetchall()
                # Opportunistic purge: remove expired rows for ANY of the
                # candidate hosts in the same transaction so the table
                # stays tidy without extra round-trips.
                conn.execute(
                    f"""
                    DELETE FROM clearance_cookies
                    WHERE  host IN ({placeholders})
                      AND  proxy_ip = ? AND ua_hash = ?
                      AND  expires_at <= ?
                    """,
                    (*hosts, proxy_ip, ua_hash_val, now_iso),
                )
                conn.commit()
            except sqlite3.Error as exc:
                log.warning("clearance_jar.lookup failed for %s: %s", host, exc)
                return {}
        # Most-specific host wins on collision. ``hosts`` is ordered
        # most-specific-first by candidate_lookup_hosts; iterate rows and
        # keep the entry whose host index is smallest per cookie name.
        host_rank = {h: i for i, h in enumerate(hosts)}
        best: dict[str, tuple[int, str]] = {}
        for row_host, name, value in rows:
            rank = host_rank.get(row_host, len(host_rank))
            existing = best.get(name)
            if existing is None or rank < existing[0]:
                best[name] = (rank, value)
        return {name: value for name, (_, value) in best.items()}

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


@dataclass(frozen=True)
class ParsedSetCookie:
    """A single Set-Cookie line, parsed enough to drive the jar.

    The producer's ``Domain`` and ``Max-Age`` directives let the caller
    store the cookie under the correct effective scope and TTL rather than
    falling back to the request host + ``PROVIDER_DEFAULT_TTL``. Both are
    optional; ``None`` means "directive absent — caller decides".
    """

    name: str
    value: str
    #: The ``Domain=`` directive (leading dot stripped, lowercased), or
    #: ``None`` when the cookie is host-only.
    domain: str | None
    #: Seconds remaining from the ``Max-Age`` directive (preferred per
    #: RFC 6265), or parsed from ``Expires`` as a fallback. ``None`` when
    #: neither directive is present.
    max_age_seconds: int | None


# Common HTTP-date formats producers emit in the ``Expires`` directive.
# Each format is tried in order; the first that parses wins. The full
# RFC 1123 format is the most common; the others cover legacy producers.
_EXPIRES_FORMATS: tuple[str, ...] = (
    "%a, %d %b %Y %H:%M:%S GMT",        # RFC 1123 (Wed, 21 Oct 2026 07:28:00 GMT)
    "%A, %d-%b-%y %H:%M:%S GMT",        # RFC 850   (Wednesday, 21-Oct-26 07:28:00 GMT)
    "%a %b %d %H:%M:%S %Y",             # asctime  (Wed Oct 21 07:28:00 2026)
)


def _iter_set_cookie_lines(headers: dict[str, str]) -> list[str]:
    """Yield every ``Set-Cookie`` value from a response-headers dict.

    Tolerates mixed-case keys (``Set-Cookie``, ``set-cookie``, ``SET-COOKIE``);
    different HTTP clients normalise differently. Some clients also merge
    multiple Set-Cookie lines onto a single value with newlines or commas;
    we split on newlines but not commas (commas legally appear inside
    ``Expires=Wed, 21 Oct 2026 ...`` and splitting there would corrupt the
    directive).
    """
    lines: list[str] = []
    seen: set[str] = set()
    for k, v in headers.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if k.lower().startswith("set-cookie") and v:
            for part in v.split("\n"):
                part = part.strip()
                if part and part not in seen:
                    seen.add(part)
                    lines.append(part)
    return lines


def _parse_max_age(directives: list[tuple[str, str]]) -> int | None:
    """Return TTL seconds parsed from ``Max-Age`` (preferred) or ``Expires``.

    Per RFC 6265 §5.3 Max-Age takes precedence over Expires when both are
    present. Returns ``None`` when neither parses to a positive integer.
    """
    for k, v in directives:
        if k == "max-age":
            try:
                n = int(v)
                return n if n > 0 else None
            except (TypeError, ValueError):
                return None
    for k, v in directives:
        if k == "expires":
            for fmt in _EXPIRES_FORMATS:
                try:
                    expires = datetime.strptime(v, fmt).replace(tzinfo=UTC)
                    delta = int((expires - _now_utc()).total_seconds())
                    return delta if delta > 0 else None
                except (TypeError, ValueError):
                    continue
    return None


def _parse_domain(directives: list[tuple[str, str]]) -> str | None:
    """Return the normalised ``Domain`` directive value, or ``None`` if absent.

    Normalisation strips a single leading dot (legacy syntax — RFC 6265
    treats ``.example.com`` and ``example.com`` as equivalent) and
    lowercases the result.
    """
    for k, v in directives:
        if k == "domain":
            d = v.strip().lower()
            if d.startswith("."):
                d = d[1:]
            return d or None
    return None


def parse_set_cookie_clearance(headers: dict[str, str]) -> list[ParsedSetCookie]:
    """Parse WAF clearance cookies from a response-headers dict.

    Only cookies whose names appear in :data:`CLEARANCE_COOKIE_NAMES` are
    returned; general session cookies are silently ignored. Each result
    carries the parsed ``Domain`` and ``Max-Age`` directives so the caller
    can store the cookie under its effective scope and TTL.

    Header-key matching is case-insensitive (different HTTP clients
    normalise the ``Set-Cookie`` key inconsistently); see
    :func:`_iter_set_cookie_lines` for the value-extraction rules.

    Args:
        headers: The complete response headers dict.

    Returns:
        A list of :class:`ParsedSetCookie`, one per matched clearance
        cookie. Empty list when none found. Order matches header iteration.
    """
    out: list[ParsedSetCookie] = []
    for line in _iter_set_cookie_lines(headers):
        segments = [s.strip() for s in line.split(";") if s.strip()]
        if not segments:
            continue
        head, *rest = segments
        if "=" not in head:
            continue
        name, _, value = head.partition("=")
        name = name.strip()
        if name not in CLEARANCE_COOKIE_NAMES:
            continue
        directives: list[tuple[str, str]] = []
        for seg in rest:
            if "=" in seg:
                k, _, v = seg.partition("=")
                directives.append((k.strip().lower(), v.strip()))
            else:
                directives.append((seg.lower(), ""))
        out.append(ParsedSetCookie(
            name=name,
            value=value.strip(),
            domain=_parse_domain(directives),
            max_age_seconds=_parse_max_age(directives),
        ))
    return out


def extract_clearance_from_set_cookie(
    set_cookie_header: str,
    all_headers: dict[str, str],
) -> dict[str, str]:
    """Back-compat shim — returns the bare ``{name: value}`` map.

    New callers should use :func:`parse_set_cookie_clearance` directly to
    receive ``Domain`` + ``Max-Age`` parse results alongside each cookie.
    This shim survives so the existing test surface keeps passing during
    rollout; the fetcher integration already migrated to the richer API.
    """
    # Merge the scalar arg into a synthetic headers dict so a single
    # parser path handles both call shapes. Caller may pass either, both,
    # or neither.
    merged: dict[str, str] = {}
    if isinstance(all_headers, dict):
        merged.update(all_headers)
    if set_cookie_header:
        # If there's already a Set-Cookie key, append; otherwise inject.
        existing = ""
        for k in list(merged.keys()):
            if isinstance(k, str) and k.lower() == "set-cookie":
                existing = merged.pop(k)
                break
        merged["set-cookie"] = (
            f"{existing}\n{set_cookie_header}" if existing else set_cookie_header
        )
    return {c.name: c.value for c in parse_set_cookie_clearance(merged)}
