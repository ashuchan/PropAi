"""Tests for the WAF clearance cookie jar (Phase 2 — stealth plan).

Verifies:
  - TTL expiry (expired rows not returned, purge_expired deletes them)
  - (host, proxy_ip, ua_hash) key uniqueness — different keys don't share cookies
  - Concurrent-write safety (SQLite WAL mode; two jars on the same DB)
  - Upsert semantics (store updates value + expiry for existing key)
  - ua_hash() stability — same input produces the same output
  - extract_clearance_from_set_cookie() parses the known cookie names
  - close() / context-manager behaviour
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ma_poc.fetch.clearance_jar import (
    CLEARANCE_COOKIE_NAMES,
    ClearanceJar,
    ParsedSetCookie,
    candidate_lookup_hosts,
    extract_clearance_from_set_cookie,
    parse_set_cookie_clearance,
    ua_hash,
)


# ── ua_hash() ────────────────────────────────────────────────────────────────


def test_ua_hash_stable():
    h1 = ua_hash("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/136", "en-US,en;q=0.9")
    h2 = ua_hash("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/136", "en-US,en;q=0.9")
    assert h1 == h2


def test_ua_hash_different_ua_yields_different_hash():
    h1 = ua_hash("Mozilla/5.0 Chrome/136", "en-US")
    h2 = ua_hash("Mozilla/5.0 Firefox/137", "en-US")
    assert h1 != h2


def test_ua_hash_different_lang_yields_different_hash():
    h1 = ua_hash("Mozilla/5.0 Chrome/136", "en-US")
    h2 = ua_hash("Mozilla/5.0 Chrome/136", "de-DE")
    assert h1 != h2


def test_ua_hash_empty_inputs_stable():
    h = ua_hash("", "")
    assert isinstance(h, str) and len(h) == 16


def test_ua_hash_length():
    h = ua_hash("ua", "lang")
    assert len(h) == 16


# ── ClearanceJar basic CRUD ──────────────────────────────────────────────────


def test_store_and_lookup_roundtrip(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("example.com", "1.2.3.4", "abc123", "cloudflare",
               {"cf_clearance": "TOKEN"}, ttl_seconds=3600)
    result = jar.lookup("example.com", "1.2.3.4", "abc123")
    assert result == {"cf_clearance": "TOKEN"}
    jar.close()


def test_lookup_returns_empty_for_unknown_key(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    result = jar.lookup("nonexistent.com", "", "xyz")
    assert result == {}
    jar.close()


def test_upsert_updates_value(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("site.com", "", "uh1", "cloudflare", {"cf_clearance": "OLD"}, 3600)
    jar.store("site.com", "", "uh1", "cloudflare", {"cf_clearance": "NEW"}, 3600)
    result = jar.lookup("site.com", "", "uh1")
    assert result["cf_clearance"] == "NEW"
    jar.close()


def test_store_multiple_cookies_same_key(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    cookies = {"cf_clearance": "TOK1", "__cf_bm": "TOK2"}
    jar.store("multi.com", "10.0.0.1", "uh2", "cloudflare", cookies, 1800)
    result = jar.lookup("multi.com", "10.0.0.1", "uh2")
    assert result == cookies
    jar.close()


# ── Key isolation ────────────────────────────────────────────────────────────


def test_different_host_does_not_share_cookies(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("a.com", "1.1.1.1", "uh", "cloudflare", {"cf_clearance": "A"}, 3600)
    result = jar.lookup("b.com", "1.1.1.1", "uh")
    assert result == {}
    jar.close()


def test_different_proxy_ip_does_not_share_cookies(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("x.com", "1.1.1.1", "uh", "cloudflare", {"cf_clearance": "A"}, 3600)
    result = jar.lookup("x.com", "2.2.2.2", "uh")
    assert result == {}
    jar.close()


def test_different_ua_hash_does_not_share_cookies(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("y.com", "", "hash_a", "cloudflare", {"cf_clearance": "X"}, 3600)
    result = jar.lookup("y.com", "", "hash_b")
    assert result == {}
    jar.close()


# ── TTL expiry ───────────────────────────────────────────────────────────────


def test_expired_cookie_not_returned(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    # Store with a 1-second TTL — insert directly with past expiry.
    jar.store("ttl.com", "", "uh", "cloudflare", {"cf_clearance": "EXP"}, ttl_seconds=1)
    # Monkey-patch the DB row to have already expired.
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "jar.sqlite"))
    conn.execute(
        "UPDATE clearance_cookies SET expires_at = ? WHERE host = ?",
        ((datetime.now(UTC) - timedelta(seconds=10)).isoformat(), "ttl.com"),
    )
    conn.commit()
    conn.close()
    result = jar.lookup("ttl.com", "", "uh")
    assert result == {}
    jar.close()


def test_purge_expired_removes_stale_rows(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("purge.com", "", "uh", "cloudflare", {"cf_clearance": "X"}, 3600)
    # Force the expiry to be in the past via direct SQL.
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "jar.sqlite"))
    conn.execute(
        "UPDATE clearance_cookies SET expires_at = ? WHERE host = ?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "purge.com"),
    )
    conn.commit()
    conn.close()
    deleted = jar.purge_expired()
    assert deleted == 1
    # Confirm row is gone.
    result = jar.lookup("purge.com", "", "uh")
    assert result == {}
    jar.close()


def test_purge_expired_does_not_touch_live_rows(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("live.com", "", "uh", "cloudflare", {"cf_clearance": "Y"}, 7200)
    deleted = jar.purge_expired()
    assert deleted == 0
    result = jar.lookup("live.com", "", "uh")
    assert result == {"cf_clearance": "Y"}
    jar.close()


# ── Concurrent-write safety ──────────────────────────────────────────────────


def test_concurrent_writes_are_safe(tmp_path: Path):
    """Two jar instances writing to the same SQLite file concurrently should
    not raise or corrupt data.  SQLite WAL mode handles the serialisation."""
    db = tmp_path / "concurrent.sqlite"
    errors: list[Exception] = []

    def _writer(n: int) -> None:
        j = ClearanceJar(db)
        try:
            for i in range(10):
                j.store(
                    f"host{n}-{i}.com", "", "uh", "cloudflare",
                    {"cf_clearance": f"T{n}-{i}"}, 3600,
                )
        except Exception as exc:
            errors.append(exc)
        finally:
            j.close()

    threads = [threading.Thread(target=_writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent write errors: {errors}"


# ── extract_clearance_from_set_cookie ───────────────────────────────────────


def test_extract_cf_clearance():
    hdr = "cf_clearance=TOK123; Path=/; Secure; HttpOnly; SameSite=None; Max-Age=1800"
    result = extract_clearance_from_set_cookie(hdr, {})
    assert result == {"cf_clearance": "TOK123"}


def test_extract_cf_bm():
    hdr = "__cf_bm=BMBM; Path=/; Secure; SameSite=None"
    result = extract_clearance_from_set_cookie(hdr, {})
    assert result == {"__cf_bm": "BMBM"}


def test_extract_ignores_non_clearance_cookies():
    hdr = "sessionid=abc123; Path=/; HttpOnly"
    result = extract_clearance_from_set_cookie(hdr, {})
    assert result == {}


def test_extract_from_headers_dict():
    headers = {
        "set-cookie": "cf_clearance=DICT_TOK; Path=/; Secure",
        "content-type": "text/html",
    }
    result = extract_clearance_from_set_cookie("", headers)
    assert result.get("cf_clearance") == "DICT_TOK"


def test_extract_empty_inputs():
    assert extract_clearance_from_set_cookie("", {}) == {}


def test_clearance_cookie_names_set_includes_known_names():
    assert "cf_clearance" in CLEARANCE_COOKIE_NAMES
    assert "__cf_bm" in CLEARANCE_COOKIE_NAMES
    assert "_pxhd" in CLEARANCE_COOKIE_NAMES
    assert "srcfh-cookie" in CLEARANCE_COOKIE_NAMES


# ── close() and context-manager ──────────────────────────────────────────────


def test_close_is_idempotent(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.close()
    jar.close()  # Should not raise.


def test_context_manager_closes_on_exit(tmp_path: Path):
    with ClearanceJar(tmp_path / "jar.sqlite") as jar:
        jar.store("ctx.com", "", "uh", "cloudflare", {"cf_clearance": "CTX"}, 3600)
    # Verify connection is closed by re-opening and confirming data is there.
    j2 = ClearanceJar(tmp_path / "jar.sqlite")
    assert j2.lookup("ctx.com", "", "uh") == {"cf_clearance": "CTX"}
    j2.close()


# ── candidate_lookup_hosts: parent-domain walk ──────────────────────────────


def test_candidate_lookup_hosts_single_host_returns_self():
    assert candidate_lookup_hosts("example.com") == ["example.com"]


def test_candidate_lookup_hosts_walks_subdomains():
    """``app.example.com`` lookup must also check ``example.com`` so a
    cookie set with ``Domain=.example.com`` is found."""
    out = candidate_lookup_hosts("app.example.com")
    assert out == ["app.example.com", "example.com"]


def test_candidate_lookup_hosts_walks_multiple_levels():
    out = candidate_lookup_hosts("app.www.example.com")
    assert out == ["app.www.example.com", "www.example.com", "example.com"]


def test_candidate_lookup_hosts_stops_before_bare_tld():
    """We never reduce to a single-label host — ``com``/``uk`` would
    match every cookie in the jar."""
    out = candidate_lookup_hosts("example.com")
    assert "com" not in out
    out = candidate_lookup_hosts("foo.bar.co.uk")
    assert "uk" not in out
    assert "co.uk" in out  # crude eTLD shortcoming, documented


def test_candidate_lookup_hosts_empty_input():
    assert candidate_lookup_hosts("") == [""]


# ── Parent-domain lookup against the live jar ───────────────────────────────


def test_lookup_finds_cookie_via_parent_domain(tmp_path: Path):
    """Cookie stored under bare domain must be found for subdomain lookup.

    This is the canonical Cloudflare pattern: ``Set-Cookie: cf_clearance=...
    ; Domain=.example.com`` on a response to ``app.example.com`` means
    the cookie applies to every ``*.example.com`` subdomain.
    """
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("example.com", "", "uh", "cloudflare", {"cf_clearance": "PARENT"}, 3600)
    # Lookup from subdomain — must find the parent's cookie.
    assert jar.lookup("app.example.com", "", "uh") == {"cf_clearance": "PARENT"}
    assert jar.lookup("api.www.example.com", "", "uh") == {"cf_clearance": "PARENT"}
    jar.close()


def test_lookup_does_not_leak_across_unrelated_domains(tmp_path: Path):
    """A cookie under ``example.com`` must NOT be returned for
    ``other.com`` even though both have the same number of labels."""
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("example.com", "", "uh", "cloudflare", {"cf_clearance": "X"}, 3600)
    assert jar.lookup("other.com", "", "uh") == {}
    assert jar.lookup("notexample.com", "", "uh") == {}
    jar.close()


def test_lookup_specific_host_wins_over_parent(tmp_path: Path):
    """When both ``app.example.com`` and ``example.com`` have a cookie
    of the same name, the more-specific host wins (RFC 6265 precedence)."""
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("example.com", "", "uh", "cloudflare", {"cf_clearance": "PARENT"}, 3600)
    jar.store("app.example.com", "", "uh", "cloudflare", {"cf_clearance": "CHILD"}, 3600)
    assert jar.lookup("app.example.com", "", "uh") == {"cf_clearance": "CHILD"}
    # And a sibling subdomain still picks up the parent.
    assert jar.lookup("api.example.com", "", "uh") == {"cf_clearance": "PARENT"}
    jar.close()


# ── parse_set_cookie_clearance: rich parse with Domain + Max-Age ─────────────


def test_parse_set_cookie_basic():
    headers = {"set-cookie": "cf_clearance=TOK1; Path=/; Secure; HttpOnly"}
    out = parse_set_cookie_clearance(headers)
    assert len(out) == 1
    assert out[0] == ParsedSetCookie(
        name="cf_clearance", value="TOK1", domain=None, max_age_seconds=None,
    )


def test_parse_set_cookie_domain_directive_strips_leading_dot():
    headers = {
        "set-cookie": "cf_clearance=T; Domain=.example.com; Path=/; Secure",
    }
    out = parse_set_cookie_clearance(headers)
    assert out[0].domain == "example.com"


def test_parse_set_cookie_domain_directive_lowercased():
    headers = {"set-cookie": "cf_clearance=T; Domain=Example.COM; Path=/"}
    out = parse_set_cookie_clearance(headers)
    assert out[0].domain == "example.com"


def test_parse_set_cookie_max_age_overrides_provider_default():
    headers = {"set-cookie": "cf_clearance=T; Max-Age=43200; Path=/"}
    out = parse_set_cookie_clearance(headers)
    assert out[0].max_age_seconds == 43200


def test_parse_set_cookie_max_age_takes_precedence_over_expires():
    """RFC 6265 §5.3 — Max-Age wins when both directives present."""
    headers = {
        "set-cookie": (
            "cf_clearance=T; Expires=Wed, 21 Oct 2099 07:28:00 GMT; "
            "Max-Age=600; Path=/"
        ),
    }
    out = parse_set_cookie_clearance(headers)
    assert out[0].max_age_seconds == 600


def test_parse_set_cookie_expires_fallback_when_no_max_age():
    """When only ``Expires=`` is present, parse it as a relative TTL."""
    future = datetime.now(UTC) + timedelta(hours=2)
    expires_str = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    headers = {"set-cookie": f"cf_clearance=T; Expires={expires_str}; Path=/"}
    out = parse_set_cookie_clearance(headers)
    # Should be ~7200 seconds, allow ±5 for clock drift in the test.
    assert out[0].max_age_seconds is not None
    assert 7195 <= out[0].max_age_seconds <= 7200


def test_parse_set_cookie_expires_in_past_returns_none():
    """An already-expired ``Expires=`` directive doesn't yield a positive
    TTL — caller falls back to provider default."""
    past = datetime.now(UTC) - timedelta(days=1)
    expires_str = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
    headers = {"set-cookie": f"cf_clearance=T; Expires={expires_str}"}
    out = parse_set_cookie_clearance(headers)
    assert out[0].max_age_seconds is None


def test_parse_set_cookie_malformed_max_age_ignored():
    headers = {"set-cookie": "cf_clearance=T; Max-Age=not-a-number"}
    out = parse_set_cookie_clearance(headers)
    assert out[0].max_age_seconds is None


def test_parse_set_cookie_case_insensitive_header_key():
    """Different HTTP clients capitalise Set-Cookie differently —
    must match all of ``Set-Cookie`` / ``set-cookie`` / ``SET-COOKIE``."""
    for key in ("Set-Cookie", "set-cookie", "SET-COOKIE", "Set-cookie"):
        headers = {key: "cf_clearance=T; Path=/"}
        out = parse_set_cookie_clearance(headers)
        assert len(out) == 1, f"header key {key!r} failed to match"
        assert out[0].name == "cf_clearance"


def test_parse_set_cookie_multiple_lines_via_newline():
    """Some clients merge multiple Set-Cookie lines on a single value
    separated by newlines. Comma-separation is NOT safe (commas appear
    inside ``Expires=Wed, 21 Oct ...``)."""
    headers = {
        "set-cookie": (
            "cf_clearance=T1; Path=/\n"
            "__cf_bm=T2; Path=/"
        ),
    }
    out = parse_set_cookie_clearance(headers)
    names = sorted(c.name for c in out)
    assert names == ["__cf_bm", "cf_clearance"]


def test_parse_set_cookie_ignores_non_clearance_names():
    headers = {
        "set-cookie": "sessionid=abc; Path=/\ncf_clearance=T; Path=/",
    }
    out = parse_set_cookie_clearance(headers)
    assert len(out) == 1
    assert out[0].name == "cf_clearance"


def test_parse_set_cookie_empty_input():
    assert parse_set_cookie_clearance({}) == []
    assert parse_set_cookie_clearance({"content-type": "text/html"}) == []


# ── Back-compat shim sanity ─────────────────────────────────────────────────


def test_extract_clearance_shim_returns_bare_map():
    """The legacy dict-returning API still works for callers that haven't
    migrated to :func:`parse_set_cookie_clearance`."""
    out = extract_clearance_from_set_cookie(
        "cf_clearance=SHIM; Path=/; Secure", {}
    )
    assert out == {"cf_clearance": "SHIM"}


def test_extract_clearance_shim_handles_mixed_case_header_key():
    """The shim's case-insensitive contract — both ``Set-Cookie`` and
    ``set-cookie`` must yield the cookie regardless of which the client
    emits."""
    for key in ("Set-Cookie", "set-cookie", "SET-COOKIE"):
        out = extract_clearance_from_set_cookie(
            "", {key: "cf_clearance=CASE; Path=/"}
        )
        assert out == {"cf_clearance": "CASE"}, f"failed for key {key!r}"
